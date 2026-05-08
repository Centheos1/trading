"""
Phase 10B — `strategies.orderflow.run_live` integration tests.

Drives the new Python-WS-backed `run_live` against an in-process fake
``websockets`` module and a stub ``orderflow_engine`` (mirroring the
Phase 10 pattern in `test_binance_futures_ws.py`). No network, no real
C++ engine — just confirmation that the function spins up the
Phase 10 WS thread, routes trades/depth into ``engine.process_trade``
and ``engine.process_depth``, primes the REST snapshot, wires the
signal callback, and shuts down cleanly via ``LiveSession.stop()``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies import orderflow as orderflow_mod


# ----------------------------------------------------------------------
# Fake websockets module (lifted from test_binance_futures_ws)

class FakeConnectionClosed(Exception):
    pass


class _Block:
    """Sentinel: next recv() blocks until WS is closed."""


BLOCK = _Block()


class FakeWS:
    def __init__(self, scripted: List[Any]):
        self._items = list(scripted)
        self._closed = asyncio.Event()
        self.closed = False

    async def recv(self):
        if self.closed:
            raise FakeConnectionClosed("ws closed")
        if not self._items:
            await self._closed.wait()
            raise FakeConnectionClosed("ws closed")
        item = self._items.pop(0)
        if isinstance(item, _Block):
            await self._closed.wait()
            raise FakeConnectionClosed("ws closed")
        return item

    async def close(self):
        self.closed = True
        self._closed.set()


class FakeWebsocketsModule:
    def __init__(self, *, on_connect: Callable[[str], FakeWS]):
        self._on_connect = on_connect
        self.ConnectionClosed = FakeConnectionClosed

    async def connect(self, uri, **kwargs):
        return self._on_connect(uri)


# ----------------------------------------------------------------------
# Stub orderflow_engine module

class _StubTrade:
    def __init__(self):
        self.timestamp = 0
        self.price = 0.0
        self.quantity = 0.0
        self.is_buyer_maker = False


class _StubDepthLevel:
    def __init__(self):
        self.price = 0.0
        self.quantity = 0.0


class _StubDepthUpdate:
    def __init__(self):
        self.timestamp = 0
        self.first_update_id = 0
        self.final_update_id = 0
        self.is_snapshot = False
        self.bids = []
        self.asks = []


class _StubLifecycle(SimpleNamespace):
    pass


class _StubRippleCfg(SimpleNamespace):
    def __init__(self):
        super().__init__(
            tick_size=0.0,
            paper_fills=False,
            wall_min_relative_size=0.0,
            absorption_entry=0.0,
            exhaustion_entry=0.0,
            breakout_entry=0.0,
            idle_exit_threshold=0.0,
            bounce_max_break_risk=0.0,
            feature_window_ms=0,
            lifecycle=_StubLifecycle(
                confirmation_window_ms=0,
                max_hold_time_ms=0,
                trailing_stop_sigma=0.0,
                target_distance_sigma=0.0,
            ),
        )


class _StubSignalParams(SimpleNamespace):
    def __init__(self):
        super().__init__(
            imbalance_threshold=0.0,
            stacked_imbalance_levels=0,
            absorption_volume_ratio=0.0,
            cvd_divergence_lookback=0,
            exhaustion_lookback_bars=0,
            signal_strength_min=0.0,
        )


class _StubEngineConfig(SimpleNamespace):
    def __init__(self):
        super().__init__(
            tick_size=0.0,
            signal_params=_StubSignalParams(),
            ripple=_StubRippleCfg(),
        )


class _StubEngine:
    def __init__(self, config):
        self.config = config
        self.start_calls: List[str] = []
        self.stop_calls = 0
        self.trades: List[Any] = []
        self.depth: List[Any] = []
        self.signal_callback: Any = None
        self.tick_store: Any = None
        self.tick_store_symbol: Any = None
        self._raise_on_trade = False
        self._raise_on_depth = False

    def start(self, symbol):
        self.start_calls.append(symbol)

    def stop(self):
        self.stop_calls += 1

    def process_trade(self, t):
        if self._raise_on_trade:
            raise RuntimeError("process_trade boom")
        self.trades.append(t)

    def process_depth(self, u):
        if self._raise_on_depth:
            raise RuntimeError("process_depth boom")
        self.depth.append(u)

    def set_signal_callback(self, cb):
        self.signal_callback = cb

    def set_tick_store(self, store, symbol):
        self.tick_store = store
        self.tick_store_symbol = symbol


class _StubTickStore:
    def __init__(self, path):
        self.path = path


def _make_ofe_stub() -> SimpleNamespace:
    return SimpleNamespace(
        Trade=_StubTrade,
        DepthLevel=_StubDepthLevel,
        DepthUpdate=_StubDepthUpdate,
        EngineConfig=_StubEngineConfig,
        SignalParams=_StubSignalParams,
        OrderFlowEngine=_StubEngine,
        TickStore=_StubTickStore,
    )


# ----------------------------------------------------------------------
# Helpers

def _trade_msg(*, t: int = 1, ts: int = 1_700_000_000_000,
               price: float = 100.0, qty: float = 1.0,
               m: bool = False) -> str:
    return json.dumps({"T": ts, "p": str(price), "q": str(qty), "m": m, "t": t})


def _depth_msg(*, ts: int = 1_700_000_000_000) -> str:
    return json.dumps({
        "E": ts, "U": 1, "u": 2,
        "b": [["100.0", "1.0"]],
        "a": [["101.0", "2.0"]],
    })


def _factory_with_one_trade_and_one_depth():
    def factory(uri: str) -> FakeWS:
        if "@trade" in uri:
            return FakeWS([_trade_msg(t=1), BLOCK])
        return FakeWS([_depth_msg(), BLOCK])
    return factory


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ----------------------------------------------------------------------
# Tests

class TestRunLiveErrors(unittest.TestCase):

    def test_raises_when_ofe_module_missing(self):
        with patch.object(orderflow_mod, "ofe", None):
            with self.assertRaises(RuntimeError):
                orderflow_mod.run_live("BTCUSDT")

    def test_raises_when_websockets_unavailable(self):
        # Inject ofe stub but force websockets_module=None *and* prevent
        # the runtime fallback `import websockets` from succeeding.
        stub = _make_ofe_stub()
        with patch.object(orderflow_mod, "ofe", stub), \
             patch.dict(sys.modules, {"websockets": None}):
            with self.assertRaises(RuntimeError):
                orderflow_mod.run_live(
                    "BTCUSDT", apply_rest_snapshot=False)


class TestRunLiveLifecycle(unittest.TestCase):

    def setUp(self):
        self.stub = _make_ofe_stub()
        self.ws_mod = FakeWebsocketsModule(
            on_connect=_factory_with_one_trade_and_one_depth())

    def test_happy_path_dispatches_trade_and_depth(self):
        with patch.object(orderflow_mod, "ofe", self.stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=self.ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            engine: _StubEngine = session.engine
            ok = _wait_until(
                lambda: len(engine.trades) >= 1 and len(engine.depth) >= 1,
                timeout_s=5.0)
            self.assertTrue(
                ok, f"WS dispatch never landed: trades={len(engine.trades)} "
                    f"depth={len(engine.depth)}")
            self.assertEqual(engine.start_calls, ["BTCUSDT"])
            # Trade fields propagated.
            self.assertEqual(engine.trades[0].price, 100.0)
            self.assertEqual(engine.trades[0].quantity, 1.0)
            # Depth update parsed.
            self.assertEqual(len(engine.depth[0].bids), 1)
            self.assertEqual(len(engine.depth[0].asks), 1)
        finally:
            session.stop()
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(engine.stop_calls, 1)

    def test_stop_is_idempotent(self):
        with patch.object(orderflow_mod, "ofe", self.stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=self.ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        session.stop()
        session.stop()  # must not raise
        self.assertEqual(session.engine.stop_calls, 1)

    def test_context_manager_stops_on_exit(self):
        with patch.object(orderflow_mod, "ofe", self.stub):
            with orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=self.ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            ) as session:
                self.assertTrue(session.thread.is_alive())
        # After context exit thread joined and engine stopped.
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(session.engine.stop_calls, 1)


class TestRunLiveCallbacks(unittest.TestCase):

    def test_signal_callback_is_wired(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))

        seen_signals: List[Any] = []

        def my_cb(sig):
            seen_signals.append(sig)

        with patch.object(orderflow_mod, "ofe", stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=my_cb,
            )
        try:
            engine: _StubEngine = session.engine
            self.assertIs(engine.signal_callback, my_cb)
            engine.signal_callback("fake_signal")
            self.assertEqual(seen_signals, ["fake_signal"])
        finally:
            session.stop()

    def test_signal_callback_none_does_not_override(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))

        with patch.object(orderflow_mod, "ofe", stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            self.assertIsNone(session.engine.signal_callback)
        finally:
            session.stop()

    def test_default_signal_logger_does_not_raise(self):
        # Just exercise the default formatter against a duck-typed signal.
        sig = SimpleNamespace(
            type_name=lambda: "ABSORPTION",
            price=100.0, strength=0.5, description="test",
        )
        orderflow_mod._default_signal_logger(sig)


class TestRunLiveTickStore(unittest.TestCase):

    def test_tick_store_path_registers_store(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))
        with patch.object(orderflow_mod, "ofe", stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                tick_store_path="/tmp/test_run_live.h5",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            self.assertIsInstance(session.engine.tick_store, _StubTickStore)
            self.assertEqual(
                session.engine.tick_store.path, "/tmp/test_run_live.h5")
            self.assertEqual(session.engine.tick_store_symbol, "BTCUSDT")
        finally:
            session.stop()

    def test_no_tick_store_path_leaves_store_unset(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))
        with patch.object(orderflow_mod, "ofe", stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            self.assertIsNone(session.engine.tick_store)
        finally:
            session.stop()


class TestRunLiveRestSnapshot(unittest.TestCase):

    def test_rest_snapshot_primed_and_applied(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))

        # Patch fetch_and_build_depth_snapshot to return a known DepthUpdate.
        canned = _StubDepthUpdate()
        canned.timestamp = 99
        canned.is_snapshot = True
        # Two bids, three asks so we can identify it.
        canned.bids = [_StubDepthLevel(), _StubDepthLevel()]
        canned.asks = [_StubDepthLevel() for _ in range(3)]

        # `run_live` does `from data_feed import fetch_and_build_depth_snapshot`,
        # so we patch the re-export bound on the `data_feed` package.
        import data_feed
        with patch.object(orderflow_mod, "ofe", stub), \
             patch.object(data_feed, "fetch_and_build_depth_snapshot",
                          return_value=canned):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=True,
                signal_callback=None,
            )
        try:
            engine: _StubEngine = session.engine
            # Snapshot was applied first (before any WS data could land).
            self.assertGreaterEqual(len(engine.depth), 1)
            self.assertIs(engine.depth[0], canned)
            self.assertEqual(engine.depth[0].timestamp, 99)
        finally:
            session.stop()

    def test_rest_snapshot_failure_does_not_abort_session(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))

        import data_feed
        with patch.object(orderflow_mod, "ofe", stub), \
             patch.object(data_feed, "fetch_and_build_depth_snapshot",
                          side_effect=RuntimeError("network down")):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=True,
                signal_callback=None,
            )
        try:
            # Engine still started, thread still running.
            self.assertEqual(session.engine.start_calls, ["BTCUSDT"])
            self.assertTrue(session.thread.is_alive())
        finally:
            session.stop()

    def test_apply_rest_snapshot_false_skips_fetch(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS([BLOCK]))

        import data_feed
        with patch.object(orderflow_mod, "ofe", stub), \
             patch.object(data_feed, "fetch_and_build_depth_snapshot") as p:
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            p.assert_not_called()
        finally:
            session.stop()


class TestRunLiveErrorPropagation(unittest.TestCase):

    def test_process_trade_exception_does_not_kill_thread(self):
        stub = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(
            on_connect=lambda uri: FakeWS(
                [_trade_msg(t=1), _trade_msg(t=2), BLOCK])
                if "@trade" in uri else FakeWS([BLOCK]))

        with patch.object(orderflow_mod, "ofe", stub):
            session = orderflow_mod.run_live(
                "BTCUSDT",
                websockets_module=ws_mod,
                apply_rest_snapshot=False,
                signal_callback=None,
            )
        try:
            engine: _StubEngine = session.engine
            engine._raise_on_trade = True
            # Wait for the WS thread to attempt at least one dispatch.
            ok = _wait_until(
                lambda: session.trade_health.messages_received >= 1,
                timeout_s=5.0)
            self.assertTrue(ok, "no trade was ever dispatched")
            self.assertTrue(session.thread.is_alive())
        finally:
            session.stop()
        self.assertFalse(session.thread.is_alive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
