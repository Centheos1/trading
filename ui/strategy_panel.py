"""
Phase 6 + Phase 2: Strategy diagnostics panel.

Displays strategy UI state badge, Tide, Wave, Ripple state,
trade lifecycle, archetype, stop/target, hold time, risk budget,
and liquidity map summary in a compact read-only panel.
"""

from PySide6.QtWidgets import QWidget
from PySide6.QtGui import QColor, QPainter, QPen, QFont

from execution.models import StrategyUIState


_STATE_BADGE = {
    StrategyUIState.DISARMED:       ("OFF",        QColor(100, 100, 120)),
    StrategyUIState.ARMING:         ("ARMING\u2026",  QColor(200, 180, 60)),
    StrategyUIState.ARMED_WAITING:  ("WATCHING",   QColor(80, 200, 120)),
    StrategyUIState.ARMED_ACTIVE:   ("ACTIVE",     QColor(30, 220, 80)),
    StrategyUIState.ARMED_EXITING:  ("EXITING",    QColor(220, 150, 40)),
    StrategyUIState.ARMED_COOLDOWN: ("COOLDOWN",   QColor(80, 140, 220)),
    StrategyUIState.DISARMING:      ("DISARMING\u2026", QColor(200, 180, 60)),
}


class StrategyDiagnosticsPanel(QWidget):
    """Compact diagnostics panel showing strategy state from StrategySnapshot."""

    _BG = QColor(12, 14, 28)
    _BORDER = QColor(25, 28, 45)
    _LABEL_COLOR = QColor(80, 85, 105)
    _VALUE_COLOR = QColor(160, 165, 185)
    _ACCENT_GREEN = QColor(80, 200, 120)
    _ACCENT_RED = QColor(200, 80, 80)
    _ACCENT_YELLOW = QColor(200, 180, 60)
    _ACCENT_BLUE = QColor(80, 140, 220)
    _SECTION_COLOR = QColor(110, 115, 140)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMaximumHeight(350)

        self._rows = []
        self._title = "Strategy"
        self._strategy_state = StrategyUIState.DISARMED

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, self._BG)
        p.setPen(QPen(self._BORDER, 1))
        p.drawRect(0, 0, w - 1, h - 1)

        font_label = QFont("Menlo", 9)
        font_value = QFont("Menlo", 9, QFont.Bold)
        font_badge = QFont("Menlo", 10, QFont.Bold)
        y = 6

        # Title
        p.setFont(QFont("Menlo", 10, QFont.Bold))
        p.setPen(QPen(self._SECTION_COLOR))
        p.drawText(8, y + 10, self._title)
        y += 18

        # Strategy state badge (full-width row)
        badge_text, badge_color = _STATE_BADGE.get(
            self._strategy_state,
            ("?", self._VALUE_COLOR),
        )
        p.setFont(font_badge)
        p.setPen(QPen(badge_color))
        p.drawText(8, y + 10, f"\u25cf {badge_text}")
        y += 18

        col_width = max(w // 2, 120)

        for i, (label, value, color) in enumerate(self._rows):
            col = i % 2
            row = i // 2
            x = 8 + col * col_width
            ry = y + row * 14

            if ry + 14 > h:
                break

            p.setFont(font_label)
            p.setPen(QPen(self._LABEL_COLOR))
            p.drawText(x, ry + 10, f"{label}:")

            p.setFont(font_value)
            p.setPen(QPen(color or self._VALUE_COLOR))
            p.drawText(x + 75, ry + 10, str(value))

        p.end()

    def set_strategy_state(self, state: StrategyUIState):
        if state != self._strategy_state:
            self._strategy_state = state
            self.update()

    def update_snapshot(self, snap):
        """Update display from a StrategySnapshot (C++ or Python)."""
        rows = []

        # Tide bias
        tide_bias = _attr(snap, "tide.bias", "?")
        bias_str = str(tide_bias).split(".")[-1]
        bias_color = {
            "LONG": self._ACCENT_GREEN,
            "SHORT": self._ACCENT_RED,
            "NEUTRAL": self._VALUE_COLOR,
        }.get(bias_str, self._VALUE_COLOR)
        rows.append(("Tide", bias_str, bias_color))

        # Wave regime
        regime = _attr(snap, "wave.regime", "?")
        regime_color = {
            "MEAN_REVERSION": self._ACCENT_GREEN,
            "BREAKOUT": self._ACCENT_BLUE,
            "BREAKDOWN": self._ACCENT_RED,
            "NEUTRAL": self._VALUE_COLOR,
        }.get(str(regime), self._VALUE_COLOR)
        rows.append(("Wave", str(regime).split(".")[-1], regime_color))

        # Wave trend efficiency
        eta = _attr(snap, "wave.trend_efficiency", 0.0)
        rows.append(("\u03b7", f"{eta:.3f}" if isinstance(eta, float) else "?", self._VALUE_COLOR))

        # Ripple liquidity state
        liq = _attr(snap, "ripple.liquidity_state", "?")
        rows.append(("Liquidity", str(liq).split(".")[-1], self._ACCENT_BLUE))

        # Microprice
        micro = _attr(snap, "ripple.microprice", 0.0)
        rows.append(("\u00b5Price", f"{micro:.4f}" if micro else "\u2014", self._VALUE_COLOR))

        # Trade lifecycle state
        trade_state = _attr(snap, "trade.state", "?")
        ts_color = self._ACCENT_GREEN if "CONFIRM" in str(trade_state) or "EXPAN" in str(trade_state) else self._VALUE_COLOR
        rows.append(("Trade", str(trade_state).split(".")[-1], ts_color))

        # Trade archetype
        archetype = _attr(snap, "trade.archetype", "")
        arch_str = str(archetype).split(".")[-1] if archetype else "\u2014"
        rows.append(("Archetype", arch_str, self._VALUE_COLOR))

        # Unrealized PnL
        upnl = _attr(snap, "trade.unrealized_pnl", 0.0)
        pnl_color = self._ACCENT_GREEN if upnl > 0 else (self._ACCENT_RED if upnl < 0 else self._VALUE_COLOR)
        rows.append(("uPnL", f"{upnl:+.4f}" if isinstance(upnl, (int, float)) else "\u2014", pnl_color))

        # Stop / Target
        stop = _attr(snap, "trade.stop_price", 0.0)
        target = _attr(snap, "trade.target_price", 0.0)
        rows.append(("Stop", f"{stop:.2f}" if stop else "\u2014", self._ACCENT_RED))
        rows.append(("Target", f"{target:.2f}" if target else "\u2014", self._ACCENT_GREEN))

        # Hold time
        hold_ms = _attr(snap, "trade.hold_time_ms", 0)
        if hold_ms and isinstance(hold_ms, (int, float)) and hold_ms > 0:
            mins, secs = divmod(int(hold_ms / 1000), 60)
            rows.append(("Hold", f"{mins:02d}:{secs:02d}", self._VALUE_COLOR))
        else:
            rows.append(("Hold", "\u2014", self._VALUE_COLOR))

        # Risk
        consumed = _attr(snap, "risk.consumed_es", 0.0)
        budget = _attr(snap, "risk.es_budget", 0.0)
        if budget > 0:
            pct = consumed / budget * 100
            risk_color = self._ACCENT_RED if pct > 80 else (self._ACCENT_YELLOW if pct > 50 else self._ACCENT_GREEN)
            rows.append(("ES Used", f"{pct:.0f}%", risk_color))
        else:
            rows.append(("ES Used", "\u2014", self._VALUE_COLOR))

        # Imbalance
        imb = _attr(snap, "ripple.imbalance", 0.0)
        rows.append(("Imbal", f"{imb:+.3f}" if isinstance(imb, (int, float)) else "\u2014", self._VALUE_COLOR))

        self._rows = rows
        self.update()

    def clear(self):
        self._rows = []
        self._strategy_state = StrategyUIState.DISARMED
        self.update()


def _attr(obj, path, default=None):
    """Safely traverse dotted attribute path."""
    parts = path.split(".")
    cur = obj
    for part in parts:
        try:
            cur = getattr(cur, part)
        except AttributeError:
            return default
    return cur
