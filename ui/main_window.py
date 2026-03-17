import sys
import os
import logging
import json
import asyncio
import time
import math
import threading
from enum import Enum
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QToolBar, QComboBox, QPushButton, QLabel, QLineEdit,
    QStatusBar, QMessageBox,
)
from PySide6.QtCore import Qt, QTimer, Signal as QtSignal, QSettings
from PySide6.QtGui import QFont, QAction, QPainter, QColor, QPen

from ui.heatmap_widget import HeatmapWidget
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

import requests


# ---------------------------------------------------------------------------
# Trade / Depth feed health state machine
# ---------------------------------------------------------------------------

class FeedState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"
    STALE = "stale"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class FeedStreamHealth:
    """Per-stream (trade or depth) health snapshot."""
    state: FeedState = FeedState.DISCONNECTED
    symbol: str = ""
    last_msg_wall_clock: float = 0.0
    last_msg_exchange_ts: int = 0
    messages_received: int = 0
    reconnect_count: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    last_reconnect_wall_clock: float = 0.0

    @property
    def seconds_since_last_msg(self) -> float:
        if self.last_msg_wall_clock <= 0:
            return 999.0
        return time.monotonic() - self.last_msg_wall_clock

    @property
    def is_healthy(self) -> bool:
        return self.state == FeedState.LIVE

    def on_message(self, exchange_ts: int = 0):
        self.last_msg_wall_clock = time.monotonic()
        if exchange_ts > 0:
            self.last_msg_exchange_ts = exchange_ts
        self.messages_received += 1
        if self.state != FeedState.LIVE:
            self.state = FeedState.LIVE
            self.consecutive_failures = 0

    def on_disconnect(self, error: str = ""):
        self.state = FeedState.DISCONNECTED
        if error:
            self.last_error = error

    def on_reconnect_start(self):
        self.state = FeedState.RECONNECTING
        self.last_reconnect_wall_clock = time.monotonic()
        self.reconnect_count += 1

    def on_reconnect_fail(self, error: str):
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= _FEED_MAX_CONSECUTIVE_FAILURES:
            self.state = FeedState.FAILED
        else:
            self.state = FeedState.DISCONNECTED

    def on_stale(self):
        if self.state == FeedState.LIVE:
            self.state = FeedState.STALE

    @property
    def short_status(self) -> str:
        s = self.state.value.upper()
        if self.state == FeedState.LIVE:
            return s
        if self.state == FeedState.STALE:
            return f"{s} {self.seconds_since_last_msg:.0f}s"
        if self.state == FeedState.RECONNECTING:
            return f"{s} #{self.reconnect_count}"
        if self.state == FeedState.FAILED:
            return f"{s} ({self.consecutive_failures}x)"
        return s


# Feed resiliency configuration
_FEED_STALE_MS = 10_000
_FEED_RECONNECT_INITIAL_BACKOFF_S = 1.0
_FEED_RECONNECT_MAX_BACKOFF_S = 30.0
_FEED_MAX_CONSECUTIVE_FAILURES = 10


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


