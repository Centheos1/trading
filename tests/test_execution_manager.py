"""
Phase 12 — Track B: ExecutionManager orchestration tests.

Stubs the broker, runs the manager's coroutines on a per-test asyncio
event loop (no real threads / sleeps unless a lifecycle test needs
them). Covers the routing layer end-to-end without touching python-
binance or Qt.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import List, Optional
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.broker_interface import BrokerInterface
from execution.execution_manager import ExecutionManager
from execution.models import (
    AccountInfo,
    ExecutionIntent,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    SizingConfig,
    SizingMode,
)


# ----------------------------------------------------------------------
# Stub broker

class StubBroker(BrokerInterface):
    """In-memory broker used by every test in this module."""

    def __init__(self, *, step_size: float = 0.001) -> None:
        self.connect_returns = True
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.connected = False

        self.account_info = AccountInfo(
            balance=10_000.0, available_balance=5_000.0, positions=[])
        self.account_calls = 0
        self.account_info_exc: Optional[Exception] = None

        self.position: Optional[Position] = None
        self.placed_orders: List[Order] = []
        self.next_place_order: Optional[Order] = None  # if set, returned by place_order
        self.next_close_order: Optional[Order] = None
        self.cancel_returns = True

        self._step_size = step_size

    async def connect(self) -> bool:
        self.connect_calls += 1
        self.connected = self.connect_returns
        return self.connect_returns

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def get_account_info(self) -> AccountInfo:
        self.account_calls += 1
        if self.account_info_exc is not None:
            raise self.account_info_exc
        return self.account_info

    async def place_order(self, symbol: str, side: OrderSide,
                          quantity: float,
                          order_type: OrderType = OrderType.MARKET,
                          price: float = 0.0) -> Order:
        if self.next_place_order is not None:
            order = self.next_place_order
            self.next_place_order = None
        else:
            order = Order(
                symbol=symbol, side=side, quantity=quantity,
                order_type=order_type,
                status=OrderStatus.FILLED,
                fill_price=100.0,
                fill_quantity=quantity,
            )
        self.placed_orders.append(order)
        return order

    async def cancel_order(self, symbol: str, broker_order_id: str) -> bool:
        return self.cancel_returns

    async def place_oco(self, symbol: str, side: OrderSide,
                        quantity: float, target_price: float,
                        stop_price: float):
        target = Order(symbol=symbol, side=side, quantity=quantity,
                       order_type=OrderType.LIMIT, status=OrderStatus.SUBMITTED)
        stop_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
        stop = Order(symbol=symbol, side=stop_side, quantity=quantity,
                     order_type=OrderType.MARKET, status=OrderStatus.SUBMITTED)
        return target, stop

    async def get_open_orders(self, symbol: str) -> List[Order]:
        return []

    async def get_position(self, symbol: str) -> Optional[Position]:
        return self.position

    async def close_position(self, symbol: str) -> Optional[Order]:
        if self.next_close_order is not None:
            order = self.next_close_order
            self.next_close_order = None
            self.placed_orders.append(order)
            return order
        if self.position is None or self.position.quantity == 0:
            return None
        # Default: synthesize a filled close.
        close_side = (OrderSide.SELL if self.position.side == OrderSide.BUY
                      else OrderSide.BUY)
        order = Order(
            symbol=symbol, side=close_side,
            quantity=self.position.quantity,
            status=OrderStatus.FILLED,
            fill_price=100.0,
            fill_quantity=self.position.quantity,
        )
        self.placed_orders.append(order)
        return order

    def get_step_size(self, symbol: str) -> float:
        return self._step_size


# ----------------------------------------------------------------------
# Helpers

def _make_manager(broker: StubBroker, *,
                  sizing: SizingConfig | None = None,
                  cooldown_s: float = 5.0,
                  callback=None) -> ExecutionManager:
    return ExecutionManager(
        broker=broker,
        symbol="BTCUSDT",
        sizing=sizing or SizingConfig(mode=SizingMode.FIXED_QTY,
                                      value=0.1, max_position=10.0),
        cooldown_s=cooldown_s,
        order_callback=callback,
    )


# ----------------------------------------------------------------------
# Async-coroutine tests (no thread / no real loop)

class _LoopBaseCase(unittest.TestCase):
    """Provide a per-test event loop without starting the manager's
    background thread. Tests call ``self.run_async(coro)`` to drive
    coroutines deterministically."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)


