"""Phase 9D — Dead Element Audit & Toolbar Cleanup regression suite.

Covers UI_STRATEGY_INTEGRATION_PLAN.md §22.5 acceptance criteria:

1. QML toolbar contains no disabled or hidden controls inherited from
   the old QWidget toolbar.  ``_tick_size_input`` / ``_imbalance_input``
   / ``_candle_combo`` and the disabled "Replay" item are fully
   removed from ``ui.main_window.MainWindow``.
2. The chart timeframe ``ComboBox`` in ``CandleChartView.qml`` is the
   only candle-duration control; ``MainWindowBridge.setCandleBucketMs``
   routes to ``MainWindow.set_candle_duration_ms`` AND
   ``CandleItem.bucketMs`` so every dependent view updates.
3. Each overlay ``CheckBox`` persists its state via ``Qt.labs.settings``;
   ``MainWindowBridge.setOverlayEnabled`` toggles the per-overlay
   ``enabled`` flag on ``CandleChartViewWidget``;
   ``_draw_overlays_fn`` skips overlays where ``enabled=False``.
4. Suppression metrics surface in the diagnostics tooltip:
   ``SuppressionModel.update(_ripple_metrics)`` emits notify on change
   and the resulting ``summary`` string mentions every counter.
   ``MainWindowBridge.on_timer_tick`` refreshes the model from the
   hidden ``MainWindow._ripple_metrics`` within one tick.

All tests run under ``QT_QPA_PLATFORM=offscreen`` so they execute in
headless CI / Docker without a display.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.chart_overlays import (  # noqa: E402
    EmaOverlay, SmaOverlay, StructuralLevelsOverlay,
    VolProfileOverlay, VwapOverlay,
)
from ui.main_window_bridge import MainWindowBridge  # noqa: E402
from ui.models.position_model import PositionModel  # noqa: E402
from ui.models.snapshot_model import SnapshotModel  # noqa: E402
from ui.models.suppression_model import (  # noqa: E402
    SuppressionModel, SuppressionSnapshot, _snapshot_from_metrics,
)
from ui.models.trade_blotter_model import TradeBlotterModel  # noqa: E402


_QML_DIR = _PROJECT_ROOT / "ui" / "qml" / "components"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gui_app() -> QCoreApplication:
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv[:1])
    return instance


class _FakeRippleMetrics:
    """Duck-typed ``_SuppressionMetrics`` for bridge tests."""

    def __init__(
        self,
        emitted: int = 0,
        cooldown_suppressed: int = 0,
        dedupe_suppressed: int = 0,
        mode_suppressed: int = 0,
        confidence_suppressed: int = 0,
        inventory_suppressed: int = 0,
    ) -> None:
        self.emitted = emitted
        self.cooldown_suppressed = cooldown_suppressed
        self.dedupe_suppressed = dedupe_suppressed
        self.mode_suppressed = mode_suppressed
        self.confidence_suppressed = confidence_suppressed
        self.inventory_suppressed = inventory_suppressed


def _fresh_bridge(suppression: bool = True) -> MainWindowBridge:
    snap = SnapshotModel()
    pos = PositionModel()
    blotter = TradeBlotterModel()
    sup = SuppressionModel() if suppression else None
    return MainWindowBridge(snap, pos, blotter, suppression_model=sup)


# ===========================================================================
# AC #1 — Dead element cleanup in MainWindow.
# ===========================================================================

class TestMainWindowDeadElementsRemoved(unittest.TestCase):
    """AC #1 — confirm the QWidget MainWindow no longer ships the
    Phase 9D-removed controls.  ``MainWindow`` is still importable
    (the hidden engine host needs it); we just want its toolbar to
    match the Phase 9D spec."""

    def setUp(self) -> None:
        self.app = _gui_app()
        from ui.main_window import MainWindow
        self.mw = MainWindow()

    def tearDown(self) -> None:
        self.mw.deleteLater()
        self.app.processEvents()

    def test_tick_size_input_removed(self) -> None:
        self.assertFalse(hasattr(self.mw, "_tick_size_input"))
        self.assertFalse(hasattr(self.mw, "_tick_size_label"))

    def test_imbalance_input_removed(self) -> None:
        self.assertFalse(hasattr(self.mw, "_imbalance_input"))
        self.assertFalse(hasattr(self.mw, "_imbalance_label"))

    def test_candle_combo_removed(self) -> None:
        self.assertFalse(hasattr(self.mw, "_candle_combo"))
        # Bucket duration is mutable via the new public mutator.
        self.assertTrue(hasattr(self.mw, "set_candle_duration_ms"))

    def test_mode_combo_only_live_item(self) -> None:
        combo = self.mw._mode_combo
        self.assertEqual(combo.count(), 1)
        self.assertEqual(combo.itemText(0), "Live")
        self.assertEqual(combo.currentText(), "Live")

    def test_set_candle_duration_ms_updates_state(self) -> None:
        self.mw.set_candle_duration_ms(300_000)
        self.assertEqual(self.mw._candle_duration_ms, 300_000)
        self.mw.set_candle_duration_ms(60_000)
        self.assertEqual(self.mw._candle_duration_ms, 60_000)

    def test_set_candle_duration_ms_ignores_invalid(self) -> None:
        original = self.mw._candle_duration_ms
        self.mw.set_candle_duration_ms(0)
        self.assertEqual(self.mw._candle_duration_ms, original)
        self.mw.set_candle_duration_ms(-5)
        self.assertEqual(self.mw._candle_duration_ms, original)


# ===========================================================================
# AC #2 — Candle timeframe ComboBox routing.
# ===========================================================================

class TestCandleBucketMsRouting(unittest.TestCase):
    """AC #2 — ``MainWindowBridge.setCandleBucketMs`` is the single
    funnel for changing the candle timeframe.  It must update both the
    legacy ``MainWindow`` (so the heatmap / VP / market state pick up
    the new bucket) and the native QML ``CandleItem`` (so its scene
    graph re-buckets)."""

    def setUp(self) -> None:
        self.app = _gui_app()
        self.bridge = _fresh_bridge()

    def test_setter_routes_to_main_window(self) -> None:
        mw = MagicMock()
        self.bridge.set_main_window(mw)
        self.bridge.setCandleBucketMs(300_000)
        mw.set_candle_duration_ms.assert_called_once_with(300_000)

    def test_setter_updates_candle_item_bucket(self) -> None:
        candle = MagicMock()
        candle.bucketMs = 60_000
        self.bridge.set_candle_item(candle)
        self.bridge.setCandleBucketMs(900_000)
        self.assertEqual(candle.bucketMs, 900_000)

    def test_setter_ignores_zero_or_negative(self) -> None:
        mw = MagicMock()
        self.bridge.set_main_window(mw)
        self.bridge.setCandleBucketMs(0)
        self.bridge.setCandleBucketMs(-1)
        mw.set_candle_duration_ms.assert_not_called()

    def test_setter_idle_when_no_main_window(self) -> None:
        # Sanity: should not raise even without an attached MainWindow.
        self.bridge.setCandleBucketMs(60_000)

    def test_main_window_failure_is_swallowed(self) -> None:
        mw = MagicMock()
        mw.set_candle_duration_ms.side_effect = RuntimeError("boom")
        self.bridge.set_main_window(mw)
        candle = MagicMock()
        candle.bucketMs = 60_000
        self.bridge.set_candle_item(candle)
        # The bridge must still update the QML candle item even if the
        # MainWindow setter throws — partial recovery is preferable to
        # propagating the error to the QML render thread.
        self.bridge.setCandleBucketMs(900_000)
        self.assertEqual(candle.bucketMs, 900_000)


class TestCandleChartToolbarQml(unittest.TestCase):
    """Static QML inspection: the new toolbar exists, has a ComboBox,
    five overlay CheckBoxes, and uses ``Qt.labs.settings``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (_QML_DIR / "CandleChartView.qml").read_text()

    def test_imports_qtcore_settings(self) -> None:
        # Qt 6 moved ``Settings`` out of ``Qt.labs.settings``; the
        # supported path is ``import QtCore`` (which exposes the
        # ``Settings`` element).  See QTBUG-110572.
        self.assertIn("import QtCore", self.text)

    def test_toolbar_object_present(self) -> None:
        self.assertIn('objectName: "candleChartToolbar"', self.text)
        self.assertIn('objectName: "candleDurationCombo"', self.text)

    def test_overlay_checkboxes_present(self) -> None:
        for tag in (
            'objectName: "overlayChkSma"',
            'objectName: "overlayChkEma"',
            'objectName: "overlayChkVwap"',
            'objectName: "overlayChkStructural"',
            'objectName: "overlayChkVolProfile"',
        ):
            self.assertIn(tag, self.text, msg=tag)

    def test_combo_uses_bridge_setter(self) -> None:
        # ComboBox must wire to the bridge slot named in AC #2.
        self.assertIn("setCandleBucketMs", self.text)

    def test_no_disabled_or_hidden_controls(self) -> None:
        # AC #1 — no Phase 9A→9C carry-over disabled/hidden controls.
        self.assertNotIn("setEnabled(false)", self.text)
        self.assertNotIn("setVisible(false)", self.text)
        self.assertNotIn("enabled: false", self.text)
        self.assertNotIn("visible: false", self.text)


