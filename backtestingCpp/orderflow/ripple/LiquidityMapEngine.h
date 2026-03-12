#pragma once

#include <cstdint>
#include <vector>
#include "../Schemas.h"
#include "RippleTypes.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

/// Structured input for a single map update.
/// Pointers are non-owning — caller keeps data alive for the duration of update().
struct LiquidityMapInput {
    // Walls
    const std::vector<WallCandidate>* walls   = nullptr;
    const std::vector<WallMetrics>*   metrics = nullptr;

    // Volume profile (may be empty)
    const std::vector<orderflow::VolumeNode>* profile = nullptr;
    double poc_price = 0.0;
    double vah       = 0.0;
    double val       = 0.0;

    // Recent trades for flow computation
    const Trade* trades_begin = nullptr;
    const Trade* trades_end   = nullptr;

    // Structural anchors
    double vwap         = 0.0;
    double session_high = 0.0;
    double session_low  = 0.0;

    // Market state for scoring
    double mid_price = 0.0;
    double sigma_P   = 0.0;
    double imbalance = 0.0;   // top-of-book imbalance for hold scoring

    int64_t timestamp = 0;
};

/// Maintains a real-time liquidity map combining resting depth, traded volume,
/// aggressive flow, and structural anchors.  strategy.md §10.
///
/// Deterministic: same LiquidityMapInput → same LiquidityMapSnapshot.
/// Bounded: at most max_levels levels in the output.
/// Working vectors pre-allocated; flow bucketing and profile classification
/// use small transient allocations bounded by input size.
class LiquidityMapEngine {
public:
    explicit LiquidityMapEngine(const LiquidityMapConfig& cfg = {});

    /// Rebuild the map from current market state.
    void update(const LiquidityMapInput& input);

    const LiquidityMapSnapshot& snapshot() const { return snap_; }

    // Convenience queries
    double get_hold_score(double price) const;
    double get_dest_score(double price) const;

    /// Destination levels beyond from_price in the given direction,
    /// sorted by dest_score descending.
    std::vector<LiquidityLevel> get_destinations_in_direction(
        TradeSide side, double from_price) const;

    size_t level_count() const { return snap_.levels.size(); }

    void reset();

private:
    void collect_wall_levels(const LiquidityMapInput& input);
    void collect_profile_levels(const LiquidityMapInput& input);
    void collect_flow_into_levels(const LiquidityMapInput& input);
    void collect_structural_anchors(const LiquidityMapInput& input);
    void detect_voids(const LiquidityMapInput& input);
    void compute_scores(const LiquidityMapInput& input);
    void finalize(const LiquidityMapInput& input);

    const LiquidityLevel* find_nearest(double price) const;

    LiquidityMapConfig cfg_;
    LiquidityMapSnapshot snap_;
    std::vector<LiquidityLevel> work_;   // pre-allocated workspace
};

} // namespace orderflow::ripple
