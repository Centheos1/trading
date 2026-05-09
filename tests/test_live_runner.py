"""Phase 14A — live_runner topology tests.

Pins the wiring between :func:`execution.live_runner.run_live_execute`
and :class:`execution.execution_manager.ExecutionManager`. The
critical invariant is that the LIVE path consumes ``RippleDecision``
intents (which already pass through the lifecycle FSM, Wave
permissions, and ``RiskEngine``) — NOT raw ``Signal`` events.
``set_signal_callback`` is reserved for the optional recorder.

These tests are pure unit tests: they stub the C++ engine, the
``websockets`` package, the depth-snapshot fetcher, and the WS feed
runner so no real network / threads / GIL contention occurs.
"""
from __future__ import annotations

import logging
import sys
import threading
import unittest
from pathlib import Path
from typing import Any, Callable, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.live_runner import run_live_execute
from execution.models import (
    ExecutionIntent,
    OrderSide,
)


# ----------------------------------------------------------------------
# Stubs

class _StubExecMgr:
    """Mimics :class:`ExecutionManager`'s public surface used by
    ``run_live_execute``."""
    def __init__(self) -> None:
        self.on_intent_calls: List[ExecutionIntent] = []
        self.on_signal_calls: List[Any] = []
        self.disarm_calls = 0
        self.stop_calls = 0
        self.disarm_close_position: List[bool] = []
        self.current_side: Optional[OrderSide] = None
        self.current_qty: float = 0.0
        self.orders: List[Any] = []
        self.armed = True

    def on_intent(self, intent: ExecutionIntent) -> None:
        self.on_intent_calls.append(intent)

    def on_signal(self, signal) -> None:
        self.on_signal_calls.append(signal)

    def disarm(self, close_position: bool = True) -> None:
        self.disarm_calls += 1
        self.disarm_close_position.append(close_position)

    def stop(self) -> None:
        self.stop_calls += 1


class _StubEngine:
    """Mimics ``orderflow_engine.OrderFlowEngine``'s public surface
    touched by ``run_live_execute``. Captures the callbacks the
    runner registers so tests can fire synthetic decisions / signals
    at them and inspect downstream effects."""
    def __init__(self) -> None:
        self.signal_callback: Optional[Callable] = None
        self.ripple_callback: Optional[Callable] = None
        self.start_calls = 0
        self.stop_calls = 0
        self.process_depth_calls = 0
        self.process_trade_calls = 0
        self._cfg = object()

    def set_signal_callback(self, cb: Callable) -> None:
        self.signal_callback = cb

    def set_ripple_callback(self, cb: Callable) -> None:
        self.ripple_callback = cb

    def get_config(self) -> Any:
        return self._cfg

    def start(self, _symbol: str) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def process_depth(self, _u: Any) -> None:
        self.process_depth_calls += 1

    def process_trade(self, _t: Any) -> None:
        self.process_trade_calls += 1


class _StubOFEModule:
    """Stand-in for the ``orderflow_engine`` C++ module."""
    pass


class _NoopWebsocketsModule:
    """Stand-in for the ``websockets`` package — never invoked by
    these tests because we patch out the WS feed runner."""
    pass


class _StubRecorder:
    """Captures sidecar writes; exposes the same public surface as
    :class:`tools.session_recorder.SessionRecorder`."""
    def __init__(self) -> None:
        self.header_written = False
        self.signal_calls: List[Any] = []
        self.ripple_decision_calls: List[Any] = []
        self.write_header_kwargs: dict = {}
        # Mirror SessionRecorder's "_header_written" private attr so
        # ``getattr(recorder, '_header_written', False)`` in the
        # runner works the same way.
        self._header_written = False

    def write_header(self, *, symbol: str, engine_config: Any) -> None:
        self.write_header_kwargs = dict(symbol=symbol,
                                        engine_config=engine_config)
        self._header_written = True

    def record_signal(self, sig: Any) -> None:
        self.signal_calls.append(sig)

    def record_ripple_decision(self, decision: Any) -> None:
        self.ripple_decision_calls.append(decision)


