from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, List, Optional


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderLifecycle(Enum):
    """Phase 15 — lifecycle states for LIMIT and OCO orders."""
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FILLED = "FILLED"


class ExitType(Enum):
    """Phase 15 — exit type carried on ExecutionIntent for order-type routing."""
    INVALIDATION = "INVALIDATION"
    TARGET = "TARGET"
    EXHAUSTION = "EXHAUSTION"
    TIME = "TIME"
    RISK_BUDGET = "RISK_BUDGET"


class SizingMode(Enum):
    FIXED_QTY = "fixed_qty"
    FIXED_NOTIONAL = "fixed_notional"
    PCT_BALANCE = "pct_balance"


class RippleMode(Enum):
    DISABLED = "disabled"
    LOG_ONLY = "log_only"
    PAPER = "paper"


class StrategyMode(Enum):
    OBSERVE = "observe"
    PAPER = "paper"
    LIVE = "live"


class StrategyUIState(Enum):
    DISARMED = "disarmed"
    ARMING = "arming"
    ARMED_WAITING = "armed_waiting"
    ARMED_ACTIVE = "armed_active"
    ARMED_EXITING = "armed_exiting"
    ARMED_COOLDOWN = "armed_cooldown"
    DISARMING = "disarming"


class SignalCategory(Enum):
    TIDE = "TIDE"
    WAVE = "WAVE"
    CONTEXT = "CONTEXT"
    RIPPLE_PREPARE = "RIPPLE_PREPARE"
    RIPPLE_ENTRY = "RIPPLE_ENTRY"
    RIPPLE_EXIT = "RIPPLE_EXIT"
    RIPPLE_CANCEL = "RIPPLE_CANCEL"
    RIPPLE_REARM = "RIPPLE_REARM"
    TRADE_LIFECYCLE = "TRADE_LIFECYCLE"
    LEGACY_RAW = "LEGACY_RAW"
    EXECUTION = "EXECUTION"
    DIAGNOSTIC = "DIAGNOSTIC"
    STRATEGY_ARM = "STRATEGY_ARM"
    STRATEGY_DISARM = "STRATEGY_DISARM"
    STRATEGY_STATE_CHANGE = "STRATEGY_STATE_CHANGE"


class SuppressionReason(Enum):
    NONE = "none"
    COOLDOWN = "cooldown"
    DEDUPE = "dedupe"
    INVENTORY = "inventory"
    MODE_DISABLED = "mode_disabled"
    CONFIDENCE = "confidence"


# Maps C++ RippleIntent name → (SignalCategory, display_name, is_buy_side)
_RIPPLE_INTENT_MAP = {
    "PREPARE_BOUNCE_LONG":   (SignalCategory.RIPPLE_PREPARE,   "RIPPLE_PREPARE_BOUNCE_LONG",  True),
    "PREPARE_BOUNCE_SHORT":  (SignalCategory.RIPPLE_PREPARE,   "RIPPLE_PREPARE_BOUNCE_SHORT", False),
    "ENTER_BOUNCE_LONG":     (SignalCategory.TRADE_LIFECYCLE,  "ENTRY_BOUNCE_LONG",           True),
    "ENTER_BOUNCE_SHORT":    (SignalCategory.TRADE_LIFECYCLE,  "ENTRY_BOUNCE_SHORT",          False),
    "ENTER_BREAKOUT_LONG":   (SignalCategory.TRADE_LIFECYCLE,  "ENTRY_BREAKOUT_LONG",         True),
    "ENTER_BREAKOUT_SHORT":  (SignalCategory.TRADE_LIFECYCLE,  "ENTRY_BREAKOUT_SHORT",        False),
    "EXIT_BOUNCE":           (SignalCategory.TRADE_LIFECYCLE,  "EXIT_BOUNCE",                 None),
    "EXIT_BREAKOUT":         (SignalCategory.TRADE_LIFECYCLE,  "EXIT_BREAKOUT",               None),
    "REARM_FOR_NEXT_BOUNCE": (SignalCategory.RIPPLE_REARM,     "RIPPLE_REARM",                None),
    "CANCEL_PASSIVE_ORDERS": (SignalCategory.RIPPLE_CANCEL,    "RIPPLE_CANCEL_PASSIVE",       None),
}

