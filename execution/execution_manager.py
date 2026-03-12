from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import Callable, Deque, List, Optional
from collections import deque

from execution.broker_interface import BrokerInterface
from execution.models import (
    AccountInfo, Order, OrderSide, OrderStatus, OrderType,
    Position, SizingConfig, SizingMode,
)

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Routes signals to a broker with position sizing, cooldown, and safety guards.

    Runs its own asyncio event loop in a background thread so the caller
    (Qt main thread or CLI) never blocks.
    """

    MIN_NOTIONAL = 105

    def __init__(
        self,
        broker: BrokerInterface,
        symbol: str,
        sizing: SizingConfig,
        cooldown_s: float = 5.0,
        order_callback: Optional[Callable[[Order], None]] = None,
    ) -> None:
        self._broker = broker
        self._symbol = symbol.upper()
        self._sizing = sizing
        self._cooldown_s = cooldown_s
        self._order_callback = order_callback

        self._armed = False
        self._current_side: Optional[OrderSide] = None
        self._current_qty: float = 0.0
        self._last_order_ts: float = 0.0
        self._last_signal_price: float = 0.0
        self._step_size: float = 0.0
        self._orders: Deque[Order] = deque(maxlen=500)
        self._account: AccountInfo = AccountInfo()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def armed(self) -> bool:
        return self._armed

    @property
    def orders(self) -> List[Order]:
        with self._lock:
            return list(self._orders)

    @property
    def account(self) -> AccountInfo:
        return self._account

    @property
    def current_side(self) -> Optional[OrderSide]:
        return self._current_side

    @property
    def current_qty(self) -> float:
        return self._current_qty

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        ok = future.result(timeout=15)
        if ok:
            asyncio.run_coroutine_threadsafe(self._periodic_refresh(), self._loop)
        return ok

    def stop(self) -> None:
        if self._armed:
            self.disarm(close_position=True)
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._shutdown(), self._loop
                ).result(timeout=15)
            except Exception as e:
                logger.warning("ExecutionManager shutdown error: %s", e)
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def arm(self) -> None:
        self._armed = True
        logger.info("Execution ARMED for %s", self._symbol)

    def disarm(self, close_position: bool = True) -> None:
        self._armed = False
        logger.info("Execution DISARMED for %s", self._symbol)
        if close_position and self._current_qty > 0:
            self._submit(self._close_position_coro())

    def on_signal(self, signal) -> None:
        """Called from the signal callback (any thread)."""
        if not self._armed or not self._loop:
            return

        now = time.time()
        if now - self._last_order_ts < self._cooldown_s:
            return

        type_name = signal.type_name()
        is_buy = "BUY" in type_name or "BULL" in type_name
        desired_side = OrderSide.BUY if is_buy else OrderSide.SELL

        if signal.price > 0:
            self._last_signal_price = signal.price

        if self._current_side == desired_side:
            return

        asyncio.run_coroutine_threadsafe(
            self._execute_signal(desired_side, type_name), self._loop
        )

    def refresh_account(self) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._refresh_account(), self._loop)

    def update_sizing(self, sizing: SizingConfig) -> None:
        self._sizing = sizing

    async def _connect(self) -> bool:
        ok = await self._broker.connect()
        if ok:
            self._step_size = self._broker.get_step_size(self._symbol)
            await self._refresh_account()
            await self._sync_position()
        return ok

    async def _shutdown(self) -> None:
        """Cancel all pending tasks, disconnect broker, and drain the loop."""
        await self._broker.disconnect()

        tasks = [t for t in asyncio.all_tasks(self._loop)
                 if t is not asyncio.current_task()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._loop.shutdown_asyncgens()

    async def _disconnect(self) -> None:
        await self._broker.disconnect()

    async def _refresh_account(self) -> None:
        self._account = await self._broker.get_account_info()

    async def _periodic_refresh(self) -> None:
        consecutive_failures = 0
        while self._broker.is_connected():
            try:
                await self._refresh_account()
                if consecutive_failures > 0:
                    logger.info("Account refresh recovered after %d failures",
                                consecutive_failures)
                consecutive_failures = 0
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures <= 3 or consecutive_failures % 30 == 0:
                    logger.warning("Account refresh failed (%d): %s",
                                   consecutive_failures, e)
            await asyncio.sleep(2)

    async def _sync_position(self) -> None:
        pos = await self._broker.get_position(self._symbol)
        if pos and pos.quantity > 0:
            self._current_side = pos.side
            self._current_qty = pos.quantity
            logger.info("Synced position: %s %.6f %s",
                        self._symbol, pos.quantity, pos.side.value)
        else:
            self._current_side = None
            self._current_qty = 0.0

    async def _execute_signal(self, desired_side: OrderSide, signal_type: str) -> None:
        self._last_order_ts = time.time()
        try:
            if self._current_qty > 0 and self._current_side != desired_side:
                close_order = await self._broker.close_position(self._symbol)
                if close_order:
                    close_order.signal_type = f"CLOSE_{signal_type}"
                    self._record_order(close_order)
                    if close_order.status not in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                        logger.warning("Close order not filled: %s", close_order.status)
                        return
                    await self._refresh_account()
                self._current_side = None
                self._current_qty = 0.0

            qty = self._compute_quantity(desired_side)
            if qty <= 0:
                return

            if qty > self._sizing.max_position:
                qty = self._sizing.max_position

            order = await self._broker.place_order(
                self._symbol, desired_side, qty, OrderType.MARKET
            )
            order.signal_type = signal_type
            self._record_order(order)

            if order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                self._current_side = desired_side
                self._current_qty = order.fill_quantity or qty
                await self._refresh_account()
            else:
                logger.warning("Order %s: %s", order.status.value, order.error_message)

        except Exception as e:
            logger.error("Execution error: %s", e)

    async def _close_position_coro(self) -> None:
        order = await self._broker.close_position(self._symbol)
        if order:
            order.signal_type = "DISARM_CLOSE"
            self._record_order(order)
        self._current_side = None
        self._current_qty = 0.0
        await self._refresh_account()

    def _compute_quantity(self, side: OrderSide) -> float:
        mode = self._sizing.mode
        val = self._sizing.value
        price = self._estimate_price()
        if price <= 0:
            logger.warning("Cannot compute quantity: no price estimate for %s", self._symbol)
            return 0.0

        min_notional_qty = self.MIN_NOTIONAL / price
        if self._step_size > 0:
            min_notional_qty = math.ceil(min_notional_qty / self._step_size) * self._step_size

        if mode == SizingMode.FIXED_QTY:
            qty = max(val, min_notional_qty)
        elif mode == SizingMode.FIXED_NOTIONAL:
            qty = max(val / price, min_notional_qty)
        elif mode == SizingMode.PCT_BALANCE:
            bal = self._account.available_balance
            qty = max((bal * val / 100.0) / price, min_notional_qty) if bal > 0 else min_notional_qty
        else:
            qty = min_notional_qty

        return qty

    def _estimate_price(self) -> float:
        for pos in self._account.positions:
            if pos.symbol == self._symbol and pos.entry_price > 0:
                return pos.entry_price
        return self._last_signal_price

    def _record_order(self, order: Order) -> None:
        with self._lock:
            self._orders.append(order)
        if self._order_callback:
            try:
                self._order_callback(order)
            except Exception:
                pass

    def _submit(self, coro) -> None:
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        self._loop.close()
