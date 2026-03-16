#pragma once

#include <cstdint>
#include <string>
#include "../Schemas.h"
#include "RippleConfig.h"

namespace orderflow::ripple {

// Lightweight per-tick market state passed to the lifecycle engine.
// Decouples the lifecycle from full RippleFeatures/RippleContext.
struct TickContext {
    int64_t timestamp          = 0;
    double  microprice         = 0.0;
    double  last_trade_price   = 0.0;
    double  cvd_slope          = 0.0;     // signed: +buy / -sell
    double  trade_rate         = 0.0;     // trades per second
    double  impact             = 0.0;     // impact per unit volume
    double  imbalance          = 0.0;     // top-1 book imbalance
    double  recent_sigma_P     = 0.0;     // recent price volatility
};

/// Phase 3: scale-out target level (§14.2.1).
struct ScaleOutTarget {
    double price    = 0.0;
    double quantity = 0.0;
    double new_stop = 0.0;   // stop tightens to this after fill
    bool   filled   = false;
};

/// Manages the lifecycle of a single trade from SETUP through COOLDOWN.
///
/// Implements the FSM from strategy.md §12:
///   SETUP → ENTRY → CONFIRMATION → EXPANSION → MATURATION → EXIT → COOLDOWN
///   SETUP → CANCELLED (timeout / permission revoked)
///
/// V1 constraints (§12.3, §22.1):
///   - At most one trade active per symbol
///   - Event-time only (no wall-clock in decision logic)
///   - Single symbol
class TradeLifecycleEngine {
public:
    explicit TradeLifecycleEngine(const LifecycleConfig& cfg = {});

    /// Begin a new trade setup. Returns false if not idle.
    bool try_setup(TradeArchetype archetype, TradeSide side,
                   double wall_price, double stop_price, double target_price,
                   double quantity, int64_t timestamp);

    /// Confirm entry (trigger conditions met). SETUP → ENTRY.
    bool confirm_entry(int64_t timestamp);

    /// Process a fill. ENTRY → CONFIRMATION, scale-in, or scale-out fill.
    void on_fill(const FillEvent& fill);

    /// Per-tick update: exit checks, state advancement, trailing stop, scale-out.
    void on_tick(const TickContext& ctx,
                 const RiskBudgetSnapshot& risk,
                 const PermissionSet& permissions);

    /// External exit signal (e.g. wall broke, from trigger engine).
    void on_exit_signal(ExitType type, int64_t timestamp);

    /// Phase 3: set scale-out targets derived from the liquidity map (§14.2.1).
    /// Must be called after try_setup, before or after confirm_entry.
    void set_scale_out_targets(const ScaleOutTarget* targets, int count);

    // --- read accessors ---
    LifecycleState     get_lifecycle_state()  const { return state_; }
    TradeArchetype     get_archetype()        const { return archetype_; }
    TradeSide          get_side()             const { return side_; }
    ExitType           get_last_exit_type()   const { return last_exit_type_; }
    double             get_stop_price()       const { return stop_price_; }
    double             get_target_price()     const { return target_price_; }
    int32_t            get_scale_out_idx()    const { return scale_out_idx_; }
    int32_t            get_scale_out_target_count() const { return scale_out_target_count_; }

    TradeStateSnapshot get_snapshot()         const;
    bool               is_active()            const;
    bool               is_idle()              const;
    bool               has_pending_intent()   const { return has_intent_; }
    StrategyExecutionIntent consume_pending_intent();

    uint64_t completed_trades() const { return completed_trades_; }
    double   cumulative_pnl()  const { return cumulative_pnl_; }
    double   peak_equity()     const { return peak_equity_; }
    double   max_drawdown_value() const { return max_drawdown_; }

    void reset();

private:
    void transition_to(LifecycleState new_state, int64_t timestamp);
    void begin_exit(ExitType type, int64_t timestamp);

    // Intent generators (no heap allocation — reuse pending_intent_ member)
    void emit_entry_intent(int64_t timestamp);
    void emit_exit_intent(ExitType type, int64_t timestamp);
    void emit_scale_in_intent(int64_t timestamp);
    void emit_scale_out_intent(int64_t timestamp);

    // Exit checks — evaluated in priority order per §13.1
    bool check_risk_budget_exit(const RiskBudgetSnapshot& risk) const;
    bool check_invalidation_exit(double last_trade_price) const;
    bool check_time_exit(int64_t timestamp) const;
    bool check_target_exit(double microprice) const;
    bool check_exhaustion_exit(const TickContext& ctx) const;

    // State advancement
    bool check_expansion(const TickContext& ctx) const;
    bool check_maturation(const TickContext& ctx) const;
    bool check_scale_in(const TickContext& ctx) const;
    bool check_scale_out(const TickContext& ctx) const;
    void update_trailing_stop(const TickContext& ctx);

    double side_sign() const;
    std::string make_trade_id();

    LifecycleConfig cfg_;

    // Trade identity
    LifecycleState state_     = LifecycleState::CANCELLED;
    bool           has_trade_  = false;
    TradeArchetype archetype_  = TradeArchetype::BOUNCE;
    TradeSide      side_       = TradeSide::LONG;
    std::string    trade_id_;

    // Price levels
    double wall_price_   = 0.0;
    double entry_price_  = 0.0;
    double stop_price_   = 0.0;
    double target_price_ = 0.0;

    // Position
    double quantity_          = 0.0;
    double initial_quantity_  = 0.0;
    double filled_quantity_   = 0.0;
    double avg_fill_price_    = 0.0;

    // Timestamps
    int64_t setup_ts_        = 0;
    int64_t entry_ts_        = 0;
    int64_t confirmation_ts_ = 0;
    int64_t last_tick_ts_    = 0;
    int64_t cooldown_end_    = 0;

    // Scaling
    int32_t scale_count_          = 0;
    int32_t scale_out_count_      = 0;
    bool    trailing_stop_active_ = false;

    // Phase 3: scale-out plan (§14.2.1)
    static constexpr int MAX_SCALE_OUT_TARGETS = LifecycleConfig::MAX_SCALE_OUT;
    ScaleOutTarget scale_out_targets_[MAX_SCALE_OUT_TARGETS] = {};
    int32_t        scale_out_target_count_ = 0;
    int32_t        scale_out_idx_          = 0;
    bool           scale_out_pending_      = false;

    // Entry-time snapshots (for exhaustion comparison)
    double entry_trade_rate_ = 0.0;
    double entry_impact_     = 0.0;
    double last_microprice_  = 0.0;      // for unrealized PnL and exhaustion limit

    // Pending intent
    bool                    has_intent_ = false;
    StrategyExecutionIntent pending_intent_;

    ExitType last_exit_type_   = ExitType::INVALIDATION;
    uint64_t trade_counter_    = 0;
    uint64_t completed_trades_ = 0;

    // PnL tracking (Phase 6)
    double cumulative_pnl_ = 0.0;
    double peak_equity_    = 0.0;
    double max_drawdown_   = 0.0;
};

} // namespace orderflow::ripple