# ===========================================================================
# AC #3 — Overlay CheckBox toggle + persistence.
# ===========================================================================

class TestOverlayEnabledFlags(unittest.TestCase):
    """AC #3 — the per-overlay ``enabled`` flag is the contract that
    powers the QML CheckBox row.  ``_draw_overlays_fn`` must skip
    disabled overlays without raising."""

    def test_default_enabled_true(self) -> None:
        for cls in (SmaOverlay, EmaOverlay, VwapOverlay,
                    StructuralLevelsOverlay, VolProfileOverlay):
            self.assertTrue(cls().enabled, msg=cls.__name__)

    def test_overlay_name_constants(self) -> None:
        self.assertEqual(SmaOverlay.OVERLAY_NAME, "sma")
        self.assertEqual(EmaOverlay.OVERLAY_NAME, "ema")
        self.assertEqual(VwapOverlay.OVERLAY_NAME, "vwap")
        self.assertEqual(StructuralLevelsOverlay.OVERLAY_NAME, "structural")
        self.assertEqual(VolProfileOverlay.OVERLAY_NAME, "volprofile")

    def test_disabled_overlay_skipped(self) -> None:
        """A disabled overlay must not be invoked by ``_draw_overlays_fn``."""
        from ui.candle_chart_view import CandleChartView
        from ui.market_state import MarketState

        app = _gui_app()  # noqa: F841

        widget = CandleChartView(MarketState())
        try:
            calls: list[str] = []

            def make_fake(name: str):
                class _Fake:
                    OVERLAY_NAME = name
                    enabled = True

                    def __call__(self, *args, **kwargs):
                        calls.append(name)
                return _Fake()

            widget.clear_overlays()
            widget._overlays.append(make_fake("sma"))
            widget._overlays.append(make_fake("ema"))

            widget.set_overlay_enabled("ema", False)
            widget._draw_overlays_fn(None, 0, 0, 10, 10, 0.0, 1.0, [])
            self.assertEqual(calls, ["sma"])
            calls.clear()

            widget.set_overlay_enabled("ema", True)
            widget._draw_overlays_fn(None, 0, 0, 10, 10, 0.0, 1.0, [])
            self.assertEqual(set(calls), {"sma", "ema"})
        finally:
            widget.deleteLater()

    def test_set_overlay_enabled_unknown_name_returns_false(self) -> None:
        from ui.candle_chart_view import CandleChartView
        from ui.market_state import MarketState

        widget = CandleChartView(MarketState())
        try:
            changed = widget.set_overlay_enabled("nope", False)
            self.assertFalse(changed)
        finally:
            widget.deleteLater()

    def test_overlay_enabled_state_dict(self) -> None:
        from ui.candle_chart_view import CandleChartView
        from ui.market_state import MarketState

        widget = CandleChartView(MarketState())
        try:
            state = widget.overlay_enabled_state()
            for k in ("sma", "ema", "vwap", "structural", "volprofile"):
                self.assertIn(k, state)
                self.assertTrue(state[k], msg=k)
            widget.set_overlay_enabled("vwap", False)
            state = widget.overlay_enabled_state()
            self.assertFalse(state["vwap"])
        finally:
            widget.deleteLater()


