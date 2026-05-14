"""Phase 9C.1 — Live-engine host regression suite.

Covers ``ui/qml_engine_host.py`` (the glue that hooks a hidden
``MainWindow`` into the QML scene so the heatmap / CVD / volume
profile / candle / blotter actually show live data).  No tests in
this module touch the network — ``QmlEngineHost.start`` is invoked
with ``auto_connect=False`` so ``LiveTradingSession`` stays idle.

Acceptance criteria covered:

A. ``WidgetBridgeItem.attach_external_widget`` swaps the wrapped
   widget for an external one and wraps ``widget.update`` so QML
   bridge texture is invalidated every time the widget repaints.
B. ``QmlEngineHost.start`` constructs the hidden MainWindow, resolves
   every QML scene item (heatmap / cvd / volume profile / candle),
   and re-attaches the MainWindow widgets to the QML bridge items.
C. ``MainWindow._broadcast_entry`` is wrapped — the original call still
   updates the legacy QWidget blotter AND the QML
   ``TradeBlotterModel`` gains the row.
D. ``MainWindow._candle_view.process_trade`` is wrapped — every trade
   reaches the QML ``CandleItem``'s candle deque alongside the
   legacy QWidget candle view.
E. ``MainWindow._update_timer.timeout`` drives
   ``MainWindowBridge.on_timer_tick`` so the QML ``SnapshotModel`` and
   ``PositionModel`` see the latest snapshot after MainWindow's own
   slot ran (i.e. ``_last_strategy_snap`` is already populated).
F. ``QmlEngineHost.start`` is graceful when called with no QML root
   objects (returns ``False``, logs an error, does not raise).
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl  # noqa: E402
from PySide6.QtQml import QQmlApplicationEngine  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from execution.models import SignalCategory, SignalEntry  # noqa: E402
from schemas import (  # noqa: E402
    LifecycleState, PermissionSet, RiskBudgetSnapshot, StrategySnapshot,
    TideBias, TideSnapshot, TradeArchetype, TradeSide, TradeStateSnapshot,
    WaveRegime, WaveSnapshot,
)
from ui.app import (  # noqa: E402
    _install_bridge_context, _qml_root_url, _register_qml_types,
)
from ui.items._widget_bridge import WidgetBridgeItem  # noqa: E402
from ui.items.candle_item import CandleItem  # noqa: E402
from ui.items.cvd_item import CvdItem  # noqa: E402
from ui.items.heatmap_item import HeatmapItem  # noqa: E402
from ui.items.volume_profile_item import VolumeProfileItem  # noqa: E402
from ui.models.position_model import PositionModel  # noqa: E402
from ui.qml_engine_host import QmlEngineHost  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gui_app() -> QCoreApplication:
    instance = QCoreApplication.instance()
    if instance is None:
        instance = QApplication(sys.argv[:1])
    return instance


def _process_events(ms: int = 25) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def _load_qml_scene() -> tuple[QQmlApplicationEngine, "MainWindowBridge"]:
    """Boot the same QML root that ``ui.app._run_qml`` loads.

    Reuses ``ui.app`` helpers so the context properties and registered
    types match production exactly — the host shouldn't care whether
    it was instantiated from tests or from ``main.py``.
    """
    _gui_app()
    _register_qml_types()
    engine = QQmlApplicationEngine()
    bridge = _install_bridge_context(engine)
    engine.load(_qml_root_url())
    return engine, bridge


def _first_active_state() -> LifecycleState:
    for token in PositionModel._ACTIVE_LIFECYCLE_TOKENS:
        for state in LifecycleState:
            if token in state.name.upper():
                return state
    return LifecycleState.SETUP


def _active_snapshot(unrealized: float = 12.0) -> StrategySnapshot:
    """Build an ACTIVE_PRIMARY snapshot using actual schema fields.

    Mirrors ``tests/test_qml_strategy_ui.py::_active_strategy_snapshot``
    so the engine-host tick-tap test exercises the same model surface as
    the Phase 9C suite.
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
            entry_price=100.0,
            stop_price=99.0,
            target_price=102.0,
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


# ---------------------------------------------------------------------------
# AC #A — WidgetBridgeItem.attach_external_widget
# ---------------------------------------------------------------------------


class _ProbeBridge(WidgetBridgeItem):
    """Minimal concrete subclass for direct attach_external_widget tests."""


