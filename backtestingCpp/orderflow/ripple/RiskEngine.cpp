#include "RiskEngine.h"
#include <algorithm>
#include <cmath>

namespace orderflow::ripple {

RiskEngine::RiskEngine(const RiskConfig& cfg)
    : cfg_(cfg),
      es_budget_(0.0),
      max_position_usd_(0.0),
      risk_multiplier_(1.0),
      realized_vol_(0.80) {}

void RiskEngine::set_budget(double es_budget, double max_position_usd, double risk_multiplier) {
    es_budget_        = std::max(es_budget, 0.0);
    max_position_usd_ = std::max(max_position_usd, 0.0);
    risk_multiplier_  = std::clamp(risk_multiplier, 0.0, 1.0);
}

void RiskEngine::set_volatility(double realized_vol) {
    realized_vol_ = std::max(realized_vol, 1e-10);
}

void RiskEngine::on_fill(const FillEvent& fill) {
    double signed_qty = (fill.side == OrderSide::BUY) ? fill.quantity : -fill.quantity;
    double old_qty = position_qty_;
    position_qty_ += signed_qty;

    // Update average entry price for the position
    if (std::abs(position_qty_) < 1e-15) {
        // Flat — reset tracking
        avg_entry_price_ = 0.0;
        total_cost_basis_ = 0.0;
    } else if ((old_qty >= 0.0 && signed_qty > 0.0) || (old_qty <= 0.0 && signed_qty < 0.0)) {
        // Adding to position (same side)
        total_cost_basis_ += fill.price * std::abs(signed_qty);
        avg_entry_price_   = total_cost_basis_ / std::abs(position_qty_);
    } else if (std::abs(position_qty_) < std::abs(old_qty)) {
        // Reducing position — keep old avg price, shrink cost basis proportionally
        avg_entry_price_ = (std::abs(old_qty) > 1e-15)
                           ? total_cost_basis_ / std::abs(old_qty)
                           : fill.price;
        total_cost_basis_ = avg_entry_price_ * std::abs(position_qty_);
    } else {
        // Flipped sides through zero — new position at fill price
        avg_entry_price_  = fill.price;
        total_cost_basis_ = fill.price * std::abs(position_qty_);
    }

    last_price_ = fill.price;
    last_ts_    = fill.timestamp;
    recompute_consumed_es();
}

void RiskEngine::on_price_update(double price, int64_t timestamp) {
    last_price_ = price;
    last_ts_    = timestamp;
    recompute_consumed_es();
}

// §15.2: ES_i ≈ Q_i · P_i · σ̂_i · √(Δt) · φ(z_α)/(1-α)
double RiskEngine::compute_es_for_position(double quantity, double price) const {
    if (quantity <= 0.0 || price <= 0.0 || realized_vol_ <= 0.0)
        return 0.0;
    return quantity * price * realized_vol_ * SQRT_DT_1DAY * ES_MULTIPLIER_095;
}

void RiskEngine::recompute_consumed_es() {
    double abs_qty = std::abs(position_qty_);
    double price   = (last_price_ > 0.0) ? last_price_ : avg_entry_price_;
    consumed_es_   = compute_es_for_position(abs_qty, price);
}

bool RiskEngine::check_new_order(double quantity, double price) const {
    if (es_budget_ <= 0.0) return false;
    if (risk_multiplier_ <= 0.0) return false;
    if (price <= 0.0 || quantity <= 0.0) return false;

    double new_abs_qty = std::abs(position_qty_) + std::abs(quantity);
    double new_notional = new_abs_qty * price;
    if (new_notional > max_position_usd_ && max_position_usd_ > 0.0)
        return false;

    double new_es = compute_es_for_position(new_abs_qty, price);
    return new_es <= es_budget_;
}

double RiskEngine::get_allowed_size(double desired_quantity, double price) const {
    if (es_budget_ <= 0.0 || price <= 0.0 || risk_multiplier_ <= 0.0)
        return 0.0;

    double abs_desired = std::abs(desired_quantity);

    // Position cap
    double q_max_notional = (max_position_usd_ > 0.0)
                            ? max_position_usd_ / price
                            : 1e18;
    double current_abs = std::abs(position_qty_);
    double room_notional = std::max(q_max_notional - current_abs, 0.0);

    // ES budget cap
    double per_unit_es = compute_es_for_position(1.0, price);
    double room_es = (per_unit_es > 0.0)
                     ? std::max((es_budget_ - consumed_es_) / per_unit_es, 0.0)
                     : 0.0;

    double allowed = std::min({abs_desired, room_notional, room_es});
    return std::max(allowed, 0.0);
}

// §14.4 Position Sizing Formula (3 steps)
double RiskEngine::compute_position_size(double entry_price, double stop_price) const {
    if (entry_price <= 0.0 || stop_price <= 0.0) return 0.0;

    double stop_distance = std::abs(entry_price - stop_price);
    if (stop_distance < 1e-15) return 0.0;

    // Step 1: Risk-denominated raw size
    double q_risk = cfg_.target_risk_usd / stop_distance;

    // Step 2: Volatility adjustment with clamp [0.25, 2.0]
    double vol_ratio = (realized_vol_ > 1e-10)
                       ? cfg_.sigma_target / realized_vol_
                       : 1.0;
    vol_ratio = std::clamp(vol_ratio, 0.25, 2.0);
    double q_raw = q_risk * vol_ratio;

    // Step 3: Tide throttle and budget clip
    double r_budget = (es_budget_ > 0.0)
                      ? std::clamp((es_budget_ - consumed_es_) / es_budget_, 0.0, 1.0)
                      : 0.0;

    double q_max = (max_position_usd_ > 0.0 && entry_price > 0.0)
                   ? max_position_usd_ / entry_price
                   : 1e18;

    double q_final = q_raw * risk_multiplier_ * r_budget;
    return std::clamp(q_final, 0.0, q_max);
}

double RiskEngine::get_remaining_budget() const {
    return std::max(es_budget_ - consumed_es_, 0.0);
}

bool RiskEngine::is_budget_exhausted() const {
    if (es_budget_ <= 0.0) return true;
    return consumed_es_ >= es_budget_;
}

double RiskEngine::get_position_usd() const {
    return std::abs(position_qty_) * last_price_;
}

double RiskEngine::get_unrealized_pnl() const {
    if (std::abs(position_qty_) < 1e-15 || last_price_ <= 0.0 || avg_entry_price_ <= 0.0)
        return 0.0;
    return (last_price_ - avg_entry_price_) * position_qty_;
}

RiskBudgetSnapshot RiskEngine::get_snapshot() const {
    RiskBudgetSnapshot snap;
    snap.timestamp        = last_ts_;
    snap.es_budget        = es_budget_;
    snap.consumed_es      = consumed_es_;
    snap.risk_multiplier  = risk_multiplier_;
    snap.max_position_usd = max_position_usd_;
    return snap;
}

void RiskEngine::reset() {
    es_budget_         = 0.0;
    max_position_usd_  = 0.0;
    risk_multiplier_   = 1.0;
    realized_vol_      = 0.80;
    position_qty_      = 0.0;
    avg_entry_price_   = 0.0;
    total_cost_basis_  = 0.0;
    consumed_es_       = 0.0;
    last_price_        = 0.0;
    last_ts_           = 0;
}

} // namespace orderflow::ripple
