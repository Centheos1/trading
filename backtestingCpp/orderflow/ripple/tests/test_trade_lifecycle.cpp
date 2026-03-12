// Phase 2 — Trade Lifecycle FSM Tests
// Tests every state transition, exit type, scaling rule, trailing stop,
// determinism, and intent generation per strategy.md §12–§14.

#include "../TradeLifecycleEngine.h"
#include "../../Schemas.h"
#include <cassert>
#include <cmath>
#include <iostream>
#include <string>
#include <vector>

using namespace orderflow;
using namespace orderflow::ripple;

static int tests_run = 0;
static int tests_passed = 0;

#define CHECK(cond, msg) do { \
    ++tests_run; \
    if (!(cond)) { \
        std::cerr << "FAIL [" << __LINE__ << "]: " << msg << "\n"; \
    } else { \
        ++tests_passed; \
    } \
} while(0)

// ---------- helpers ----------

static LifecycleConfig make_cfg() {
    LifecycleConfig cfg;
    cfg.confirmation_window_ms = 5000;
    cfg.expand_threshold_sigma = 1.0;
    cfg.max_hold_time_ms       = 300000;
    cfg.cooldown_ms            = 10000;
    cfg.max_scale_ins          = 1;
    cfg.scale_in_threshold_sigma = 2.0;
    cfg.scale_in_cvd_slope_min   = 0.02;
    cfg.trailing_stop_sigma      = 2.0;
    cfg.exhaustion_cvd_slope_thresh = 0.01;
    cfg.exhaustion_impact_ratio    = 1.5;
    cfg.target_distance_sigma      = 3.0;
    cfg.budget_exit_threshold      = 0.90;
    cfg.maturation_momentum_fade   = 0.5;
    return cfg;
}

static TickContext make_tick(int64_t ts, double microprice, double sigma = 1.0) {
    TickContext tc;
    tc.timestamp        = ts;
    tc.microprice       = microprice;
    tc.last_trade_price = microprice;
    tc.cvd_slope        = 0.05;
    tc.trade_rate       = 10.0;
    tc.impact           = 0.001;
    tc.imbalance        = 0.1;
    tc.recent_sigma_P   = sigma;
    return tc;
}

static FillEvent make_fill(int64_t ts, double price, double qty, OrderSide side = OrderSide::BUY) {
    FillEvent f;
    f.timestamp = ts;
    f.trade_id  = "T1";
    f.order_id  = "O1";
    f.symbol    = "BTCUSDT";
    f.side      = side;
    f.price     = price;
    f.quantity  = qty;
    return f;
}

static RiskBudgetSnapshot default_risk() {
    RiskBudgetSnapshot r;
    r.es_budget       = 1000.0;
    r.consumed_es     = 0.0;
    r.risk_multiplier = 1.0;
    r.max_position_usd = 10000.0;
    return r;
}

static PermissionSet default_perms() {
    return PermissionSet{};
}

// ---------- 1. Initial state ----------

static void test_initial_state() {
    TradeLifecycleEngine lce(make_cfg());
    CHECK(lce.is_idle(), "engine starts idle");
    CHECK(!lce.is_active(), "engine not active initially");
    CHECK(!lce.has_pending_intent(), "no pending intent initially");
    CHECK(lce.completed_trades() == 0, "no completed trades initially");
}

// ---------- 2. Setup ----------

static void test_try_setup_when_idle() {
    TradeLifecycleEngine lce(make_cfg());
    bool ok = lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG,
                            100.0, 99.0, 103.0, 1.0, 1000);
    CHECK(ok, "try_setup succeeds when idle");
    CHECK(lce.get_lifecycle_state() == LifecycleState::SETUP, "state is SETUP");
    CHECK(!lce.is_idle(), "not idle after setup");
    CHECK(!lce.is_active(), "not active in SETUP");
}

static void test_try_setup_when_active() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));
    CHECK(lce.is_active(), "active after fill");

    bool ok = lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT,
                            200.0, 201.0, 197.0, 1.0, 2000);
    CHECK(!ok, "try_setup fails when active");
}

