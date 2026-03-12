#include "TriggerDecisionEngine.h"
#include <cmath>
#include <sstream>

namespace orderflow::ripple {

TriggerDecisionEngine::TriggerDecisionEngine(const RippleConfig& cfg) : cfg_(cfg) {}

void TriggerDecisionEngine::reset() {
    last_intent_ts_.clear();
    wall_intent_ts_.clear();
    active_entry_      = RippleIntent::NO_ACTION;
    active_entry_wall_ = 0;
    active_entry_ts_   = 0;
    exit_emitted_      = false;
}

// -----------------------------------------------------------------------
//  Main evaluate — called once per pipeline tick
// -----------------------------------------------------------------------
RippleDecision TriggerDecisionEngine::evaluate(
    const RippleInferenceResult& inf,
    const RippleEvidence& ev,
    const WallCandidate* wall,
    const InventorySnapshot& inventory,
    Timestamp now) {

    RippleDecision d;
    d.timestamp        = now;
    d.triggering_state = inf.state;
    d.confidence       = inf.confidence;

    if (wall) {
        d.wall_id        = wall->wall_id;
        d.reference_side = wall->side;
        d.reference_price = wall->price;
    }

    RippleIntent intent = RippleIntent::NO_ACTION;
    std::string reason;

    // --- 1. edge-triggered: only on state transitions ---
    if (inf.is_transition)
        intent = try_transition_intent(inf, ev, wall, reason);

    // --- 2. within-state checks (exits, preemptive cancels, time stop) ---
    if (intent == RippleIntent::NO_ACTION)
        intent = try_within_state_intent(inf, ev, wall, now, reason);

    // --- 3. filters: cooldown, spatial dedup, inventory ---
    if (intent != RippleIntent::NO_ACTION) {
        uint64_t wid = wall ? wall->wall_id : 0;

        if (!check_global_cooldown(intent, now)) {
            intent = RippleIntent::NO_ACTION;
            reason = "[filtered: global cooldown]";
        }
        if (intent != RippleIntent::NO_ACTION &&
            !check_spatial_dedup(intent, wid, now)) {
            intent = RippleIntent::NO_ACTION;
            reason = "[filtered: wall intent cooldown]";
        }
        if (intent != RippleIntent::NO_ACTION &&
            !check_inventory(intent, inventory)) {
            intent = RippleIntent::NO_ACTION;
            reason = "[filtered: inventory constraint]";
        }
    }

    // --- 4. record and track ---
    if (intent != RippleIntent::NO_ACTION) {
        uint64_t wid = wall ? wall->wall_id : 0;
        record_intent(intent, wid, now);

        if (is_entry(intent)) {
            active_entry_      = intent;
            active_entry_wall_ = wid;
            active_entry_ts_   = now;
            exit_emitted_      = false;
        }
        if (intent == RippleIntent::EXIT_BOUNCE ||
            intent == RippleIntent::EXIT_BREAKOUT) {
            exit_emitted_ = true;
        }
        if (intent == RippleIntent::REARM_FOR_NEXT_BOUNCE) {
            active_entry_ = RippleIntent::NO_ACTION;
            exit_emitted_ = false;
        }
    }

    d.intent = intent;
    d.reason = reason;
    if (wall)
        d.invalidation_price = compute_invalidation(wall);

    return d;
}

// -----------------------------------------------------------------------
//  Edge-triggered logic (state transition)
// -----------------------------------------------------------------------
RippleIntent TriggerDecisionEngine::try_transition_intent(
    const RippleInferenceResult& inf,
    const RippleEvidence& ev,
    const WallCandidate* wall,
    std::string& reason) {

    std::ostringstream rs;

    switch (inf.state) {

    // --- WALL_FORMING: prepare bounce ---
    case RippleState::WALL_FORMING:
        if (wall && wall->current_qty > 0) {
            auto intent = (wall->side == WallSide::BID)
                ? RippleIntent::PREPARE_BOUNCE_LONG
                : RippleIntent::PREPARE_BOUNCE_SHORT;
            rs << "Wall forming " << to_string(wall->side)
               << " @ " << wall->price;
            reason = rs.str();
            return intent;
        }
        break;

    // --- ABSORBING: stronger prepare bounce (wall is holding) ---
    case RippleState::ABSORBING:
        if (wall && wall->current_qty > 0 && passes_evidence_guard_bounce(ev)) {
            auto intent = (wall->side == WallSide::BID)
                ? RippleIntent::PREPARE_BOUNCE_LONG
                : RippleIntent::PREPARE_BOUNCE_SHORT;
            rs << "Absorbing " << to_string(wall->side)
               << " @ " << wall->price
               << " abs=" << ev.absorption;
            reason = rs.str();
            return intent;
        }
        break;

    // --- EXHAUSTING: enter bounce (aggressors tiring against the wall) ---
    case RippleState::EXHAUSTING:
        if (wall && ev.exhaustion >= cfg_.exhaustion_entry &&
            passes_evidence_guard_bounce(ev)) {
            auto intent = (wall->side == WallSide::BID)
                ? RippleIntent::ENTER_BOUNCE_LONG
                : RippleIntent::ENTER_BOUNCE_SHORT;
            rs << "Exhaustion entry " << to_string(wall->side)
               << " @ " << wall->price
               << " exh=" << ev.exhaustion
               << " brk=" << ev.breakout
               << " wdr=" << ev.withdrawal;
            reason = rs.str();
            return intent;
        }
        break;

    // --- BREAKING: enter breakout ---
    case RippleState::BREAKING:
        if (wall && ev.breakout >= cfg_.breakout_entry &&
            passes_evidence_guard_breakout(ev)) {
            auto intent = (wall->side == WallSide::BID)
                ? RippleIntent::ENTER_BREAKOUT_SHORT
                : RippleIntent::ENTER_BREAKOUT_LONG;
            rs << "Breakout " << to_string(wall->side)
               << " @ " << wall->price
               << " brk=" << ev.breakout
               << " abs=" << ev.absorption;
            reason = rs.str();
            return intent;
        }
        break;

    // --- WITHDRAWING: cancel passive orders ---
    case RippleState::WITHDRAWING:
        rs << "Withdrawal risk wdr=" << ev.withdrawal;
        reason = rs.str();
        return RippleIntent::CANCEL_PASSIVE_ORDERS;

    // --- REFILLING / STABILIZING: rearm if evidence strong enough ---
    case RippleState::REFILLING:
        if (ev.refill >= cfg_.rearm_min_evidence) {
            rs << "Refill rearm ref=" << ev.refill;
            reason = rs.str();
            return RippleIntent::REARM_FOR_NEXT_BOUNCE;
        }
        break;

    case RippleState::STABILIZING:
        if (ev.stabilization >= cfg_.rearm_min_evidence) {
            rs << "Stabilization rearm stab=" << ev.stabilization;
            reason = rs.str();
            return RippleIntent::REARM_FOR_NEXT_BOUNCE;
        }
        break;

    default:
        break;
    }

    return RippleIntent::NO_ACTION;
}

// -----------------------------------------------------------------------
//  Within-state checks (every tick, not just transitions)
// -----------------------------------------------------------------------
RippleIntent TriggerDecisionEngine::try_within_state_intent(
    const RippleInferenceResult& inf,
    const RippleEvidence& ev,
    const WallCandidate* /*wall*/,
    Timestamp now,
    std::string& reason) {

    // --- A. Exit bounce: wall broke while we were in a bounce ---
    if (!exit_emitted_ && is_bounce_entry(active_entry_) &&
        (inf.state == RippleState::BREAKING ||
         inf.state == RippleState::WITHDRAWING)) {
        reason = "Wall broke/withdrawn during bounce, exit";
        return RippleIntent::EXIT_BOUNCE;
    }

    // --- B. Exit breakout: momentum reversing (refill / stabilization) ---
    if (!exit_emitted_ && is_breakout_entry(active_entry_) &&
        (inf.state == RippleState::REFILLING ||
         inf.state == RippleState::STABILIZING)) {
        reason = "Refill/stabilization during breakout, exit";
        return RippleIntent::EXIT_BREAKOUT;
    }

    // --- C. Preemptive cancel: high withdrawal or break risk ---
    if (active_entry_ != RippleIntent::NO_ACTION &&
        (ev.withdrawal >= cfg_.cancel_risk_threshold ||
         ev.breakout   >= cfg_.cancel_risk_threshold)) {
        std::ostringstream rs;
        rs << "Preemptive cancel wdr=" << ev.withdrawal
           << " brk=" << ev.breakout;
        reason = rs.str();
        return RippleIntent::CANCEL_PASSIVE_ORDERS;
    }

    // --- D. Time stop: entry has been held too long ---
    if (!exit_emitted_ && is_entry(active_entry_) &&
        cfg_.time_stop_ms > 0 &&
        active_entry_ts_ > 0 &&
        (now - active_entry_ts_) >= cfg_.time_stop_ms) {
        if (is_bounce_entry(active_entry_)) {
            reason = "Time stop on bounce";
            active_entry_ = RippleIntent::NO_ACTION;
            return RippleIntent::EXIT_BOUNCE;
        }
        if (is_breakout_entry(active_entry_)) {
            reason = "Time stop on breakout";
            active_entry_ = RippleIntent::NO_ACTION;
            return RippleIntent::EXIT_BREAKOUT;
        }
    }

    // --- E. Safety: clear zombie active_entry_ after 2x time stop ---
    if (is_entry(active_entry_) && active_entry_ts_ > 0 &&
        cfg_.time_stop_ms > 0 &&
        (now - active_entry_ts_) >= cfg_.time_stop_ms * 2) {
        active_entry_ = RippleIntent::NO_ACTION;
        exit_emitted_ = false;
    }

    return RippleIntent::NO_ACTION;
}

// -----------------------------------------------------------------------
//  Evidence guards
// -----------------------------------------------------------------------
bool TriggerDecisionEngine::passes_evidence_guard_bounce(
    const RippleEvidence& ev) const {
    return ev.breakout   < cfg_.bounce_max_break_risk &&
           ev.withdrawal < cfg_.bounce_max_withdrawal_risk;
}

bool TriggerDecisionEngine::passes_evidence_guard_breakout(
    const RippleEvidence& ev) const {
    return ev.absorption < cfg_.breakout_max_absorption;
}

// -----------------------------------------------------------------------
//  Cooldown / dedup / inventory
// -----------------------------------------------------------------------
bool TriggerDecisionEngine::check_global_cooldown(
    RippleIntent intent, Timestamp now) const {
    int key = static_cast<int>(intent);
    auto it = last_intent_ts_.find(key);
    if (it == last_intent_ts_.end()) return true;
    return (now - it->second) >= cfg_.global_intent_cooldown_ms;
}

bool TriggerDecisionEngine::check_spatial_dedup(
    RippleIntent intent, uint64_t wall_id, Timestamp now) const {
    if (wall_id == 0) return true;
    WallIntentKey wk{wall_id, intent};
    auto wit = wall_intent_ts_.find(wk);
    if (wit == wall_intent_ts_.end()) return true;
    return (now - wit->second) >= cfg_.intent_cooldown_ms;
}

bool TriggerDecisionEngine::check_inventory(
    RippleIntent intent, const InventorySnapshot& inv) const {
    double pos     = inv.position;
    double max_pos = cfg_.max_position;

    switch (intent) {
        case RippleIntent::ENTER_BOUNCE_LONG:
        case RippleIntent::ENTER_BREAKOUT_LONG:
            return pos < max_pos;
        case RippleIntent::ENTER_BOUNCE_SHORT:
        case RippleIntent::ENTER_BREAKOUT_SHORT:
            return pos > -max_pos;
        default:
            return true;
    }
}

void TriggerDecisionEngine::record_intent(
    RippleIntent intent, uint64_t wall_id, Timestamp now) {
    last_intent_ts_[static_cast<int>(intent)] = now;
    if (wall_id != 0)
        wall_intent_ts_[{wall_id, intent}] = now;

    // Prune stale wall-intent entries (2x cooldown)
    if (wall_intent_ts_.size() > 50) {
        Duration max_age = cfg_.intent_cooldown_ms * 3;
        for (auto it = wall_intent_ts_.begin(); it != wall_intent_ts_.end(); ) {
            if (now - it->second > max_age)
                it = wall_intent_ts_.erase(it);
            else
                ++it;
        }
    }
}

// -----------------------------------------------------------------------
//  Invalidation price heuristic
// -----------------------------------------------------------------------
double TriggerDecisionEngine::compute_invalidation(
    const WallCandidate* wall) const {
    if (!wall) return 0.0;
    double offset = cfg_.tick_size * cfg_.activation_distance_ticks;
    return (wall->side == WallSide::BID)
        ? wall->price - offset
        : wall->price + offset;
}

// -----------------------------------------------------------------------
//  Intent classification helpers
// -----------------------------------------------------------------------
bool TriggerDecisionEngine::is_entry(RippleIntent i) {
    return is_bounce_entry(i) || is_breakout_entry(i);
}

bool TriggerDecisionEngine::is_bounce_entry(RippleIntent i) {
    return i == RippleIntent::ENTER_BOUNCE_LONG ||
           i == RippleIntent::ENTER_BOUNCE_SHORT;
}

bool TriggerDecisionEngine::is_breakout_entry(RippleIntent i) {
    return i == RippleIntent::ENTER_BREAKOUT_LONG ||
           i == RippleIntent::ENTER_BREAKOUT_SHORT;
}

} // namespace orderflow::ripple
