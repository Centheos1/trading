#include "RippleStateTracker.h"

namespace orderflow::ripple {

RippleStateTracker::RippleStateTracker(const RippleConfig& cfg,
                                       std::unique_ptr<IRippleStateInference> inference)
    : cfg_(cfg), inference_(std::move(inference)) {}

void RippleStateTracker::reset() {
    current_state_   = RippleState::IDLE;
    state_entered_at_ = 0;
    dedup_map_.clear();
}

RippleInferenceResult RippleStateTracker::update(const RippleEvidence& evidence,
                                                  const WallCandidate* wall,
                                                  Timestamp now) {
    Duration age = (state_entered_at_ > 0) ? (now - state_entered_at_) : 0;

    auto raw = inference_->infer(evidence, current_state_, wall, now, age);

    if (raw.state == current_state_) {
        raw.is_transition = false;
        raw.state_age_ms = age;
        return raw;
    }

    // Anti-flicker: minimum dwell time in current state
    if (!passes_dwell_check(raw.state, now)) {
        raw.state = current_state_;
        raw.is_transition = false;
        raw.state_age_ms = age;
        raw.reason += " [blocked: dwell]";
        return raw;
    }

    // Spatial dedup: same wall + same state within cooldown
    if (!passes_dedup(wall, raw.state, now)) {
        raw.state = current_state_;
        raw.is_transition = false;
        raw.state_age_ms = age;
        raw.reason += " [blocked: dedup]";
        return raw;
    }

    // Accept transition
    raw.prev_state = current_state_;
    raw.is_transition = true;
    raw.state_age_ms = 0;
    current_state_ = raw.state;
    state_entered_at_ = now;

    if (wall) {
        dedup_map_[{wall->wall_id, raw.state}] = now;
    }

    // Prune stale dedup entries (2x cooldown window)
    Duration max_age = cfg_.intent_cooldown_ms * 2;
    for (auto it = dedup_map_.begin(); it != dedup_map_.end(); ) {
        if (now - it->second > max_age)
            it = dedup_map_.erase(it);
        else
            ++it;
    }

    return raw;
}

bool RippleStateTracker::passes_dwell_check(RippleState /*proposed*/, Timestamp now) const {
    // First-ever transition: always allow (state_entered_at_ uninitialised).
    if (state_entered_at_ == 0 && current_state_ == RippleState::IDLE)
        return true;

    Duration elapsed = now - state_entered_at_;
    bool fast_state = (current_state_ == RippleState::ABSORBING ||
                       current_state_ == RippleState::EXHAUSTING ||
                       current_state_ == RippleState::WITHDRAWING ||
                       current_state_ == RippleState::BREAKING);
    Duration min_dwell = fast_state ? cfg_.min_dwell_fast_ms : cfg_.min_dwell_slow_ms;
    return elapsed >= min_dwell;
}

bool RippleStateTracker::passes_dedup(const WallCandidate* wall,
                                       RippleState proposed,
                                       Timestamp now) const {
    if (!wall) return true;

    DedupKey key{wall->wall_id, proposed};
    auto it = dedup_map_.find(key);
    if (it == dedup_map_.end()) return true;

    return (now - it->second) >= cfg_.intent_cooldown_ms;
}

} // namespace orderflow::ripple
