#pragma once

#include "RippleTypes.h"

namespace orderflow::ripple {

/// Abstract interface for microstructure state inference.
///
/// This is the central abstraction boundary in the Ripple architecture.
/// Everything upstream (features, evidence) produces observations.
/// Everything downstream (tracker, trigger engine) consumes a
/// RippleInferenceResult.  The inference engine is the only component
/// that decides which hidden state the market is in.
///
/// Current backend: ScoreBasedInference
///   - Builds per-state raw scores from evidence + wall lifecycle context
///   - Applies persistence bonus, transition mask, and margin/threshold gates
///   - Deterministic and fully explainable
///
/// Planned backends:
///
///   HMMBasedInference (stub exists):
///     - Maintains a forward-variable vector α[k] over the 8 states
///     - Each tick: α'[k] = emission(evidence | k) * Σ_j α[j] * T[j→k]
///     - Normalise α' to sum to 1 → posterior distribution
///     - MAP state = argmax(α'), confidence = margin to runner-up
///     - Populate result.scores[] with posteriors
///     - Transition matrix T learned offline from replay data
///     - Emission model: per-state Beta distributions over evidence scores
///     - Everything downstream (tracker dwell checks, trigger engine)
///       works unchanged — it reads the same RippleInferenceResult fields
///
///   Particle filter / sequential Monte Carlo:
///     - Maintain N weighted particles, each carrying a state hypothesis
///     - Resample + propagate + weight with evidence likelihoods
///     - MAP from mode of particle cloud
///     - Naturally handles multi-modal posterior (e.g. ABSORBING and
///       EXHAUSTING both plausible), which the trigger engine could use
///       via result.scores[]
///
///   Offline-trained neural classifier:
///     - Takes features or evidence as input
///     - Outputs softmax over 8 states
///     - Populate result.scores[] with softmax logits/probabilities
///     - Requires ONNX or TensorRT runtime — keep as a separate backend
///
/// Contract:
///   - `infer()` must be deterministic given the same inputs
///   - `result.scores[]` should be populated for all 8 states
///   - `result.state` is the MAP / winning state
///   - `result.confidence` is the margin between winner and runner-up
///   - `result.reason` should be a human-readable explanation
///   - Downstream layers (tracker, trigger) never inspect which backend
///     produced the result — they only read RippleInferenceResult fields
class IRippleStateInference {
public:
    virtual ~IRippleStateInference() = default;

    /// @param evidence      current observation scores
    /// @param prev_state    state chosen on the previous tick
    /// @param wall          active wall (nullptr if none)
    /// @param now           event timestamp
    /// @param state_age_ms  how long prev_state has been active (0 on first call)
    virtual RippleInferenceResult infer(
        const RippleEvidence& evidence,
        RippleState prev_state,
        const WallCandidate* wall,
        Timestamp now,
        Duration state_age_ms = 0) = 0;
};

} // namespace orderflow::ripple
