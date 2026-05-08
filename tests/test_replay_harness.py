"""Tests for Phase 13 — Deterministic-replay session recorder + harness.

Mixes pure-Python schema/divergence tests (no C++ required) with end-to-end
HDF5 round-trip tests that exercise the real ``orderflow_engine`` C++
module, ``TickStore``, ``ReplayFeed``, and ``PaperEngine``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe  # type: ignore[import]
    HAS_C_MODULE = (
        hasattr(ofe, "OrderFlowEngine")
        and hasattr(ofe, "TickStore")
        and hasattr(ofe, "ReplayFeed")
    )
except ImportError:
    ofe = None  # type: ignore[assignment]
    HAS_C_MODULE = False

from tools.session_recorder import (  # noqa: E402
    SCHEMA_VERSION, SessionRecorder, apply_engine_config,
    apply_sizing_config, load_sidecar, serialize_engine_config,
    serialize_sizing_config,
)
from tools.replay_harness import (  # noqa: E402
    Divergence, ReplayCounts, ReplayReport, replay_session,
)
from execution.models import (  # noqa: E402
    OrderSide, OrderStatus, OrderType, SizingConfig, SizingMode,
)


# ---------------------------------------------------------------------------
# Stub objects used by pure-Python tests
# ---------------------------------------------------------------------------


class _StubEnum:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return f"FakeEnum.{self.name}"


class _StubLifecycle:
    def __init__(self, **kwargs: Any) -> None:
        self.confirmation_window_ms = 1000
        self.expand_threshold_sigma = 1.0
        self.max_hold_time_ms = 60_000
        self.cooldown_ms = 5000
        self.max_scale_ins = 2
        self.scale_in_threshold_sigma = 1.5
        self.scale_in_cvd_slope_min = 0.0
        self.trailing_stop_sigma = 1.0
        self.exhaustion_cvd_slope_thresh = 0.5
        self.exhaustion_impact_ratio = 0.5
        self.target_distance_sigma = 2.0
        self.budget_exit_threshold = 0.5
        self.maturation_momentum_fade = 0.5
        self.scale_out_count = 1
        for k, v in kwargs.items():
            setattr(self, k, v)


class _StubRippleConfig:
    def __init__(self) -> None:
        self.tick_size = 0.01
        self.wall_min_relative_size = 3.0
        self.wall_max_distance_ticks = 10
        self.feature_window_ms = 5000
        self.absorption_entry = 0.5
        self.exhaustion_entry = 0.5
        self.withdrawal_entry = 0.5
        self.breakout_entry = 0.5
        self.max_position = 1.0
        self.bounce_max_break_risk = 0.3
        self.bounce_max_withdrawal_risk = 0.3
        self.breakout_max_absorption = 0.5
        self.cancel_risk_threshold = 0.5
        self.rearm_min_evidence = 0.5
        self.time_stop_ms = 60_000
        self.enable_diagnostics = False
        self.console_diagnostics = False
        self.diagnostics_path = ""
        self.replay_mode = False
        self.paper_fills = True
        self.pipeline_min_interval_ms = 0
        self.hmm_enabled = False
        self.hmm_model_path = ""
        self.lifecycle = _StubLifecycle()


class _StubSignalParams:
    def __init__(self) -> None:
        self.imbalance_threshold = 3.0
        self.stacked_imbalance_levels = 3
        self.absorption_volume_ratio = 5.0
        self.absorption_window_ms = 1000
        self.cvd_divergence_lookback = 100
        self.exhaustion_lookback_bars = 5
        self.exhaustion_price_threshold = 0.001
        self.poc_rejection_distance = 0.001
        self.poc_rejection_window_ms = 1000
        self.signal_strength_min = 0.3


class _StubEngineConfig:
    def __init__(self) -> None:
        self.tick_size = 0.01
        self.large_trade_threshold = 10.0
        self.footprint_bar_ms = 60_000
        self.cluster_window_ms = 500
        self.signal_params = _StubSignalParams()
        self.ripple = _StubRippleConfig()


class _StubSignal:
    """Mirrors the C++ ``Signal`` shape. Provides ``type_name()`` and
    ``direction()`` so the recorder's preferred path is exercised."""

    def __init__(self, ts: int, type_name: str, price: float, strength: float,
                 description: str = "", direction: int = 1) -> None:
        self.timestamp = ts
        self.type = _StubEnum(type_name)
        self.price = price
        self.strength = strength
        self.description = description
        self._type_name = type_name
        self._direction = direction

    def type_name(self) -> str:
        return self._type_name

    def direction(self) -> int:
        return self._direction


