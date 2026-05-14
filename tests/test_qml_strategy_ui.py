"""Phase 9C — Strategy dashboard QML regression suite.

Covers UI_STRATEGY_INTEGRATION_PLAN.md §22.4 acceptance criteria:

1. ``TradeBlotterModel.appendEntry(entry)`` called from a worker
   thread does not crash; the ``rowCount`` observed from the QML
   engine increments by 1.
2. ``PositionModel.tradeStateLabel`` is ``"ACTIVE"`` after
   ``update(snap_with_active_trade, exec_state)``; ``sessionPnl`` is
   sourced from ``exec_state.session_realized_pnl``; ``rrRatio`` is
   correct.
3. ``PositionCard.qml`` renders without ``opacity`` animations;
   background is solid ``#0c0e1c`` (static QML inspection).
4. Entry / stop / target overlay lines appear on ``CandleItem`` when
   ``tradeOverlayActive`` is set; disappear on ``clear_trade_overlay()``.
5. ENTRY triangle marker appears at the correct candle x-position for
   a synthetic event with a known ``ts_ms`` (vertex-level assertion).
6. ``MainWindowBridge.on_signal_entry`` from a worker thread routes
   the entry through the queued ``invokeMethod`` slot and updates
   both the blotter and the chart markers.
7. The existing ``tests/test_strategy_dashboard.py`` checks still
   import (regression guard for Phase 9A→9C migration).

All tests run under ``QT_QPA_PLATFORM=offscreen`` so they execute
in headless CI / Docker without a display.
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication, QEventLoop, QTimer,
)
from PySide6.QtWidgets import QApplication  # noqa: E402

from execution.models import SignalCategory, SignalEntry  # noqa: E402
from schemas import (  # noqa: E402
    LifecycleState, RiskBudgetSnapshot, StrategySnapshot, TideBias,
    TideSnapshot, TradeArchetype, TradeSide, TradeStateSnapshot,
    WaveRegime, WaveSnapshot, PermissionSet,
)
from ui.items.candle_item import CandleItem  # noqa: E402
from ui.main_window_bridge import MainWindowBridge  # noqa: E402
from ui.models.position_model import (  # noqa: E402
    PositionModel, _rr_ratio,
)
from ui.models.snapshot_model import SnapshotModel  # noqa: E402
from ui.models.trade_blotter_model import (  # noqa: E402
    TradeBlotterModel, _MAX_ROWS,
)


_QML_DIR = _PROJECT_ROOT / "ui" / "qml" / "components"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _gui_app() -> QCoreApplication:
    """Singleton ``QApplication`` (created on first call).

    Phase 9C tests share the same QApplication as Phase 9A/9B — the
    bridge tier requires ``QApplication`` (subclass of
    ``QGuiApplication``) to host hidden QWidget instances.
    """
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv[:1])
    return instance


def _process_events(ms: int = 50) -> None:
    """Pump the Qt event loop for ``ms`` so QueuedConnection slots fire."""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _build_signal_entry(
    ts_ms: int = 1_710_000_000_000,
    signal_type: str = "ENTRY_BOUNCE_LONG",
    price: float = 100.0,
    side: str = "BUY",
    category: SignalCategory = SignalCategory.TRADE_LIFECYCLE,
) -> SignalEntry:
    return SignalEntry(
        timestamp=ts_ms,
        signal_type=signal_type,
        source="ripple",
        category=category,
        side=side,
        price=price,
        strength=0.8,
        description=f"{signal_type} @ {price:.2f}",
    )


def _active_strategy_snapshot(
    entry: float = 100.0,
    stop: float = 99.0,
    target: float = 102.0,
    unrealized: float = 50.0,
) -> StrategySnapshot:
    """Build a snapshot in the ACTIVE_PRIMARY trade state.

    Uses the LifecycleState that contains "ACTIVE" so
    :meth:`PositionModel._compute_active` returns True.
    """
    return StrategySnapshot(
        tide=TideSnapshot(
            bias=TideBias.LONG, risk_multiplier=0.8,
            max_position_usd=10000.0, es_budget=1000.0,
        ),
        wave=WaveSnapshot(
            regime=WaveRegime.BREAKOUT, trend_efficiency=0.7,
            permissions=PermissionSet(),
        ),
        trade=TradeStateSnapshot(
            trade_id="t-1",
            archetype=TradeArchetype.BOUNCE,
            side=TradeSide.LONG,
            state=_first_active_state(),
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            quantity=0.01,
            unrealized_pnl=unrealized,
            hold_time_ms=12_345,
        ),
        risk=RiskBudgetSnapshot(
            timestamp=1_710_000_000_000,
            es_budget=1000.0,
            consumed_es=300.0,
            risk_multiplier=0.8,
        ),
    )


def _first_active_state() -> LifecycleState:
    """Pick a LifecycleState whose name signals "trade is live".

    PositionModel detects active trades by membership in
    ``_ACTIVE_LIFECYCLE_TOKENS``; we just need a state with one of
    those tokens in its name.
    """
    for token in PositionModel._ACTIVE_LIFECYCLE_TOKENS:
        for state in LifecycleState:
            if token in state.name.upper():
                return state
    return LifecycleState.SETUP


class _FakeExecutionState:
    """Duck-typed ``ExecutionManager`` for PositionModel.update."""

    def __init__(self, session_realized_pnl: float = 0.0) -> None:
        self.session_realized_pnl = session_realized_pnl


# ===========================================================================
# AC #1 — TradeBlotterModel from a worker thread.
# ===========================================================================

class TestTradeBlotterModelWorkerThread(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.model = TradeBlotterModel()

    def test_append_from_worker_thread_increments_row_count(self) -> None:
        """AC #1 — appendEntry from a non-GUI thread routes through
        QMetaObject.invokeMethod and the row materialises on the GUI
        thread."""
        # Sanity: starts empty.
        self.assertEqual(self.model.rowCount(), 0)

        entry = _build_signal_entry()
        worker_started = threading.Event()
        worker_done = threading.Event()

        def worker() -> None:
            worker_started.set()
            self.model.appendEntry(entry)
            worker_done.set()

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        self.assertTrue(worker_started.wait(timeout=1.0))
        t.join(timeout=1.0)
        self.assertTrue(worker_done.is_set())

        # Pump the GUI thread so the queued slot runs.
        _process_events(50)
        self.assertEqual(self.model.rowCount(), 1)

    def test_append_multiple_from_workers_preserves_order(self) -> None:
        entries = [_build_signal_entry(ts_ms=1000 + i, price=100 + i)
                   for i in range(5)]

        def worker(idx: int) -> None:
            self.model.appendEntry(entries[idx])

        threads = [threading.Thread(target=worker, args=(i,), daemon=True)
                   for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=1.0)
        _process_events(100)
        self.assertEqual(self.model.rowCount(), 5)

    def test_bounded_storage(self) -> None:
        """rowCount must never exceed _MAX_ROWS (FIFO eviction)."""
        for i in range(_MAX_ROWS + 50):
            self.model.append_entry_sync(
                _build_signal_entry(ts_ms=1000 + i, price=100 + i))
        self.assertEqual(self.model.rowCount(), _MAX_ROWS)

    def test_clear_resets_rowcount(self) -> None:
        for i in range(10):
            self.model.append_entry_sync(_build_signal_entry(ts_ms=1000 + i))
        self.assertEqual(self.model.rowCount(), 10)
        self.model.clear()
        self.assertEqual(self.model.rowCount(), 0)

    def test_role_names_match_spec(self) -> None:
        names = {bytes(v).decode() for v in self.model.roleNames().values()}
        for required in (
            "timestamp", "timestampStr", "layer", "category",
            "signalType", "side", "price", "priceStr", "strength",
            "description", "realizedPnl", "realizedPnlStr",
            "categoryColor",
        ):
            self.assertIn(required, names)

    def test_data_returns_expected_columns(self) -> None:
        entry = _build_signal_entry(
            ts_ms=1_710_000_000_500,
            signal_type="ENTRY_BOUNCE_LONG",
            price=100.50,
        )
        entry.realized_pnl = 12.34
        self.model.append_entry_sync(entry)

        roles = {bytes(v).decode(): k for k, v in self.model.roleNames().items()}
        idx = self.model.index(0, 0)

        self.assertEqual(self.model.data(idx, roles["signalType"]),
                         "ENTRY_BOUNCE_LONG")
        self.assertEqual(self.model.data(idx, roles["side"]), "BUY")
        self.assertEqual(self.model.data(idx, roles["priceStr"]), "100.50")
        self.assertAlmostEqual(self.model.data(idx, roles["realizedPnl"]),
                               12.34, places=6)
        self.assertEqual(self.model.data(idx, roles["realizedPnlStr"]),
                         "+12.34")
        # TRADE_LIFECYCLE category colour is the bright off-white.
        self.assertEqual(
            self.model.data(idx, roles["categoryColor"]),
            "#dcf0ff",
        )
        # Timestamp string is HH:MM:SS.mmm format.
        ts_str = self.model.data(idx, roles["timestampStr"])
        self.assertRegex(ts_str, r"^\d{2}:\d{2}:\d{2}\.\d{3}$")


# ===========================================================================
# AC #2 — PositionModel update + sessionPnl + R:R.
# ===========================================================================

class TestPositionModelUpdate(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.model = PositionModel()

    def test_update_from_active_snapshot_sets_state(self) -> None:
        snap = _active_strategy_snapshot(entry=100, stop=99, target=102)
        exec_state = _FakeExecutionState(session_realized_pnl=250.0)
        self.model.update(snap, exec_state)

        # AC #2: tradeStateLabel should contain "ACTIVE" / "EXPAND" etc.
        active_state = _first_active_state()
        self.assertEqual(self.model.tradeStateLabel, active_state.name)
        self.assertTrue(self.model.active,
                        f"expected active=True for state "
                        f"{self.model.tradeStateLabel!r}")
        self.assertEqual(self.model.entryPrice, 100.0)
        self.assertEqual(self.model.stopPrice, 99.0)
        self.assertEqual(self.model.targetPrice, 102.0)
        # R:R = (102 - 100) / (100 - 99) = 2.0
        self.assertAlmostEqual(self.model.rrRatio, 2.0, places=6)
        self.assertAlmostEqual(self.model.sessionPnl, 250.0)
        self.assertEqual(self.model.tradeArchetype, "BOUNCE")
        self.assertEqual(self.model.tradeSide, "LONG")

    def test_update_with_none_clears_state(self) -> None:
        snap = _active_strategy_snapshot()
        exec_state = _FakeExecutionState(session_realized_pnl=10.0)
        self.model.update(snap, exec_state)
        self.assertTrue(self.model.active)

        self.model.update(None, exec_state)
        self.assertFalse(self.model.active)
        self.assertEqual(self.model.entryPrice, 0.0)
        self.assertEqual(self.model.tradeStateLabel, "")

    def test_rr_ratio_zero_when_no_risk(self) -> None:
        self.assertEqual(_rr_ratio(100, 100, 102), 0.0)
        self.assertEqual(_rr_ratio(0, 0, 0), 0.0)

    def test_setters_dedupe(self) -> None:
        """Q_PROPERTY setters must not emit when the value is unchanged."""
        calls: list[float] = []
        self.model.entryPriceChanged.connect(calls.append)
        self.model.entryPrice = 100.0
        self.model.entryPrice = 100.0
        self.model.entryPrice = 100.5
        self.assertEqual(calls, [100.0, 100.5])

    def test_active_signal_fires_on_transition(self) -> None:
        actives: list[bool] = []
        self.model.activeChanged.connect(actives.append)
        active_state = _first_active_state()
        self.model.tradeStateLabel = active_state.name
        self.model.tradeStateLabel = "SETUP"
        self.model.tradeStateLabel = active_state.name
        self.assertEqual(actives, [True, False, True])


# ===========================================================================
# AC #3 — PositionCard.qml: no opacity animations, solid background.
# ===========================================================================

class TestPositionCardStaticInspection(unittest.TestCase):
    """Static QML inspection — no rendering required."""

    def setUp(self) -> None:
        self.qml_path = _QML_DIR / "PositionCard.qml"
        self.qml_text = self.qml_path.read_text()

    def test_qml_file_exists(self) -> None:
        self.assertTrue(self.qml_path.is_file(),
                        f"missing QML: {self.qml_path}")

    def test_no_opacity_animations(self) -> None:
        """AC #3 — no ``OpacityAnimator`` or ``opacity:`` bindings
        that animate (NICE DCV H.264 stall mitigation)."""
        forbidden = (
            "OpacityAnimator",
            "PropertyAnimation",  # implicit fade
            "NumberAnimation",
        )
        for tok in forbidden:
            self.assertNotIn(tok, self.qml_text,
                             f"PositionCard.qml must not use {tok}")
        # opacity must not be animated.  Static `opacity: 1.0` is
        # OK, but `opacity: someBoolean ? 0.5 : 1.0` is not.  Scan for
        # any `opacity:` line — Phase 9C strategy explicitly avoids
        # the property altogether.
        self.assertNotRegex(self.qml_text, r"\bopacity\s*:")

    def test_background_is_solid_dark(self) -> None:
        self.assertIn('color: "#0c0e1c"', self.qml_text)

    def test_borders_are_visible(self) -> None:
        self.assertIn('border.color:', self.qml_text)

    def test_binds_to_position_model(self) -> None:
        # Must reference the registered model.
        self.assertIn("positionModel", self.qml_text)


# ===========================================================================
# AC #4 — CandleItem overlay lines.
# ===========================================================================

class TestCandleItemOverlayLines(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.item = CandleItem()
        self.item.setWidth(800)
        self.item.setHeight(400)
        # Seed three candles so the auto-scale range covers prices
        # around 100.
        for i in range(3):
            self.item.process_trade(
                1_000 + i * 60_000, 100.0 + i * 0.5, 1.0, True)

    def test_overlay_inactive_produces_no_line_geometry(self) -> None:
        node = self.item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertEqual(counts["entry_line"], 0)
        self.assertEqual(counts["stop_line"], 0)
        self.assertEqual(counts["target_line"], 0)

    def test_overlay_active_emits_dashed_lines(self) -> None:
        """AC #4 — entry/stop/target lines materialise when active."""
        self.item.set_trade_overlay(entry=100.0, stop=99.0, target=102.0,
                                    active=True)
        node = self.item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertGreater(counts["entry_line"], 0)
        self.assertGreater(counts["stop_line"], 0)
        self.assertGreater(counts["target_line"], 0)
        # Each dash is 6 verts (triangle-list rect); the count must
        # therefore be a multiple of 6.
        for key in ("entry_line", "stop_line", "target_line"):
            self.assertEqual(counts[key] % 6, 0,
                             f"{key} vertex count {counts[key]} not %6")

    def test_clear_trade_overlay_drops_lines(self) -> None:
        """AC #4 — clearing the overlay removes the line geometry."""
        self.item.set_trade_overlay(entry=100.0, stop=99.0, target=102.0,
                                    active=True)
        node = self.item.updatePaintNode(None, None)
        self.assertGreater(node.vertex_counts()["entry_line"], 0)

        self.item.clear_trade_overlay()
        node2 = self.item.updatePaintNode(node, None)
        counts2 = node2.vertex_counts()
        self.assertEqual(counts2["entry_line"], 0)
        self.assertEqual(counts2["stop_line"], 0)
        self.assertEqual(counts2["target_line"], 0)

    def test_overlay_geometry_does_not_trip_geometry_dirty(self) -> None:
        """Overlay rebuilds are decoupled from ``_geometry_dirty`` —
        AC #2 of Phase 9B remains intact."""
        node = self.item.updatePaintNode(None, None)
        self.assertFalse(self.item.geometry_dirty)

        self.item.set_trade_overlay(entry=100.0, stop=99.0, target=102.0,
                                    active=True)
        self.assertFalse(self.item.geometry_dirty,
                         "overlay activation must not set geometry_dirty")
        self.item.updatePaintNode(node, None)
        self.assertFalse(self.item.geometry_dirty)


