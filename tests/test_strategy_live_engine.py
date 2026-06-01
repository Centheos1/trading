"""Phase 21 — distributed strategy-service live-execution compliance.

Pins the V1 contract on the *service* path
(``strategy/engine/execution_bridge.py`` + ``strategy/engine/live_engine.py``),
mirroring the monolith assertions in
``tests/test_live_execution_v1_compliance.py`` +
``tests/test_layered_live_wiring.py`` but for the Redis/FastAPI topology.

These tests are deliberately C++-free: the C++ ``orderflow_engine``
module is stubbed so the suite runs in any sandbox. The contracts pinned:

  1. A RippleDecision stream produces ``on_intent`` calls with the
     correct intent_type / side / qty / event-timestamp.
  2. The risk gate blocks under ES-exhausted / Wave-DISABLED /
     Tide-CRISIS / max-position — zero ``on_intent`` calls.
  3. Exits are NEVER blocked.
  4. ``ARM_EXECUTION=false`` (OBSERVE) → zero target orders even when a
     target is wired.
  5. Cooldown + two-trades-concurrent are re-pinned through a real
     ``ExecutionManager`` driven by the bridge.
  6. ``LiveEngine`` wires ``set_ripple_callback`` (not signal-driven
     routing), spawns the layered-push thread, and feeds Wave/RV from
     the trade stream.
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from execution.models import OrderSide  # noqa: E402
from strategy.engine.execution_bridge import (  # noqa: E402
    ExecutionBridge,
    ExecutionMode,
    resolve_mode,
)


# ----------------------------------------------------------------------
# Stubs (no C++ build required)
# ----------------------------------------------------------------------


class _Intent:
    """Stand-in for a C++ ``RippleIntent`` enum member.

    ``execution.models._parse_intent_name`` does
    ``str(decision.intent).split(".")[-1]`` so we only need a faithful
    ``__str__``.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __str__(self) -> str:
        return f"RippleIntent.{self._name}"


def _decision(
    name: str,
    *,
    ts: int = 1_000,
    ref: float = 100.0,
    confidence: float = 0.9,
    reason: str = "test",
    wall_id: int = 0,
    invalidation_price: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        intent=_Intent(name),
        timestamp=ts,
        reference_price=ref,
        confidence=confidence,
        reason=reason,
        wall_id=wall_id,
        invalidation_price=invalidation_price,
    )


class _StubPermissions:
    def __init__(self, frac: float = 1.0) -> None:
        self._frac = frac

    def size_fraction(self, _arch: Any, _side: Any) -> float:
        return self._frac


class _StubRisk:
    def __init__(
        self,
        *,
        es_budget: float = 1_000.0,
        consumed_es: float = 0.0,
        risk_multiplier: float = 1.0,
        max_position_usd: float = 1_000_000.0,
    ) -> None:
        self.es_budget = es_budget
        self.consumed_es = consumed_es
        self.risk_multiplier = risk_multiplier
        self.max_position_usd = max_position_usd


class _StubSnapshot:
    def __init__(self, *, wave_frac: float = 1.0, risk: Optional[_StubRisk] = None) -> None:
        self.wave = SimpleNamespace(permissions=_StubPermissions(wave_frac))
        self.risk = risk if risk is not None else _StubRisk()


class _StubEngine:
    """Mimics the parts of ``OrderFlowEngine`` the bridge touches."""

    def __init__(self, snapshot: Optional[_StubSnapshot] = None) -> None:
        self._snapshot = snapshot if snapshot is not None else _StubSnapshot()
        self.ripple_callback = None
        self.signal_callback = None

    def get_strategy_snapshot(self) -> _StubSnapshot:
        return self._snapshot

    def set_ripple_callback(self, cb) -> None:
        self.ripple_callback = cb

    def set_signal_callback(self, cb) -> None:
        self.signal_callback = cb


class _StubOfeModule:
    """Provides the enum surfaces ``intent_risk_block_reason`` resolves."""

    TradeArchetype = SimpleNamespace(BOUNCE="BOUNCE", BREAKOUT="BREAKOUT")
    TradeSide = SimpleNamespace(LONG="LONG", SHORT="SHORT")


class _StubTarget:
    """Records ``on_intent`` calls. Optionally tracks position qty."""

    def __init__(self, current_qty: float = 0.0) -> None:
        self.intents: List[Any] = []
        self.current_qty = current_qty
        self.armed = False

    def on_intent(self, intent: Any) -> None:
        self.intents.append(intent)

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        self.armed = False


def _make_bridge(
    *,
    mode: ExecutionMode = ExecutionMode.LIVE,
    snapshot: Optional[_StubSnapshot] = None,
    target: Optional[_StubTarget] = None,
) -> tuple[ExecutionBridge, _StubEngine, _StubTarget]:
    engine = _StubEngine(snapshot)
    tgt = target if target is not None else _StubTarget()
    bridge = ExecutionBridge(
        symbol="BTCUSDT",
        ofe_module=_StubOfeModule(),
        mode=mode,
        target=tgt,
    )
    return bridge, engine, tgt


