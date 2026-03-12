#include "RippleEngine.h"
#include "RippleFeatureEngine.h"
#include "RippleEvidenceEngine.h"
#include "ScoreBasedInference.h"
#include "TriggerDecisionEngine.h"
#include "RippleDiagnostics.h"
#include <cmath>

namespace orderflow::ripple {

RippleEngine::RippleEngine(const RippleConfig& cfg)
    : cfg_(cfg),
      ctx_(cfg),
      wall_det_(cfg),
      feat_engine_(std::make_unique<RippleFeatureEngine>(cfg)),
      ev_engine_(std::make_unique<RippleEvidenceEngine>(cfg)),
      state_tracker_(cfg, std::make_unique<ScoreBasedInference>(cfg)),
      trigger_engine_(std::make_unique<TriggerDecisionEngine>(cfg)),
      lifecycle_(cfg.lifecycle),
      liq_map_(cfg.liquidity_map) {
    ensure_diagnostics();
}

RippleEngine::~RippleEngine() = default;

void RippleEngine::ensure_diagnostics() {
    if (cfg_.enable_diagnostics && !diagnostics_) {
        diagnostics_ = std::make_shared<RippleDiagnostics>(
            cfg_.diagnostics_path, 10000, cfg_.console_diagnostics);
    }
}

void RippleEngine::reset() {
    ctx_.reset();
    wall_det_.reset();
    state_tracker_.reset();
    if (trigger_engine_) trigger_engine_->reset();
    lifecycle_.reset();
    liq_map_.reset();
    last_features_    = {};
    last_evidence_    = {};
    last_inference_   = {};
    last_decision_    = {};
    last_trade_price_      = 0.0;
    last_pipeline_ts_      = 0;
    pipeline_runs_         = 0;
    trade_count_           = 0;
    depth_count_           = 0;
    trades_since_pipeline_ = 0;
    session_vwap_    = 0.0;
    session_volume_  = 0.0;
    session_pv_      = 0.0;
    session_high_    = 0.0;
    session_low_     = 1e18;
    if (diagnostics_) {
        diagnostics_ = std::make_shared<RippleDiagnostics>(
            cfg_.diagnostics_path, 10000, cfg_.console_diagnostics);
    }
}

void RippleEngine::on_fill(const FillEvent& fill) {
    lifecycle_.on_fill(fill);
}

void RippleEngine::on_trade(const Trade& trade, const OrderBook& /*book*/) {
    ctx_.on_trade(trade);
    last_trade_price_ = trade.price;
    ++trade_count_;
    ++trades_since_pipeline_;

    // Session VWAP and high/low tracking
    session_pv_     += trade.price * trade.quantity;
    session_volume_ += trade.quantity;
    session_vwap_    = (session_volume_ > 0.0) ? session_pv_ / session_volume_ : 0.0;
    if (trade.price > session_high_) session_high_ = trade.price;
    if (trade.price < session_low_)  session_low_  = trade.price;

    // Allow trade bursts to trigger the pipeline so trade-driven evidence
    // (exhaustion, flow imbalance) doesn't go stale between depth events.
    if (trades_since_pipeline_ >= cfg_.trade_trigger_count &&
        last_pipeline_ts_ > 0 &&
        (trade.timestamp - last_pipeline_ts_) >= cfg_.pipeline_min_interval_ms / 2) {
        run_pipeline(trade.timestamp);
        last_pipeline_ts_ = trade.timestamp;
        trades_since_pipeline_ = 0;
    }
}

void RippleEngine::on_depth(const DepthUpdate& update, const OrderBook& book) {
    ctx_.on_depth(book, update.timestamp);
    wall_det_.on_depth(ctx_);
    ++depth_count_;

    bool due = (update.timestamp - last_pipeline_ts_) >= cfg_.pipeline_min_interval_ms
               || last_pipeline_ts_ == 0;
    if (due) {
        run_pipeline(update.timestamp);
        last_pipeline_ts_ = update.timestamp;
        trades_since_pipeline_ = 0;
    }
}

void RippleEngine::set_decision_callback(RippleDecisionCallback cb) {
    callback_ = std::move(cb);
}

void RippleEngine::set_inventory(const InventorySnapshot& inv) {
    inventory_ = inv;
}

void RippleEngine::set_diagnostics(std::shared_ptr<RippleDiagnostics> diag) {
    diagnostics_ = std::move(diag);
}

void RippleEngine::set_feature_engine(std::unique_ptr<IRippleFeatureEngine> engine) {
    feat_engine_ = std::move(engine);
}

void RippleEngine::set_evidence_engine(std::unique_ptr<IRippleEvidenceEngine> engine) {
    ev_engine_ = std::move(engine);
}

void RippleEngine::set_inference_backend(std::unique_ptr<IRippleStateInference> backend) {
    state_tracker_.set_inference(std::move(backend));
}

void RippleEngine::set_trigger_engine(std::unique_ptr<ITriggerDecisionEngine> engine) {
    trigger_engine_ = std::move(engine);
}

RippleState RippleEngine::current_state() const {
    return state_tracker_.current_state();
}

void RippleEngine::run_pipeline(Timestamp ts) {
    ++pipeline_runs_;

    const WallCandidate* wall = nullptr;
    const WallMetrics*   wall_m = nullptr;
    auto* bid_wall = wall_det_.primary_bid_wall();
    auto* ask_wall = wall_det_.primary_ask_wall();
    auto* bid_m    = wall_det_.primary_bid_metrics();
    auto* ask_m    = wall_det_.primary_ask_metrics();

    auto book = ctx_.snapshot_book();
    if (bid_wall && ask_wall) {
        double bid_dist = std::abs(book.mid_price - bid_wall->price);
        double ask_dist = std::abs(ask_wall->price - book.mid_price);
        if (bid_dist <= ask_dist) { wall = bid_wall; wall_m = bid_m; }
        else                      { wall = ask_wall; wall_m = ask_m; }
    } else if (bid_wall) {
        wall = bid_wall; wall_m = bid_m;
    } else if (ask_wall) {
        wall = ask_wall; wall_m = ask_m;
    }

    if (!wall) {
        RippleEvidence idle_ev{};
        last_inference_ = state_tracker_.update(idle_ev, nullptr, ts);
        last_features_ = {};
        last_evidence_ = idle_ev;
        last_decision_ = {};
        last_decision_.timestamp = ts;
        if (diagnostics_)
            diagnostics_->log(ts, last_features_, last_evidence_,
                              last_inference_, last_decision_);
        return;
    }

    last_features_  = feat_engine_->compute(ctx_, *wall, wall_m);

    // --- Phase 3: overlay CVD/VP features when available ---
    if (cvd_) {
        auto div = cvd_->detect_divergence(20);
        if (div.detected) {
            last_features_.cvd_divergence_detected = true;
            last_features_.cvd_divergence_strength =
                div.is_bearish ? -std::abs(div.delta_change) : std::abs(div.delta_change);
        }
    }
    if (vp_) {
        auto va = vp_->compute_value_area();
        last_features_.poc_price = va.poc_price;
        last_features_.vah       = va.vah;
        last_features_.val       = va.val;
        if (cfg_.tick_size > 0.0)
            last_features_.distance_to_poc_ticks =
                std::abs(book.mid_price - va.poc_price) / cfg_.tick_size;
    }

    last_evidence_  = ev_engine_->compute(last_features_, *wall);
    last_inference_ = state_tracker_.update(last_evidence_, wall, ts);
    last_decision_  = trigger_engine_->evaluate(
        last_inference_, last_evidence_, wall, inventory_, ts);

    if (diagnostics_)
        diagnostics_->log(ts, last_features_, last_evidence_,
                          last_inference_, last_decision_);

    if (last_decision_.intent != RippleIntent::NO_ACTION && callback_)
        callback_(last_decision_);

    // --- Phase 3: update liquidity map ---
    {
        LiquidityMapInput map_in;
        map_in.walls   = &wall_det_.active_walls();
        map_in.metrics = &wall_det_.metrics();
        map_in.mid_price = book.mid_price;
        map_in.sigma_P   = last_features_.short_horizon_volatility;
        map_in.imbalance = last_features_.top1_imbalance;
        map_in.vwap      = session_vwap_;
        map_in.session_high = session_high_;
        map_in.session_low  = (session_low_ < 1e17) ? session_low_ : 0.0;
        map_in.timestamp = ts;

        if (vp_) {
            auto prof = vp_->get_profile();
            auto va   = vp_->compute_value_area();
            map_in.profile   = &prof;
            map_in.poc_price = va.poc_price;
            map_in.vah       = va.vah;
            map_in.val       = va.val;

            auto tw = ctx_.trade_window(cfg_.feature_window_ms);
            if (!tw.recent.empty()) {
                map_in.trades_begin = tw.recent.data();
                map_in.trades_end   = tw.recent.data() + tw.recent.size();
            }
            liq_map_.update(map_in);
        } else {
            auto tw = ctx_.trade_window(cfg_.feature_window_ms);
            if (!tw.recent.empty()) {
                map_in.trades_begin = tw.recent.data();
                map_in.trades_end   = tw.recent.data() + tw.recent.size();
            }
            liq_map_.update(map_in);
        }
    }

    // --- Phase 2+3: lifecycle integration ---
    auto intent = last_decision_.intent;

    if (lifecycle_.is_idle() && wall) {
        bool is_enter =
            intent == RippleIntent::ENTER_BOUNCE_LONG  ||
            intent == RippleIntent::ENTER_BOUNCE_SHORT ||
            intent == RippleIntent::ENTER_BREAKOUT_LONG ||
            intent == RippleIntent::ENTER_BREAKOUT_SHORT;

        if (is_enter) {
            TradeArchetype arch =
                (intent == RippleIntent::ENTER_BOUNCE_LONG || intent == RippleIntent::ENTER_BOUNCE_SHORT)
                ? TradeArchetype::BOUNCE : TradeArchetype::BREAKOUT;
            TradeSide side =
                (intent == RippleIntent::ENTER_BOUNCE_LONG || intent == RippleIntent::ENTER_BREAKOUT_LONG)
                ? TradeSide::LONG : TradeSide::SHORT;
            double ss = (side == TradeSide::LONG) ? 1.0 : -1.0;
            double sigma = std::max(last_features_.short_horizon_volatility, cfg_.tick_size);
            double stop  = last_decision_.invalidation_price;
            double micro = ctx_.microprice();

            // Phase 3: use liquidity map destinations for target, fall back to sigma-based
            double target = micro + ss * cfg_.lifecycle.target_distance_sigma * sigma;
            auto dests = liq_map_.get_destinations_in_direction(side, micro);
            if (!dests.empty())
                target = dests.front().price;

            double qty = cfg_.max_position;

            if (lifecycle_.try_setup(arch, side, wall->price, stop, target, qty, ts)) {
                // Phase 3: compute scale-out plan from map destinations (§14.2.1)
                if (!dests.empty()) {
                    const auto& lc = cfg_.lifecycle;
                    int n = std::min(static_cast<int>(dests.size()), lc.scale_out_count);
                    ScaleOutTarget sot[LifecycleConfig::MAX_SCALE_OUT];
                    double remaining = qty;
                    for (int i = 0; i < n; ++i) {
                        sot[i].price    = dests[i].price;
                        double frac     = lc.scale_out_fractions[i];
                        sot[i].quantity = std::max(qty * frac, cfg_.tick_size);
                        remaining -= sot[i].quantity;
                        sot[i].new_stop = (i == 0) ? micro : dests[i - 1].price;
                    }
                    // §14.2.1: if fewer destinations than fractions, last level gets remainder
                    if (remaining > cfg_.tick_size && n > 0)
                        sot[n - 1].quantity += remaining;
                    lifecycle_.set_scale_out_targets(sot, n);
                }
                lifecycle_.confirm_entry(ts);
            }
        }
    }

    if (lifecycle_.is_active()) {
        if (intent == RippleIntent::EXIT_BOUNCE || intent == RippleIntent::EXIT_BREAKOUT)
            lifecycle_.on_exit_signal(ExitType::INVALIDATION, ts);
    }

    // Per-tick lifecycle update
    TickContext tc;
    tc.timestamp        = ts;
    tc.microprice       = ctx_.microprice();
    tc.last_trade_price = (last_trade_price_ > 0.0) ? last_trade_price_ : tc.microprice;
    tc.cvd_slope        = last_features_.aggressive_flow_imbalance;
    tc.impact           = last_features_.impact_per_unit_volume;
    tc.imbalance        = last_features_.top1_imbalance;
    tc.recent_sigma_P   = last_features_.short_horizon_volatility;
    tc.trade_rate       = 0.0;
    {
        auto tw = ctx_.trade_window(cfg_.feature_window_ms);
        int total_trades = tw.buy_count + tw.sell_count;
        int64_t span = tw.window_end - tw.window_start;
        if (total_trades > 0 && span > 0)
            tc.trade_rate = static_cast<double>(total_trades) / (static_cast<double>(span) / 1000.0);
    }

    auto default_risk = RiskBudgetSnapshot{};
    default_risk.es_budget       = DefaultTideSnapshot::ES_BUDGET;
    default_risk.risk_multiplier = DefaultTideSnapshot::RISK_MULTIPLIER;
    auto default_perms = DefaultWaveSnapshot::make().permissions;

    lifecycle_.on_tick(tc, default_risk, default_perms);
}

} // namespace orderflow::ripple
