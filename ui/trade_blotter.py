"""Signal log table with model-based rendering, layer-based filtering,
and Qt's built-in row virtualization via QTableView + QAbstractTableModel.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import Deque, List, Optional, Set

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QHeaderView,
    QLabel, QComboBox, QCheckBox, QPushButton,
)
from PySide6.QtCore import Qt, QAbstractTableModel, QModelIndex, QSortFilterProxyModel
from PySide6.QtGui import QColor, QFont

from execution.models import SignalEntry, SignalCategory, _CATEGORY_LAYER_MAP

_COLUMNS = ["Time", "Layer", "Category", "Type", "Side", "Price",
            "Strength", "PnL", "Description"]
_COL_COUNT = len(_COLUMNS)

_MAX_ROWS = 2000

# Phase 8C — threshold below which realized_pnl is treated as zero for display
_PNL_DISPLAY_THRESHOLD = 1e-8

_CAT_COLORS = {
    SignalCategory.TIDE:                 QColor(210, 170, 60),
    SignalCategory.WAVE:                 QColor(100, 130, 200),
    SignalCategory.RIPPLE_ENTRY:         QColor(30, 200, 100),
    SignalCategory.RIPPLE_EXIT:          QColor(255, 180, 60),
    SignalCategory.RIPPLE_PREPARE:       QColor(100, 160, 220),
    SignalCategory.RIPPLE_CANCEL:        QColor(255, 80, 80),
    SignalCategory.RIPPLE_REARM:         QColor(160, 140, 220),
    SignalCategory.TRADE_LIFECYCLE:      QColor(220, 240, 255),
    SignalCategory.EXECUTION:            QColor(255, 215, 0),
    SignalCategory.LEGACY_RAW:           QColor(130, 130, 150),
    SignalCategory.CONTEXT:              QColor(120, 120, 150),
    SignalCategory.DIAGNOSTIC:           QColor(100, 100, 130),
    SignalCategory.STRATEGY_ARM:         QColor(80, 200, 120),
    SignalCategory.STRATEGY_DISARM:      QColor(200, 100, 80),
    SignalCategory.STRATEGY_STATE_CHANGE: QColor(140, 160, 220),
}

_BOLD_FONT = QFont("Menlo", 11, QFont.Bold)

# ---- layer-based filter sets ----

_FILTER_STRATEGY = {
    SignalCategory.TIDE,
    SignalCategory.WAVE,
    SignalCategory.STRATEGY_ARM,
    SignalCategory.STRATEGY_DISARM,
    SignalCategory.STRATEGY_STATE_CHANGE,
    SignalCategory.RIPPLE_PREPARE,
    SignalCategory.RIPPLE_ENTRY,
    SignalCategory.RIPPLE_EXIT,
    SignalCategory.RIPPLE_CANCEL,
    SignalCategory.RIPPLE_REARM,
    SignalCategory.TRADE_LIFECYCLE,
    SignalCategory.CONTEXT,
    SignalCategory.EXECUTION,
}

_FILTER_TRADE = {
    SignalCategory.TRADE_LIFECYCLE,
    SignalCategory.EXECUTION,
}

_FILTER_RIPPLE = {
    SignalCategory.RIPPLE_PREPARE,
    SignalCategory.RIPPLE_ENTRY,
    SignalCategory.RIPPLE_EXIT,
    SignalCategory.RIPPLE_CANCEL,
    SignalCategory.RIPPLE_REARM,
}

_FILTER_RAW = {
    SignalCategory.LEGACY_RAW,
    SignalCategory.CONTEXT,
    SignalCategory.DIAGNOSTIC,
}

# Phase 8C — debug filter: raw legacy + diagnostics (excludes CONTEXT which
# is strategically meaningful and visible in the Strategy filter).
_FILTER_DEBUG = {
    SignalCategory.LEGACY_RAW,
    SignalCategory.DIAGNOSTIC,
}

_CONTEXT_SIGNAL_PREFIXES = (
    "EXHAUSTION_", "ABSORPTION_", "SWEEP_", "FLIP_",
)


class _SignalTableModel(QAbstractTableModel):
    """Flat list model backed by a bounded deque of SignalEntry."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data: Deque[SignalEntry] = deque(maxlen=_MAX_ROWS)

    def rowCount(self, parent=QModelIndex()):
        return len(self._data)

    def columnCount(self, parent=QModelIndex()):
        return _COL_COUNT

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        col = index.column()
        if row < 0 or row >= len(self._data):
            return None
        entry = self._data[row]

        if role == Qt.DisplayRole:
            return self._display(entry, col)
        if role == Qt.ForegroundRole:
            return self._foreground(entry, col)
        if role == Qt.FontRole:
            return self._font(entry, col)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        if role == Qt.UserRole:
            return entry
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return _COLUMNS[section] if section < _COL_COUNT else ""
        return None

    def append(self, entry: SignalEntry) -> None:
        if len(self._data) >= _MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._data.popleft()
            self.endRemoveRows()
        pos = len(self._data)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._data.append(entry)
        self.endInsertRows()

    def clear_all(self) -> None:
        self.beginResetModel()
        self._data.clear()
        self.endResetModel()

    @staticmethod
    def _display(e: SignalEntry, col: int) -> str:
        if col == 0:
            dt = datetime.fromtimestamp(e.timestamp / 1000, tz=timezone.utc)
            return dt.strftime("%H:%M:%S.%f")[:-3]
        if col == 1:
            return _CATEGORY_LAYER_MAP.get(e.category, e.source)
        if col == 2:
            return e.category.value
        if col == 3:
            return e.signal_type
        if col == 4:
            return e.side
        if col == 5:
            return f"{e.price:.2f}" if e.price else ""
        if col == 6:
            return f"{e.strength:.2f}" if e.strength else ""
        if col == 7:
            # Phase 8C — PnL column: show +x.xx / -x.xx when non-zero
            pnl = getattr(e, "realized_pnl", 0.0)
            if abs(pnl) > _PNL_DISPLAY_THRESHOLD:
                return f"{pnl:+.2f}"
            return ""
        if col == 8:
            desc = e.description
            if e.state_summary:
                desc = f"[{e.state_summary}] {desc}"
            return desc
        return ""

    @staticmethod
    def _foreground(e: SignalEntry, col: int) -> Optional[QColor]:
        if col in (1, 2, 3):
            return _CAT_COLORS.get(e.category)
        if col == 4:
            if e.side == "BUY":
                return QColor(30, 200, 100)
            elif e.side == "SELL":
                return QColor(255, 80, 80)
        return None

    @staticmethod
    def _font(e: SignalEntry, col: int) -> Optional[QFont]:
        if e.category == SignalCategory.TRADE_LIFECYCLE:
            return _BOLD_FONT
        return None