class _StubRippleDecision:
    def __init__(
        self,
        ts: int,
        intent: str,
        ref_price: float,
        confidence: float = 0.7,
        reason: str = "",
        reference_side: str = "BID",
        invalidation_price: float = 0.0,
        wall_id: int = 0,
        triggering_state: str = "IDLE",
    ) -> None:
        self.timestamp = ts
        self.intent = _StubEnum(intent)
        self.reference_side = _StubEnum(reference_side)
        self.reference_price = ref_price
        self.invalidation_price = invalidation_price
        self.confidence = confidence
        self.wall_id = wall_id
        self.triggering_state = _StubEnum(triggering_state)
        self.reason = reason


class _RecordedEngine:
    """Captures callbacks. Tests fire events via ``on_engine_ready``."""

    def __init__(self, _config: Any) -> None:
        self.config = _config
        self.signal_cb: Optional[Callable[[Any], None]] = None
        self.ripple_cb: Optional[Callable[[Any], None]] = None
        self.start_calls: List[str] = []
        self.stopped = False
        self.processed_trades = 0
        self.processed_depth = 0

    def set_signal_callback(self, cb: Callable[[Any], None]) -> None:
        self.signal_cb = cb

    def set_ripple_callback(self, cb: Callable[[Any], None]) -> None:
        self.ripple_cb = cb

    def get_config(self) -> Any:
        return self.config

    def start(self, symbol: str) -> None:
        self.start_calls.append(symbol)

    def stop(self) -> None:
        self.stopped = True

    def process_trade(self, _t: Any) -> None:
        self.processed_trades += 1

    def process_depth(self, _u: Any) -> None:
        self.processed_depth += 1


