"""Phase 9C — Thin Python bridge between the strategy engine and the
QML models.

Replaces the direct widget calls in :mod:`ui.main_window` (e.g.
``self._strategy_dashboard.blotter.add_entry(entry)``) with a single
object that owns references to the QML-facing
:class:`~ui.models.snapshot_model.SnapshotModel`,
:class:`~ui.models.position_model.PositionModel`, and
:class:`~ui.models.trade_blotter_model.TradeBlotterModel` instances and
the live :class:`~ui.items.candle_item.CandleItem`.

The bridge owns the marshalling discipline: every callback either
runs on the GUI thread already (e.g. ``on_timer_tick`` driven by a
QTimer) or hops via :func:`PySide6.QtCore.QMetaObject.invokeMethod`
with ``Qt.QueuedConnection`` (e.g. ``on_signal_entry`` invoked by
the ripple drain pump from a worker thread).

Wiring into ``MainWindow`` is deferred to a follow-up patch — the
bridge is engine/UI-agnostic and is exercised end-to-end in
``tests/test_qml_strategy_ui.py``.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Deque, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QMetaObject,
    QObject,
    Qt,
    Signal,
    Slot,
)

from execution.models import SignalEntry
from ui.items.candle_item import CandleItem
from ui.models.position_model import PositionModel
from ui.models.snapshot_model import SnapshotModel
from ui.models.suppression_model import SuppressionModel
from ui.models.trade_blotter_model import TradeBlotterModel

logger = logging.getLogger(__name__)


class MainWindowBridge(QObject):
    """Single hub that fans engine events out to the QML models.

    The bridge lives on the GUI thread.  Engine callbacks (ripple,
    execution, timer tick) call its public methods; the bridge
    forwards the data to the QML models / chart item.  Threading is
    handled here so call-sites stay short.

    Parameters
    ----------
    snapshot_model:
        ``SnapshotModel`` driving ``StrategyDiagnostics.qml``.
    position_model:
        ``PositionModel`` driving ``PositionCard.qml``.
    blotter_model:
        ``TradeBlotterModel`` driving ``TradeBlotter.qml``.
    candle_item:
        ``CandleItem`` instance whose overlay (entry / stop / target
        lines, ENTRY / EXIT markers) is updated on every tick.
        Optional — Phase 9D wires it in once the chart is the sole
        candle-chart surface.
    """

    bridgeReady = Signal()

    def __init__(
        self,
        snapshot_model: SnapshotModel,
        position_model: PositionModel,
        blotter_model: TradeBlotterModel,
        candle_item: Optional[CandleItem] = None,
        parent: Optional[QObject] = None,
        *,
        suppression_model: Optional[SuppressionModel] = None,
        main_window: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._snapshot_model = snapshot_model
        self._position_model = position_model
        self._blotter_model = blotter_model
        self._suppression_model = suppression_model
        self._candle_item = candle_item
        # Phase 9D — reference to the hidden ``MainWindow`` so the
        # bridge can route toolbar slots (candle timeframe, overlay
        # toggles) back to legacy widget state.  ``None`` is fine for
        # idle bridges + offscreen tests.
        self._main_window = main_window
        # Phase 9C — keep the last N trade events so the CandleItem
        # can repaint its markers whenever the visible window shifts
        # (we don't want to drop markers just because the bucket
        # window scrolls).  Capped at 200 entries to bound memory.
        self._trade_events: list[tuple[int, str, float]] = []
        self._max_trade_events = 200
        # Thread-safe staging queue for ``_enqueue_marker`` calls
        # coming from worker threads (mirrors the
        # ``TradeBlotterModel`` pattern).
        self._pending_markers: Deque[Tuple[int, str, float]] = deque()
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_candle_item(self, candle_item: Optional[CandleItem]) -> None:
        """Late-bind the ``CandleItem`` reference.

        The candle item is owned by the QML engine and may not be
        resolved until ``engine.load(main.qml)`` has produced its
        root objects.  Callers typically invoke this after walking
        ``engine.rootObjects()``.
        """
        self._candle_item = candle_item
        if candle_item is not None and self._trade_events:
            candle_item.set_trade_events(tuple(self._trade_events))

    @property
    def snapshot_model(self) -> SnapshotModel:
        return self._snapshot_model

    @property
    def position_model(self) -> PositionModel:
        return self._position_model

    @property
    def blotter_model(self) -> TradeBlotterModel:
        return self._blotter_model

    @property
    def candle_item(self) -> Optional[CandleItem]:
        return self._candle_item

    @property
    def suppression_model(self) -> Optional[SuppressionModel]:
        return self._suppression_model

    @property
    def main_window(self) -> Optional[Any]:
        return self._main_window

    def set_main_window(self, main_window: Optional[Any]) -> None:
        """Phase 9D — let ``QmlEngineHost`` register the hidden
        ``MainWindow`` after construction so the toolbar slots have a
        target to route to.  Passing ``None`` detaches the reference
        (used by ``clear_session`` in test harnesses)."""
        self._main_window = main_window

    # ------------------------------------------------------------------
    # Public hooks called by :class:`MainWindow`.
    # ------------------------------------------------------------------

    def on_signal_entry(self, entry: SignalEntry) -> None:
        """Forward a ``SignalEntry`` to the trade blotter.

        Safe to call from any thread; the underlying ``appendEntry``
        marshals onto the GUI thread via ``QMetaObject.invokeMethod``.

        For ``TRADE_LIFECYCLE`` entries with a non-zero price, the
        bridge also appends an ENTRY / EXIT marker on the candle
        chart so the live tape lines up with the candle stream.
        """
        if entry is None:
            return
        try:
            self._blotter_model.appendEntry(entry)
        except Exception:
            logger.exception("blotter appendEntry failed")

        kind = _classify_event_kind(entry)
        if kind is None:
            return
        if entry.price <= 0:
            return
        # Append on the GUI thread to keep ``CandleItem.set_trade_events``
        # off worker threads (the scene graph buffer mutation is
        # GUI-affine).
        self._enqueue_marker(int(entry.timestamp), kind, float(entry.price))

    def on_timer_tick(
        self,
        snapshot: Any,
        execution_state: Any = None,
    ) -> None:
        """Refresh every QML model from a ``StrategySnapshot``.

        Must be called from the GUI thread (the standard ``QTimer``
        callback site).  Passing ``snapshot=None`` clears the dashboard
        (used when the strategy disarms).
        """
        try:
            self._snapshot_model.update_from_snapshot(snapshot)
        except Exception:
            logger.exception("SnapshotModel update_from_snapshot failed")
        try:
            self._position_model.update(snapshot, execution_state)
        except Exception:
            logger.exception("PositionModel update failed")

        # Phase 9D — refresh the ripple-suppression counter snapshot if
        # the hidden ``MainWindow`` is attached.  Reading from a Python
        # attribute is cheap; ``SuppressionModel.update`` dedupes so
        # binding storms are bounded.
        if self._suppression_model is not None and self._main_window is not None:
            metrics = getattr(self._main_window, "_ripple_metrics", None)
            try:
                self._suppression_model.update(metrics)
            except Exception:
                logger.exception("SuppressionModel update failed")

        if self._candle_item is None:
            return

        # Drive the chart overlay from the trade lifecycle state.  We
        # use ``position_model.active`` because that's the canonical
        # "is there a live trade" flag and avoids duplicating the
        # ACTIVE-token list here.
        active = self._position_model.active
        try:
            self._candle_item.set_trade_overlay(
                entry=self._position_model.entryPrice,
                stop=self._position_model.stopPrice,
                target=self._position_model.targetPrice,
                active=active,
            )
        except Exception:
            logger.exception("CandleItem set_trade_overlay failed")

    # ------------------------------------------------------------------
    # Phase 9D — QML toolbar slots
    # ------------------------------------------------------------------

    @Slot(int)
    def setCandleBucketMs(self, duration_ms: int) -> None:
        """Slot called by the QML ``CandleChartView`` ComboBox.

        Routes to ``MainWindow.set_candle_duration_ms`` (the legacy
        ``_on_candle_changed`` replacement) AND updates the live
        ``CandleItem.bucketMs`` so the QML chart re-buckets in lock-step.
        Both paths are wrapped in try/except so a missing attribute
        on either side (idle bridge, headless tests) is non-fatal.
        """
        duration_ms = int(duration_ms)
        if duration_ms <= 0:
            return
        mw = self._main_window
        if mw is not None:
            try:
                mw.set_candle_duration_ms(duration_ms)
            except Exception:
                logger.exception(
                    "MainWindow.set_candle_duration_ms(%s) failed", duration_ms,
                )
        if self._candle_item is not None:
            try:
                self._candle_item.bucketMs = duration_ms
            except Exception:
                logger.exception(
                    "CandleItem.bucketMs = %s failed", duration_ms,
                )

    @Slot(str, bool)
    def setOverlayEnabled(self, name: str, enabled: bool) -> None:
        """Slot called by the QML overlay-toggle CheckBox row.

        Routes the toggle to ``MainWindow._candle_view.set_overlay_enabled``
        which mutates the per-overlay ``enabled`` flag (see
        :meth:`ui.candle_chart_view.CandleChartView.set_overlay_enabled`).
        Returns silently if the legacy candle widget is not present.
        """
        if not name:
            return
        mw = self._main_window
        if mw is None:
            return
        candle_view = getattr(mw, "_candle_view", None)
        if candle_view is None:
            return
        setter = getattr(candle_view, "set_overlay_enabled", None)
        if setter is None:
            return
        try:
            setter(name, bool(enabled))
        except Exception:
            logger.exception(
                "candle_view.set_overlay_enabled(%s, %s) failed",
                name, enabled,
            )

    def push_trade_events(
        self,
        events: Sequence[tuple[int, str, float]],
    ) -> None:
        """Bulk-replace the candle chart's marker buffer.

        Useful for tests and for the session resume path that rehydrates
        the blotter from disk.
        """
        self._trade_events = list(events)[-self._max_trade_events:]
        if self._candle_item is not None:
            self._candle_item.set_trade_events(tuple(self._trade_events))

    def clear_session(self) -> None:
        """Reset the session — clears the blotter, position card, and
        chart overlay/markers.

        Used by the "New Session" menu action.
        """
        self._trade_events = []
        try:
            self._blotter_model.clear()
        except Exception:
            logger.exception("blotter clear failed")
        try:
            self._position_model.update(None, None)
        except Exception:
            logger.exception("PositionModel reset failed")
        try:
            self._snapshot_model.update_from_snapshot(None)
        except Exception:
            logger.exception("SnapshotModel reset failed")
        if self._suppression_model is not None:
            try:
                self._suppression_model.clear()
            except Exception:
                logger.exception("SuppressionModel clear failed")
        if self._candle_item is not None:
            self._candle_item.clear_trade_overlay()
            self._candle_item.clear_trade_events()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _enqueue_marker(self, ts_ms: int, kind: str, price: float) -> None:
        """Append a marker on the GUI thread.

        We use a queued invokeMethod for the candle update so callers
        from worker threads (ripple drain pump) don't touch scene-graph
        data outside the render context.  Concurrent calls race onto a
        shared deque (lock-protected) so no marker is lost even under
        bursty traffic.
        """
        with self._pending_lock:
            self._pending_markers.append((ts_ms, kind, price))
        QMetaObject.invokeMethod(
            self, "_apply_pending_marker", Qt.QueuedConnection,
        )

    @Slot()
    def _apply_pending_marker(self) -> None:
        with self._pending_lock:
            if not self._pending_markers:
                return
            marker = self._pending_markers.popleft()
        self._trade_events.append(marker)
        if len(self._trade_events) > self._max_trade_events:
            self._trade_events = self._trade_events[-self._max_trade_events:]
        if self._candle_item is not None:
            self._candle_item.set_trade_events(tuple(self._trade_events))


def _classify_event_kind(entry: SignalEntry) -> Optional[str]:
    """Return ``"ENTRY"`` / ``"EXIT"`` / ``None`` for ``entry``.

    The bridge only emits markers for actual fill events — bookkeeping
    entries (CONTEXT, DIAGNOSTIC, etc.) are skipped.
    """
    if entry is None:
        return None
    cat = getattr(entry, "category", None)
    cat_name = getattr(cat, "value", str(cat) if cat is not None else "")
    if cat_name != "TRADE_LIFECYCLE":
        # Allow EXECUTION-categorised fills, but require the signal
        # type to start with ENTRY_/EXIT_.
        if cat_name != "EXECUTION":
            return None
    signal_type = str(getattr(entry, "signal_type", "") or "").upper()
    if signal_type.startswith("ENTRY_") or signal_type.startswith("ENTER_"):
        return "ENTRY"
    if signal_type.startswith("EXIT_") or signal_type.startswith("EXEC_EXIT_"):
        return "EXIT"
    return None
