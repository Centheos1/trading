#pragma once

#include <algorithm>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <vector>
#include <span>
#include "../Types.h"

namespace orderflow::ripple {

using Timestamp = int64_t;  // epoch milliseconds — deterministic from event stream
using Duration  = int64_t;  // milliseconds

// ===================================================================
//  Enums
// ===================================================================

enum class WallSide : uint8_t { BID, ASK };

/// Per-wall lifecycle state (how the wall itself is behaving)
enum class WallLifecycle : uint8_t {
    FORMING,
    ACTIVE,
    DEPLETING,
    REFILLING,
    WITHDRAWN,
    FAILED
};

/// Local microstructure state inferred from wall + order flow
/// (also available as the alias `LiquidityState`)
enum class RippleState : uint8_t {
    IDLE,
    WALL_FORMING,
    ABSORBING,
    EXHAUSTING,
    WITHDRAWING,
    BREAKING,
    REFILLING,
    STABILIZING
};

/// Alias requested by the design spec — identical to RippleState
using LiquidityState = RippleState;

/// Execution intents emitted by the trigger layer
/// (also available as the alias `RippleAction`)
enum class RippleIntent : uint8_t {
    NO_ACTION,
    PREPARE_BOUNCE_LONG,
    PREPARE_BOUNCE_SHORT,
    ENTER_BOUNCE_LONG,
    ENTER_BOUNCE_SHORT,
    ENTER_BREAKOUT_LONG,
    ENTER_BREAKOUT_SHORT,
    EXIT_BOUNCE,
    EXIT_BREAKOUT,
    REARM_FOR_NEXT_BOUNCE,
    CANCEL_PASSIVE_ORDERS
};

/// Alias — use whichever name reads better in your calling code
using RippleAction = RippleIntent;

// ===================================================================
//  to_string helpers
// ===================================================================

inline const char* to_string(WallSide s) {
    switch (s) {
        case WallSide::BID: return "BID";
        case WallSide::ASK: return "ASK";
    }
    return "?";
}

inline const char* to_string(WallLifecycle lc) {
    switch (lc) {
        case WallLifecycle::FORMING:   return "FORMING";
        case WallLifecycle::ACTIVE:    return "ACTIVE";
        case WallLifecycle::DEPLETING: return "DEPLETING";
        case WallLifecycle::REFILLING: return "REFILLING";
        case WallLifecycle::WITHDRAWN: return "WITHDRAWN";
        case WallLifecycle::FAILED:    return "FAILED";
    }
    return "?";
}

inline const char* to_string(RippleState st) {
    switch (st) {
        case RippleState::IDLE:          return "IDLE";
        case RippleState::WALL_FORMING:  return "WALL_FORMING";
        case RippleState::ABSORBING:     return "ABSORBING";
        case RippleState::EXHAUSTING:    return "EXHAUSTING";
        case RippleState::WITHDRAWING:   return "WITHDRAWING";
        case RippleState::BREAKING:      return "BREAKING";
        case RippleState::REFILLING:     return "REFILLING";
        case RippleState::STABILIZING:   return "STABILIZING";
    }
    return "?";
}

inline const char* to_string(RippleIntent i) {
    switch (i) {
        case RippleIntent::NO_ACTION:              return "NO_ACTION";
        case RippleIntent::PREPARE_BOUNCE_LONG:    return "PREPARE_BOUNCE_LONG";
        case RippleIntent::PREPARE_BOUNCE_SHORT:   return "PREPARE_BOUNCE_SHORT";
        case RippleIntent::ENTER_BOUNCE_LONG:      return "ENTER_BOUNCE_LONG";
        case RippleIntent::ENTER_BOUNCE_SHORT:     return "ENTER_BOUNCE_SHORT";
        case RippleIntent::ENTER_BREAKOUT_LONG:    return "ENTER_BREAKOUT_LONG";
        case RippleIntent::ENTER_BREAKOUT_SHORT:   return "ENTER_BREAKOUT_SHORT";
        case RippleIntent::EXIT_BOUNCE:            return "EXIT_BOUNCE";
        case RippleIntent::EXIT_BREAKOUT:          return "EXIT_BREAKOUT";
        case RippleIntent::REARM_FOR_NEXT_BOUNCE:  return "REARM_FOR_NEXT_BOUNCE";
        case RippleIntent::CANCEL_PASSIVE_ORDERS:  return "CANCEL_PASSIVE_ORDERS";
    }
    return "?";
}

// ===================================================================
//  Lightweight market-data structs (Ripple's own vocabulary)
// ===================================================================

/// Single price level — used in OrderBookView and anywhere Ripple
/// needs to reason about individual book levels.
struct BookLevel {
    double    price    = 0.0;
    double    quantity = 0.0;
    Timestamp last_update = 0;  // when this level was last changed
};

/// Ripple-local trade representation.  Wraps the engine-level Trade with
/// pre-computed side and an explicit event sequence number for replay.
struct TradeEvent {
    Timestamp ts       = 0;
    double    price    = 0.0;
    double    quantity = 0.0;
    bool      is_buy   = false;   // true = buy aggressor
    uint64_t  seq      = 0;       // monotonic event counter for determinism

