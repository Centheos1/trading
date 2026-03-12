from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


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


class SizingMode(Enum):
    FIXED_QTY = "fixed_qty"
    FIXED_NOTIONAL = "fixed_notional"
    PCT_BALANCE = "pct_balance"


class RippleMode(Enum):
    DISABLED = "disabled"
    LOG_ONLY = "log_only"
    PAPER = "paper"


class SignalCategory(Enum):
    CONTEXT = "CONTEXT"
    RIPPLE_PREPARE = "RIPPLE_PREPARE"
    RIPPLE_ENTRY = "RIPPLE_ENTRY"
    RIPPLE_EXIT = "RIPPLE_EXIT"
    RIPPLE_CANCEL = "RIPPLE_CANCEL"
    RIPPLE_REARM = "RIPPLE_REARM"
    LEGACY_RAW = "LEGACY_RAW"
    EXECUTION = "EXECUTION"
    DIAGNOSTIC = "DIAGNOSTIC"


class SuppressionReason(Enum):
    NONE = "none"
    COOLDOWN = "cooldown"
    DEDUPE = "dedupe"
    INVENTORY = "inventory"
    MODE_DISABLED = "mode_disabled"
    CONFIDENCE = "confidence"


# Maps C++ RippleIntent name → (SignalCategory, display_name, is_buy_side)
_RIPPLE_INTENT_MAP = {
    "PREPARE_BOUNCE_LONG":   (SignalCategory.RIPPLE_PREPARE, "RIPPLE_PREPARE_BOUNCE_LONG",  True),
    "PREPARE_BOUNCE_SHORT":  (SignalCategory.RIPPLE_PREPARE, "RIPPLE_PREPARE_BOUNCE_SHORT", False),
    "ENTER_BOUNCE_LONG":     (SignalCategory.RIPPLE_ENTRY,   "RIPPLE_ENTER_BOUNCE_LONG",    True),
    "ENTER_BOUNCE_SHORT":    (SignalCategory.RIPPLE_ENTRY,   "RIPPLE_ENTER_BOUNCE_SHORT",   False),
    "ENTER_BREAKOUT_LONG":   (SignalCategory.RIPPLE_ENTRY,   "RIPPLE_ENTER_BREAKOUT_LONG",  True),
    "ENTER_BREAKOUT_SHORT":  (SignalCategory.RIPPLE_ENTRY,   "RIPPLE_ENTER_BREAKOUT_SHORT", False),
    "EXIT_BOUNCE":           (SignalCategory.RIPPLE_EXIT,    "RIPPLE_EXIT_BOUNCE",          None),
    "EXIT_BREAKOUT":         (SignalCategory.RIPPLE_EXIT,    "RIPPLE_EXIT_BREAKOUT",        None),
    "REARM_FOR_NEXT_BOUNCE": (SignalCategory.RIPPLE_REARM,   "RIPPLE_REARM",                None),
    "CANCEL_PASSIVE_ORDERS": (SignalCategory.RIPPLE_CANCEL,  "RIPPLE_CANCEL_PASSIVE",       None),
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
    source: str = "legacy"      # "ripple", "legacy", "execution", "diagnostic"
    category: SignalCategory = SignalCategory.LEGACY_RAW
    side: str = ""              # "BUY", "SELL", ""
    price: float = 0.0
    strength: float = 0.0
    description: str = ""
    state_summary: str = ""
    suppressed: SuppressionReason = SuppressionReason.NONE


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
    if cat == SignalCategory.RIPPLE_ENTRY:
        intent_type = "entry"
    elif cat == SignalCategory.RIPPLE_EXIT:
        intent_type = "exit"
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
