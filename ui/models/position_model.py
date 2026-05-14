"""Phase 9C — ``PositionModel``.

``QObject`` adapter that exposes the live trade state (entry / stop /
target / R:R / unrealised PnL / session PnL / lifecycle label) to QML.
Backs :file:`ui/qml/components/PositionCard.qml`.

The single mutator is :meth:`update`, which accepts a
``StrategySnapshot`` plus the ``ExecutionManager`` (any object exposing
a ``session_realized_pnl`` property is accepted; duck-typed for
testability).  Setters dedupe so QML bindings only invalidate on real
change — matching the Phase 9A ``SnapshotModel`` discipline (per
``AGENT_STRATEGY_RULES §22.3``).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot


class PositionModel(QObject):
    """QML-facing view of the live position (entry / stop / target /
    R:R / unrealised PnL / session PnL / trade-state label)."""

    entryPriceChanged = Signal(float)
    stopPriceChanged = Signal(float)
    targetPriceChanged = Signal(float)
    rrRatioChanged = Signal(float)
    unrealizedPnlChanged = Signal(float)
    sessionPnlChanged = Signal(float)
    tradeStateLabelChanged = Signal(str)
    tradeArchetypeChanged = Signal(str)
    tradeSideChanged = Signal(str)
    holdTimeMsChanged = Signal("qlonglong")
    activeChanged = Signal(bool)

    # Trade-state strings that count as "active" (overlay drawn,
    # PositionCard renders trade data instead of placeholder text).
    _ACTIVE_LIFECYCLE_TOKENS = (
        "ACTIVE", "EXPAND", "CONFIRM", "OPEN", "FILLED", "IN_TRADE",
    )

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entry_price: float = 0.0
        self._stop_price: float = 0.0
        self._target_price: float = 0.0
        self._rr_ratio: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._session_pnl: float = 0.0
        self._trade_state_label: str = ""
        self._trade_archetype: str = ""
        self._trade_side: str = ""
        self._hold_time_ms: int = 0
        self._active: bool = False

    # ------------------------------------------------------------------
    # Q_PROPERTY definitions — order matches the PositionCard layout.
    # ------------------------------------------------------------------

    @Property(float, notify=entryPriceChanged)
    def entryPrice(self) -> float:
        return self._entry_price

    @entryPrice.setter
    def entryPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._entry_price:
            self._entry_price = value
            self.entryPriceChanged.emit(value)

    @Property(float, notify=stopPriceChanged)
    def stopPrice(self) -> float:
        return self._stop_price

    @stopPrice.setter
    def stopPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._stop_price:
            self._stop_price = value
            self.stopPriceChanged.emit(value)

    @Property(float, notify=targetPriceChanged)
    def targetPrice(self) -> float:
        return self._target_price

    @targetPrice.setter
    def targetPrice(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._target_price:
            self._target_price = value
            self.targetPriceChanged.emit(value)

    @Property(float, notify=rrRatioChanged)
    def rrRatio(self) -> float:
        return self._rr_ratio

    @rrRatio.setter
    def rrRatio(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._rr_ratio:
            self._rr_ratio = value
            self.rrRatioChanged.emit(value)

    @Property(float, notify=unrealizedPnlChanged)
    def unrealizedPnl(self) -> float:
        return self._unrealized_pnl

    @unrealizedPnl.setter
    def unrealizedPnl(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._unrealized_pnl:
            self._unrealized_pnl = value
            self.unrealizedPnlChanged.emit(value)

    @Property(float, notify=sessionPnlChanged)
    def sessionPnl(self) -> float:
        return self._session_pnl

    @sessionPnl.setter
    def sessionPnl(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._session_pnl:
            self._session_pnl = value
            self.sessionPnlChanged.emit(value)

    @Property(str, notify=tradeStateLabelChanged)
    def tradeStateLabel(self) -> str:
        return self._trade_state_label

    @tradeStateLabel.setter
    def tradeStateLabel(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._trade_state_label:
            self._trade_state_label = value
            self.tradeStateLabelChanged.emit(value)
            # active is derived from the lifecycle label; recompute.
            active = self._compute_active(value)
            if active != self._active:
                self._active = active
                self.activeChanged.emit(active)

    @Property(str, notify=tradeArchetypeChanged)
    def tradeArchetype(self) -> str:
        return self._trade_archetype

    @tradeArchetype.setter
    def tradeArchetype(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._trade_archetype:
            self._trade_archetype = value
            self.tradeArchetypeChanged.emit(value)

    @Property(str, notify=tradeSideChanged)
    def tradeSide(self) -> str:
        return self._trade_side

    @tradeSide.setter
    def tradeSide(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._trade_side:
            self._trade_side = value
            self.tradeSideChanged.emit(value)

    @Property("qlonglong", notify=holdTimeMsChanged)
    def holdTimeMs(self) -> int:
        return self._hold_time_ms

    @holdTimeMs.setter
    def holdTimeMs(self, value: int) -> None:
        value = int(value) if value is not None else 0
        if value != self._hold_time_ms:
            self._hold_time_ms = value
            self.holdTimeMsChanged.emit(value)

    @Property(bool, notify=activeChanged)
    def active(self) -> bool:
        """Derived flag — True when ``tradeStateLabel`` is one of the
        lifecycle states (ACTIVE / EXPAND / CONFIRM / OPEN / FILLED /
        IN_TRADE).  QML uses this to switch ``PositionCard`` between
        the placeholder and active layouts and to drive
        ``CandleItem.tradeOverlayActive``."""
        return self._active

    # ------------------------------------------------------------------
    # Bulk update from a snapshot.
    # ------------------------------------------------------------------

    @Slot(object, object)
    def update(self, snapshot: Any, execution_state: Any = None) -> None:
        """Refresh every property from a ``StrategySnapshot`` + optional
        execution-state object.

        ``execution_state`` is duck-typed: anything with a
        ``session_realized_pnl`` attribute / property works.  Pass
        ``None`` to leave the session PnL unchanged.

        Setting ``snapshot=None`` clears every field — used when the
        strategy disarms.
        """
        if snapshot is None:
            self.entryPrice = 0.0
            self.stopPrice = 0.0
            self.targetPrice = 0.0
            self.rrRatio = 0.0
            self.unrealizedPnl = 0.0
            self.tradeStateLabel = ""
            self.tradeArchetype = ""
            self.tradeSide = ""
            self.holdTimeMs = 0
            if execution_state is not None:
                self.sessionPnl = float(getattr(
                    execution_state, "session_realized_pnl", 0.0))
            return

        trade = getattr(snapshot, "trade", None)
        entry = float(getattr(trade, "entry_price", 0.0)) if trade else 0.0
        stop = float(getattr(trade, "stop_price", 0.0)) if trade else 0.0
        target = float(getattr(trade, "target_price", 0.0)) if trade else 0.0

        self.entryPrice = entry
        self.stopPrice = stop
        self.targetPrice = target
        self.rrRatio = _rr_ratio(entry, stop, target)

        if trade is not None:
            self.unrealizedPnl = float(getattr(trade, "unrealized_pnl", 0.0))
            self.tradeStateLabel = _enum_name(getattr(trade, "state", None))
            self.tradeArchetype = _enum_name(getattr(trade, "archetype", None))
            self.tradeSide = _enum_name(getattr(trade, "side", None))
            self.holdTimeMs = int(getattr(trade, "hold_time_ms", 0) or 0)

        if execution_state is not None:
            self.sessionPnl = float(getattr(
                execution_state, "session_realized_pnl", 0.0))

    @classmethod
    def _compute_active(cls, label: str) -> bool:
        if not label:
            return False
        upper = label.upper()
        return any(token in upper for token in cls._ACTIVE_LIFECYCLE_TOKENS)


def _rr_ratio(entry: float, stop: float, target: float) -> float:
    """Reward / risk ratio = (target - entry) / (entry - stop).

    Returns 0.0 when the inputs don't form a valid trade triangle (zero
    risk, identical prices, missing values).  Sign is preserved so
    short trades report a positive R:R when targets sit *below* entry.
    """
    if entry <= 0 or stop <= 0 or target <= 0:
        return 0.0
    risk = entry - stop
    reward = target - entry
    if abs(risk) < 1e-12:
        return 0.0
    return reward / risk


def _enum_name(value: Any) -> str:
    """Return ``value.name`` when it is an ``Enum``; otherwise ``str(value)``.

    ``""`` for ``None`` so QML bindings render an empty label rather
    than the literal text ``"None"``.
    """
    if value is None:
        return ""
    if isinstance(value, Enum):
        return value.name
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)