    /// Construct from the engine-level Trade
    static TradeEvent from(const Trade& t, uint64_t seq_num) {
        return {t.timestamp, t.price, t.quantity, t.is_buy_aggressor(), seq_num};
    }
};

// ===================================================================
//  Wall types
// ===================================================================

struct WallCandidate {
    uint64_t      wall_id         = 0;
    WallSide      side            = WallSide::BID;
    double        price           = 0.0;
    double        initial_qty     = 0.0;
    double        current_qty     = 0.0;
    double        peak_qty        = 0.0;
    double        prev_qty        = 0.0;    // qty at the previous depth update
    Timestamp     first_seen      = 0;
    Timestamp     last_seen       = 0;
    WallLifecycle lifecycle       = WallLifecycle::FORMING;
    int           refill_count    = 0;
    double        cumulative_absorbed = 0.0;
    double        depletion_rate  = 0.0;    // qty / sec (EMA, shrinking)
    double        refill_rate     = 0.0;    // qty / sec (EMA, growing)
    int           update_count    = 0;      // how many depth ticks this wall has survived
};

/// Derived metrics for a wall, recomputed every depth update by WallDetector.
/// Downstream stages (features, evidence, diagnostics) read these without
/// reaching into raw WallCandidate internals.
struct WallMetrics {
    uint64_t wall_id             = 0;
    WallSide side                = WallSide::BID;
    double   price               = 0.0;

    // Size
    double   absolute_size       = 0.0;    // current_qty
    double   relative_size       = 0.0;    // current_qty / baseline_depth
    double   baseline_depth      = 0.0;    // local normalisation value used

    // Distance
    double   distance_from_mid_ticks  = 0.0;
    double   distance_from_best_ticks = 0.0;

    // Temporal
    Timestamp first_seen_ts      = 0;
    double    persistence_sec    = 0.0;    // seconds since first_seen

    // Rates (EMA)
    double   growth_rate         = 0.0;    // qty/sec when growing
    double   depletion_rate      = 0.0;    // qty/sec when shrinking
    double   depletion_pct       = 0.0;    // (peak - current) / peak

    // Rank among all visible levels on the same side (1 = largest)
    int      local_rank          = 0;

    // Flags
    bool     is_active           = false;  // lifecycle >= ACTIVE
    bool     same_price_persists = false;  // seen at same price for >= 2 updates
    bool     disappeared         = false;  // dropped to 0 / off book this tick
    bool     withdrawn           = false;  // abrupt disappearance placeholder

    WallLifecycle lifecycle      = WallLifecycle::FORMING;
};

// ===================================================================
//  Feature / Evidence / Inference / Decision
// ===================================================================

struct RippleFeatures {
    // wall-relative
    double relative_wall_size        = 0.0;
    double wall_persistence_sec      = 0.0;
    double wall_depletion_rate       = 0.0;
    double wall_refill_rate          = 0.0;
    double wall_cancel_rate          = 0.0;
    double distance_to_wall_ticks    = 0.0;
    double queue_stability           = 0.0;
    // flow
    double aggressive_flow_imbalance = 0.0;
    double trade_count_imbalance     = 0.0;
    double microprice_drift          = 0.0;
    double top1_imbalance            = 0.0;
    double top5_imbalance            = 0.0;
    double impact_per_unit_volume    = 0.0;
    // volatility / spread
    double spread_shock              = 0.0;
    double short_horizon_volatility  = 0.0;
    // temporal
    double time_since_wall_formed_sec = 0.0;

