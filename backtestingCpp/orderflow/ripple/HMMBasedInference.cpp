#include "HMMBasedInference.h"
#include <cstring>

namespace orderflow::ripple {

HMMBasedInference::HMMBasedInference(const RippleConfig& cfg) : cfg_(cfg) {
    // Initialize uniform prior
    for (int i = 0; i < NUM_STATES; ++i)
        emission_belief_[i] = 1.0 / NUM_STATES;

    // Initialize identity-ish transition matrix (stay-in-state bias)
    for (int i = 0; i < NUM_STATES; ++i)
        for (int j = 0; j < NUM_STATES; ++j)
            transition_[i][j] = (i == j) ? 0.7 : 0.3 / (NUM_STATES - 1);
}

RippleInferenceResult HMMBasedInference::infer(
    const RippleEvidence& /*evidence*/,
    RippleState prev_state,
    const WallCandidate* wall,
    Timestamp /*now*/,
    Duration state_age_ms) {

    RippleInferenceResult result;
    result.prev_state = prev_state;
    result.state_age_ms = state_age_ms;

    if (!model_loaded_ || !wall) {
        result.state = prev_state;
        result.confidence = 0.0;
        result.is_transition = false;
        result.reason = "HMM: model not loaded or no wall";
        return result;
    }

    // TODO: Implement forward algorithm / Viterbi once model is trained.
    // When implemented, populate result.scores[] with posterior state
    // probabilities and set winning/runner-up from the posteriors.
    result.state = prev_state;
    result.confidence = 0.0;
    result.is_transition = false;
    result.reason = "HMM: untrained fallback";
    return result;
}

bool HMMBasedInference::load_model(const std::string& /*path*/) {
    // TODO: deserialize transition/emission matrices from file
    // Format TBD — could be JSON, binary, or HDF5
    model_loaded_ = false;
    return false;
}

void HMMBasedInference::record_observation(const Observation& obs) {
    training_buffer_.push_back(obs);
}

} // namespace orderflow::ripple
