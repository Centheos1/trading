"""Phase 9D — ripple suppression metrics model exposed to QML.

Surfaces ``MainWindow._ripple_metrics`` (a ``_SuppressionMetrics`` dataclass
holding the per-reason ripple counts) to QML so the strategy diagnostics
panel can render a tooltip / row that explains *why* ripples were
suppressed since the session started.

Acceptance criterion #4 of Phase 9D requires the tooltip to refresh
within one render frame of receiving a ripple decision.  We achieve
that by reading the metrics on every ``MainWindowBridge.on_timer_tick``
(100 ms cadence) and emitting Qt notify signals only when a value
changes — QML bindings then re-evaluate as part of the next scene
graph commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Property, QObject, Signal


@dataclass(frozen=True)
class SuppressionSnapshot:
    """Plain-old-data view of the counters used by :class:`SuppressionModel`.

    Keeps the model decoupled from ``ui.main_window._SuppressionMetrics``
    (which still uses six mutable ``int`` fields) so the bridge layer
    can stage updates from any thread.
    """

    emitted: int = 0
    cooldown_suppressed: int = 0
    dedupe_suppressed: int = 0
    mode_suppressed: int = 0
    confidence_suppressed: int = 0
    inventory_suppressed: int = 0

    @property
    def total_suppressed(self) -> int:
        return (
            self.cooldown_suppressed
            + self.dedupe_suppressed
            + self.mode_suppressed
            + self.confidence_suppressed
            + self.inventory_suppressed
        )


def _snapshot_from_metrics(metrics: Any) -> SuppressionSnapshot:
    """Best-effort conversion of a ``_SuppressionMetrics`` dataclass into
    a :class:`SuppressionSnapshot`.  Falls back to zeros if attributes
    are missing so tests can pass a partial object."""
    if metrics is None:
        return SuppressionSnapshot()
    return SuppressionSnapshot(
        emitted=int(getattr(metrics, "emitted", 0) or 0),
        cooldown_suppressed=int(getattr(metrics, "cooldown_suppressed", 0) or 0),
        dedupe_suppressed=int(getattr(metrics, "dedupe_suppressed", 0) or 0),
        mode_suppressed=int(getattr(metrics, "mode_suppressed", 0) or 0),
        confidence_suppressed=int(getattr(metrics, "confidence_suppressed", 0) or 0),
        inventory_suppressed=int(getattr(metrics, "inventory_suppressed", 0) or 0),
    )


class SuppressionModel(QObject):
    """QObject wrapper around :class:`SuppressionSnapshot`.

    All setters dedupe on identity so QML bindings only re-evaluate when
    a counter actually changes.  ``summary`` is a single-string property
    intended for the ``ToolTip.text`` binding in ``StrategyDiagnostics.qml``.
    """

    emittedChanged = Signal()
    cooldownSuppressedChanged = Signal()
    dedupeSuppressedChanged = Signal()
    modeSuppressedChanged = Signal()
    confidenceSuppressedChanged = Signal()
    inventorySuppressedChanged = Signal()
    totalSuppressedChanged = Signal()
    summaryChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._snapshot = SuppressionSnapshot()

    # ----- Q_PROPERTYs ------------------------------------------------

    @Property(int, notify=emittedChanged)
    def emitted(self) -> int:
        return self._snapshot.emitted

    @Property(int, notify=cooldownSuppressedChanged)
    def cooldownSuppressed(self) -> int:
        return self._snapshot.cooldown_suppressed

    @Property(int, notify=dedupeSuppressedChanged)
    def dedupeSuppressed(self) -> int:
        return self._snapshot.dedupe_suppressed

    @Property(int, notify=modeSuppressedChanged)
    def modeSuppressed(self) -> int:
        return self._snapshot.mode_suppressed

    @Property(int, notify=confidenceSuppressedChanged)
    def confidenceSuppressed(self) -> int:
        return self._snapshot.confidence_suppressed

    @Property(int, notify=inventorySuppressedChanged)
    def inventorySuppressed(self) -> int:
        return self._snapshot.inventory_suppressed

    @Property(int, notify=totalSuppressedChanged)
    def totalSuppressed(self) -> int:
        return self._snapshot.total_suppressed

    @Property(str, notify=summaryChanged)
    def summary(self) -> str:
        s = self._snapshot
        if s.emitted == 0 and s.total_suppressed == 0:
            return "No ripple decisions yet."
        return (
            f"Emitted: {s.emitted}  •  "
            f"Suppressed: {s.total_suppressed}\n"
            f"  cooldown: {s.cooldown_suppressed}\n"
            f"  dedupe: {s.dedupe_suppressed}\n"
            f"  mode: {s.mode_suppressed}\n"
            f"  confidence: {s.confidence_suppressed}\n"
            f"  inventory: {s.inventory_suppressed}"
        )

    # ----- mutators ---------------------------------------------------

    def update(self, metrics: Any) -> None:
        """Replace the snapshot from a ``_SuppressionMetrics`` instance.

        Emits notify signals only for fields whose value actually
        changed, and always emits ``totalSuppressedChanged`` /
        ``summaryChanged`` when any suppression counter moves so the
        bound QML text + tooltip re-evaluates in lock-step.
        """
        new_snap = _snapshot_from_metrics(metrics)
        if new_snap == self._snapshot:
            return
        old = self._snapshot
        self._snapshot = new_snap
        if new_snap.emitted != old.emitted:
            self.emittedChanged.emit()
        if new_snap.cooldown_suppressed != old.cooldown_suppressed:
            self.cooldownSuppressedChanged.emit()
        if new_snap.dedupe_suppressed != old.dedupe_suppressed:
            self.dedupeSuppressedChanged.emit()
        if new_snap.mode_suppressed != old.mode_suppressed:
            self.modeSuppressedChanged.emit()
        if new_snap.confidence_suppressed != old.confidence_suppressed:
            self.confidenceSuppressedChanged.emit()
        if new_snap.inventory_suppressed != old.inventory_suppressed:
            self.inventorySuppressedChanged.emit()
        if new_snap.total_suppressed != old.total_suppressed:
            self.totalSuppressedChanged.emit()
        # Summary depends on every counter; emit whenever anything changes.
        self.summaryChanged.emit()

    def clear(self) -> None:
        """Reset to the zero snapshot (used by ``MainWindowBridge.clear_session``)."""
        if self._snapshot == SuppressionSnapshot():
            return
        self.update(SuppressionSnapshot())


__all__ = ["SuppressionModel", "SuppressionSnapshot"]
