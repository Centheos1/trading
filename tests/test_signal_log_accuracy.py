"""Tests for Phase 8C — Signal Log Accuracy.

Validates:
1. LEGACY_RAW signals suppressed in _on_signal_received when strategy armed.
2. LEGACY_RAW signals appear when strategy disarmed.
3. Tide BIAS_CHANGE entry emitted on bias transition.
4. Wave REGIME_CHANGE entry emitted on regime transition.
5. No spurious event when bias/regime unchanged across ticks.
6. Exit TRADE_LIFECYCLE entry has non-zero realized_pnl after fill.
7. Trades-Only filter hides non-trade entries, shows TRADE_LIFECYCLE + EXECUTION.
8. Debug filter shows LEGACY_RAW + DIAGNOSTIC.
9. PnL column renders +x.xx / -x.xx when non-zero, blank when zero.
"""
import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import (
    SignalCategory, SignalEntry, StrategyMode, StrategyUIState,
    SuppressionReason,
)
from ui.trade_blotter import (
    TradeBlotter,
    _FILTER_DEBUG, _FILTER_TRADE,
    _PNL_DISPLAY_THRESHOLD,
    _SignalTableModel,
)
from ui.main_window import (
    MainWindow, _SIGNAL_TYPE_BIAS_CHANGE, _SIGNAL_TYPE_REGIME_CHANGE,
    _LEGACY_CONTEXT_PREFIXES,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snap(tide_bias: str, wave_regime: str):
    """Build a minimal mock strategy snapshot with Tide and Wave sub-objects."""
    tide = MagicMock()
    tide.bias = tide_bias
    wave = MagicMock()
    wave.regime = wave_regime
    snap = MagicMock()
    snap.tide = tide
    snap.wave = wave
    # Make str() return value split-safe
    type(tide).bias = property(lambda self: tide_bias)
    type(wave).regime = property(lambda self: wave_regime)
    return snap


def _snap_metadata_from(main_win, bias, regime):
    """Override _last_strategy_snap with a mock and call _snap_metadata."""
    main_win._last_strategy_snap = _make_snap(bias, regime)
    return main_win._snap_metadata()


# ---------------------------------------------------------------------------
# Test 1: LEGACY_RAW suppressed when strategy armed
# ---------------------------------------------------------------------------

class TestLegacyRawSuppression(unittest.TestCase):

    def setUp(self):
        self.win = MainWindow()
        # Inject a blotter we can inspect
        self.blotter = self.win._strategy_dashboard.blotter

    def tearDown(self):
        self.win.close()

    def _emit_raw_signal(self, type_name="STACKED_IMBALANCE_BUY"):
        """Simulate a raw C++ engine signal via _on_signal_received."""
        sig = MagicMock()
        sig.type_name.return_value = type_name
        sig.price = 83000.0
        sig.strength = 0.5
        sig.description = "test"
        sig.timestamp = int(time.time() * 1000)
        self.win._on_signal_received(sig)

    def test_legacy_raw_suppressed_when_paper(self):
        """Test 1 — LEGACY_RAW signal is NOT added to blotter when PAPER armed."""
        self.win._strategy_ui_state = StrategyUIState.ARMED_WAITING
        self.win._strategy_mode = StrategyMode.PAPER
        before = self.blotter._model.rowCount()
        self._emit_raw_signal("STACKED_IMBALANCE_BUY")
        after = self.blotter._model.rowCount()
        self.assertEqual(before, after,
                         "LEGACY_RAW signal should be suppressed when armed")

    def test_legacy_raw_suppressed_when_live(self):
        """Test 1b — LEGACY_RAW signal suppressed in LIVE armed state."""
        self.win._strategy_ui_state = StrategyUIState.ARMED_ACTIVE
        self.win._strategy_mode = StrategyMode.LIVE
        before = self.blotter._model.rowCount()
        self._emit_raw_signal("BULLISH_IMBALANCE")
        after = self.blotter._model.rowCount()
        self.assertEqual(before, after,
                         "LEGACY_RAW signal should be suppressed in LIVE mode")

    def test_legacy_raw_visible_when_disarmed(self):
        """Test 2 — LEGACY_RAW signal IS added to blotter when DISARMED."""
        self.win._strategy_ui_state = StrategyUIState.DISARMED
        before = self.blotter._model.rowCount()
        self._emit_raw_signal("STACKED_IMBALANCE_BUY")
        after = self.blotter._model.rowCount()
        self.assertEqual(before + 1, after,
                         "LEGACY_RAW signal should appear when DISARMED")

    def test_context_signal_passes_through_when_armed(self):
        """Context signals (EXHAUSTION_, etc.) still reach blotter when armed."""
        self.win._strategy_ui_state = StrategyUIState.ARMED_WAITING
        before = self.blotter._model.rowCount()
        self._emit_raw_signal("EXHAUSTION_BUY")
        after = self.blotter._model.rowCount()
        self.assertEqual(before + 1, after,
                         "CONTEXT signal (EXHAUSTION_) should NOT be suppressed")


# ---------------------------------------------------------------------------
# Test 3 & 4: Tide/Wave transition events in _on_timer_tick
# ---------------------------------------------------------------------------

class TestTideWaveTransitions(unittest.TestCase):

    def setUp(self):
        self.win = MainWindow()
        self.blotter = self.win._strategy_dashboard.blotter
        # Reset caches so tests are independent
        self.win._last_tide_bias = None
        self.win._last_wave_regime = None

    def tearDown(self):
        self.win.close()

    def _run_tick_with_snap(self, tide_bias: str, wave_regime: str):
        """Inject a snapshot and run the Tide/Wave detection block directly."""
        self.win._last_strategy_snap = _make_snap(tide_bias, wave_regime)
        # Patch session.on_timer_tick to avoid live engine calls
        with patch.object(self.win._session, "on_timer_tick"), \
             patch.object(self.win._session, "block_status",
                          return_value=("", 0, 0)), \
             patch.object(self.win._session, "layered_push_status",
                          return_value={}):
            self.win._on_timer_tick()

    def _entries_of_category(self, cat: SignalCategory) -> list:
        return [e for e in self.win._strategy_dashboard.blotter._model._data
                if e.category == cat]

    def test_tide_bias_change_emits_entry(self):
        """Test 3 — TIDE BIAS_CHANGE entry emitted on bias transition."""
        # First tick: initialise cache (no emission)
        self._run_tick_with_snap("NEUTRAL", "NEUTRAL")
        tide_entries_before = self._entries_of_category(SignalCategory.TIDE)

        # Second tick: bias changes NEUTRAL → LONG
        self._run_tick_with_snap("LONG", "NEUTRAL")
        tide_entries_after = self._entries_of_category(SignalCategory.TIDE)

        self.assertEqual(len(tide_entries_after), len(tide_entries_before) + 1,
                         "Should have emitted one TIDE entry on bias change")
        entry = tide_entries_after[-1]
        self.assertEqual(entry.signal_type, _SIGNAL_TYPE_BIAS_CHANGE)
        self.assertEqual(entry.category, SignalCategory.TIDE)
        self.assertIn("NEUTRAL", entry.description)
        self.assertIn("LONG", entry.description)
        self.assertEqual(entry.source, "strategy")

    def test_wave_regime_change_emits_entry(self):
        """Test 4 — WAVE REGIME_CHANGE entry emitted on regime transition."""
        self._run_tick_with_snap("NEUTRAL", "NEUTRAL")
        wave_before = self._entries_of_category(SignalCategory.WAVE)

        self._run_tick_with_snap("NEUTRAL", "BREAKDOWN")
        wave_after = self._entries_of_category(SignalCategory.WAVE)

        self.assertEqual(len(wave_after), len(wave_before) + 1,
                         "Should have emitted one WAVE entry on regime change")
        entry = wave_after[-1]
        self.assertEqual(entry.signal_type, _SIGNAL_TYPE_REGIME_CHANGE)
        self.assertEqual(entry.category, SignalCategory.WAVE)
        self.assertIn("NEUTRAL", entry.description)
        self.assertIn("BREAKDOWN", entry.description)

    def test_no_first_tick_spurious_event(self):
        """Test 5a — first tick initialises cache without emitting."""
        tide_before = self._entries_of_category(SignalCategory.TIDE)
        wave_before = self._entries_of_category(SignalCategory.WAVE)

        # First tick (caches were None)
        self._run_tick_with_snap("LONG", "BREAKDOWN")

        tide_after = self._entries_of_category(SignalCategory.TIDE)
        wave_after = self._entries_of_category(SignalCategory.WAVE)
        self.assertEqual(tide_before, tide_after,
                         "First tick must NOT emit a TIDE entry")
        self.assertEqual(wave_before, wave_after,
                         "First tick must NOT emit a WAVE entry")
        # Caches should now be populated
        self.assertEqual(self.win._last_tide_bias, "LONG")
        self.assertEqual(self.win._last_wave_regime, "BREAKDOWN")

    def test_no_spurious_event_when_stable(self):
        """Test 5 — no duplicate events when bias/regime unchanged."""
        self._run_tick_with_snap("NEUTRAL", "NEUTRAL")  # initialise
        tide_before = self._entries_of_category(SignalCategory.TIDE)
        wave_before = self._entries_of_category(SignalCategory.WAVE)

        # Same values again — should not emit
        self._run_tick_with_snap("NEUTRAL", "NEUTRAL")

        tide_after = self._entries_of_category(SignalCategory.TIDE)
        wave_after = self._entries_of_category(SignalCategory.WAVE)
        self.assertEqual(tide_before, tide_after,
                         "No TIDE entry emitted when bias stable")
        self.assertEqual(wave_before, wave_after,
                         "No WAVE entry emitted when regime stable")

    def test_wiring_timer_tick_emits_tide_wave_on_change(self):
        """§3.5 Wiring test — _on_timer_tick produces TIDE/WAVE entries when
        snapshot changes (not just that the emission helper works in isolation).
        """
        # Initialise caches via first tick
        self._run_tick_with_snap("NEUTRAL", "NEUTRAL")
        initial_total = self.blotter._model.rowCount()

        # Change both bias and regime simultaneously
        self._run_tick_with_snap("LONG", "TRENDING")
        final_total = self.blotter._model.rowCount()

        # Expect at least 2 new entries (TIDE + WAVE)
        self.assertGreaterEqual(final_total, initial_total + 2,
                                "Expected ≥2 new entries (TIDE + WAVE) in blotter")
        categories = [e.category for e in self.blotter._model._data]
        self.assertIn(SignalCategory.TIDE, categories)
        self.assertIn(SignalCategory.WAVE, categories)


# ---------------------------------------------------------------------------
# Test 6: Exit realized_pnl annotation
# ---------------------------------------------------------------------------

class TestExitPnlAnnotation(unittest.TestCase):

    def setUp(self):
        self.win = MainWindow()
        self.blotter = self.win._strategy_dashboard.blotter

    def tearDown(self):
        self.win.close()

    def _make_exit_decision(self, intent_name="EXIT_BOUNCE",
                            confidence=0.8, price=83000.0):
        """Build a minimal mock RippleDecision for an exit intent."""
        decision = MagicMock()
        decision.intent = MagicMock()
        # Make str(decision.intent).split(".")[-1] return intent_name
        decision.intent.__str__ = lambda self: f"RippleIntent.{intent_name}"
        decision.timestamp = int(time.time() * 1000)
        decision.reference_price = price
        decision.confidence = confidence
        decision.reason = "test exit"
        decision.state_summary = ""
        decision.triggering_state = MagicMock()
        decision.triggering_state.__str__ = lambda self: "TRADE.EXIT"
        return decision

    def test_exit_entry_has_realized_pnl(self):
        """Test 6 — EXIT TRADE_LIFECYCLE entry has non-zero realized_pnl."""
        # Arm the strategy so _on_ripple_received doesn't gate on DISARMED
        self.win._strategy_ui_state = StrategyUIState.ARMED_ACTIVE
        self.win._strategy_mode = StrategyMode.LIVE

        # Mock exec_manager with session_realized_pnl attribute
        mock_exec = MagicMock()
        pnl_values = iter([100.0, 115.50])  # before → after processing
        mock_exec.session_realized_pnl = property(
            lambda self: next(pnl_values, 115.50))
        # Use getattr-compatible mock (direct attribute, not property)
        mock_exec.session_realized_pnl = 100.0
        mock_exec.armed = True
        self.win._exec_manager = mock_exec

        # Intercept broadcast to capture the entry
        captured = []
        original_broadcast = self.win._broadcast_entry

        def capture(entry):
            captured.append(entry)
            original_broadcast(entry)

        self.win._broadcast_entry = capture

        # Simulate pnl changing after on_intent is called
        def fake_on_intent(intent):
            mock_exec.session_realized_pnl = 115.50

        mock_exec.on_intent.side_effect = fake_on_intent

        decision = self._make_exit_decision()
        with patch("ui.main_window._parse_intent_name",
                   return_value="EXIT_BOUNCE"), \
             patch("ui.main_window.intent_risk_block_reason",
                   return_value=None), \
             patch("ui.main_window.ripple_decision_to_intent") as mock_rdi:
            mock_intent = MagicMock()
            mock_intent.intent_type = "exit"
            mock_intent.reference_price = 83000.0
            mock_rdi.return_value = mock_intent
            self.win._on_ripple_received(decision)

        # Find the EXIT entry
        exit_entries = [e for e in captured
                        if e.category == SignalCategory.TRADE_LIFECYCLE]
        self.assertTrue(len(exit_entries) >= 1,
                        "Expected at least one TRADE_LIFECYCLE exit entry")
        exit_entry = exit_entries[-1]
        self.assertAlmostEqual(exit_entry.realized_pnl, 15.50, places=4,
                               msg="realized_pnl should be pnl_after - pnl_before")

    def test_exit_pnl_zero_when_no_exec_manager(self):
        """Test 6b — EXIT entry has realized_pnl=0 when exec_manager is None."""
        self.win._strategy_ui_state = StrategyUIState.ARMED_ACTIVE
        self.win._strategy_mode = StrategyMode.PAPER
        self.win._exec_manager = None

        captured = []
        original_broadcast = self.win._broadcast_entry

        def capture(entry):
            captured.append(entry)
            original_broadcast(entry)

        self.win._broadcast_entry = capture

        decision = self._make_exit_decision()
        with patch("ui.main_window._parse_intent_name",
                   return_value="EXIT_BOUNCE"), \
             patch("ui.main_window.ripple_decision_to_intent",
                   return_value=None):
            self.win._on_ripple_received(decision)

        exit_entries = [e for e in captured
                        if e.category == SignalCategory.TRADE_LIFECYCLE]
        if exit_entries:
            self.assertAlmostEqual(exit_entries[-1].realized_pnl, 0.0, places=6,
                                   msg="realized_pnl should be 0.0 when no exec_manager")


# ---------------------------------------------------------------------------
# Test 7 & 8: TradeBlotter filter buttons
# ---------------------------------------------------------------------------

class TestBlotterFilters(unittest.TestCase):

    def setUp(self):
        self.blotter = TradeBlotter()
        now = int(time.time() * 1000)
        # Add one entry per category we care about
        self.blotter.add_entry(SignalEntry(
            timestamp=now, signal_type="ENTRY_BOUNCE_LONG", source="ripple",
            category=SignalCategory.TRADE_LIFECYCLE, description="Entry"))
        self.blotter.add_entry(SignalEntry(
            timestamp=now + 1, signal_type="EXEC_BUY", source="execution",
            category=SignalCategory.EXECUTION, description="Exec"))
        self.blotter.add_entry(SignalEntry(
            timestamp=now + 2, signal_type="STACKED_IMBALANCE", source="legacy",
            category=SignalCategory.LEGACY_RAW, description="Raw"))
        self.blotter.add_entry(SignalEntry(
            timestamp=now + 3, signal_type="DIAG_01", source="diagnostic",
            category=SignalCategory.DIAGNOSTIC, description="Diag"))
        self.blotter.add_entry(SignalEntry(
            timestamp=now + 4, signal_type="STRATEGY_ARM", source="strategy",
            category=SignalCategory.STRATEGY_ARM, description="Arm"))

    def test_trades_only_filter_shows_trade_lifecycle_and_execution(self):
        """Test 7 — Trades filter shows only TRADE_LIFECYCLE + EXECUTION."""
        self.blotter._btn_trades_only.setChecked(True)
        visible = self.blotter._proxy.rowCount()
        # Only TRADE_LIFECYCLE + EXECUTION rows (2 entries)
        self.assertEqual(visible, 2,
                         f"Trades filter should show 2 rows (got {visible})")
        # Verify categories
        for row in range(visible):
            idx = self.blotter._proxy.index(row, 0)
            src_idx = self.blotter._proxy.mapToSource(idx)
            from PySide6.QtCore import Qt
            entry = self.blotter._model.data(src_idx, Qt.UserRole)
            self.assertIn(entry.category,
                          (SignalCategory.TRADE_LIFECYCLE, SignalCategory.EXECUTION),
                          f"Unexpected category in Trades filter: {entry.category}")

    def test_debug_filter_shows_legacy_raw_and_diagnostic(self):
        """Test 8 — Debug filter shows LEGACY_RAW + DIAGNOSTIC."""
        self.blotter._btn_debug.setChecked(True)
        visible = self.blotter._proxy.rowCount()
        # Only LEGACY_RAW + DIAGNOSTIC (2 entries)
        self.assertEqual(visible, 2,
                         f"Debug filter should show 2 rows (got {visible})")
        from PySide6.QtCore import Qt
        for row in range(visible):
            idx = self.blotter._proxy.index(row, 0)
            src_idx = self.blotter._proxy.mapToSource(idx)
            entry = self.blotter._model.data(src_idx, Qt.UserRole)
            self.assertIn(entry.category,
                          (SignalCategory.LEGACY_RAW, SignalCategory.DIAGNOSTIC),
                          f"Unexpected category in Debug filter: {entry.category}")

    def test_trades_only_hides_non_trade_entries(self):
        """Test 7b — Trades filter hides Strategy/Raw/Diagnostic entries."""
        self.blotter._btn_trades_only.setChecked(True)
        visible = self.blotter._proxy.rowCount()
        total = self.blotter._model.rowCount()
        self.assertLess(visible, total,
                        "Trades filter should hide some entries")

    def test_debug_excludes_strategy_and_trade_entries(self):
        """Test 8b — Debug filter does NOT show STRATEGY_ARM or TRADE_LIFECYCLE."""
        self.blotter._btn_debug.setChecked(True)
        from PySide6.QtCore import Qt
        visible_cats = []
        for row in range(self.blotter._proxy.rowCount()):
            idx = self.blotter._proxy.index(row, 0)
            src_idx = self.blotter._proxy.mapToSource(idx)
            entry = self.blotter._model.data(src_idx, Qt.UserRole)
            visible_cats.append(entry.category)
        self.assertNotIn(SignalCategory.STRATEGY_ARM, visible_cats)
        self.assertNotIn(SignalCategory.TRADE_LIFECYCLE, visible_cats)


# ---------------------------------------------------------------------------
# Test 9: PnL column rendering
# ---------------------------------------------------------------------------

class TestPnlColumnRendering(unittest.TestCase):

    def test_pnl_positive_renders_with_plus_sign(self):
        """Test 9 — +x.xx format for positive realized_pnl."""
        e = SignalEntry(realized_pnl=15.50)
        rendered = _SignalTableModel._display(e, 7)
        self.assertEqual(rendered, "+15.50",
                         "Positive PnL should render as '+15.50'")

    def test_pnl_negative_renders_with_minus(self):
        """Test 9 — -x.xx format for negative realized_pnl."""
        e = SignalEntry(realized_pnl=-3.25)
        rendered = _SignalTableModel._display(e, 7)
        self.assertEqual(rendered, "-3.25",
                         "Negative PnL should render as '-3.25'")

    def test_pnl_zero_renders_blank(self):
        """Test 9 — blank when realized_pnl is 0."""
        e = SignalEntry(realized_pnl=0.0)
        rendered = _SignalTableModel._display(e, 7)
        self.assertEqual(rendered, "",
                         "Zero PnL should render as empty string")

    def test_pnl_below_threshold_renders_blank(self):
        """Test 9 — blank when |realized_pnl| < _PNL_DISPLAY_THRESHOLD."""
        e = SignalEntry(realized_pnl=_PNL_DISPLAY_THRESHOLD * 0.5)
        rendered = _SignalTableModel._display(e, 7)
        self.assertEqual(rendered, "",
                         "Sub-threshold PnL should render as empty string")

    def test_description_still_in_col8(self):
        """PnL column insertion must not break description at col 8."""
        e = SignalEntry(description="test desc", state_summary="ENTRY")
        rendered = _SignalTableModel._display(e, 8)
        self.assertIn("test desc", rendered)
        self.assertIn("ENTRY", rendered)

    def test_pnl_default_zero_on_existing_entries(self):
        """realized_pnl defaults to 0.0 — existing entries unaffected."""
        e = SignalEntry(
            signal_type="ENTRY_BOUNCE_LONG",
            source="ripple",
            category=SignalCategory.TRADE_LIFECYCLE,
            price=83000.0,
        )
        self.assertEqual(e.realized_pnl, 0.0)
        rendered = _SignalTableModel._display(e, 7)
        self.assertEqual(rendered, "")


# ---------------------------------------------------------------------------
# Additional: Filter constants
# ---------------------------------------------------------------------------

class TestFilterConstants(unittest.TestCase):

    def test_filter_debug_contents(self):
        """_FILTER_DEBUG contains exactly LEGACY_RAW + DIAGNOSTIC."""
        self.assertIn(SignalCategory.LEGACY_RAW, _FILTER_DEBUG)
        self.assertIn(SignalCategory.DIAGNOSTIC, _FILTER_DEBUG)
        self.assertEqual(len(_FILTER_DEBUG), 2)

    def test_filter_debug_excludes_context(self):
        """_FILTER_DEBUG must NOT include CONTEXT (that's _FILTER_RAW)."""
        self.assertNotIn(SignalCategory.CONTEXT, _FILTER_DEBUG)

    def test_pnl_threshold_is_named_constant(self):
        """_PNL_DISPLAY_THRESHOLD must be accessible and positive."""
        self.assertGreater(_PNL_DISPLAY_THRESHOLD, 0.0)
        self.assertLess(_PNL_DISPLAY_THRESHOLD, 1e-6)

    def test_signal_type_constants_defined(self):
        """Named signal-type constants must match spec strings."""
        self.assertEqual(_SIGNAL_TYPE_BIAS_CHANGE, "BIAS_CHANGE")
        self.assertEqual(_SIGNAL_TYPE_REGIME_CHANGE, "REGIME_CHANGE")

    def test_legacy_context_prefixes_match_blotter(self):
        """_LEGACY_CONTEXT_PREFIXES in main_window must mirror trade_blotter."""
        from ui.trade_blotter import _CONTEXT_SIGNAL_PREFIXES
        for p in _LEGACY_CONTEXT_PREFIXES:
            self.assertIn(p, _CONTEXT_SIGNAL_PREFIXES,
                          f"'{p}' not in trade_blotter._CONTEXT_SIGNAL_PREFIXES")


if __name__ == "__main__":
    unittest.main(verbosity=2)