static void test_setup_cancelled_on_timeout() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    auto tc = make_tick(7000, 100.0);  // 6s > 5s window
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::CANCELLED, "SETUP → CANCELLED on timeout");
    CHECK(lce.is_idle(), "idle after cancel");
}

// ---------- 3. Entry ----------

static void test_confirm_entry_from_setup() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    bool ok = lce.confirm_entry(1000);
    CHECK(ok, "confirm_entry succeeds from SETUP");
    CHECK(lce.get_lifecycle_state() == LifecycleState::ENTRY, "state is ENTRY");
    CHECK(lce.has_pending_intent(), "entry intent generated");
}

static void test_confirm_entry_not_in_setup() {
    TradeLifecycleEngine lce(make_cfg());
    bool ok = lce.confirm_entry(1000);
    CHECK(!ok, "confirm_entry fails when not in SETUP");
}

static void test_entry_intent_bounce() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    auto intent = lce.consume_pending_intent();
    CHECK(intent.intent_type == IntentType::ENTRY, "intent type is ENTRY");
    CHECK(intent.side == OrderSide::BUY, "side is BUY for LONG bounce");
    CHECK(intent.order_type == OrderType::LIMIT, "bounce uses LIMIT order");
    CHECK(intent.quantity == 1.0, "quantity matches");
}

static void test_entry_intent_breakout() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 200.0, 201.0, 197.0, 2.0, 1000);
    lce.confirm_entry(1000);
    auto intent = lce.consume_pending_intent();
    CHECK(intent.order_type == OrderType::MARKET, "breakout uses MARKET order");
    CHECK(intent.side == OrderSide::SELL, "side is SELL for SHORT breakout");
    CHECK(intent.quantity == 2.0, "quantity matches");
}

// ---------- 4. Fill → Confirmation ----------

static void test_fill_in_entry_to_confirmation() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();
    lce.on_fill(make_fill(1001, 100.05, 1.0));
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION, "ENTRY → CONFIRMATION on fill");
    CHECK(lce.is_active(), "active after fill");
}

static void test_fill_sets_entry_price() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.05, 1.0));
    auto snap = lce.get_snapshot();
    CHECK(std::abs(snap.entry_price - 100.05) < 1e-9, "entry price matches fill price");
    CHECK(std::abs(snap.quantity - 1.0) < 1e-9, "filled quantity correct");
}

// ---------- 5. Confirmation → Expansion ----------

static void test_expansion_on_favorable_move() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // sigma=1.0, expand_threshold=1.0 → need microprice > 101.0
    auto tc = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "CONFIRMATION → EXPANSION");
}

static void test_no_expansion_below_threshold() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 100.5, 1.0);  // 0.5 σ < 1.0 threshold
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION, "stays in CONFIRMATION");
}

// ---------- 6. Expansion → Maturation ----------

static void test_maturation_near_target() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 106.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Get to EXPANSION first
    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "in EXPANSION");

    // target=106, entry=100, total_distance=6, maturation_fade=0.5
    // MATURATION when distance_remaining < 6*0.5 = 3 → price > 103
    auto tc2 = make_tick(3000, 104.0, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::MATURATION, "EXPANSION → MATURATION");
}

// ---------- 7. Exit types ----------

static void test_invalidation_exit_long() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 98.5, 1.0);
    tc.last_trade_price = 98.5;  // below stop of 99.0
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "invalidation → EXIT for long");
    CHECK(lce.get_last_exit_type() == ExitType::INVALIDATION, "exit type is INVALIDATION");
    CHECK(lce.has_pending_intent(), "exit intent generated");

    auto intent = lce.consume_pending_intent();
    CHECK(intent.urgency == Urgency::IMMEDIATE, "invalidation exit is IMMEDIATE");
    CHECK(intent.order_type == OrderType::MARKET, "invalidation exit uses MARKET");
    CHECK(intent.exit_reason == ExitReason::INVALIDATION, "exit reason correct");
}

static void test_invalidation_exit_short() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 200.0, 201.0, 197.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 200.0, 1.0, OrderSide::SELL));

    auto tc = make_tick(2000, 201.5, 1.0);
    tc.last_trade_price = 201.5;  // above stop of 201.0
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "invalidation → EXIT for short");
}

