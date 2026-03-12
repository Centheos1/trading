#pragma once

#include "RippleTypes.h"

namespace orderflow::ripple {

/// Abstract interface for the trigger / decision layer.
///
/// Implementations map (inference result, evidence, wall, inventory) → execution
/// intents with edge-triggering, cooldowns, and inventory filtering.
///
/// The trigger layer receives ONLY the abstract RippleInferenceResult — it does
/// not depend on how the state was inferred (score-based, HMM, particle filter,
/// etc.).  This is the key decoupling boundary: any inference backend that
/// populates RippleInferenceResult can drive the same trigger engine.
///
/// Extension points:
///   - Plug in portfolio-level position limits
///   - Add Tide/Wave context gating
///   - Implement probabilistic intent sizing using inference.confidence
///   - Use inference.scores[] for soft multi-state decision weighting
///   - Replace the rule-based trigger with a learned policy
class ITriggerDecisionEngine {
public:
    virtual ~ITriggerDecisionEngine() = default;

    virtual RippleDecision evaluate(
        const RippleInferenceResult& inference,
        const RippleEvidence& evidence,
        const WallCandidate* wall,
        const InventorySnapshot& inventory,
        Timestamp now) = 0;

    /// Clear stateful cooldowns, active-entry tracking, etc.
    /// Default is a no-op for stateless implementations.
    virtual void reset() {}
};

} // namespace orderflow::ripple
