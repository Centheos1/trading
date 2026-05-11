import sys
import os
import logging
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QToolBar, QComboBox, QPushButton, QLabel, QLineEdit,
    QStatusBar, QMessageBox,
)
from PySide6.QtCore import Qt, QTimer, Signal as QtSignal, QSettings
from PySide6.QtGui import QFont, QAction, QPainter, QColor, QPen

from ui.heatmap_widget import HeatmapWidget
from ui.live_trading_session import LiveTradingSession
from ui.orderflow_viewmodel import OrderFlowViewModel
from ui.volume_profile_widget import VolumeProfileWidget
from ui.cvd_widget import CVDWidget
from ui.market_state import MarketState
from ui.candle_chart_view import CandleChartView, TIMEFRAMES as CHART_TIMEFRAMES
from ui.strategy_dashboard_view import StrategyDashboardView
from execution.models import (
    RippleMode, SignalCategory, SignalEntry,
    SizingConfig, SizingMode, StrategyMode, StrategyUIState,
    SuppressionReason,
    intent_risk_block_reason,
    ripple_decision_to_entry, ripple_decision_to_intent,
    _parse_intent_name,
)
from execution.binance_broker import BinanceBroker
from execution.execution_manager import ExecutionManager
from execution.paper_engine import PaperEngine
from strategy_store import StrategyStore

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
except ImportError:
    ofe = None
    logger.warning("orderflow_engine not available")

try:
    import websockets
except ImportError:
    websockets = None
    logger.warning("websockets not installed — pip install websockets")

# ---------------------------------------------------------------------------
# Suppression / cooldown tracker for Ripple decisions
# ---------------------------------------------------------------------------

@dataclass
class _SuppressionMetrics:
    emitted: int = 0
    cooldown_suppressed: int = 0
    dedupe_suppressed: int = 0
    mode_suppressed: int = 0
    confidence_suppressed: int = 0
    inventory_suppressed: int = 0

    @property
    def total_suppressed(self) -> int:
        return (self.cooldown_suppressed + self.dedupe_suppressed +
                self.mode_suppressed + self.confidence_suppressed +
                self.inventory_suppressed)


class _RippleCooldown:
    """Per-intent-type cooldown and same-price-zone deduplication."""

    def __init__(self, cooldown_ms: int = 5000, price_tolerance: float = 0.5):
        self._cooldown_ms = cooldown_ms
        self._price_tol = price_tolerance
        self._last_emit: dict[str, tuple[int, float]] = {}

    def check(self, intent_name: str, ts: int, price: float) -> SuppressionReason:
        prev = self._last_emit.get(intent_name)
        if prev is None:
            return SuppressionReason.NONE
        prev_ts, prev_price = prev
        if ts - prev_ts < self._cooldown_ms:
            if abs(price - prev_price) < self._price_tol:
                return SuppressionReason.DEDUPE
            return SuppressionReason.COOLDOWN
        return SuppressionReason.NONE

    def record(self, intent_name: str, ts: int, price: float) -> None:
        self._last_emit[intent_name] = (ts, price)


# ---------------------------------------------------------------------------
# Compact status / health panel (sits below Volume Profile)
# ---------------------------------------------------------------------------

class _StatusPanel(QWidget):
    """Compact panel showing per-pipeline connection and health status."""

    _BG = QColor(12, 14, 28)
    _BORDER = QColor(25, 28, 45)
    _LABEL_COLOR = QColor(80, 85, 105)
    _VALUE_COLORS = {
        "live":     QColor(0, 204, 102),
        "ok":       QColor(0, 204, 102),
        "synced":   QColor(0, 204, 102),
        "stale":    QColor(255, 170, 0),
        "cross":    QColor(255, 170, 0),
        "stalled":  QColor(255, 170, 0),
        "recon":    QColor(255, 102, 0),
        "empty":    QColor(255, 51, 51),
        "failed":   QColor(255, 51, 51),
        "invalid":  QColor(255, 51, 51),
        "off":      QColor(80, 85, 105),
        "?":        QColor(80, 85, 105),
    }
    _DEFAULT_COLOR = QColor(80, 85, 105)

    _FONT_TITLE = None
    _FONT_ROW = None
    _BORDER_PEN = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(80)
        self._rows: list[tuple[str, str]] = []
        self._title = "Status"
        if _StatusPanel._FONT_TITLE is None:
            f = QFont("Menlo", 8)
            f.setBold(True)
            _StatusPanel._FONT_TITLE = f
            _StatusPanel._FONT_ROW = QFont("Menlo", 8)
            _StatusPanel._BORDER_PEN = QPen(self._BORDER, 1)

    def set_health(self, rows: list[tuple[str, str]], title: str = "Status"):
        """Update the displayed rows.  Each row is (label, status_key)."""
        if rows != self._rows or title != self._title:
            self._rows = rows
            self._title = title
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._BG)

        w = self.width()
        h = self.height()

        painter.setPen(self._BORDER_PEN)
        painter.drawLine(0, 0, w, 0)

        y = 8
        painter.setFont(self._FONT_TITLE)
        painter.setPen(self._LABEL_COLOR)
        painter.drawText(8, y + 10, self._title)
        y += 18

        painter.setFont(self._FONT_ROW)
        line_h = 15
        for label, status in self._rows:
            if y + line_h > h:
                break
            painter.setPen(self._LABEL_COLOR)
            painter.drawText(8, y + 10, label)

            key = status.lower().strip()
            color = self._VALUE_COLORS.get(key, self._DEFAULT_COLOR)
            painter.setPen(color)
            painter.drawText(w - 62, y + 10, status)
            y += line_h

        painter.end()


