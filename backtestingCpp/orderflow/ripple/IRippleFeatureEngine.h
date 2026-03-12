#pragma once

#include "RippleTypes.h"

namespace orderflow::ripple {

class RippleContext;  // forward

/// Abstract interface for feature computation.
///
/// Implementations take the current context + wall candidate and produce the
/// RippleFeatures vector that downstream layers (evidence, inference) consume.
///
/// The feature vector is the observation model's raw input.  All downstream
/// layers treat it as opaque numerical data, so swapping the feature engine
/// has no effect on the rest of the pipeline as long as the same struct is
/// populated.
///
/// Current backend: RippleFeatureEngine
///   - 16 hand-crafted features from order book + trade flow
///
/// Future backends / extension points:
///   - Learned feature extractor (e.g. auto-encoder on raw book/trade data)
///   - Cross-venue features (Tide-layer inputs)
///   - Latent features from a pre-trained representation model
///   - Feature augmentation: append new fields to RippleFeatures without
///     breaking downstream code (existing fields remain, new ones default to 0)
///
/// HMM integration note:
///   An HMM backend sitting in the inference layer needs emission likelihoods
///   derived from features.  The evidence engine already compresses features
///   into 6 interpretable scores, which serve as the emission observation.
///   If a future model needs raw features directly, add a second code path in
///   the inference engine that reads RippleFeatures — the feature engine
///   itself does not need to change.
class IRippleFeatureEngine {
public:
    virtual ~IRippleFeatureEngine() = default;

    virtual RippleFeatures compute(const RippleContext& ctx,
                                    const WallCandidate& wall,
                                    const WallMetrics* metrics = nullptr) const = 0;
};

} // namespace orderflow::ripple