class TestConnectFlow(_LoopBaseCase):

    def test_connect_primes_step_size_account_and_position(self):
        broker = StubBroker(step_size=0.0005)
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.5, entry_price=200.0)
        m = _make_manager(broker)
        ok = self.run_async(m._connect())
        self.assertTrue(ok)
        self.assertEqual(broker.connect_calls, 1)
        self.assertEqual(m._step_size, 0.0005)
        self.assertEqual(m.account.balance, 10_000.0)
        self.assertEqual(m.current_side, OrderSide.BUY)
        self.assertEqual(m.current_qty, 0.5)

    def test_connect_returns_false_on_broker_failure(self):
        broker = StubBroker()
        broker.connect_returns = False
        m = _make_manager(broker)
        ok = self.run_async(m._connect())
        self.assertFalse(ok)
        # Failure path skips step_size / account / position.
        self.assertEqual(m._step_size, 0.0)
        self.assertIsNone(m.current_side)


class TestArmDisarm(_LoopBaseCase):

    def test_arm_and_disarm_toggle_state(self):
        broker = StubBroker()
        m = _make_manager(broker)
        self.assertFalse(m.armed)
        m.arm()
        self.assertTrue(m.armed)
        m.disarm(close_position=False)
        self.assertFalse(m.armed)

    def test_disarm_close_position_emits_close_when_qty(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._loop = self.loop
        # Fake an existing position.
        m._current_side = OrderSide.BUY
        m._current_qty = 0.5
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.5, entry_price=100.0)
        m.arm()

        # `disarm(True)` schedules `_close_position_coro` on m._loop.
        # Drive it via run_async since we aren't running m._loop in a thread.
        m._armed = False
        self.run_async(m._close_position_coro())
        self.assertIsNone(m.current_side)
        self.assertEqual(m.current_qty, 0.0)
        # Close order was recorded.
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].side, OrderSide.SELL)
        # signal_type set by _close_position_coro:
        self.assertEqual(broker.placed_orders[0].signal_type, "DISARM_CLOSE")


class TestRecordOrderCallback(_LoopBaseCase):
    """Phase 14F: the legacy ``_execute_signal`` test that exercised
    ``_record_order`` indirectly is now driven through the canonical
    ``_execute_intent_entry`` path. Verifies the order-callback
    exception is still swallowed end-to-end."""

    def test_record_order_callback_exception_swallowed(self):
        seen: List[Order] = []

        def boom(o):
            seen.append(o)
            raise RuntimeError("callback failure")

        broker = StubBroker()
        m = _make_manager(broker, callback=boom)
        m._last_signal_price = 100.0
        # Drive through the canonical intent entry path.
        self.run_async(m._execute_intent_entry(
            _intent("entry", OrderSide.BUY, action="ENTER_LONG"),
            OrderSide.BUY))
        self.assertEqual(len(m.orders), 1)
        self.assertEqual(len(seen), 1)


class TestComputeQuantity(_LoopBaseCase):

    def test_fixed_qty_respects_min_notional(self):
        # value below MIN_NOTIONAL/price → bumped up to min_notional_qty
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_QTY, value=0.0001, max_position=10.0))
        m._last_signal_price = 100.0
        # MIN_NOTIONAL=105, 105/100 = 1.05 → max(0.0001, 1.05) = 1.05
        qty = m._compute_quantity(OrderSide.BUY)
        self.assertAlmostEqual(qty, 1.05)

    def test_fixed_notional_uses_value_over_price(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_NOTIONAL, value=500.0, max_position=10.0))
        m._last_signal_price = 100.0
        qty = m._compute_quantity(OrderSide.BUY)
        self.assertAlmostEqual(qty, 5.0)

    def test_pct_balance_uses_available_balance(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.PCT_BALANCE, value=10.0, max_position=10.0))
        m._account = AccountInfo(balance=10_000.0,
                                 available_balance=2_000.0, positions=[])
        m._last_signal_price = 100.0
        # 10% of $2000 = $200; / 100 price = 2.0 qty
        qty = m._compute_quantity(OrderSide.BUY)
        self.assertAlmostEqual(qty, 2.0)

    def test_step_size_rounds_up_min_notional_qty(self):
        # step_size=0.001 → ceil(1.05/0.001)*0.001 = 1.05
        broker = StubBroker(step_size=0.001)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_QTY, value=0.0001, max_position=10.0))
        m._step_size = 0.001
        m._last_signal_price = 200.0
        # 105 / 200 = 0.525 → ceil(0.525/0.001)*0.001 = 0.525
        qty = m._compute_quantity(OrderSide.BUY)
        self.assertAlmostEqual(qty, 0.525, places=5)

    def test_no_price_returns_zero(self):
        broker = StubBroker()
        m = _make_manager(broker)
        # _estimate_price → 0.0 (no positions, no _last_signal_price)
        qty = m._compute_quantity(OrderSide.BUY)
        self.assertEqual(qty, 0.0)


