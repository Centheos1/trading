#pragma once

#include <vector>
#include "RippleTypes.h"

namespace orderflow::ripple {

class RippleContext;  // forward

/// Abstract interface for wall detection.
/// Implementations scan the order book for liquidity walls and track their lifecycle.
///
/// Extension points:
///   - Override on_depth() with different detection logic (percentile, ML, etc.)
///   - Override primary selection to weight walls by distance, age, or flow pressure
///   - Override baseline computation (median, mean, percentile-based)
class IWallDetector {
public:
    virtual ~IWallDetector() = default;

    /// Called on every depth update.  Implementations should update internal
    /// wall tracking and make results available via active_walls() / metrics().
    virtual void on_depth(const RippleContext& ctx) = 0;

    /// All walls currently being tracked (not yet failed/withdrawn).
    virtual const std::vector<WallCandidate>& active_walls() const = 0;

    /// Derived metrics for each active wall (same order as active_walls).
    virtual const std::vector<WallMetrics>& metrics() const = 0;

    /// Closest / most relevant bid wall (may be null).
    virtual const WallCandidate* primary_bid_wall() const = 0;

    /// Closest / most relevant ask wall (may be null).
    virtual const WallCandidate* primary_ask_wall() const = 0;

    /// Metrics for the primary bid wall (null if none).
    virtual const WallMetrics* primary_bid_metrics() const = 0;

    /// Metrics for the primary ask wall (null if none).
    virtual const WallMetrics* primary_ask_metrics() const = 0;
};

} // namespace orderflow::ripple