_CATEGORY_LAYER_MAP = {
    SignalCategory.TIDE:                 "TIDE",
    SignalCategory.WAVE:                 "WAVE",
    SignalCategory.RIPPLE_PREPARE:       "RIPPLE",
    SignalCategory.RIPPLE_ENTRY:         "RIPPLE",
    SignalCategory.RIPPLE_EXIT:          "RIPPLE",
    SignalCategory.RIPPLE_CANCEL:        "RIPPLE",
    SignalCategory.RIPPLE_REARM:         "RIPPLE",
    SignalCategory.TRADE_LIFECYCLE:      "TRADE",
    SignalCategory.EXECUTION:            "EXEC",
    SignalCategory.STRATEGY_ARM:         "STRATEGY",
    SignalCategory.STRATEGY_DISARM:      "STRATEGY",
    SignalCategory.STRATEGY_STATE_CHANGE: "STRATEGY",
    SignalCategory.LEGACY_RAW:           "RAW",
    SignalCategory.CONTEXT:              "CONTEXT",
    SignalCategory.DIAGNOSTIC:           "DIAG",
}


@dataclass
class SizingConfig:
    mode: SizingMode = SizingMode.FIXED_QTY
    value: float = 0.001
    max_position: float = 0.01


@dataclass
class SignalEntry:
    """Unified signal log row consumed by the trade blotter."""
    timestamp: int = 0          # epoch ms
    signal_type: str = ""       # display name (e.g. RIPPLE_ENTER_BOUNCE_LONG)
    source: str = "legacy"      # "ripple", "legacy", "execution", "diagnostic", "strategy"
    category: SignalCategory = SignalCategory.LEGACY_RAW
    side: str = ""              # "BUY", "SELL", ""
    price: float = 0.0
    strength: float = 0.0
    description: str = ""
    state_summary: str = ""
    suppressed: SuppressionReason = SuppressionReason.NONE
    wave_regime: str = ""
    tide_bias: str = ""
    risk_budget_pct: float = 0.0
    lifecycle_state: str = ""
    archetype: str = ""


@dataclass
class ExecutionIntent:
    """Bridge between Ripple decisions and the paper execution layer."""
    timestamp: int = 0
    source: str = "Ripple"
    action: str = ""            # raw intent name (e.g. ENTER_BOUNCE_LONG)
    side: Optional[OrderSide] = None
    intent_type: str = ""       # "entry", "exit", "cancel", "rearm", "prepare"
    reference_price: float = 0.0
    invalidation_price: float = 0.0
    confidence: float = 0.0
    reason: str = ""
    state_summary: str = ""
    wall_id: int = 0
    # Phase 15 — order-type routing fields
    urgency: str = "NORMAL"     # "IMMEDIATE" | "NORMAL"
    exit_type: Optional[ExitType] = None  # set on exit intents


@dataclass
class Order:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    broker_order_id: Optional[str] = None
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    order_type: OrderType = OrderType.MARKET
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0.0
    fill_quantity: float = 0.0
    timestamp: float = field(default_factory=time.time)
    signal_type: str = ""
    error_message: str = ""
    ripple_reason: str = ""
    # Phase 15 — LIMIT / OCO fields
    price: float = 0.0                  # limit price (0.0 for MARKET)
    limit_order_id: str = ""            # OCO sibling cross-reference
    lifecycle: OrderLifecycle = OrderLifecycle.FILLED  # default for MARKET orders
    placed_ms: int = 0                  # event-time when order was placed (for timeout)


@dataclass
class Position:
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class AccountInfo:
    balance: float = 0.0
    available_balance: float = 0.0
    positions: List[Position] = field(default_factory=list)


def _parse_intent_name(decision) -> str:
    """Extract the enum member name from a C++ RippleIntent. Cached per object."""
    return str(decision.intent).split(".")[-1]


def ripple_decision_to_entry(decision, state_name: str = "",
                             intent_name: str = "") -> Optional[SignalEntry]:
    """Convert a C++ RippleDecision into a SignalEntry for the blotter."""
    if not intent_name:
        intent_name = _parse_intent_name(decision)
    if intent_name == "NO_ACTION":
        return None
    mapping = _RIPPLE_INTENT_MAP.get(intent_name)
    if mapping is None:
        return None
    cat, display_name, is_buy = mapping
    if is_buy is True:
        side = "BUY"
    elif is_buy is False:
        side = "SELL"
    else:
        side = ""
    return SignalEntry(
        timestamp=decision.timestamp,
        signal_type=display_name,
        source="ripple",
        category=cat,
        side=side,
        price=decision.reference_price,
        strength=decision.confidence,
        description=decision.reason,
        state_summary=state_name,
    )