class TestExecuteIntentMaxPositionClamp(_LoopBaseCase):
    """Phase 14F: ``_execute_intent_entry`` clamps quantity to
    ``_sizing.max_position`` after ``_compute_quantity`` returns. This
    is verified at the order surface (formerly tested through the
    deleted ``_execute_signal`` path)."""

    def test_quantity_clamped_to_max_position(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_NOTIONAL, value=1_000_000.0,
            max_position=0.5))
        m._last_signal_price = 100.0
        self.run_async(m._execute_intent_entry(
            _intent("entry", OrderSide.BUY, action="ENTER_LONG"),
            OrderSide.BUY))
        self.assertEqual(len(broker.placed_orders), 1)
        # raw qty = 10_000 → clamped to 0.5
        self.assertAlmostEqual(broker.placed_orders[0].quantity, 0.5)


class TestEstimatePrice(_LoopBaseCase):

    def test_estimate_price_prefers_position_entry(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._account = AccountInfo(
            balance=0.0, available_balance=0.0,
            positions=[Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                quantity=0.5, entry_price=42_000.0)])
        m._last_signal_price = 100.0
        self.assertEqual(m._estimate_price(), 42_000.0)

    def test_estimate_price_falls_back_to_last_signal(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._last_signal_price = 250.0
        # No matching position → fallback.
        self.assertEqual(m._estimate_price(), 250.0)

    def test_estimate_price_zero_when_neither_set(self):
        broker = StubBroker()
        m = _make_manager(broker)
        self.assertEqual(m._estimate_price(), 0.0)


class TestPeriodicRefresh(_LoopBaseCase):
    """Drive `_periodic_refresh` for a couple of iterations with sleep
    monkey-patched to a fast no-op so we can observe failure handling."""

    def test_refresh_resilient_to_exceptions(self):
        broker = StubBroker()
        # First two calls raise, third succeeds, fourth call breaks the
        # loop by toggling `is_connected` to False.
        broker.connected = True
        broker.account_info_exc = RuntimeError("boom")
        m = _make_manager(broker)

        call_seq = {"count": 0}

        async def fake_sleep(_seconds):
            call_seq["count"] += 1
            if call_seq["count"] == 2:
                # After two failed iterations, succeed once.
                broker.account_info_exc = None
            elif call_seq["count"] >= 3:
                # End the loop on the next iteration.
                broker.connected = False

        with patch("execution.execution_manager.asyncio.sleep", fake_sleep):
            self.run_async(m._periodic_refresh())

        # Must have invoked get_account_info at least 3 times without
        # propagating the exceptions.
        self.assertGreaterEqual(broker.account_calls, 3)


class TestUpdateSizingAndRefresh(_LoopBaseCase):

    def test_update_sizing_takes_effect_on_next_compute(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_NOTIONAL, value=500.0, max_position=10.0))
        m._last_signal_price = 100.0
        self.assertAlmostEqual(m._compute_quantity(OrderSide.BUY), 5.0)
        m.update_sizing(SizingConfig(mode=SizingMode.FIXED_NOTIONAL,
                                     value=1_000.0, max_position=10.0))
        self.assertAlmostEqual(m._compute_quantity(OrderSide.BUY), 10.0)

    def test_refresh_account_noop_when_no_loop(self):
        broker = StubBroker()
        m = _make_manager(broker)
        # _loop is None → no scheduling, no exception.
        m.refresh_account()
        self.assertEqual(broker.account_calls, 0)


# ----------------------------------------------------------------------
# Real-thread lifecycle (slow path; bounded with timeouts)