# ----------------------------------------------------------------------
# 1. Happy path — decode + dispatch
# ----------------------------------------------------------------------


class TestDispatchHappyPath(unittest.TestCase):
    def test_resolve_mode(self):
        self.assertIs(resolve_mode(False, "binance"), ExecutionMode.OBSERVE)
        self.assertIs(resolve_mode(False, "paper"), ExecutionMode.OBSERVE)
        self.assertIs(resolve_mode(True, "binance"), ExecutionMode.LIVE)
        self.assertIs(resolve_mode(True, "live"), ExecutionMode.LIVE)
        self.assertIs(resolve_mode(True, "paper"), ExecutionMode.PAPER)
        self.assertIs(resolve_mode(True, "anything"), ExecutionMode.PAPER)

    def test_entry_long_produces_buy_intent(self):
        bridge, engine, tgt = _make_bridge()
        cb = bridge.make_ripple_callback(engine)
        cb(_decision("ENTER_BOUNCE_LONG", ts=12_345, ref=101.5))
        self.assertEqual(len(tgt.intents), 1)
        intent = tgt.intents[0]
        self.assertEqual(intent.intent_type, "entry")
        self.assertIs(intent.side, OrderSide.BUY)
        self.assertEqual(intent.timestamp, 12_345)
        self.assertEqual(intent.action, "ENTER_BOUNCE_LONG")
        self.assertAlmostEqual(intent.reference_price, 101.5)

    def test_entry_short_produces_sell_intent(self):
        bridge, engine, tgt = _make_bridge()
        bridge.make_ripple_callback(engine)(_decision("ENTER_BREAKOUT_SHORT"))
        self.assertEqual(len(tgt.intents), 1)
        self.assertIs(tgt.intents[0].side, OrderSide.SELL)

    def test_no_action_is_dropped(self):
        bridge, engine, tgt = _make_bridge()
        bridge.make_ripple_callback(engine)(_decision("NO_ACTION"))
        self.assertEqual(len(tgt.intents), 0)


# ----------------------------------------------------------------------
# 2. Risk gate — six failure modes (gate-level)
# ----------------------------------------------------------------------


class TestRiskGateBlocks(unittest.TestCase):
    def _drive_entry(self, snapshot, *, current_qty=0.0):
        target = _StubTarget(current_qty=current_qty)
        bridge, engine, tgt = _make_bridge(snapshot=snapshot, target=target)
        bridge.make_ripple_callback(engine)(
            _decision("ENTER_BOUNCE_LONG", ref=100.0))
        return bridge, tgt

    def test_es_exhausted_blocks(self):
        snap = _StubSnapshot(risk=_StubRisk(es_budget=10.0, consumed_es=10.0))
        bridge, tgt = self._drive_entry(snap)
        self.assertEqual(len(tgt.intents), 0)
        self.assertEqual(bridge._last_block_reason, "ES_EXHAUSTED")

    def test_wave_disabled_blocks(self):
        snap = _StubSnapshot(wave_frac=0.0)
        bridge, tgt = self._drive_entry(snap)
        self.assertEqual(len(tgt.intents), 0)
        self.assertTrue(bridge._last_block_reason.startswith("WAVE_DISABLED"))

    def test_tide_crisis_blocks(self):
        snap = _StubSnapshot(risk=_StubRisk(risk_multiplier=0.0))
        bridge, tgt = self._drive_entry(snap)
        self.assertEqual(len(tgt.intents), 0)
        self.assertEqual(bridge._last_block_reason, "TIDE_CRISIS")

    def test_max_position_blocks(self):
        snap = _StubSnapshot(risk=_StubRisk(max_position_usd=100.0))
        # current_qty * ref = 2 * 100 = 200 USD >= 100 cap
        bridge, tgt = self._drive_entry(snap, current_qty=2.0)
        self.assertEqual(len(tgt.intents), 0)
        self.assertEqual(bridge._last_block_reason, "MAX_POSITION_EXCEEDED")

    def test_block_reason_counts_accumulate(self):
        snap = _StubSnapshot(risk=_StubRisk(risk_multiplier=0.0))
        target = _StubTarget()
        bridge, engine, tgt = _make_bridge(snapshot=snap, target=target)
        cb = bridge.make_ripple_callback(engine)
        cb(_decision("ENTER_BOUNCE_LONG"))
        cb(_decision("ENTER_BREAKOUT_SHORT"))
        self.assertEqual(len(tgt.intents), 0)
        self.assertEqual(bridge._blocked, 2)
        self.assertEqual(bridge._block_reason_counts["TIDE_CRISIS"], 2)


