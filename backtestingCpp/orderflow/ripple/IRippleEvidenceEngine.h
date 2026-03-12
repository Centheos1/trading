#pragma once

#include "RippleTypes.h"

namespace orderflow::ripple {

/// Abstract interface for evidence computation.
///
/// Implementations map the raw feature vector into interpretable evidence
/// scores, each in [0, 1], representing the strength of a particular
/// microstructure narrative (absorption, exhaustion, withdrawal, breakout,
/// refill, stabilization).
///
/// Current backend: RippleEvidenceEngine
///   - Weighted sums of RippleFeatures, clamped to [0, 1]
///
/// Future backends / extension points:
///   - Logistic regression or neural network scorer
///   - Bayesian evidence accumulator (returns posterior belief per narrative)
///   - Multi-source evidence fusion (combine local + cross-venue signals)
///
/// HMM integration note:
///   The RippleEvidence struct doubles as the "observation" that the HMM
///   inference engine uses to compute emission likelihoods.  Specifically:
///
///     emission_likelihood(state_k | evidence) =
///         product over j of  P(evidence_j | state_k)
///
///   Where each evidence_j ∈ {absorption, exhaustion, ...} is treated as an
///   independent Beta-distributed observation conditioned on the hidden state.
///   The evidence engine doesn't need to know about the HMM — it just
///   provides the observation vector.  The HMM backend's emission model
///   converts these into log-likelihoods internally.
///
///   If a future model requires un-clamped or log-scale evidence, add an
///   optional `compute_raw()` method alongside `compute()`.
class IRippleEvidenceEngine {
public:
    virtual ~IRippleEvidenceEngine() = default;

    virtual RippleEvidence compute(const RippleFeatures& features,
                                    const WallCandidate& wall) const = 0;
};

} // namespace orderflow::ripple
