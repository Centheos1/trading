#pragma once

#include "RippleTypes.h"

namespace orderflow::ripple {

struct EvidenceWeights {
    // --- absorption: wall absorbing aggression without failing ---
    double abs_flow_w        = 0.25;   // aggressive flow directed at the wall
    double abs_hold_w        = 0.20;   // wall retaining qty (1 - depletion_pct)
    double abs_size_w        = 0.15;   // relative wall size vs threshold
    double abs_proximity_w   = 0.10;   // wall closeness to mid price
    double abs_persist_w     = 0.15;   // wall has been present for a while
    double abs_low_impact_w  = 0.15;   // low price response despite aggression

    // --- exhaustion: aggressive flow losing momentum ---
    double exh_depletion_w   = 0.25;   // wall being consumed (depletion_pct)
    double exh_low_refill_w  = 0.20;   // refill rate low relative to depletion
    double exh_impact_w      = 0.25;   // rising impact per unit volume
    double exh_flow_w        = 0.15;   // sustained one-directional flow
    double exh_queue_w       = 0.15;   // queue instability at top of book

    // --- withdrawal: wall vanishing via cancellation, not fills ---
    double wth_cancel_w      = 0.40;   // cancel rate dominates total shrinkage
    double wth_depth_w       = 0.25;   // depth shrinkage (depletion_pct)
    double wth_spread_w      = 0.20;   // spread shock (liquidity pulled)
    double wth_low_flow_w    = 0.15;   // little aggressive flow (so loss ≠ fills)

    // --- breakout: aggressive flow overwhelming the wall ---
    double brk_depletion_w   = 0.25;   // wall heavily depleted
    double brk_flow_w        = 0.25;   // strong directional flow toward wall
    double brk_impact_w      = 0.15;   // high impact per volume
    double brk_volatility_w  = 0.20;   // spread / volatility expansion
    double brk_drift_w       = 0.15;   // microprice accelerating through wall

    // --- refill: depth reappearing after depletion/break ---
    double ref_rate_w        = 0.35;   // refill > depletion
    double ref_flow_rev_w    = 0.25;   // aggression reversed or balanced
    double ref_spread_w      = 0.20;   // spread normalising
    double ref_impact_w      = 0.20;   // declining impact per volume

    // --- stabilization: market settling down ---
    double stb_vol_w         = 0.25;   // volatility declining
    double stb_spread_w      = 0.20;   // spread near baseline
    double stb_queue_w       = 0.20;   // top-of-book stable
    double stb_drift_w       = 0.15;   // microprice flat
    double stb_flow_w        = 0.20;   // flow near zero / balanced

    // --- normaliser knobs (instrument-dependent) ---
    double impact_scale         = 1e4;    // impact_per_unit_volume * this → ~[0,1]
    double volatility_scale     = 10.0;   // short_horizon_volatility * this → ~[0,1]
    double persist_saturate_sec = 10.0;   // persistence saturates at this many seconds

    // Phase 3: CVD divergence boost for absorption/exhaustion (§9.9)
    double cvd_divergence_boost = 0.1;    // additive boost magnitude when CVD diverges
};

// Phase 2: trade lifecycle configuration (strategy.md §12–§14).
struct LifecycleConfig {
    int64_t confirmation_window_ms     = 5000;    // §12.2: max time in SETUP/ENTRY before cancel
    double  expand_threshold_sigma     = 1.0;     // §12.2: PnL > this × σ_P → EXPANSION
    int64_t max_hold_time_ms           = 300000;  // §13.3.4: time exit after this (5 min)
    int64_t cooldown_ms                = 30000;   // §12.1: no new entries after exit
    int32_t max_scale_ins              = 1;       // §14.1: max scale-in tranches
    double  scale_in_threshold_sigma   = 2.0;     // §14.1.1: favorable move before scale-in
    double  scale_in_cvd_slope_min     = 0.02;    // §14.1.1: min signed CVD slope for scale-in
    double  trailing_stop_sigma        = 2.0;     // §14.3: trail distance in σ_P
    double  exhaustion_cvd_slope_thresh = 0.01;   // §13.3.3: CVD slope below this = fading
    double  exhaustion_impact_ratio    = 1.5;     // §13.3.3: impact must be this × entry impact
    double  target_distance_sigma      = 3.0;     // fallback when no map destinations available
    double  budget_exit_threshold      = 0.90;    // §13.3.5: exit when consumed_es / budget ≥ this
    double  maturation_momentum_fade   = 0.5;     // enter MATURATION when this fraction of target remaining

