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
                          order_type: OrderType = OrderType.MARKET) -> Order:
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

def _signal(type_name: str = "BUY", price: float = 100.0):
    """Tiny stand-in for the C++ signal object used by `on_signal`."""
    class _Sig:
        def __init__(self, t, p):
            self._t = t
            self.price = p
        def type_name(self):
            return self._t
    return _Sig(type_name, price)


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


class TestOnSignalGates(_LoopBaseCase):

    def test_on_signal_when_disarmed_is_noop(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._loop = self.loop  # so the schedule path could run, but armed=False
        m.on_signal(_signal("BUY", 100.0))
        self.assertEqual(broker.account_calls, 0)
        self.assertEqual(len(broker.placed_orders), 0)

    def test_on_signal_within_cooldown_dropped(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=10.0)
        m._loop = self.loop
        m.arm()
        m._last_order_ts = time.time()  # just fired
        m.on_signal(_signal("BUY", 100.0))
        # No coroutine scheduled because cooldown gate fired before
        # asyncio.run_coroutine_threadsafe is reached.
        self.assertEqual(len(broker.placed_orders), 0)

    def test_on_signal_same_side_dropped(self):
        broker = StubBroker()
        m = _make_manager(broker, cooldown_s=0.0)
        m._loop = self.loop
        m.arm()
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1
        # Capture whether anything got scheduled.
        scheduled: List = []

        real_run_threadsafe = asyncio.run_coroutine_threadsafe

        def fake_run_threadsafe(coro, loop):
            scheduled.append(coro)
            # Cancel the coroutine to avoid resource warnings.
            coro.close()
            class _F:
                def result(self, timeout=None): return None
            return _F()

        with patch("execution.execution_manager.asyncio.run_coroutine_threadsafe",
                   side_effect=fake_run_threadsafe):
            m.on_signal(_signal("BUY", 100.0))

        self.assertEqual(scheduled, [],
                         "same-side signal must not schedule _execute_signal")


class TestExecuteSignal(_LoopBaseCase):

    def test_happy_path_places_order_and_updates_state(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._last_signal_price = 100.0
        # _execute_signal short-circuits the close branch when qty==0
        self.run_async(m._execute_signal(OrderSide.BUY, "ENTER_LONG"))
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].signal_type, "ENTER_LONG")
        self.assertEqual(m.current_side, OrderSide.BUY)
        self.assertGreater(m.current_qty, 0.0)

    def test_reverse_position_closes_first_then_places_new(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._last_signal_price = 100.0
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1
        broker.position = Position(symbol="BTCUSDT", side=OrderSide.BUY,
                                   quantity=0.1, entry_price=100.0)
        self.run_async(m._execute_signal(OrderSide.SELL, "ENTER_SHORT"))
        # close (BUY→SELL) + new order (SELL) = 2 orders
        self.assertEqual(len(broker.placed_orders), 2)
        self.assertEqual(broker.placed_orders[0].signal_type, "CLOSE_ENTER_SHORT")
        self.assertEqual(broker.placed_orders[1].signal_type, "ENTER_SHORT")
        self.assertEqual(m.current_side, OrderSide.SELL)

    def test_close_not_filled_returns_early(self):
        broker = StubBroker()
        m = _make_manager(broker)
        m._last_signal_price = 100.0
        m._current_side = OrderSide.BUY
        m._current_qty = 0.1
        # Force close_position to return a non-filled order.
        broker.next_close_order = Order(
            symbol="BTCUSDT", side=OrderSide.SELL, quantity=0.1,
            status=OrderStatus.REJECTED, error_message="exchange down")
        self.run_async(m._execute_signal(OrderSide.SELL, "ENTER_SHORT"))
        # Only the close attempt was recorded; no new order placed.
        self.assertEqual(len(broker.placed_orders), 1)
        self.assertEqual(broker.placed_orders[0].status, OrderStatus.REJECTED)
        # State remains as it was (close did not flip the position).
        self.assertEqual(m.current_side, OrderSide.BUY)
        self.assertEqual(m.current_qty, 0.1)

    def test_record_order_callback_exception_swallowed(self):
        seen: List[Order] = []

        def boom(o):
            seen.append(o)
            raise RuntimeError("callback failure")

        broker = StubBroker()
        m = _make_manager(broker, callback=boom)
        m._last_signal_price = 100.0
        # Must not raise; deque still appends.
        self.run_async(m._execute_signal(OrderSide.BUY, "ENTER_LONG"))
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


class TestExecuteSignalMaxPositionClamp(_LoopBaseCase):
    """`_execute_signal` clamps quantity to `_sizing.max_position` after
    `_compute_quantity` returns. This is verified at the order surface."""

    def test_quantity_clamped_to_max_position(self):
        broker = StubBroker(step_size=0.0)
        m = _make_manager(broker, sizing=SizingConfig(
            mode=SizingMode.FIXED_NOTIONAL, value=1_000_000.0,
            max_position=0.5))
        m._last_signal_price = 100.0
        self.run_async(m._execute_signal(OrderSide.BUY, "ENTER_LONG"))
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


if __name__ == "__main__":
    # Make warning/error logs visible if a test fails unexpectedly.
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
