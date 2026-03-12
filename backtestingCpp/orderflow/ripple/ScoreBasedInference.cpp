#include "ScoreBasedInference.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <sstream>

namespace orderflow::ripple {

ScoreBasedInference::ScoreBasedInference(const RippleConfig& cfg) : cfg_(cfg) {}

// ------------------------------------------------------------------
//  Public entry point
// ------------------------------------------------------------------

RippleInferenceResult ScoreBasedInference::infer(
    const RippleEvidence& evidence,
    RippleState prev_state,
    const WallCandidate* wall,
    Timestamp /*now*/,
    Duration state_age_ms) {

    RippleInferenceResult r;
    r.prev_state    = prev_state;
    r.state_age_ms  = state_age_ms;

    // --- step 1: raw observation scores from evidence ---
    compute_raw_scores(r.scores, evidence, wall);

    // --- step 2: wall-lifecycle context boosts ---
    apply_lifecycle_context(r.scores, wall);

    // --- step 3: transition mask ---
    // Only states reachable from prev_state keep their scores.
    // This prevents nonsensical jumps (e.g. IDLE → STABILIZING).
    apply_transition_mask(r.scores, prev_state);

    // --- step 4: persistence bonus for prior state ---
    double pre_persist[N];
    std::memcpy(pre_persist, r.scores, sizeof(pre_persist));
    apply_persistence(r.scores, prev_state);

    // --- step 5: pick winner and runner-up ---
    RippleState runner_up = RippleState::IDLE;
    RippleState winner    = pick_winner(r.scores, &runner_up);

    r.winning_score   = r.scores[static_cast<int>(winner)];
    r.runner_up_score = r.scores[static_cast<int>(runner_up)];
    r.runner_up_state = runner_up;

    // --- step 6: threshold and margin gates ---

    // Gate A: leaving IDLE requires the winner to exceed idle_exit_threshold.
    if (prev_state == RippleState::IDLE && winner != RippleState::IDLE) {
        if (r.winning_score < cfg_.idle_exit_threshold) {
            winner = RippleState::IDLE;
        }
    }

    // Gate B: switching away from prev_state requires sufficient margin
    // between the new winner's raw score and the prior state's raw score.
    if (winner != prev_state) {
        double new_raw  = pre_persist[static_cast<int>(winner)];
        double prev_raw = pre_persist[static_cast<int>(prev_state)];
        if (new_raw - prev_raw < cfg_.state_switch_margin) {
            winner = prev_state;
        }
    }

    // --- finalise ---
    r.state         = winner;
    r.is_transition = (winner != prev_state);
    r.confidence    = std::clamp(r.winning_score - r.runner_up_score, 0.0, 1.0);

    std::ostringstream oss;
    oss.precision(2);
    oss << std::fixed;
    oss << to_string(winner) << " (" << r.winning_score << ")";
    if (r.is_transition)
        oss << " [from " << to_string(prev_state) << "]";
    oss << " margin=" << (r.winning_score - r.runner_up_score);
    r.reason = oss.str();

    return r;
}

// ------------------------------------------------------------------
//  Step 1: raw scores from evidence + base scores
// ------------------------------------------------------------------

void ScoreBasedInference::compute_raw_scores(
    double out[N],
    const RippleEvidence& ev,
    const WallCandidate* wall) const {

    out[static_cast<int>(RippleState::ABSORBING)]   = ev.absorption;
    out[static_cast<int>(RippleState::EXHAUSTING)]   = ev.exhaustion;
    out[static_cast<int>(RippleState::WITHDRAWING)]  = ev.withdrawal;
    out[static_cast<int>(RippleState::BREAKING)]     = ev.breakout;
    out[static_cast<int>(RippleState::REFILLING)]    = ev.refill;
    out[static_cast<int>(RippleState::STABILIZING)]  = ev.stabilization;

    out[static_cast<int>(RippleState::IDLE)] = cfg_.idle_base_score;
    out[static_cast<int>(RippleState::WALL_FORMING)] =
        wall ? cfg_.wall_forming_base_score : 0.0;
}

// ------------------------------------------------------------------
//  Step 2: lifecycle context boosts
// ------------------------------------------------------------------

void ScoreBasedInference::apply_lifecycle_context(
    double out[N],
    const WallCandidate* wall) const {

    if (!wall) {
        for (int i = 0; i < N; ++i) {
            if (static_cast<RippleState>(i) != RippleState::IDLE)
                out[i] = 0.0;
        }
        return;
    }

    double boost = cfg_.lifecycle_boost;

    switch (wall->lifecycle) {
        case WallLifecycle::FORMING:
            out[static_cast<int>(RippleState::WALL_FORMING)] += boost;
            break;
        case WallLifecycle::ACTIVE:
        case WallLifecycle::DEPLETING:
            break;
        case WallLifecycle::REFILLING:
            out[static_cast<int>(RippleState::REFILLING)] += boost;
            break;
        case WallLifecycle::WITHDRAWN:
            out[static_cast<int>(RippleState::WITHDRAWING)] += boost;
            break;
        case WallLifecycle::FAILED:
            out[static_cast<int>(RippleState::BREAKING)] += boost;
            break;
    }
}

// ------------------------------------------------------------------
//  Step 3: transition mask
//
//  Only states reachable from the prior state keep their scores.
//  Unreachable states are zeroed to prevent nonsensical jumps
//  (e.g. IDLE → STABILIZING, IDLE → EXHAUSTING).
//
//  An HMM backend achieves this naturally through the transition matrix
//  where disallowed cells have probability 0.
// ------------------------------------------------------------------

void ScoreBasedInference::apply_transition_mask(
    double out[N], RippleState prev) {

    // allowed[i] = true means state i is reachable from prev.
    // The current state itself is always allowed (stay).
    bool allowed[N] = {};
    allowed[static_cast<int>(prev)] = true;

    // IDLE is always reachable as a universal fallback.
    allowed[static_cast<int>(RippleState::IDLE)] = true;

    switch (prev) {
        case RippleState::IDLE:
            // From IDLE the only forward transition is WALL_FORMING.
            // Active states (ABSORBING, BREAKING, etc.) require passing
            // through WALL_FORMING first to avoid false activation from
            // calm-market evidence artifacts.
            allowed[static_cast<int>(RippleState::WALL_FORMING)] = true;
            break;

        case RippleState::WALL_FORMING:
            allowed[static_cast<int>(RippleState::ABSORBING)]  = true;
            allowed[static_cast<int>(RippleState::WITHDRAWING)] = true;
            allowed[static_cast<int>(RippleState::BREAKING)]   = true;
            break;

        case RippleState::ABSORBING:
            allowed[static_cast<int>(RippleState::EXHAUSTING)]  = true;
            allowed[static_cast<int>(RippleState::WITHDRAWING)] = true;
            allowed[static_cast<int>(RippleState::BREAKING)]    = true;
            allowed[static_cast<int>(RippleState::WALL_FORMING)] = true;
            break;

        case RippleState::EXHAUSTING:
            allowed[static_cast<int>(RippleState::ABSORBING)]   = true;
            allowed[static_cast<int>(RippleState::BREAKING)]    = true;
            allowed[static_cast<int>(RippleState::STABILIZING)] = true;
            allowed[static_cast<int>(RippleState::WALL_FORMING)] = true;
            break;

        case RippleState::WITHDRAWING:
            allowed[static_cast<int>(RippleState::BREAKING)]   = true;
            allowed[static_cast<int>(RippleState::REFILLING)]  = true;
            allowed[static_cast<int>(RippleState::IDLE)]       = true;
            break;

        case RippleState::BREAKING:
            allowed[static_cast<int>(RippleState::REFILLING)]   = true;
            allowed[static_cast<int>(RippleState::STABILIZING)] = true;
            allowed[static_cast<int>(RippleState::IDLE)]        = true;
            break;

        case RippleState::REFILLING:
            allowed[static_cast<int>(RippleState::STABILIZING)] = true;
            allowed[static_cast<int>(RippleState::BREAKING)]    = true;
            allowed[static_cast<int>(RippleState::IDLE)]        = true;
            break;

        case RippleState::STABILIZING:
            allowed[static_cast<int>(RippleState::WALL_FORMING)] = true;
            allowed[static_cast<int>(RippleState::ABSORBING)]    = true;
            allowed[static_cast<int>(RippleState::IDLE)]         = true;
            break;
    }

    for (int i = 0; i < N; ++i) {
        if (!allowed[i]) out[i] = 0.0;
    }
}

// ------------------------------------------------------------------
//  Step 4: persistence bonus
// ------------------------------------------------------------------

void ScoreBasedInference::apply_persistence(
    double out[N], RippleState prev) const {
    int idx = static_cast<int>(prev);
    if (idx >= 0 && idx < N)
        out[idx] += cfg_.persistence_bonus;
}

// ------------------------------------------------------------------
//  Step 5: winner selection
// ------------------------------------------------------------------

RippleState ScoreBasedInference::pick_winner(
    const double scores[N], RippleState* runner_up) {

    int best_idx = 0;
    int second_idx = 0;

    for (int i = 1; i < N; ++i) {
        if (scores[i] > scores[best_idx]) {
            second_idx = best_idx;
            best_idx = i;
        } else if (scores[i] > scores[second_idx] || second_idx == best_idx) {
            second_idx = i;
        }
    }

    if (runner_up)
        *runner_up = static_cast<RippleState>(second_idx);

    return static_cast<RippleState>(best_idx);
}

} // namespace orderflow::ripple