def compute_realized_vol_from_prices(prices: Iterable[float]) -> float:
    """Phase 14B — rolling realized vol from a price buffer.

    Computes the sample standard deviation of log returns. Returns 0.0
    if fewer than two log returns are computable (i.e. fewer than 2
    valid prices), so callers can call this unconditionally on a
    short buffer without special-casing the startup window.

    This is intentionally simple for V1 (matches the live UI
    realized-vol estimator). V2 (strategy.md §23) replaces it with a
    proper intraday vol model fed by ``tide.tide_features``.
    """
    plist = [p for p in prices if p > 0]
    if len(plist) < 2:
        return 0.0
    logrets = []
    for i in range(1, len(plist)):
        logrets.append(math.log(plist[i] / plist[i - 1]))
    if len(logrets) < 2:
        return 0.0
    mean = sum(logrets) / len(logrets)
    var = sum((r - mean) ** 2 for r in logrets) / (len(logrets) - 1)
    return math.sqrt(max(var, 0.0))


def wave_snapshot_to_ofe(snap, ofe_module):
    """Phase 14B — translate a Python ``schemas.WaveSnapshot`` into an
    ``ofe.WaveSnapshot`` (C++ pybind type) for ``RippleEngine.set_wave_snapshot``.

    Enum members on both sides share the same names (Python ``schemas.WaveRegime``
    / ``schemas.PermissionLevel`` were authored to match the C++ enums in
    ``backtestingCpp/orderflow/Schemas.h``), so we translate by ``.name``
    rather than by integer value. This stays robust if the C++ side ever
    reorders the underlying integers.

    Returns the constructed ``ofe.WaveSnapshot`` (never ``None``). Raises if
    the snapshot carries an unknown enum member — callers should wrap in a
    try/except, since the live push path is best-effort.
    """
    ws = ofe_module.WaveSnapshot()
    ws.timestamp = int(getattr(snap, "timestamp", 0) or 0)
    ws.regime = getattr(ofe_module.WaveRegime, snap.regime.name)
    ws.trend_efficiency = float(snap.trend_efficiency)
    ws.dispersion = float(snap.dispersion)
    ws.absorption_ratio = float(snap.absorption_ratio)
    for attr in ("long_bounce", "short_bounce",
                 "long_breakout", "short_breakout"):
        py_lvl = getattr(snap.permissions, attr)
        setattr(ws.permissions, attr,
                getattr(ofe_module.PermissionLevel, py_lvl.name))
    ws.permissions.reduced_size_fraction = float(
        snap.permissions.reduced_size_fraction)
    return ws


# ----------------------------------------------------------------------
# Phase 14C — V1 §22.2 #12 live execution risk gate.
#
# The C++ `RippleEngine::on_trigger_decision` applies Wave permissions
# + RiskEngine gates BEFORE it opens an internal lifecycle setup or
# emits a paper fill, but `set_ripple_callback` fires for every
# non-NO_ACTION decision regardless of risk state (it is a pure
# observation channel from the C++ side's perspective). The live
# Python execution path (`execution.live_runner._ripple_cb` and
# `ui.main_window._on_ripple_received`) MUST therefore re-apply the
# same gate before forwarding the intent to a real broker — otherwise
# `consumed_es >= es_budget`, Wave DISABLED, Tide CRISIS, and over-cap
# positions would all result in real orders despite the engine having
# decided NOT to open a trade internally.
#
# This helper is the single source of truth for the gate. The wiring
# is checked by `tests/test_live_execution_v1_compliance.py`.

# Map RippleIntent action names → (TradeArchetype name, TradeSide name).
# Mirrors `RippleEngine::on_trigger_decision` (RippleEngine.cpp:274-280).
_INTENT_ARCH_SIDE = {
    "ENTER_BOUNCE_LONG":    ("BOUNCE",   "LONG"),
    "ENTER_BOUNCE_SHORT":   ("BOUNCE",   "SHORT"),
    "ENTER_BREAKOUT_LONG":  ("BREAKOUT", "LONG"),
    "ENTER_BREAKOUT_SHORT": ("BREAKOUT", "SHORT"),
}


