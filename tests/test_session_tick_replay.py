"""Phase 13B — `LiveTradingSession` orchestration replay tests.

Covers the new `session_tick` event added to `tools.session_recorder`
and the `verify_session_ticks` helper in `tools.replay_harness`. The
last block also exercises the `LiveTradingSession.attach_recorder`
hook end-to-end against a stub MainWindow.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from tools.session_recorder import (  # noqa: E402
    SessionRecorder, load_sidecar,
)
from tools.replay_harness import verify_session_ticks  # noqa: E402


# ── Minimal serialisable engine config stub ──────────────────────────


class _StubSignalParams:
    imbalance_threshold = 3.0
    stacked_imbalance_levels = 3
    absorption_volume_ratio = 5.0
    absorption_window_ms = 200
    cvd_divergence_lookback = 100
    exhaustion_lookback_bars = 5
    exhaustion_price_threshold = 0.001
    poc_rejection_distance = 0.001
    poc_rejection_window_ms = 200
    signal_strength_min = 0.3


class _StubLifecycle:
    confirmation_window_ms = 5000
    expand_threshold_sigma = 1.0
    max_hold_time_ms = 300_000
    cooldown_ms = 1000
    max_scale_ins = 2
    scale_in_threshold_sigma = 1.5
    scale_in_cvd_slope_min = 0.1
    trailing_stop_sigma = 1.5
    exhaustion_cvd_slope_thresh = 0.0
    exhaustion_impact_ratio = 0.5
    target_distance_sigma = 2.5
    budget_exit_threshold = 0.8
    maturation_momentum_fade = 0.3
    scale_out_count = 2


class _StubRipple:
    tick_size = 0.01
    wall_min_relative_size = 2.0
    wall_max_distance_ticks = 10
    feature_window_ms = 5000
    absorption_entry = 0.4
    exhaustion_entry = 0.4
    withdrawal_entry = 0.4
    breakout_entry = 0.5
    max_position = 1.0
    bounce_max_break_risk = 0.4
    bounce_max_withdrawal_risk = 0.4
    breakout_max_absorption = 0.5
    cancel_risk_threshold = 0.6
    rearm_min_evidence = 0.3
    time_stop_ms = 60_000
    enable_diagnostics = False
    console_diagnostics = False
    diagnostics_path = ""
    replay_mode = False
    paper_fills = False
    pipeline_min_interval_ms = 0
    hmm_enabled = False
    hmm_model_path = ""
    lifecycle = _StubLifecycle()


class _StubEngineConfig:
    tick_size = 0.01
    large_trade_threshold = 0.0
    footprint_bar_ms = 60_000
    cluster_window_ms = 500
    signal_params = _StubSignalParams()
    ripple = _StubRipple()


# ── Schema tests ────────────────────────────────────────────────────


class TestRecordSessionTickSchema(unittest.TestCase):
    """Direct field-by-field assertions on the JSONL emitted by
    `SessionRecorder.record_session_tick`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.jsonl")

    def _read_session_ticks(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as fp:
            for raw in fp:
                if not raw.strip():
                    continue
                evt = json.loads(raw)
                if evt.get("event") == "session_tick":
                    out.append(evt)
        return out

    def test_session_tick_emits_with_all_fields(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
        rec.record_session_tick(
            5,
            drained=12,
            best_bid=42_000.50,
            best_ask=42_001.00,
            bid_count=20,
            ask_count=20,
            book_empty=False,
            book_crossed=False,
            book_empty_ticks=0,
            book_resync_pending=False,
            book_resync_count=0,
            trade_buf_remaining=42,
        )
        rec.close()
        ticks = self._read_session_ticks()
        self.assertEqual(len(ticks), 1)
        evt = ticks[0]
        self.assertEqual(evt["tick_n"], 5)
        self.assertEqual(evt["drained"], 12)
        self.assertEqual(evt["best_bid"], 42_000.50)
        self.assertEqual(evt["best_ask"], 42_001.00)
        self.assertEqual(evt["bid_count"], 20)
        self.assertEqual(evt["ask_count"], 20)
        self.assertFalse(evt["book_empty"])
        self.assertFalse(evt["book_crossed"])
        self.assertEqual(evt["book_empty_ticks"], 0)
        self.assertFalse(evt["book_resync_pending"])
        self.assertEqual(evt["book_resync_count"], 0)
        self.assertEqual(evt["trade_buf_remaining"], 42)

    def test_session_tick_defaults(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
        rec.record_session_tick(1)
        rec.close()
        ticks = self._read_session_ticks()
        self.assertEqual(len(ticks), 1)
        evt = ticks[0]
        self.assertEqual(evt["tick_n"], 1)
        self.assertEqual(evt["drained"], 0)
        self.assertEqual(evt["best_bid"], 0.0)
        self.assertEqual(evt["best_ask"], 0.0)

    def test_session_tick_skipped_before_header(self) -> None:
        rec = SessionRecorder(self.path)
        rec.record_session_tick(1, drained=99)
        rec.write_header(symbol="X", engine_config=_StubEngineConfig())
        rec.close()
        self.assertEqual(self._read_session_ticks(), [])

    def test_counter_increments_per_tick(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
        for n in range(7):
            rec.record_session_tick(n)
        self.assertEqual(rec.counters.session_ticks, 7)
        rec.close()

    def test_footer_includes_session_tick_count(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
        for n in range(3):
            rec.record_session_tick(n, drained=n)
        rec.close()
        with open(self.path, "r", encoding="utf-8") as fp:
            lines = [json.loads(x) for x in fp if x.strip()]
        footer = lines[-1]
        self.assertEqual(footer["event"], "footer")
        self.assertEqual(footer["n_session_ticks"], 3)


class TestLoadSidecarSessionTicks(unittest.TestCase):
    """`load_sidecar` must populate `SidecarTrace.session_ticks`."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "session.jsonl")

    def test_round_trip(self) -> None:
        rec = SessionRecorder(self.path)
        rec.write_header(symbol="BTCUSDT", engine_config=_StubEngineConfig())
        for n in range(4):
            rec.record_session_tick(n, drained=n * 2,
                                    best_bid=42000.0 + n,
                                    best_ask=42001.0 + n,
                                    book_empty=(n == 2))
        rec.close()
        trace = load_sidecar(self.path)
        self.assertEqual(len(trace.session_ticks), 4)
        self.assertEqual(trace.session_ticks[0]["tick_n"], 0)
        self.assertEqual(trace.session_ticks[3]["drained"], 6)
        self.assertTrue(trace.session_ticks[2]["book_empty"])
        self.assertFalse(trace.session_ticks[1]["book_empty"])


# ── verify_session_ticks divergence detection ───────────────────────


def _make_tick(n: int, **overrides: Any) -> Dict[str, Any]:
    base = {
        "event": "session_tick",
        "tick_n": n,
        "drained": 0,
        "best_bid": 100.0,
        "best_ask": 100.5,
        "bid_count": 10,
        "ask_count": 10,
        "book_empty": False,
        "book_crossed": False,
        "book_empty_ticks": 0,
        "book_resync_pending": False,
        "book_resync_count": 0,
        "trade_buf_remaining": 0,
    }
    base.update(overrides)
    return base


class TestVerifySessionTicks(unittest.TestCase):

    def test_identical_traces_pass(self) -> None:
        ticks = [_make_tick(n, drained=n) for n in range(5)]
        rep = verify_session_ticks(ticks, list(ticks))
        self.assertTrue(rep.ok, msg=rep.summary())
        self.assertEqual(rep.expected_count, 5)
        self.assertEqual(rep.actual_count, 5)
        self.assertEqual(rep.divergences, [])

    def test_length_mismatch_detected(self) -> None:
        expected = [_make_tick(n) for n in range(5)]
        actual = [_make_tick(n) for n in range(3)]
        rep = verify_session_ticks(expected, actual)
        self.assertFalse(rep.ok)
        self.assertEqual(rep.expected_count, 5)
        self.assertEqual(rep.actual_count, 3)
        # First divergence is the length mismatch.
        self.assertEqual(rep.divergences[0].section, "session_ticks")
        self.assertEqual(rep.divergences[0].index, -1)
        self.assertEqual(rep.divergences[0].field, "<length>")
        self.assertEqual(rep.divergences[0].expected, 5)
        self.assertEqual(rep.divergences[0].actual, 3)

    def test_field_mismatch_detected(self) -> None:
        expected = [_make_tick(n, drained=n) for n in range(3)]
        actual = [_make_tick(n, drained=n) for n in range(3)]
        actual[1]["drained"] = 999
        rep = verify_session_ticks(expected, actual)
        self.assertFalse(rep.ok)
        # Find the drained mismatch (other fields are equal).
        drained_divs = [d for d in rep.divergences if d.field == "drained"]
        self.assertEqual(len(drained_divs), 1)
        self.assertEqual(drained_divs[0].index, 1)
        self.assertEqual(drained_divs[0].expected, 1)
        self.assertEqual(drained_divs[0].actual, 999)

    def test_book_state_flip_detected(self) -> None:
        expected = [_make_tick(n, book_empty=False) for n in range(3)]
        actual = [_make_tick(n, book_empty=False) for n in range(3)]
        actual[2]["book_empty"] = True
        rep = verify_session_ticks(expected, actual)
        self.assertFalse(rep.ok)
        flip = [d for d in rep.divergences if d.field == "book_empty"]
        self.assertEqual(len(flip), 1)
        self.assertEqual(flip[0].index, 2)

    def test_max_divergences_caps_output(self) -> None:
        expected = [_make_tick(n) for n in range(20)]
        actual = [_make_tick(n, drained=999) for n in range(20)]
        rep = verify_session_ticks(expected, actual, max_divergences=3)
        self.assertFalse(rep.ok)
        self.assertLessEqual(len(rep.divergences), 3)

    def test_float_tolerance_accepts_jitter(self) -> None:
        expected = [_make_tick(n, best_bid=100.0) for n in range(3)]
        actual = [_make_tick(n, best_bid=100.000_000_001) for n in range(3)]
        # Strict: divergence.
        rep_strict = verify_session_ticks(expected, actual, float_tolerance=0.0)
        self.assertFalse(rep_strict.ok)
        # Relaxed: pass.
        rep_loose = verify_session_ticks(expected, actual, float_tolerance=1e-6)
        self.assertTrue(rep_loose.ok)


# ── LiveTradingSession integration (stub MainWindow) ────────────────


class _RecordingRecorder:
    """Lightweight stand-in for SessionRecorder that captures
    `record_session_tick` calls for inspection."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def record_session_tick(self, tick_n: int, **kwargs: Any) -> None:
        self.calls.append({"tick_n": tick_n, **kwargs})


class TestLiveTradingSessionRecorderHook(unittest.TestCase):
    """Phase 13B integration: attach a recorder, call `on_timer_tick`,
    confirm a `session_tick` event is emitted with the expected scalars.

    Uses a heavily-stubbed MainWindow to avoid Qt and the real C++
    engine — the goal is to pin the recorder hook contract, not to
    re-test the existing on_timer_tick behaviour (covered by the
    Phase 11/11C dashboard suites).
    """

    def _make_session(self, *, book_empty: bool = False):
        # Import lazily so this file can still be imported even if Qt
        # isn't installed (the stub MainWindow makes Qt unnecessary).
        from ui.live_trading_session import LiveTradingSession

        # Build a stub MainWindow with the minimum attributes
        # `on_timer_tick` reaches for.
        mw = MagicMock()
        mw._is_replay_mode = False
        mw._heatmap = MagicMock()
        mw._heatmap.chart_now = 0
        mw._heatmap.get_viewport_params = MagicMock(
            return_value=(800, 600, 50, 30, 10))
        mw._heatmap._visible_window_ms = 60_000
        mw._heatmap._chart_right_inset = 10
        mw._heatmap._last_visible_bubble_count = 0
        mw._heatmap._slices = []
        mw._heatmap._trades = []
        mw._heatmap._last_paint_ms = 0
        mw._heatmap._last_depth_ts = 0
        mw._heatmap._last_trade_ts = 0
        mw._heatmap.get_bubble_diagnostics = MagicMock(return_value={})
        mw._cvd = MagicMock()
        mw._candle_view = MagicMock()
        mw._candle_window = MagicMock()
        mw._candle_window.isVisible = MagicMock(return_value=False)
        mw._strategy_window = MagicMock()
        mw._strategy_window.isVisible = MagicMock(return_value=False)
        mw._strategy_dashboard = MagicMock()
        mw._strategy_dashboard.account_panel = MagicMock()
        mw._exec_manager = None
        mw._orderflow_vm = MagicMock()
        mw._orderflow_vm.compute_frame = MagicMock(return_value=None)
        mw._orderflow_vm.chart_now = 0
        mw._orderflow_vm.visible_window_ms = 60_000
        mw._orderflow_vm.bucket_duration_ms = 100
        mw._market_state = MagicMock()
        mw._market_state.snapshot_history = []
        mw._strategy_store = None
        mw._strategy_ui_state = MagicMock()
        mw._strategy_ui_state.value = "disarmed"
        mw._symbol_input = MagicMock()
        mw._symbol_input.text = MagicMock(return_value="BTCUSDT")
        mw._signal_count = 0
        mw._last_strategy_snap = None
        mw._update_strategy_state_from_snapshot = MagicMock()
        mw._update_heatmap_overlay_from_snapshot = MagicMock()
        mw._trades_label = MagicMock()
        mw._signals_label = MagicMock()
        mw._exec_label = MagicMock()

        sess = LiveTradingSession(mw)

        # Stub engine that exposes the order-book + trade-flow accessors
        # `on_timer_tick` calls. The book reports either populated or
        # empty depending on the test parameter.
        engine = MagicMock()
        snap = MagicMock()
        snap.timestamp = 1_000_000
        if book_empty:
            snap.get_bids = MagicMock(return_value=[])
            snap.get_asks = MagicMock(return_value=[])
            snap.best_bid = 0.0
            snap.best_ask = 0.0
        else:
            snap.get_bids = MagicMock(return_value=[(100.0, 1.0)])
            snap.get_asks = MagicMock(return_value=[(100.5, 1.0)])
            snap.best_bid = 100.0
            snap.best_ask = 100.5
        ob = MagicMock()
        ob.get_snapshot = MagicMock(return_value=snap)
        engine.get_order_book = MagicMock(return_value=ob)
        engine.get_trade_flow = MagicMock()
        engine.get_signal_engine = MagicMock()
        sess._engine = engine
        return sess

    def test_no_recorder_attached_does_not_raise(self) -> None:
        sess = self._make_session()
        sess.on_timer_tick()  # must not raise

    def test_recorder_called_each_tick(self) -> None:
        sess = self._make_session()
        rec = _RecordingRecorder()
        sess.attach_recorder(rec)
        sess.on_timer_tick()
        sess.on_timer_tick()
        sess.on_timer_tick()
        self.assertEqual(len(rec.calls), 3)

    def test_recorded_tick_carries_book_state(self) -> None:
        sess = self._make_session(book_empty=False)
        rec = _RecordingRecorder()
        sess.attach_recorder(rec)
        sess.on_timer_tick()
        call = rec.calls[0]
        self.assertEqual(call["best_bid"], 100.0)
        self.assertEqual(call["best_ask"], 100.5)
        self.assertFalse(call["book_empty"])

    def test_book_empty_flag_propagates(self) -> None:
        sess = self._make_session(book_empty=True)
        rec = _RecordingRecorder()
        sess.attach_recorder(rec)
        sess.on_timer_tick()
        sess.on_timer_tick()
        # First tick: book is empty → book_empty_ticks = 1 → flag True.
        self.assertTrue(rec.calls[0]["book_empty"])
        self.assertEqual(rec.calls[0]["book_empty_ticks"], 1)
        self.assertEqual(rec.calls[1]["book_empty_ticks"], 2)

    def test_recorder_exception_does_not_break_tick(self) -> None:
        sess = self._make_session()

        class _RaisingRecorder:
            def record_session_tick(self, tick_n, **kw):
                raise RuntimeError("boom")
        sess.attach_recorder(_RaisingRecorder())
        # Must not raise — recorder hook is wrapped in try/except.
        sess.on_timer_tick()

    def test_attach_then_detach(self) -> None:
        sess = self._make_session()
        rec = _RecordingRecorder()
        sess.attach_recorder(rec)
        sess.on_timer_tick()
        self.assertEqual(len(rec.calls), 1)
        sess.attach_recorder(None)
        sess.on_timer_tick()
        sess.on_timer_tick()
        self.assertEqual(len(rec.calls), 1, "no further calls after detach")


if __name__ == "__main__":
    unittest.main()
