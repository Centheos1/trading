"""Strategy dashboard view — observability / diagnostics screen.

Composes strategy diagnostics, signal log, account info, and a rolling
history panel into a dedicated view tab.  Reads from MarketState for
strategy snapshots, snapshot history, and signals.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, List, Tuple

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QSplitter, QLabel,
    QFrame,
)
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QFont, QColor, QPainter, QPen, QPainterPath

from ui.strategy_panel import StrategyDiagnosticsPanel, _attr
from ui.trade_blotter import TradeBlotter
from ui.account_panel import AccountPanel

if TYPE_CHECKING:
    from ui.market_state import MarketState


# ──────────────────────────────────────────────────── shared colors

_BG = QColor(12, 14, 28)
_BORDER = QColor(25, 28, 45)
_AXIS = QColor(40, 45, 70)
_TEXT_MUTED = QColor(120, 125, 150)
_TEXT_HI = QColor(200, 205, 230)
_TEXT_VAL = QColor(180, 185, 215)

# Regime / bias colors (match StrategyDiagnosticsPanel where possible)
_BIAS_COLOR = {
    "LONG":    QColor(40, 220, 130),
    "SHORT":   QColor(230, 80, 80),
    "NEUTRAL": QColor(140, 145, 175),
}
_REGIME_COLOR = {
    "MEAN_REVERSION": QColor(40, 220, 130),
    "BREAKOUT":       QColor(80, 160, 255),
    "BREAKDOWN":      QColor(230, 80, 80),
    "NEUTRAL":        QColor(140, 145, 175),
}

_RISK_COLOR = QColor(255, 165, 60)
_PNL_POS = QColor(40, 220, 130)
_PNL_NEG = QColor(230, 80, 80)
_PNL_NEUTRAL = QColor(200, 205, 230)


def _enum_str(value, default: str = "?") -> str:
    """Robustly stringify an enum or stringable value to its name."""
    if value is None:
        return default
    s = str(value)
    return s.split(".")[-1] if "." in s else s


# ──────────────────────────────────────────── _StrategyHistoryPanel (C2)


class _StrategyHistoryPanel(QWidget):
    """Rolling time-series mini-chart of Tide / Wave / risk / PnL.

    Reads MarketState.snapshot_history (a deque of (ts_ms, snapshot)
    tuples) and paints two stacked rows in pure QPainter:

        Row 1  – Wave regime (top half) and Tide bias (bottom half)
                 as horizontal coloured bands per snapshot index.
        Row 2  – risk_budget_pct (orange line) and unrealized_pnl
                 (green/red line, normalised to fit row).

    No external charting library — the panel is self-contained and runs
    in the existing paint cycle.
    """

    _ROW1_FRAC = 0.35  # band row height fraction
    _PAD_X = 6
    _PAD_Y = 4
    _LABEL_W = 64

    def __init__(self, market_state: MarketState, parent=None):
        super().__init__(parent)
        self._ms = market_state
        self.setMinimumHeight(110)
        self.setMaximumHeight(220)

        # Per-paint-friendly cache. The deque is appended to roughly every
        # 500 ms (Phase 11 cadence gate), but the widget's paintEvent can
        # also fire on resize / expose / explicit `update()` calls. Without
        # this cache, every paint walks all N (≤600) snapshots through
        # multiple `_attr()` reflective lookups — that's the regression
        # that pushed live-tick avg from 11 ms to 145 ms.
        self._cache_key: tuple | None = None
        self._cache_wave_colors: List[QColor] = []
        self._cache_tide_colors: List[QColor] = []
        self._cache_risk_pcts: List[float] = []
        self._cache_pnls: List[float] = []
        self._cache_t0: int = 0
        self._cache_t1: int = 0
        self._cache_n: int = 0

    def invalidate_cache(self) -> None:
        """Drop the per-paint cache (call after snapshot_history mutation)."""
        self._cache_key = None

    def _refresh_cache(self, history: list) -> None:
        """Rebuild the per-snapshot series cache from `history`.

        Cheap to call: O(N) once per snapshot append, then O(1) reads
        (paintEvent reuses the cached lists across resize / expose paints).
        """
        n = len(history)
        wave_colors: List[QColor] = [_REGIME_COLOR["NEUTRAL"]] * n
        tide_colors: List[QColor] = [_BIAS_COLOR["NEUTRAL"]] * n
        risk_pcts: List[float] = [0.0] * n
        pnls: List[float] = [0.0] * n
        for i, (_ts, snap) in enumerate(history):
            wave = _enum_str(_attr(snap, "wave.regime", "NEUTRAL"))
            tide = _enum_str(_attr(snap, "tide.bias", "NEUTRAL"))
            wave_colors[i] = _REGIME_COLOR.get(wave, _REGIME_COLOR["NEUTRAL"])
            tide_colors[i] = _BIAS_COLOR.get(tide, _BIAS_COLOR["NEUTRAL"])
            consumed = _attr(snap, "risk.consumed_es", 0.0) or 0.0
            budget = _attr(snap, "risk.es_budget", 0.0) or 0.0
            pct = (consumed / budget * 100.0) if budget > 0 else 0.0
            risk_pcts[i] = max(0.0, min(150.0, pct))
            pnls[i] = float(_attr(snap, "trade.unrealized_pnl", 0.0) or 0.0)
        self._cache_wave_colors = wave_colors
        self._cache_tide_colors = tide_colors
        self._cache_risk_pcts = risk_pcts
        self._cache_pnls = pnls
        self._cache_t0 = int(history[0][0]) if n else 0
        self._cache_t1 = int(history[-1][0]) if n else 0
        self._cache_n = n

    def _ensure_cache(self, history: list) -> None:
        """Rebuild the cache only when (len, last_ts) changes.

        Using the deque length plus the most-recent timestamp as the cache
        key catches every realistic mutation: appends bump both, and the
        deque's bounded `maxlen` means once full an append still bumps the
        last timestamp (and the first one — but len stays the same, which
        is fine because we still detect change via `last_ts`).
        """
        n = len(history)
        if n == 0:
            self._cache_key = (0, 0)
            self._cache_n = 0
            return
        last_ts = int(history[-1][0])
        key = (n, last_ts)
        if key == self._cache_key:
            return
        self._refresh_cache(history)
        self._cache_key = key

    # paintEvent reads MarketState.snapshot_history live; update() invokes it.

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            self._paint(p)
        finally:
            p.end()

    def _paint(self, p: QPainter):
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, _BG)
        p.setPen(QPen(_BORDER, 1))
        p.drawRect(0, 0, w - 1, h - 1)

        history: list = list(self._ms.snapshot_history)
        self._ensure_cache(history)
        n = self._cache_n

        # Title
        p.setFont(QFont("Menlo", 9, QFont.Bold))
        p.setPen(_TEXT_HI)
        p.drawText(self._PAD_X, 14, f"Strategy History ({n})")

        if n < 2:
            p.setFont(QFont("Menlo", 8))
            p.setPen(_TEXT_MUTED)
            p.drawText(self._PAD_X, 32,
                       "waiting for snapshots\u2026 (need \u22652 entries)")
            return

        # Plot region: leave space for title + per-row labels on the left.
        plot_x = self._PAD_X + self._LABEL_W
        plot_y = 22
        plot_w = max(20, w - plot_x - self._PAD_X)
        plot_h = max(60, h - plot_y - self._PAD_Y - 14)

        row1_h = int(plot_h * self._ROW1_FRAC)
        row2_h = plot_h - row1_h - 2

        # Row 1: Tide / Wave bands (uses cached colors)
        self._draw_row1_bands(p, n, plot_x, plot_y, plot_w, row1_h)

        # Row 2: risk + PnL (uses cached numeric series)
        row2_y = plot_y + row1_h + 2
        self._draw_row2_lines(p, n, plot_x, row2_y, plot_w, row2_h)

        # Time-axis label (just oldest / newest)
        span_s = max(0.0, (self._cache_t1 - self._cache_t0) / 1000.0)
        p.setFont(QFont("Menlo", 7))
        p.setPen(_TEXT_MUTED)
        p.drawText(plot_x, h - 4,
                   f"window: {span_s:6.1f}s  ({n} samples)")

    def _draw_row1_bands(self, p: QPainter, n: int, x: int, y: int,
                         w: int, h: int):
        if n < 1 or w <= 0 or h <= 4:
            return
        wave_colors = self._cache_wave_colors
        tide_colors = self._cache_tide_colors
        # Split row1 into two stripes
        half = max(2, h // 2)
        stripe_top = y
        stripe_bot = y + half + 1
        stripe_h_top = half
        stripe_h_bot = h - half - 1

        # Background fill for stripes
        p.fillRect(x, stripe_top, w, stripe_h_top, _BG)
        p.fillRect(x, stripe_bot, w, stripe_h_bot, _BG)

        # Labels
        p.setFont(QFont("Menlo", 7))
        p.setPen(_TEXT_MUTED)
        p.drawText(self._PAD_X, stripe_top + stripe_h_top - 2, "Wave")
        p.drawText(self._PAD_X, stripe_bot + stripe_h_bot - 2, "Tide")

        # Per-sample column width
        col_w = w / n
        cw = max(1.0, col_w + 1.0)  # +1 px overlap to avoid hairline gaps
        for i in range(n):
            cx = x + i * col_w
            p.fillRect(QRectF(cx, stripe_top, cw, stripe_h_top),
                       wave_colors[i])
            p.fillRect(QRectF(cx, stripe_bot, cw, stripe_h_bot),
                       tide_colors[i])

    def _draw_row2_lines(self, p: QPainter, n: int, x: int, y: int,
                         w: int, h: int):
        if n < 2 or w <= 0 or h <= 6:
            return
        risk_pcts = self._cache_risk_pcts
        pnls = self._cache_pnls

        # Frame the row
        p.setPen(QPen(_AXIS, 1))
        p.drawRect(x, y, w, h)

        # Labels
        p.setFont(QFont("Menlo", 7))
        p.setPen(_RISK_COLOR)
        p.drawText(self._PAD_X, y + 10, "ES %")
        p.setPen(_PNL_NEUTRAL)
        p.drawText(self._PAD_X, y + h - 2, "uPnL")

        # ─── Risk line: always 0..100% reference (we clip overshoot above)
        risk_max = max(100.0, max(risk_pcts) or 100.0)
        risk_path = QPainterPath()
        for i, v in enumerate(risk_pcts):
            cx = x + (w * i) / max(n - 1, 1)
            cy = y + h - (v / risk_max) * h
            if i == 0:
                risk_path.moveTo(cx, cy)
            else:
                risk_path.lineTo(cx, cy)
        pen = QPen(_RISK_COLOR, 1)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.drawPath(risk_path)

        # 100% reference line
        ref_y = y + h - (100.0 / risk_max) * h
        ref_pen = QPen(_RISK_COLOR.darker(180), 1, Qt.DotLine)
        ref_pen.setCosmetic(True)
        p.setPen(ref_pen)
        p.drawLine(x, int(ref_y), x + w, int(ref_y))

        # ─── PnL line: auto-scale, centred on 0
        pnl_min = min(pnls)
        pnl_max = max(pnls)
        span = max(abs(pnl_min), abs(pnl_max))
        if span <= 0:
            span = 1.0
        # Centre 0 mid-row
        mid_y = y + h * 0.5
        pnl_path = QPainterPath()
        for i, v in enumerate(pnls):
            cx = x + (w * i) / max(n - 1, 1)
            cy = mid_y - (v / span) * (h * 0.45)
            if i == 0:
                pnl_path.moveTo(cx, cy)
            else:
                pnl_path.lineTo(cx, cy)
        pnl_color = _PNL_POS if pnls[-1] > 0 else (
            _PNL_NEG if pnls[-1] < 0 else _PNL_NEUTRAL)
        pen2 = QPen(pnl_color, 1)
        pen2.setCosmetic(True)
        p.setPen(pen2)
        p.drawPath(pnl_path)

        # Zero line
        zero_pen = QPen(QColor(60, 65, 90), 1, Qt.DotLine)
        zero_pen.setCosmetic(True)
        p.setPen(zero_pen)
        p.drawLine(x, int(mid_y), x + w, int(mid_y))


# ────────────────────────────────────────── _RippleStateTable (C3)


class _RippleStateTable(QFrame):
    """Compact grid showing the *current* Ripple / trade state."""

    _LABELS = [
        ("Lifecycle", "trade.state"),
        ("Archetype", "trade.archetype"),
        ("Entry",     "trade.entry_price"),
        ("Stop",      "trade.stop_price"),
        ("Target",    "trade.target_price"),
        ("Hold",      "trade.hold_time_ms"),
        ("uPnL",      "trade.unrealized_pnl"),
        ("ES Used",   None),  # special: derived from risk
    ]

    def __init__(self, market_state: MarketState, parent=None):
        super().__init__(parent)
        self._ms = market_state
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "QFrame { background: #0f0f19; border: 1px solid #22243a; }"
            "QLabel { color: #b4b4c8; font-family: Menlo; font-size: 10px; }"
        )
        self.setMinimumHeight(72)
        self.setMaximumHeight(110)

        layout = QGridLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(2)

        title = QLabel("Ripple Trade State")
        title.setStyleSheet("color: #b4b4c8; font-weight: bold;")
        layout.addWidget(title, 0, 0, 1, 4)

        # 4 columns x 2 rows of label/value pairs (8 fields)
        self._value_labels: dict[str, QLabel] = {}
        for i, (display, _path) in enumerate(self._LABELS):
            row = 1 + (i // 4)
            col_pair = i % 4
            col = col_pair * 2
            lbl = QLabel(display)
            lbl.setStyleSheet("color: #8888aa;")
            val = QLabel("\u2014")
            val.setStyleSheet("color: #c8c8d8;")
            val.setMinimumWidth(60)
            layout.addWidget(lbl, row, col)
            layout.addWidget(val, row, col + 1)
            self._value_labels[display] = val

    def update_from_state(self):
        snap = self._ms.strategy_snapshot
        if snap is None:
            for v in self._value_labels.values():
                v.setText("\u2014")
            return

        # Lifecycle
        lc = _enum_str(_attr(snap, "trade.state", "?"))
        self._value_labels["Lifecycle"].setText(lc)
        # Archetype
        arch = _enum_str(_attr(snap, "trade.archetype", ""), default="\u2014")
        self._value_labels["Archetype"].setText(arch or "\u2014")
        # Prices
        for key, path in (("Entry", "trade.entry_price"),
                          ("Stop", "trade.stop_price"),
                          ("Target", "trade.target_price")):
            v = _attr(snap, path, 0.0) or 0.0
            self._value_labels[key].setText(
                f"{v:.4f}" if v else "\u2014")
        # Hold time MM:SS
        hold_ms = _attr(snap, "trade.hold_time_ms", 0) or 0
        if hold_ms > 0:
            mins, secs = divmod(int(hold_ms / 1000), 60)
            self._value_labels["Hold"].setText(f"{mins:02d}:{secs:02d}")
        else:
            self._value_labels["Hold"].setText("\u2014")
        # uPnL with sign
        upnl = _attr(snap, "trade.unrealized_pnl", 0.0) or 0.0
        self._value_labels["uPnL"].setText(
            f"{upnl:+.4f}" if upnl else "0")
        if upnl > 0:
            self._value_labels["uPnL"].setStyleSheet("color: #28dc82;")
        elif upnl < 0:
            self._value_labels["uPnL"].setStyleSheet("color: #e65050;")
        else:
            self._value_labels["uPnL"].setStyleSheet("color: #c8c8d8;")
        # ES used
        consumed = _attr(snap, "risk.consumed_es", 0.0) or 0.0
        budget = _attr(snap, "risk.es_budget", 0.0) or 0.0
        if budget > 0:
            pct = consumed / budget * 100.0
            self._value_labels["ES Used"].setText(f"{pct:.0f}%")
            color = "#e65050" if pct > 80 else (
                "#ffa53c" if pct > 50 else "#28dc82")
            self._value_labels["ES Used"].setStyleSheet(f"color: {color};")
        else:
            self._value_labels["ES Used"].setText("\u2014")
            self._value_labels["ES Used"].setStyleSheet("color: #c8c8d8;")


# ────────────────────────────────────────── StrategyDashboardView


class StrategyDashboardView(QWidget):
    """Composite strategy view: diagnostics + signal log + account + history."""

    def __init__(self, market_state: MarketState, parent=None):
        super().__init__(parent)
        self._ms = market_state

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        title = QLabel("Strategy Dashboard")
        title.setFont(QFont("Menlo", 11, QFont.Bold))
        title.setStyleSheet("color: #b4b4c8; background: #0f0f19; padding: 4px;")
        layout.addWidget(title)

        splitter = QSplitter(Qt.Vertical)

        top_splitter = QSplitter(Qt.Horizontal)
        self._strategy_panel = StrategyDiagnosticsPanel()
        self._account_panel = AccountPanel()
        top_splitter.addWidget(self._strategy_panel)
        top_splitter.addWidget(self._account_panel)
        top_splitter.setStretchFactor(0, 1)
        top_splitter.setStretchFactor(1, 1)

        self._blotter = TradeBlotter()

        # C2: rolling history mini-chart (replaces _DiagnosticsPlaceholder)
        self._history_panel = _StrategyHistoryPanel(market_state)
        # C3: current Ripple state grid
        self._ripple_table = _RippleStateTable(market_state)

        # Bottom area: history above table
        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(2)
        bottom_layout.addWidget(self._history_panel)
        bottom_layout.addWidget(self._ripple_table)

        splitter.addWidget(top_splitter)
        splitter.addWidget(self._blotter)
        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)

        layout.addWidget(splitter)

    # ---- public API ----

    @property
    def strategy_panel(self) -> StrategyDiagnosticsPanel:
        return self._strategy_panel

    @property
    def blotter(self) -> TradeBlotter:
        return self._blotter

    @property
    def account_panel(self) -> AccountPanel:
        return self._account_panel

    @property
    def history_panel(self) -> _StrategyHistoryPanel:
        return self._history_panel

    @property
    def ripple_table(self) -> _RippleStateTable:
        return self._ripple_table

    def update_from_state(self):
        """Pull latest data from MarketState and push to child widgets."""
        snap = self._ms.strategy_snapshot
        if snap is not None:
            self._strategy_panel.update_snapshot(snap)
        self._strategy_panel.set_strategy_state(self._ms.strategy_ui_state)
        self._ripple_table.update_from_state()
        self._history_panel.update()