# ----------------------------------------------------------------------
# 3. Exits are never blocked
# ----------------------------------------------------------------------


class TestExitsNeverBlocked(unittest.TestCase):
    def test_exit_passes_even_when_all_gates_red(self):
        snap = _StubSnapshot(
            wave_frac=0.0,
            risk=_StubRisk(es_budget=1.0, consumed_es=99.0, risk_multiplier=0.0,
                           max_position_usd=1.0),
        )
        target = _StubTarget(current_qty=5.0)
        bridge, engine, tgt = _make_bridge(snapshot=snap, target=target)
        bridge.make_ripple_callback(engine)(_decision("EXIT_BOUNCE"))
        self.assertEqual(len(tgt.intents), 1)
        self.assertEqual(tgt.intents[0].intent_type, "exit")
        self.assertEqual(bridge._blocked, 0)


# ----------------------------------------------------------------------
# 4. OBSERVE mode never places orders
# ----------------------------------------------------------------------


class TestObserveMode(unittest.TestCase):
    def test_observe_never_forwards_even_with_target(self):
        # A target is wired but mode is OBSERVE → it must never see an intent.
        target = _StubTarget()
        bridge, engine, tgt = _make_bridge(
            mode=ExecutionMode.OBSERVE, target=target)
        self.assertFalse(bridge.armed)
        cb = bridge.make_ripple_callback(engine)
        cb(_decision("ENTER_BOUNCE_LONG"))
        cb(_decision("EXIT_BOUNCE"))
        self.assertEqual(len(tgt.intents), 0)
        # would-execute decisions are still counted for observability.
        self.assertEqual(bridge._dispatched, 2)

    def test_observe_arm_request_is_noop(self):
        bridge, engine, tgt = _make_bridge(mode=ExecutionMode.OBSERVE)
        self.assertFalse(bridge.set_armed(True))
        self.assertFalse(bridge.armed)

    def test_disarmed_live_target_does_not_forward(self):
        bridge, engine, tgt = _make_bridge(mode=ExecutionMode.LIVE)
        bridge.set_armed(False)
        bridge.make_ripple_callback(engine)(_decision("ENTER_BOUNCE_LONG"))
        self.assertEqual(len(tgt.intents), 0)


# ----------------------------------------------------------------------
# 5. Cooldown + two-trades via a real ExecutionManager (no C++)
# ----------------------------------------------------------------------


class TestExecutionManagerIntegration(unittest.TestCase):
    """Drive a real ExecutionManager + StubBroker through the bridge."""

    def _build(self, cooldown_s: float):
        from execution.execution_manager import ExecutionManager
        from execution.models import SizingConfig, SizingMode
        from tests.test_execution_manager import StubBroker

        broker = StubBroker()
        mgr = ExecutionManager(
            broker=broker, symbol="BTCUSDT",
            sizing=SizingConfig(mode=SizingMode.FIXED_QTY, value=0.1,
                                max_position=10.0),
            cooldown_s=cooldown_s,
        )
        mgr.start()
        mgr.arm()
        bridge = ExecutionBridge(
            symbol="BTCUSDT", ofe_module=_StubOfeModule(),
            mode=ExecutionMode.LIVE, target=mgr,
        )
        engine = _StubEngine()
        return broker, mgr, bridge, engine

    def test_cooldown_blocks_second_rapid_entry(self):
        broker, mgr, bridge, engine = self._build(cooldown_s=5.0)
        try:
            cb = bridge.make_ripple_callback(engine)
            cb(_decision("ENTER_BOUNCE_LONG", ts=1_000, ref=100.0))
            # Within cooldown window (1.0s < 5.0s) and opposite side so the
            # inventory guard does not pre-empt the cooldown gate.
            cb(_decision("ENTER_BREAKOUT_SHORT", ts=2_000, ref=100.0))
            time.sleep(0.3)
            self.assertEqual(len(broker.placed_orders), 1)
        finally:
            mgr.stop()

    def test_same_side_concurrent_entry_suppressed(self):
        broker, mgr, bridge, engine = self._build(cooldown_s=0.0)
        try:
            cb = bridge.make_ripple_callback(engine)
            cb(_decision("ENTER_BOUNCE_LONG", ts=1_000, ref=100.0))
            time.sleep(0.2)
            cb(_decision("ENTER_BOUNCE_LONG", ts=2_000, ref=100.0))
            time.sleep(0.2)
            # Second same-side entry while long → suppressed by the manager.
            self.assertEqual(len(broker.placed_orders), 1)
        finally:
            mgr.stop()


# ----------------------------------------------------------------------
# 6. LiveEngine service wiring (C++ module stubbed)
# ----------------------------------------------------------------------


