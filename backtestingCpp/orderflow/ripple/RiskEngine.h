#pragma once

#include <cstdint>
#include "../Schemas.h"

namespace orderflow::ripple {

/// Phase 4: Simple global ES throttle and position sizing (strategy.md §15.2, §14.4).
///
/// Pure arithmetic on the hot path — no allocation, no branching beyond
/// simple comparisons.  Target: check_new_order < 1 µs, on_fill < 1 µs.
///
/// V1 constraints:
///   - Single global ES budget (no sleeve/asset/cell hierarchy)
///   - Parametric ES with Gaussian assumption
///   - 1-day risk horizon
///   - No Euler decomposition
class RiskEngine {
public:
    explicit RiskEngine(const RiskConfig& cfg = {});

    // --- Tide interface: set budget parameters ---
    void set_budget(double es_budget, double max_position_usd, double risk_multiplier);
    void set_volatility(double realized_vol);

    // --- Event processing ---
    void on_fill(const FillEvent& fill);
    void on_price_update(double price, int64_t timestamp);

    // --- Order admission (hot path, pure arithmetic) ---
    bool   check_new_order(double quantity, double price) const;
    double get_allowed_size(double desired_quantity, double price) const;

    // --- Position sizing (§14.4) ---
    double compute_position_size(double entry_price, double stop_price) const;

    // --- Read accessors ---
    double get_consumed_es()      const { return consumed_es_; }
    double get_remaining_budget() const;
    bool   is_budget_exhausted()  const;
    double get_position_qty()     const { return position_qty_; }
    double get_position_usd()     const;
    double get_unrealized_pnl()   const;

    RiskBudgetSnapshot get_snapshot() const;

    void reset();

    // --- Constants ---
    // φ(z_0.95) / (1 - 0.95) ≈ 2.063 for α = 0.95
    static constexpr double ES_MULTIPLIER_095 = 2.0627;
    // √(1/365) ≈ 0.05234 for 1-day horizon, 365-day year
    static constexpr double SQRT_DT_1DAY = 0.052342;

private:
    double compute_es_for_position(double quantity, double price) const;
    void   recompute_consumed_es();

    RiskConfig cfg_;

    // Budget parameters (set by Tide)
    double es_budget_        = 0.0;
    double max_position_usd_ = 0.0;
    double risk_multiplier_  = 1.0;
    double realized_vol_     = 0.80;   // annualized; default 80% per strategy.md example

    // Position state
    double position_qty_       = 0.0;   // signed: + for long, - for short
    double avg_entry_price_    = 0.0;
    double total_cost_basis_   = 0.0;   // sum of (fill_price * fill_qty) for avg calculation

    // Risk state
    double consumed_es_        = 0.0;

    // Mark-to-market
    double last_price_         = 0.0;
    int64_t last_ts_           = 0;
};

} // namespace orderflow::ripple