@dataclass
class _HealthSnapshot:
    """Lightweight per-tick health telemetry."""
    tick_count: int = 0
    trades_drained_total: int = 0
    last_trade_buffer_depth: int = 0
    last_signal_log_size: int = 0
    last_ripple_state: str = ""
    last_ripple_state_age_ms: int = 0
    last_emitted_decision_ts: int = 0
    last_candidate_decision_ts: int = 0
    tick_duration_ms_avg: float = 0.0
    tick_duration_ms_max: float = 0.0
    vp_recomputes: int = 0
    status_updates: int = 0
    badge_updates: int = 0
    phase_trade_ms: float = 0.0
    phase_book_ms: float = 0.0
    phase_vp_cvd_ms: float = 0.0
    _recent_durations: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_tick(self, duration_ms: float):
        self.tick_count += 1
        self._recent_durations.append(duration_ms)
        n = len(self._recent_durations)
        if n > 0:
            self.tick_duration_ms_avg = sum(self._recent_durations) / n
            self.tick_duration_ms_max = max(self._recent_durations)


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

        self._engine = None
        self._feed = None
        self._exec_manager: ExecutionManager | None = None
        self._paper_engine: PaperEngine | None = None
        self._update_timer = QTimer()
        self._update_timer.timeout.connect(self._on_timer_tick)
        self._update_interval_ms = 100

        self._signal_buffer = []
        self._trade_buffer = deque(maxlen=200_000)
        self._candle_duration_ms = 60_000
        self._signal_count = 0
        self._trades_received_ws = 0
        self._trades_dropped_in_drain = 0
        self._last_drained_trade_ts = 0
        self._latest_ws_trade_ts = 0
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
        self._health = _HealthSnapshot()

        # Feed health tracking (live mode only)
        self._trade_feed_health = FeedStreamHealth()
        self._depth_feed_health = FeedStreamHealth()
        self._is_replay_mode = False

        # Order book health
        self._book_empty_ticks = 0
        self._book_crossed_ticks = 0
        self._last_valid_bid = 0.0
        self._last_valid_ask = 0.0
        self._last_valid_bid_count = 0
        self._last_valid_ask_count = 0
        self._book_resync_count = 0
        self._book_resync_pending = False

        # Dedup: (exchange_ts, price, qty) of recent trades to discard
        # duplicates that some exchanges emit on reconnect.
        self._recent_trade_keys: deque = deque(maxlen=200)

        self._settings = QSettings("OrderFlowTrading", "MainWindow")
        self._load_strategy_settings()

        self._market_state = MarketState(
            bucket_duration_ms=self._candle_duration_ms,
            visible_window_ms=self._candle_duration_ms,
        )

        self._setup_ui()
        self._setup_toolbar()
        self._setup_statusbar()
        self._apply_stylesheet()

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

        config = ofe.EngineConfig()
        config.tick_size = tick_size
        config.signal_params.imbalance_threshold = imbalance

        try:
            config.ripple.enable_diagnostics = True
            config.ripple.console_diagnostics = False
        except AttributeError:
            pass

        self._engine = ofe.OrderFlowEngine(config)
        self._engine.set_signal_callback(self._on_engine_signal)
        self._engine.set_ripple_callback(self._on_ripple_decision)

        window_ms = self._vp_window_combo.currentData()
        self._engine.get_volume_profile().set_window(window_ms)

        sizing = self._build_sizing_config()
        self._paper_engine = PaperEngine(
            symbol=symbol,
            sizing=sizing,
            order_callback=lambda o: self._new_order.emit(o),
        )

        mode = self._mode_combo.currentText()
        self._is_replay_mode = (mode != "Live")

        if mode == "Live":
            self._trade_feed_health = FeedStreamHealth(symbol=symbol)
            self._depth_feed_health = FeedStreamHealth(symbol=symbol)
            self._recent_trade_keys.clear()
            self._book_empty_ticks = 0
            self._book_crossed_ticks = 0
            self._book_resync_count = 0
            self._book_resync_pending = False

            self._ws_stop_event = threading.Event()
            self._ws_thread = threading.Thread(
                target=self._run_ws_feed,
                args=(symbol.lower(), self._ws_stop_event),
                daemon=True,
            )
            self._fetch_depth_snapshot(symbol)
            self._ws_thread.start()
        else:
            QMessageBox.information(
                self, "Replay Mode",
                "For replay, use the CLI backtest mode with the 'orderflow' strategy."
            )
            return

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

    def _fetch_depth_snapshot(self, symbol: str):
        try:
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/depth",
                params={"symbol": symbol, "limit": 1000}, timeout=10)
            
            resp.raise_for_status()
            j = resp.json()

            snap = ofe.DepthUpdate()
            snap.timestamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            snap.first_update_id = j.get("lastUpdateId", 0)
            snap.final_update_id = snap.first_update_id
            snap.is_snapshot = True
            bids = []
            for b in j.get("bids", []):
                lv = ofe.DepthLevel()
                lv.price, lv.quantity = float(b[0]), float(b[1])
                bids.append(lv)
            asks = []
            for a in j.get("asks", []):
                lv = ofe.DepthLevel()
                lv.price, lv.quantity = float(a[0]), float(a[1])
                asks.append(lv)
            snap.bids, snap.asks = bids, asks
            self._engine.process_depth(snap)
            logger.info("Depth snapshot loaded: %d bids, %d asks",
                        len(bids), len(asks))
        except Exception as e:
            logger.error("Depth snapshot failed: %s", e)

    def _run_ws_feed(self, symbol_lower: str, stop_event: threading.Event):
        """Run trade + depth WS streams with independent reconnect per stream.

        Each stream runs in its own long-lived coroutine with bounded
        exponential backoff.  A shared watchdog polls *stop_event* and
        also enforces the stale-trade timeout, requesting a reconnect
        of the trade stream when it goes silent while the depth stream
        is still alive.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        base = "wss://fstream.binance.com/ws/"

        trade_reconnect_event = asyncio.Event()
        depth_reconnect_event = asyncio.Event()

        # -- helpers -----------------------------------------------------------

        def _backoff(failures: int) -> float:
            raw = _FEED_RECONNECT_INITIAL_BACKOFF_S * (2 ** min(failures, 8))
            return min(raw, _FEED_RECONNECT_MAX_BACKOFF_S)

        # Recv timeout: allows stream coroutines to check stop_event
        # without relying on task cancellation.  This is critical because
        # cancelled tasks cannot properly await ws.close(), leaving the
        # websockets library's internal keepalive task dangling.
        _RECV_TIMEOUT_S = 0.5

        # -- trade stream ------------------------------------------------------

        async def _run_trade_stream():
            uri = f"{base}{symbol_lower}@trade"
            h = self._trade_feed_health

            while not stop_event.is_set():
                ws = None
                try:
                    h.state = FeedState.CONNECTING
                    ws = await asyncio.wait_for(
                        websockets.connect(uri), timeout=15)
                    logger.info("Trade WS connected: %s", uri)

                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=_RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            continue
                        except websockets.ConnectionClosed:
                            break

                        try:
                            j = json.loads(raw)
                            ts = j["T"]
                            price = float(j["p"])
                            qty = float(j["q"])
                            is_buyer_maker = j["m"]

                            trade_id = j["t"]
                            if trade_id in self._recent_trade_keys:
                                continue
                            self._recent_trade_keys.append(trade_id)

                            t = ofe.Trade()
                            t.timestamp = ts
                            t.price = price
                            t.quantity = qty
                            t.is_buyer_maker = is_buyer_maker
                            self._engine.process_trade(t)
                            self._trades_received_ws += 1
                            if ts > self._latest_ws_trade_ts:
                                self._latest_ws_trade_ts = ts
                            self._trade_buffer.append(
                                (ts, price, qty, not is_buyer_maker))
                            h.on_message(ts)
                        except (KeyError, ValueError, TypeError) as parse_err:
                            logger.warning("Trade parse error: %s", parse_err)

                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    err_str = f"{type(exc).__name__}: {exc}"
                    if not stop_event.is_set():
                        logger.warning("Trade WS error: %s", err_str)
                    h.on_disconnect(err_str)
                finally:
                    if ws is not None:
                        try:
                            await ws.close()
                        except BaseException:
                            pass

                if stop_event.is_set():
                    return

                h.on_reconnect_start()
                wait = _backoff(h.consecutive_failures)
                logger.info("Trade WS reconnect in %.1fs (attempt #%d)",
                            wait, h.reconnect_count)
                try:
                    await asyncio.wait_for(
                        _wait_or_stop(stop_event, trade_reconnect_event,
                                      wait),
                        timeout=wait + 1)
                except asyncio.TimeoutError:
                    pass
                trade_reconnect_event.clear()

                if h.state == FeedState.FAILED:
                    logger.error("Trade feed FAILED after %d consecutive "
                                 "failures — giving up",
                                 h.consecutive_failures)
                    return
                if h.state != FeedState.LIVE:
                    h.on_reconnect_fail(h.last_error or "retry")

        # -- depth stream ------------------------------------------------------

        async def _run_depth_stream():
            uri = f"{base}{symbol_lower}@depth@100ms"
            h = self._depth_feed_health

            while not stop_event.is_set():
                ws = None
                try:
                    h.state = FeedState.CONNECTING
                    ws = await asyncio.wait_for(
                        websockets.connect(uri), timeout=15)
                    logger.info("Depth WS connected: %s", uri)

                    while not stop_event.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=_RECV_TIMEOUT_S)
                        except asyncio.TimeoutError:
                            continue
                        except websockets.ConnectionClosed:
                            break

                        try:
                            j = json.loads(raw)
                            u = ofe.DepthUpdate()
                            u.timestamp = j.get("E", 0)
                            u.first_update_id = j.get("U", 0)
                            u.final_update_id = j.get("u", 0)
                            u.is_snapshot = False
                            bids = []
                            for b in j.get("b", []):
                                lv = ofe.DepthLevel()
                                lv.price = float(b[0])
                                lv.quantity = float(b[1])
                                bids.append(lv)
                            asks = []
                            for a in j.get("a", []):
                                lv = ofe.DepthLevel()
                                lv.price = float(a[0])
                                lv.quantity = float(a[1])
                                asks.append(lv)
                            u.bids, u.asks = bids, asks
                            self._engine.process_depth(u)
                            h.on_message(u.timestamp)
                        except (KeyError, ValueError, TypeError) as parse_err:
                            logger.warning("Depth parse error: %s", parse_err)

                except asyncio.CancelledError:
                    return
                except Exception as exc:
                    err_str = f"{type(exc).__name__}: {exc}"
                    if not stop_event.is_set():
                        logger.warning("Depth WS error: %s", err_str)
                    h.on_disconnect(err_str)
                finally:
                    if ws is not None:
                        try:
                            await ws.close()
                        except BaseException:
                            pass

                if stop_event.is_set():
                    return

                h.on_reconnect_start()
                wait = _backoff(h.consecutive_failures)
                logger.info("Depth WS reconnect in %.1fs (attempt #%d)",
                            wait, h.reconnect_count)
                try:
                    await asyncio.wait_for(
                        _wait_or_stop(stop_event, depth_reconnect_event,
                                      wait),
                        timeout=wait + 1)
                except asyncio.TimeoutError:
                    pass
                depth_reconnect_event.clear()

                if h.state == FeedState.FAILED:
                    logger.error("Depth feed FAILED after %d consecutive "
                                 "failures — giving up",
                                 h.consecutive_failures)
                    return
                if h.state != FeedState.LIVE:
                    h.on_reconnect_fail(h.last_error or "retry")

        # -- watchdog: stop-event + stale-trade detection ----------------------

        async def _wait_or_stop(stop_ev: threading.Event,
                                async_ev: asyncio.Event,
                                timeout_s: float):
            """Sleep for *timeout_s* but wake early on stop or async event."""
            end = time.monotonic() + timeout_s
            while time.monotonic() < end:
                if stop_ev.is_set() or async_ev.is_set():
                    return
                await asyncio.sleep(0.25)

        async def _watchdog():
            stale_threshold_s = _FEED_STALE_MS / 1000.0
            while not stop_event.is_set():
                await asyncio.sleep(1.0)

                th = self._trade_feed_health
                dh = self._depth_feed_health

                if (th.state == FeedState.LIVE and
                        th.seconds_since_last_msg > stale_threshold_s):
                    th.on_stale()
                    logger.warning(
                        "Trade feed STALE — no message for %.1fs "
                        "(depth %s, %.1fs ago)",
                        th.seconds_since_last_msg,
                        dh.state.value,
                        dh.seconds_since_last_msg)

                if th.state == FeedState.STALE:
                    trade_reconnect_event.set()

        # -- helpers: clean shutdown -------------------------------------------

        async def _cancel_remaining_tasks():
            """Cancel library-internal tasks (websockets keepalive, etc.)."""
            tasks = [t for t in asyncio.all_tasks()
                     if t is not asyncio.current_task() and not t.done()]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        # -- main orchestration ------------------------------------------------

        async def _main():
            tasks = [
                asyncio.create_task(_run_trade_stream()),
                asyncio.create_task(_run_depth_stream()),
                asyncio.create_task(_watchdog()),
            ]
            # All three tasks exit naturally when stop_event is set
            # (recv timeout loop, sleep loop).  No task cancellation needed
            # for the normal disconnect path.
            await asyncio.gather(*tasks, return_exceptions=True)

            # Sweep websockets-internal tasks (keepalive, etc.)
            await _cancel_remaining_tasks()

        try:
            loop.run_until_complete(_main())
        except Exception as e:
            if not stop_event.is_set():
                logger.error("WS feed loop error: %s", e)
        finally:
            try:
                loop.run_until_complete(_cancel_remaining_tasks())
            except Exception:
                pass
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._trade_feed_health.on_disconnect("loop exited")
            self._depth_feed_health.on_disconnect("loop exited")

    def _on_disconnect(self):
        if self._strategy_ui_state != StrategyUIState.DISARMED:
            self._disarm_strategy()

        if self._exec_manager:
            self._exec_manager.stop()
            self._exec_manager = None
            self._exec_label.setText("")

        self._paper_engine = None
        self._update_arm_button_style()

        if hasattr(self, '_ws_stop_event') and self._ws_stop_event is not None:
            self._ws_stop_event.set()
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=5)
            self._ws_stop_event = None
            self._ws_thread = None

        self._trade_feed_health = FeedStreamHealth()
        self._depth_feed_health = FeedStreamHealth()
        self._recent_trade_keys.clear()
        self._trades_received_ws = 0
        self._trades_dropped_in_drain = 0
        self._last_drained_trade_ts = 0
        self._latest_ws_trade_ts = 0
        self._book_empty_ticks = 0
        self._book_crossed_ticks = 0
        self._book_resync_count = 0
        self._book_resync_pending = False

        if self._strategy_store:
            try:
                self._strategy_store.write_event(
                    int(time.time() * 1000), "DISCONNECT")
                self._strategy_store.close()
            except Exception as e:
                logger.error("Strategy store close error: %s", e)
            self._strategy_store = None

        self._engine = None
        self._update_timer.stop()
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._status_label.setText("Disconnected")

    # ------------------------------------------------------------------
    # Toolbar callbacks
    # ------------------------------------------------------------------

    def _on_vp_window_changed(self, _index):
        if self._engine:
            window_ms = self._vp_window_combo.currentData()
            self._engine.get_volume_profile().set_window(window_ms)

    def _on_candle_changed(self, _index):
        duration_ms = self._candle_combo.currentData()
        self._candle_duration_ms = duration_ms
        self._heatmap.set_bucket_duration_ms(duration_ms)
        self._market_state.bucket_duration_ms = duration_ms
        if self._engine:
            self._engine.get_volume_profile().set_window(duration_ms)
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
        """C++ legacy signal callback — invoked from WS thread."""
        self._new_signal.emit(signal)
        if self._exec_manager and self._exec_manager.armed:
            self._exec_manager.on_signal(signal)

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
        self._health.last_emitted_decision_ts = decision.timestamp
        self._health.last_candidate_decision_ts = decision.timestamp

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

    _MAX_TRADES_PER_TICK = 2000
    _VP_RECOMPUTE_EVERY = 5
    _STATUS_PANEL_EVERY = 10
    _FEED_BADGE_EVERY = 5

    _BOOK_EMPTY_RESYNC_THRESHOLD = 30
    _BOOK_HEALTH_LOG_INTERVAL = 50

    def _on_timer_tick(self):
        if not self._engine:
            return

        t0 = time.monotonic()
        drained = 0

        # --- Phase 0: sync heatmap trade-time to real-time WS feed --------
        # Prevents chart_now from freezing when the drain buffer has a
        # backlog — keeps the heatmap scrolling at real-time pace.
        ws_ts = self._latest_ws_trade_ts
        if ws_ts > 0:
            self._heatmap.sync_trade_time(ws_ts)

        # --- Phase 1: drain trade buffer (independent of C++ engine) ------
        try:
            buf_depth = len(self._trade_buffer)
            self._health.last_trade_buffer_depth = buf_depth
            cap = self._MAX_TRADES_PER_TICK
            if buf_depth > 10_000:
                cap = min(buf_depth, cap * 4)
            elif buf_depth > 5000:
                cap = cap * 2
            drain_errors = 0
            while self._trade_buffer and drained < cap:
                try:
                    ts, price, qty, is_buy = self._trade_buffer.popleft()
                except IndexError:
                    break
                try:
                    self._heatmap.add_trade(ts, price, qty, is_buy)
                except Exception as exc:
                    drain_errors += 1
                    if drain_errors <= 3:
                        logger.warning("heatmap.add_trade error: %s", exc)
                try:
                    self._cvd.add_trade(ts, price, qty, is_buy)
                except Exception as exc:
                    drain_errors += 1
                    if drain_errors <= 3:
                        logger.warning("cvd.add_trade error: %s", exc)
                try:
                    self._candle_view.process_trade(ts, price, qty, is_buy)
                except Exception:
                    pass
                self._last_drained_trade_ts = ts
                drained += 1
            if drain_errors > 0:
                self._trades_dropped_in_drain += drain_errors
                if (self._trades_dropped_in_drain <= 10
                        or self._health.tick_count % 50 == 0):
                    logger.warning(
                        "Trade drain errors: %d this tick, %d total",
                        drain_errors, self._trades_dropped_in_drain)
            remaining = len(self._trade_buffer)
            if remaining > 5000:
                logger.warning(
                    "Trade buffer backlog: %d remaining after draining %d "
                    "(started with %d)", remaining, drained, buf_depth)
        except Exception as e:
            logger.error("Trade drain error: %s", e)

        t1 = time.monotonic()

        # --- Phase 2: sample order book (isolated — must not kill tick) ----
        book_ok = False
        try:
            ob = self._engine.get_order_book()
            snap = ob.get_snapshot()

            bids = snap.get_bids()
            asks = snap.get_asks()
            best_bid = snap.best_bid
            best_ask = snap.best_ask
            snap_ts = snap.timestamp

            bid_count = len(bids)
            ask_count = len(asks)
            book_empty = (bid_count == 0 and ask_count == 0)
            book_crossed = (best_bid > 0 and best_ask > 0
                            and best_bid >= best_ask)

            if book_empty:
                self._book_empty_ticks += 1
                if (self._book_empty_ticks == 1 or
                        self._book_empty_ticks % self._BOOK_HEALTH_LOG_INTERVAL == 0):
                    logger.warning(
                        "Order book EMPTY for %d ticks — last valid "
                        "bid=%.2f ask=%.2f (%d/%d levels)",
                        self._book_empty_ticks,
                        self._last_valid_bid, self._last_valid_ask,
                        self._last_valid_bid_count,
                        self._last_valid_ask_count)
            else:
                if self._book_empty_ticks > 0:
                    logger.info("Order book recovered after %d empty ticks",
                                self._book_empty_ticks)
                self._book_empty_ticks = 0
                self._last_valid_bid = best_bid
                self._last_valid_ask = best_ask
                self._last_valid_bid_count = bid_count
                self._last_valid_ask_count = ask_count

            if book_crossed:
                self._book_crossed_ticks += 1
            else:
                self._book_crossed_ticks = 0

            # Auto-resync: re-fetch snapshot if book empty for too long
            if (not self._is_replay_mode
                    and self._book_empty_ticks >= self._BOOK_EMPTY_RESYNC_THRESHOLD
                    and not self._book_resync_pending):
                self._book_resync_pending = True
                self._book_resync_count += 1
                symbol = self._symbol_input.text().strip().upper()
                logger.warning("Triggering depth snapshot resync #%d for %s",
                               self._book_resync_count, symbol)
                threading.Thread(
                    target=self._resync_depth_snapshot,
                    args=(symbol,),
                    daemon=True,
                ).start()

            # Only feed to heatmap if book has data
            if not book_empty:
                chart_now_ts = self._heatmap.chart_now
                sample_ts = chart_now_ts if chart_now_ts > 0 else snap_ts
                self._heatmap.add_depth_column(
                    snap_ts, bids, asks, best_bid, best_ask,
                    sample_ts=sample_ts,
                )
                book_ok = True
            else:
                # Still advance the heatmap sample time so slices don't
                # age out — use existing book data (preserved by the
                # empty-guard in add_depth_column).
                chart_now_ts = self._heatmap.chart_now
                if chart_now_ts > 0:
                    self._heatmap.add_depth_column(
                        snap_ts, [], [], 0, 0,
                        sample_ts=chart_now_ts,
                    )

        except Exception as e:
            logger.error("Order book snapshot error: %s", e)

        t2 = time.monotonic()

        # --- Phase 3: volume profile, CVD alignment (safe even if book failed)
        try:
            self._vp_tick_counter = getattr(self, '_vp_tick_counter', 0) + 1
            if self._vp_tick_counter >= self._VP_RECOMPUTE_EVERY:
                self._vp_tick_counter = 0
                self._recompute_volume_profile()

            chart_now = self._heatmap.chart_now
            if chart_now > 0:
                self._cvd.set_time_ref(
                    chart_now,
                    self._heatmap._visible_window_ms,
                    self._heatmap._chart_right_inset,
                )
            self._cvd.refresh()
        except Exception as e:
            logger.error("VP/CVD update error: %s", e)

        t3 = time.monotonic()

        # --- Phase 4: status labels (safe, no C++ calls) ------------------
        try:
            if self._engine:
                tf = self._engine.get_trade_flow()
                self._trades_label.setText(
                    f"Trades: {int(tf.get_total_volume())}")

                sig = self._engine.get_signal_engine()
                self._signals_label.setText(
                    f"Signals: {self._signal_count} | "
                    f"PnL: {sig.get_pnl():.2f}% | "
                    f"Pos: {sig.get_position()}")
        except Exception as e:
            logger.error("Status label error: %s", e)

        # --- Phase 5: feed + pipeline health badge (throttled) -------------
        try:
            self._badge_tick_counter = getattr(
                self, '_badge_tick_counter', 0) + 1
            self._status_tick_counter = getattr(
                self, '_status_tick_counter', 0) + 1

            if self._badge_tick_counter >= self._FEED_BADGE_EVERY:
                self._badge_tick_counter = 0
                self._update_feed_health_badge()

            if self._status_tick_counter >= self._STATUS_PANEL_EVERY:
                self._status_tick_counter = 0
                self._update_pipeline_diagnostics(book_ok)
        except Exception as e:
            logger.error("Diagnostics error: %s", e)

        # --- Phase 6: strategy diagnostics + state machine (throttled) -------
        try:
            self._strat_tick_counter = getattr(self, '_strat_tick_counter', 0) + 1
            if self._strat_tick_counter >= 5:
                self._strat_tick_counter = 0
                if self._engine and hasattr(self._engine, 'get_strategy_snapshot'):
                    snap = self._engine.get_strategy_snapshot()
                    self._last_strategy_snap = snap
                    self._update_strategy_state_from_snapshot(snap)
                    self._update_heatmap_overlay_from_snapshot(snap)
                    if (self._strategy_store
                            and self._strategy_ui_state.value.startswith("armed")):
                        self._strategy_store.buffer_snapshot(
                            int(time.time() * 1000), snap)
        except Exception as e:
            logger.error("Strategy panel error: %s", e)

        # --- Phase 6b: execution manager (isolated) -----------------------
        try:
            if self._exec_manager:
                acct = self._exec_manager.account
                self._strategy_dashboard.account_panel.update_account(
                    acct.balance, acct.available_balance, acct.positions
                )
                if self._exec_manager.armed:
                    side = self._exec_manager.current_side
                    qty = self._exec_manager.current_qty
                    pos_str = f"{side.value} {qty:.6f}" if side else "Flat"
                    self._exec_label.setText(
                        f"ARMED | Bal: {acct.balance:.2f} | Pos: {pos_str}")
        except Exception as e:
            logger.error("Exec manager update error: %s", e)

        # --- Phase 7: compute ViewModel frame + sync MarketState + repaint ---
        vm = self._orderflow_vm
        pw, ph, ml, mt, cri = self._heatmap.get_viewport_params()
        frame = vm.compute_frame(pw, ph, ml, mt, cri)
        self._heatmap.set_frame(frame)

        ms = self._market_state
        ms.chart_now = vm.chart_now
        ms.visible_window_ms = vm.visible_window_ms
        ms.bucket_duration_ms = vm.bucket_duration_ms
        ms.best_bid = self._last_valid_bid
        ms.best_ask = self._last_valid_ask
        ms.strategy_snapshot = self._last_strategy_snap
        ms.strategy_ui_state = self._strategy_ui_state

        self._heatmap.update()
        if self._candle_window.isVisible():
            self._candle_view.update()
        if self._strategy_window.isVisible():
            self._strategy_dashboard.update_from_state()
            self._strategy_dashboard.update()

        elapsed = (time.monotonic() - t0) * 1000.0
        self._health.record_tick(elapsed)
        self._health.trades_drained_total += drained
        self._health.phase_trade_ms = (t1 - t0) * 1000.0
        self._health.phase_book_ms = (t2 - t1) * 1000.0
        self._health.phase_vp_cvd_ms = (t3 - t2) * 1000.0

        if self._health.tick_count % 50 == 0:
            hm = self._heatmap
            trade_lag = 0
            if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
                trade_lag = hm.chart_now - self._last_drained_trade_ts
            buf_len = len(self._trade_buffer)
            overflow = max(0, self._trades_received_ws
                          - self._health.trades_drained_total - buf_len)
            bd = hm.get_bubble_diagnostics() if hasattr(hm, 'get_bubble_diagnostics') else {}
            dt_skew = hm._last_depth_ts - hm._last_trade_ts
            logger.info(
                "PERF tick=%d avg=%.1fms max=%.1fms | "
                "drain=%.1fms book=%.1fms vp=%.1fms | "
                "q=%d bub=%d slc=%d trd=%d paint=%.1fms | "
                "lag=%dms ovfl=%d drErr=%d skew=%dms | "
                "fT=%d fX=%d fY=%d p95=%.4g prn=%d stage=%s",
                self._health.tick_count,
                self._health.tick_duration_ms_avg,
                self._health.tick_duration_ms_max,
                self._health.phase_trade_ms,
                self._health.phase_book_ms,
                self._health.phase_vp_cvd_ms,
                buf_len,
                getattr(hm, '_last_visible_bubble_count', 0),
                len(hm._slices),
                len(hm._trades),
                getattr(hm, '_last_paint_ms', 0),
                trade_lag,
                overflow,
                self._trades_dropped_in_drain,
                dt_skew,
                bd.get('filtered_by_time', -1),
                bd.get('filtered_by_x', -1),
                bd.get('filtered_by_y', -1),
                bd.get('p95_size', -1),
                bd.get('trades_pruned_this_frame', -1),
                bd.get('failure_stage', '?'),
            )

    # ------------------------------------------------------------------
    # Feed health badge
    # ------------------------------------------------------------------

    _FEED_BADGE_STYLES = {
        FeedState.LIVE: "color: #00cc66;",
        FeedState.STALE: "color: #ffaa00;",
        FeedState.RECONNECTING: "color: #ff6600;",
        FeedState.FAILED: "color: #ff3333;",
        FeedState.CONNECTING: "color: #6a6a8a;",
        FeedState.DISCONNECTED: "color: #6a6a8a;",
    }

    _FEED_STATE_LABEL = {
        FeedState.LIVE: "Live",
        FeedState.STALE: "Stale",
        FeedState.RECONNECTING: "Recon",
        FeedState.FAILED: "Failed",
        FeedState.CONNECTING: "...",
        FeedState.DISCONNECTED: "Off",
    }

    def _update_feed_health_badge(self):
        """Compact footer summary — detailed health goes to _status_panel."""
        if self._is_replay_mode:
            txt = "Replay"
            style = ("font-family: Menlo; font-size: 10px; "
                     "padding: 0 6px; color: #6a6a8a;")
        else:
            th = self._trade_feed_health
            dh = self._depth_feed_health

            if self._book_empty_ticks > 0:
                color = "#ff3333"
            elif th.state in (FeedState.STALE, FeedState.FAILED):
                color = "#ffaa00"
            elif th.state == FeedState.LIVE and dh.state == FeedState.LIVE:
                color = "#00cc66"
            else:
                color = "#6a6a8a"

            style = (f"font-family: Menlo; font-size: 10px; "
                     f"padding: 0 6px; color: {color};")
            txt = (f"T:{self._FEED_STATE_LABEL.get(th.state, '?')} "
                   f"D:{self._FEED_STATE_LABEL.get(dh.state, '?')}")

        prev = getattr(self, '_feed_badge_cache', (None, None))
        if (txt, style) != prev:
            self._feed_badge_cache = (txt, style)
            self._trade_feed_label.setStyleSheet(style)
            self._trade_feed_label.setText(txt)
            self._health.badge_updates += 1

    _BUBBLE_DEAD_THRESHOLD_TICKS = 50

    def _update_pipeline_diagnostics(self, book_ok: bool):
        """Update footer + side status panel (called at throttled cadence)."""

        # --- Side status panel rows (stable keys only) --------------------
        th = self._trade_feed_health
        dh = self._depth_feed_health
        hm = self._heatmap

        if self._is_replay_mode:
            mode_val = "Replay"
        elif not self._engine:
            mode_val = "Off"
        else:
            mode_val = "Live"

        t_label = self._FEED_STATE_LABEL.get(th.state, "?")
        d_label = self._FEED_STATE_LABEL.get(dh.state, "?")

        if self._book_empty_ticks > 0:
            book_val = "Empty"
        elif self._book_crossed_ticks > 0:
            book_val = "Cross"
        elif book_ok:
            book_val = "Synced"
        else:
            book_val = "?"

        hm_ok = hm._heatmap_samples > 0 and hm._last_log_max > 0
        hm_val = "Live" if hm_ok else ("Stalled" if hm._heatmap_samples > 0 else "?")

        ripple_val = (f"{self._strategy_mode.value.title()}"
                      f"/{self._strategy_ui_state.value.split('_')[-1].title()}")

        # Bubble pipeline health — granular failure-stage detection
        bubble_ct = getattr(hm, '_last_visible_bubble_count', 0)
        trade_ct = len(hm._trades)
        buf_depth = self._health.last_trade_buffer_depth
        trades_arriving = (th.state == FeedState.LIVE or trade_ct > 0)
        bd = hm.get_bubble_diagnostics() if hasattr(hm, 'get_bubble_diagnostics') else {}
        failure_stage = bd.get('failure_stage', 'NONE')

        if bubble_ct > 0:
            bub_val = "Live"
            self._bubble_dead_ticks = 0
        elif trades_arriving and trade_ct > 0:
            self._bubble_dead_ticks = getattr(
                self, '_bubble_dead_ticks', 0) + self._STATUS_PANEL_EVERY
            if self._bubble_dead_ticks >= self._BUBBLE_DEAD_THRESHOLD_TICKS:
                bub_val = f"DEAD:{failure_stage}"
                if self._bubble_dead_ticks == self._BUBBLE_DEAD_THRESHOLD_TICKS:
                    self._emit_bubbles_dead_warning(hm, trade_ct, buf_depth)
            else:
                bub_val = "Live"
        elif trades_arriving and trade_ct == 0:
            bub_val = f"DEAD:{failure_stage}"
            self._bubble_dead_ticks = getattr(
                self, '_bubble_dead_ticks', 0) + self._STATUS_PANEL_EVERY
            if self._bubble_dead_ticks == self._STATUS_PANEL_EVERY:
                self._emit_bubbles_dead_warning(hm, trade_ct, buf_depth)
        else:
            bub_val = "?" if not self._engine else "Off"
            self._bubble_dead_ticks = 0

        tl = 0
        if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
            tl = hm.chart_now - self._last_drained_trade_ts
        buf_val = f"{self._health.last_trade_buffer_depth}"
        if tl > 2000:
            buf_val += f" lag={tl // 1000}s"
        if self._trades_dropped_in_drain > 0:
            buf_val += f" err={self._trades_dropped_in_drain}"

        rows = [
            ("Mode", mode_val),
            ("Trade", t_label),
            ("Depth", d_label),
            ("Book", book_val),
            ("Heatmap", hm_val),
            ("Bubbles", bub_val),
            ("TrdBuf", buf_val),
            ("Strategy", ripple_val),
        ]

        self._status_panel.set_health(rows, title="Pipeline")
        self._health.status_updates += 1

        # --- Footer: compact Ripple summary --------------------------------
        m = self._ripple_metrics
        h = self._health

        ripple_txt = f"Ripple: {m.emitted} emit"
        total_suppr = m.total_suppressed
        if total_suppr:
            ripple_txt += f" | {total_suppr} suppr"
        if self._paper_engine:
            pm = self._paper_engine.metrics
            if pm.entries_filled or pm.exits_filled:
                ripple_txt += (
                    f" | Paper: {pm.entries_filled}E "
                    f"{pm.exits_filled}X "
                    f"PnL:{pm.realized_pnl:+.2f}")

        ripple_txt += (
            f" | bub:{bubble_ct}/{trade_ct}"
            f" buf:{buf_depth}"
            f" slc:{hm._heatmap_samples}")

        self._ripple_label.setText(ripple_txt)

    def _emit_bubbles_dead_warning(self, hm, trade_ct, buf_depth):
        """Log BUBBLES_DEAD_WHILE_TRADES_LIVE with full pipeline diagnostics."""
        diag = hm.get_bubble_diagnostics() if hasattr(hm, 'get_bubble_diagnostics') else {}
        stage = diag.get('failure_stage', 'UNKNOWN')

        sample_price = hm._trades[-1][1] if hm._trades else 0
        sample_ts = hm._trades[-1][0] if hm._trades else 0
        trade_lag = 0
        if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
            trade_lag = hm.chart_now - self._last_drained_trade_ts
        buf_overflow = max(
            0,
            self._trades_received_ws
            - self._health.trades_drained_total
            - len(self._trade_buffer))

        depth_trade_skew = hm._last_depth_ts - hm._last_trade_ts

        logger.warning(
            "BUBBLES_DEAD_WHILE_TRADES_LIVE stage=%s | "
            "trades=%d buf=%d deadTicks=%d | "
            "priceRange=[%.2f,%.2f] samplePrice=%.2f sampleTs=%d | "
            "chartNow=%d depthTs=%d tradeTs=%d depthTradeSkew=%dms | "
            "tradeLag=%dms bufOverflow=%d drainErrors=%d | "
            "filtTime=%d filtX=%d filtY=%d vis=%d | "
            "rMin=%.1f rMax=%.1f rAvg=%.1f | "
            "newestX=%.0f oldestX=%.0f pw=%d | "
            "p95=%.6g prunedFrame=%d prunedTotal=%d zeroFrames=%d | "
            "addedTotal=%d rejectedPrice=%d maxDeque=%d "
            "activeMinTs=%d activeMaxTs=%d",
            stage,
            trade_ct, buf_depth, self._bubble_dead_ticks,
            hm._price_min, hm._price_max, sample_price, sample_ts,
            hm.chart_now, hm._last_depth_ts, hm._last_trade_ts,
            depth_trade_skew,
            trade_lag, buf_overflow, self._trades_dropped_in_drain,
            diag.get('filtered_by_time', -1),
            diag.get('filtered_by_x', -1),
            diag.get('filtered_by_y', -1),
            diag.get('visible', -1),
            diag.get('min_radius', -1),
            diag.get('max_radius', -1),
            diag.get('avg_radius', -1),
            diag.get('newest_x', -1),
            diag.get('oldest_x', -1),
            diag.get('pw', -1),
            diag.get('p95_size', -1),
            diag.get('trades_pruned_this_frame', -1),
            diag.get('trades_pruned_total', -1),
            diag.get('consecutive_zero_frames', -1),
            diag.get('trades_added_total', -1),
            diag.get('trades_rejected_price', -1),
            diag.get('max_deque_depth', -1),
            diag.get('active_min_ts', -1),
            diag.get('active_max_ts', -1),
        )

    def _resync_depth_snapshot(self, symbol: str):
        """Re-fetch a depth snapshot from Binance REST and apply it.

        Called from a daemon thread when the order book has been empty for
        too many ticks.  Reuses the same logic as the initial snapshot
        fetch to reinitialize the C++ order book.
        """
        try:
            url = (f"https://fapi.binance.com/fapi/v1/depth"
                   f"?symbol={symbol}&limit=1000")
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()

            if not self._engine:
                return

            import orderflow_engine as ofe

            u = ofe.DepthUpdate()
            u.timestamp = int(time.time() * 1000)
            u.is_snapshot = True
            u.first_update_id = data.get("lastUpdateId", 0)
            u.final_update_id = u.first_update_id
            for p_str, q_str in data.get("bids", []):
                u.add_bid(float(p_str), float(q_str))
            for p_str, q_str in data.get("asks", []):
                u.add_ask(float(p_str), float(q_str))
            self._engine.process_depth(u)

            logger.info("Depth snapshot resync applied — %d bids, %d asks",
                        len(data.get("bids", [])), len(data.get("asks", [])))
        except Exception as e:
            logger.error("Depth snapshot resync failed: %s", e)
        finally:
            self._book_resync_pending = False

    def _recompute_volume_profile(self):
        hm_min = self._heatmap._price_min
        hm_max = self._heatmap._price_max

        if (hm_max <= hm_min or not math.isfinite(hm_min)
                or not math.isfinite(hm_max)):
            if self._heatmap._trades:
                last_trade_price = self._heatmap._trades[-1][1]
                margin = last_trade_price * 0.002
                hm_min = last_trade_price - margin
                hm_max = last_trade_price + margin
            else:
                return

        if not self._heatmap._slices and not self._heatmap._trades:
            return

        now_ts = self._heatmap.chart_now
        if now_ts <= 0:
            return
        t_start = now_ts - self._heatmap._visible_window_ms
        trade_count = len(self._heatmap._trades)

        # Skip recompute if inputs are unchanged since last run.
        vp_key = (round(hm_min, 4), round(hm_max, 4),
                  t_start // 500, trade_count // 50)
        if vp_key == getattr(self, '_vp_cache_key', None):
            return
        self._vp_cache_key = vp_key
        self._health.vp_recomputes += 1

        pr = hm_max - hm_min
        if pr <= 0:
            return
        n_bins = 200
        bin_size = pr / n_bins
        inv_bin = 1.0 / bin_size

        bins_buy = [0.0] * n_bins
        bins_sell = [0.0] * n_bins

        for i in range(trade_count - 1, -1, -1):
            ts, price, qty, is_buy = self._heatmap._trades[i]
            if ts < t_start:
                break
            row = int((price - hm_min) * inv_bin)
            if 0 <= row < n_bins:
                if is_buy:
                    bins_buy[row] += qty
                else:
                    bins_sell[row] += qty

        profile_data = []
        poc_price = 0.0
        poc_vol = 0.0
        for i in range(n_bins):
            bv = bins_buy[i]
            sv = bins_sell[i]
            total = bv + sv
            if total <= 0:
                continue
            p = hm_min + (i + 0.5) * bin_size
            profile_data.append((p, total, bv, sv))
            if total > poc_vol:
                poc_vol = total
                poc_price = p

        self._volume_profile.set_profile(
            profile_data, poc_price, hm_min, hm_max
        )

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
