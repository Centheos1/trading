#pragma once

#include "IRippleStateInference.h"
#include "RippleConfig.h"
#include <array>
#include <string>
#include <vector>

namespace orderflow::ripple {

/// HMM-based state inference backend (strategy.md §9.10).
///
/// Maintains a forward-variable vector α[k] over K latent states and
/// updates it on each tick using trained transition and emission parameters.
/// The posterior γ_t(k) populates result.scores[] and the MAP state is
/// returned as result.state.
///
/// Emission model: diagonal Gaussian on the 6 evidence scores.
/// Forward algorithm: O(K²) per event in log-space.
/// Training: offline Baum-Welch in Python; this class only loads and runs.
class HMMBasedInference : public IRippleStateInference {
public:
    static constexpr int MAX_K       = 8;   // max latent states (= NUM_STATES)
    static constexpr int OBS_DIM     = 6;   // evidence vector dimension
    static constexpr double LOG_ZERO = -1e30;

    explicit HMMBasedInference(const RippleConfig& cfg);

    RippleInferenceResult infer(
        const RippleEvidence& evidence,
        RippleState prev_state,
        const WallCandidate* wall,
        Timestamp now,
        Duration state_age_ms = 0) override;

    /// Load trained model from JSON file.
    /// Format: { "K": int, "transition": [[...]], "means": [[...]], "variances": [[...]],
    ///           "state_map": [int...], "log_prior": [...] }
    bool load_model(const std::string& path);

    /// Load model from in-memory JSON string (for testing / pybind11).
    bool load_model_from_string(const std::string& json);

    bool model_loaded() const { return model_loaded_; }
    int  num_states()   const { return K_; }

    /// Reset forward variable to the prior (call between independent sequences).
    void reset_forward();

    /// Export current evidence observation for offline training.
    struct Observation {
        Timestamp       ts;
        RippleEvidence  evidence;
        RippleState     labeled_state;
    };
    void record_observation(const Observation& obs);
    const std::vector<Observation>& training_buffer() const { return training_buffer_; }
    void clear_training_buffer() { training_buffer_.clear(); }

private:
    void extract_obs(const RippleEvidence& ev, double out[OBS_DIM]) const;
    double log_emission(int k, const double obs[OBS_DIM]) const;
    void forward_step(const double obs[OBS_DIM]);
    RippleState map_state(int k) const;

    const RippleConfig& cfg_;

    int K_ = 0;

    // Transition matrix: log_A_[i][j] = log P(z_t=j | z_{t-1}=i)
    double log_A_[MAX_K][MAX_K] = {};

    // Emission: diagonal Gaussian per state
    double means_[MAX_K][OBS_DIM]     = {};
    double variances_[MAX_K][OBS_DIM] = {};    // diagonal elements (σ²)
    double log_norm_[MAX_K]           = {};    // precomputed -0.5 * Σ log(2π σ²_d)

    // Forward variable: log α_t(k)
    double log_alpha_[MAX_K] = {};

    // Prior: log π(k)
    double log_prior_[MAX_K] = {};

    // Mapping: HMM state index → RippleState
    int state_map_[MAX_K] = {};

    bool model_loaded_ = false;

    std::vector<Observation> training_buffer_;
};

} // namespace orderflow::ripple
