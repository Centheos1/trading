#pragma once

#include "IRippleStateInference.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

/// Stub HMM-based inference backend.
/// Implements the same IRippleStateInference interface so it can be swapped in
/// for ScoreBasedInference. The actual HMM math is left unimplemented — this
/// class exists to validate the abstraction and demonstrate the extension point.
class HMMBasedInference : public IRippleStateInference {
public:
    explicit HMMBasedInference(const RippleConfig& cfg);

    RippleInferenceResult infer(
        const RippleEvidence& evidence,
        RippleState prev_state,
        const WallCandidate* wall,
        Timestamp now,
        Duration state_age_ms = 0) override;

    /// Load trained transition and emission matrices from a file.
    /// Placeholder — returns false until implemented.
    bool load_model(const std::string& path);

    /// Export current feature/evidence observation for offline training.
    struct Observation {
        Timestamp       ts;
        RippleEvidence  evidence;
        RippleState     labeled_state;  // ground truth (if available)
    };
    void record_observation(const Observation& obs);

private:
    const RippleConfig& cfg_;

    // Placeholder: transition matrix [from][to] = probability
    // Will be populated by load_model() once training pipeline exists
    static constexpr int NUM_STATES = 8;
    double transition_[NUM_STATES][NUM_STATES] = {};
    double emission_belief_[NUM_STATES] = {};
    bool   model_loaded_ = false;

    std::vector<Observation> training_buffer_;
};

} // namespace orderflow::ripple