static void test_time_exit() {
    auto cfg = make_cfg();
    cfg.max_hold_time_ms = 10000;  // 10 seconds
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(12000, 100.5, 1.0);  // 11s > 10s limit
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "time → EXIT");
    CHECK(lce.get_last_exit_type() == ExitType::TIME, "exit type is TIME");
}

static void test_target_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 103.5, 1.0);  // above target 103.0
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "target → EXIT");
    CHECK(lce.get_last_exit_type() == ExitType::TARGET, "exit type is TARGET");
}

static void test_exhaustion_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 105.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // First tick to set entry baselines
    auto tc1 = make_tick(2000, 100.5, 1.0);
    tc1.trade_rate = 20.0;
    tc1.impact     = 0.001;
    lce.on_tick(tc1, default_risk(), default_perms());

    // Exhaustion: cvd fading, rate declining, impact rising
    auto tc2 = make_tick(3000, 101.0, 1.0);
    tc2.cvd_slope  = -0.05;    // adverse for LONG
    tc2.trade_rate = 5.0;      // < 50% of 20.0
    tc2.impact     = 0.002;    // > 1.5 × 0.001
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "exhaustion → EXIT");
    CHECK(lce.get_last_exit_type() == ExitType::EXHAUSTION, "exit type is EXHAUSTION");
}

static void test_risk_budget_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto risk = default_risk();
    risk.consumed_es = 950.0;  // 95% of 1000 > 90% threshold
    auto tc = make_tick(2000, 100.5, 1.0);
    lce.on_tick(tc, risk, default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "risk-budget → EXIT");
    CHECK(lce.get_last_exit_type() == ExitType::RISK_BUDGET, "exit type is RISK_BUDGET");
}

static void test_risk_multiplier_zero_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto risk = default_risk();
    risk.risk_multiplier = 0.0;
    auto tc = make_tick(2000, 100.5, 1.0);
    lce.on_tick(tc, risk, default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "risk_multiplier=0 → EXIT");
}

static void test_exit_priority_risk_over_invalidation() {
    // Risk-budget should fire before invalidation even when both conditions hold
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto risk = default_risk();
    risk.consumed_es = 950.0;
    auto tc = make_tick(2000, 98.5, 1.0);  // also below stop
    tc.last_trade_price = 98.5;
    lce.on_tick(tc, risk, default_perms());
    CHECK(lce.get_last_exit_type() == ExitType::RISK_BUDGET,
          "risk-budget has higher priority than invalidation");
}

// ---------- 8. Exit flow ----------

static void test_exit_to_cooldown() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Trigger exit
    auto tc = make_tick(2000, 103.5, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "in EXIT");

    // Fill the exit order
    lce.on_fill(make_fill(2001, 103.5, 1.0, OrderSide::SELL));
    CHECK(lce.get_lifecycle_state() == LifecycleState::COOLDOWN, "EXIT → COOLDOWN after fill");
    CHECK(lce.completed_trades() == 1, "completed_trades incremented");
}

static void test_cooldown_to_idle() {
    auto cfg = make_cfg();
    cfg.cooldown_ms = 5000;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Target exit
    auto tc1 = make_tick(2000, 103.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    lce.on_fill(make_fill(2001, 103.5, 1.0, OrderSide::SELL));
    CHECK(lce.get_lifecycle_state() == LifecycleState::COOLDOWN, "in COOLDOWN");
    CHECK(!lce.is_idle(), "not idle during cooldown");

    // Wait for cooldown (fill at 2001 + 5000 = 7001)
    auto tc2 = make_tick(7002, 100.0, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.is_idle(), "idle after cooldown expires");
}

// ---------- 9. Scale-in ----------

static void test_scale_in_in_expansion() {
    auto cfg = make_cfg();
    cfg.scale_in_threshold_sigma = 1.5;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 106.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Move to EXPANSION (sigma=1.0, expand_threshold=1.0 → price > 101)
    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "in EXPANSION");

    // Scale-in: favorable move > 1.5σ (1.5) → price > 101.5
    auto tc2 = make_tick(3000, 102.0, 1.0);
    tc2.cvd_slope = 0.05;  // > scale_in_cvd_slope_min (0.02)
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.has_pending_intent(), "scale-in intent generated");

    auto intent = lce.consume_pending_intent();
    CHECK(intent.intent_type == IntentType::SCALE_IN, "intent is SCALE_IN");
    CHECK(intent.quantity == 1.0, "scale-in qty matches initial");
}