class _RecordedTickStore:
    """Fake TickStore returning empty event lists by default."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load_trades(self, *_a: Any, **_k: Any) -> List[Any]:
        return []

    def load_depth_snapshots(self, *_a: Any, **_k: Any) -> List[Any]:
        return []

    def load_depth_updates(self, *_a: Any, **_k: Any) -> List[Any]:
        return []

    def close(self) -> None:
        pass


def _make_fake_ofe() -> Any:
    """Build a fake ``ofe_module`` for divergence tests.

    Tests drive engine callbacks via :func:`replay_session`'s
    ``on_engine_ready`` hook — the fake engine doesn't emit anything on
    its own.
    """

    class _FakeOfe:
        EngineConfig = _StubEngineConfig

        @staticmethod
        def OrderFlowEngine(config: Any) -> _RecordedEngine:  # noqa: N802
            return _RecordedEngine(config)

        TickStore = _RecordedTickStore

    return _FakeOfe


# ---------------------------------------------------------------------------
# Pure-Python: engine config round-trip
# ---------------------------------------------------------------------------


class TestEngineConfigRoundTrip(unittest.TestCase):

    def test_top_level_fields_round_trip(self) -> None:
        cfg = _StubEngineConfig()
        cfg.tick_size = 0.05
        cfg.large_trade_threshold = 25.0
        cfg.footprint_bar_ms = 30_000
        cfg.cluster_window_ms = 250
        out = serialize_engine_config(cfg)
        self.assertEqual(out["tick_size"], 0.05)
        self.assertEqual(out["large_trade_threshold"], 25.0)
        self.assertEqual(out["footprint_bar_ms"], 30_000)
        self.assertEqual(out["cluster_window_ms"], 250)

    def test_signal_params_serialized(self) -> None:
        cfg = _StubEngineConfig()
        cfg.signal_params.imbalance_threshold = 4.5
        cfg.signal_params.signal_strength_min = 0.42
        out = serialize_engine_config(cfg)
        self.assertEqual(out["signal_params"]["imbalance_threshold"], 4.5)
        self.assertEqual(out["signal_params"]["signal_strength_min"], 0.42)

    def test_ripple_lifecycle_serialized(self) -> None:
        cfg = _StubEngineConfig()
        cfg.ripple.absorption_entry = 0.31
        cfg.ripple.lifecycle.max_hold_time_ms = 12_345
        cfg.ripple.lifecycle.trailing_stop_sigma = 1.7
        out = serialize_engine_config(cfg)
        self.assertEqual(out["ripple"]["absorption_entry"], 0.31)
        self.assertEqual(out["ripple"]["lifecycle"]["max_hold_time_ms"], 12_345)
        self.assertEqual(
            out["ripple"]["lifecycle"]["trailing_stop_sigma"], 1.7)

    def test_apply_round_trip_via_stub_module(self) -> None:
        cfg = _StubEngineConfig()
        cfg.tick_size = 0.25
        cfg.signal_params.absorption_volume_ratio = 6.5
        cfg.ripple.breakout_entry = 0.71
        cfg.ripple.lifecycle.cooldown_ms = 9999
        serialized = serialize_engine_config(cfg)

        class _Mod:
            EngineConfig = _StubEngineConfig

        out = apply_engine_config(serialized, _Mod)
        self.assertEqual(out.tick_size, 0.25)
        self.assertEqual(out.signal_params.absorption_volume_ratio, 6.5)
        self.assertEqual(out.ripple.breakout_entry, 0.71)
        self.assertEqual(out.ripple.lifecycle.cooldown_ms, 9999)

    def test_unknown_serialized_fields_skipped(self) -> None:
        serialized = {
            "tick_size": 0.1,
            "phase_99_field": "ignored",
            "signal_params": {"imbalance_threshold": 2.5, "future_field": 7},
            "ripple": {
                "absorption_entry": 0.4,
                "future_ripple_field": "x",
                "lifecycle": {"cooldown_ms": 1000, "future_lc_field": True},
            },
        }

        class _Mod:
            EngineConfig = _StubEngineConfig

        out = apply_engine_config(serialized, _Mod)
        self.assertEqual(out.tick_size, 0.1)
        self.assertEqual(out.signal_params.imbalance_threshold, 2.5)
        self.assertEqual(out.ripple.absorption_entry, 0.4)
        self.assertEqual(out.ripple.lifecycle.cooldown_ms, 1000)
        self.assertFalse(hasattr(out, "phase_99_field"))


# ---------------------------------------------------------------------------
# Pure-Python: sizing config round-trip
# ---------------------------------------------------------------------------


class TestSizingConfigRoundTrip(unittest.TestCase):

    def test_fixed_qty(self) -> None:
        sc = SizingConfig(mode=SizingMode.FIXED_QTY, value=0.001, max_position=0.01)
        out = serialize_sizing_config(sc)
        self.assertEqual(out, {"mode": "FIXED_QTY", "value": 0.001, "max_position": 0.01})
        sc2 = apply_sizing_config(out)
        self.assertEqual(sc2.mode, SizingMode.FIXED_QTY)
        self.assertEqual(sc2.value, 0.001)
        self.assertEqual(sc2.max_position, 0.01)

    def test_fixed_notional(self) -> None:
        sc = SizingConfig(mode=SizingMode.FIXED_NOTIONAL, value=250.0, max_position=2500.0)
        out = serialize_sizing_config(sc)
        sc2 = apply_sizing_config(out)
        self.assertEqual(sc2.mode, SizingMode.FIXED_NOTIONAL)
        self.assertEqual(sc2.value, 250.0)

    def test_pct_balance(self) -> None:
        sc = SizingConfig(mode=SizingMode.PCT_BALANCE, value=2.5, max_position=10.0)
        out = serialize_sizing_config(sc)
        sc2 = apply_sizing_config(out)
        self.assertEqual(sc2.mode, SizingMode.PCT_BALANCE)
        self.assertEqual(sc2.value, 2.5)

    def test_unknown_mode_falls_back_to_fixed_qty(self) -> None:
        sc = apply_sizing_config({"mode": "NOT_A_MODE", "value": 1.0,
                                  "max_position": 5.0})
        self.assertEqual(sc.mode, SizingMode.FIXED_QTY)
        self.assertEqual(sc.value, 1.0)


# ---------------------------------------------------------------------------
# Pure-Python: SessionRecorder schema
# ---------------------------------------------------------------------------


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fp:
        return [json.loads(line) for line in fp if line.strip()]


class TestSessionRecorderSchema(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.jsonl")

    def test_header_then_events_then_footer(self) -> None:
        cfg = _StubEngineConfig()
        sizing = SizingConfig(mode=SizingMode.FIXED_QTY, value=0.01, max_position=0.1)
        with SessionRecorder(self.path, time_fn=lambda: 1234.5) as rec:
            rec.write_header(symbol="BTCUSDT", engine_config=cfg,
                             sizing_config=sizing,
                             meta={"source": "unit-test"})
            rec.record_signal(_StubSignal(1, "ABSORPTION_BUY", 100.5, 0.7))
            rec.record_ripple_decision(_StubRippleDecision(
                2, "ENTER_BOUNCE_LONG", 100.5, reason="rip"))

        events = _read_jsonl(self.path)
        kinds = [e["event"] for e in events]
        self.assertEqual(kinds, ["header", "signal", "ripple", "footer"])

        header = events[0]
        self.assertEqual(header["schema"], SCHEMA_VERSION)
        self.assertEqual(header["symbol"], "BTCUSDT")
        self.assertEqual(header["created_at_ms"], 1_234_500)
        self.assertEqual(header["sizing_config"]["mode"], "FIXED_QTY")
        self.assertEqual(header["meta"], {"source": "unit-test"})
        self.assertFalse(header["record_no_action"])

        sig = events[1]
        self.assertEqual(sig["type"], "ABSORPTION_BUY")
        self.assertEqual(sig["price"], 100.5)
        self.assertEqual(sig["direction"], 1)

        rip = events[2]
        self.assertEqual(rip["intent"], "ENTER_BOUNCE_LONG")
        self.assertEqual(rip["reference_price"], 100.5)
        self.assertEqual(rip["triggering_state"], "IDLE")

        footer = events[-1]
        self.assertEqual(footer["n_signals"], 1)
        self.assertEqual(footer["n_ripples"], 1)
        self.assertEqual(footer["n_orders"], 0)

    def test_no_action_filtered_by_default(self) -> None:
        with SessionRecorder(self.path) as rec:
            rec.write_header(symbol="X", engine_config=_StubEngineConfig())
            rec.record_ripple_decision(_StubRippleDecision(1, "NO_ACTION", 0.0))
            rec.record_ripple_decision(_StubRippleDecision(2, "EXIT_BOUNCE", 100.0))
        events = _read_jsonl(self.path)
        ripple_events = [e for e in events if e["event"] == "ripple"]
        self.assertEqual(len(ripple_events), 1)
        self.assertEqual(ripple_events[0]["intent"], "EXIT_BOUNCE")

    def test_record_no_action_when_enabled(self) -> None:
        with SessionRecorder(self.path, record_no_action=True) as rec:
            rec.write_header(symbol="X", engine_config=_StubEngineConfig())
            rec.record_ripple_decision(_StubRippleDecision(1, "NO_ACTION", 0.0))
        events = _read_jsonl(self.path)
        ripple_events = [e for e in events if e["event"] == "ripple"]
        self.assertEqual(len(ripple_events), 1)
        self.assertEqual(ripple_events[0]["intent"], "NO_ACTION")
        self.assertTrue(events[0]["record_no_action"])

    def test_paper_order_callback_chains_to_next(self) -> None:
        captured: List[Any] = []

        class _Order:
            def __init__(self) -> None:
                self.timestamp = 1.5
                self.symbol = "BTCUSDT"
                self.side = OrderSide.BUY
                self.quantity = 0.01
                self.order_type = OrderType.MARKET
                self.status = OrderStatus.FILLED
                self.fill_price = 100.0
                self.fill_quantity = 0.01
                self.signal_type = "ENTRY"
                self.ripple_reason = "test"

        with SessionRecorder(self.path) as rec:
            rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
            cb = rec.wrap_paper_callback(next_callback=captured.append)
            cb(_Order())
            cb(_Order())

        self.assertEqual(len(captured), 2)
        events = _read_jsonl(self.path)
        order_events = [e for e in events if e["event"] == "order"]
        self.assertEqual(len(order_events), 2)
        self.assertEqual(order_events[0]["symbol"], "BTCUSDT")
        self.assertEqual(order_events[0]["side"], "BUY")
        self.assertEqual(order_events[0]["status"], "FILLED")
        self.assertEqual(order_events[0]["ts_ms"], 1500)

    def test_paper_callback_swallows_next_exceptions(self) -> None:
        def boom(_o: Any) -> None:
            raise RuntimeError("kaboom")

        class _Order:
            timestamp = 0.0
            symbol = "BTCUSDT"
            side = OrderSide.SELL
            quantity = 0.0
            order_type = OrderType.MARKET
            status = OrderStatus.FILLED
            fill_price = 0.0
            fill_quantity = 0.0
            signal_type = ""
            ripple_reason = ""

        with SessionRecorder(self.path) as rec:
            rec.write_header(symbol="X", engine_config=_StubEngineConfig())
            cb = rec.wrap_paper_callback(next_callback=boom)
            cb(_Order())  # must not raise

        events = _read_jsonl(self.path)
        order_events = [e for e in events if e["event"] == "order"]
        self.assertEqual(len(order_events), 1)

    def test_close_is_idempotent(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="X", engine_config=_StubEngineConfig())
        rec.close()
        rec.close()  # no exception
        events = _read_jsonl(self.path)
        self.assertEqual(events[-1]["event"], "footer")

    def test_events_dropped_before_header(self) -> None:
        rec = SessionRecorder(self.path)
        try:
            rec.record_signal(_StubSignal(1, "X", 100, 0.5))
            rec.write_header(symbol="X", engine_config=_StubEngineConfig())
            rec.record_signal(_StubSignal(2, "Y", 101, 0.5))
        finally:
            rec.close()
        events = _read_jsonl(self.path)
        signal_events = [e for e in events if e["event"] == "signal"]
        self.assertEqual(len(signal_events), 1)
        self.assertEqual(signal_events[0]["type"], "Y")

    def test_double_header_raises(self) -> None:
        with SessionRecorder(self.path) as rec:
            rec.write_header(symbol="X", engine_config=_StubEngineConfig())
            with self.assertRaises(RuntimeError):
                rec.write_header(symbol="X", engine_config=_StubEngineConfig())

    def test_writes_after_close_raise(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="X", engine_config=_StubEngineConfig())
        rec.close()
        with self.assertRaises(RuntimeError):
            rec.record_signal(_StubSignal(1, "X", 100, 0.5))


# ---------------------------------------------------------------------------
# Pure-Python: load_sidecar
# ---------------------------------------------------------------------------


class TestLoadSidecar(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "trace.jsonl")

    def _write(self, lines: List[Dict[str, Any]]) -> None:
        with open(self.path, "w", encoding="utf-8") as fp:
            for line in lines:
                fp.write(json.dumps(line) + "\n")

    def test_loads_full_trace(self) -> None:
        self._write([
            {"event": "header", "schema": 1, "symbol": "BTCUSDT",
             "created_at_ms": 1, "engine_config": {"tick_size": 0.01},
             "sizing_config": {"mode": "FIXED_QTY", "value": 0.001,
                               "max_position": 0.01}},
            {"event": "signal", "ts": 100, "type": "ABSORPTION_BUY"},
            {"event": "ripple", "ts": 200, "intent": "ENTER_BOUNCE_LONG"},
            {"event": "order", "ts_ms": 300, "side": "BUY"},
            {"event": "footer", "stopped_at_ms": 999, "n_signals": 1,
             "n_ripples": 1, "n_orders": 1},
        ])
        trace = load_sidecar(self.path)
        self.assertEqual(trace.symbol, "BTCUSDT")
        self.assertEqual(trace.created_at_ms, 1)
        self.assertEqual(trace.stopped_at_ms, 999)
        self.assertEqual(len(trace.signals), 1)
        self.assertEqual(len(trace.ripples), 1)
        self.assertEqual(len(trace.orders), 1)

    def test_missing_header_raises(self) -> None:
        self._write([{"event": "signal", "ts": 1}])
        with self.assertRaises(ValueError):
            load_sidecar(self.path)

    def test_unknown_schema_raises(self) -> None:
        self._write([{"event": "header", "schema": 999, "symbol": "X",
                      "engine_config": {}}])
        with self.assertRaises(ValueError):
            load_sidecar(self.path)

    def test_malformed_line_raises(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write('{"event":"header","schema":1,"engine_config":{}}\n')
            fp.write("not json\n")
        with self.assertRaises(ValueError):
            load_sidecar(self.path)

    def test_unknown_event_kind_silently_ignored(self) -> None:
        self._write([
            {"event": "header", "schema": 1, "symbol": "X",
             "engine_config": {}},
            {"event": "future_kind", "data": 1},
            {"event": "footer", "stopped_at_ms": 0, "n_signals": 0,
             "n_ripples": 0, "n_orders": 0},
        ])
        trace = load_sidecar(self.path)
        self.assertEqual(len(trace.signals), 0)

    def test_record_no_action_flag_round_trips(self) -> None:
        self._write([
            {"event": "header", "schema": 1, "symbol": "X",
             "engine_config": {}, "record_no_action": True},
        ])
        trace = load_sidecar(self.path)
        self.assertTrue(trace.record_no_action)


# ---------------------------------------------------------------------------
# Replay-harness divergence detection (fake ofe module — no C++ required)
# ---------------------------------------------------------------------------


def _capture_baseline_sidecar(
    path: str, symbol: str, signals: List[_StubSignal],
    ripples: List[_StubRippleDecision],
) -> None:
    """Drive a recorder with the supplied events to produce a sidecar."""
    cfg = _StubEngineConfig()
    with SessionRecorder(path) as rec:
        rec.write_header(symbol=symbol, engine_config=cfg)
        for s in signals:
            rec.record_signal(s)
        for r in ripples:
            rec.record_ripple_decision(r)


class TestReplayHarnessDivergence(unittest.TestCase):
    """Replay through a fake ofe module that re-emits identical events."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sidecar = os.path.join(self.tmp.name, "sidecar.jsonl")
        self.tick_path = os.path.join(self.tmp.name, "ticks.h5")

        self.signals = [
            _StubSignal(100, "ABSORPTION_BUY", 100.5, 0.7, "abs"),
            _StubSignal(200, "EXHAUSTION_SELL", 101.5, 0.6, "exh", direction=-1),
        ]
        self.ripples = [
            _StubRippleDecision(150, "ENTER_BOUNCE_LONG", 100.4,
                                reason="enter"),
            _StubRippleDecision(220, "EXIT_BOUNCE", 101.6, reason="exit"),
        ]
        _capture_baseline_sidecar(
            self.sidecar, "BTCUSDT", self.signals, self.ripples)

    def _replay_events(self, signals: List[Any],
                       ripples: List[Any]) -> ReplayReport:
        all_evts = (
            [("signal", s, s.timestamp) for s in signals]
            + [("ripple", r, r.timestamp) for r in ripples]
        )
        all_evts.sort(key=lambda x: x[2])

        def _fire(engine: _RecordedEngine) -> None:
            for kind, evt, _ts in all_evts:
                if kind == "signal" and engine.signal_cb is not None:
                    engine.signal_cb(evt)
                elif kind == "ripple" and engine.ripple_cb is not None:
                    engine.ripple_cb(evt)

        fake = _make_fake_ofe()
        return replay_session(
            sidecar_path=self.sidecar,
            tick_store_path=self.tick_path,
            ofe_module=fake,
            on_engine_ready=_fire,
        )

    def test_identical_replay_passes(self) -> None:
        report = self._replay_events(self.signals, self.ripples)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.expected, ReplayCounts(2, 2, 0))
        self.assertEqual(report.actual, ReplayCounts(2, 2, 0))
        self.assertEqual(report.divergences, [])

    def test_signal_price_mismatch_detected(self) -> None:
        bad_signals = [
            _StubSignal(100, "ABSORPTION_BUY", 100.5, 0.7, "abs"),
            _StubSignal(200, "EXHAUSTION_SELL", 999.99, 0.6, "exh",
                        direction=-1),  # wrong price
        ]
        report = self._replay_events(bad_signals, self.ripples)
        self.assertFalse(report.ok)
        prices = [d for d in report.divergences
                  if d.section == "signals" and d.field == "price"]
        self.assertEqual(len(prices), 1)
        self.assertEqual(prices[0].expected, 101.5)
        self.assertEqual(prices[0].actual, 999.99)

    def test_extra_signal_yields_length_divergence(self) -> None:
        extra = list(self.signals) + [
            _StubSignal(300, "STACKED_IMBALANCE_BUY", 102.0, 0.5, "x"),
        ]
        report = self._replay_events(extra, self.ripples)
        self.assertFalse(report.ok)
        length_divs = [d for d in report.divergences
                       if d.section == "signals" and d.index == -1]
        self.assertEqual(len(length_divs), 1)
        self.assertEqual(length_divs[0].expected, 2)
        self.assertEqual(length_divs[0].actual, 3)

    def test_ripple_intent_mismatch_detected(self) -> None:
        bad_ripples = [
            _StubRippleDecision(150, "ENTER_BREAKOUT_LONG", 100.4,
                                reason="enter"),  # different intent
            _StubRippleDecision(220, "EXIT_BOUNCE", 101.6, reason="exit"),
        ]
        report = self._replay_events(self.signals, bad_ripples)
        self.assertFalse(report.ok)
        intents = [d for d in report.divergences
                   if d.section == "ripples" and d.field == "intent"]
        self.assertEqual(len(intents), 1)

    def test_missing_ripple_decision_detected(self) -> None:
        report = self._replay_events(self.signals, self.ripples[:1])
        self.assertFalse(report.ok)
        length = [d for d in report.divergences
                  if d.section == "ripples" and d.index == -1]
        self.assertEqual(len(length), 1)

    def test_no_action_filtered_in_replay_too(self) -> None:
        ripples_with_noop = list(self.ripples) + [
            _StubRippleDecision(225, "NO_ACTION", 0.0),
        ]
        report = self._replay_events(self.signals, ripples_with_noop)
        self.assertTrue(report.ok, msg=report.summary())

    def test_paper_engine_factory_invoked(self) -> None:
        called: List[Any] = []

        class _FakePaper:
            def __init__(self) -> None:
                self.intents: List[Any] = []
                self._order_callback: Optional[Callable[[Any], None]] = None

            def on_intent(self, intent: Any) -> None:
                self.intents.append(intent)

        def _factory(symbol: str, sizing: SizingConfig) -> _FakePaper:
            called.append((symbol, sizing))
            return _FakePaper()

        # add sizing_config to the sidecar so the factory path is taken
        with open(self.sidecar, "w", encoding="utf-8") as fp:
            fp.write(json.dumps({
                "event": "header", "schema": 1, "symbol": "BTCUSDT",
                "created_at_ms": 1, "engine_config": {"tick_size": 0.01},
                "sizing_config": {"mode": "FIXED_QTY", "value": 0.001,
                                  "max_position": 0.01},
            }) + "\n")
            fp.write(json.dumps({
                "event": "footer", "stopped_at_ms": 2,
                "n_signals": 0, "n_ripples": 0, "n_orders": 0,
            }) + "\n")

        decision = _StubRippleDecision(1, "ENTER_BOUNCE_LONG", 100.0)

        def _fire(engine: _RecordedEngine) -> None:
            if engine.ripple_cb is not None:
                engine.ripple_cb(decision)

        fake = _make_fake_ofe()
        replay_session(
            sidecar_path=self.sidecar,
            tick_store_path=self.tick_path,
            ofe_module=fake,
            paper_engine_factory=_factory,
            on_engine_ready=_fire,
        )
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][0], "BTCUSDT")
        self.assertEqual(called[0][1].mode, SizingMode.FIXED_QTY)


