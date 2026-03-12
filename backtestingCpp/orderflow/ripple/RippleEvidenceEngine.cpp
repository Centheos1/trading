#include "RippleEvidenceEngine.h"
#include <algorithm>
#include <cmath>

namespace orderflow::ripple {

RippleEvidenceEngine::RippleEvidenceEngine(const RippleConfig& cfg) : cfg_(cfg) {}

RippleEvidence RippleEvidenceEngine::compute(const RippleFeatures& f,
                                              const WallCandidate& wall) const {
    RippleEvidence ev{};
    const auto& w = cfg_.weights;

    // ------------------------------------------------------------------
    //  Shared helper quantities
    // ------------------------------------------------------------------

    // Flow direction relative to the wall:
    //   For a BID wall, sell aggressors hit it  → negative imbalance = toward bid.
    //   For an ASK wall, buy aggressors hit it → positive imbalance = toward ask.
    double flow_toward_wall = (wall.side == WallSide::BID)
        ? clamp01(-f.aggressive_flow_imbalance)
        : clamp01( f.aggressive_flow_imbalance);

    // Fractional depletion from peak [0,1].  0 = fully held, 1 = fully consumed.
    double depletion_pct = 0.0;
    if (wall.peak_qty > 0.0)
        depletion_pct = clamp01((wall.peak_qty - wall.current_qty) / wall.peak_qty);

    double max_dist = static_cast<double>(cfg_.activation_distance_ticks);

    // Impact normalised to roughly [0,1] via config scale.
    double impact_norm = clamp01(f.impact_per_unit_volume * w.impact_scale);

    // ------------------------------------------------------------------
    //  1. ABSORPTION EVIDENCE
    //
    //  Intuition: the wall is absorbing aggressive flow without failing.
    //  High when the wall is large, persistent, close, being hit, and
    //  price barely moves despite the aggression.
    // ------------------------------------------------------------------
    double abs_hold      = clamp01(1.0 - depletion_pct);
    double abs_size      = clamp01(f.relative_wall_size / cfg_.wall_min_relative_size);
    double abs_proximity = (max_dist > 0.0)
        ? clamp01(1.0 - f.distance_to_wall_ticks / max_dist) : 0.0;
    double abs_persist   = clamp01(f.wall_persistence_sec / w.persist_saturate_sec);
    double abs_low_impact = clamp01(1.0 - impact_norm);

    ev.absorption = w.abs_flow_w      * flow_toward_wall
                  + w.abs_hold_w      * abs_hold
                  + w.abs_size_w      * abs_size
                  + w.abs_proximity_w * abs_proximity
                  + w.abs_persist_w   * abs_persist
                  + w.abs_low_impact_w * abs_low_impact;
    ev.absorption = clamp01(ev.absorption);

    // ------------------------------------------------------------------
    //  2. EXHAUSTION EVIDENCE
    //
    //  Intuition: the aggressors are losing steam.  The wall is being
    //  consumed but refill is absent, per-unit impact is rising (thinner
    //  remaining depth), and one-sided flow has been sustained so long
    //  that momentum is likely fading.
    // ------------------------------------------------------------------
    double exh_depletion  = depletion_pct;

    // Refill deficit: if refill ≈ 0 and depletion > 0, this is 1.
    double total_rate = f.wall_refill_rate + f.wall_depletion_rate;
    double exh_low_refill = (total_rate > 0.0)
        ? clamp01(1.0 - f.wall_refill_rate / total_rate) : 0.0;

    // Rising impact: more price movement per unit of aggressive volume
    // means remaining depth is thinning — a hallmark of exhaustion.
    double exh_high_impact = impact_norm;

    // Sustained one-directional flow: high absolute imbalance means flow
    // has been persistent, which raises the probability of exhaustion.
    double exh_sustained = clamp01(std::abs(f.aggressive_flow_imbalance));

    // Queue instability: top-of-book churning signals depth uncertainty.
    double exh_queue = clamp01(f.queue_stability);

    ev.exhaustion = w.exh_depletion_w  * exh_depletion
                  + w.exh_low_refill_w * exh_low_refill
                  + w.exh_impact_w     * exh_high_impact
                  + w.exh_flow_w       * exh_sustained
                  + w.exh_queue_w      * exh_queue;
    ev.exhaustion = clamp01(ev.exhaustion);

    // ------------------------------------------------------------------
    //  3. WITHDRAWAL EVIDENCE
    //
    //  Intuition: the wall is disappearing via cancellation, not through
    //  trade absorption.  Cancel rate dominates, depth is shrinking, the
    //  spread may be widening (liquidity pulled), and there is little
    //  aggressive flow to explain the loss.
    // ------------------------------------------------------------------
    double total_shrink = f.wall_cancel_rate + f.wall_depletion_rate;
    double wth_cancel_dom = (total_shrink > 0.01)
        ? clamp01(f.wall_cancel_rate / total_shrink) : 0.0;

    double wth_depth_shrink = depletion_pct;

    double wth_spread = clamp01(f.spread_shock - 1.0);

    // Little aggression — depletion is NOT from fills.
    double wth_low_flow = clamp01(1.0 - std::abs(f.aggressive_flow_imbalance));

    ev.withdrawal = w.wth_cancel_w   * wth_cancel_dom
                  + w.wth_depth_w    * wth_depth_shrink
                  + w.wth_spread_w   * wth_spread
                  + w.wth_low_flow_w * wth_low_flow;
    ev.withdrawal = clamp01(ev.withdrawal);

    // ------------------------------------------------------------------
    //  4. BREAKOUT EVIDENCE
    //
    //  Intuition: aggressive flow is overwhelming the wall.  Price is
    //  accelerating through, the wall is nearly consumed, spread and
    //  volatility are expanding.
    // ------------------------------------------------------------------
    double brk_depletion = depletion_pct;
    double brk_flow      = flow_toward_wall;
    double brk_impact    = impact_norm;
    double brk_vol_exp   = clamp01(f.spread_shock - 1.0)
                         + clamp01(f.short_horizon_volatility * w.volatility_scale);
    brk_vol_exp = clamp01(brk_vol_exp * 0.5);

    // Microprice drift toward/through the wall.
    // BID wall break → price falls (negative drift), ASK wall break → price rises.
    double drift_toward = (wall.side == WallSide::BID)
        ? clamp01(-f.microprice_drift)
        : clamp01( f.microprice_drift);

    ev.breakout = w.brk_depletion_w * brk_depletion
                + w.brk_flow_w      * brk_flow
                + w.brk_impact_w    * brk_impact
                + w.brk_volatility_w * brk_vol_exp
                + w.brk_drift_w     * drift_toward;
    ev.breakout = clamp01(ev.breakout);

    // ------------------------------------------------------------------
    //  5. REFILL EVIDENCE
    //
    //  Intuition: after a depletion/break, new depth is reappearing on
    //  the same side.  Refill rate exceeds depletion, aggression is
    //  reversing, spread is normalising, and impact is declining.
    // ------------------------------------------------------------------
    double ref_strength = (total_rate > 0.0)
        ? clamp01(f.wall_refill_rate / total_rate) : 0.0;
    double ref_flow_rev = clamp01(1.0 - flow_toward_wall);
    double ref_spread   = clamp01(2.0 - f.spread_shock);
    double ref_low_imp  = clamp01(1.0 - impact_norm);

    ev.refill = w.ref_rate_w     * ref_strength
              + w.ref_flow_rev_w * ref_flow_rev
              + w.ref_spread_w   * ref_spread
              + w.ref_impact_w   * ref_low_imp;
    ev.refill = clamp01(ev.refill);

    // ------------------------------------------------------------------
    //  6. STABILIZATION EVIDENCE
    //
    //  Intuition: the market is settling after a break or bounce.
    //  Volatility declining, spread back to normal, queues stable,
    //  microprice flat, and flow balanced.
    // ------------------------------------------------------------------
    double stb_vol     = clamp01(1.0 - f.short_horizon_volatility * w.volatility_scale);
    double stb_spread  = clamp01(2.0 - f.spread_shock);
    double stb_queue   = clamp01(1.0 - f.queue_stability);
    double stb_drift   = clamp01(1.0 - std::abs(f.microprice_drift));
    double stb_balance = clamp01(1.0 - std::abs(f.aggressive_flow_imbalance));

    ev.stabilization = w.stb_vol_w    * stb_vol
                     + w.stb_spread_w * stb_spread
                     + w.stb_queue_w  * stb_queue
                     + w.stb_drift_w  * stb_drift
                     + w.stb_flow_w   * stb_balance;
    ev.stabilization = clamp01(ev.stabilization);

    // ------------------------------------------------------------------
    //  Phase 3: CVD divergence modulation (§9.9)
    //
    //  Bullish divergence (price down, CVD up) → strengthen absorption.
    //  Bearish divergence (price up, CVD down) → strengthen exhaustion.
    //  No divergence (strength == 0) → no change, preserving Phase 2 behavior.
    // ------------------------------------------------------------------
    if (f.cvd_divergence_detected && w.cvd_divergence_boost > 0.0) {
        double boost = w.cvd_divergence_boost * std::abs(f.cvd_divergence_strength);
        if (f.cvd_divergence_strength > 0.0)
            ev.absorption = clamp01(ev.absorption + boost);
        else
            ev.exhaustion = clamp01(ev.exhaustion + boost);
    }

    return ev;
}

} // namespace orderflow::ripple