static void test_no_scale_in_max_reached() {
    auto cfg = make_cfg();
    cfg.max_scale_ins = 1;
    cfg.scale_in_threshold_sigma = 1.5;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc1 = make_tick(2000, 102.0, 1.0);
    tc1.cvd_slope = 0.05;
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.has_pending_intent(), "first scale-in ok");
    lce.consume_pending_intent();
    lce.on_fill(make_fill(2001, 102.0, 1.0));

    // Second scale-in attempt: max_scale_ins=1, already at 1
    auto tc2 = make_tick(4000, 104.0, 1.0);
    tc2.cvd_slope = 0.05;
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(!lce.has_pending_intent(), "no second scale-in when max reached");
}

static void test_scale_in_moves_stop_to_breakeven() {
    auto cfg = make_cfg();
    cfg.scale_in_threshold_sigma = 1.5;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));
    CHECK(std::abs(lce.get_stop_price() - 99.0) < 1e-9, "stop at 99 before scale-in");

    auto tc = make_tick(2000, 102.0, 1.0);
    tc.cvd_slope = 0.05;
    lce.on_tick(tc, default_risk(), default_perms());
    lce.consume_pending_intent();
    lce.on_fill(make_fill(2001, 102.0, 1.0));
    CHECK(std::abs(lce.get_stop_price() - 100.0) < 1e-9,
          "stop moves to breakeven after scale-in fill");
}

// ---------- 10. Trailing stop ----------

static void test_trailing_stop_only_tightens_long() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Move through EXPANSION to MATURATION (activates trailing stop)
    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());  // → EXPANSION

    auto tc2 = make_tick(3000, 107.0, 1.0);  // near target
    lce.on_tick(tc2, default_risk(), default_perms());  // → MATURATION
    CHECK(lce.get_lifecycle_state() == LifecycleState::MATURATION, "in MATURATION");

    double stop_after_107 = lce.get_stop_price();

    // Price drops → stop should NOT loosen
    auto tc3 = make_tick(4000, 105.0, 1.0);
    tc3.last_trade_price = 105.0;
    lce.on_tick(tc3, default_risk(), default_perms());
    CHECK(lce.get_stop_price() >= stop_after_107, "trailing stop only tightens for long");
}

static void test_trailing_stop_only_tightens_short() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 100.0, 101.0, 90.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0, OrderSide::SELL));

    auto tc1 = make_tick(2000, 98.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());  // → EXPANSION

    auto tc2 = make_tick(3000, 93.0, 1.0);  // near target=90, within maturation zone
    lce.on_tick(tc2, default_risk(), default_perms());  // → MATURATION
    CHECK(lce.get_lifecycle_state() == LifecycleState::MATURATION, "short in MATURATION");

    double stop_after = lce.get_stop_price();

    // Price rises → stop should NOT loosen (for short, stop should only decrease)
    auto tc3 = make_tick(4000, 95.0, 1.0);
    tc3.last_trade_price = 95.0;
    lce.on_tick(tc3, default_risk(), default_perms());
    CHECK(lce.get_stop_price() <= stop_after, "trailing stop only tightens for short");
}

// ---------- 11. Permission revocation ----------

static void test_permission_revoked_during_trade() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    PermissionSet perms;
    perms.long_bounce = PermissionLevel::DISABLED;
    auto tc = make_tick(2000, 100.5, 1.0);
    lce.on_tick(tc, default_risk(), perms);
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT,
          "permission revoked → EXIT");
}

// ---------- 12. External exit signal ----------

static void test_external_exit_signal() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    lce.on_exit_signal(ExitType::INVALIDATION, 2000);
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "external signal → EXIT");
    CHECK(lce.has_pending_intent(), "exit intent generated");
}

