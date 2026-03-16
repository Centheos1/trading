#include "TradeLifecycleEngine.h"
#include <algorithm>
#include <cmath>
#include <sstream>

namespace orderflow::ripple {

TradeLifecycleEngine::TradeLifecycleEngine(const LifecycleConfig& cfg)
    : cfg_(cfg) {}

void TradeLifecycleEngine::reset() {
    state_              = LifecycleState::CANCELLED;
    has_trade_          = false;
    has_intent_         = false;
    trade_counter_      = 0;
    completed_trades_   = 0;
    trailing_stop_active_ = false;
    scale_count_        = 0;
    scale_out_count_    = 0;
    last_microprice_    = 0.0;
    scale_out_target_count_ = 0;
    scale_out_idx_          = 0;
    scale_out_pending_      = false;
    pending_intent_     = {};
    cumulative_pnl_     = 0.0;
    peak_equity_        = 0.0;
    max_drawdown_       = 0.0;
}

double TradeLifecycleEngine::side_sign() const {
    return (side_ == TradeSide::LONG) ? 1.0 : -1.0;
}

std::string TradeLifecycleEngine::make_trade_id() {
    std::ostringstream oss;
    oss << "T" << (++trade_counter_);
    return oss.str();
}

bool TradeLifecycleEngine::is_active() const {
    return has_trade_ &&
           (state_ == LifecycleState::CONFIRMATION ||
            state_ == LifecycleState::EXPANSION ||
            state_ == LifecycleState::MATURATION);
}

bool TradeLifecycleEngine::is_idle() const {
    if (!has_trade_) return true;
    if (state_ == LifecycleState::COOLDOWN)
        return last_tick_ts_ >= cooldown_end_;
    return false;
}

// -----------------------------------------------------------------------
//  Setup & Entry
// -----------------------------------------------------------------------

bool TradeLifecycleEngine::try_setup(
    TradeArchetype archetype, TradeSide side,
    double wall_price, double stop_price, double target_price,
    double quantity, int64_t timestamp) {

    if (!is_idle()) return false;

    trade_id_         = make_trade_id();
    archetype_        = archetype;
    side_             = side;
    wall_price_       = wall_price;
    stop_price_       = stop_price;
    target_price_     = target_price;
    quantity_         = quantity;
    initial_quantity_ = quantity;
    filled_quantity_  = 0.0;
    avg_fill_price_   = 0.0;
    entry_price_      = 0.0;
    setup_ts_         = timestamp;
    entry_ts_         = 0;
    confirmation_ts_  = 0;
    scale_count_      = 0;
    trailing_stop_active_ = false;
    entry_trade_rate_ = 0.0;
    entry_impact_     = 0.0;
    last_microprice_  = 0.0;
    scale_out_count_  = 0;
    scale_out_target_count_ = 0;
    scale_out_idx_          = 0;
    scale_out_pending_      = false;

    has_trade_ = true;
    transition_to(LifecycleState::SETUP, timestamp);
    return true;
}

bool TradeLifecycleEngine::confirm_entry(int64_t timestamp) {
    if (!has_trade_ || state_ != LifecycleState::SETUP) return false;

    transition_to(LifecycleState::ENTRY, timestamp);
    emit_entry_intent(timestamp);
    return true;
}

// -----------------------------------------------------------------------
//  Fill handling
// -----------------------------------------------------------------------

void TradeLifecycleEngine::on_fill(const FillEvent& fill) {
    if (!has_trade_) return;

    if (state_ == LifecycleState::ENTRY) {
        filled_quantity_ = fill.quantity;
        avg_fill_price_  = fill.price;
        entry_price_     = fill.price;
        entry_ts_        = fill.timestamp;

        // §14.2.1: first scale-out stop = entry_price (breakeven)
        if (scale_out_target_count_ > 0)
            scale_out_targets_[0].new_stop = entry_price_;

        transition_to(LifecycleState::CONFIRMATION, fill.timestamp);

    } else if ((state_ == LifecycleState::EXPANSION || state_ == LifecycleState::MATURATION)
               && scale_out_pending_) {
        // Scale-out fill (§14.2.1)
        filled_quantity_ -= fill.quantity;
        if (filled_quantity_ < 1e-15) filled_quantity_ = 0.0;

        auto& sot = scale_out_targets_[scale_out_idx_];
        sot.filled = true;
        stop_price_ = sot.new_stop;
        ++scale_out_idx_;
        ++scale_out_count_;
        scale_out_pending_ = false;

        // If all scale-out levels done, activate trailing stop for remainder
        if (scale_out_idx_ >= scale_out_target_count_)
            trailing_stop_active_ = true;

        // If position fully exited by scale-outs, go to cooldown
        if (filled_quantity_ <= 1e-15) {
            filled_quantity_ = 0.0;
            cooldown_end_    = fill.timestamp + cfg_.cooldown_ms;
            ++completed_trades_;
            transition_to(LifecycleState::COOLDOWN, fill.timestamp);
        }

    } else if (state_ == LifecycleState::EXPANSION && !scale_out_pending_) {
        // Scale-in fill
        double total_qty = filled_quantity_ + fill.quantity;
        if (total_qty > 0.0)
            avg_fill_price_ = (avg_fill_price_ * filled_quantity_
                               + fill.price * fill.quantity) / total_qty;
        filled_quantity_ = total_qty;
        stop_price_ = entry_price_;

    } else if (state_ == LifecycleState::EXIT) {
        filled_quantity_ -= fill.quantity;
        if (filled_quantity_ <= 1e-15) {
            filled_quantity_ = 0.0;
            cooldown_end_    = fill.timestamp + cfg_.cooldown_ms;
            ++completed_trades_;
            transition_to(LifecycleState::COOLDOWN, fill.timestamp);
        }
    }
}

// -----------------------------------------------------------------------
//  Per-tick update
// -----------------------------------------------------------------------

void TradeLifecycleEngine::on_tick(
    const TickContext& ctx,
    const RiskBudgetSnapshot& risk,
    const PermissionSet& permissions) {

    if (!has_trade_) return;
    last_tick_ts_    = ctx.timestamp;
    last_microprice_ = ctx.microprice;

    switch (state_) {

    case LifecycleState::SETUP:
    case LifecycleState::ENTRY: {
        if (ctx.timestamp - setup_ts_ > cfg_.confirmation_window_ms) {
            transition_to(LifecycleState::CANCELLED, ctx.timestamp);
            has_trade_ = false;
        }
        break;
    }

    case LifecycleState::CONFIRMATION:
    case LifecycleState::EXPANSION:
    case LifecycleState::MATURATION: {
        // Capture entry-time baselines on first active tick
        if (entry_trade_rate_ == 0.0 && ctx.trade_rate > 0.0) {
            entry_trade_rate_ = ctx.trade_rate;
            entry_impact_     = ctx.impact;
        }

        // --- Exit checks in priority order (§13.1) ---
        if (check_risk_budget_exit(risk))               { begin_exit(ExitType::RISK_BUDGET,  ctx.timestamp); return; }
        if (check_invalidation_exit(ctx.last_trade_price)) { begin_exit(ExitType::INVALIDATION, ctx.timestamp); return; }
        if (check_time_exit(ctx.timestamp))              { begin_exit(ExitType::TIME,         ctx.timestamp); return; }
        if (check_target_exit(ctx.microprice))           { begin_exit(ExitType::TARGET,       ctx.timestamp); return; }
        if (check_exhaustion_exit(ctx))                  { begin_exit(ExitType::EXHAUSTION,   ctx.timestamp); return; }

        // Permission revocation → exit
        if (!permissions.is_allowed(archetype_, side_))  { begin_exit(ExitType::INVALIDATION, ctx.timestamp); return; }

        // --- Scale-out check (§14.2.1) ---
        if (!scale_out_pending_ && check_scale_out(ctx)) {
            emit_scale_out_intent(ctx.timestamp);
            scale_out_pending_ = true;
        }

        // --- State advancement ---
        if (state_ == LifecycleState::CONFIRMATION && check_expansion(ctx))
            transition_to(LifecycleState::EXPANSION, ctx.timestamp);

        if (state_ == LifecycleState::EXPANSION) {
            if (check_scale_in(ctx)) {
                ++scale_count_;
                emit_scale_in_intent(ctx.timestamp);
            }
            if (check_maturation(ctx))
                transition_to(LifecycleState::MATURATION, ctx.timestamp);
        }

        // §12.2: re-acceleration — only if no scale-out has occurred yet
        if (state_ == LifecycleState::MATURATION && scale_out_count_ == 0 && !check_maturation(ctx))
            transition_to(LifecycleState::EXPANSION, ctx.timestamp);

        if (trailing_stop_active_)
            update_trailing_stop(ctx);

        break;
    }

    case LifecycleState::EXIT:
        break;   // waiting for exit fill

    case LifecycleState::COOLDOWN:
        if (ctx.timestamp >= cooldown_end_)
            has_trade_ = false;
        break;

    case LifecycleState::CANCELLED:
        has_trade_ = false;
        break;
    }
}

// -----------------------------------------------------------------------
//  External exit signal
// -----------------------------------------------------------------------

void TradeLifecycleEngine::on_exit_signal(ExitType type, int64_t timestamp) {
    if (!is_active()) return;
    begin_exit(type, timestamp);
}

// -----------------------------------------------------------------------
//  Snapshot
// -----------------------------------------------------------------------

TradeStateSnapshot TradeLifecycleEngine::get_snapshot() const {
    TradeStateSnapshot snap;
    if (!has_trade_) return snap;

    snap.trade_id      = trade_id_;
    snap.archetype     = archetype_;
    snap.side          = side_;
    snap.state         = state_;
    snap.entry_price   = entry_price_;
    snap.stop_price    = stop_price_;
    snap.target_price  = target_price_;
    snap.quantity      = filled_quantity_;
    snap.scale_count   = scale_count_;

    if (entry_price_ > 0.0 && last_microprice_ > 0.0 && filled_quantity_ > 0.0)
        snap.unrealized_pnl = (last_microprice_ - entry_price_) * side_sign() * filled_quantity_;

    if (entry_ts_ > 0 && last_tick_ts_ > entry_ts_)
        snap.hold_time_ms = last_tick_ts_ - entry_ts_;

    return snap;
}

StrategyExecutionIntent TradeLifecycleEngine::consume_pending_intent() {
    has_intent_ = false;
    auto intent = pending_intent_;
    pending_intent_ = {};
    return intent;
}

// -----------------------------------------------------------------------
//  State transitions
// -----------------------------------------------------------------------

void TradeLifecycleEngine::transition_to(LifecycleState new_state, int64_t timestamp) {
    state_ = new_state;
    if (new_state == LifecycleState::CONFIRMATION)
        confirmation_ts_ = timestamp;
    // Activate trailing stop on MATURATION entry
    if (new_state == LifecycleState::MATURATION)
        trailing_stop_active_ = true;
}

void TradeLifecycleEngine::begin_exit(ExitType type, int64_t timestamp) {
    if (state_ == LifecycleState::EXIT) return;

    double trade_pnl = side_sign() * (last_microprice_ - entry_price_) * quantity_;
    cumulative_pnl_ += trade_pnl;
    peak_equity_ = std::max(peak_equity_, cumulative_pnl_);
    double dd = peak_equity_ - cumulative_pnl_;
    max_drawdown_ = std::max(max_drawdown_, dd);

    last_exit_type_ = type;
    transition_to(LifecycleState::EXIT, timestamp);
    emit_exit_intent(type, timestamp);
}

// -----------------------------------------------------------------------
//  Exit checks
// -----------------------------------------------------------------------

bool TradeLifecycleEngine::check_risk_budget_exit(const RiskBudgetSnapshot& risk) const {
    if (risk.es_budget <= 0.0) return false;
    return (risk.consumed_es / risk.es_budget >= cfg_.budget_exit_threshold) ||
           (risk.risk_multiplier == 0.0);
}

bool TradeLifecycleEngine::check_invalidation_exit(double last_trade_price) const {
    if (stop_price_ <= 0.0 || last_trade_price <= 0.0) return false;
    return (last_trade_price - stop_price_) * side_sign() < 0.0;
}

bool TradeLifecycleEngine::check_time_exit(int64_t timestamp) const {
    if (cfg_.max_hold_time_ms <= 0 || entry_ts_ <= 0) return false;
    return (timestamp - entry_ts_) > cfg_.max_hold_time_ms;
}

bool TradeLifecycleEngine::check_target_exit(double microprice) const {
    // When scale-out targets exist and haven't all been filled,
    // target exit is handled by the scale-out mechanism instead.
    if (scale_out_target_count_ > 0 && scale_out_idx_ < scale_out_target_count_)
        return false;
    if (target_price_ <= 0.0 || microprice <= 0.0) return false;
    return (microprice - target_price_) * side_sign() >= 0.0;
}

bool TradeLifecycleEngine::check_exhaustion_exit(const TickContext& ctx) const {
    // All three conditions must hold simultaneously (§13.3.3)
    bool cvd_fading     = ctx.cvd_slope * side_sign() < cfg_.exhaustion_cvd_slope_thresh;
    bool rate_declining  = entry_trade_rate_ > 0.0 && ctx.trade_rate < entry_trade_rate_ * 0.5;
    bool impact_rising   = entry_impact_ > 0.0 && ctx.impact > entry_impact_ * cfg_.exhaustion_impact_ratio;
    return cvd_fading && rate_declining && impact_rising;
}

// -----------------------------------------------------------------------
//  State advancement
// -----------------------------------------------------------------------

bool TradeLifecycleEngine::check_expansion(const TickContext& ctx) const {
    if (ctx.recent_sigma_P <= 0.0 || entry_price_ <= 0.0) return false;
    double pnl_sigma = (ctx.microprice - entry_price_) * side_sign() / ctx.recent_sigma_P;
    return pnl_sigma > cfg_.expand_threshold_sigma;
}

bool TradeLifecycleEngine::check_maturation(const TickContext& ctx) const {
    if (target_price_ <= 0.0 || entry_price_ <= 0.0) return false;
    double total_distance     = (target_price_ - entry_price_) * side_sign();
    double distance_remaining = (target_price_ - ctx.microprice) * side_sign();
    if (total_distance <= 0.0) return false;
    return distance_remaining < total_distance * cfg_.maturation_momentum_fade;
}

bool TradeLifecycleEngine::check_scale_in(const TickContext& ctx) const {
    if (state_ != LifecycleState::EXPANSION) return false;
    if (scale_count_ >= cfg_.max_scale_ins) return false;
    if (ctx.recent_sigma_P <= 0.0 || entry_price_ <= 0.0) return false;

    double favorable_move = (ctx.microprice - entry_price_) * side_sign();
    if (favorable_move < cfg_.scale_in_threshold_sigma * ctx.recent_sigma_P) return false;
    if (ctx.cvd_slope * side_sign() < cfg_.scale_in_cvd_slope_min) return false;
    return true;
}

void TradeLifecycleEngine::update_trailing_stop(const TickContext& ctx) {
    if (ctx.recent_sigma_P <= 0.0) return;
    double trail_distance = cfg_.trailing_stop_sigma * ctx.recent_sigma_P;

    if (side_ == TradeSide::LONG) {
        double new_stop = ctx.microprice - trail_distance;
        stop_price_ = std::max(stop_price_, new_stop);
    } else {
        double new_stop = ctx.microprice + trail_distance;
        stop_price_ = std::min(stop_price_, new_stop);
    }
}

// -----------------------------------------------------------------------
//  Intent generation
// -----------------------------------------------------------------------

void TradeLifecycleEngine::emit_entry_intent(int64_t timestamp) {
    pending_intent_.timestamp   = timestamp;
    pending_intent_.trade_id    = trade_id_;
    pending_intent_.side        = (side_ == TradeSide::LONG) ? OrderSide::BUY : OrderSide::SELL;
    pending_intent_.quantity    = quantity_;
    pending_intent_.order_type  = (archetype_ == TradeArchetype::BOUNCE) ? OrderType::LIMIT : OrderType::MARKET;
    pending_intent_.limit_price = wall_price_;
    pending_intent_.intent_type = IntentType::ENTRY;
    pending_intent_.exit_reason = ExitReason::NONE;
    pending_intent_.urgency     = Urgency::NORMAL;
    has_intent_ = true;
}

void TradeLifecycleEngine::emit_exit_intent(ExitType type, int64_t timestamp) {
    pending_intent_.timestamp   = timestamp;
    pending_intent_.trade_id    = trade_id_;
    pending_intent_.side        = (side_ == TradeSide::LONG) ? OrderSide::SELL : OrderSide::BUY;
    pending_intent_.quantity    = filled_quantity_;
    pending_intent_.intent_type = IntentType::EXIT;

    switch (type) {
        case ExitType::INVALIDATION: pending_intent_.exit_reason = ExitReason::INVALIDATION; break;
        case ExitType::TARGET:       pending_intent_.exit_reason = ExitReason::TARGET;       break;
        case ExitType::EXHAUSTION:   pending_intent_.exit_reason = ExitReason::EXHAUSTION;   break;
        case ExitType::TIME:         pending_intent_.exit_reason = ExitReason::TIME;         break;
        case ExitType::RISK_BUDGET:  pending_intent_.exit_reason = ExitReason::RISK_BUDGET;  break;
    }

    bool immediate = (type == ExitType::INVALIDATION || type == ExitType::RISK_BUDGET);
    pending_intent_.urgency = immediate ? Urgency::IMMEDIATE : Urgency::NORMAL;

    // Order type per strategy.md §13.3
    if (type == ExitType::TARGET) {
        pending_intent_.order_type  = OrderType::LIMIT;
        pending_intent_.limit_price = target_price_;
    } else if (type == ExitType::EXHAUSTION) {
        pending_intent_.order_type  = OrderType::LIMIT;
        pending_intent_.limit_price = last_microprice_;
    } else {
        pending_intent_.order_type  = OrderType::MARKET;
        pending_intent_.limit_price = 0.0;
    }
    has_intent_ = true;
}

void TradeLifecycleEngine::set_scale_out_targets(const ScaleOutTarget* targets, int count) {
    scale_out_target_count_ = std::min(count, MAX_SCALE_OUT_TARGETS);
    for (int i = 0; i < scale_out_target_count_; ++i)
        scale_out_targets_[i] = targets[i];
    scale_out_idx_     = 0;
    scale_out_pending_ = false;
}

bool TradeLifecycleEngine::check_scale_out(const TickContext& ctx) const {
    if (scale_out_idx_ >= scale_out_target_count_) return false;
    if (state_ != LifecycleState::EXPANSION && state_ != LifecycleState::MATURATION) return false;
    const auto& t = scale_out_targets_[scale_out_idx_];
    return (ctx.microprice - t.price) * side_sign() >= 0.0;
}

void TradeLifecycleEngine::emit_scale_out_intent(int64_t timestamp) {
    const auto& t = scale_out_targets_[scale_out_idx_];
    pending_intent_.timestamp   = timestamp;
    pending_intent_.trade_id    = trade_id_;
    pending_intent_.side        = (side_ == TradeSide::LONG) ? OrderSide::SELL : OrderSide::BUY;
    pending_intent_.quantity    = t.quantity;
    pending_intent_.order_type  = OrderType::LIMIT;
    pending_intent_.limit_price = t.price;
    pending_intent_.intent_type = IntentType::EXIT;
    pending_intent_.exit_reason = ExitReason::TARGET;
    pending_intent_.urgency     = Urgency::NORMAL;
    has_intent_ = true;
}

void TradeLifecycleEngine::emit_scale_in_intent(int64_t timestamp) {
    pending_intent_.timestamp   = timestamp;
    pending_intent_.trade_id    = trade_id_;
    pending_intent_.side        = (side_ == TradeSide::LONG) ? OrderSide::BUY : OrderSide::SELL;
    pending_intent_.quantity    = initial_quantity_;
    pending_intent_.order_type  = OrderType::MARKET;
    pending_intent_.limit_price = 0.0;
    pending_intent_.intent_type = IntentType::SCALE_IN;
    pending_intent_.exit_reason = ExitReason::NONE;
    pending_intent_.urgency     = Urgency::NORMAL;
    has_intent_ = true;
}

} // namespace orderflow::ripple
