#pragma once

#include "IRippleStateInference.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

/// Score-based state inference engine.
///
/// For each tick the engine:
///   1.  Builds a raw score for every RippleState from the evidence vector
///       and wall lifecycle context.
///   2.  Adds a persistence bonus to the prior state.
///   3.  Selects the winner and runner-up.
///   4.  Applies IDLE exit threshold and minimum switch margin.
///   5.  Returns the full RippleInferenceResult.
///
/// HMM replacement note:
///   An HMM backend would replace steps 1-2 with a forward-variable update
///   using emission likelihoods (derived from evidence) and a trained
///   transition matrix.  Steps 3-5 would use the posterior distribution
///   instead of weighted scores.  The interface is unchanged.
class ScoreBasedInference : public IRippleStateInference {
public:
    explicit ScoreBasedInference(const RippleConfig& cfg);

    RippleInferenceResult infer(
        const RippleEvidence& evidence,
        RippleState prev_state,
        const WallCandidate* wall,
        Timestamp now,
        Duration state_age_ms = 0) override;

private:
    static constexpr int N = RippleInferenceResult::NUM_STATES;

    void compute_raw_scores(double out[N],
                            const RippleEvidence& ev,
                            const WallCandidate* wall) const;

    void apply_lifecycle_context(double out[N],
                                 const WallCandidate* wall) const;

    void apply_persistence(double out[N], RippleState prev) const;

    static void apply_transition_mask(double out[N], RippleState prev);

    static RippleState pick_winner(const double scores[N],
                                   RippleState* runner_up);

    const RippleConfig& cfg_;
};

} // namespace orderflow::ripple
