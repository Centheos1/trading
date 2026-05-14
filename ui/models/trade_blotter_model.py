"""Phase 9C — ``TradeBlotterModel``.

``QAbstractListModel`` adapter for the QML ``TradeBlotter`` list view.
Replaces the ``QTableView`` + ``_SignalTableModel`` pair in
:mod:`ui.trade_blotter` with a list-model surface that QML can drive
through a single ``ListView`` + delegate.

Threading
---------
``appendEntry`` may be called from any thread (the ripple drain pump
calls it directly today).  The implementation uses
``QMetaObject.invokeMethod(self, "_append_main_thread", Qt.QueuedConnection)``
to marshal the row insertion onto the GUI thread, where
``beginInsertRows`` / ``endInsertRows`` must run.  Callers therefore do
not need to wrap ``appendEntry`` in a queued slot themselves.

Bounded storage
---------------
The deque is capped at :data:`_MAX_ROWS` rows (500).  When the cap is
hit the oldest row is dropped with the proper ``beginRemoveRows`` /
``endRemoveRows`` pair so QML's ``ListView`` re-virtualises correctly.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Deque, Optional

from PySide6.QtCore import (
    Property,
    QAbstractListModel,
    QByteArray,
    QMetaObject,
    QModelIndex,
    QObject,
    Qt,
    Signal,
    Slot,
)

from execution.models import SignalCategory, SignalEntry, _CATEGORY_LAYER_MAP

logger = logging.getLogger(__name__)

# §22.4 — TradeBlotterModel keeps the last 500 rows.  The legacy
# ``_SignalTableModel`` capped at 2000; 500 is the new ceiling because
# QML ListView delegates allocate more aggressively than QTableView
# cells and the dashboard only needs a recent scrollback window.
_MAX_ROWS = 500

# Display threshold matching the legacy QTableView blotter (Phase 8C).
_PNL_DISPLAY_THRESHOLD = 1e-8


# QML role names — exposed via ``roleNames`` so QML delegates can
# reference ``model.timestamp``, ``model.category`` etc. directly.
_ROLE_TIMESTAMP = Qt.UserRole + 1
_ROLE_TIMESTAMP_STR = Qt.UserRole + 2
_ROLE_LAYER = Qt.UserRole + 3
_ROLE_CATEGORY = Qt.UserRole + 4
_ROLE_SIGNAL_TYPE = Qt.UserRole + 5
_ROLE_SIDE = Qt.UserRole + 6
_ROLE_PRICE = Qt.UserRole + 7
_ROLE_PRICE_STR = Qt.UserRole + 8
_ROLE_STRENGTH = Qt.UserRole + 9
_ROLE_DESCRIPTION = Qt.UserRole + 10
_ROLE_REALIZED_PNL = Qt.UserRole + 11
_ROLE_REALIZED_PNL_STR = Qt.UserRole + 12
_ROLE_CATEGORY_COLOR = Qt.UserRole + 13


# Hex strings (not QColor) — QML consumes them via ``color: model.categoryColor``.
_CATEGORY_HEX_COLOURS: dict[SignalCategory, str] = {
    SignalCategory.TIDE:                  "#d2aa3c",
    SignalCategory.WAVE:                  "#6482c8",
    SignalCategory.RIPPLE_ENTRY:          "#1ec864",
    SignalCategory.RIPPLE_EXIT:           "#ffb43c",
    SignalCategory.RIPPLE_PREPARE:        "#64a0dc",
    SignalCategory.RIPPLE_CANCEL:         "#ff5050",
    SignalCategory.RIPPLE_REARM:          "#a08cdc",
    SignalCategory.TRADE_LIFECYCLE:       "#dcf0ff",
    SignalCategory.EXECUTION:             "#ffd700",
    SignalCategory.LEGACY_RAW:            "#828296",
    SignalCategory.CONTEXT:               "#787896",
    SignalCategory.DIAGNOSTIC:            "#646482",
    SignalCategory.STRATEGY_ARM:          "#50c878",
    SignalCategory.STRATEGY_DISARM:       "#c86450",
    SignalCategory.STRATEGY_STATE_CHANGE: "#8ca0dc",
}

_DEFAULT_CATEGORY_COLOUR = "#b4b4c8"


class TradeBlotterModel(QAbstractListModel):
    """QML-facing list model for the trade blotter (signal log).

    Public API
    ----------
    * :meth:`appendEntry` — thread-safe append; the row is materialised
      on the GUI thread.
    * :meth:`clear` — wipe all rows.
    * :pyattr:`rowCount` — number of currently stored rows.

    QML usage::

        ListView {
            model: tradeBlotterModel
            delegate: Item {
                width: parent.width
                height: 32
                Text { text: model.timestampStr; color: model.categoryColor }
                ...
            }
        }
    """

    countChanged = Signal(int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._entries: Deque[SignalEntry] = deque(maxlen=_MAX_ROWS)
        # Thread-safe staging queue for ``appendEntry`` calls coming
        # from worker threads.  ``_append_main_thread`` drains the
        # queue inside the GUI thread.
        self._pending: deque = deque()
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    # QAbstractListModel overrides
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._entries)

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            _ROLE_TIMESTAMP:        QByteArray(b"timestamp"),
            _ROLE_TIMESTAMP_STR:    QByteArray(b"timestampStr"),
            _ROLE_LAYER:            QByteArray(b"layer"),
            _ROLE_CATEGORY:         QByteArray(b"category"),
            _ROLE_SIGNAL_TYPE:      QByteArray(b"signalType"),
            _ROLE_SIDE:             QByteArray(b"side"),
            _ROLE_PRICE:            QByteArray(b"price"),
            _ROLE_PRICE_STR:        QByteArray(b"priceStr"),
            _ROLE_STRENGTH:         QByteArray(b"strength"),
            _ROLE_DESCRIPTION:      QByteArray(b"description"),
            _ROLE_REALIZED_PNL:     QByteArray(b"realizedPnl"),
            _ROLE_REALIZED_PNL_STR: QByteArray(b"realizedPnlStr"),
            _ROLE_CATEGORY_COLOR:   QByteArray(b"categoryColor"),
        }

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if row < 0 or row >= len(self._entries):
            return None
        entry = self._entries[row]

        if role == _ROLE_TIMESTAMP:
            return int(entry.timestamp)
        if role == _ROLE_TIMESTAMP_STR:
            return _fmt_timestamp(entry.timestamp)
        if role == _ROLE_LAYER:
            return _CATEGORY_LAYER_MAP.get(entry.category, entry.source)
        if role == _ROLE_CATEGORY:
            return entry.category.value
        if role == _ROLE_SIGNAL_TYPE:
            return entry.signal_type
        if role == _ROLE_SIDE:
            return entry.side
        if role == _ROLE_PRICE:
            return float(entry.price)
        if role == _ROLE_PRICE_STR:
            return f"{entry.price:.2f}" if entry.price else ""
        if role == _ROLE_STRENGTH:
            return float(entry.strength)
        if role == _ROLE_DESCRIPTION:
            desc = entry.description
            if entry.state_summary:
                desc = f"[{entry.state_summary}] {desc}"
            return desc
        if role == _ROLE_REALIZED_PNL:
            return float(getattr(entry, "realized_pnl", 0.0))
        if role == _ROLE_REALIZED_PNL_STR:
            pnl = float(getattr(entry, "realized_pnl", 0.0))
            return f"{pnl:+.2f}" if abs(pnl) > _PNL_DISPLAY_THRESHOLD else ""
        if role == _ROLE_CATEGORY_COLOR:
            return _CATEGORY_HEX_COLOURS.get(
                entry.category, _DEFAULT_CATEGORY_COLOUR)

        # ``DisplayRole`` provides a sensible fallback for the QML
        # debug inspector (no delegate referencing this role in
        # production code today).
        if role == Qt.DisplayRole:
            return entry.signal_type
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @Property(int, notify=countChanged)
    def count(self) -> int:
        """Q_PROPERTY mirror of ``rowCount`` for QML bindings."""
        return len(self._entries)

    def appendEntry(self, entry: SignalEntry) -> None:
        """Thread-safe append.

        If called from the GUI thread the row is inserted immediately;
        if called from any other thread the insertion is marshalled via
        ``QMetaObject.invokeMethod(..., Qt.QueuedConnection)`` so
        ``beginInsertRows`` / ``endInsertRows`` run on the right thread.

        ``entry`` is captured by reference; do not mutate it after
        passing it in.  The expected type is
        :class:`execution.models.SignalEntry`, but any object with the
        same attribute surface works (duck-typed for testability).
        """
        if entry is None:
            return
        # Push the entry onto a thread-safe queue so concurrent
        # ``appendEntry`` calls from multiple worker threads never
        # race on a single attribute.  ``_append_main_thread`` drains
        # the queue inside the GUI thread.  We can't pass the entry
        # directly through ``invokeMethod`` because PySide6's
        # QueuedConnection marshaller does not support arbitrary
        # ``object`` arguments reliably.
        with self._pending_lock:
            self._pending.append(entry)
        QMetaObject.invokeMethod(
            self, "_append_main_thread", Qt.QueuedConnection,
        )

    @Slot()
    def _append_main_thread(self) -> None:
        """Inner slot dispatched from :meth:`appendEntry`."""
        with self._pending_lock:
            if not self._pending:
                return
            entry = self._pending.popleft()
        self._append_now(entry)

    # ------------------------------------------------------------------
    # GUI-thread direct append — used by tests and by code that already
    # lives on the GUI thread (saves a queued dispatch).
    # ------------------------------------------------------------------

    def append_entry_sync(self, entry: SignalEntry) -> None:
        """Append on the calling thread (must be the GUI thread)."""
        if entry is None:
            return
        self._append_now(entry)

    def _append_now(self, entry: SignalEntry) -> None:
        if len(self._entries) >= _MAX_ROWS:
            self.beginRemoveRows(QModelIndex(), 0, 0)
            self._entries.popleft()
            self.endRemoveRows()
        pos = len(self._entries)
        self.beginInsertRows(QModelIndex(), pos, pos)
        self._entries.append(entry)
        self.endInsertRows()
        self.countChanged.emit(len(self._entries))

    @Slot()
    def clear(self) -> None:
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self.endResetModel()
        self.countChanged.emit(0)


def _fmt_timestamp(ts_ms: int) -> str:
    """Render an epoch-millisecond timestamp as ``HH:MM:SS.mmm`` (UTC)."""
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
    except (OSError, ValueError, OverflowError):
        return ""
    return dt.strftime("%H:%M:%S.%f")[:-3]