static void test_external_exit_signal_not_active() {
    TradeLifecycleEngine lce(make_cfg());
    lce.on_exit_signal(ExitType::INVALIDATION, 2000);
    CHECK(lce.is_idle(), "exit signal ignored when idle");
}

// ---------- 13. Snapshot ----------

static void test_snapshot_fields() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 200.0, 201.0, 197.0, 2.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 200.0, 2.0, OrderSide::SELL));

    auto snap = lce.get_snapshot();
    CHECK(snap.archetype == TradeArchetype::BREAKOUT, "snapshot archetype");
    CHECK(snap.side == TradeSide::SHORT, "snapshot side");
    CHECK(snap.state == LifecycleState::CONFIRMATION, "snapshot state");
    CHECK(std::abs(snap.entry_price - 200.0) < 1e-9, "snapshot entry_price");
    CHECK(std::abs(snap.stop_price - 201.0) < 1e-9, "snapshot stop_price");
    CHECK(std::abs(snap.target_price - 197.0) < 1e-9, "snapshot target_price");
    CHECK(std::abs(snap.quantity - 2.0) < 1e-9, "snapshot quantity");
    CHECK(snap.scale_count == 0, "snapshot scale_count");
}

static void test_snapshot_hold_time() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(6001, 100.5, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    auto snap = lce.get_snapshot();
    CHECK(snap.hold_time_ms == 5000, "hold_time_ms = last_tick_ts - entry_ts");
}

// ---------- 14. Determinism ----------

static void test_determinism() {
    auto run_scenario = [&]() -> TradeStateSnapshot {
        TradeLifecycleEngine lce(make_cfg());
        lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
        lce.confirm_entry(1000);
        lce.on_fill(make_fill(1001, 100.0, 1.0));

        for (int i = 0; i < 10; ++i) {
            auto tc = make_tick(2000 + i * 500, 100.0 + i * 0.1, 1.0);
            lce.on_tick(tc, default_risk(), default_perms());
        }
        return lce.get_snapshot();
    };

    auto snap1 = run_scenario();
    auto snap2 = run_scenario();
    CHECK(snap1.state == snap2.state, "determinism: same state");
    CHECK(std::abs(snap1.entry_price - snap2.entry_price) < 1e-15, "determinism: same entry_price");
    CHECK(snap1.hold_time_ms == snap2.hold_time_ms, "determinism: same hold_time");
    CHECK(snap1.scale_count == snap2.scale_count, "determinism: same scale_count");
}

// ---------- 15. Reset ----------

static void test_reset() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));
    CHECK(lce.is_active(), "active before reset");

    lce.reset();
    CHECK(lce.is_idle(), "idle after reset");
    CHECK(!lce.has_pending_intent(), "no pending intent after reset");
    CHECK(lce.completed_trades() == 0, "completed_trades reset");
}

// ---------- 16. New setup after cooldown ----------

static void test_new_setup_after_cooldown() {
    auto cfg = make_cfg();
    cfg.cooldown_ms = 5000;
    TradeLifecycleEngine lce(cfg);

    // First trade
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));
    auto tc1 = make_tick(2000, 103.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    lce.on_fill(make_fill(2001, 103.5, 1.0, OrderSide::SELL));

    // Still in cooldown
    auto tc2 = make_tick(6000, 100.0, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());
    bool ok1 = lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT,
                             200.0, 201.0, 197.0, 1.0, 6000);
    CHECK(!ok1, "cannot setup during cooldown");

    // After cooldown
    auto tc3 = make_tick(8000, 100.0, 1.0);
    lce.on_tick(tc3, default_risk(), default_perms());
    bool ok2 = lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT,
                             200.0, 201.0, 197.0, 1.0, 8000);
    CHECK(ok2, "setup succeeds after cooldown");
    CHECK(lce.get_archetype() == TradeArchetype::BREAKOUT, "new archetype");
}

// ---------- 17. Entry cancelled before fill ----------

static void test_entry_cancelled_on_timeout() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    // No fill arrives
    auto tc = make_tick(7000, 100.0, 1.0);  // > confirmation_window (5s)
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::CANCELLED, "ENTRY → CANCELLED on timeout");
    CHECK(lce.is_idle(), "idle after entry cancel");
}

