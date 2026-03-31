"""Strategy dashboard view — observability / diagnostics screen.

Composes strategy diagnostics, signal log, and account info into a
dedicated view tab.  Reads from MarketState for strategy snapshots
and signals.  Provides placeholder areas for future diagnostics
(correlation heatmaps, state-variable plots, etc.).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QSplitter, QLabel,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QColor, QPainter, QPen

from ui.strategy_panel import StrategyDiagnosticsPanel
from ui.trade_blotter import TradeBlotter
from ui.account_panel import AccountPanel

if TYPE_CHECKING:
    from ui.market_state import MarketState


class _DiagnosticsPlaceholder(QWidget):
    """Placeholder for future strategy diagnostics (correlation heatmaps, etc.)."""

    _BG = QColor(12, 14, 28)
    _BORDER = QColor(25, 28, 45)
    _TEXT = QColor(80, 85, 105)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(80)

    def paintEvent(self, event):
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, self._BG)
        p.setPen(QPen(self._BORDER, 1))
        p.drawRect(0, 0, w - 1, h - 1)
        p.setPen(self._TEXT)
        p.setFont(QFont("Menlo", 9))
        p.drawText(8, 18, "Diagnostics")
        p.setFont(QFont("Menlo", 8))
        p.drawText(8, 36, "Correlation heatmaps, state plots — coming soon")
        p.end()


class StrategyDashboardView(QWidget):
    """Composite strategy view: diagnostics + signal log + account + placeholder."""

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

        self._diagnostics_placeholder = _DiagnosticsPlaceholder()

        splitter.addWidget(top_splitter)
        splitter.addWidget(self._blotter)
        splitter.addWidget(self._diagnostics_placeholder)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 1)

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

    def update_from_state(self):
        """Pull latest data from MarketState and push to child widgets."""
        snap = self._ms.strategy_snapshot
        if snap is not None:
            self._strategy_panel.update_snapshot(snap)
        self._strategy_panel.set_strategy_state(self._ms.strategy_ui_state)
