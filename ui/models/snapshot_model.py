"""Phase 9A — ``SnapshotModel``.

A ``QObject`` adapter that exposes the relevant fields of a
``StrategySnapshot`` to QML as ``Q_PROPERTY`` values.  The model is the
typed boundary between the strategy engine and the QML scene graph;
``update_from_snapshot()`` is the only mutator that QML or Python code
should call.

Per ``AGENT_STRATEGY_RULES.md §22.3``:

- Setters emit a single ``Notify`` signal per field that actually changed,
  so QML bindings only invalidate when the value differs.
- The model holds no thread-affine state — values are scalars.  Callers
  that mutate from worker threads must marshal via
  ``QMetaObject.invokeMethod(..., Qt.QueuedConnection)``.

Wiring into the live UI (``main_window`` → ``MainWindowBridge``) is added
in Phase 9C; for Phase 9A the model is registered as a QML type so 9B/9C
work can bind against a stable surface.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot


class SnapshotModel(QObject):
    """QML-facing view of a ``StrategySnapshot``.

    The Q_PROPERTYs are deliberately a subset of ``StrategySnapshot``:
    only the fields the dashboard needs to display.  Add fields here as
    Phase 9C wires new bindings; keep names camelCase on the QML side
    (auto-generated from the snake_case property names by PySide6).
    """

    tideBiasChanged = Signal(str)
    waveRegimeChanged = Signal(str)
    riskBudgetPctChanged = Signal(float)
    unrealizedPnlChanged = Signal(float)
    tradeStateChanged = Signal(str)
    # Phase 9C — overlay binding (chart entry / stop / target lines +
    # archetype string).
    entryPriceChanged = Signal(float)
    stopPriceChanged = Signal(float)
    targetPriceChanged = Signal(float)
    tradeArchetypeChanged = Signal(str)
    # Aggregate "any overlay field changed" notify — emitted from
    # ``update_from_snapshot`` after individual fields are reconciled
    # so QML can hook a single signal for batch re-evaluation
    # (UI_STRATEGY_INTEGRATION_PLAN.md §22.4 "Wire from Python").
    overlayChanged = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tide_bias: str = ""
        self._wave_regime: str = ""
        self._risk_budget_pct: float = 0.0
        self._unrealized_pnl: float = 0.0
        self._trade_state: str = ""
        self._entry_price: float = 0.0
        self._stop_price: float = 0.0
        self._target_price: float = 0.0
        self._trade_archetype: str = ""

    # ------------------------------------------------------------------
    # Q_PROPERTY definitions
    # ------------------------------------------------------------------

    @Property(str, notify=tideBiasChanged)
    def tideBias(self) -> str:
        return self._tide_bias

    @tideBias.setter
    def tideBias(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._tide_bias:
            self._tide_bias = value
            self.tideBiasChanged.emit(value)

    @Property(str, notify=waveRegimeChanged)
    def waveRegime(self) -> str:
        return self._wave_regime

    @waveRegime.setter
    def waveRegime(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._wave_regime:
            self._wave_regime = value
            self.waveRegimeChanged.emit(value)

    @Property(float, notify=riskBudgetPctChanged)
    def riskBudgetPct(self) -> float:
        return self._risk_budget_pct

    @riskBudgetPct.setter
    def riskBudgetPct(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._risk_budget_pct:
            self._risk_budget_pct = value
            self.riskBudgetPctChanged.emit(value)

    @Property(float, notify=unrealizedPnlChanged)
    def unrealizedPnl(self) -> float:
        return self._unrealized_pnl

    @unrealizedPnl.setter
    def unrealizedPnl(self, value: float) -> None:
        value = float(value) if value is not None else 0.0
        if value != self._unrealized_pnl:
            self._unrealized_pnl = value
            self.unrealizedPnlChanged.emit(value)

    @Property(str, notify=tradeStateChanged)
    def tradeState(self) -> str:
        return self._trade_state

    @tradeState.setter
    def tradeState(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._trade_state:
            self._trade_state = value
            self.tradeStateChanged.emit(value)

    # ------------------------------------------------------------------
    # Phase 9C overlay properties — entry / stop / target / archetype.
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

    @Property(str, notify=tradeArchetypeChanged)
    def tradeArchetype(self) -> str:
        return self._trade_archetype

    @tradeArchetype.setter
    def tradeArchetype(self, value: str) -> None:
        value = str(value) if value is not None else ""
        if value != self._trade_archetype:
            self._trade_archetype = value
            self.tradeArchetypeChanged.emit(value)

    # ------------------------------------------------------------------
    # Bulk update from a StrategySnapshot.
    # ------------------------------------------------------------------

    @Slot(object)
    def update_from_snapshot(self, snapshot: Any) -> None:
        """Refresh every property from a ``StrategySnapshot``.

        ``snapshot`` is typed as ``Any`` to avoid importing ``schemas`` at
        module load (keeps Phase 9A's QML scaffold isolated from the
        strategy / engine package boundary).  Callers pass a real
        ``StrategySnapshot``; ``None`` is tolerated and clears the model.
        """
        if snapshot is None:
            self.tideBias = ""
            self.waveRegime = ""
            self.riskBudgetPct = 0.0
            self.unrealizedPnl = 0.0
            self.tradeState = ""
            self.entryPrice = 0.0
            self.stopPrice = 0.0
            self.targetPrice = 0.0
            self.tradeArchetype = ""
            self.overlayChanged.emit()
            return

        tide = getattr(snapshot, "tide", None)
        wave = getattr(snapshot, "wave", None)
        risk = getattr(snapshot, "risk", None)
        trade = getattr(snapshot, "trade", None)

        if tide is not None:
            bias = getattr(tide, "bias", None)
            self.tideBias = _enum_name(bias)
        if wave is not None:
            regime = getattr(wave, "regime", None)
            self.waveRegime = _enum_name(regime)
        if risk is not None:
            # Phase 9C — surface the consumed/budget ratio so the
            # diagnostics panel can render the "ES Used %" gauge.
            # Falls back to ``risk_multiplier`` when budget is zero.
            consumed = float(getattr(risk, "consumed_es", 0.0))
            budget = float(getattr(risk, "es_budget", 0.0))
            if budget > 0:
                self.riskBudgetPct = consumed / budget
            else:
                self.riskBudgetPct = float(getattr(
                    risk, "risk_multiplier", 0.0))
        if trade is not None:
            self.unrealizedPnl = float(getattr(trade, "unrealized_pnl", 0.0))
            self.tradeState = _enum_name(getattr(trade, "state", None))
            self.entryPrice = float(getattr(trade, "entry_price", 0.0))
            self.stopPrice = float(getattr(trade, "stop_price", 0.0))
            self.targetPrice = float(getattr(trade, "target_price", 0.0))
            self.tradeArchetype = _enum_name(getattr(trade, "archetype", None))
        else:
            self.entryPrice = 0.0
            self.stopPrice = 0.0
            self.targetPrice = 0.0
            self.tradeArchetype = ""

        # Aggregate notify — emit *after* individual fields settle so
        # QML expressions that depend on multiple overlay properties
        # re-evaluate exactly once.
        self.overlayChanged.emit()


def _enum_name(value: Any) -> str:
    """Return ``value.name`` when it is an ``Enum``; otherwise ``str(value)``.

    ``""`` for ``None`` so QML bindings render an empty label rather than
    the literal text ``"None"``.
    """
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)
