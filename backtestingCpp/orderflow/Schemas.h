#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>
#include "ripple/RippleConfig.h"
#include "ripple/RippleTypes.h"

namespace orderflow {

// ===================================================================
//  Canonical enums — strategy.md §29
//  Names and values MUST match strategy.md exactly.
// ===================================================================

// §29.1 Layer Enums

enum class TideBias : uint8_t { LONG, SHORT, NEUTRAL };

enum class VolRegime : uint8_t { LOW, NORMAL, HIGH, CRISIS };

enum class WaveRegime : uint8_t { MEAN_REVERSION, BREAKOUT, BREAKDOWN, NEUTRAL };

enum class PermissionLevel : uint8_t { FULL, REDUCED, DISABLED };

// §29.2 Ripple State Enums
// Note: WallSide and RippleState/LiquidityState are defined in RippleTypes.h
// in the orderflow::ripple namespace. Re-export here for convenience.
using ripple::WallSide;

enum class LevelType : uint8_t {
    WALL_BID, WALL_ASK, HVN, LVN, POC, VWAP, VOID_BOUNDARY
};

// §29.3 Trade Enums

enum class TradeArchetype : uint8_t { BOUNCE, BREAKOUT };

enum class TradeSide : uint8_t { LONG, SHORT };

enum class LifecycleState : uint8_t {
    SETUP, ENTRY, CONFIRMATION, EXPANSION, MATURATION, EXIT, COOLDOWN, CANCELLED
};

enum class ExitType : uint8_t {
    INVALIDATION, TARGET, EXHAUSTION, TIME, RISK_BUDGET
};

// §29.4 Execution Enums

enum class OrderSide : uint8_t { BUY, SELL };

enum class OrderType : uint8_t { MARKET, LIMIT };

enum class IntentType : uint8_t { ENTRY, SCALE_IN, SCALE_OUT, EXIT };

enum class Urgency : uint8_t { IMMEDIATE, NORMAL };

enum class ExitReason : uint8_t {
    INVALIDATION, TARGET, EXHAUSTION, TIME, RISK_BUDGET, NONE
};

// §29.5 Market Data Enums

enum class EventType : uint8_t { TRADE, DEPTH_UPDATE, DEPTH_SNAPSHOT };

// ===================================================================
//  to_string helpers
// ===================================================================

inline const char* to_string(TideBias b) {
    switch (b) {
        case TideBias::LONG:    return "LONG";
        case TideBias::SHORT:   return "SHORT";
        case TideBias::NEUTRAL: return "NEUTRAL";
    }
    return "?";
}

inline const char* to_string(VolRegime r) {
    switch (r) {
        case VolRegime::LOW:    return "LOW";
        case VolRegime::NORMAL: return "NORMAL";
        case VolRegime::HIGH:   return "HIGH";
        case VolRegime::CRISIS: return "CRISIS";
    }
    return "?";
}

inline const char* to_string(WaveRegime r) {
    switch (r) {
        case WaveRegime::MEAN_REVERSION: return "MEAN_REVERSION";
        case WaveRegime::BREAKOUT:       return "BREAKOUT";
        case WaveRegime::BREAKDOWN:      return "BREAKDOWN";
        case WaveRegime::NEUTRAL:        return "NEUTRAL";
    }
    return "?";
}

inline const char* to_string(PermissionLevel p) {
    switch (p) {
        case PermissionLevel::FULL:     return "FULL";
        case PermissionLevel::REDUCED:  return "REDUCED";
        case PermissionLevel::DISABLED: return "DISABLED";
    }
    return "?";
}

inline const char* to_string(LevelType t) {
    switch (t) {
        case LevelType::WALL_BID:      return "WALL_BID";
        case LevelType::WALL_ASK:      return "WALL_ASK";
        case LevelType::HVN:           return "HVN";
        case LevelType::LVN:           return "LVN";
        case LevelType::POC:           return "POC";
        case LevelType::VWAP:          return "VWAP";
        case LevelType::VOID_BOUNDARY: return "VOID_BOUNDARY";
    }
    return "?";
}

inline const char* to_string(TradeArchetype a) {
    switch (a) {
        case TradeArchetype::BOUNCE:   return "BOUNCE";
        case TradeArchetype::BREAKOUT: return "BREAKOUT";
    }
    return "?";
}

inline const char* to_string(TradeSide s) {
    switch (s) {
        case TradeSide::LONG:  return "LONG";
        case TradeSide::SHORT: return "SHORT";
    }
    return "?";
}

inline const char* to_string(LifecycleState s) {
    switch (s) {
        case LifecycleState::SETUP:        return "SETUP";
        case LifecycleState::ENTRY:        return "ENTRY";
        case LifecycleState::CONFIRMATION: return "CONFIRMATION";
        case LifecycleState::EXPANSION:    return "EXPANSION";
        case LifecycleState::MATURATION:   return "MATURATION";
        case LifecycleState::EXIT:         return "EXIT";
        case LifecycleState::COOLDOWN:     return "COOLDOWN";
        case LifecycleState::CANCELLED:    return "CANCELLED";
    }
    return "?";
}

inline const char* to_string(ExitType t) {
    switch (t) {
        case ExitType::INVALIDATION: return "INVALIDATION";
        case ExitType::TARGET:       return "TARGET";
        case ExitType::EXHAUSTION:   return "EXHAUSTION";
        case ExitType::TIME:         return "TIME";
        case ExitType::RISK_BUDGET:  return "RISK_BUDGET";
    }
    return "?";
}

inline const char* to_string(OrderSide s) {
    switch (s) {
        case OrderSide::BUY:  return "BUY";
        case OrderSide::SELL: return "SELL";
    }
    return "?";
}

inline const char* to_string(OrderType t) {
    switch (t) {
        case OrderType::MARKET: return "MARKET";
        case OrderType::LIMIT:  return "LIMIT";
    }
    return "?";
}

inline const char* to_string(IntentType t) {
    switch (t) {
        case IntentType::ENTRY:     return "ENTRY";
        case IntentType::SCALE_IN:  return "SCALE_IN";
        case IntentType::SCALE_OUT: return "SCALE_OUT";
        case IntentType::EXIT:      return "EXIT";
    }
    return "?";
}

inline const char* to_string(Urgency u) {
    switch (u) {
        case Urgency::IMMEDIATE: return "IMMEDIATE";
        case Urgency::NORMAL:    return "NORMAL";
    }
    return "?";
}

inline const char* to_string(ExitReason r) {
    switch (r) {
        case ExitReason::INVALIDATION: return "INVALIDATION";
        case ExitReason::TARGET:       return "TARGET";
        case ExitReason::EXHAUSTION:   return "EXHAUSTION";
        case ExitReason::TIME:         return "TIME";
        case ExitReason::RISK_BUDGET:  return "RISK_BUDGET";
        case ExitReason::NONE:         return "NONE";
    }
    return "?";
}

inline const char* to_string(EventType t) {
    switch (t) {
        case EventType::TRADE:          return "TRADE";
        case EventType::DEPTH_UPDATE:   return "DEPTH_UPDATE";
        case EventType::DEPTH_SNAPSHOT: return "DEPTH_SNAPSHOT";
    }
    return "?";
}

// ===================================================================
//  Data contracts — strategy.md §17
// ===================================================================

// §17.12 Permission Set
struct PermissionSet {
    PermissionLevel long_bounce    = PermissionLevel::FULL;
    PermissionLevel short_bounce   = PermissionLevel::FULL;
    PermissionLevel long_breakout  = PermissionLevel::FULL;
    PermissionLevel short_breakout = PermissionLevel::FULL;
    double reduced_size_fraction   = 0.5;

