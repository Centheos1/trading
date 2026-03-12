"""Lightweight paper execution engine for Ripple intent-driven trading.

Routes ExecutionIntents into simulated fills without a broker connection.
Tracks a single position per symbol with entry price, PnL, and fill history.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional

from execution.models import (
    ExecutionIntent, Order, OrderSide, OrderStatus, OrderType,
    SizingConfig, SizingMode, SuppressionReason,
)

logger = logging.getLogger(__name__)


@dataclass
class PaperPosition:
    side: Optional[OrderSide] = None
    quantity: float = 0.0
    entry_price: float = 0.0


@dataclass
class PaperMetrics:
    intents_received: int = 0
    entries_filled: int = 0
    exits_filled: int = 0
    suppressed_inventory: int = 0
    suppressed_no_position: int = 0
    realized_pnl: float = 0.0


class PaperEngine:
    """Simulates fills from Ripple ExecutionIntents.

    Runs synchronously on the caller's thread (no broker, no asyncio).
    """

    def __init__(
        self,
        symbol: str,
        sizing: SizingConfig,
        order_callback: Optional[Callable[[Order], None]] = None,
    ) -> None:
        self._symbol = symbol.upper()
        self._sizing = sizing
        self._order_callback = order_callback
        self._position = PaperPosition()
        self._orders: Deque[Order] = deque(maxlen=500)
        self._metrics = PaperMetrics()
        self._balance = 10_000.0
        self._last_price = 0.0

    @property
    def position(self) -> PaperPosition:
        return self._position

    @property
    def orders(self) -> List[Order]:
        return list(self._orders)

    @property
    def metrics(self) -> PaperMetrics:
        return self._metrics

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def realized_pnl(self) -> float:
        return self._metrics.realized_pnl

    def unrealized_pnl(self, current_price: float) -> float:
        if self._position.side is None or self._position.quantity <= 0:
            return 0.0
        if self._position.side == OrderSide.BUY:
            return (current_price - self._position.entry_price) * self._position.quantity
        else:
            return (self._position.entry_price - current_price) * self._position.quantity

    def update_sizing(self, sizing: SizingConfig) -> None:
        self._sizing = sizing

    def on_intent(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        """Process an execution intent. Returns None if filled, or a
        SuppressionReason if the intent was suppressed."""
        self._metrics.intents_received += 1

        if intent.reference_price > 0:
            self._last_price = intent.reference_price

        if intent.intent_type == "entry":
            return self._handle_entry(intent)
        elif intent.intent_type == "exit":
            return self._handle_exit(intent)
        elif intent.intent_type == "cancel":
            logger.info("Paper: cancel passive — no-op (no passive orders)")
            return None
        elif intent.intent_type == "rearm":
            logger.info("Paper: rearm — eligibility reset")
            return None
        else:
            return None

    def _handle_entry(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        if intent.side is None:
            return SuppressionReason.INVENTORY

        if (self._position.side == intent.side and
                self._position.quantity > 0):
            self._metrics.suppressed_inventory += 1
            return SuppressionReason.INVENTORY

        if (self._position.side is not None and
                self._position.side != intent.side and
                self._position.quantity > 0):
            self._close_position(intent, f"CLOSE_{intent.action}")

        price = intent.reference_price if intent.reference_price > 0 else self._last_price
        if price <= 0:
            return SuppressionReason.INVENTORY

        qty = self._compute_quantity(price)
        if qty <= 0:
            return SuppressionReason.INVENTORY

        self._position = PaperPosition(
            side=intent.side, quantity=qty, entry_price=price)
        self._metrics.entries_filled += 1

        order = Order(
            symbol=self._symbol,
            side=intent.side,
            quantity=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=price,
            fill_quantity=qty,
            timestamp=intent.timestamp / 1000.0,
            signal_type=intent.action,
            ripple_reason=intent.reason,
        )
        self._record_order(order)
        return None

    def _handle_exit(self, intent: ExecutionIntent) -> Optional[SuppressionReason]:
        if self._position.side is None or self._position.quantity <= 0:
            self._metrics.suppressed_no_position += 1
            return SuppressionReason.INVENTORY
        self._close_position(intent, intent.action)
        self._metrics.exits_filled += 1
        return None

    def _close_position(self, intent: ExecutionIntent, signal_type: str) -> None:
        price = intent.reference_price if intent.reference_price > 0 else self._last_price
        if price <= 0:
            return

        qty = self._position.quantity
        if self._position.side == OrderSide.BUY:
            pnl = (price - self._position.entry_price) * qty
            close_side = OrderSide.SELL
        else:
            pnl = (self._position.entry_price - price) * qty
            close_side = OrderSide.BUY

        self._metrics.realized_pnl += pnl
        self._balance += pnl

        order = Order(
            symbol=self._symbol,
            side=close_side,
            quantity=qty,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=price,
            fill_quantity=qty,
            timestamp=intent.timestamp / 1000.0,
            signal_type=f"CLOSE_{signal_type}",
            ripple_reason=intent.reason,
        )
        self._record_order(order)
        self._position = PaperPosition()

    def _compute_quantity(self, price: float) -> float:
        mode = self._sizing.mode
        val = self._sizing.value

        if mode == SizingMode.FIXED_QTY:
            return val
        elif mode == SizingMode.FIXED_NOTIONAL:
            return val / price if price > 0 else 0.0
        elif mode == SizingMode.PCT_BALANCE:
            return (self._balance * val / 100.0) / price if price > 0 else 0.0
        return 0.0

    def _record_order(self, order: Order) -> None:
        self._orders.append(order)
        if self._order_callback:
            try:
                self._order_callback(order)
            except Exception:
                pass

    def reset(self) -> None:
        self._position = PaperPosition()
        self._orders.clear()
        self._metrics = PaperMetrics()
        self._balance = 10_000.0