def _make_decision(
    *,
    intent_name: str = "ENTER_BREAKOUT_LONG",
    timestamp: int = 1_700_000_000_000,
    reference_price: float = 42_000.0,
    confidence: float = 0.8,
    reason: str = "wall fail confirmed",
    wall_id: int = 7,
):
    """Build a duck-typed RippleDecision compatible with
    :func:`execution.models.ripple_decision_to_intent`."""
    class _Intent:
        def __init__(self, name: str) -> None:
            self._name = name
        def __str__(self) -> str:
            return f"RippleIntent.{self._name}"

    class _Decision:
        def __init__(self) -> None:
            self.intent = _Intent(intent_name)
            self.timestamp = timestamp
            self.reference_price = reference_price
            self.confidence = confidence
            self.reason = reason
            self.wall_id = wall_id
            self.invalidation_price = 0.0
    return _Decision()


def _make_signal(type_name: str = "BUY_RIPPLE", price: float = 100.0):
    class _Sig:
        def __init__(self, t: str, p: float) -> None:
            self._t = t
            self.price = p
        def type_name(self) -> str:
            return self._t
    return _Sig(type_name, price)


def _run_live_execute_one_pass(
    *,
    exec_mgr: _StubExecMgr,
    engine: _StubEngine,
    recorder: Optional[_StubRecorder] = None,
    apply_rest_snapshot: bool = False,
) -> int:
    """Drive ``run_live_execute`` for a single pass with the WS feed
    runner stubbed and the main loop short-circuited via
    ``interrupt_event``."""
    interrupt_event = threading.Event()
    interrupt_event.set()  # main loop falls through immediately

    # Patch the feed runner so no WS thread spins up against the
    # network / asyncio scheduler.
    def _noop_ws_feed(**_kwargs):
        return None

    with patch("data_feed.run_binance_usdm_futures_ws_feed",
               new=_noop_ws_feed), \
         patch("data_feed.stream_health.FeedStreamHealth"):
        rc = run_live_execute(
            symbol="BTCUSDT",
            exec_mgr=exec_mgr,
            ofe_module=_StubOFEModule(),
            websockets_module=_NoopWebsocketsModule(),
            apply_rest_snapshot=apply_rest_snapshot,
            engine_factory=lambda _ofe: engine,
            interrupt_event=interrupt_event,
            disarm_grace_s=0.0,
            recorder=recorder,
        )
    return rc


# ----------------------------------------------------------------------
# Tests