    PermissionLevel get(TradeArchetype archetype, TradeSide side) const {
        if (archetype == TradeArchetype::BOUNCE) {
            return (side == TradeSide::LONG) ? long_bounce : short_bounce;
        }
        return (side == TradeSide::LONG) ? long_breakout : short_breakout;
    }

    bool is_allowed(TradeArchetype archetype, TradeSide side) const {
        return get(archetype, side) != PermissionLevel::DISABLED;
    }

    double size_fraction(TradeArchetype archetype, TradeSide side) const {
        auto level = get(archetype, side);
        if (level == PermissionLevel::FULL)    return 1.0;
        if (level == PermissionLevel::REDUCED) return reduced_size_fraction;
        return 0.0;
    }
};

// §17.13 Tide Snapshot
struct TideSnapshot {
    int64_t  timestamp        = 0;
    TideBias bias             = TideBias::NEUTRAL;
    double   risk_multiplier  = 1.0;
    double   max_position_usd = 10000.0;
    double   es_budget        = 1000.0;
    double   consumed_es      = 0.0;
    VolRegime vol_regime      = VolRegime::NORMAL;
    double   crypto_beta_return = 0.0;
    double   realized_vol     = 0.0;
    double   liquidity_stress = 0.0;
};

// §17.5 Wave State / Wave Snapshot
struct WaveSnapshot {
    int64_t     timestamp         = 0;
    WaveRegime  regime            = WaveRegime::NEUTRAL;
    double      trend_efficiency  = 0.5;
    double      dispersion        = 0.0;
    double      absorption_ratio  = 0.5;
    PermissionSet permissions;
};

// §17.8 Risk Budget Snapshot
struct RiskBudgetSnapshot {
    int64_t   timestamp        = 0;
    double    es_budget        = 0.0;
    double    consumed_es      = 0.0;
    double    risk_multiplier  = 1.0;
    double    max_position_usd = 0.0;
    TideBias  bias             = TideBias::NEUTRAL;
    VolRegime vol_regime       = VolRegime::NORMAL;
};

// §17.11 Wall (public cross-module view)
struct WallView {
    double    price             = 0.0;
    double    depth             = 0.0;
    WallSide  side              = WallSide::BID;
    double    quality           = 0.0;
    double    persistence       = 0.0;
    double    cancellation_rate = 0.0;
    int64_t   first_seen_ts     = 0;
    int64_t   last_update_ts    = 0;
};

// §17.9 / §10.4 Liquidity Map types
struct LiquidityLevel {
    double    price          = 0.0;
    double    depth          = 0.0;
    double    wall_quality   = 0.0;
    double    profile_volume = 0.0;
    double    net_flow       = 0.0;
    double    hold_score     = 0.0;
    double    dest_score     = 0.0;
    LevelType type           = LevelType::HVN;
};

struct VoidCorridor {
    double lo = 0.0;
    double hi = 0.0;
};

struct LiquidityMapSnapshot {
    int64_t                     timestamp        = 0;
    std::vector<LiquidityLevel> levels;
    std::vector<VoidCorridor>   void_corridors;
    double                      nearest_bid_wall = 0.0;
    double                      nearest_ask_wall = 0.0;
    double                      poc              = 0.0;
    double                      vwap             = 0.0;
};

// §17.7 Trade State
struct TradeStateSnapshot {
    std::string    trade_id;
    TradeArchetype archetype      = TradeArchetype::BOUNCE;
    TradeSide      side           = TradeSide::LONG;
    LifecycleState state          = LifecycleState::SETUP;
    double         entry_price    = 0.0;
    double         stop_price     = 0.0;
    double         target_price   = 0.0;
    double         quantity       = 0.0;
    double         unrealized_pnl = 0.0;
    int64_t        hold_time_ms   = 0;
    int32_t        scale_count    = 0;
};

// §17.10 Fill Event
struct FillEvent {
    int64_t     timestamp  = 0;
    std::string trade_id;
    std::string order_id;
    std::string symbol;
    OrderSide   side       = OrderSide::BUY;
    double      price      = 0.0;
    double      quantity   = 0.0;
    double      commission = 0.0;
    bool        is_maker   = false;
};

// §17.4 Feature Snapshot (lightweight wrapper)
struct FeatureSnapshot {
    int64_t timestamp = 0;
    ripple::RippleFeatures ripple_features;
};

// §17.6 Ripple State Snapshot
struct RippleStateSnapshot {
    int64_t              timestamp       = 0;
    ripple::RippleState  liquidity_state = ripple::RippleState::IDLE;
    std::vector<WallView> walls;
    double               microprice      = 0.0;
    double               imbalance       = 0.0;
    double               cvd             = 0.0;
    double               ofi             = 0.0;
    double               lsi             = 0.0;
};

// §17.1 Market Event
struct MarketEvent {
    int64_t     timestamp = 0;
    std::string symbol;
    EventType   event_type = EventType::TRADE;
};

// §17.14 Execution Intent (strategy-level, distinct from RippleDecision)
struct StrategyExecutionIntent {
    int64_t     timestamp   = 0;
    std::string trade_id;
    std::string symbol;
    OrderSide   side        = OrderSide::BUY;
    double      quantity    = 0.0;
    OrderType   order_type  = OrderType::MARKET;
    double      limit_price = 0.0;
    IntentType  intent_type = IntentType::ENTRY;
    ExitReason  exit_reason = ExitReason::NONE;
    Urgency     urgency     = Urgency::NORMAL;
};

// ===================================================================
//  Pre-phase defaults — strategy.md §7.5
//  Used by downstream layers before Tide/Wave are implemented.
// ===================================================================

struct DefaultTideSnapshot {
    static constexpr TideBias  BIAS             = TideBias::NEUTRAL;
    static constexpr double    RISK_MULTIPLIER  = 1.0;
    static constexpr double    MAX_POSITION_USD = 10000.0;
    static constexpr double    ES_BUDGET        = 1000.0;
    static constexpr VolRegime VOL_REGIME       = VolRegime::NORMAL;
    static constexpr int64_t   UPDATE_TS        = 0;