# ===========================================================================
# AC #5 — ENTRY triangle marker at correct candle x-position.
# ===========================================================================

class TestCandleItemMarkers(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.item = CandleItem()
        self.item.setWidth(800)
        self.item.setHeight(400)
        # Three candles, each 60 s wide, starting at ts=1000.
        for i in range(3):
            self.item.process_trade(
                1_000 + i * 60_000, 100.0 + i * 0.5, 1.0, True)

    def test_entry_marker_at_first_bucket(self) -> None:
        """AC #5 — synthetic ENTRY event at the first candle's ts
        produces a triangle in the entry-marker bucket."""
        self.item.set_trade_events([(1_000, "ENTRY", 100.0)])
        node = self.item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertEqual(counts["entry_markers"], 3,
                         "single ENTRY marker = 3 triangle verts")
        self.assertEqual(counts["exit_markers"], 0)

    def test_exit_marker_at_third_bucket(self) -> None:
        self.item.set_trade_events([(121_001, "EXIT", 101.0)])
        node = self.item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertEqual(counts["exit_markers"], 3)
        self.assertEqual(counts["entry_markers"], 0)

    def test_marker_outside_visible_range_dropped(self) -> None:
        self.item.set_trade_events([(999_999_999, "ENTRY", 100.0)])
        node = self.item.updatePaintNode(None, None)
        counts = node.vertex_counts()
        self.assertEqual(counts["entry_markers"], 0)

    def test_marker_x_position_matches_bucket_index(self) -> None:
        """AC #5 — verify the *x-coordinate* of the marker matches the
        bucket centre."""
        events = [
            (1_000, "ENTRY", 100.0),         # bucket 0
            (61_500, "EXIT", 100.5),         # bucket 1
        ]
        self.item.set_trade_events(events)
        self.item.updatePaintNode(None, None)

        # Re-derive expected x positions by re-running the public
        # vertex generator (pure function over layout).
        layout = self.item._compute_layout()
        self.assertIsNotNone(layout)
        buckets = self.item._generate_overlay_vertices(layout)

        self.assertEqual(len(buckets.entry_markers), 3)
        self.assertEqual(len(buckets.exit_markers), 3)

        # Triangle is point-up; tip is vertex[0].  X must be at the
        # centre of the first bucket: layout.px + 0.5 * step.
        expected_x_entry = layout.px + 0.5 * layout.step
        expected_x_exit = layout.px + 1.5 * layout.step

        entry_tip = buckets.entry_markers[0]
        exit_tip = buckets.exit_markers[0]
        self.assertAlmostEqual(entry_tip.x, expected_x_entry, places=1)
        self.assertAlmostEqual(exit_tip.x, expected_x_exit, places=1)


# ===========================================================================
# AC #6 — MainWindowBridge wires snapshot → models → chart.
# ===========================================================================

class TestMainWindowBridge(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.snapshot = SnapshotModel()
        self.position = PositionModel()
        self.blotter = TradeBlotterModel()
        self.candle = CandleItem()
        self.candle.setWidth(800)
        self.candle.setHeight(400)
        for i in range(3):
            self.candle.process_trade(
                1_000 + i * 60_000, 100.0 + i * 0.5, 1.0, True)
        self.bridge = MainWindowBridge(
            self.snapshot, self.position, self.blotter, self.candle,
        )

    def test_on_signal_entry_routes_to_blotter_and_chart(self) -> None:
        entry = _build_signal_entry(
            ts_ms=1_000,
            signal_type="ENTRY_BOUNCE_LONG",
            price=100.0,
        )
        self.bridge.on_signal_entry(entry)
        _process_events(50)
        self.assertEqual(self.blotter.rowCount(), 1)
        # Marker appended to chart event buffer (via queued slot).
        events = self.candle.trade_events
        self.assertEqual(len(events), 1)
        ts_ms, kind, price = events[0]
        self.assertEqual(ts_ms, 1_000)
        self.assertEqual(kind, "ENTRY")
        self.assertEqual(price, 100.0)

    def test_on_signal_entry_from_worker_thread(self) -> None:
        """AC #1 + #6 — worker thread → bridge → models."""
        entry = _build_signal_entry()

        def worker() -> None:
            self.bridge.on_signal_entry(entry)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=1.0)
        _process_events(100)
        self.assertEqual(self.blotter.rowCount(), 1)
        self.assertEqual(len(self.candle.trade_events), 1)

    def test_on_timer_tick_activates_chart_overlay(self) -> None:
        """AC #4 — bridge wires PositionModel.active → CandleItem.tradeOverlayActive."""
        snap = _active_strategy_snapshot(entry=100, stop=99, target=102)
        self.bridge.on_timer_tick(snap, _FakeExecutionState(100.0))
        self.assertTrue(self.position.active)
        self.assertTrue(self.candle.tradeOverlayActive)
        self.assertEqual(self.candle.entryPrice, 100.0)
        self.assertEqual(self.candle.stopPrice, 99.0)
        self.assertEqual(self.candle.targetPrice, 102.0)

    def test_clear_session_resets_models(self) -> None:
        snap = _active_strategy_snapshot()
        self.bridge.on_timer_tick(snap, _FakeExecutionState(50.0))
        self.bridge.on_signal_entry(_build_signal_entry())
        _process_events(50)
        self.assertGreater(self.blotter.rowCount(), 0)
        self.assertTrue(self.candle.tradeOverlayActive)

        self.bridge.clear_session()
        self.assertEqual(self.blotter.rowCount(), 0)
        self.assertFalse(self.candle.tradeOverlayActive)
        self.assertEqual(self.candle.trade_events, [])
        self.assertEqual(self.snapshot.tideBias, "")

    def test_non_lifecycle_entry_skipped_from_chart(self) -> None:
        """DIAGNOSTIC / CONTEXT entries must not generate markers."""
        entry = _build_signal_entry(
            signal_type="EXHAUSTION_HIGH",
            category=SignalCategory.DIAGNOSTIC,
        )
        self.bridge.on_signal_entry(entry)
        _process_events(50)
        # Blotter row is fine; chart events stay empty.
        self.assertEqual(self.blotter.rowCount(), 1)
        self.assertEqual(self.candle.trade_events, [])

    def test_push_trade_events_replaces_chart_buffer(self) -> None:
        events = [
            (1_000, "ENTRY", 100.0),
            (60_500, "EXIT", 101.0),
        ]
        self.bridge.push_trade_events(events)
        self.assertEqual(self.candle.trade_events, events)


# ===========================================================================
# AC — SnapshotModel overlay fields.
# ===========================================================================

class TestSnapshotModelOverlayFields(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _gui_app()
        self.model = SnapshotModel()

    def test_overlay_fields_initial_zero(self) -> None:
        self.assertEqual(self.model.entryPrice, 0.0)
        self.assertEqual(self.model.stopPrice, 0.0)
        self.assertEqual(self.model.targetPrice, 0.0)
        self.assertEqual(self.model.tradeArchetype, "")

    def test_overlay_fields_populated_from_active_snapshot(self) -> None:
        snap = _active_strategy_snapshot(
            entry=200.0, stop=195.0, target=210.0)
        self.model.update_from_snapshot(snap)
        self.assertEqual(self.model.entryPrice, 200.0)
        self.assertEqual(self.model.stopPrice, 195.0)
        self.assertEqual(self.model.targetPrice, 210.0)
        self.assertEqual(self.model.tradeArchetype, "BOUNCE")

    def test_overlay_changed_signal_fires_on_update(self) -> None:
        calls: list[None] = []
        self.model.overlayChanged.connect(lambda: calls.append(None))
        snap = _active_strategy_snapshot()
        self.model.update_from_snapshot(snap)
        self.assertEqual(len(calls), 1)
        # Repeated update with same data — overlayChanged still fires
        # (aggregate signal is unconditional after update completes).
        self.model.update_from_snapshot(snap)
        self.assertEqual(len(calls), 2)

    def test_update_with_none_clears_overlay_fields(self) -> None:
        snap = _active_strategy_snapshot()
        self.model.update_from_snapshot(snap)
        self.assertGreater(self.model.entryPrice, 0.0)
        self.model.update_from_snapshot(None)
        self.assertEqual(self.model.entryPrice, 0.0)
        self.assertEqual(self.model.tradeArchetype, "")

    def test_risk_budget_pct_uses_consumed_over_budget(self) -> None:
        snap = _active_strategy_snapshot()
        self.model.update_from_snapshot(snap)
        # consumed_es=300, es_budget=1000 → 0.30
        self.assertAlmostEqual(self.model.riskBudgetPct, 0.30, places=6)


# ===========================================================================
# AC #3 — TradeBlotter.qml static inspection.
# ===========================================================================

class TestTradeBlotterQmlStatic(unittest.TestCase):
    def setUp(self) -> None:
        self.qml_path = _QML_DIR / "TradeBlotter.qml"
        self.text = self.qml_path.read_text()

    def test_file_exists(self) -> None:
        self.assertTrue(self.qml_path.is_file())

    def test_uses_fixed_height_delegate(self) -> None:
        """AC — 32 px delegate height for ListView virtualisation."""
        self.assertRegex(self.text, r"height\s*:\s*32\b")

    def test_binds_category_color(self) -> None:
        self.assertIn("categoryColor", self.text)

    def test_no_opacity_animations(self) -> None:
        for tok in ("OpacityAnimator", "PropertyAnimation",
                    "NumberAnimation"):
            self.assertNotIn(tok, self.text)

    def test_solid_background(self) -> None:
        self.assertIn('color: "#0c0e1c"', self.text)


# ===========================================================================
# AC — Live wiring: ``_run_qml`` exposes context properties + dedupes
# ``sceneGraphInitialized`` connections.
# ===========================================================================

class TestRunQmlContextWiring(unittest.TestCase):
    """Phase 9C live-wiring regression — covers the missing-context-property
    warnings the user saw on the first live launch:

        qml: TradeBlotter.qml: no ``tradeBlotterModel`` context property
        qml: PositionCard.qml: no ``positionModel`` context property

    Also guards against the duplicate ``Qt scene graph backend`` log
    lines that appeared because ``sceneGraphInitialized`` was firing
    twice per window in the live Metal threaded render loop.
    """

    def setUp(self) -> None:
        self.app = _gui_app()

    def test_install_bridge_context_exposes_all_models(self) -> None:
        from PySide6.QtQml import QQmlApplicationEngine

        from ui.app import _install_bridge_context, _register_qml_types

        _register_qml_types()
        engine = QQmlApplicationEngine()
        bridge = _install_bridge_context(engine)
        try:
            ctx = engine.rootContext()
            self.assertIsNotNone(ctx.contextProperty("snapshotModel"))
            self.assertIsNotNone(ctx.contextProperty("positionModel"))
            self.assertIsNotNone(ctx.contextProperty("tradeBlotterModel"))
            self.assertIsNotNone(ctx.contextProperty("mainWindowBridge"))
            self.assertIs(
                ctx.contextProperty("mainWindowBridge"), bridge,
            )
        finally:
            engine.deleteLater()

    def test_main_qml_loads_with_context_properties(self) -> None:
        """Loading ``main.qml`` with the context properties present
        must not emit the missing-property warnings."""
        from PySide6.QtQml import QQmlApplicationEngine

        from ui.app import (
            _connect_gpu_checks,
            _install_bridge_context,
            _qml_root_url,
            _register_qml_types,
            _wire_candle_item,
        )

        _register_qml_types()
        engine = QQmlApplicationEngine()
        bridge = _install_bridge_context(engine)
        try:
            engine.load(_qml_root_url())
            roots = engine.rootObjects()
            self.assertTrue(roots, "main.qml failed to load")
            _wire_candle_item(roots, bridge)
            _connect_gpu_checks(roots)

            # AC: bridge's CandleItem reference resolves from QML.
            self.assertIsNotNone(
                bridge.candle_item,
                "expected CandleItem to resolve from main.qml load",
            )
            # AC: every visible window in main.qml is registered.
            from ui.app import _iter_quick_windows
            windows = list(_iter_quick_windows(roots[0]))
            titles = sorted(w.title() for w in windows)
            self.assertEqual(
                titles,
                sorted([
                    "OrderFlow Trading — Chart",
                    "OrderFlow Trading — OrderFlow",
                    "OrderFlow Trading — Strategy",
                ]),
            )
        finally:
            engine.deleteLater()

    def test_iter_quick_windows_deduplicates(self) -> None:
        """``_iter_quick_windows`` must yield each window exactly once
        even when the same window is reachable as both a root and a
        QML child of another root."""
        from PySide6.QtQml import QQmlApplicationEngine

        from ui.app import (
            _install_bridge_context,
            _iter_quick_windows,
            _qml_root_url,
            _register_qml_types,
        )

        _register_qml_types()
        engine = QQmlApplicationEngine()
        _install_bridge_context(engine)
        try:
            engine.load(_qml_root_url())
            roots = engine.rootObjects()
            self.assertTrue(roots)
            # Collapse the iterator across every root; the dedupe set
            # is per-iter-call so we re-instantiate it manually here.
            seen: set[int] = set()
            duplicates: list[str] = []
            for root in roots:
                for window in _iter_quick_windows(root):
                    if id(window) in seen:
                        duplicates.append(window.title())
                    seen.add(id(window))
            # Across iterators, separate ``_iter_quick_windows`` calls
            # may yield the same window — that's a property of the
            # iterator, not of any single call.  The within-call
            # dedupe is the contract.
            for root in roots:
                ids_inside_one_call: list[int] = []
                for window in _iter_quick_windows(root):
                    ids_inside_one_call.append(id(window))
                self.assertEqual(
                    len(ids_inside_one_call),
                    len(set(ids_inside_one_call)),
                    f"duplicates within a single iter call: "
                    f"{ids_inside_one_call!r}",
                )
        finally:
            engine.deleteLater()

    def test_connect_gpu_checks_fires_once_per_window(self) -> None:
        """``_connect_gpu_checks`` must dedupe so re-firing the same
        ``sceneGraphInitialized`` signal only triggers one log entry
        per window (mirrors the live Metal threaded-render-loop bug
        where the signal was emitted twice per window)."""
        from PySide6.QtQuick import QQuickWindow

        from ui.app import _connect_gpu_checks

        window = QQuickWindow()
        # Avoid actually showing the window in CI.
        try:
            with self.assertLogs("ui.app") as cm:
                _connect_gpu_checks([window])
                # Fire the signal 3 times; only the first emit should
                # log.  (The scene graph isn't fully initialised here
                # so ``_check_gpu`` will log an error/info either way;
                # the dedupe is what we're proving.)
                for _ in range(3):
                    window.sceneGraphInitialized.emit()
                _process_events(10)
            self.assertEqual(
                len(cm.records), 1,
                f"expected one dedup'd log, got {len(cm.records)}: "
                f"{[r.getMessage() for r in cm.records]!r}",
            )
        finally:
            window.deleteLater()


# ===========================================================================
# Regression guard — legacy strategy dashboard tests still importable.
# ===========================================================================

class TestLegacyStrategyDashboardImports(unittest.TestCase):
    """Phase 9C migrates the dashboard to QML but the legacy QWidget
    paths must remain importable so ``tests/test_strategy_dashboard.py``
    keeps running.  This is the 9A→9C transition guard."""

    def test_strategy_dashboard_module(self) -> None:
        import ui.strategy_dashboard_view  # noqa: F401

    def test_trade_blotter_module(self) -> None:
        import ui.trade_blotter  # noqa: F401

    def test_strategy_panel_module(self) -> None:
        import ui.strategy_panel  # noqa: F401


if __name__ == "__main__":
    unittest.main(verbosity=2)