class TestRippleDrivenTopology(unittest.TestCase):
    """Phase 14A core: ``set_ripple_callback`` is the live execution
    subscription point; ``set_signal_callback`` is observation-only."""

    def test_no_recorder_set_ripple_callback_is_wired(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        self.assertIsNotNone(engine.ripple_callback,
                             "live_runner must wire set_ripple_callback")

    def test_no_recorder_set_signal_callback_is_not_wired(self):
        """Without a recorder there is no observation channel to
        feed, so ``set_signal_callback`` must NOT be registered.
        Routing the broker through signals is exactly the V1
        contract violation Phase 14A closes."""
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        self.assertIsNone(engine.signal_callback,
                          "no-recorder path must not subscribe to signals")

    def test_ripple_decision_routed_as_intent_to_exec_mgr(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)

        # Fire a synthetic Ripple decision through the registered cb.
        engine.ripple_callback(_make_decision(
            intent_name="ENTER_BREAKOUT_LONG",
            reference_price=42_000.0,
            timestamp=1_700_000_000_000))

        self.assertEqual(len(exec_mgr.on_intent_calls), 1)
        intent = exec_mgr.on_intent_calls[0]
        self.assertIsInstance(intent, ExecutionIntent)
        self.assertEqual(intent.intent_type, "entry")
        self.assertEqual(intent.side, OrderSide.BUY)
        self.assertEqual(intent.action, "ENTER_BREAKOUT_LONG")
        self.assertEqual(intent.timestamp, 1_700_000_000_000)
        self.assertEqual(intent.reference_price, 42_000.0)

    def test_no_action_decision_does_not_call_on_intent(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        engine.ripple_callback(_make_decision(intent_name="NO_ACTION"))
        self.assertEqual(len(exec_mgr.on_intent_calls), 0)

    def test_unknown_intent_does_not_call_on_intent(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        engine.ripple_callback(_make_decision(intent_name="MYSTERY_INTENT"))
        self.assertEqual(len(exec_mgr.on_intent_calls), 0)

    def test_on_signal_is_never_called_on_no_recorder_path(self):
        """Regression: the legacy signal-driven topology placed orders
        from raw SignalEngine signals. Phase 14A removes that path."""
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        # No signal_callback was registered, so even if the engine
        # tried to fire one, exec_mgr.on_signal would not be called.
        self.assertEqual(len(exec_mgr.on_signal_calls), 0)

    def test_exit_decision_routes_as_exit_intent(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        engine.ripple_callback(_make_decision(
            intent_name="EXIT_BREAKOUT", reference_price=42_500.0))
        self.assertEqual(len(exec_mgr.on_intent_calls), 1)
        intent = exec_mgr.on_intent_calls[0]
        self.assertEqual(intent.intent_type, "exit")
        self.assertIsNone(intent.side)


class TestRecorderObservation(unittest.TestCase):
    """When a recorder is attached, signals AND ripple decisions are
    captured for the sidecar but only ripple decisions drive
    execution."""

    def test_recorder_captures_signals_for_observation(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        rec = _StubRecorder()
        _run_live_execute_one_pass(
            exec_mgr=exec_mgr, engine=engine, recorder=rec)
        self.assertIsNotNone(engine.signal_callback)
        engine.signal_callback(_make_signal("BUY_RIPPLE", 100.0))
        self.assertEqual(len(rec.signal_calls), 1)
        # Critical: signals MUST NOT drive on_intent.
        self.assertEqual(len(exec_mgr.on_intent_calls), 0)
        # And they MUST NOT drive on_signal either (the deprecated
        # entry point exists but the runner no longer calls it).
        self.assertEqual(len(exec_mgr.on_signal_calls), 0)

    def test_recorder_captures_ripple_decisions_alongside_intent(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        rec = _StubRecorder()
        _run_live_execute_one_pass(
            exec_mgr=exec_mgr, engine=engine, recorder=rec)
        decision = _make_decision(intent_name="ENTER_BOUNCE_SHORT",
                                  reference_price=42_300.0)
        engine.ripple_callback(decision)
        # Recorder saw the raw decision (sidecar use) and exec_mgr
        # received the converted intent (live execution).
        self.assertEqual(len(rec.ripple_decision_calls), 1)
        self.assertIs(rec.ripple_decision_calls[0], decision)
        self.assertEqual(len(exec_mgr.on_intent_calls), 1)
        self.assertEqual(exec_mgr.on_intent_calls[0].side, OrderSide.SELL)

    def test_recorder_write_header_invoked_with_engine_config(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        rec = _StubRecorder()
        _run_live_execute_one_pass(
            exec_mgr=exec_mgr, engine=engine, recorder=rec)
        self.assertEqual(rec.write_header_kwargs.get("symbol"), "BTCUSDT")
        # engine_config is the placeholder object the stub returns.
        self.assertIs(rec.write_header_kwargs.get("engine_config"),
                      engine._cfg)

    def test_recorder_exception_does_not_break_intent_dispatch(self):
        """If the recorder raises while writing a sidecar event, the
        live execution path must still receive the intent."""
        class BoomyRecorder(_StubRecorder):
            def record_ripple_decision(self, decision: Any) -> None:
                raise RuntimeError("disk full")
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        rec = BoomyRecorder()
        _run_live_execute_one_pass(
            exec_mgr=exec_mgr, engine=engine, recorder=rec)
        engine.ripple_callback(_make_decision())
        # Recorder raised — but exec_mgr still received the intent.
        self.assertEqual(len(exec_mgr.on_intent_calls), 1)

    def test_exec_mgr_exception_does_not_kill_callback(self):
        """If on_intent raises, the runner's exception handler must
        absorb it so the WS thread keeps running."""
        class BoomyExec(_StubExecMgr):
            def on_intent(self, intent: ExecutionIntent) -> None:
                raise RuntimeError("broker down")
        exec_mgr = BoomyExec()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        # Must not propagate.
        engine.ripple_callback(_make_decision())


class TestShutdownLifecycle(unittest.TestCase):
    """Sanity: the runner still calls disarm/stop on the exec manager."""

    def test_disarm_close_position_called_on_clean_exit(self):
        exec_mgr = _StubExecMgr()
        engine = _StubEngine()
        _run_live_execute_one_pass(exec_mgr=exec_mgr, engine=engine)
        self.assertEqual(exec_mgr.disarm_calls, 1)
        self.assertEqual(exec_mgr.disarm_close_position, [True])
        self.assertEqual(exec_mgr.stop_calls, 1)
        self.assertEqual(engine.stop_calls, 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