class TestBridgeSetOverlayEnabled(unittest.TestCase):
    """AC #3 — ``MainWindowBridge.setOverlayEnabled`` routes to the
    legacy ``CandleChartViewWidget`` and is idempotent / safe in
    headless tests."""

    def setUp(self) -> None:
        self.app = _gui_app()
        self.bridge = _fresh_bridge()

    def test_routes_to_candle_view(self) -> None:
        mw = MagicMock()
        mw._candle_view = MagicMock()
        mw._candle_view.set_overlay_enabled.return_value = True
        self.bridge.set_main_window(mw)
        self.bridge.setOverlayEnabled("sma", False)
        mw._candle_view.set_overlay_enabled.assert_called_once_with("sma", False)

    def test_no_main_window_is_noop(self) -> None:
        # No exception when called before MainWindow is attached.
        self.bridge.setOverlayEnabled("sma", True)

    def test_no_candle_view_is_noop(self) -> None:
        mw = MagicMock(spec=[])
        self.bridge.set_main_window(mw)
        self.bridge.setOverlayEnabled("sma", True)  # must not raise

    def test_empty_name_ignored(self) -> None:
        mw = MagicMock()
        mw._candle_view = MagicMock()
        self.bridge.set_main_window(mw)
        self.bridge.setOverlayEnabled("", True)
        mw._candle_view.set_overlay_enabled.assert_not_called()