class _StubRipple:
    def __init__(self) -> None:
        self.risk_budget_calls: List[tuple] = []
        self.wave_snapshot_calls: List[Any] = []
        self.realized_vol_calls: List[float] = []

    def set_risk_budget(self, es, mp, rm) -> None:
        self.risk_budget_calls.append((es, mp, rm))

    def set_wave_snapshot(self, snap) -> None:
        self.wave_snapshot_calls.append(snap)

    def set_realized_vol(self, vol) -> None:
        self.realized_vol_calls.append(vol)


class _StubOFEngine:
    """Stub OrderFlowEngine for LiveEngine wiring tests."""

    def __init__(self) -> None:
        self.ripple = _StubRipple()
        self.ripple_callback = None
        self.signal_callback = None
        self.trades: List[Any] = []
        self._snapshot = _StubSnapshot()

    def set_ripple_callback(self, cb) -> None:
        self.ripple_callback = cb

    def set_signal_callback(self, cb) -> None:
        self.signal_callback = cb

    def get_ripple(self):
        return self.ripple

    def get_strategy_snapshot(self):
        return self._snapshot

    def process_trade(self, t) -> None:
        self.trades.append(t)


class _StubOFEModuleFull(_StubOfeModule):
    """Adds the constructors LiveEngine touches."""

    OrderFlowEngine = _StubOFEngine

    class Trade:
        def __init__(self) -> None:
            self.timestamp = 0
            self.price = 0.0
            self.quantity = 0.0
            self.is_buyer_maker = False


def _import_live_engine():
    from strategy.engine import live_engine
    return live_engine


class TestLiveEngineWiring(unittest.TestCase):
    def setUp(self):
        self.le = _import_live_engine()
        self._patch = patch.object(self.le, "ofe", _StubOFEModuleFull())
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_observe_build_wires_ripple_callback_not_routing_signal(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=False)
        eng._build_engines()
        state = eng._states["BTCUSDT"]
        self.assertIsNotNone(state.engine.ripple_callback)
        self.assertIs(state.bridge.mode, ExecutionMode.OBSERVE)
        self.assertIsNone(state.target)
        # Drive an entry decision through the wired ripple callback.
        state.engine.ripple_callback(_decision("ENTER_BOUNCE_LONG"))
        self.assertEqual(state.bridge._dispatched, 1)

    def test_get_execution_status_shape(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=False)
        eng._build_engines()
        status = eng.get_execution_status()
        self.assertEqual(status["mode"], "observe")
        self.assertFalse(status["arm_execution"])
        self.assertIn("BTCUSDT", status["symbols"])
        self.assertEqual(status["symbols"]["BTCUSDT"]["mode"], "observe")

    def test_set_armed_observe_is_noop(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=False)
        eng._build_engines()
        status = eng.set_armed(True)
        self.assertFalse(status["symbols"]["BTCUSDT"]["armed"])

    def test_trade_payload_feeds_wave_and_rv_buffer(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=True)
        eng._build_engines()
        state = eng._states["BTCUSDT"]
        # Tide/Wave engines are real Python objects; ensure they were built.
        self.assertIsNotNone(state.wave_engine)
        import msgpack
        payload = msgpack.packb(
            {"ts_ms": 5_000, "price": 100.0, "qty": 1.0,
             "is_buyer_maker": False}, use_bin_type=True)
        eng._handle_trade_payload("BTCUSDT", payload)
        self.assertEqual(len(state.engine.trades), 1)
        self.assertEqual(list(state.rv_price_buf), [100.0])
        self.assertEqual(state.last_trade_ts_holder[0], 5_000)

    def test_layered_push_thread_spawns_and_pushes(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=True)
        eng._build_engines()
        state = eng._states["BTCUSDT"]
        # Prime a price so RV/Wave have data.
        state.last_trade_ts_holder[0] = 5_000
        state.rv_price_buf.extend([100.0, 100.5, 101.0])
        eng._start_layered_push()
        try:
            self.assertIsNotNone(state.push_thread)
            self.assertTrue(state.push_thread.is_alive())
            # RV pushes every tick; wait for at least one iteration.
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if state.engine.ripple.realized_vol_calls:
                    break
                time.sleep(0.05)
            self.assertTrue(state.engine.ripple.realized_vol_calls)
        finally:
            if state.push_stop_event is not None:
                state.push_stop_event.set()
            if state.push_thread is not None:
                state.push_thread.join(timeout=5.0)

    def test_layered_push_skipped_when_disabled(self):
        eng = self.le.LiveEngine(symbols=["BTCUSDT"], arm_execution=False,
                                 enable_layered_strategy=False)
        eng._build_engines()
        eng._start_layered_push()
        state = eng._states["BTCUSDT"]
        self.assertIsNone(state.push_thread)


if __name__ == "__main__":
    unittest.main()
