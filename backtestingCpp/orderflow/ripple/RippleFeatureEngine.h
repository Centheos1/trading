#pragma once

#include "IRippleFeatureEngine.h"
#include "RippleConfig.h"
#include "RippleContext.h"

namespace orderflow::ripple {

class RippleFeatureEngine : public IRippleFeatureEngine {
public:
    explicit RippleFeatureEngine(const RippleConfig& cfg);

    RippleFeatures compute(const RippleContext& ctx,
                           const WallCandidate& wall,
                           const WallMetrics* metrics = nullptr) const override;

private:
    double compute_cancel_rate(const WallCandidate& wall,
                               const TradeWindow& tw,
                               double tick_size) const;

    double compute_impact(const RippleContext& ctx,
                          const TradeWindow& tw) const;

    const RippleConfig& cfg_;
};

} // namespace orderflow::ripple