class TestOverlaySettingsPersistence(unittest.TestCase):
    """Static check: the QML toolbar declares a ``Qt.labs.settings``
    block under the ``Phase9D/ChartOverlays`` category with one
    boolean property per overlay.  This is what makes AC #3 persist
    state across restarts."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (_QML_DIR / "CandleChartView.qml").read_text()

    def test_settings_block_present(self) -> None:
        self.assertIn("Settings {", self.text)
        self.assertIn("category: \"Phase9D/ChartOverlays\"", self.text)

    def test_settings_props_present(self) -> None:
        for prop in ("sma", "ema", "vwap", "structural", "volprofile"):
            self.assertIn(f"property bool {prop}: true", self.text,
                          msg=prop)


# ===========================================================================
# AC #4 — Suppression metrics surface in diagnostics tooltip.
# ===========================================================================

class TestSuppressionModelMutations(unittest.TestCase):
    """AC #4 — :class:`SuppressionModel` accepts a duck-typed
    ``_SuppressionMetrics`` and emits notify signals only on real
    change, mirroring the dedupe discipline used by SnapshotModel /
    PositionModel."""

    def setUp(self) -> None:
        self.app = _gui_app()
        self.model = SuppressionModel()
        self.snapshot_signals = 0
        self.summary_signals = 0
        self.model.totalSuppressedChanged.connect(self._bump_total)
        self.model.summaryChanged.connect(self._bump_summary)

    def _bump_total(self) -> None:
        self.snapshot_signals += 1

    def _bump_summary(self) -> None:
        self.summary_signals += 1

    def test_initial_values(self) -> None:
        self.assertEqual(self.model.emitted, 0)
        self.assertEqual(self.model.totalSuppressed, 0)
        self.assertIn("No ripple decisions yet", self.model.summary)

    def test_update_emits_signals(self) -> None:
        metrics = _FakeRippleMetrics(
            emitted=3,
            cooldown_suppressed=2,
            dedupe_suppressed=1,
        )
        self.model.update(metrics)
        self.assertEqual(self.model.emitted, 3)
        self.assertEqual(self.model.cooldownSuppressed, 2)
        self.assertEqual(self.model.dedupeSuppressed, 1)
        self.assertEqual(self.model.totalSuppressed, 3)
        self.assertEqual(self.snapshot_signals, 1)
        self.assertEqual(self.summary_signals, 1)

    def test_update_dedupes_unchanged(self) -> None:
        metrics = _FakeRippleMetrics(emitted=1, cooldown_suppressed=1)
        self.model.update(metrics)
        self.snapshot_signals = 0
        self.summary_signals = 0
        # Same metrics → no notify.
        self.model.update(metrics)
        self.assertEqual(self.snapshot_signals, 0)
        self.assertEqual(self.summary_signals, 0)

    def test_summary_contains_every_counter(self) -> None:
        self.model.update(_FakeRippleMetrics(
            emitted=10,
            cooldown_suppressed=2,
            dedupe_suppressed=3,
            mode_suppressed=4,
            confidence_suppressed=5,
            inventory_suppressed=6,
        ))
        s = self.model.summary
        for token in ("cooldown", "dedupe", "mode", "confidence", "inventory"):
            self.assertIn(token, s, msg=token)
        self.assertIn("10", s)  # emitted count
        self.assertIn("20", s)  # 2+3+4+5+6 = 20

    def test_clear_resets(self) -> None:
        self.model.update(_FakeRippleMetrics(emitted=5, cooldown_suppressed=1))
        self.assertGreater(self.model.totalSuppressed, 0)
        self.model.clear()
        self.assertEqual(self.model.emitted, 0)
        self.assertEqual(self.model.totalSuppressed, 0)

    def test_snapshot_from_metrics_handles_none(self) -> None:
        snap = _snapshot_from_metrics(None)
        self.assertEqual(snap, SuppressionSnapshot())

    def test_snapshot_from_metrics_handles_partial(self) -> None:
        class _Partial:
            emitted = 5  # Missing other fields

        snap = _snapshot_from_metrics(_Partial())
        self.assertEqual(snap.emitted, 5)
        self.assertEqual(snap.cooldown_suppressed, 0)


class TestBridgeRefreshesSuppression(unittest.TestCase):
    """AC #4 — ``MainWindowBridge.on_timer_tick`` lifts
    ``_main_window._ripple_metrics`` into the QML suppression model
    so the tooltip refreshes within one render frame."""

    def setUp(self) -> None:
        self.app = _gui_app()
        self.bridge = _fresh_bridge()

    def test_timer_tick_refreshes_suppression(self) -> None:
        mw = MagicMock()
        mw._ripple_metrics = _FakeRippleMetrics(
            emitted=2, cooldown_suppressed=1, dedupe_suppressed=1,
        )
        self.bridge.set_main_window(mw)
        self.bridge.on_timer_tick(None, None)
        self.assertEqual(self.bridge.suppression_model.emitted, 2)
        self.assertEqual(self.bridge.suppression_model.totalSuppressed, 2)

    def test_timer_tick_idle_without_main_window(self) -> None:
        # Should not raise; suppression model stays zero.
        self.bridge.on_timer_tick(None, None)
        self.assertEqual(self.bridge.suppression_model.emitted, 0)

    def test_clear_session_resets_suppression(self) -> None:
        mw = MagicMock()
        mw._ripple_metrics = _FakeRippleMetrics(emitted=5, cooldown_suppressed=2)
        self.bridge.set_main_window(mw)
        self.bridge.on_timer_tick(None, None)
        self.assertEqual(self.bridge.suppression_model.emitted, 5)
        self.bridge.clear_session()
        self.assertEqual(self.bridge.suppression_model.emitted, 0)
        self.assertEqual(self.bridge.suppression_model.totalSuppressed, 0)


class TestSuppressionDiagnosticsQmlStatic(unittest.TestCase):
    """Static inspection: the diagnostics panel has a Ripples row with
    a ToolTip bound to ``suppressionModel.summary``."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = (_QML_DIR / "StrategyDiagnostics.qml").read_text()

    def test_suppression_row_present(self) -> None:
        self.assertIn('objectName: "suppressionRow"', self.text)

    def test_ripples_label_present(self) -> None:
        self.assertIn("Ripples", self.text)

    def test_tooltip_bound_to_summary(self) -> None:
        self.assertIn("ToolTip.text", self.text)
        self.assertIn("suppression.summary", self.text)


# ===========================================================================
# Regression guard — Phase 9A → 9D imports still resolve.
# ===========================================================================

class TestPhase9DRegressionImports(unittest.TestCase):
    def test_suppression_model_importable(self) -> None:
        import ui.models.suppression_model as mod
        self.assertTrue(hasattr(mod, "SuppressionModel"))
        self.assertTrue(hasattr(mod, "SuppressionSnapshot"))

    def test_chart_overlays_have_name_attribute(self) -> None:
        for cls in (SmaOverlay, EmaOverlay, VwapOverlay,
                    StructuralLevelsOverlay, VolProfileOverlay):
            self.assertTrue(hasattr(cls, "OVERLAY_NAME"), msg=cls.__name__)
            self.assertTrue(hasattr(cls(), "enabled"), msg=cls.__name__)

    def test_bridge_has_toolbar_slots(self) -> None:
        bridge = _fresh_bridge()
        self.assertTrue(hasattr(bridge, "setCandleBucketMs"))
        self.assertTrue(hasattr(bridge, "setOverlayEnabled"))
        self.assertTrue(hasattr(bridge, "set_main_window"))


if __name__ == "__main__":
    unittest.main()