    static TideSnapshot make() {
        TideSnapshot s;
        s.bias             = BIAS;
        s.risk_multiplier  = RISK_MULTIPLIER;
        s.max_position_usd = MAX_POSITION_USD;
        s.es_budget        = ES_BUDGET;
        s.vol_regime       = VOL_REGIME;
        s.timestamp        = UPDATE_TS;
        return s;
    }
};

struct DefaultWaveSnapshot {
    static constexpr WaveRegime REGIME           = WaveRegime::NEUTRAL;
    static constexpr double     TREND_EFFICIENCY = 0.5;
    static constexpr double     DISPERSION       = 0.0;
    static constexpr double     ABSORPTION_RATIO = 0.5;
    static constexpr int64_t    UPDATE_TS        = 0;

    static WaveSnapshot make() {
        WaveSnapshot s;
        s.regime           = REGIME;
        s.trend_efficiency = TREND_EFFICIENCY;
        s.dispersion       = DISPERSION;
        s.absorption_ratio = ABSORPTION_RATIO;
        s.permissions      = PermissionSet{};
        s.timestamp        = UPDATE_TS;
        return s;
    }
};

// ===================================================================
//  Configuration — strategy.md §27, AGENT_STRATEGY_RULES.md §20
// ===================================================================

struct TideConfig {
    int64_t update_interval_ms       = 60000;
    double  risk_multiplier_default  = 1.0;
    double  es_budget_global         = 1000.0;
    double  max_position_usd         = 10000.0;
    std::array<double, 3> vol_regime_thresholds = {0.4, 0.8, 1.5};
    std::array<double, 3> lsi_weights           = {0.33, 0.33, 0.34};
    std::array<double, 4> risk_mult_by_regime   = {1.0, 0.8, 0.5, 0.0};
    double  lsi_reduce_threshold     = 1.5;
    double  lsi_reduce_slope         = 0.2;
};

struct WaveConfig {
    int64_t update_interval_ms    = 5000;
    double  eta_mr_threshold      = 0.3;
    double  eta_bo_threshold      = 0.7;
    double  eta_neutral_threshold = 0.5;
    double  dispersion_threshold  = 0.02;
    double  dispersion_critical   = 0.05;
    double  ar_critical           = 0.85;
    double  ar_recover            = 0.70;
    double  reduced_size_fraction = 0.5;
};

struct RiskConfig {
    double  es_confidence_level    = 0.95;
    double  target_risk_usd        = 50.0;
    double  sigma_target           = 0.60;
    double  maker_fee_bps          = 2.0;
    double  taker_fee_bps          = 4.0;
    double  leverage               = 1.0;
    double  budget_exit_threshold  = 0.90;
};

struct ExecutionConfig {
    OrderType bounce_order_type        = OrderType::LIMIT;
    OrderType breakout_order_type      = OrderType::MARKET;
    double    slippage_tolerance_bps   = 5.0;
};

struct TestingConfig {
    double  replay_tolerance             = 0.0;
    int64_t benchmark_latency_target_us  = 100;
};

struct StrategyConfig {
    TideConfig              tide;
    WaveConfig              wave;
    ripple::RippleConfig    ripple;
    RiskConfig              risk;
    ExecutionConfig         execution;
    TestingConfig           testing;
};

// ===================================================================
//  Feature schema registry — strategy.md §16
// ===================================================================

enum class FeatureType : uint8_t { FLOAT64, INT32, INT64, ENUM, ARRAY };
enum class FeatureCadence : uint8_t { PER_EVENT, PER_SECOND, PER_FILL, ON_ENTRY, MS_100 };
enum class FeatureCategory : uint8_t { RAW, DERIVED, MODEL_OUTPUT, STATE, COMPOSITE, LABEL };

struct FeatureDescriptor {
    const char* full_name;
    const char* ns;
    FeatureType type;
    FeatureCadence cadence;
    FeatureCategory category;
};

inline constexpr FeatureDescriptor FEATURE_REGISTRY[] = {
    // §16.1 tide.*
    {"tide.crypto_beta_return", "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::RAW},
    {"tide.realized_vol",       "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"tide.funding_rate",       "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::RAW},
    {"tide.oi_change_pct",      "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"tide.vol_regime",         "tide", FeatureType::ENUM,    FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},
    {"tide.macro_sentiment",    "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"tide.liquidity_stress",   "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"tide.bias",               "tide", FeatureType::ENUM,    FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},
    {"tide.risk_multiplier",    "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},
    {"tide.max_position_usd",   "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"tide.es_budget",          "tide", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},

    // §16.2 wave.*
    {"wave.trend_efficiency",      "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.dispersion",            "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.absorption_ratio",      "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.residual_dislocation",  "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.distance_vwap",         "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.distance_session_high", "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.distance_session_low",  "wave", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"wave.regime",                "wave", FeatureType::ENUM,    FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},
    {"wave.permissions",           "wave", FeatureType::ENUM,    FeatureCadence::PER_SECOND, FeatureCategory::MODEL_OUTPUT},

    // §16.3 ripple.*
    {"ripple.imbalance",                "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.microprice",               "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.ofi",                      "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.cvd",                      "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.cvd_slope",                "ripple", FeatureType::FLOAT64, FeatureCadence::MS_100,    FeatureCategory::DERIVED},
    {"ripple.impact",                   "ripple", FeatureType::FLOAT64, FeatureCadence::MS_100,    FeatureCategory::DERIVED},
    {"ripple.vpin",                     "ripple", FeatureType::FLOAT64, FeatureCadence::PER_SECOND, FeatureCategory::DERIVED},
    {"ripple.lsi",                      "ripple", FeatureType::FLOAT64, FeatureCadence::MS_100,    FeatureCategory::DERIVED},
    {"ripple.liquidity_state",          "ripple", FeatureType::ENUM,    FeatureCadence::PER_EVENT, FeatureCategory::MODEL_OUTPUT},
    {"ripple.best_bid_wall_quality",    "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.best_ask_wall_quality",    "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    // engine-derived extensions (not in §16, computed by existing Ripple pipeline)
    {"ripple.impact_per_unit_volume",   "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.wall_count",               "ripple", FeatureType::INT32,   FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.nearest_wall_distance",    "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.evidence_absorption",      "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.evidence_exhaustion",      "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.evidence_withdrawal",      "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"ripple.evidence_breakout",        "ripple", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},

    // §16.4 liquidity_map.*
    {"liquidity_map.nearest_bid_wall",  "liquidity_map", FeatureType::FLOAT64, FeatureCadence::PER_EVENT,   FeatureCategory::DERIVED},
    {"liquidity_map.nearest_ask_wall",  "liquidity_map", FeatureType::FLOAT64, FeatureCadence::PER_EVENT,   FeatureCategory::DERIVED},
    {"liquidity_map.poc",               "liquidity_map", FeatureType::FLOAT64, FeatureCadence::PER_SECOND,  FeatureCategory::DERIVED},
    {"liquidity_map.vwap",              "liquidity_map", FeatureType::FLOAT64, FeatureCadence::PER_SECOND,  FeatureCategory::DERIVED},
    {"liquidity_map.void_count",        "liquidity_map", FeatureType::INT32,   FeatureCadence::PER_SECOND,  FeatureCategory::DERIVED},
    {"liquidity_map.levels",            "liquidity_map", FeatureType::ARRAY,   FeatureCadence::PER_EVENT,   FeatureCategory::COMPOSITE},

    // §16.5 risk.*
    {"risk.consumed_es",                "risk", FeatureType::FLOAT64, FeatureCadence::PER_FILL,  FeatureCategory::DERIVED},
    {"risk.remaining_budget",           "risk", FeatureType::FLOAT64, FeatureCadence::PER_FILL,  FeatureCategory::DERIVED},
    {"risk.position_usd",              "risk", FeatureType::FLOAT64, FeatureCadence::PER_FILL,  FeatureCategory::DERIVED},
    {"risk.unrealized_pnl",            "risk", FeatureType::FLOAT64, FeatureCadence::MS_100,    FeatureCategory::DERIVED},

    // §16.6 trade.*
    {"trade.state",                    "trade", FeatureType::ENUM,    FeatureCadence::PER_EVENT, FeatureCategory::STATE},
    {"trade.archetype",                "trade", FeatureType::ENUM,    FeatureCadence::ON_ENTRY,  FeatureCategory::LABEL},
    {"trade.entry_price",              "trade", FeatureType::FLOAT64, FeatureCadence::PER_FILL,  FeatureCategory::RAW},
    {"trade.stop_price",               "trade", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"trade.target_price",             "trade", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"trade.hold_time_ms",             "trade", FeatureType::INT64,   FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"trade.unrealized_pnl",           "trade", FeatureType::FLOAT64, FeatureCadence::PER_EVENT, FeatureCategory::DERIVED},
    {"trade.scale_count",              "trade", FeatureType::INT32,   FeatureCadence::PER_FILL,  FeatureCategory::STATE},
};

inline constexpr size_t FEATURE_REGISTRY_SIZE =
    sizeof(FEATURE_REGISTRY) / sizeof(FEATURE_REGISTRY[0]);

// ===================================================================
//  Strategy-level data contracts — §17.2 and §17.3
//  These wrap the engine-level types with the canonical field names
//  from strategy.md.  The engine's Trade/DepthUpdate types are the
//  authoritative hot-path representations; these contracts are for
//  cross-layer and cross-language use.
// ===================================================================

// §17.2 Trade Event
struct TradeEventContract {
    int64_t     timestamp      = 0;
    std::string symbol;
    double      price          = 0.0;
    double      quantity       = 0.0;
    bool        is_buyer_maker = false;
};

// §17.3 Depth Update (strategy-level view)
struct DepthUpdateContract {
    int64_t     timestamp       = 0;
    std::string symbol;
    std::vector<std::pair<double, double>> bids;
    std::vector<std::pair<double, double>> asks;
    int64_t     first_update_id = 0;
    int64_t     last_update_id  = 0;
};

// ===================================================================
//  Composite strategy snapshot — all layer states in one place.
//  Constructed per-event (or on demand) to give downstream consumers
//  a single coherent view of the full strategy state.
//  This is the primary interface between the C++ hot-path engines
//  and the Python analytics/UI layer.
// ===================================================================

struct StrategySnapshot {
    int64_t              timestamp     = 0;
    TideSnapshot         tide;
    WaveSnapshot         wave;
    RippleStateSnapshot  ripple;
    TradeStateSnapshot   trade;
    LiquidityMapSnapshot liquidity_map;
    RiskBudgetSnapshot   risk;
    FeatureSnapshot      features;

    static StrategySnapshot make_default() {
        StrategySnapshot ss;
        ss.tide = DefaultTideSnapshot::make();
        ss.wave = DefaultWaveSnapshot::make();
        return ss;
    }
};

// ===================================================================
//  Canonical feature names from strategy.md §16.
//  Used by tests to validate registry completeness.
// ===================================================================

inline constexpr const char* CANONICAL_FEATURES_S16[] = {
    // §16.1 tide
    "tide.crypto_beta_return", "tide.realized_vol", "tide.funding_rate",
    "tide.oi_change_pct", "tide.vol_regime", "tide.macro_sentiment",
    "tide.liquidity_stress", "tide.bias", "tide.risk_multiplier",
    "tide.max_position_usd", "tide.es_budget",
    // §16.2 wave
    "wave.trend_efficiency", "wave.dispersion", "wave.absorption_ratio",
    "wave.residual_dislocation", "wave.distance_vwap",
    "wave.distance_session_high", "wave.distance_session_low",
    "wave.regime", "wave.permissions",
    // §16.3 ripple
    "ripple.imbalance", "ripple.microprice", "ripple.ofi", "ripple.cvd",
    "ripple.cvd_slope", "ripple.impact", "ripple.vpin", "ripple.lsi",
    "ripple.liquidity_state", "ripple.best_bid_wall_quality",
    "ripple.best_ask_wall_quality",
    // §16.4 liquidity_map
    "liquidity_map.nearest_bid_wall", "liquidity_map.nearest_ask_wall",
    "liquidity_map.poc", "liquidity_map.vwap", "liquidity_map.void_count",
    "liquidity_map.levels",
    // §16.5 risk
    "risk.consumed_es", "risk.remaining_budget", "risk.position_usd",
    "risk.unrealized_pnl",
    // §16.6 trade
    "trade.state", "trade.archetype", "trade.entry_price", "trade.stop_price",
    "trade.target_price", "trade.hold_time_ms", "trade.unrealized_pnl",
    "trade.scale_count",
};

inline constexpr size_t CANONICAL_FEATURES_S16_SIZE =
    sizeof(CANONICAL_FEATURES_S16) / sizeof(CANONICAL_FEATURES_S16[0]);

} // namespace orderflow