// ---------- 18. Exit intent fields ----------

static void test_exit_intent_target() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 103.5, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    auto intent = lce.consume_pending_intent();
    CHECK(intent.exit_reason == ExitReason::TARGET, "target exit reason");
    CHECK(intent.urgency == Urgency::NORMAL, "target exit urgency is NORMAL");
    CHECK(intent.side == OrderSide::SELL, "exit side is SELL for LONG");
    CHECK(intent.order_type == OrderType::LIMIT, "target exit uses LIMIT per §13.3.2");
    CHECK(std::abs(intent.limit_price - 103.0) < 1e-9, "target exit limit_price = target_price");
}

static void test_exit_intent_risk_budget() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto risk = default_risk();
    risk.consumed_es = 950.0;
    auto tc = make_tick(2000, 100.5, 1.0);
    lce.on_tick(tc, risk, default_perms());
    auto intent = lce.consume_pending_intent();
    CHECK(intent.exit_reason == ExitReason::RISK_BUDGET, "risk-budget exit reason");
    CHECK(intent.urgency == Urgency::IMMEDIATE, "risk-budget exit is IMMEDIATE");
}

// ---------- 19. Double exit protection ----------

static void test_no_double_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // First exit signal
    lce.on_exit_signal(ExitType::INVALIDATION, 2000);
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "first exit works");
    lce.consume_pending_intent();

    // Second exit signal should be ignored
    lce.on_exit_signal(ExitType::TARGET, 2001);
    CHECK(!lce.has_pending_intent(), "second exit ignored");
}

// ---------- 20. Scale-in only in EXPANSION ----------

static void test_no_scale_in_in_confirmation() {
    auto cfg = make_cfg();
    cfg.scale_in_threshold_sigma = 1.5;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();  // consume ENTRY intent
    lce.on_fill(make_fill(1001, 100.0, 1.0));
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION, "in CONFIRMATION");

    // Tick with favorable move but we're NOT in EXPANSION
    auto tc = make_tick(2000, 100.3, 1.0);  // below expand threshold
    tc.cvd_slope = 0.05;
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(!lce.has_pending_intent(), "no scale-in intent in CONFIRMATION");
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION, "still in CONFIRMATION");
}

// ---------- 21. Scale-in rejected when CVD too low ----------

static void test_scale_in_rejected_low_cvd() {
    auto cfg = make_cfg();
    cfg.scale_in_threshold_sigma = 1.5;
    cfg.scale_in_cvd_slope_min   = 0.02;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();  // consume ENTRY intent
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // 101.3: expand_sigma=1.3 > 1.0 (EXPANSION), but < 1.5 (no scale-in on this tick)
    auto tc1 = make_tick(2000, 101.3, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "in EXPANSION");

    // Now favorable move is sufficient (102.5-100=2.5 > 1.5) but CVD too low
    auto tc2 = make_tick(3000, 102.5, 1.0);
    tc2.cvd_slope = 0.01;  // below min 0.02
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(!lce.has_pending_intent(), "no scale-in when CVD slope below threshold");
}

// ---------- 22. Exhaustion requires ALL three conditions ----------

static void test_exhaustion_partial_no_exit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 105.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // Set baselines
    auto tc1 = make_tick(2000, 100.5, 1.0);
    tc1.trade_rate = 20.0;
    tc1.impact     = 0.001;
    lce.on_tick(tc1, default_risk(), default_perms());

    // Only 2 of 3 conditions: CVD fading + rate declining, but impact NOT rising
    auto tc2 = make_tick(3000, 101.0, 1.0);
    tc2.cvd_slope  = -0.05;
    tc2.trade_rate = 5.0;
    tc2.impact     = 0.001;   // same as entry — not > 1.5× entry
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() != LifecycleState::EXIT,
          "no exit when only 2 of 3 exhaustion conditions hold");

    // Only 1 of 3 conditions: impact rising only
    auto tc3 = make_tick(4000, 101.0, 1.0);
    tc3.cvd_slope  = 0.05;   // favorable, not fading
    tc3.trade_rate = 20.0;   // not declining
    tc3.impact     = 0.002;  // rising but alone insufficient
    lce.on_tick(tc3, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() != LifecycleState::EXIT,
          "no exit when only 1 of 3 exhaustion conditions holds");
}

