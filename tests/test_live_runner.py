"""
Phase 10C — `execution.live_runner.run_live_execute` tests.

Drives the live-execute orchestrator with a fake `websockets`
module, a stub ``orderflow_engine`` module, and a stub
:class:`ExecutionManager`. Verifies trade/depth dispatch into the
engine, signal routing into the manager, REST snapshot priming, the
status-loop tick cadence, and the disarm-on-shutdown contract — all
without spawning real network or asyncio.
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

from execution import live_runner


# ----------------------------------------------------------------------
# Fake websockets / stub ofe (mirrors test_run_live.py / test_binance_futures_ws.py)

class FakeConnectionClosed(Exception):
    pass


class _Block:
    pass


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


class _StubEngineConfig(SimpleNamespace):
    def __init__(self):
        super().__init__(tick_size=0.0)


class _StubEngine:
    def __init__(self, config):
        self.config = config
        self.start_calls: List[str] = []
        self.stop_calls = 0
        self.trades: List[Any] = []
        self.depth: List[Any] = []
        self.signal_callback: Any = None

    def start(self, symbol):
        self.start_calls.append(symbol)

    def stop(self):
        self.stop_calls += 1

    def process_trade(self, t):
        self.trades.append(t)

    def process_depth(self, u):
        self.depth.append(u)

    def set_signal_callback(self, cb):
        self.signal_callback = cb


def _make_ofe_stub() -> SimpleNamespace:
    return SimpleNamespace(
        Trade=_StubTrade,
        DepthLevel=_StubDepthLevel,
        DepthUpdate=_StubDepthUpdate,
        EngineConfig=_StubEngineConfig,
        OrderFlowEngine=_StubEngine,
    )


class _StubExecMgr:
    def __init__(self):
        self.current_side = None
        self.current_qty = 0.0
        self.orders: List[Any] = []
        self.signals_received: List[Any] = []
        self.disarm_calls: List[bool] = []
        self.stop_calls = 0

    def on_signal(self, sig):
        self.signals_received.append(sig)

    def disarm(self, *, close_position: bool = False):
        self.disarm_calls.append(close_position)

    def stop(self):
        self.stop_calls += 1


# ----------------------------------------------------------------------
# Helpers

def _trade_msg(*, t: int = 1, ts: int = 1_700_000_000_000) -> str:
    return json.dumps({
        "T": ts, "p": "100.0", "q": "1.0", "m": False, "t": t,
    })


def _depth_msg() -> str:
    return json.dumps({
        "E": 1_700_000_000_000, "U": 1, "u": 2,
        "b": [["100.0", "1.0"]],
        "a": [["101.0", "2.0"]],
    })


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _run_in_thread(fn, **kwargs):
    """Run ``fn(**kwargs)`` in a daemon thread so the test can drive
    it via the injected ``interrupt_event``."""
    result = {}

    def target():
        try:
            result["rc"] = fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001
            result["exc"] = exc
    th = threading.Thread(target=target, daemon=True)
    th.start()
    return th, result


# ----------------------------------------------------------------------
# Tests

class TestEngineConfiguration(unittest.TestCase):

    def test_engine_built_with_minimal_config_and_signal_callback_wired(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()
        interrupt = threading.Event()

        # Capture the engine produced inside the runner via a custom factory.
        captured = {}

        def factory(ofe_mod):
            cfg = ofe_mod.EngineConfig()
            cfg.tick_size = 0.123
            eng = ofe_mod.OrderFlowEngine(cfg)
            captured["engine"] = eng
            captured["cfg"] = cfg
            return eng

        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=False,
            status_interval_s=10.0,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            engine_factory=factory,
        )
        try:
            self.assertTrue(_wait_until(
                lambda: "engine" in captured
                and captured["engine"].start_calls == ["BTCUSDT"]))
            engine = captured["engine"]
            cfg = captured["cfg"]
            self.assertEqual(cfg.tick_size, 0.123)
            # Signal callback routes into exec_mgr.on_signal
            engine.signal_callback("fake_signal")
            self.assertEqual(exec_mgr.signals_received, ["fake_signal"])
        finally:
            interrupt.set()
            th.join(timeout=5.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(result.get("rc"), 0)


class TestWebsocketDispatch(unittest.TestCase):

    def test_trade_and_depth_landed_in_engine(self):
        ofe = _make_ofe_stub()

        def factory(uri: str) -> FakeWS:
            if "@trade" in uri:
                return FakeWS([_trade_msg(t=1), BLOCK])
            return FakeWS([_depth_msg(), BLOCK])

        ws_mod = FakeWebsocketsModule(on_connect=factory)
        exec_mgr = _StubExecMgr()
        interrupt = threading.Event()

        captured = {}

        def eng_factory(ofe_mod):
            cfg = ofe_mod.EngineConfig()
            cfg.tick_size = 0.01
            eng = ofe_mod.OrderFlowEngine(cfg)
            captured["engine"] = eng
            return eng

        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=False,
            status_interval_s=10.0,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            engine_factory=eng_factory,
        )
        try:
            ok = _wait_until(
                lambda: "engine" in captured
                and len(captured["engine"].trades) >= 1
                and len(captured["engine"].depth) >= 1,
                timeout_s=5.0,
            )
            self.assertTrue(ok, "trade/depth never landed in engine")
            engine = captured["engine"]
            self.assertEqual(engine.trades[0].price, 100.0)
            self.assertEqual(engine.trades[0].quantity, 1.0)
            self.assertEqual(len(engine.depth[0].bids), 1)
            self.assertEqual(len(engine.depth[0].asks), 1)
        finally:
            interrupt.set()
            th.join(timeout=5.0)
        self.assertEqual(result.get("rc"), 0)


class TestRestSnapshot(unittest.TestCase):

    def test_snapshot_applied_before_ws_thread(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()

        canned = _StubDepthUpdate()
        canned.timestamp = 99
        canned.bids = [_StubDepthLevel()]
        canned.asks = [_StubDepthLevel(), _StubDepthLevel()]

        captured = {}

        def eng_factory(ofe_mod):
            eng = ofe_mod.OrderFlowEngine(ofe_mod.EngineConfig())
            captured["engine"] = eng
            return eng

        def fake_fetch(ofe_mod, url, symbol):
            return canned

        interrupt = threading.Event()
        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=True,
            fetch_depth_snapshot=fake_fetch,
            rest_depth_url="https://example/depth",
            status_interval_s=10.0,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            engine_factory=eng_factory,
        )
        try:
            self.assertTrue(_wait_until(lambda: "engine" in captured))
            engine = captured["engine"]
            ok = _wait_until(lambda: len(engine.depth) >= 1, timeout_s=2.0)
            self.assertTrue(ok)
            self.assertIs(engine.depth[0], canned)
        finally:
            interrupt.set()
            th.join(timeout=5.0)
        self.assertEqual(result.get("rc"), 0)

    def test_snapshot_failure_does_not_abort_session(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()

        captured = {}

        def eng_factory(ofe_mod):
            eng = ofe_mod.OrderFlowEngine(ofe_mod.EngineConfig())
            captured["engine"] = eng
            return eng

        def boom(*a, **kw):
            raise RuntimeError("network down")

        interrupt = threading.Event()
        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=True,
            fetch_depth_snapshot=boom,
            status_interval_s=10.0,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            engine_factory=eng_factory,
        )
        try:
            self.assertTrue(_wait_until(
                lambda: "engine" in captured
                and captured["engine"].start_calls == ["BTCUSDT"]))
        finally:
            interrupt.set()
            th.join(timeout=5.0)
        self.assertEqual(result.get("rc"), 0)


class TestStatusLoopAndShutdown(unittest.TestCase):

    def test_status_printer_invoked_at_interval(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()
        interrupt = threading.Event()

        calls: List[dict] = []

        def my_printer(**kwargs):
            calls.append(kwargs)
            if len(calls) >= 2:
                interrupt.set()

        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=False,
            status_interval_s=0.05,  # ≈ 50 ms tick
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            status_printer=my_printer,
        )
        th.join(timeout=5.0)
        self.assertFalse(th.is_alive())
        self.assertGreaterEqual(len(calls), 2)
        # Printer received the documented kwargs.
        self.assertIn("exec_mgr", calls[0])
        self.assertIn("trade_health", calls[0])
        self.assertIn("depth_health", calls[0])
        self.assertEqual(result.get("rc"), 0)

    def test_shutdown_disarms_with_close_position_and_stops_exec_mgr(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()
        interrupt = threading.Event()

        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=False,
            status_interval_s=10.0,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
        )
        # Let the thread spin up the WS, then ask it to shut down.
        time.sleep(0.05)
        interrupt.set()
        th.join(timeout=5.0)
        self.assertFalse(th.is_alive())
        self.assertEqual(exec_mgr.disarm_calls, [True])
        self.assertEqual(exec_mgr.stop_calls, 1)
        self.assertEqual(result.get("rc"), 0)

    def test_status_printer_exception_does_not_kill_loop(self):
        ofe = _make_ofe_stub()
        ws_mod = FakeWebsocketsModule(on_connect=lambda uri: FakeWS([BLOCK]))
        exec_mgr = _StubExecMgr()
        interrupt = threading.Event()
        call_count = {"n": 0}

        def boom(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("printer boom")
            interrupt.set()

        th, result = _run_in_thread(
            live_runner.run_live_execute,
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=ofe,
            websockets_module=ws_mod,
            apply_rest_snapshot=False,
            status_interval_s=0.05,
            join_timeout_s=2.0,
            disarm_grace_s=0.0,
            interrupt_event=interrupt,
            status_printer=boom,
        )
        th.join(timeout=5.0)
        self.assertFalse(th.is_alive())
        self.assertGreaterEqual(call_count["n"], 2)
        self.assertEqual(result.get("rc"), 0)


class TestDefaultStatusPrinter(unittest.TestCase):

    def test_default_printer_runs_without_raising(self):
        # Just exercise the formatter against duck-typed inputs.
        exec_mgr = SimpleNamespace(
            current_side=SimpleNamespace(value="BUY"),
            current_qty=0.5,
            orders=[1, 2, 3],
        )
        trade_health = SimpleNamespace(short_status="LIVE")
        depth_health = SimpleNamespace(short_status="LIVE")
        live_runner._default_status_printer(
            exec_mgr=exec_mgr,
            trade_health=trade_health,
            depth_health=depth_health,
        )

    def test_default_printer_handles_none_side(self):
        exec_mgr = SimpleNamespace(
            current_side=None, current_qty=0.0, orders=[])
        trade_health = SimpleNamespace(short_status="STALE")
        depth_health = SimpleNamespace(short_status="STALE")
        live_runner._default_status_printer(
            exec_mgr=exec_mgr,
            trade_health=trade_health,
            depth_health=depth_health,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