def intent_risk_block_reason(
    intent: Optional[ExecutionIntent],
    ofe_engine: Any,
    *,
    current_position_usd: float = 0.0,
    ofe_module: Optional[Any] = None,
) -> Optional[str]:
    """Phase 14C — return a non-None reason string when an
    ``ExecutionIntent`` would be suppressed by the C++ engine's
    Wave permissions, ES budget, Tide risk multiplier, or
    max_position cap. Returns ``None`` when the intent should pass
    through to the broker.

    This mirrors the gating logic in
    ``backtestingCpp/orderflow/ripple/RippleEngine.cpp`` lines
    282-307 (Wave permission gate + `RiskEngine::check_new_order` +
    `compute_position_size`). Both gates exist in the C++ engine to
    suppress the internal lifecycle setup; this Python mirror exists
    to suppress real broker orders on the live execute path. See
    `AGENT_STRATEGY_RULES.md` §7.6 and `strategy.md` §22.2 #12.

    **Exits are NEVER blocked.** V1 must always allow risk-reducing
    flows to fire so an open position can be unwound. Likewise
    cancel/rearm/prepare intents short-circuit immediately.

    Parameters
    ----------
    intent
        The decoded ``ExecutionIntent`` (from
        :func:`ripple_decision_to_intent`).
    ofe_engine
        The ``orderflow_engine.OrderFlowEngine`` instance — used to
        query :py:meth:`get_strategy_snapshot` for Wave permissions
        and the live risk-budget snapshot.
    current_position_usd
        Caller-supplied estimate of the current position in USD
        (e.g. ``exec_mgr.current_qty * intent.reference_price``).
        Used only for the ``MAX_POSITION_EXCEEDED`` check. Defaults
        to ``0.0`` so callers without a position view skip that gate
        cleanly.
    ofe_module
        The imported ``orderflow_engine`` module. Needed to resolve
        the ``TradeArchetype`` / ``TradeSide`` enums when performing
        the Wave permission lookup. When ``None`` the Wave gate is
        skipped (best-effort behaviour preserved).
    """
    if intent is None:
        return None
    if intent.intent_type != "entry":
        return None
    if intent.side is None:
        return None

    arch_side = _INTENT_ARCH_SIDE.get(intent.action or "")
    if arch_side is None:
        return None

    try:
        snap = ofe_engine.get_strategy_snapshot()
    except Exception:
        # Engine cannot give us a snapshot — let the intent through.
        # C++ engine remains the source of truth.
        return None

    arch_name, side_name = arch_side

    if ofe_module is not None:
        try:
            perms = snap.wave.permissions
            arch_enum = getattr(ofe_module.TradeArchetype, arch_name)
            side_enum = getattr(ofe_module.TradeSide, side_name)
            frac = perms.size_fraction(arch_enum, side_enum)
            if frac <= 0.0:
                return f"WAVE_DISABLED({arch_name}/{side_name})"
        except Exception:
            pass

    try:
        risk = snap.risk
    except Exception:
        return None

    risk_mult = float(getattr(risk, "risk_multiplier", 1.0) or 0.0)
    if risk_mult <= 0.0:
        return "TIDE_CRISIS"

    es_budget = float(getattr(risk, "es_budget", 0.0) or 0.0)
    consumed = float(getattr(risk, "consumed_es", 0.0) or 0.0)
    if es_budget > 0.0 and consumed >= es_budget:
        return "ES_EXHAUSTED"

    max_pos_usd = float(getattr(risk, "max_position_usd", 0.0) or 0.0)
    if (max_pos_usd > 0.0
            and abs(float(current_position_usd or 0.0)) >= max_pos_usd):
        return "MAX_POSITION_EXCEEDED"

    return None


def ripple_decision_to_intent(decision, state_name: str = "",
                              intent_name: str = "") -> Optional[ExecutionIntent]:
    """Convert a C++ RippleDecision into an ExecutionIntent for paper execution."""
    if not intent_name:
        intent_name = _parse_intent_name(decision)
    if intent_name == "NO_ACTION":
        return None
    mapping = _RIPPLE_INTENT_MAP.get(intent_name)
    if mapping is None:
        return None
    cat, _, is_buy = mapping
    if cat in (SignalCategory.RIPPLE_ENTRY, SignalCategory.TRADE_LIFECYCLE):
        intent_type = "exit" if intent_name.startswith("EXIT_") else "entry"
    elif cat == SignalCategory.RIPPLE_CANCEL:
        intent_type = "cancel"
    elif cat == SignalCategory.RIPPLE_REARM:
        intent_type = "rearm"
    else:
        intent_type = "prepare"
    side = None
    if is_buy is True:
        side = OrderSide.BUY
    elif is_buy is False:
        side = OrderSide.SELL
    inv_price = getattr(decision, "invalidation_price", 0.0)
    return ExecutionIntent(
        timestamp=decision.timestamp,
        action=intent_name,
        side=side,
        intent_type=intent_type,
        reference_price=decision.reference_price,
        invalidation_price=inv_price,
        confidence=decision.confidence,
        reason=decision.reason,
        state_summary=state_name,
        wall_id=decision.wall_id,
    )