// ---------- 23. Exit from EXPANSION ----------

static void test_exit_from_expansion() {
    auto cfg = make_cfg();
    cfg.max_hold_time_ms = 10000;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "in EXPANSION");

    // Time exit from EXPANSION
    auto tc2 = make_tick(12000, 101.5, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "time exit from EXPANSION");
    CHECK(lce.get_last_exit_type() == ExitType::TIME, "exit type is TIME");
}

// ---------- 24. Exit from MATURATION ----------

static void test_exit_from_maturation() {
    auto cfg = make_cfg();
    cfg.max_hold_time_ms = 10000;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 106.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());  // → EXPANSION

    auto tc2 = make_tick(3000, 104.0, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());  // → MATURATION
    CHECK(lce.get_lifecycle_state() == LifecycleState::MATURATION, "in MATURATION");

    // Time exit from MATURATION
    auto tc3 = make_tick(12000, 104.0, 1.0);
    lce.on_tick(tc3, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "time exit from MATURATION");
}

// ---------- 25. Unrealized PnL ----------

static void test_snapshot_unrealized_pnl() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 102.0, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    auto snap = lce.get_snapshot();
    // PnL = (102 - 100) * 1.0 * 1.0 = 2.0
    CHECK(std::abs(snap.unrealized_pnl - 2.0) < 1e-9, "unrealized_pnl for LONG");
}

static void test_snapshot_unrealized_pnl_short() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 100.0, 101.0, 90.0, 2.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 2.0, OrderSide::SELL));

    auto tc = make_tick(2000, 97.0, 1.0);
    lce.on_tick(tc, default_risk(), default_perms());
    auto snap = lce.get_snapshot();
    // PnL = (97 - 100) * (-1) * 2.0 = 6.0
    CHECK(std::abs(snap.unrealized_pnl - 6.0) < 1e-9, "unrealized_pnl for SHORT");
}

// ---------- 26. Exhaustion exit uses LIMIT ----------

static void test_exhaustion_exit_uses_limit() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 105.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc1 = make_tick(2000, 100.5, 1.0);
    tc1.trade_rate = 20.0;
    tc1.impact     = 0.001;
    lce.on_tick(tc1, default_risk(), default_perms());

    auto tc2 = make_tick(3000, 101.0, 1.0);
    tc2.cvd_slope  = -0.05;
    tc2.trade_rate = 5.0;
    tc2.impact     = 0.002;
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT, "exhaustion → EXIT");
    auto intent = lce.consume_pending_intent();
    CHECK(intent.order_type == OrderType::LIMIT, "exhaustion exit uses LIMIT per §13.3.3");
    CHECK(std::abs(intent.limit_price - 101.0) < 1e-9,
          "exhaustion exit limit_price = last microprice");
}

// ---------- 27. MATURATION → EXPANSION re-acceleration ----------

static void test_maturation_to_expansion_reacceleration() {
    TradeLifecycleEngine lce(make_cfg());
    // Wide target range so trailing stop doesn't interfere with pullback
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 110.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    // CONFIRMATION → EXPANSION
    auto tc1 = make_tick(2000, 101.5, 1.0);
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "in EXPANSION");

    // EXPANSION → MATURATION (target=110, entry=100, total=10, fade=0.5 → need price>105)
    auto tc2 = make_tick(3000, 106.0, 1.0);
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::MATURATION, "in MATURATION");
    // Trailing stop at MATURATION: trail=2σ=2 → stop = max(99, 106-2) = 104

    // Price pulls back above the trailing stop but outside maturation zone
    // distance_remaining = (110 - 104.5) = 5.5, total=10, 5.5 > 10*0.5=5 → not maturation
    // last_trade_price = 104.5 > stop=104 → no invalidation
    auto tc3 = make_tick(4000, 104.5, 1.0);
    tc3.last_trade_price = 104.5;
    lce.on_tick(tc3, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION,
          "MATURATION → EXPANSION on re-acceleration (§12.2)");
}