class _CategoryFilterProxy(QSortFilterProxyModel):
    """Filters rows by source and category sets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sources: Optional[Set[str]] = None
        self._categories: Optional[Set[SignalCategory]] = None

    def set_source_filter(self, sources: Optional[Set[str]]) -> None:
        self._sources = sources
        self.invalidateFilter()

    def set_category_filter(self, cats: Optional[Set[SignalCategory]]) -> None:
        self._categories = cats
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        idx = self.sourceModel().index(source_row, 0, source_parent)
        entry: SignalEntry = self.sourceModel().data(idx, Qt.UserRole)
        if entry is None:
            return True
        if self._sources is not None and entry.source not in self._sources:
            return False
        if self._categories is not None and entry.category not in self._categories:
            return False
        return True


class TradeBlotter(QWidget):
    """Signal/trade log table with structured entries, layer-based filtering,
    and virtualised rendering."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(400, 150)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(4, 2, 4, 2)

        lbl = QLabel("Signal Log")
        lbl.setFont(QFont("Menlo", 9, QFont.Bold))
        lbl.setStyleSheet("color: #b4b4c8; background: transparent;")
        header_row.addWidget(lbl)

        header_row.addStretch()

        header_row.addWidget(self._make_label("Filter:"))

        self._btn_strategy = QCheckBox("Strategy")
        self._btn_strategy.setStyleSheet("color: #b4b4c8; font-size: 10px;")
        self._btn_strategy.toggled.connect(self._on_strategy_toggled)
        header_row.addWidget(self._btn_strategy)

        # Phase 8C: renamed from "Trade" → "Trades" (_btn_trades_only)
        self._btn_trades_only = QCheckBox("Trades")
        self._btn_trades_only.setStyleSheet(
            "color: #dceeff; font-size: 10px; font-weight: bold;")
        self._btn_trades_only.toggled.connect(self._on_trades_only_toggled)
        header_row.addWidget(self._btn_trades_only)

        self._btn_ripple = QCheckBox("Ripple")
        self._btn_ripple.setStyleSheet("color: #64a0dc; font-size: 10px;")
        self._btn_ripple.toggled.connect(self._on_ripple_toggled)
        header_row.addWidget(self._btn_ripple)

        self._btn_raw = QCheckBox("Raw")
        self._btn_raw.setStyleSheet("color: #828296; font-size: 10px;")
        self._btn_raw.toggled.connect(self._on_raw_toggled)
        header_row.addWidget(self._btn_raw)

        # Phase 8C: "Debug" filter — shows LEGACY_RAW + DIAGNOSTIC
        self._btn_debug = QCheckBox("Debug")
        self._btn_debug.setStyleSheet("color: #606070; font-size: 10px;")
        self._btn_debug.toggled.connect(self._on_debug_toggled)
        header_row.addWidget(self._btn_debug)

        clear_btn = QPushButton("Clear")
        clear_btn.setMaximumWidth(50)
        clear_btn.setStyleSheet(
            "QPushButton { font-size: 10px; padding: 2px 6px; }")
        clear_btn.clicked.connect(self.clear)
        header_row.addWidget(clear_btn)

        header_widget = QWidget()
        header_widget.setLayout(header_row)
        header_widget.setStyleSheet("background: #0f0f19;")
        layout.addWidget(header_widget)

        self._model = _SignalTableModel(self)
        self._proxy = _CategoryFilterProxy(self)
        self._proxy.setSourceModel(self._model)

        self._btn_strategy.setChecked(True)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(22)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self._table.setColumnWidth(0, 90)   # Time
        self._table.setColumnWidth(1, 65)   # Layer
        self._table.setColumnWidth(2, 110)  # Category
        self._table.setColumnWidth(3, 180)  # Type
        self._table.setColumnWidth(4, 40)   # Side
        self._table.setColumnWidth(5, 80)   # Price
        self._table.setColumnWidth(6, 55)   # Strength
        self._table.setColumnWidth(7, 65)   # PnL (Phase 8C)

        self._table.setStyleSheet("""
            QTableView {
                background-color: #0f0f19;
                color: #b4b4c8;
                gridline-color: #282838;
                font-family: Menlo;
                font-size: 11px;
            }
            QTableView::item:selected {
                background-color: #2a2a4a;
            }
            QHeaderView::section {
                background-color: #1a1a2e;
                color: #b4b4c8;
                padding: 4px;
                border: 1px solid #282838;
                font-weight: bold;
                font-size: 10px;
            }
        """)
        layout.addWidget(self._table)

    # ---- public API (backwards compatible) ----

    def add_signal(self, timestamp, signal_type, price, strength, description):
        """Legacy API — wraps raw signals into a SignalEntry."""
        is_buy = "BUY" in signal_type or "BULL" in signal_type
        side = "BUY" if is_buy else ("SELL" if ("SELL" in signal_type or "BEAR" in signal_type) else "")
        cat = SignalCategory.LEGACY_RAW
        source = "legacy"
        if signal_type.startswith("EXEC_"):
            cat = SignalCategory.EXECUTION
            source = "execution"
        elif any(signal_type.startswith(p) for p in _CONTEXT_SIGNAL_PREFIXES):
            cat = SignalCategory.CONTEXT
            source = "legacy"
        entry = SignalEntry(
            timestamp=int(timestamp),
            signal_type=signal_type,
            source=source,
            category=cat,
            side=side,
            price=price,
            strength=strength,
            description=description,
        )
        self.add_entry(entry)

    def add_entry(self, entry: SignalEntry) -> None:
        """Add a structured SignalEntry to the log."""
        self._model.append(entry)
        self._table.scrollToBottom()

    def clear(self):
        self._model.clear_all()

    # ---- filter callbacks ----

    def _apply_filter(self, cats: Optional[Set[SignalCategory]]):
        self._proxy.set_source_filter(None)
        self._proxy.set_category_filter(cats)

    def _on_strategy_toggled(self, checked):
        if checked:
            self._clear_checkboxes(skip="strategy")
            self._apply_filter(_FILTER_STRATEGY)
        else:
            self._apply_filter(None)

    def _on_trades_only_toggled(self, checked):
        if checked:
            self._clear_checkboxes(skip="trades")
            self._apply_filter(_FILTER_TRADE)
        else:
            self._apply_filter(None)

    def _on_ripple_toggled(self, checked):
        if checked:
            self._clear_checkboxes(skip="ripple")
            self._apply_filter(_FILTER_RIPPLE)
        else:
            self._apply_filter(None)

    def _on_raw_toggled(self, checked):
        if checked:
            self._clear_checkboxes(skip="raw")
            self._apply_filter(_FILTER_RAW)
        else:
            self._apply_filter(None)

    def _on_debug_toggled(self, checked):
        # Phase 8C — Debug filter: LEGACY_RAW + DIAGNOSTIC
        if checked:
            self._clear_checkboxes(skip="debug")
            self._apply_filter(_FILTER_DEBUG)
        else:
            self._apply_filter(None)

    def _clear_checkboxes(self, skip=""):
        for name, btn in [("strategy", self._btn_strategy),
                          ("trades", self._btn_trades_only),
                          ("ripple", self._btn_ripple),
                          ("raw", self._btn_raw),
                          ("debug", self._btn_debug)]:
            if name != skip:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)

    @staticmethod
    def _make_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #888; font-size: 10px; background: transparent;")
        return lbl