    // Phase 3: CVD divergence (populated when CVD engine available)
    double cvd_divergence_strength   = 0.0;   // signed: +bullish, −bearish
    bool   cvd_divergence_detected   = false;

    // Phase 3: volume profile context (populated when VP engine available)
    double poc_price                 = 0.0;
    double distance_to_poc_ticks     = 0.0;
    double vah                       = 0.0;   // value area high
    double val                       = 0.0;   // value area low
};

struct RippleEvidence {
    double absorption    = 0.0;
    double exhaustion    = 0.0;
    double withdrawal    = 0.0;
    double breakout      = 0.0;
    double refill        = 0.0;
    double stabilization = 0.0;

    /// Name of the highest-scoring evidence channel (for diagnostics / logging).
    const char* dominant() const {
        double mx = absorption;
        const char* name = "absorption";
        if (exhaustion    > mx) { mx = exhaustion;    name = "exhaustion"; }
        if (withdrawal    > mx) { mx = withdrawal;    name = "withdrawal"; }
        if (breakout      > mx) { mx = breakout;      name = "breakout"; }
        if (refill        > mx) { mx = refill;        name = "refill"; }
        if (stabilization > mx) { mx = stabilization; name = "stabilization"; }
        return name;
    }

    double dominant_score() const {
        return std::max({absorption, exhaustion, withdrawal,
                         breakout, refill, stabilization});
    }
};

struct RippleInferenceResult {
    static constexpr int NUM_STATES = 8;

    RippleState state         = RippleState::IDLE;
    RippleState prev_state    = RippleState::IDLE;
    bool        is_transition = false;

    // Per-state raw scores (indexed by static_cast<int>(RippleState)).
    // These are the observation scores *before* persistence bonus.
    double      scores[NUM_STATES] = {};

    double      winning_score   = 0.0;
    double      runner_up_score = 0.0;
    RippleState runner_up_state = RippleState::IDLE;

    // Confidence = margin between winner and runner-up, clamped to [0,1].
    double      confidence      = 0.0;

    // How long the current state has been active (ms).
    Duration    state_age_ms    = 0;

    // Human-readable explanation of why this state was chosen.
    std::string reason;

    double score_for(RippleState s) const {
        int idx = static_cast<int>(s);
        return (idx >= 0 && idx < NUM_STATES) ? scores[idx] : 0.0;
    }
};

struct RippleDecision {
    Timestamp     timestamp          = 0;
    RippleIntent  intent             = RippleIntent::NO_ACTION;
    WallSide      reference_side     = WallSide::BID;
    double        reference_price    = 0.0;
    double        invalidation_price = 0.0;   // price at which the intent is invalid
    double        confidence         = 0.0;
    uint64_t      wall_id            = 0;
    RippleState   triggering_state   = RippleState::IDLE;
    std::string   reason;
};

using RippleDecisionCallback = std::function<void(const RippleDecision&)>;

// ===================================================================
//  Views — read-only observations consumed by the pipeline
// ===================================================================

struct OrderBookView {
    double best_bid  = 0.0;
    double best_ask  = 0.0;
    double mid_price = 0.0;
    double spread    = 0.0;
    std::vector<std::pair<double, double>> bids;  // descending price
    std::vector<std::pair<double, double>> asks;  // ascending price
    Timestamp timestamp = 0;
};

struct TradeWindow {
    std::span<const Trade> recent;
    double    buy_volume   = 0.0;
    double    sell_volume  = 0.0;
    double    net_delta    = 0.0;
    int       buy_count    = 0;
    int       sell_count   = 0;
    Timestamp window_start = 0;
    Timestamp window_end   = 0;
};

struct InventorySnapshot {
    double position        = 0.0;
    double avg_entry_price = 0.0;
    double unrealized_pnl  = 0.0;
};

// ===================================================================
//  Replay / timing metadata
// ===================================================================

struct ReplayMeta {
    uint64_t  depth_event_count = 0;
    uint64_t  trade_event_count = 0;
    Timestamp first_event_ts    = 0;
    Timestamp last_event_ts     = 0;
    bool      is_replay         = false;
};

// ===================================================================
//  Extension points for Tide / Wave layers (empty for now)
// ===================================================================

struct TideContext {};
struct WaveContext {};

} // namespace orderflow::ripple