class TestStartStopLifecycle(unittest.TestCase):
    """Exercise `start()` / `stop()` with a worker thread + real loop.
    Bounded with timeouts so a hang fails fast."""

    def test_start_idempotent_and_stop_joins_thread(self):
        broker = StubBroker()
        m = _make_manager(broker)
        try:
            self.assertTrue(m.start())
            # Idempotent: second start sees an alive thread, returns True
            # without reconnecting.
            self.assertTrue(m.start())
            self.assertEqual(broker.connect_calls, 1)
        finally:
            m.stop()
        # Thread no longer alive.
        self.assertFalse(m._thread is not None and m._thread.is_alive())
        self.assertEqual(broker.disconnect_calls, 1)

    def test_start_returns_false_on_connect_failure(self):
        broker = StubBroker()
        broker.connect_returns = False
        m = _make_manager(broker)
        try:
            self.assertFalse(m.start())
        finally:
            m.stop()


# ----------------------------------------------------------------------
# Phase 14A — on_intent tests (Ripple-driven live execution path)

def _intent(
    intent_type: str = "entry",
    side: Optional[OrderSide] = OrderSide.BUY,
    timestamp_ms: int = 1_700_000_000_000,
    reference_price: float = 100.0,
    action: str = "ENTER_BOUNCE_LONG",
    reason: str = "wall holds",
) -> ExecutionIntent:
    """Build an ``ExecutionIntent`` for on_intent tests."""
    return ExecutionIntent(
        timestamp=timestamp_ms,
        action=action,
        side=side,
        intent_type=intent_type,
        reference_price=reference_price,
        reason=reason,
    )