# ---------------------------------------------------------------------------
# Detached View Window
# ---------------------------------------------------------------------------

class _DetachedViewWindow(QMainWindow):
    """Top-level window wrapper for a detached view widget.

    Each view (Candle Chart, Strategy Dashboard) lives in its own
    OS-level window so the user can drag them to different monitors.
    Closing a detached window hides it rather than destroying it;
    it can be re-shown from the View toolbar buttons.
    """

    visibility_changed = QtSignal(bool)

    _STYLESHEET = """
        QMainWindow { background-color: #0f0f19; }
    """

    def __init__(self, view_widget: QWidget, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setCentralWidget(view_widget)
        self.setStyleSheet(self._STYLESHEET)
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)

    def closeEvent(self, event):
        self.hide()
        self.visibility_changed.emit(False)
        event.ignore()


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    _new_signal = QtSignal(object)
    _new_order = QtSignal(object)
    _new_ripple = QtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Order Flow Trading - Auction Market Theory")
        self.setMinimumSize(1200, 800)
        self.resize(1600, 1000)

        self._exec_manager: ExecutionManager | None = None
        self._paper_engine: PaperEngine | None = None
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._on_timer_tick)
        self._update_interval_ms = 100

        self._signal_buffer = []
        self._candle_duration_ms = 60_000
        self._signal_count = 0
        self._new_signal.connect(self._on_signal_received)
        self._new_order.connect(self._on_order_received)
        self._new_ripple.connect(self._on_ripple_received)

        self._ripple_mode = RippleMode.LOG_ONLY
        self._ripple_cooldown = _RippleCooldown()
        self._ripple_metrics = _SuppressionMetrics()

        self._strategy_mode = StrategyMode.OBSERVE
        self._strategy_ui_state = StrategyUIState.DISARMED
        self._prev_strategy_ui_state = StrategyUIState.DISARMED
        self._last_strategy_snap = None
        self._strategy_store: Optional[StrategyStore] = None
        self._ripple_min_confidence = 0.0
        self._is_replay_mode = False

        self._settings = QSettings("OrderFlowTrading", "MainWindow")
        self._load_strategy_settings()

        self._market_state = MarketState(
            bucket_duration_ms=self._candle_duration_ms,
            visible_window_ms=self._candle_duration_ms,
        )

        self._setup_ui()
        # Session before toolbar/statusbar: they call _update_arm_button_style → _engine.
        self._websockets_module = websockets
        self._session = LiveTradingSession(self)
        self._setup_toolbar()
        self._setup_statusbar()
        self._apply_stylesheet()

    @property
    def _engine(self):
        """OrderFlowEngine while connected; owned by ``LiveTradingSession``."""
        s = getattr(self, "_session", None)
        return None if s is None else s.engine

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    _RIPPLE_TO_STRATEGY = {
        "log_only": StrategyMode.OBSERVE,
        "paper": StrategyMode.PAPER,
        "disabled": StrategyMode.OBSERVE,
    }

    def _load_strategy_settings(self):
        mode_str = self._settings.value("strategy/mode", "")
        if mode_str:
            try:
                self._strategy_mode = StrategyMode(mode_str)
            except ValueError:
                self._strategy_mode = StrategyMode.OBSERVE
        else:
            old = self._settings.value("ripple/mode", "log_only")
            self._strategy_mode = self._RIPPLE_TO_STRATEGY.get(
                old, StrategyMode.OBSERVE)

        self._ripple_mode = {
            StrategyMode.OBSERVE: RippleMode.LOG_ONLY,
            StrategyMode.PAPER: RippleMode.PAPER,
            StrategyMode.LIVE: RippleMode.LOG_ONLY,
        }.get(self._strategy_mode, RippleMode.LOG_ONLY)

        self._ripple_min_confidence = float(
            self._settings.value("ripple/min_confidence", 0.0))

    def _save_strategy_settings(self):
        self._settings.setValue("strategy/mode", self._strategy_mode.value)
        self._settings.setValue("ripple/min_confidence",
                                self._ripple_min_confidence)

    # ------------------------------------------------------------------
    # UI layout
    # ------------------------------------------------------------------

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(2, 2, 2, 2)
        main_layout.setSpacing(2)

        # ============ Order Flow (primary / main window) ================
        top_splitter = QSplitter(Qt.Horizontal)

        self._left_col = QSplitter(Qt.Vertical)
        self._volume_profile = VolumeProfileWidget()
        self._status_panel = _StatusPanel()
        self._left_col.addWidget(self._volume_profile)
        self._left_col.addWidget(self._status_panel)
        self._left_col.setStretchFactor(0, 3)
        self._left_col.setStretchFactor(1, 1)
        self._left_col.setChildrenCollapsible(False)

        self._chart_stack = QSplitter(Qt.Vertical)
        self._orderflow_vm = OrderFlowViewModel(
            candle_store=self._market_state.candles,
        )
        self._heatmap = HeatmapWidget(viewmodel=self._orderflow_vm)
        self._cvd = CVDWidget()
        self._chart_stack.addWidget(self._heatmap)
        self._chart_stack.addWidget(self._cvd)
        self._chart_stack.setStretchFactor(0, 3)
        self._chart_stack.setStretchFactor(1, 1)

        self._syncing_splitters = False
        self._chart_stack.splitterMoved.connect(self._sync_left_to_chart)
        self._left_col.splitterMoved.connect(self._sync_chart_to_left)

        top_splitter.addWidget(self._left_col)
        top_splitter.addWidget(self._chart_stack)
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 4)

        main_layout.addWidget(top_splitter)

        # ============ Detached windows ===================================
        self._candle_view = CandleChartView(self._market_state)
        self._candle_window = _DetachedViewWindow(
            self._candle_view, "Chart", self)
        self._candle_window.resize(1000, 620)
        self._candle_window.visibility_changed.connect(
            lambda vis: self._show_chart_btn.setChecked(vis))

        chart_tb = QToolBar("Chart Controls")
        chart_tb.setMovable(False)
        chart_tb.setStyleSheet(
            "QToolBar { background: #14162a; border-bottom: 1px solid #22243a; "
            "spacing: 4px; padding: 2px 4px; }"
            "QLabel { color: #8888aa; font-family: Menlo; font-size: 10px; }"
            "QComboBox { background: #0f0f19; color: #b4b4c8; "
            "border: 1px solid #282838; padding: 2px 6px; "
            "font-family: Menlo; font-size: 10px; }")
        chart_tb.addWidget(QLabel(" Timeframe: "))
        self._chart_tf_combo = QComboBox()
        for label, ms in CHART_TIMEFRAMES:
            self._chart_tf_combo.addItem(label, ms)
        self._chart_tf_combo.setCurrentIndex(0)
        self._chart_tf_combo.setMaximumWidth(80)
        self._chart_tf_combo.currentIndexChanged.connect(self._on_chart_tf_changed)
        chart_tb.addWidget(self._chart_tf_combo)
        self._candle_window.addToolBar(chart_tb)

        self._strategy_dashboard = StrategyDashboardView(self._market_state)
        self._strategy_window = _DetachedViewWindow(
            self._strategy_dashboard, "Strategy", self)
        self._strategy_window.resize(900, 600)
        self._strategy_window.visibility_changed.connect(
            lambda vis: self._show_strategy_btn.setChecked(vis))

    def _sync_left_to_chart(self, pos, idx):
        """Mirror the chart_stack split position to the left column."""
        if self._syncing_splitters:
            return
        self._syncing_splitters = True
        try:
            self._left_col.setSizes(self._chart_stack.sizes())
        finally:
            self._syncing_splitters = False

    def _sync_chart_to_left(self, pos, idx):
        """Mirror the left column split position to the chart stack."""
        if self._syncing_splitters:
            return
        self._syncing_splitters = True
        try:
            self._chart_stack.setSizes(self._left_col.sizes())
        finally:
            self._syncing_splitters = False

    def _setup_toolbar(self):
        toolbar = QToolBar("Controls")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        toolbar.addWidget(QLabel(" Symbol: "))
        self._symbol_input = QLineEdit("BTCUSDT")
        self._symbol_input.setMaximumWidth(120)
        self._symbol_input.setFont(QFont("Menlo", 10))
        toolbar.addWidget(self._symbol_input)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Mode: "))
        self._mode_combo = QComboBox()
        self._mode_combo.addItem("Live")
        self._mode_combo.addItem("Replay")
        self._mode_combo.setMaximumWidth(100)
        replay_item = self._mode_combo.model().item(1)
        replay_item.setEnabled(False)
        replay_item.setToolTip("Coming soon")
        toolbar.addWidget(self._mode_combo)

        toolbar.addSeparator()

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect)
        toolbar.addWidget(self._connect_btn)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        self._disconnect_btn.setEnabled(False)
        toolbar.addWidget(self._disconnect_btn)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" VP Window: "))
        self._vp_window_combo = QComboBox()
        self._vp_window_combo.addItem("Session", 0)
        self._vp_window_combo.addItem("1 min", 60_000)
        self._vp_window_combo.addItem("5 min", 300_000)
        self._vp_window_combo.addItem("15 min", 900_000)
        self._vp_window_combo.addItem("30 min", 1_800_000)
        self._vp_window_combo.addItem("1 hr", 3_600_000)
        self._vp_window_combo.setCurrentIndex(2)
        self._vp_window_combo.setMaximumWidth(100)
        self._vp_window_combo.currentIndexChanged.connect(self._on_vp_window_changed)
        toolbar.addWidget(self._vp_window_combo)

        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Candle: "))
        self._candle_combo = QComboBox()
        self._candle_combo.addItem("1 min", 60_000)
        self._candle_combo.addItem("5 min", 300_000)
        self._candle_combo.addItem("15 min", 900_000)
        self._candle_combo.addItem("30 min", 1_800_000)
        self._candle_combo.addItem("1 hr", 3_600_000)
        self._candle_combo.setCurrentIndex(0)
        self._candle_combo.setMaximumWidth(90)
        self._candle_combo.currentIndexChanged.connect(self._on_candle_changed)
        toolbar.addWidget(self._candle_combo)

        toolbar.addSeparator()

        self._tick_size_label = QLabel(" Tick Size: ")
        self._tick_size_label.setVisible(False)
        toolbar.addWidget(self._tick_size_label)
        self._tick_size_input = QLineEdit("0.01")
        self._tick_size_input.setMaximumWidth(80)
        self._tick_size_input.setVisible(False)
        toolbar.addWidget(self._tick_size_input)

        self._imbalance_label = QLabel(" Imbalance: ")
        self._imbalance_label.setVisible(False)
        toolbar.addWidget(self._imbalance_label)
        self._imbalance_input = QLineEdit("3.0")
        self._imbalance_input.setMaximumWidth(60)
        self._imbalance_input.setVisible(False)
        toolbar.addWidget(self._imbalance_input)

        toolbar.addSeparator()

        self._sizing_label = QLabel(" Sizing: ")
        toolbar.addWidget(self._sizing_label)
        self._sizing_mode_combo = QComboBox()
        self._sizing_mode_combo.addItem("Fixed Qty", SizingMode.FIXED_QTY.value)
        self._sizing_mode_combo.addItem("Fixed $", SizingMode.FIXED_NOTIONAL.value)
        self._sizing_mode_combo.addItem("% Balance", SizingMode.PCT_BALANCE.value)
        self._sizing_mode_combo.setCurrentIndex(2)
        self._sizing_mode_combo.setMaximumWidth(100)
        toolbar.addWidget(self._sizing_mode_combo)

        self._sizing_value_input = QLineEdit("1")
        self._sizing_value_input.setMaximumWidth(70)
        toolbar.addWidget(self._sizing_value_input)

        self._update_sizing_enabled()

        toolbar.addSeparator()

        # --- Unified strategy controls (Phase 2) ---
        toolbar.addWidget(QLabel(" Strategy: "))
        self._strategy_mode_combo = QComboBox()
        self._strategy_mode_combo.addItem("Observe", StrategyMode.OBSERVE.value)
        self._strategy_mode_combo.addItem("Paper", StrategyMode.PAPER.value)
        self._strategy_mode_combo.addItem("Live", StrategyMode.LIVE.value)
        self._strategy_mode_combo.setMaximumWidth(100)
        idx = self._strategy_mode_combo.findData(self._strategy_mode.value)
        if idx >= 0:
            self._strategy_mode_combo.setCurrentIndex(idx)
        self._strategy_mode_combo.currentIndexChanged.connect(
            self._on_strategy_mode_changed)
        toolbar.addWidget(self._strategy_mode_combo)

        self._arm_btn = QPushButton("ARM")
        self._arm_btn.setEnabled(False)
        self._arm_btn.setMinimumWidth(80)
        self._arm_btn.clicked.connect(self._on_strategy_arm_toggle)
        self._update_arm_button_style()
        toolbar.addWidget(self._arm_btn)

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" Views: "))
        self._show_chart_btn = QPushButton("Chart")
        self._show_chart_btn.setCheckable(True)
        self._show_chart_btn.setChecked(True)
        self._show_chart_btn.setMaximumWidth(70)
        self._show_chart_btn.clicked.connect(self._toggle_chart_window)
        toolbar.addWidget(self._show_chart_btn)

        self._show_strategy_btn = QPushButton("Strategy")
        self._show_strategy_btn.setCheckable(True)
        self._show_strategy_btn.setChecked(True)
        self._show_strategy_btn.setMaximumWidth(80)
        self._show_strategy_btn.clicked.connect(self._toggle_strategy_window)
        toolbar.addWidget(self._show_strategy_btn)

    def _setup_statusbar(self):
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Disconnected")
        self._status_bar.addPermanentWidget(self._status_label)

        self._trade_feed_label = QLabel("")
        self._trade_feed_label.setStyleSheet(
            "font-family: Menlo; font-size: 10px; padding: 0 6px;")
        self._status_bar.addPermanentWidget(self._trade_feed_label)

        self._trades_label = QLabel("Trades: 0")
        self._status_bar.addPermanentWidget(self._trades_label)
        self._signals_label = QLabel("Signals: 0")
        self._status_bar.addPermanentWidget(self._signals_label)
        self._strategy_state_label = QLabel("Strategy: OFF")
        self._strategy_state_label.setStyleSheet(
            "font-family: Menlo; font-size: 10px; padding: 0 6px; color: #6a6a8a;")
        self._status_bar.addPermanentWidget(self._strategy_state_label)
        self._ripple_label = QLabel("")
        self._status_bar.addPermanentWidget(self._ripple_label)
        self._exec_label = QLabel("")
        self._status_bar.addPermanentWidget(self._exec_label)

    def _toggle_chart_window(self, checked: bool):
        if checked:
            self._candle_window.show()
            self._candle_window.raise_()
        else:
            self._candle_window.hide()

    def _toggle_strategy_window(self, checked: bool):
        if checked:
            self._strategy_window.show()
            self._strategy_window.raise_()
        else:
            self._strategy_window.hide()

    def _on_chart_tf_changed(self, _index):
        ms = self._chart_tf_combo.currentData()
        if ms:
            self._candle_view.set_bucket_duration(ms)

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow { background-color: #0f0f19; }
            QToolBar {
                background-color: #1a1a2e;
                border-bottom: 1px solid #282838;
                spacing: 6px;
                padding: 4px;
            }
            QLabel { color: #b4b4c8; font-family: Menlo; font-size: 11px; }
            QLineEdit {
                background-color: #0f0f19;
                color: #b4b4c8;
                border: 1px solid #282838;
                padding: 3px;
                font-family: Menlo;
            }
            QComboBox {
                background-color: #0f0f19;
                color: #b4b4c8;
                border: 1px solid #282838;
                padding: 3px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background-color: #1a1a2e; color: #b4b4c8;
            }
            QPushButton {
                background-color: #1e3a5f;
                color: #b4b4c8;
                border: 1px solid #2a4a7f;
                padding: 5px 15px;
                font-family: Menlo;
            }
            QPushButton:hover { background-color: #2a4a7f; }
            QPushButton:disabled { background-color: #1a1a2e; color: #555; }
            QStatusBar { background-color: #1a1a2e; color: #b4b4c8; }
            QSplitter::handle { background-color: #282838; height: 2px; }
        """)

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    def _on_connect(self):
        if ofe is None:
            QMessageBox.warning(
                self, "Module Not Found",
                "orderflow_engine C++ module not built.\n"
                "Build: cd backtestingCpp/orderflow && ./build.sh"
            )
            return

        if websockets is None:
            QMessageBox.warning(self, "Missing Dependency",
                                "pip install websockets")
            return

        symbol = self._symbol_input.text().strip().upper()
        if not symbol:
            return

        try:
            tick_size = float(self._tick_size_input.text())
            imbalance = float(self._imbalance_input.text())
        except ValueError:
            tick_size = 0.01
            imbalance = 3.0

        mode = self._mode_combo.currentText()
        self._is_replay_mode = (mode != "Live")

        if mode != "Live":
            QMessageBox.information(
                self, "Replay Mode",
                "For replay, use the CLI backtest mode with the 'orderflow' strategy."
            )
            return

        window_ms = self._vp_window_combo.currentData()
        ok = self._session.start_live(
            symbol=symbol,
            tick_size=tick_size,
            imbalance=imbalance,
            vp_window_ms=int(window_ms) if window_ms is not None else 0,
            websockets_module=self._websockets_module,
            signal_callback=self._on_engine_signal,
            ripple_callback=self._on_ripple_decision,
        )
        if not ok:
            QMessageBox.warning(
                self, "Module Not Found",
                "orderflow_engine C++ module not built.\n"
                "Build: cd backtestingCpp/orderflow && ./build.sh",
            )
            return

        sizing = self._build_sizing_config()
        self._paper_engine = PaperEngine(
            symbol=symbol,
            sizing=sizing,
            order_callback=lambda o: self._new_order.emit(o),
        )

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._update_arm_button_style()
        self._status_label.setText(f"Connected: {symbol}")
        self._update_timer.start(self._update_interval_ms)

        try:
            self._strategy_store = StrategyStore(symbol)
            self._strategy_store.write_event(
                int(time.time() * 1000), "CONNECT",
                {"symbol": symbol, "mode": mode})
        except Exception as e:
            logger.error("Failed to open strategy store: %s", e)
            self._strategy_store = None

    def _on_disconnect(self):
        if self._strategy_ui_state != StrategyUIState.DISARMED:
            self._disarm_strategy()

        if self._exec_manager:
            self._exec_manager.stop()
            self._exec_manager = None
            self._exec_label.setText("")

        self._paper_engine = None
        self._update_arm_button_style()

        self._session.stop()

        if self._strategy_store:
            try:
                self._strategy_store.write_event(
                    int(time.time() * 1000), "DISCONNECT")
                self._strategy_store.close()
            except Exception as e:
                logger.error("Strategy store close error: %s", e)
            self._strategy_store = None

        self._update_timer.stop()
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._status_label.setText("Disconnected")

    # ------------------------------------------------------------------
    # Toolbar callbacks
    # ------------------------------------------------------------------

    def _on_vp_window_changed(self, _index):
        window_ms = self._vp_window_combo.currentData()
        if window_ms is not None:
            self._session.set_volume_profile_window(int(window_ms))

    def _on_candle_changed(self, _index):
        duration_ms = self._candle_combo.currentData()
        self._candle_duration_ms = duration_ms
        self._heatmap.set_bucket_duration_ms(duration_ms)
        self._market_state.bucket_duration_ms = duration_ms
        self._session.set_volume_profile_window(int(duration_ms))
        vp_idx = self._vp_window_combo.findData(duration_ms)
        if vp_idx >= 0:
            self._vp_window_combo.blockSignals(True)
            self._vp_window_combo.setCurrentIndex(vp_idx)
            self._vp_window_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Strategy state machine (Phase 2)
    # ------------------------------------------------------------------

    def _on_strategy_mode_changed(self, _index):
        mode_str = self._strategy_mode_combo.currentData()
        try:
            self._strategy_mode = StrategyMode(mode_str)
        except ValueError:
            self._strategy_mode = StrategyMode.OBSERVE

        self._ripple_mode = {
            StrategyMode.OBSERVE: RippleMode.LOG_ONLY,
            StrategyMode.PAPER: RippleMode.PAPER,
            StrategyMode.LIVE: RippleMode.LOG_ONLY,
        }.get(self._strategy_mode, RippleMode.LOG_ONLY)

        self._save_strategy_settings()
        logger.info("Strategy mode changed to %s", self._strategy_mode.value)

    def _on_strategy_arm_toggle(self):
        if self._strategy_ui_state == StrategyUIState.DISARMED:
            self._arm_strategy()
        elif self._strategy_ui_state.value.startswith("armed"):
            self._disarm_strategy()

    def _arm_strategy(self):
        if not self._engine:
            self._arm_btn.setToolTip("Connect to a feed first")
            return

        self._set_strategy_state(StrategyUIState.ARMING)

        if self._strategy_mode == StrategyMode.LIVE:
            symbol = self._symbol_input.text().strip().upper()
            if not symbol:
                self._set_strategy_state(StrategyUIState.DISARMED)
                return
            sizing = self._build_sizing_config()
            broker = BinanceBroker()
            self._exec_manager = ExecutionManager(
                broker=broker,
                symbol=symbol,
                sizing=sizing,
                cooldown_s=5.0,
                order_callback=lambda o: self._new_order.emit(o),
            )
            ok = self._exec_manager.start()
            if not ok:
                QMessageBox.warning(self, "Broker Error",
                                    "Could not connect to Binance.\n"
                                    "Check API keys in .env file.")
                self._exec_manager = None
                self._set_strategy_state(StrategyUIState.DISARMED)
                return
            self._exec_manager.arm()
            acct = self._exec_manager.account
            self._exec_label.setText(f"ARMED | Bal: {acct.balance:.2f} USDT")

        self._set_strategy_state(StrategyUIState.ARMED_WAITING)
        self._emit_strategy_signal(
            SignalCategory.STRATEGY_ARM,
            f"Strategy armed in {self._strategy_mode.value} mode",
        )
        if self._strategy_store:
            try:
                self._strategy_store.write_event(
                    int(time.time() * 1000), "ARM",
                    {"mode": self._strategy_mode.value})
            except Exception as e:
                logger.error("Strategy store ARM event error: %s", e)

    def _disarm_strategy(self):
        had_active = self._strategy_ui_state in (
            StrategyUIState.ARMED_ACTIVE, StrategyUIState.ARMED_EXITING)

        if had_active:
            self._set_strategy_state(StrategyUIState.DISARMING)

        if self._exec_manager and self._exec_manager.armed:
            self._exec_manager.disarm(close_position=True)
            self._exec_label.setText("Disarmed")

        if self._paper_engine:
            self._paper_engine.reset()

        self._heatmap.clear_strategy_overlay()
        self._set_strategy_state(StrategyUIState.DISARMED)
        self._emit_strategy_signal(
            SignalCategory.STRATEGY_DISARM,
            f"Strategy disarmed (was {'active' if had_active else 'waiting'})",
        )
        if self._strategy_store:
            try:
                self._strategy_store.write_event(
                    int(time.time() * 1000), "DISARM",
                    {"was_active": had_active})
            except Exception as e:
                logger.error("Strategy store DISARM event error: %s", e)

    def _set_strategy_state(self, new_state: StrategyUIState):
        old = self._strategy_ui_state
        if new_state == old:
            return
        self._prev_strategy_ui_state = old
        self._strategy_ui_state = new_state
        self._update_arm_button_style()
        self._update_strategy_status_bar()

        if (old.value.startswith("armed") and new_state.value.startswith("armed")
                and old != new_state):
            self._emit_strategy_signal(
                SignalCategory.STRATEGY_STATE_CHANGE,
                f"State: {old.value} \u2192 {new_state.value}",
            )
        logger.info("Strategy state: %s -> %s", old.value, new_state.value)

    def _snap_metadata(self):
        """Extract tide_bias and wave_regime from the last cached snapshot."""
        snap = self._last_strategy_snap
        if snap is None:
            return "", ""
        try:
            tide_bias = str(getattr(
                getattr(snap, "tide", None), "bias", "")).split(".")[-1]
        except Exception:
            tide_bias = ""
        try:
            wave_regime = str(getattr(
                getattr(snap, "wave", None), "regime", "")).split(".")[-1]
        except Exception:
            wave_regime = ""
        return tide_bias, wave_regime

    def _broadcast_entry(self, entry: SignalEntry):
        """Send a signal entry to the Strategy Dashboard blotter and persist."""
        self._strategy_dashboard.blotter.add_entry(entry)
        if self._strategy_store:
            try:
                self._strategy_store.write_signal(entry)
            except Exception as e:
                logger.error("Strategy store signal write error: %s", e)

    def _emit_strategy_signal(self, category: SignalCategory, description: str):
        now_ms = int(time.time() * 1000)
        tide_bias, wave_regime = self._snap_metadata()
        entry = SignalEntry(
            timestamp=now_ms,
            signal_type=category.value,
            source="strategy",
            category=category,
            description=description,
            lifecycle_state=self._strategy_ui_state.value,
            tide_bias=tide_bias,
            wave_regime=wave_regime,
        )
        self._broadcast_entry(entry)

    _ARM_STYLE_ARMED = (
        "QPushButton { background-color: #5f1e1e; color: #ff8888; "
        "border: 1px solid #992222; padding: 5px 15px; font-family: Menlo; font-weight: bold; } "
        "QPushButton:hover { background-color: #7f2a2a; }")
    _ARM_STYLE_DISARMED = (
        "QPushButton { background-color: #1e3f1e; color: #88ff88; "
        "border: 1px solid #229922; padding: 5px 15px; font-family: Menlo; font-weight: bold; } "
        "QPushButton:hover { background-color: #2a5f2a; }")
    _ARM_STYLE_TRANSITION = (
        "QPushButton { background-color: #3f3f1e; color: #ffff88; "
        "border: 1px solid #999922; padding: 5px 15px; font-family: Menlo; } "
        "QPushButton:disabled { background-color: #2a2a1e; color: #888866; }")

    def _update_arm_button_style(self):
        s = self._strategy_ui_state
        if s == StrategyUIState.DISARMED:
            self._arm_btn.setText("ARM")
            self._arm_btn.setStyleSheet(self._ARM_STYLE_DISARMED)
            self._arm_btn.setEnabled(self._engine is not None)
        elif s in (StrategyUIState.ARMING, StrategyUIState.DISARMING):
            self._arm_btn.setText("\u2026")
            self._arm_btn.setStyleSheet(self._ARM_STYLE_TRANSITION)
            self._arm_btn.setEnabled(False)
        else:
            self._arm_btn.setText("DISARM")
            self._arm_btn.setStyleSheet(self._ARM_STYLE_ARMED)
            self._arm_btn.setEnabled(True)
        self._update_sizing_enabled()

    def _update_sizing_enabled(self):
        armed = self._strategy_ui_state.value.startswith("armed")
        self._sizing_mode_combo.setEnabled(armed)
        self._sizing_value_input.setEnabled(armed)

    def _update_strategy_status_bar(self):
        s = self._strategy_ui_state
        state_labels = {
            StrategyUIState.DISARMED:       ("OFF",       "#6a6a8a"),
            StrategyUIState.ARMING:         ("ARMING",    "#ffaa00"),
            StrategyUIState.ARMED_WAITING:  ("WATCHING",  "#00cc66"),
            StrategyUIState.ARMED_ACTIVE:   ("ACTIVE",    "#22ff44"),
            StrategyUIState.ARMED_EXITING:  ("EXITING",   "#ffaa00"),
            StrategyUIState.ARMED_COOLDOWN: ("COOLDOWN",  "#4488dd"),
            StrategyUIState.DISARMING:      ("DISARMING", "#ffaa00"),
        }
        label, color = state_labels.get(s, ("?", "#6a6a8a"))
        self._strategy_state_label.setText(f"Strategy: {label}")
        self._strategy_state_label.setStyleSheet(
            f"font-family: Menlo; font-size: 10px; padding: 0 6px; color: {color};")

    def _update_strategy_state_from_snapshot(self, snap):
        """Drive strategy UI state from the C++ strategy snapshot's trade lifecycle."""
        if not self._strategy_ui_state.value.startswith("armed"):
            return
        trade_state = ""
        try:
            trade_state = str(getattr(
                getattr(snap, "trade", None), "state", "")).split(".")[-1]
        except Exception:
            return

        _LIFECYCLE_MAP = {
            "IDLE": StrategyUIState.ARMED_WAITING,
            "SETUP": StrategyUIState.ARMED_ACTIVE,
            "ENTRY": StrategyUIState.ARMED_ACTIVE,
            "CONFIRMATION": StrategyUIState.ARMED_ACTIVE,
            "EXPANSION": StrategyUIState.ARMED_ACTIVE,
            "MATURATION": StrategyUIState.ARMED_ACTIVE,
            "EXIT": StrategyUIState.ARMED_EXITING,
            "COOLDOWN": StrategyUIState.ARMED_COOLDOWN,
        }
        new = _LIFECYCLE_MAP.get(trade_state)
        if new and new != self._strategy_ui_state:
            self._set_strategy_state(new)

    def _update_heatmap_overlay_from_snapshot(self, snap):
        """Push stop/target/entry lines to heatmap when active."""
        if self._strategy_ui_state not in (
                StrategyUIState.ARMED_ACTIVE,
                StrategyUIState.ARMED_EXITING):
            self._heatmap.clear_strategy_overlay()
            return
        try:
            trade = getattr(snap, "trade", None)
            entry_p = float(getattr(trade, "entry_price", 0) or 0)
            stop_p = float(getattr(trade, "stop_price", 0) or 0)
            target_p = float(getattr(trade, "target_price", 0) or 0)
            self._heatmap.set_strategy_overlay(entry_p, stop_p, target_p)
        except Exception:
            pass

    def _build_sizing_config(self) -> SizingConfig:
        mode_val = self._sizing_mode_combo.currentData()
        mode = SizingMode(mode_val)
        try:
            value = float(self._sizing_value_input.text())
        except ValueError:
            value = 150.0
        return SizingConfig(mode=mode, value=value, max_position=value * 10)

    # ------------------------------------------------------------------
    # Signal / order / Ripple callbacks
    # ------------------------------------------------------------------

    def _on_engine_signal(self, signal):
        """C++ legacy signal callback — invoked from WS thread.

        Phase 14A: this is OBSERVATION-only. Live execution is driven
        by Ripple decisions inside :meth:`_on_ripple_received` so that
        the Ripple lifecycle FSM, Wave permissions, and ``RiskEngine``
        govern every order. Signal-driven routing was removed because
        it bypassed all three layers (AGENT_STRATEGY_RULES.md §7.4).
        """
        self._new_signal.emit(signal)

    def _on_ripple_decision(self, decision):
        """C++ Ripple decision callback — invoked from WS thread (GIL held).
        Emits a Qt signal to move processing to the main thread."""
        self._new_ripple.emit(decision)

    def _on_ripple_received(self, decision):
        """Process a Ripple decision on the main thread."""
        if self._strategy_ui_state == StrategyUIState.DISARMED:
            self._ripple_metrics.mode_suppressed += 1
            return

        intent_name = _parse_intent_name(decision)
        if intent_name == "NO_ACTION":
            return

        # Confidence filter
        if decision.confidence < self._ripple_min_confidence:
            self._ripple_metrics.confidence_suppressed += 1
            return

        # Cooldown / dedupe
        suppression = self._ripple_cooldown.check(
            intent_name, decision.timestamp, decision.reference_price)
        if suppression == SuppressionReason.COOLDOWN:
            self._ripple_metrics.cooldown_suppressed += 1
            return
        if suppression == SuppressionReason.DEDUPE:
            self._ripple_metrics.dedupe_suppressed += 1
            return

        # Resolve state name for display (parse once)
        state_name = ""
        try:
            state_name = str(decision.triggering_state).split(".")[-1]
        except Exception:
            pass

        entry = ripple_decision_to_entry(decision, state_name,
                                         intent_name=intent_name)
        if entry:
            tide_bias, wave_regime = self._snap_metadata()
            entry.tide_bias = tide_bias
            entry.wave_regime = wave_regime
            entry.lifecycle_state = self._strategy_ui_state.value
            self._broadcast_entry(entry)

        # Record emission
        self._ripple_cooldown.record(
            intent_name, decision.timestamp, decision.reference_price)
        self._ripple_metrics.emitted += 1
        h = self._session.health
        h.last_emitted_decision_ts = decision.timestamp
        h.last_candidate_decision_ts = decision.timestamp

        # Paper execution if armed in paper mode
        if (self._strategy_mode == StrategyMode.PAPER and
                self._paper_engine is not None):
            intent = ripple_decision_to_intent(decision, state_name,
                                               intent_name=intent_name)
            if intent and intent.intent_type == "entry":
                result = self._paper_engine.on_intent(intent)
                if result == SuppressionReason.INVENTORY:
                    self._ripple_metrics.inventory_suppressed += 1
            elif intent and intent.intent_type == "exit":
                self._paper_engine.on_intent(intent)

        # Phase 14A: Live execution if armed in live mode. Routes the
        # same Ripple intent through ``ExecutionManager.on_intent``
        # (event-time cooldown, Ripple-FSM-respecting). The legacy
        # ``_on_engine_signal -> on_signal`` route is OBSERVATION-only
        # now — see AGENT_STRATEGY_RULES.md §7.4.
        # Phase 14C: re-apply the C++ engine Wave / RiskEngine gate on
        # the Python side so a real broker never sees an order when
        # the engine state already decided to block the trade
        # (`strategy.md` §22.2 #12 / `AGENT_STRATEGY_RULES.md` §7.6).
        if (self._strategy_mode == StrategyMode.LIVE and
                self._exec_manager is not None and
                self._exec_manager.armed):
            intent = ripple_decision_to_intent(decision, state_name,
                                               intent_name=intent_name)
            if intent and intent.intent_type in ("entry", "exit"):
                try:
                    qty = float(getattr(self._exec_manager,
                                        "current_qty", 0.0) or 0.0)
                    ref = float(intent.reference_price or 0.0)
                    pos_usd = abs(qty) * ref
                    block = intent_risk_block_reason(
                        intent, self._engine,
                        current_position_usd=pos_usd,
                        ofe_module=ofe,
                    )
                except Exception:
                    logger.exception("intent_risk_block_reason failed")
                    block = None
                if block is not None:
                    logger.warning(
                        "V1 §22.2 #12 gate blocked live intent: %s (%s)",
                        intent.action, block)
                    return
                try:
                    self._exec_manager.on_intent(intent)
                except Exception:
                    logger.exception("exec_manager.on_intent failed")

    def _on_order_received(self, order):
        status = order.status.value
        side = order.side.value
        logger.info("Order %s: %s %s %.6f @ %.2f [%s]",
                     order.id, side, order.symbol, order.fill_quantity,
                     order.fill_price, status)
        self._strategy_dashboard.blotter.add_signal(
            int(order.timestamp * 1000), f"EXEC_{side}",
            order.fill_price, 1.0,
            f"{status} qty={order.fill_quantity:.6f}"
            + (f" | {order.ripple_reason}" if order.ripple_reason else "")
        )
        self._strategy_dashboard.account_panel.add_order(order)

    def _on_signal_received(self, signal):
        self._signal_count += 1
        is_buy = "BUY" in signal.type_name() or "BULL" in signal.type_name()
        fill_price = signal.price
        if self._engine:
            ob = self._engine.get_order_book()
            p = ob.get_best_ask() if is_buy else ob.get_best_bid()
            if p > 0:
                fill_price = p

        self._strategy_dashboard.blotter.add_signal(
            signal.timestamp, signal.type_name(),
            fill_price, signal.strength, signal.description
        )
        self._heatmap.add_signal(
            signal.timestamp, fill_price, signal.type_name(), signal.strength
        )

    # ------------------------------------------------------------------
    # Timer tick
    # ------------------------------------------------------------------

    def _on_timer_tick(self):
        self._session.on_timer_tick()

    def show(self):
        super().show()
        self._candle_window.show()
        self._strategy_window.show()

    def closeEvent(self, event):
        self._save_strategy_settings()
        self._on_disconnect()
        self._candle_window.close()
        self._strategy_window.close()
        super().closeEvent(event)