# ---------------------------------------------------------------------------
# End-to-end (requires the C++ orderflow_engine module)
# ---------------------------------------------------------------------------


def _build_e2e_engine_config() -> Any:
    """Tiny config that produces deterministic engine output."""
    cfg = ofe.EngineConfig()
    cfg.tick_size = 0.01
    rcfg = cfg.ripple
    rcfg.tick_size = 0.01
    rcfg.pipeline_min_interval_ms = 0
    rcfg.paper_fills = True
    rcfg.enable_diagnostics = False
    cfg.ripple = rcfg
    return cfg


def _make_e2e_depth(ts: int, mid: float, levels: int = 10,
                    base_qty: float = 50.0) -> Any:
    d = ofe.DepthUpdate()
    d.timestamp = ts
    d.is_snapshot = True
    bids = []
    asks = []
    for i in range(levels):
        bl = ofe.DepthLevel()
        bl.price = mid - (i + 1) * 0.01
        bl.quantity = base_qty + (i % 3) * 10.0
        bids.append(bl)
        al = ofe.DepthLevel()
        al.price = mid + (i + 1) * 0.01
        al.quantity = base_qty + (i % 3) * 10.0
        asks.append(al)
    # IMPORTANT: pybind11's default vector binding for ``DepthUpdate.bids``
    # / ``.asks`` returns a copy on read, so per-element ``.append`` is a
    # no-op. Whole-list assignment via the property setter persists.
    d.bids = bids
    d.asks = asks
    return d