// ---------- 28. Scale-in intent side for SHORT ----------

static void test_scale_in_intent_short() {
    auto cfg = make_cfg();
    cfg.scale_in_threshold_sigma = 1.5;
    TradeLifecycleEngine lce(cfg);
    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 100.0, 101.0, 90.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0, OrderSide::SELL));

    auto tc1 = make_tick(2000, 98.5, 1.0);
    tc1.cvd_slope = -0.05;  // favorable for SHORT
    lce.on_tick(tc1, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION, "SHORT in EXPANSION");

    auto tc2 = make_tick(3000, 97.5, 1.0);
    tc2.cvd_slope = -0.05;
    lce.on_tick(tc2, default_risk(), default_perms());
    CHECK(lce.has_pending_intent(), "scale-in intent for SHORT");
    auto intent = lce.consume_pending_intent();
    CHECK(intent.side == OrderSide::SELL, "scale-in intent side is SELL for SHORT");
    CHECK(intent.intent_type == IntentType::SCALE_IN, "intent type is SCALE_IN");
}

// ---------- 29. Invalidation exit uses MARKET ----------

static void test_invalidation_exit_uses_market() {
    TradeLifecycleEngine lce(make_cfg());
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 100.0, 99.0, 103.0, 1.0, 1000);
    lce.confirm_entry(1000);
    lce.on_fill(make_fill(1001, 100.0, 1.0));

    auto tc = make_tick(2000, 98.5, 1.0);
    tc.last_trade_price = 98.5;
    lce.on_tick(tc, default_risk(), default_perms());
    auto intent = lce.consume_pending_intent();
    CHECK(intent.order_type == OrderType::MARKET, "invalidation exit uses MARKET per §13.3.1");
}

// ========== main ==========

int main() {
    std::cout << "=== Phase 2: Trade Lifecycle FSM Tests ===\n\n";

    test_initial_state();
    test_try_setup_when_idle();
    test_try_setup_when_active();
    test_setup_cancelled_on_timeout();
    test_confirm_entry_from_setup();
    test_confirm_entry_not_in_setup();
    test_entry_intent_bounce();
    test_entry_intent_breakout();
    test_fill_in_entry_to_confirmation();
    test_fill_sets_entry_price();
    test_expansion_on_favorable_move();
    test_no_expansion_below_threshold();
    test_maturation_near_target();
    test_invalidation_exit_long();
    test_invalidation_exit_short();
    test_time_exit();
    test_target_exit();
    test_exhaustion_exit();
    test_risk_budget_exit();
    test_risk_multiplier_zero_exit();
    test_exit_priority_risk_over_invalidation();
    test_exit_to_cooldown();
    test_cooldown_to_idle();
    test_scale_in_in_expansion();
    test_no_scale_in_max_reached();
    test_scale_in_moves_stop_to_breakeven();
    test_trailing_stop_only_tightens_long();
    test_trailing_stop_only_tightens_short();
    test_permission_revoked_during_trade();
    test_external_exit_signal();
    test_external_exit_signal_not_active();
    test_snapshot_fields();
    test_snapshot_hold_time();
    test_determinism();
    test_reset();
    test_new_setup_after_cooldown();
    test_entry_cancelled_on_timeout();
    test_exit_intent_target();
    test_exit_intent_risk_budget();
    test_no_double_exit();
    test_no_scale_in_in_confirmation();
    test_scale_in_rejected_low_cvd();
    test_exhaustion_partial_no_exit();
    test_exit_from_expansion();
    test_exit_from_maturation();
    test_snapshot_unrealized_pnl();
    test_snapshot_unrealized_pnl_short();
    test_exhaustion_exit_uses_limit();
    test_maturation_to_expansion_reacceleration();
    test_scale_in_intent_short();
    test_invalidation_exit_uses_market();

    std::cout << "\n" << tests_passed << " / " << tests_run << " tests passed.\n";
    return (tests_passed == tests_run) ? 0 : 1;
}
