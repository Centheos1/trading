#pragma once

#include <memory>
#include <unordered_map>
#include "RippleTypes.h"
#include "RippleConfig.h"
#include "IRippleStateInference.h"

namespace orderflow::ripple {

class RippleStateTracker {
public:
    RippleStateTracker(const RippleConfig& cfg,
                       std::unique_ptr<IRippleStateInference> inference);

    RippleInferenceResult update(const RippleEvidence& evidence,
                                 const WallCandidate* wall,
                                 Timestamp now);

    RippleState current_state() const { return current_state_; }
    Timestamp   state_entered_at() const { return state_entered_at_; }
    Duration    time_in_state(Timestamp now) const { return now - state_entered_at_; }

    void set_inference(std::unique_ptr<IRippleStateInference> inf) {
        inference_ = std::move(inf);
    }

    void reset();

private:
    bool passes_dwell_check(RippleState proposed, Timestamp now) const;
    bool passes_dedup(const WallCandidate* wall, RippleState proposed, Timestamp now) const;

    const RippleConfig& cfg_;
    std::unique_ptr<IRippleStateInference> inference_;

    RippleState current_state_  = RippleState::IDLE;
    Timestamp   state_entered_at_ = 0;

    // Spatial dedup: wall_id -> last transition timestamp per state
    struct DedupKey {
        uint64_t    wall_id;
        RippleState state;
        bool operator==(const DedupKey& o) const {
            return wall_id == o.wall_id && state == o.state;
        }
    };
    struct DedupKeyHash {
        size_t operator()(const DedupKey& k) const {
            return std::hash<uint64_t>{}(k.wall_id) ^
                   (std::hash<int>{}(static_cast<int>(k.state)) << 16);
        }
    };
    std::unordered_map<DedupKey, Timestamp, DedupKeyHash> dedup_map_;
};

} // namespace orderflow::ripple
