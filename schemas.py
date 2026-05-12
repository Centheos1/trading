"""
Canonical data contracts, enums, and configuration for the Tide / Wave / Ripple strategy.

This module mirrors backtestingCpp/orderflow/Schemas.h.  All enum names, field
names, and default values MUST match the C++ counterpart exactly.
See strategy.md §17 for data contract specs and §29 for enum reference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


# ===================================================================
#  Canonical enums — strategy.md §29
# ===================================================================

# §29.1 Layer Enums

class TideBias(Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class VolRegime(Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRISIS = "CRISIS"


class WaveRegime(Enum):
    MEAN_REVERSION = "MEAN_REVERSION"
    BREAKOUT = "BREAKOUT"
    BREAKDOWN = "BREAKDOWN"
    NEUTRAL = "NEUTRAL"


class PermissionLevel(Enum):
    FULL = "FULL"
    REDUCED = "REDUCED"
    DISABLED = "DISABLED"


# §29.2 Ripple State Enums

class WallSide(Enum):
    BID = "BID"
    ASK = "ASK"


class LevelType(Enum):
    WALL_BID = "WALL_BID"
    WALL_ASK = "WALL_ASK"
    HVN = "HVN"
    LVN = "LVN"
    POC = "POC"
    VWAP = "VWAP"
    VOID_BOUNDARY = "VOID_BOUNDARY"


# §29.3 Trade Enums

class TradeArchetype(Enum):
    BOUNCE = "BOUNCE"
    BREAKOUT = "BREAKOUT"


class TradeSide(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class LifecycleState(Enum):
    SETUP = "SETUP"
    ENTRY = "ENTRY"
    CONFIRMATION = "CONFIRMATION"
    EXPANSION = "EXPANSION"
    MATURATION = "MATURATION"
    EXIT = "EXIT"
    COOLDOWN = "COOLDOWN"
    CANCELLED = "CANCELLED"


class ExitType(Enum):
    INVALIDATION = "INVALIDATION"
    TARGET = "TARGET"
    EXHAUSTION = "EXHAUSTION"
    TIME = "TIME"
    RISK_BUDGET = "RISK_BUDGET"


# §29.4 Execution Enums

class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class IntentType(Enum):
    ENTRY = "ENTRY"
    SCALE_IN = "SCALE_IN"
    SCALE_OUT = "SCALE_OUT"
    EXIT = "EXIT"


class Urgency(Enum):
    IMMEDIATE = "IMMEDIATE"
    NORMAL = "NORMAL"


class ExitReason(Enum):
    INVALIDATION = "INVALIDATION"
    TARGET = "TARGET"
    EXHAUSTION = "EXHAUSTION"
    TIME = "TIME"
    RISK_BUDGET = "RISK_BUDGET"
    NONE = "NONE"


# §29.5 Market Data Enums

class EventType(Enum):
    TRADE = "TRADE"
    DEPTH_UPDATE = "DEPTH_UPDATE"
    DEPTH_SNAPSHOT = "DEPTH_SNAPSHOT"


# Feature schema enums

class FeatureType(Enum):
    FLOAT64 = "FLOAT64"
    INT32 = "INT32"
    INT64 = "INT64"
    ENUM = "ENUM"
    ARRAY = "ARRAY"


class FeatureCadence(Enum):
    PER_EVENT = "PER_EVENT"
    PER_SECOND = "PER_SECOND"
    PER_FILL = "PER_FILL"
    ON_ENTRY = "ON_ENTRY"
    MS_100 = "MS_100"


class FeatureCategory(Enum):
    RAW = "RAW"
    DERIVED = "DERIVED"
    MODEL_OUTPUT = "MODEL_OUTPUT"
    STATE = "STATE"
    COMPOSITE = "COMPOSITE"
    LABEL = "LABEL"


# ===================================================================
#  Data contracts — strategy.md §17
# ===================================================================

@dataclass
class PermissionSet:
    """§17.12"""
    long_bounce: PermissionLevel = PermissionLevel.FULL
    short_bounce: PermissionLevel = PermissionLevel.FULL
    long_breakout: PermissionLevel = PermissionLevel.FULL
    short_breakout: PermissionLevel = PermissionLevel.FULL
    reduced_size_fraction: float = 0.5

    def get(self, archetype: TradeArchetype, side: TradeSide) -> PermissionLevel:
        if archetype == TradeArchetype.BOUNCE:
            return self.long_bounce if side == TradeSide.LONG else self.short_bounce
        return self.long_breakout if side == TradeSide.LONG else self.short_breakout

    def is_allowed(self, archetype: TradeArchetype, side: TradeSide) -> bool:
        return self.get(archetype, side) != PermissionLevel.DISABLED

    def size_fraction(self, archetype: TradeArchetype, side: TradeSide) -> float:
        level = self.get(archetype, side)
        if level == PermissionLevel.FULL:
            return 1.0
        if level == PermissionLevel.REDUCED:
            return self.reduced_size_fraction
        return 0.0


@dataclass
class TideSnapshot:
    """§17.13"""
    timestamp: int = 0
    bias: TideBias = TideBias.NEUTRAL
    risk_multiplier: float = 1.0
    max_position_usd: float = 10000.0
    es_budget: float = 1000.0
    consumed_es: float = 0.0
    vol_regime: VolRegime = VolRegime.NORMAL
    crypto_beta_return: float = 0.0
    realized_vol: float = 0.0
    liquidity_stress: float = 0.0


@dataclass
class WaveSnapshot:
    """§17.5"""
    timestamp: int = 0
    regime: WaveRegime = WaveRegime.NEUTRAL
    trend_efficiency: float = 0.5
    dispersion: float = 0.0
    absorption_ratio: float = 0.5
    permissions: PermissionSet = field(default_factory=PermissionSet)


@dataclass
class RiskBudgetSnapshot:
    """§17.8"""
    timestamp: int = 0
    es_budget: float = 0.0
    consumed_es: float = 0.0
    risk_multiplier: float = 1.0
    max_position_usd: float = 0.0
    bias: TideBias = TideBias.NEUTRAL
    vol_regime: VolRegime = VolRegime.NORMAL


@dataclass
class WallView:
    """§17.11"""
    price: float = 0.0
    depth: float = 0.0
    side: WallSide = WallSide.BID
    quality: float = 0.0
    persistence: float = 0.0
    cancellation_rate: float = 0.0
    first_seen_ts: int = 0
    last_update_ts: int = 0


@dataclass
class LiquidityLevel:
    """Single level in the liquidity map (§10.4)."""
    price: float = 0.0
    depth: float = 0.0
    wall_quality: float = 0.0
    profile_volume: float = 0.0
    net_flow: float = 0.0
    hold_score: float = 0.0
    dest_score: float = 0.0
    type: LevelType = LevelType.HVN


@dataclass
class VoidCorridor:
    lo: float = 0.0
    hi: float = 0.0


@dataclass
class LiquidityMapSnapshot:
    """§17.9"""
    timestamp: int = 0
    levels: List[LiquidityLevel] = field(default_factory=list)
    void_corridors: List[VoidCorridor] = field(default_factory=list)
    nearest_bid_wall: float = 0.0
    nearest_ask_wall: float = 0.0
    poc: float = 0.0
    vwap: float = 0.0


@dataclass
class TradeStateSnapshot:
    """§17.7"""
    trade_id: str = ""
    archetype: TradeArchetype = TradeArchetype.BOUNCE
    side: TradeSide = TradeSide.LONG
    state: LifecycleState = LifecycleState.SETUP
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    quantity: float = 0.0
    unrealized_pnl: float = 0.0
    hold_time_ms: int = 0
    scale_count: int = 0


@dataclass
class FillEvent:
    """§17.10"""
    timestamp: int = 0
    trade_id: str = ""
    order_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    price: float = 0.0
    quantity: float = 0.0
    commission: float = 0.0
    is_maker: bool = False


@dataclass
class RippleFeatures:
    """Mirrors C++ ripple::RippleFeatures."""
    relative_wall_size: float = 0.0
    wall_persistence_sec: float = 0.0
    wall_depletion_rate: float = 0.0
    wall_refill_rate: float = 0.0
    wall_cancel_rate: float = 0.0
    distance_to_wall_ticks: float = 0.0
    queue_stability: float = 0.0
    aggressive_flow_imbalance: float = 0.0
    trade_count_imbalance: float = 0.0
    microprice_drift: float = 0.0
    top1_imbalance: float = 0.0
    top5_imbalance: float = 0.0
    impact_per_unit_volume: float = 0.0
    spread_shock: float = 0.0
    short_horizon_volatility: float = 0.0
    time_since_wall_formed_sec: float = 0.0


@dataclass
class FeatureSnapshot:
    """§17.4"""
    timestamp: int = 0
    ripple_features: RippleFeatures = field(default_factory=RippleFeatures)


@dataclass
class RippleStateSnapshot:
    """§17.6"""
    timestamp: int = 0
    liquidity_state: str = "IDLE"
    walls: List[WallView] = field(default_factory=list)
    microprice: float = 0.0
    imbalance: float = 0.0
    cvd: float = 0.0
    ofi: float = 0.0
    lsi: float = 0.0


@dataclass
class MarketEvent:
    """§17.1"""
    timestamp: int = 0
    symbol: str = ""
    event_type: EventType = EventType.TRADE


@dataclass
class StrategyExecutionIntent:
    """§17.14"""
    timestamp: int = 0
    trade_id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    quantity: float = 0.0
    order_type: OrderType = OrderType.MARKET
    limit_price: float = 0.0
    intent_type: IntentType = IntentType.ENTRY
    exit_reason: ExitReason = ExitReason.NONE
    urgency: Urgency = Urgency.NORMAL


# ===================================================================
#  Pre-phase defaults — strategy.md §7.5
# ===================================================================

DEFAULT_TIDE_SNAPSHOT = TideSnapshot(
    timestamp=0,
    bias=TideBias.NEUTRAL,
    risk_multiplier=1.0,
    max_position_usd=10000.0,
    es_budget=1000.0,
    consumed_es=0.0,
    vol_regime=VolRegime.NORMAL,
    crypto_beta_return=0.0,
    realized_vol=0.0,
    liquidity_stress=0.0,
)

DEFAULT_WAVE_SNAPSHOT = WaveSnapshot(
    timestamp=0,
    regime=WaveRegime.NEUTRAL,
    trend_efficiency=0.5,
    dispersion=0.0,
    absorption_ratio=0.5,
    permissions=PermissionSet(),
)


# ===================================================================
#  Configuration — strategy.md §27
# ===================================================================

@dataclass
class TideConfig:
    """§27.1"""
    update_interval_ms: int = 60000
    risk_multiplier_default: float = 1.0
    es_budget_global: float = 1000.0
    max_position_usd: float = 10000.0
    vol_regime_thresholds: List[float] = field(default_factory=lambda: [0.4, 0.8, 1.5])
    lsi_weights: List[float] = field(default_factory=lambda: [0.33, 0.33, 0.34])
    risk_mult_by_regime: List[float] = field(default_factory=lambda: [1.0, 0.8, 0.5, 0.0])
    lsi_reduce_threshold: float = 1.5
    lsi_reduce_slope: float = 0.2


@dataclass
class WaveConfig:
    """§27.2"""
    update_interval_ms: int = 5000
    eta_mr_threshold: float = 0.3
    eta_bo_threshold: float = 0.7
    eta_neutral_threshold: float = 0.5
    dispersion_threshold: float = 0.02
    dispersion_critical: float = 0.05
    ar_critical: float = 0.85
    ar_recover: float = 0.70
    reduced_size_fraction: float = 0.5
    # Phase 14D — cross-venue boost factors lifted from the
    # hardcoded literals previously in wave_engine.py::_classify_regime.
    # Defaults reproduce pre-14D behaviour exactly (see strategy.md §8.4
    # and AGENT_STRATEGY_RULES.md §20 "no magic constants").
    crossvenue_divergence_boost: float = 2.0
    crossvenue_correlation_boost: float = 0.5


@dataclass
class RippleConfig:
    """§27.3 — subset of the full RippleConfig (C++ owns the canonical list)."""
    wall_min_quality: float = 0.5
    wall_quality_weights: List[float] = field(default_factory=lambda: [0.25, 0.25, 0.25, 0.25])
    confirmation_window_ms: int = 30000
    expand_threshold_sigma: float = 1.0
    max_hold_time_ms: int = 300000
    cooldown_ms: int = 10000
    scale_out_fractions: List[float] = field(default_factory=lambda: [0.33, 0.33, 0.34])
    vol_ratio_clamp: List[float] = field(default_factory=lambda: [0.25, 2.0])
    proximity_sigma: float = 2.0
    bounce_microprice_shift_ticks: float = 3.0
    bounce_imbalance_thresh: float = 0.1
    exhaustion_cvd_slope_thresh: float = 0.01
    breakout_depth_fail_ratio: float = 0.3
    breakout_depth_fail_abs: float = 0.0
    breakout_cvd_slope_thresh: float = 0.05
    exhaustion_impact_ratio: float = 1.5
    max_scale_ins: int = 1
    scale_in_threshold_sigma: float = 2.0
    scale_in_cvd_slope_min: float = 0.02
    trailing_stop_sigma: float = 2.0
    wall_scan_depth: int = 50
    wall_depth_multiple: float = 3.0
    wall_cancel_window: int = 20
    state_activation_threshold: float = 0.6
    state_margin: float = 0.15
    # Phase 15 — LIMIT price validation band (σ from microprice)
    limit_price_band_sigma: float = 3.0


@dataclass
class RiskConfig:
    """§27.4"""
    es_confidence_level: float = 0.95
    target_risk_usd: float = 50.0
    sigma_target: float = 0.60
    maker_fee_bps: float = 2.0
    taker_fee_bps: float = 4.0
    leverage: float = 1.0
    budget_exit_threshold: float = 0.90


@dataclass
class ExecutionConfig:
    """§27.5"""
    bounce_order_type: str = "LIMIT"
    breakout_order_type: str = "MARKET"
    slippage_tolerance_bps: float = 5.0
    # Phase 15 — LIMIT / OCO order parameters
    limit_timeout_ms: int = 30_000  # cancel OPEN LIMIT after this event-time age


@dataclass
class TestingConfig:
    """§27.6"""
    replay_tolerance: float = 0.0
    benchmark_latency_target_us: int = 100


@dataclass
class StrategyConfig:
    """Top-level configuration container — all tunables for the strategy."""
    tide: TideConfig = field(default_factory=TideConfig)
    wave: WaveConfig = field(default_factory=WaveConfig)
    ripple: RippleConfig = field(default_factory=RippleConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    testing: TestingConfig = field(default_factory=TestingConfig)

    def save(self, path: str | Path) -> None:
        """Serialize to JSON file."""
        path = Path(path)
        data = _config_to_dict(self)
        path.write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "StrategyConfig":
        """Deserialize from JSON file."""
        path = Path(path)
        data = json.loads(path.read_text())
        return _config_from_dict(data)


def _config_to_dict(cfg: StrategyConfig) -> dict:
    from dataclasses import asdict
    d = asdict(cfg)
    return d


def _config_from_dict(d: dict) -> StrategyConfig:
    cfg = StrategyConfig()
    if "tide" in d:
        for k, v in d["tide"].items():
            if hasattr(cfg.tide, k):
                setattr(cfg.tide, k, v)
    if "wave" in d:
        for k, v in d["wave"].items():
            if hasattr(cfg.wave, k):
                setattr(cfg.wave, k, v)
    if "ripple" in d:
        for k, v in d["ripple"].items():
            if hasattr(cfg.ripple, k):
                setattr(cfg.ripple, k, v)
    if "risk" in d:
        for k, v in d["risk"].items():
            if hasattr(cfg.risk, k):
                setattr(cfg.risk, k, v)
    if "execution" in d:
        for k, v in d["execution"].items():
            if hasattr(cfg.execution, k):
                setattr(cfg.execution, k, v)
    if "testing" in d:
        for k, v in d["testing"].items():
            if hasattr(cfg.testing, k):
                setattr(cfg.testing, k, v)
    return cfg


# ===================================================================
#  Feature schema registry — strategy.md §16
# ===================================================================

@dataclass
class FeatureDescriptor:
    full_name: str
    ns: str
    type: FeatureType
    cadence: FeatureCadence
    category: FeatureCategory


FEATURE_REGISTRY: List[FeatureDescriptor] = [
    # §16.1 tide.*
    FeatureDescriptor("tide.crypto_beta_return", "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.RAW),
    FeatureDescriptor("tide.realized_vol",       "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("tide.funding_rate",       "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.RAW),
    FeatureDescriptor("tide.oi_change_pct",      "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("tide.vol_regime",         "tide", FeatureType.ENUM,    FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    FeatureDescriptor("tide.macro_sentiment",    "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("tide.liquidity_stress",   "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("tide.bias",               "tide", FeatureType.ENUM,    FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    FeatureDescriptor("tide.risk_multiplier",    "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    FeatureDescriptor("tide.max_position_usd",   "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("tide.es_budget",          "tide", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    # §16.2 wave.*
    FeatureDescriptor("wave.trend_efficiency",      "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.dispersion",            "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.absorption_ratio",      "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.residual_dislocation",  "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.distance_vwap",         "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.distance_session_high", "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.distance_session_low",  "wave", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("wave.regime",                "wave", FeatureType.ENUM,    FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    FeatureDescriptor("wave.permissions",           "wave", FeatureType.ENUM,    FeatureCadence.PER_SECOND, FeatureCategory.MODEL_OUTPUT),
    # §16.3 ripple.*
    FeatureDescriptor("ripple.imbalance",              "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.microprice",             "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.ofi",                    "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.cvd",                    "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.cvd_slope",              "ripple", FeatureType.FLOAT64, FeatureCadence.MS_100,    FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.impact",                 "ripple", FeatureType.FLOAT64, FeatureCadence.MS_100,    FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.vpin",                   "ripple", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.lsi",                    "ripple", FeatureType.FLOAT64, FeatureCadence.MS_100,    FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.liquidity_state",        "ripple", FeatureType.ENUM,    FeatureCadence.PER_EVENT, FeatureCategory.MODEL_OUTPUT),
    FeatureDescriptor("ripple.best_bid_wall_quality",  "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.best_ask_wall_quality",  "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    # engine-derived extensions (not in §16, computed by existing Ripple pipeline)
    FeatureDescriptor("ripple.impact_per_unit_volume", "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.wall_count",             "ripple", FeatureType.INT32,   FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.nearest_wall_distance",  "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.evidence_absorption",    "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.evidence_exhaustion",    "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.evidence_withdrawal",    "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("ripple.evidence_breakout",      "ripple", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    # §16.4 liquidity_map.*
    FeatureDescriptor("liquidity_map.nearest_bid_wall", "liquidity_map", FeatureType.FLOAT64, FeatureCadence.PER_EVENT,  FeatureCategory.DERIVED),
    FeatureDescriptor("liquidity_map.nearest_ask_wall", "liquidity_map", FeatureType.FLOAT64, FeatureCadence.PER_EVENT,  FeatureCategory.DERIVED),
    FeatureDescriptor("liquidity_map.poc",              "liquidity_map", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("liquidity_map.vwap",             "liquidity_map", FeatureType.FLOAT64, FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("liquidity_map.void_count",       "liquidity_map", FeatureType.INT32,   FeatureCadence.PER_SECOND, FeatureCategory.DERIVED),
    FeatureDescriptor("liquidity_map.levels",           "liquidity_map", FeatureType.ARRAY,   FeatureCadence.PER_EVENT,  FeatureCategory.COMPOSITE),
    # §16.5 risk.*
    FeatureDescriptor("risk.consumed_es",       "risk", FeatureType.FLOAT64, FeatureCadence.PER_FILL, FeatureCategory.DERIVED),
    FeatureDescriptor("risk.remaining_budget",  "risk", FeatureType.FLOAT64, FeatureCadence.PER_FILL, FeatureCategory.DERIVED),
    FeatureDescriptor("risk.position_usd",      "risk", FeatureType.FLOAT64, FeatureCadence.PER_FILL, FeatureCategory.DERIVED),
    FeatureDescriptor("risk.unrealized_pnl",    "risk", FeatureType.FLOAT64, FeatureCadence.MS_100,   FeatureCategory.DERIVED),
    # §16.6 trade.*
    FeatureDescriptor("trade.state",            "trade", FeatureType.ENUM,    FeatureCadence.PER_EVENT, FeatureCategory.STATE),
    FeatureDescriptor("trade.archetype",        "trade", FeatureType.ENUM,    FeatureCadence.ON_ENTRY,  FeatureCategory.LABEL),
    FeatureDescriptor("trade.entry_price",      "trade", FeatureType.FLOAT64, FeatureCadence.PER_FILL,  FeatureCategory.RAW),
    FeatureDescriptor("trade.stop_price",       "trade", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("trade.target_price",     "trade", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("trade.hold_time_ms",     "trade", FeatureType.INT64,   FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("trade.unrealized_pnl",   "trade", FeatureType.FLOAT64, FeatureCadence.PER_EVENT, FeatureCategory.DERIVED),
    FeatureDescriptor("trade.scale_count",      "trade", FeatureType.INT32,   FeatureCadence.PER_FILL,  FeatureCategory.STATE),
]

# Canonical feature names from strategy.md §16 (used by tests)
CANONICAL_FEATURES_S16: List[str] = [
    # §16.1 tide
    "tide.crypto_beta_return", "tide.realized_vol", "tide.funding_rate",
    "tide.oi_change_pct", "tide.vol_regime", "tide.macro_sentiment",
    "tide.liquidity_stress", "tide.bias", "tide.risk_multiplier",
    "tide.max_position_usd", "tide.es_budget",
    # §16.2 wave
    "wave.trend_efficiency", "wave.dispersion", "wave.absorption_ratio",
    "wave.residual_dislocation", "wave.distance_vwap",
    "wave.distance_session_high", "wave.distance_session_low",
    "wave.regime", "wave.permissions",
    # §16.3 ripple
    "ripple.imbalance", "ripple.microprice", "ripple.ofi", "ripple.cvd",
    "ripple.cvd_slope", "ripple.impact", "ripple.vpin", "ripple.lsi",
    "ripple.liquidity_state", "ripple.best_bid_wall_quality",
    "ripple.best_ask_wall_quality",
    # §16.4 liquidity_map
    "liquidity_map.nearest_bid_wall", "liquidity_map.nearest_ask_wall",
    "liquidity_map.poc", "liquidity_map.vwap", "liquidity_map.void_count",
    "liquidity_map.levels",
    # §16.5 risk
    "risk.consumed_es", "risk.remaining_budget", "risk.position_usd",
    "risk.unrealized_pnl",
    # §16.6 trade
    "trade.state", "trade.archetype", "trade.entry_price", "trade.stop_price",
    "trade.target_price", "trade.hold_time_ms", "trade.unrealized_pnl",
    "trade.scale_count",
]


# ===================================================================
#  Strategy-level data contracts — §17.2 and §17.3
# ===================================================================

@dataclass
class TradeEventContract:
    """§17.2 — strategy-level trade event."""
    timestamp: int = 0
    symbol: str = ""
    price: float = 0.0
    quantity: float = 0.0
    is_buyer_maker: bool = False


@dataclass
class DepthUpdateContract:
    """§17.3 — strategy-level depth update."""
    timestamp: int = 0
    symbol: str = ""
    bids: List[tuple] = field(default_factory=list)
    asks: List[tuple] = field(default_factory=list)
    first_update_id: int = 0
    last_update_id: int = 0


# ===================================================================
#  Composite strategy snapshot
# ===================================================================

@dataclass
class StrategySnapshot:
    """All layer states in one place.  Constructed per-event (or on demand)
    to give downstream consumers a single coherent view."""
    timestamp: int = 0
    tide: TideSnapshot = field(default_factory=TideSnapshot)
    wave: WaveSnapshot = field(default_factory=WaveSnapshot)
    ripple: RippleStateSnapshot = field(default_factory=RippleStateSnapshot)
    trade: TradeStateSnapshot = field(default_factory=TradeStateSnapshot)
    liquidity_map: LiquidityMapSnapshot = field(default_factory=LiquidityMapSnapshot)
    risk: RiskBudgetSnapshot = field(default_factory=RiskBudgetSnapshot)
    features: FeatureSnapshot = field(default_factory=FeatureSnapshot)

    @staticmethod
    def make_default() -> "StrategySnapshot":
        return StrategySnapshot(
            tide=TideSnapshot(
                bias=TideBias.NEUTRAL,
                risk_multiplier=1.0,
                max_position_usd=10000.0,
                es_budget=1000.0,
                vol_regime=VolRegime.NORMAL,
            ),
            wave=WaveSnapshot(
                regime=WaveRegime.NEUTRAL,
                trend_efficiency=0.5,
                permissions=PermissionSet(),
            ),
        )


DEFAULT_STRATEGY_SNAPSHOT = StrategySnapshot.make_default()