def _make_e2e_trade(ts: int, price: float, qty: float, buyer_maker: bool) -> Any:
    t = ofe.Trade()
    t.timestamp = ts
    t.price = price
    t.quantity = qty
    t.is_buyer_maker = buyer_maker
    return t


def _build_e2e_event_sequence(n: int = 200, base_price: float = 100.0,
                              start_ts: int = 1_000_000) -> List[Any]:
    import math
    out: List[Any] = []
    ts = start_ts
    for i in range(n):
        drift = 0.02 * math.sin(i * 0.1)
        mid = base_price + drift
        ts += 300
        out.append(("depth", _make_e2e_depth(ts, mid)))
        ts += 100
        side = (i % 3) != 0
        tp = mid + (0.01 if side else -0.01)
        out.append(("trade", _make_e2e_trade(ts, tp, 1.0 + (i % 7), side)))
    return out


@unittest.skipUnless(HAS_C_MODULE, "orderflow_engine C++ module not available")
class TestReplayHarnessEndToEnd(unittest.TestCase):
    """Capture from a real engine, persist to HDF5, and replay round-trip."""

    SYMBOL = "REPLAYSYM"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tick_path = os.path.join(self.tmp.name, "ticks.h5")
        self.sidecar = os.path.join(self.tmp.name, "session.jsonl")

    def _capture_session(
        self,
        *,
        with_paper: bool = False,
    ) -> None:
        cfg = _build_e2e_engine_config()
        engine = ofe.OrderFlowEngine(cfg)
        store = ofe.TickStore(self.tick_path)
        engine.set_tick_store(store, self.SYMBOL)

        if with_paper:
            sizing = SizingConfig(
                mode=SizingMode.FIXED_QTY, value=0.001, max_position=0.01)
        else:
            sizing = None

        recorder = SessionRecorder(self.sidecar)
        try:
            recorder.write_header(
                symbol=self.SYMBOL, engine_config=engine.get_config(),
                sizing_config=sizing)
            recorder.attach_to_engine(engine)

            paper = None
            if with_paper:
                from execution.paper_engine import PaperEngine
                paper = PaperEngine(
                    self.SYMBOL, sizing,
                    order_callback=recorder.wrap_paper_callback())

                from execution.models import (
                    _parse_intent_name, ripple_decision_to_intent,
                )
                # Chain ripple → intent → paper alongside the recorder
                # callback. The recorder is already attached, so we wrap
                # set_ripple_callback once more — recorder's callback was
                # set last, so we extend it by replacing with a chained one.
                rec_cb = recorder.record_ripple_decision

                def _ripple_chain(decision: Any) -> None:
                    rec_cb(decision)
                    intent_name = _parse_intent_name(decision)
                    if intent_name == "NO_ACTION":
                        return
                    intent = ripple_decision_to_intent(
                        decision, intent_name=intent_name)
                    if intent is not None:
                        paper.on_intent(intent)

                engine.set_ripple_callback(_ripple_chain)

            engine.start(self.SYMBOL)
            try:
                events = _build_e2e_event_sequence(150)
                for kind, evt in events:
                    if kind == "depth":
                        engine.process_depth(evt)
                    else:
                        engine.process_trade(evt)
            finally:
                engine.stop()
        finally:
            recorder.close()
            try:
                store.flush()
                store.close()
            except Exception:
                pass

    def test_engine_round_trip_passes(self) -> None:
        self._capture_session(with_paper=False)
        report = replay_session(
            sidecar_path=self.sidecar,
            tick_store_path=self.tick_path,
            ofe_module=ofe,
        )
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.expected.signals, report.actual.signals)
        self.assertEqual(report.expected.ripples, report.actual.ripples)
        # No paper engine on capture → no order events.
        self.assertEqual(report.expected.orders, 0)
        self.assertEqual(report.actual.orders, 0)

    def test_engine_round_trip_with_paper_engine(self) -> None:
        self._capture_session(with_paper=True)
        report = replay_session(
            sidecar_path=self.sidecar,
            tick_store_path=self.tick_path,
            ofe_module=ofe,
        )
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.expected.signals, report.actual.signals)
        self.assertEqual(report.expected.ripples, report.actual.ripples)
        self.assertEqual(report.expected.orders, report.actual.orders)

    def test_corrupted_sidecar_detected(self) -> None:
        self._capture_session(with_paper=False)
        # Corrupt the first signal's price in the sidecar.
        with open(self.sidecar, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
        for i, raw in enumerate(lines):
            evt = json.loads(raw)
            if evt.get("event") == "signal":
                evt["price"] = 0.0
                lines[i] = json.dumps(evt) + "\n"
                break
        else:
            self.skipTest("no signal events emitted in this run")
        with open(self.sidecar, "w", encoding="utf-8") as fp:
            fp.writelines(lines)

        report = replay_session(
            sidecar_path=self.sidecar,
            tick_store_path=self.tick_path,
            ofe_module=ofe,
        )
        self.assertFalse(report.ok)
        sig_div = [d for d in report.divergences
                   if d.section == "signals" and d.field == "price"]
        self.assertGreaterEqual(len(sig_div), 1)


if __name__ == "__main__":
    unittest.main()
