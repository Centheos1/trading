#pragma once

#include "IRippleEvidenceEngine.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

class RippleEvidenceEngine : public IRippleEvidenceEngine {
public:
    explicit RippleEvidenceEngine(const RippleConfig& cfg);

    RippleEvidence compute(const RippleFeatures& features,
                           const WallCandidate& wall) const override;

private:
    static double clamp01(double v) { return std::max(0.0, std::min(1.0, v)); }

    const RippleConfig& cfg_;
};

} // namespace orderflow::ripple