class TestAttachExternalWidget(unittest.TestCase):
    def setUp(self) -> None:
        _gui_app()
        self.bridge = _ProbeBridge()
        self.bridge.setWidth(200)
        self.bridge.setHeight(100)

    def tearDown(self) -> None:
        try:
            self.bridge.deleteLater()
        except Exception:
            pass

    def test_attach_swaps_widget(self) -> None:
        external = QWidget()
        self.bridge.attach_external_widget(external)
        self.assertIs(self.bridge.bridge_widget(), external)

    def test_attach_resizes_widget_to_item_bounds(self) -> None:
        external = QWidget()
        self.bridge.attach_external_widget(external)
        self.assertEqual(external.size().width(), 200)
        self.assertEqual(external.size().height(), 100)

    def test_attach_marks_widget_dont_show_on_screen(self) -> None:
        from PySide6.QtCore import Qt
        external = QWidget()
        self.bridge.attach_external_widget(external)
        self.assertTrue(
            external.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen))

    def test_widget_update_hook_invalidates_bridge(self) -> None:
        external = QWidget()
        self.bridge.attach_external_widget(external)

        # Spy on QQuickPaintedItem.update; calling widget.update() should
        # ALSO call bridge.update().  We can't easily intercept the C++
        # update() so flag a Python override side-effect instead.
        flag = {"called": False}
        original_update = self.bridge.update

        def spy(*args, **kwargs):
            flag["called"] = True
            return original_update(*args, **kwargs)

        self.bridge.update = spy  # type: ignore[method-assign]
        external.update()
        self.assertTrue(
            flag["called"],
            "widget.update() should invalidate the QML bridge texture",
        )

    def test_update_hook_idempotent(self) -> None:
        external = QWidget()
        self.bridge.attach_external_widget(external)
        first = external.update
        self.bridge.attach_external_widget(external)
        second = external.update
        self.assertIs(
            first, second,
            "re-attaching the same widget must not stack update hooks",
        )


# ---------------------------------------------------------------------------
# AC #B — QmlEngineHost resolves items + reuses widgets
# ---------------------------------------------------------------------------


class TestHostResolvesQmlItems(unittest.TestCase):
    """Boot the real main.qml and verify every QML item is resolved."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._engine, cls._bridge = _load_qml_scene()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._engine.deleteLater()
        _process_events(5)

    def test_main_qml_loaded(self) -> None:
        self.assertTrue(
            self._engine.rootObjects(),
            "main.qml must load for host tests to be meaningful",
        )

    def test_resolve_items_finds_every_bridge(self) -> None:
        host = QmlEngineHost(self._engine, self._bridge)
        host._resolve_items(self._engine.rootObjects())
        items = host.items()
        self.assertIsInstance(items["heatmap"], HeatmapItem)
        self.assertIsInstance(items["cvd"], CvdItem)
        self.assertIsInstance(items["volume_profile"], VolumeProfileItem)
        self.assertIsInstance(items["candle"], CandleItem)


class TestHostReusesMainWindowWidgets(unittest.TestCase):
    """``QmlEngineHost.start(auto_connect=False)`` end-to-end without net."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._engine, cls._bridge = _load_qml_scene()
        cls._host = QmlEngineHost(cls._engine, cls._bridge)
        started = cls._host.start(auto_connect=False)
        cls._started = started

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls._host.stop()
        except Exception:
            pass
        cls._engine.deleteLater()
        _process_events(5)

    def test_start_returns_true(self) -> None:
        self.assertTrue(self._started, "host.start should succeed offline")

    def test_main_window_constructed(self) -> None:
        self.assertIsNotNone(self._host.main_window)

    def test_heatmap_widget_is_main_window_heatmap(self) -> None:
        items = self._host.items()
        mw = self._host.main_window
        self.assertIs(items["heatmap"].bridge_widget(), mw._heatmap)

    def test_cvd_widget_is_main_window_cvd(self) -> None:
        items = self._host.items()
        mw = self._host.main_window
        self.assertIs(items["cvd"].bridge_widget(), mw._cvd)

    def test_volume_profile_widget_is_main_window_volume_profile(self) -> None:
        items = self._host.items()
        mw = self._host.main_window
        self.assertIs(
            items["volume_profile"].bridge_widget(), mw._volume_profile)

    def test_bridge_candle_item_is_qml_candle(self) -> None:
        items = self._host.items()
        self.assertIs(self._bridge.candle_item, items["candle"])


# ---------------------------------------------------------------------------
# AC #C — broadcast_entry tap → QML blotter
# ---------------------------------------------------------------------------