    // §14.2: scale-out fractions (sum should ≈ 1.0)
    static constexpr int MAX_SCALE_OUT = 3;
    double  scale_out_fractions[MAX_SCALE_OUT] = {0.34, 0.33, 0.33};
    int32_t scale_out_count                    = 3;
};

// Phase 3: liquidity map configuration (strategy.md §10).
struct LiquidityMapConfig {
    int32_t max_levels             = 100;    // §21.1: bounded level count
    double  wall_min_quality       = 0.3;    // min wall quality to include in map
    double  hvn_volume_ratio       = 1.5;    // volume > median × this → HVN
    double  lvn_volume_ratio       = 0.5;    // volume < median × this → LVN
    double  void_min_gap_sigma     = 3.0;    // min gap in σ_P to qualify as void corridor

    // dest_score weights (§10.3)
    double  dest_w_depth           = 0.25;
    double  dest_w_volume          = 0.25;
    double  dest_w_vwap_proximity  = 0.25;
    double  dest_w_structural      = 0.25;

    // hold_score weights (§9.6.3 — deterministic V1)
    double  hold_w_quality         = 0.35;
    double  hold_w_depth           = 0.25;
    double  hold_w_imbalance       = 0.20;
    double  hold_w_refill          = 0.20;
};

struct RippleConfig {
    double tick_size = 0.01;

    // ---- wall detection ----
    double   wall_min_relative_size   = 3.0;
    int      wall_min_levels          = 1;
    int      wall_max_distance_ticks  = 50;
    Duration wall_dedup_cooldown_ms   = 2000;
    Duration wall_min_age_ms          = 1000;
    double   wall_break_threshold_pct = 0.15;

    // ---- feature windows ----
    Duration feature_window_ms        = 5000;
    Duration microprice_lookback_ms   = 2000;
    Duration volatility_window_ms     = 10000;
    int      book_depth_levels        = 20;

    // ---- evidence thresholds ----
    double   absorption_entry         = 0.6;
    double   absorption_persist       = 0.4;
    double   exhaustion_entry         = 0.5;
    double   exhaustion_persist       = 0.3;
    double   withdrawal_entry         = 0.6;
    double   breakout_entry           = 0.7;
    double   refill_entry             = 0.5;
    double   stabilization_entry      = 0.5;

    // ---- score-based inference ----
    double   persistence_bonus        = 0.10;  // additive bonus to prior state score
    double   idle_exit_threshold      = 0.30;  // min winning score required to leave IDLE
    double   state_switch_margin      = 0.05;  // min margin between new winner and prior state
    double   wall_forming_base_score  = 0.25;  // base score for WALL_FORMING when wall present
    double   idle_base_score          = 0.15;  // base score for IDLE
    double   lifecycle_boost          = 0.15;  // boost to state matching wall lifecycle

    // ---- state machine (tracker layer) ----
    Duration min_dwell_fast_ms        = 200;
    Duration min_dwell_slow_ms        = 500;
    Duration intent_cooldown_ms       = 1000;
    Duration global_intent_cooldown_ms = 500;
    Duration post_break_cooldown_ms   = 3000;
    Duration refill_reaction_ms       = 500;

    // ---- trigger decisions ----
    int      activation_distance_ticks = 10;
    double   prepare_wall_threshold   = 2.0;
    Duration max_intent_age_ms        = 5000;

    // Bounce entry guards
    double   bounce_max_break_risk      = 0.45;
    double   bounce_max_withdrawal_risk = 0.45;

    // Breakout entry guards
    double   breakout_max_absorption    = 0.40;

    // Preemptive cancel: fires when withdrawal or breakout evidence
    // exceeds this threshold even without a state transition.
    double   cancel_risk_threshold      = 0.55;

    // Rearm: minimum refill or stabilization evidence required
    double   rearm_min_evidence         = 0.40;

    // Time stop: forced exit after this duration since entry (0 = disabled)
    Duration time_stop_ms               = 30000;

    // ---- evidence weights ----
    EvidenceWeights weights;

    // ---- inventory ----
    double   max_position             = 1.0;

    // ---- context / history limits ----
    size_t   max_context_trades       = 5000;
    size_t   max_context_snaps        = 5000;

    // ---- pipeline throttle ----
    Duration pipeline_min_interval_ms = 250;   // full pipeline runs at most this often
    size_t   trade_trigger_count      = 50;    // force pipeline after this many trades

    // ---- trade lifecycle (Phase 2) ----
    LifecycleConfig lifecycle;

    // ---- liquidity map (Phase 3) ----
    LiquidityMapConfig liquidity_map;

    // ---- diagnostics / replay ----
    bool        enable_diagnostics       = true;
    bool        console_diagnostics      = false;  // compact per-event stdout log
    std::string diagnostics_path;                   // JSONL file path (empty = memory only)
    bool        replay_mode              = false;
};

} // namespace orderflow::ripple
