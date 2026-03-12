from __future__ import annotations

import logging
import math
import os
from typing import Dict, List, Optional

from dotenv import load_dotenv

from execution.broker_interface import BrokerInterface
from execution.models import (
    AccountInfo, Order, OrderSide, OrderStatus, OrderType, Position,
)

logger = logging.getLogger(__name__)

FUTURES_TESTNET_BASE = "https://testnet.binancefuture.com"
FUTURES_LIVE_BASE = "https://fapi.binance.com"


def _safe_float(val, default: float = 0.0) -> float:
    """Convert value to float, returning *default* for empty/invalid strings."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class BinanceBroker(BrokerInterface):
    """Binance Futures broker using python-binance AsyncClient."""

    def __init__(self) -> None:
        load_dotenv()
        self._api_key = os.getenv("BINANCE_API_KEY", "")
        self._api_secret = os.getenv("BINANCE_API_SECRET", "")
        self._testnet = os.getenv("BINANCE_TESTNET", "true").lower() == "true"
        self._client = None
        self._exchange_info: Dict[str, dict] = {}

    async def connect(self) -> bool:
        from binance import AsyncClient

        try:
            self._client = await AsyncClient.create(
                self._api_key,
                self._api_secret,
                testnet=self._testnet,
            )
            if self._testnet:
                self._client.FUTURES_URL = FUTURES_TESTNET_BASE + "/fapi"

            info = await self._client.futures_exchange_info()
            for s in info.get("symbols", []):
                self._exchange_info[s["symbol"]] = s

            mode_label = "TESTNET" if self._testnet else "LIVE"
            logger.info("Binance Futures connected (%s)", mode_label)
            return True

        except Exception as e:
            logger.error("Binance connect failed: %s", e)
            self._client = None
            return False

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close_connection()
            self._client = None
            logger.info("Binance disconnected")

    def is_connected(self) -> bool:
        return self._client is not None

    async def get_account_info(self) -> AccountInfo:
        if not self._client:
            return AccountInfo()
        try:
            acct = await self._client.futures_account()
            balance = _safe_float(acct.get("totalWalletBalance"))
            available = _safe_float(acct.get("availableBalance"))
            positions = []
            for p in acct.get("positions", []):
                qty = _safe_float(p.get("positionAmt"))
                if qty == 0:
                    continue
                positions.append(Position(
                    symbol=p.get("symbol", ""),
                    side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                    quantity=abs(qty),
                    entry_price=_safe_float(p.get("entryPrice")),
                    unrealized_pnl=_safe_float(p.get("unrealizedProfit")),
                ))
            return AccountInfo(balance=balance, available_balance=available, positions=positions)
        except Exception as e:
            logger.error("get_account_info failed: %s", e)
            return AccountInfo()

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
    ) -> Order:
        order = Order(symbol=symbol, side=side, quantity=quantity, order_type=order_type)
        if not self._client:
            order.status = OrderStatus.REJECTED
            order.error_message = "Not connected"
            return order

        try:
            rounded_qty = self._round_quantity(symbol, quantity)
            if rounded_qty <= 0:
                order.status = OrderStatus.REJECTED
                order.error_message = f"Quantity {quantity} rounds to 0 for {symbol}"
                return order

            params = dict(
                symbol=symbol,
                side=side.value,
                type=order_type.value,
                quantity=str(rounded_qty),
                newOrderRespType="RESULT",
            )
            logger.info("Placing order: %s", params)
            result = await self._client.futures_create_order(**params)

            order.broker_order_id = str(result.get("orderId", ""))
            order.status = self._map_status(result.get("status", ""))
            order.fill_price = _safe_float(result.get("avgPrice"))
            order.fill_quantity = _safe_float(result.get("executedQty"))

            if order.fill_price == 0 and order.fill_quantity > 0:
                order.fill_price = _safe_float(result.get("price"))

            if order.status == OrderStatus.SUBMITTED and order.fill_quantity == 0:
                await self._poll_order_fill(order, symbol)

            logger.info("Order %s: id=%s price=%.2f qty=%.6f",
                        order.status.value, order.broker_order_id,
                        order.fill_price, order.fill_quantity)
            return order

        except Exception as e:
            order.status = OrderStatus.REJECTED
            order.error_message = str(e)
            logger.error("place_order failed: %s", e)
            return order

    async def _poll_order_fill(self, order: Order, symbol: str, max_attempts: int = 5) -> None:
        import asyncio
        for _ in range(max_attempts):
            await asyncio.sleep(0.3)
            try:
                result = await self._client.futures_get_order(
                    symbol=symbol, orderId=int(order.broker_order_id)
                )
                order.status = self._map_status(result.get("status", ""))
                order.fill_price = _safe_float(result.get("avgPrice"))
                order.fill_quantity = _safe_float(result.get("executedQty"))
                if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    return
            except Exception as e:
                logger.warning("Poll order failed: %s", e)

    async def cancel_order(self, symbol: str, broker_order_id: str) -> bool:
        if not self._client:
            return False
        try:
            await self._client.futures_cancel_order(symbol=symbol, orderId=int(broker_order_id))
            logger.info("Cancelled order %s", broker_order_id)
            return True
        except Exception as e:
            logger.error("cancel_order failed: %s", e)
            return False

    async def get_open_orders(self, symbol: str) -> List[Order]:
        if not self._client:
            return []
        try:
            raw = await self._client.futures_get_open_orders(symbol=symbol)
            orders = []
            for r in raw:
                orders.append(Order(
                    broker_order_id=str(r.get("orderId", "")),
                    symbol=r.get("symbol", ""),
                    side=OrderSide(r.get("side", "BUY")),
                    quantity=_safe_float(r.get("origQty")),
                    order_type=OrderType(r.get("type", "MARKET")),
                    status=self._map_status(r.get("status", "")),
                ))
            return orders
        except Exception as e:
            logger.error("get_open_orders failed: %s", e)
            return []

    async def get_position(self, symbol: str) -> Optional[Position]:
        if not self._client:
            return None
        try:
            positions = await self._client.futures_position_information(symbol=symbol)
            for p in positions:
                qty = _safe_float(p.get("positionAmt"))
                if qty == 0:
                    continue
                return Position(
                    symbol=p.get("symbol", ""),
                    side=OrderSide.BUY if qty > 0 else OrderSide.SELL,
                    quantity=abs(qty),
                    entry_price=_safe_float(p.get("entryPrice")),
                    unrealized_pnl=_safe_float(p.get("unRealizedProfit")),
                )
            return None
        except Exception as e:
            logger.error("get_position failed: %s", e)
            return None

    async def close_position(self, symbol: str) -> Optional[Order]:
        pos = await self.get_position(symbol)
        if pos is None or pos.quantity == 0:
            return None
        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
        return await self.place_order(symbol, close_side, pos.quantity)

    def get_step_size(self, symbol: str) -> float:
        info = self._exchange_info.get(symbol)
        if not info:
            return 0.0
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                return float(f["stepSize"])
        return 0.0

    def _round_quantity(self, symbol: str, quantity: float) -> float:
        step = self.get_step_size(symbol)
        if step > 0:
            precision = max(0, -int(math.log10(step)))
            return round(math.floor(quantity / step) * step, precision)
        return round(quantity, 8)

    @staticmethod
    def _map_status(raw: str) -> OrderStatus:
        mapping = {
            "NEW": OrderStatus.SUBMITTED,
            "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "EXPIRED": OrderStatus.CANCELLED,
        }
        return mapping.get(raw, OrderStatus.PENDING)
