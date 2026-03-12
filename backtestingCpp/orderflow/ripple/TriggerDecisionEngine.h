#pragma once

#include <unordered_map>
#include "ITriggerDecisionEngine.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

class TriggerDecisionEngine : public ITriggerDecisionEngine {
public:
    explicit TriggerDecisionEngine(const RippleConfig& cfg);

    RippleDecision evaluate(const RippleInferenceResult& inference,
                            const RippleEvidence& evidence,
                            const WallCandidate* wall,
                            const InventorySnapshot& inventory,
                            Timestamp now) override;

    void reset() override;

private:
    // --- helpers ---
    RippleIntent try_transition_intent(const RippleInferenceResult& inf,
                                       const RippleEvidence& ev,
                                       const WallCandidate* wall,
                                       std::string& reason);

    RippleIntent try_within_state_intent(const RippleInferenceResult& inf,
                                          const RippleEvidence& ev,
                                          const WallCandidate* wall,
                                          Timestamp now,
                                          std::string& reason);

    bool passes_evidence_guard_bounce(const RippleEvidence& ev) const;
    bool passes_evidence_guard_breakout(const RippleEvidence& ev) const;

    bool check_global_cooldown(RippleIntent intent, Timestamp now) const;
    bool check_inventory(RippleIntent intent, const InventorySnapshot& inv) const;
    bool check_spatial_dedup(RippleIntent intent, uint64_t wall_id, Timestamp now) const;
    void record_intent(RippleIntent intent, uint64_t wall_id, Timestamp now);

    double compute_invalidation(const WallCandidate* wall) const;

    static bool is_entry(RippleIntent i);
    static bool is_bounce_entry(RippleIntent i);
    static bool is_breakout_entry(RippleIntent i);

    const RippleConfig& cfg_;

    // --- global cooldown tracking ---
    std::unordered_map<int, Timestamp> last_intent_ts_;

    // --- per-wall spatial dedup ---
    struct WallIntentKey {
        uint64_t     wall_id;
        RippleIntent intent;
        bool operator==(const WallIntentKey& o) const {
            return wall_id == o.wall_id && intent == o.intent;
        }
    };
    struct WallIntentKeyHash {
        size_t operator()(const WallIntentKey& k) const {
            return std::hash<uint64_t>{}(k.wall_id) ^
                   (std::hash<int>{}(static_cast<int>(k.intent)) << 16);
        }
    };
    std::unordered_map<WallIntentKey, Timestamp, WallIntentKeyHash> wall_intent_ts_;

    // --- active entry tracking for exit / time-stop ---
    RippleIntent active_entry_       = RippleIntent::NO_ACTION;
    uint64_t     active_entry_wall_  = 0;
    Timestamp    active_entry_ts_    = 0;
    bool         exit_emitted_       = false;
};

} // namespace orderflow::ripple