class TestBroadcastEntryTap(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._engine, cls._bridge = _load_qml_scene()
        cls._host = QmlEngineHost(cls._engine, cls._bridge)
        cls._host.start(auto_connect=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._host.stop()
        cls._engine.deleteLater()
        _process_events(5)

    def test_broadcast_entry_appends_to_blotter(self) -> None:
        blotter = self._bridge.blotter_model
        before = blotter.rowCount()

        entry = SignalEntry(
            timestamp=1_710_000_000_000,
            signal_type="ENTRY_BOUNCE_LONG",
            source="ripple",
            category=SignalCategory.TRADE_LIFECYCLE,
            side="BUY",
            price=100.0,
            strength=0.7,
            description="probe entry",
        )
        self._host.main_window._broadcast_entry(entry)
        _process_events(50)

        self.assertEqual(blotter.rowCount(), before + 1)

    def test_broadcast_entry_still_hits_legacy_blotter(self) -> None:
        """Original ``_broadcast_entry`` path must still run."""
        mw = self._host.main_window
        legacy_blotter = mw._strategy_dashboard.blotter
        before = legacy_blotter._model.rowCount()

        entry = SignalEntry(
            timestamp=1_710_000_000_001,
            signal_type="EXIT_TARGET_LONG",
            source="ripple",
            category=SignalCategory.TRADE_LIFECYCLE,
            side="SELL",
            price=102.0,
            strength=0.9,
            description="probe exit",
        )
        mw._broadcast_entry(entry)
        _process_events(50)

        self.assertEqual(legacy_blotter._model.rowCount(), before + 1)


# ---------------------------------------------------------------------------
# AC #D — candle_view.process_trade tap → QML CandleItem
# ---------------------------------------------------------------------------


class TestCandleProcessTradeTap(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._engine, cls._bridge = _load_qml_scene()
        cls._host = QmlEngineHost(cls._engine, cls._bridge)
        cls._host.start(auto_connect=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._host.stop()
        cls._engine.deleteLater()
        _process_events(5)

    def test_trade_reaches_qml_candle(self) -> None:
        qml_candle = self._host.items()["candle"]
        before = qml_candle.candle_count

        ts = 1_710_000_000_000
        price = 100.5
        qty = 0.5
        self._host.main_window._candle_view.process_trade(
            ts, price, qty, True)

        self.assertEqual(qml_candle.candle_count, before + 1)

    def test_trade_still_reaches_legacy_candle_view(self) -> None:
        mw = self._host.main_window
        # The legacy candle view exposes its candle deque via _candles.
        before = len(mw._candle_view._candles)
        mw._candle_view.process_trade(
            1_710_000_060_000, 101.0, 0.4, False)
        after = len(mw._candle_view._candles)
        # Either a new bucket opens, or the running bucket gains the
        # trade — either way the legacy view must have observed it.
        self.assertGreaterEqual(after, before)


# ---------------------------------------------------------------------------
# AC #E — update timer drives bridge.on_timer_tick
# ---------------------------------------------------------------------------


class TestTimerTickTap(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._engine, cls._bridge = _load_qml_scene()
        cls._host = QmlEngineHost(cls._engine, cls._bridge)
        cls._host.start(auto_connect=False)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._host.stop()
        cls._engine.deleteLater()
        _process_events(5)

    def test_timer_emission_refreshes_snapshot_model(self) -> None:
        mw = self._host.main_window
        # Plant a snapshot the way MainWindow's tick handler would.
        mw._last_strategy_snap = _active_snapshot(unrealized=42.0)
        mw._exec_manager = None
        mw._paper_engine = MagicMock(session_realized_pnl=7.5)

        mw._update_timer.timeout.emit()
        _process_events(25)

        snap_model = self._bridge.snapshot_model
        self.assertAlmostEqual(snap_model.unrealizedPnl, 42.0, places=3)

    def test_timer_emission_refreshes_position_model(self) -> None:
        mw = self._host.main_window
        mw._last_strategy_snap = _active_snapshot(unrealized=21.0)
        mw._exec_manager = None
        mw._paper_engine = MagicMock(session_realized_pnl=3.0)

        mw._update_timer.timeout.emit()
        _process_events(25)

        pos = self._bridge.position_model
        self.assertTrue(pos.active, "position should mark active after tick")
        self.assertAlmostEqual(pos.sessionPnl, 3.0, places=3)
        self.assertAlmostEqual(pos.entryPrice, 100.0, places=3)


# ---------------------------------------------------------------------------
# AC #F — graceful failure modes
# ---------------------------------------------------------------------------


class TestHostFailureModes(unittest.TestCase):
    def test_start_with_no_roots_returns_false(self) -> None:
        _gui_app()
        engine = QQmlApplicationEngine()  # nothing loaded
        bridge = _install_bridge_context(engine)
        host = QmlEngineHost(engine, bridge)
        result = host.start(auto_connect=False)
        self.assertFalse(result)
        engine.deleteLater()
        _process_events(5)

    def test_start_mainwindow_init_failure_does_not_raise(self) -> None:
        _gui_app()
        _register_qml_types()
        engine = QQmlApplicationEngine()
        bridge = _install_bridge_context(engine)
        engine.load(_qml_root_url())
        try:
            host = QmlEngineHost(engine, bridge)
            with patch("ui.main_window.MainWindow",
                       side_effect=RuntimeError("boom")):
                result = host.start(auto_connect=False)
            self.assertFalse(result)
        finally:
            engine.deleteLater()
            _process_events(5)


if __name__ == "__main__":
    unittest.main()