class TestOnIntentDispatch(_LoopBaseCase):
    """Verifies :meth:`ExecutionManager.on_intent` dispatches each
    ``intent_type`` to the right downstream coroutine and that
    no-op intent types short-circuit cleanly."""

    def _arm_with_loop(self, broker: StubBroker, *, cooldown_s: float = 0.0):
        m = _make_manager(broker, cooldown_s=cooldown_s)
        m._loop = self.loop
        m.arm()
        return m

    def test_entry_intent_schedules_execution(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        scheduled: List = []

        def fake_run(coro, loop):
            scheduled.append(coro)
            self.run_async(coro)
            class _F:
                def result(self, timeout=None): return None
            return _F()

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=fake_run):
            m.on_intent(_intent("entry", OrderSide.BUY,
                                reference_price=200.0))

        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].side, OrderSide.BUY)
        self.assertEqual(broker.placed_orders[0].signal_type,
                         "ENTER_BOUNCE_LONG")
        self.assertEqual(broker.placed_orders[0].ripple_reason,
                         "wall holds")

    def test_exit_intent_closes_position(self):
        broker = StubBroker()
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.5, entry_price=100.0)
        m = self._arm_with_loop(broker)
        m._current_side = OrderSide.BUY
        m._current_qty = 0.5

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("exit", side=None,
                                action="EXIT_BOUNCE",
                                timestamp_ms=42_000,
                                reason="invalidation"))

        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].side, OrderSide.SELL)
        self.assertEqual(broker.placed_orders[0].signal_type, "EXIT_BOUNCE")
        self.assertEqual(broker.placed_orders[0].ripple_reason,
                         "invalidation")
        self.assertIsNone(m.current_side)
        self.assertEqual(m.current_qty, 0.0)
        # Symmetric invariant: a dispatched exit MUST advance the
        # cooldown stamp (counterpart to the no-op exit regression
        # above which must NOT advance it).
        self.assertEqual(m._last_intent_ts_ms, 42_000)

    def test_exit_with_rejected_close_preserves_position_state(self):
        """Regression — Phase 14A bug: ``_execute_intent_exit`` used to
        unconditionally clear ``_current_side`` / ``_current_qty`` even
        when the broker rejected the close order, silently orphaning a
        real open position. Mirrors the entry path's fill-status guard
        (``_execute_intent_entry`` lines 385-389)."""
        broker = StubBroker()
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.5, entry_price=100.0)
        broker.next_close_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, quantity=0.5,
            status=OrderStatus.REJECTED, error_message="exchange down")
        m = self._arm_with_loop(broker)
        m._current_side = OrderSide.BUY
        m._current_qty = 0.5

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("exit", side=None,
                                action="EXIT_BOUNCE",
                                timestamp_ms=42_000,
                                reason="invalidation"))

        # Broker did receive the close attempt and recorded it...
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].status,
                         OrderStatus.REJECTED)
        # ...but local state MUST be preserved so the next exit intent
        # can retry. Pre-fix this would have wrongly read
        # (None, 0.0) and the manager would have permanently lost
        # track of a still-open broker position.
        self.assertEqual(m.current_side, OrderSide.BUY)
        self.assertEqual(m.current_qty, 0.5)

    def test_exit_with_cancelled_close_preserves_position_state(self):
        """Same invariant as the rejected-close case, but with a
        ``CANCELLED`` status. Catches the (rejected, cancelled,
        pending, submitted) tuple as a class rather than just the
        most common failure."""
        broker = StubBroker()
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.SELL,
                                   quantity=0.3, entry_price=100.0)
        broker.next_close_order = Order(
            symbol="BTCUSDT", side=OrderSide.BUY, quantity=0.3,
            status=OrderStatus.CANCELLED)
        m = self._arm_with_loop(broker)
        m._current_side = OrderSide.SELL
        m._current_qty = 0.3

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("exit", side=None,
                                action="EXIT_BREAKOUT",
                                timestamp_ms=42_000,
                                reason="invalidation"))

        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].status,
                         OrderStatus.CANCELLED)
        self.assertEqual(m.current_side, OrderSide.SELL)
        self.assertEqual(m.current_qty, 0.3)

    def test_exit_with_no_broker_position_still_clears_state(self):
        """Counter-control: when the broker reports no open position
        (``close_position`` returns ``None``), local state SHOULD be
        cleared — the broker is the source of truth and there's
        nothing to retry. Pre-fix this happened to be correct by
        accident; post-fix the helper must still preserve this
        behaviour."""
        broker = StubBroker()
        # ``StubBroker.close_position`` returns ``None`` when
        # ``broker.position`` is ``None`` AND no override is queued.
        broker.position = None
        m = self._arm_with_loop(broker)
        m._current_side = OrderSide.BUY
        m._current_qty = 0.5

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("exit", side=None,
                                action="EXIT_BOUNCE",
                                timestamp_ms=42_000))

        self.assertEqual(len(broker.placed_orders), 0)
        self.assertIsNone(m.current_side)
        self.assertEqual(m.current_qty, 0.0)

    def test_cancel_intent_is_noop(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        m.on_intent(_intent("cancel", side=None,
                            action="CANCEL_PASSIVE_ORDERS"))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_rearm_intent_is_noop(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        m.on_intent(_intent("rearm", side=None,
                            action="REARM_FOR_NEXT_BOUNCE"))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_prepare_intent_is_noop(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        m.on_intent(_intent("prepare", action="PREPARE_BOUNCE_LONG"))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_none_intent_is_noop(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        m.on_intent(None)  # type: ignore[arg-type]
        self.assertEqual(len(broker.placed_orders), 0)

    def test_entry_without_side_is_noop(self):
        broker = StubBroker()
        m = self._arm_with_loop(broker)
        m.on_intent(_intent("entry", side=None))
        self.assertEqual(len(broker.placed_orders), 0)


class TestOnIntentEventTimeCooldown(_LoopBaseCase):
    """The cooldown gate on the on_intent path uses ``intent.timestamp``
    (event time) — never wall-clock. This is the V1 §22.2 +
    AGENT_STRATEGY_RULES.md §7.1 determinism contract."""

    def _scheduled(self, m, intent):
        scheduled: List = []

        def fake_run(coro, loop):
            scheduled.append(coro)
            self.run_async(coro)
            class _F:
                def result(self, timeout=None): return None
            return _F()

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=fake_run):
            m.on_intent(intent)
        return scheduled

    def test_first_intent_always_passes(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        # No prior intent — cooldown gate must not fire.
        s = self._scheduled(m, _intent("entry", OrderSide.BUY,
                                       timestamp_ms=10_000))
        self.assertEqual(len(s), 1)
        self.assertEqual(m._last_intent_ts_ms, 10_000)

    def test_intent_within_cooldown_is_dropped_by_event_time(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        # Seed a prior intent at t=10_000ms.
        m._last_intent_ts_ms = 10_000
        # Same side — irrelevant; cooldown must fire first.
        s = self._scheduled(m, _intent("entry", OrderSide.SELL,
                                       timestamp_ms=12_000))
        # 12_000 - 10_000 = 2_000 ms < 5_000 ms cooldown → dropped.
        self.assertEqual(len(s), 0)
        self.assertEqual(len(broker.placed_orders), 0)

    def test_intent_after_cooldown_passes(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        m._last_intent_ts_ms = 10_000
        # 10_000 + 5_000 = 15_000 — exactly at the boundary should pass.
        s = self._scheduled(m, _intent("entry", OrderSide.SELL,
                                       timestamp_ms=15_001))
        self.assertEqual(len(s), 1)
        self.assertEqual(m._last_intent_ts_ms, 15_001)

    def test_no_wall_clock_in_decision_logic(self):
        """Phase 14F: the ``import time`` statement was removed from
        ``execution_manager.py`` along with the deprecated ``on_signal``
        / ``_execute_signal`` path. Wall-clock reads are now impossible
        by construction — there is no ``time`` symbol in the module's
        namespace. Confirms the V1 §22.2 contract that live-execution
        decision logic is event-time only."""
        import execution.execution_manager as em_mod
        self.assertFalse(
            hasattr(em_mod, "time"),
            "execution_manager.py must not import `time` — wall-clock "
            "reads on the decision path are forbidden by V1 §22.2."
        )

        # Additionally verify the on_intent cooldown gate still uses
        # event time correctly across the boundary.
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        m._last_intent_ts_ms = 10_000

        def fake_run(coro, loop):
            coro.close()
            class _F:
                def result(self, timeout=None): return None
            return _F()

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=fake_run):
            m.on_intent(_intent("entry", OrderSide.SELL,
                                timestamp_ms=11_000))
            m.on_intent(_intent("entry", OrderSide.SELL,
                                timestamp_ms=20_000))
        # Schedule succeeded for the second intent: state advanced.
        self.assertEqual(m._last_intent_ts_ms, 20_000)

    def test_noop_exit_does_not_advance_cooldown_blocking_next_entry(self):
        """Regression: a flat-state ``exit`` intent must NOT advance
        ``_last_intent_ts_ms``. A prior revision updated the stamp
        before the position-existence check, which silently dropped
        the next legitimate entry that arrived within the cooldown
        window. The entry path's invariant — only dispatched intents
        advance the cooldown — must apply symmetrically to exits."""
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        # No prior cooldown stamp; manager is flat.
        self.assertEqual(m._last_intent_ts_ms, 0)
        self.assertEqual(m._current_qty, 0.0)

        # 1) No-op exit at t=10_000ms (no position to close).
        m.on_intent(_intent("exit", side=None, action="EXIT_BOUNCE",
                            timestamp_ms=10_000))
        self.assertEqual(len(broker.placed_orders), 0)
        self.assertEqual(m._last_intent_ts_ms, 0,
                         "no-op exit must not advance cooldown")

        # 2) Entry at t=12_000ms (well within 5_000ms cooldown of the
        #    no-op exit). With the bug present the cooldown gate would
        #    drop this; with the fix in place the entry must dispatch.
        s = self._scheduled(m, _intent("entry", OrderSide.BUY,
                                       timestamp_ms=12_000,
                                       reference_price=200.0))
        self.assertEqual(len(s), 1, "entry after no-op exit was dropped")
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].side, OrderSide.BUY)
        self.assertEqual(m._last_intent_ts_ms, 12_000)

    def test_zero_timestamp_intent_does_not_corrupt_cooldown_state(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        m._last_intent_ts_ms = 10_000
        # An intent with timestamp=0 must NOT advance _last_intent_ts_ms
        # (defensive: a malformed intent should be ignored, not break
        # the cooldown gate for the rest of the session).
        s = self._scheduled(m, _intent("entry", OrderSide.SELL,
                                       timestamp_ms=0))
        # Cooldown gate: 0 vs 10_000 — abs delta is 10_000 which is
        # past cooldown. Schedule should pass but ts stays at 10_000.
        self.assertEqual(len(s), 1)
        self.assertEqual(m._last_intent_ts_ms, 10_000)


class TestOnIntentGuards(_LoopBaseCase):
    """Side / inventory / loop / armed guards on the on_intent path."""

    def test_disarmed_intent_is_dropped(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._loop = self.loop  # loop set but not armed
        m.on_intent(_intent("entry", OrderSide.BUY))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_no_loop_intent_is_dropped(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m.arm()
        # _loop is None — must short-circuit (no loop to schedule on).
        m.on_intent(_intent("entry", OrderSide.BUY))
        self.assertEqual(len(broker.placed_orders), 0)

    def test_same_side_entry_is_dropped(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=0.0)
        m._loop = self.loop
        m.arm()
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1

        scheduled: List = []
        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (scheduled.append(c), c.close(),
                                             type("F", (), {"result": lambda *a, **k: None})())[2]):
            m.on_intent(_intent("entry", OrderSide.BUY))

        self.assertEqual(scheduled, [],
                         "same-side entry must not schedule execution")

    def test_side_flip_closes_then_reverses(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=0.0)
        m._loop = self.loop
        m.arm()
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.1, entry_price=100.0)

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("entry", OrderSide.SELL,
                                action="ENTER_BREAKOUT_SHORT"))

        # close (BUY→SELL) + new SELL = 2 orders
        self.assertEqual(len(broker.placed_orders), 2)
        self.assertEqual(broker.placed_orders[0].signal_type,
                         "CLOSE_ENTER_BREAKOUT_SHORT")
        self.assertEqual(broker.placed_orders[1].signal_type,
                         "ENTER_BREAKOUT_SHORT")
        self.assertEqual(m.current_side, OrderSide.SELL)

    def test_exit_with_no_position_is_noop(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        m._loop = self.loop
        m.arm()
        # Seed a prior cooldown stamp so we can prove the no-op exit
        # leaves it untouched (regression: an earlier revision advanced
        # the stamp BEFORE the position check, polluting the gate).
        m._last_intent_ts_ms = 10_000
        m.on_intent(_intent("exit", side=None, action="EXIT_BOUNCE",
                            timestamp_ms=12_000))
        self.assertEqual(len(broker.placed_orders), 0)
        # No-op exit MUST NOT advance the cooldown stamp.
        self.assertEqual(m._last_intent_ts_ms, 10_000)


class TestOnIntentSizingAndPriceHints(_LoopBaseCase):
    """Sizing math + reference-price priming on the on_intent path."""

    def test_intent_reference_price_seeds_last_signal_price(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._loop = self.loop
        m.arm()
        # Intercept scheduling so we can inspect _last_signal_price
        # before the coroutine fires (which would also seed it).
        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (c.close(),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("entry", OrderSide.BUY,
                                reference_price=42_000.0))
        self.assertEqual(m._last_signal_price, 42_000.0)

    def test_intent_max_position_clamp(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_NOTIONAL, value=1_000_000.0,
            max_position=0.5))
        m._loop = self.loop
        m.arm()
        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("entry", OrderSide.BUY,
                                reference_price=100.0))
        self.assertEqual(len(broker.placed_orders), 1)
        # raw qty = 10_000 → clamped to 0.5
        self.assertAlmostEqual(broker.placed_orders[0].quantity, 0.5)

    def test_intent_zero_qty_is_dropped_silently(self):
        """With FIXED_QTY=0 and no MIN_NOTIONAL fallback (price=0),
        :meth:`_compute_quantity` returns 0.0. The intent path must
        short-circuit cleanly, not place a zero-quantity order."""
        broker = StubBroker()
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_QTY, value=0.0001, max_position=10.0))
        m._loop = self.loop
        m.arm()
        # No reference price supplied AND no _last_signal_price seeded.
        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=lambda c, l: (self.run_async(c),
                                             type("F", (), {"result": lambda *a, **k: None})())[1]):
            m.on_intent(_intent("entry", OrderSide.BUY,
                                reference_price=0.0))
        self.assertEqual(len(broker.placed_orders), 0)


class TestOnSignalSurfaceRemoved(_LoopBaseCase):
    """Phase 14F: the deprecated ``on_signal`` / ``_execute_signal``
    surface was removed. This test pins the removal so a future
    well-intentioned refactor cannot silently re-introduce a
    wall-clock cooldown path on the live broker."""

    def test_on_signal_attribute_does_not_exist(self):
        broker = StubBroker()
        m = _make_manager(broker)
        self.assertFalse(hasattr(m, "on_signal"))
        self.assertFalse(hasattr(m, "_execute_signal"))
        self.assertFalse(hasattr(m, "_last_order_ts"))
        self.assertFalse(hasattr(m, "_on_signal_warning_logged"))


class TestUpdateCooldown(_LoopBaseCase):
    """:meth:`update_cooldown` keeps the seconds-precision public knob
    and the ms-precision internal gate in sync."""

    def test_update_cooldown_recomputes_ms(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=5.0)
        self.assertEqual(m._cooldown_ms, 5000)
        m.update_cooldown(0.5)
        self.assertEqual(m._cooldown_s, 0.5)
        self.assertEqual(m._cooldown_ms, 500)


if __name__ == "__main__":
    # Make warning/error logs visible if a test fails unexpectedly.
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
