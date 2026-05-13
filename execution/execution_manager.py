from __future__ import annotations

import asyncio
import logging
import math
import threading
from typing import Callable, Deque, List, Optional
from collections import deque

from execution.broker_interface import BrokerInterface
from execution.models import (
    AccountInfo, ExecutionIntent, ExitType, Order, OrderSide, OrderStatus,
    OrderType, Position, SizingConfig, SizingMode,
)

logger = logging.getLogger(__name__)


class ExecutionManager:
    """Routes Ripple intents to a broker with position sizing, cooldown,
    and safety guards.

    Runs its own asyncio event loop in a background thread so the caller
    (Qt main thread or CLI) never blocks.

    Live execution topology contract (AGENT_STRATEGY_RULES.md §7.4): the
    live path consumes ``RippleDecision`` intents via :meth:`on_intent`,
    which already honour the Ripple lifecycle FSM, Wave permissions,
    the ``RiskEngine`` ES throttle, and Tide budgets. The legacy
    signal-driven entry point (``on_signal`` / ``_execute_signal``) was
    removed in Phase 14F — it had been a deprecated shim since
    Phase 14A and was not wired by any production caller.

    Cooldowns are enforced in **event time** (`intent.timestamp_ms`) per
    the V1 §22.2 / AGENT_STRATEGY_RULES.md §7.1 determinism contract.
    There is no wall-clock fallback anywhere on the decision path.
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
        # Phase 14A: ms-precision event-time cooldown gate. We keep the
        # ``cooldown_s`` constructor parameter unchanged for backwards
        # compatibility but enforce it as ``cooldown_s * 1000`` ms
        # against ``intent.timestamp`` on the on_intent path.
        self._cooldown_ms = int(cooldown_s * 1000.0)
        self._order_callback = order_callback

        self._armed = False
        self._current_side: Optional[OrderSide] = None
        self._current_qty: float = 0.0
        self._current_entry_price: float = 0.0
        # Event-time milliseconds — used by ``on_intent``. No wall-clock
        # counterpart exists post-14F.
        self._last_intent_ts_ms: int = 0
        self._last_signal_price: float = 0.0
        self._step_size: float = 0.0
        self._orders: Deque[Order] = deque(maxlen=500)
        self._account: AccountInfo = AccountInfo()

        # Phase 8B — session-attributed PnL and trade count.
        # Reset to zero on each ARM so the panel always shows current-session
        # data and never accumulates across multiple arm/disarm cycles.
        self._session_realized_pnl: float = 0.0
        self._session_trade_count: int = 0

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

    # ------------------------------------------------------------------
    # Phase 8B — session stats (read-only; reset on ARM)
    # ------------------------------------------------------------------

    @property
    def session_realized_pnl(self) -> float:
        """Cumulative strategy-attributed realized PnL for the current ARM session."""
        return self._session_realized_pnl

    @property
    def session_trade_count(self) -> int:
        """Number of completed exit fills (round-trip count) for the current ARM session."""
        return self._session_trade_count

    def reset_session_stats(self) -> None:
        """Clear session PnL and trade count.  Called automatically on ARM."""
        self._session_realized_pnl = 0.0
        self._session_trade_count = 0
        self._current_entry_price = 0.0

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
        self.reset_session_stats()
        logger.info("Execution ARMED for %s", self._symbol)

    def disarm(self, close_position: bool = True) -> None:
        self._armed = False
        logger.info("Execution DISARMED for %s", self._symbol)
        if close_position and self._current_qty > 0:
            self._submit(self._close_position_coro())

    def on_intent(self, intent: ExecutionIntent) -> None:
        """Phase 14A — canonical live-execution entry point.

        Consumes a Ripple ``ExecutionIntent`` (from
        :func:`execution.models.ripple_decision_to_intent`) and routes
        it to the broker. The intent has already passed through the
        Ripple lifecycle FSM, Wave permissions matrix, and ``RiskEngine``
        on the C++ side, so this method is a thin transport layer:
        cooldown gate, side resolution, broker dispatch.

        **Determinism contract (V1 §22.2 + AGENT_STRATEGY_RULES.md §7.1):**
        the cooldown gate uses ``intent.timestamp`` (event time, ms)
        and never consults wall-clock time. A captured live session
        can therefore be replayed with no temporal drift.

        Thread-safe: dispatches the actual broker call onto the
        manager's asyncio worker via ``run_coroutine_threadsafe``.
        """
        if intent is None:
            return
        if not self._armed or not self._loop:
            return

        intent_type = intent.intent_type or ""
        # cancel/rearm/prepare are no-ops on the live MARKET path —
        # we have no passive orders to cancel and no preparation
        # state. They are still legitimate intents (used by paper).
        if intent_type in ("cancel", "rearm", "prepare"):
            return

        # Event-time cooldown gate. Reject intents that arrive within
        # ``cooldown_ms`` of the last accepted intent.
        ts_ms = int(intent.timestamp or 0)
        if (self._last_intent_ts_ms > 0
                and ts_ms > 0
                and (ts_ms - self._last_intent_ts_ms) < self._cooldown_ms):
            return

        # Cache the reference price for downstream sizing.
        if intent.reference_price > 0:
            self._last_signal_price = intent.reference_price

        if intent_type == "exit":
            # Exits always close the current position regardless of
            # side. If we have no position there is nothing to do —
            # return WITHOUT advancing the cooldown stamp so a later
            # legitimate entry intent isn't blocked by a no-op exit.
            # This mirrors the entry-path invariant: only dispatched
            # intents advance the cooldown gate (suppressed entries on
            # same-side / None-side also leave the stamp untouched).
            if self._current_qty <= 0 or self._current_side is None:
                return
            self._last_intent_ts_ms = ts_ms if ts_ms > 0 else self._last_intent_ts_ms
            asyncio.run_coroutine_threadsafe(
                self._execute_intent_exit(intent), self._loop)
            return

        if intent_type == "entry":
            if intent.side is None:
                return
            desired_side = intent.side
            if self._current_side == desired_side and self._current_qty > 0:
                return
            self._last_intent_ts_ms = ts_ms if ts_ms > 0 else self._last_intent_ts_ms
            asyncio.run_coroutine_threadsafe(
                self._execute_intent_entry(intent, desired_side),
                self._loop)
            return

    def refresh_account(self) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._refresh_account(), self._loop)

    def update_sizing(self, sizing: SizingConfig) -> None:
        self._sizing = sizing

    def update_cooldown(self, cooldown_s: float) -> None:
        """Update the cooldown window (seconds). Recomputes the
        ms-precision event-time gate used by :meth:`on_intent`."""
        self._cooldown_s = cooldown_s
        self._cooldown_ms = int(cooldown_s * 1000.0)

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

    async def _close_position_coro(self) -> None:
        order = await self._broker.close_position(self._symbol)
        if order:
            order.signal_type = "DISARM_CLOSE"
            self._record_order(order)
        self._current_side = None
        self._current_qty = 0.0
        await self._refresh_account()

    # ------------------------------------------------------------------
    # Phase 15 — order-type routing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_order_type(intent: ExecutionIntent) -> tuple[OrderType, float, bool]:
        """Phase 15 — determine the correct order type for an intent.

        Returns ``(order_type, limit_price, did_fallback)`` where
        ``did_fallback`` is True when a LIMIT was requested but fell back
        to MARKET due to an invalid ``reference_price``.

        Routing table (strategy.md §13.3):
        - bounce entry  (NORMAL)        → LIMIT at reference_price
        - breakout entry (IMMEDIATE)    → MARKET
        - scale-in (NORMAL)             → LIMIT near microprice
        - TARGET / EXHAUSTION exit      → LIMIT at reference_price
        - INVALIDATION / RISK_BUDGET exit → MARKET (IMMEDIATE, never blocked)
        - TIME exit                     → MARKET
        """
        import math as _math

        urgency = (intent.urgency or "NORMAL").upper()
        intent_type = intent.intent_type or ""
        action = intent.action or ""
        exit_type: Optional[ExitType] = intent.exit_type

        if intent_type == "exit":
            if exit_type in (ExitType.TARGET, ExitType.EXHAUSTION):
                ref = intent.reference_price
                if ref > 0 and _math.isfinite(ref):
                    return OrderType.LIMIT, ref, False
                # Invalid ref price on exit — fall back to MARKET, never block
                logger.warning(
                    "LIMIT_FALLBACK: exit %s reference_price invalid (%.6f) → MARKET",
                    exit_type, ref if ref is not None else 0.0)
                return OrderType.MARKET, 0.0, True
            return OrderType.MARKET, 0.0, False

        if intent_type == "entry":
            if urgency == "IMMEDIATE":
                return OrderType.MARKET, 0.0, False
            # NORMAL urgency → LIMIT for bounce entries
            if "BOUNCE" in action or "SCALE" in action:
                ref = intent.reference_price
                if ref > 0 and _math.isfinite(ref):
                    return OrderType.LIMIT, ref, False
                # Invalid ref price → fall back to MARKET + warning
                logger.warning(
                    "LIMIT_FALLBACK: entry %s reference_price invalid (%.6f) → MARKET",
                    action, ref if ref is not None else 0.0)
                return OrderType.MARKET, 0.0, True
            return OrderType.MARKET, 0.0, False

        return OrderType.MARKET, 0.0, False

    async def _execute_intent_entry(
        self, intent: ExecutionIntent, desired_side: OrderSide
    ) -> None:
        """Phase 14A / Phase 15 — entry path driven by a Ripple ``ExecutionIntent``.

        Phase 15 adds order-type routing: bounce entries use LIMIT orders when
        ``reference_price`` is valid; breakout entries always use MARKET.
        ``LIMIT_FALLBACK`` is logged when a LIMIT was requested but the
        reference price was invalid.
        """
        signal_type = intent.action or "RIPPLE_ENTRY"
        try:
            if (self._current_qty > 0
                    and self._current_side != desired_side):
                close_order = await self._broker.close_position(self._symbol)
                if close_order:
                    close_order.signal_type = f"CLOSE_{signal_type}"
                    close_order.ripple_reason = intent.reason or ""
                    self._record_order(close_order)
                    if close_order.status not in (
                            OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                        logger.warning(
                            "Close order not filled: %s", close_order.status)
                        return
                    await self._refresh_account()
                self._current_side = None
                self._current_qty = 0.0

            qty = self._compute_quantity(desired_side)
            if qty <= 0:
                return

            if qty > self._sizing.max_position:
                qty = self._sizing.max_position

            order_type, limit_price, _ = self._resolve_order_type(intent)
            order = await self._broker.place_order(
                self._symbol, desired_side, qty, order_type, price=limit_price)
            order.signal_type = signal_type
            order.ripple_reason = intent.reason or ""
            self._record_order(order)

            if order.status in (
                    OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                self._current_side = desired_side
                self._current_qty = order.fill_quantity or qty
                if order.fill_price and order.fill_price > 0:
                    self._current_entry_price = order.fill_price
                await self._refresh_account()
            else:
                logger.warning(
                    "Order %s: %s",
                    order.status.value, order.error_message)
        except Exception as e:
            logger.error("Execution error (intent entry): %s", e)

    async def _execute_intent_exit(self, intent: ExecutionIntent) -> None:
        """Phase 14A / Phase 15 — exit path driven by a Ripple ``ExecutionIntent``.

        Phase 15 note: INVALIDATION and RISK_BUDGET exits are always routed as
        MARKET regardless of order-type routing; they must never be blocked by
        a missing reference price. TARGET and EXHAUSTION exits may use LIMIT
        but fall back to close_position (MARKET) on the live broker path for
        simplicity — the PaperEngine handles LIMIT exit simulation directly.

        Local position state is cleared only when the broker confirms the close
        executed (``FILLED`` / ``PARTIALLY_FILLED``) or reports no open
        position. A rejected / cancelled / expired close leaves
        ``_current_side`` / ``_current_qty`` intact for retry.
        """
        try:
            order = await self._broker.close_position(self._symbol)
            if order:
                order.signal_type = intent.action or "RIPPLE_EXIT"
                order.ripple_reason = intent.reason or ""
                self._record_order(order)
                if order.status not in (
                        OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED):
                    logger.warning(
                        "Exit order not filled: %s — local position "
                        "state preserved (side=%s qty=%s) for retry",
                        order.status,
                        self._current_side,
                        self._current_qty,
                    )
                    await self._refresh_account()
                    return
                # Phase 8B — attribute PnL to this session on a confirmed fill.
                if order.status == OrderStatus.FILLED:
                    fill_price = order.fill_price or 0.0
                    entry_price = self._current_entry_price
                    qty = self._current_qty
                    side_sign = (1.0 if self._current_side == OrderSide.BUY
                                 else -1.0)
                    if fill_price > 0 and entry_price > 0 and qty > 0:
                        pnl = (fill_price - entry_price) * qty * side_sign
                        self._session_realized_pnl += pnl
                    self._session_trade_count += 1
            self._current_side = None
            self._current_qty = 0.0
            self._current_entry_price = 0.0
            await self._refresh_account()
        except Exception as e:
            logger.error("Execution error (intent exit): %s", e)

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
