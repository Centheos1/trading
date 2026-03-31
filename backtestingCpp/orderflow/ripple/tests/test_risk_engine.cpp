#include <cassert>
#include <cmath>
#include <iostream>
#include <string>
#include "../../Schemas.h"
#include "../RiskEngine.h"

using namespace orderflow;
using namespace orderflow::ripple;

static int tests_run = 0;
static int tests_passed = 0;

#define CHECK(cond, msg)                                           \
    do {                                                           \
        tests_run++;                                               \
        if (!(cond)) {                                             \
            std::cerr << "FAIL: " << msg << " (" << __FILE__      \
                      << ":" << __LINE__ << ")" << std::endl;      \
        } else {                                                   \
            tests_passed++;                                        \
        }                                                          \
    } while (0)

#define CHECK_NEAR(a, b, tol, msg)                                 \
    CHECK(std::abs((a) - (b)) < (tol), msg << " (got " << (a) << ", expected " << (b) << ")")

static FillEvent make_fill(int64_t ts, OrderSide side, double price, double qty) {
    FillEvent f;
    f.timestamp = ts;
    f.side      = side;
    f.price     = price;
    f.quantity  = qty;
    return f;
}

// ===================================================================
//  1. Construction and defaults
// ===================================================================

void test_default_construction() {
    RiskEngine re;
    CHECK(re.get_consumed_es() == 0.0, "initial consumed_es should be 0");
    CHECK(re.get_position_qty() == 0.0, "initial position should be 0");
    CHECK(re.get_remaining_budget() == 0.0, "no budget set → remaining = 0");
    CHECK(re.is_budget_exhausted(), "no budget → exhausted");
}

void test_set_budget() {
    RiskEngine re;
    re.set_budget(1000.0, 10000.0, 0.8);
    auto snap = re.get_snapshot();
    CHECK_NEAR(snap.es_budget, 1000.0, 1e-10, "es_budget");
    CHECK_NEAR(snap.max_position_usd, 10000.0, 1e-10, "max_position_usd");
    CHECK_NEAR(snap.risk_multiplier, 0.8, 1e-10, "risk_multiplier");
    CHECK(!re.is_budget_exhausted(), "budget not exhausted");
    CHECK_NEAR(re.get_remaining_budget(), 1000.0, 1e-10, "remaining budget");
}

void test_set_budget_clamps() {
    RiskEngine re;
    re.set_budget(-100.0, -50.0, 2.5);
    CHECK(re.get_snapshot().es_budget == 0.0, "negative es_budget clamped to 0");
    CHECK(re.get_snapshot().max_position_usd == 0.0, "negative max_pos clamped to 0");
    CHECK(re.get_snapshot().risk_multiplier == 1.0, "risk_mult clamped to [0,1]");
}

void test_set_volatility() {
    RiskEngine re;
    re.set_volatility(0.5);
    re.set_budget(1000.0, 10000.0, 1.0);
    double sz = re.compute_position_size(100.0, 95.0);
    CHECK(sz > 0.0, "position size should be positive with valid volatility");
}

// ===================================================================
//  2. ES computation
// ===================================================================

void test_es_computation_example() {
    // strategy.md §15.2 example: notional=100, vol=0.80
    // ES ≈ 100 * 0.80 * 0.0523 * 2.063 ≈ 8.63
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    auto fill = make_fill(1000, OrderSide::BUY, 100.0, 1.0);
    re.on_fill(fill);

    double es = re.get_consumed_es();
    // Q=1, P=100 → notional=100, vol=0.80
    double expected = 1.0 * 100.0 * 0.80 * RiskEngine::SQRT_DT_1DAY * RiskEngine::ES_MULTIPLIER_095;
    CHECK_NEAR(es, expected, 0.01, "ES matches parametric formula");
    CHECK_NEAR(es, 8.63, 0.1, "ES near strategy.md example value of ~8.63");
}

void test_es_updates_on_price_change() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 2.0));

    double es1 = re.get_consumed_es();
    re.on_price_update(110.0, 2000);
    double es2 = re.get_consumed_es();

    CHECK(es2 > es1, "ES should increase when price increases (larger notional)");
}

void test_es_zero_with_no_position() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.on_price_update(100.0, 1000);
    CHECK(re.get_consumed_es() == 0.0, "no position → no consumed ES");
}

// ===================================================================
//  3. check_new_order
// ===================================================================

void test_check_new_order_within_budget() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    CHECK(re.check_new_order(1.0, 100.0), "small order within budget");
}

void test_check_new_order_exceeds_es_budget() {
    RiskEngine re;
    re.set_budget(5.0, 1000000.0, 1.0);   // tiny ES budget
    re.set_volatility(0.80);
    CHECK(!re.check_new_order(100.0, 1000.0), "large order exceeds ES budget");
}

void test_check_new_order_exceeds_notional_cap() {
    RiskEngine re;
    re.set_budget(100000.0, 100.0, 1.0);   // generous ES, small notional cap
    re.set_volatility(0.80);
    CHECK(!re.check_new_order(2.0, 100.0), "order exceeds max_position_usd");
    CHECK(re.check_new_order(0.5, 100.0), "smaller order fits notional cap");
}

void test_check_new_order_zero_risk_mult() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 0.0);
    re.set_volatility(0.80);
    CHECK(!re.check_new_order(1.0, 100.0), "zero risk mult → reject all orders");
}

void test_check_new_order_no_budget() {
    RiskEngine re;
    CHECK(!re.check_new_order(1.0, 100.0), "no budget set → reject");
}

void test_check_new_order_cumulative() {
    RiskEngine re;
    re.set_budget(20.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    // First order should succeed
    CHECK(re.check_new_order(1.0, 100.0), "first order fits");
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 1.0));

    // Second order: consumed_es now ~8.63, adding another ~8.63 → ~17.26, still < 20
    CHECK(re.check_new_order(1.0, 100.0), "second order still fits");
    re.on_fill(make_fill(2000, OrderSide::BUY, 100.0, 1.0));

    // Third order: consumed_es now ~17.26, adding 1 more → ~25.9, exceeds 20
    CHECK(!re.check_new_order(1.0, 100.0), "third order exceeds budget");
}

// ===================================================================
//  4. get_allowed_size
// ===================================================================

void test_get_allowed_size_within_budget() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    double allowed = re.get_allowed_size(10.0, 100.0);
    CHECK_NEAR(allowed, 10.0, 1e-6, "full size allowed when budget ample");
}

void test_get_allowed_size_clips_to_budget() {
    RiskEngine re;
    re.set_budget(10.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    double allowed = re.get_allowed_size(1000.0, 100.0);
    CHECK(allowed > 0.0, "some size allowed");
    CHECK(allowed < 1000.0, "size clipped below desired");
}

void test_get_allowed_size_clips_to_notional() {
    RiskEngine re;
    re.set_budget(1000000.0, 500.0, 1.0);   // notional cap = 500 USDT
    re.set_volatility(0.80);
    double allowed = re.get_allowed_size(100.0, 100.0);
    CHECK_NEAR(allowed, 5.0, 1e-6, "clipped to max_position_usd / price");
}

void test_get_allowed_size_zero_with_no_budget() {
    RiskEngine re;
    double allowed = re.get_allowed_size(10.0, 100.0);
    CHECK(allowed == 0.0, "no budget → allowed = 0");
}

// ===================================================================
//  5. Position sizing formula (§14.4)
// ===================================================================

void test_position_sizing_basic() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.60);  // matches sigma_target → vol_ratio = 1.0

    // entry=100, stop=95 → distance=5
    // Q_risk = 50/5 = 10
    // vol_ratio = 0.60/0.60 = 1.0
    // R_budget = (1000-0)/1000 = 1.0
    // Q_final = 10 * 1.0 * 1.0 = 10
    // Q_max = 100000/100 = 1000
    double qty = re.compute_position_size(100.0, 95.0);
    CHECK_NEAR(qty, 10.0, 0.01, "basic position sizing");
}

void test_position_sizing_vol_adjustment() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(1.20);  // 2x target → vol_ratio = 0.5

    double qty = re.compute_position_size(100.0, 95.0);
    // Q_risk = 10, vol_ratio = 0.5, → 5
    CHECK_NEAR(qty, 5.0, 0.01, "vol adjustment halves size");
}

void test_position_sizing_vol_clamp_low() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(100000.0, 10000000.0, 1.0);
    re.set_volatility(0.01);  // very low vol → ratio = 60.0, clamped to 2.0

    double qty = re.compute_position_size(100.0, 95.0);
    // Q_risk = 10, vol_ratio = 2.0 (clamped)
    CHECK_NEAR(qty, 20.0, 0.01, "vol ratio clamped at 2.0");
}

void test_position_sizing_vol_clamp_high() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(100000.0, 10000000.0, 1.0);
    re.set_volatility(100.0);  // extreme vol → ratio = 0.006, clamped to 0.25

    double qty = re.compute_position_size(100.0, 95.0);
    // Q_risk = 10, vol_ratio = 0.25 (clamped)
    CHECK_NEAR(qty, 2.5, 0.01, "vol ratio clamped at 0.25");
}

void test_position_sizing_risk_multiplier_throttle() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000.0, 100000.0, 0.5);  // half throttle
    re.set_volatility(0.60);

    double qty = re.compute_position_size(100.0, 95.0);
    CHECK_NEAR(qty, 5.0, 0.01, "risk multiplier 0.5 → half size");
}

void test_position_sizing_budget_fraction() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.60);

    // Consume half the budget with a fill
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 58.0));
    // consumed_es ≈ 58 * 100 * 0.6 * 0.0523 * 2.063 ≈ ~500
    // R_budget ≈ 0.5

    double qty = re.compute_position_size(100.0, 95.0);
    CHECK(qty < 10.0, "budget partially consumed → smaller size");
    CHECK(qty > 0.0, "still some budget remaining");
}

void test_position_sizing_zero_budget() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(0.0, 100000.0, 1.0);  // zero budget
    re.set_volatility(0.60);

    double qty = re.compute_position_size(100.0, 95.0);
    CHECK(qty == 0.0, "zero budget → zero size");
}

void test_position_sizing_q_max_cap() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000000.0, 200.0, 1.0);  // max_pos = 200 USDT
    re.set_volatility(0.60);

    double qty = re.compute_position_size(100.0, 95.0);
    // Q_max = 200/100 = 2.0
    CHECK_NEAR(qty, 2.0, 0.01, "capped at Q_max = max_position_usd / entry");
}

void test_position_sizing_zero_stop_distance() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    double qty = re.compute_position_size(100.0, 100.0);
    CHECK(qty == 0.0, "zero stop distance → zero size");
}

// ===================================================================
//  6. Fill processing and position tracking
// ===================================================================

void test_fill_buy_tracks_position() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 5.0));
    CHECK_NEAR(re.get_position_qty(), 5.0, 1e-10, "buy fill → positive position");
    CHECK(re.get_consumed_es() > 0.0, "consumed_es > 0 after fill");
}

void test_fill_sell_tracks_position() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    re.on_fill(make_fill(1000, OrderSide::SELL, 100.0, 5.0));
    CHECK_NEAR(re.get_position_qty(), -5.0, 1e-10, "sell fill → negative position");
}

void test_fill_close_position_resets_es() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 5.0));
    CHECK(re.get_consumed_es() > 0.0, "open position has ES");

    re.on_fill(make_fill(2000, OrderSide::SELL, 105.0, 5.0));
    CHECK_NEAR(re.get_position_qty(), 0.0, 1e-10, "flat after close");
    CHECK_NEAR(re.get_consumed_es(), 0.0, 1e-10, "flat → zero ES");
}

void test_fill_partial_close() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 10.0));
    double es_full = re.get_consumed_es();

    re.on_fill(make_fill(2000, OrderSide::SELL, 100.0, 5.0));
    double es_half = re.get_consumed_es();

    CHECK(es_half < es_full, "partial close reduces ES");
    CHECK_NEAR(re.get_position_qty(), 5.0, 1e-10, "half position remains");
}

// ===================================================================
//  7. Unrealized PnL
// ===================================================================

void test_unrealized_pnl_long_profit() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 2.0));
    re.on_price_update(110.0, 2000);
    CHECK_NEAR(re.get_unrealized_pnl(), 20.0, 1e-6, "long profit: (110-100)*2 = 20");
}

void test_unrealized_pnl_long_loss() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 2.0));
    re.on_price_update(90.0, 2000);
    CHECK_NEAR(re.get_unrealized_pnl(), -20.0, 1e-6, "long loss: (90-100)*2 = -20");
}

void test_unrealized_pnl_short_profit() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.on_fill(make_fill(1000, OrderSide::SELL, 100.0, 2.0));
    re.on_price_update(90.0, 2000);
    CHECK_NEAR(re.get_unrealized_pnl(), 20.0, 1e-6, "short profit: (90-100)*(-2) = 20");
}

void test_unrealized_pnl_short_loss() {
    RiskEngine re;
    re.set_budget(10000.0, 100000.0, 1.0);
    re.on_fill(make_fill(1000, OrderSide::SELL, 100.0, 2.0));
    re.on_price_update(110.0, 2000);
    CHECK_NEAR(re.get_unrealized_pnl(), -20.0, 1e-6, "short loss: (110-100)*(-2) = -20");
}

void test_unrealized_pnl_flat() {
    RiskEngine re;
    CHECK_NEAR(re.get_unrealized_pnl(), 0.0, 1e-10, "no position → 0 PnL");
}

// ===================================================================
//  8. Risk-budget exhaustion checks
// ===================================================================

void test_budget_exhausted_when_consumed_exceeds() {
    RiskEngine re;
    re.set_budget(10.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    // Fill enough to exceed budget: ES ≈ qty * 100 * 0.8 * 0.0523 * 2.063
    // Need ES > 10 → qty * 8.63 > 10 → qty > 1.16
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 2.0));
    CHECK(re.is_budget_exhausted(), "consumed ES exceeds budget");
}

void test_budget_not_exhausted_when_within() {
    RiskEngine re;
    re.set_budget(100.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 1.0));
    CHECK(!re.is_budget_exhausted(), "consumed ES within budget");
}

// ===================================================================
//  9. Snapshot
// ===================================================================

void test_snapshot_fields() {
    RiskEngine re;
    re.set_budget(1000.0, 10000.0, 0.75);
    re.set_volatility(0.80);
    re.on_fill(make_fill(5000, OrderSide::BUY, 100.0, 1.0));

    auto snap = re.get_snapshot();
    CHECK_NEAR(snap.es_budget, 1000.0, 1e-10, "snapshot es_budget");
    CHECK_NEAR(snap.max_position_usd, 10000.0, 1e-10, "snapshot max_position_usd");
    CHECK_NEAR(snap.risk_multiplier, 0.75, 1e-10, "snapshot risk_multiplier");
    CHECK(snap.consumed_es > 0.0, "snapshot consumed_es > 0");
    CHECK(snap.timestamp == 5000, "snapshot timestamp from last fill");
}

// ===================================================================
//  10. Reset
// ===================================================================

void test_reset() {
    RiskEngine re;
    re.set_budget(1000.0, 10000.0, 0.5);
    re.set_volatility(0.50);
    re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 5.0));

    re.reset();
    CHECK_NEAR(re.get_consumed_es(), 0.0, 1e-10, "reset consumed_es");
    CHECK_NEAR(re.get_position_qty(), 0.0, 1e-10, "reset position");
    CHECK(re.get_snapshot().es_budget == 0.0, "reset clears budget");
}

// ===================================================================
//  11. Determinism
// ===================================================================

void test_determinism() {
    auto run = [](RiskConfig cfg) {
        RiskEngine re(cfg);
        re.set_budget(1000.0, 50000.0, 0.8);
        re.set_volatility(0.75);

        re.on_fill(make_fill(1000, OrderSide::BUY, 100.0, 3.0));
        re.on_price_update(105.0, 2000);
        double sz = re.compute_position_size(105.0, 100.0);
        re.on_fill(make_fill(3000, OrderSide::BUY, 105.0, sz));
        re.on_price_update(110.0, 4000);

        return std::make_tuple(
            re.get_consumed_es(),
            re.get_position_qty(),
            re.get_unrealized_pnl(),
            re.compute_position_size(110.0, 105.0)
        );
    };

    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;

    auto [es1, qty1, pnl1, sz1] = run(cfg);
    auto [es2, qty2, pnl2, sz2] = run(cfg);

    CHECK(es1 == es2, "deterministic consumed_es");
    CHECK(qty1 == qty2, "deterministic position");
    CHECK(pnl1 == pnl2, "deterministic PnL");
    CHECK(sz1 == sz2, "deterministic sizing");
}

// ===================================================================
//  12. Property-based: ES >= 0, Q_final in [0, Q_max]
// ===================================================================

void test_es_always_non_negative() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    for (int i = 0; i < 100; ++i) {
        double price = 50.0 + i * 0.5;
        double qty   = 0.1 * (i + 1);
        re.on_fill(make_fill(i * 100, (i % 2 == 0) ? OrderSide::BUY : OrderSide::SELL,
                              price, qty));
        CHECK(re.get_consumed_es() >= 0.0, "ES >= 0 invariant");
    }
}

void test_position_size_in_valid_range() {
    RiskConfig cfg;
    cfg.target_risk_usd = 50.0;
    cfg.sigma_target    = 0.60;
    RiskEngine re(cfg);
    re.set_budget(1000.0, 5000.0, 1.0);

    double vols[] = {0.01, 0.1, 0.5, 0.8, 1.5, 5.0, 100.0};
    for (double vol : vols) {
        re.set_volatility(vol);
        double qty = re.compute_position_size(100.0, 95.0);
        double q_max = 5000.0 / 100.0;
        CHECK(qty >= 0.0, "Q_final >= 0");
        CHECK(qty <= q_max + 1e-10, "Q_final <= Q_max");
    }
}

// ===================================================================
//  13. Boundary / adversarial
// ===================================================================

void test_zero_price() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    CHECK(!re.check_new_order(1.0, 0.0), "zero price → reject");
    CHECK(re.compute_position_size(0.0, 95.0) == 0.0, "zero entry → zero size");
    CHECK(re.compute_position_size(100.0, 0.0) == 0.0, "zero stop → zero size");
}

void test_negative_quantity() {
    RiskEngine re;
    re.set_budget(1000.0, 100000.0, 1.0);
    re.set_volatility(0.80);
    double allowed = re.get_allowed_size(-5.0, 100.0);
    CHECK(allowed >= 0.0, "negative desired qty → non-negative allowed");
}

void test_very_large_position() {
    RiskEngine re;
    re.set_budget(1e12, 1e15, 1.0);
    re.set_volatility(0.80);
    CHECK(re.check_new_order(1e10, 1000.0), "huge budget → huge order accepted");
}

// ===================================================================
//  14. Integration: risk check gates cumulative fills
// ===================================================================

void test_cumulative_fill_budget_enforcement() {
    RiskEngine re;
    re.set_budget(50.0, 100000.0, 1.0);
    re.set_volatility(0.80);

    int fills = 0;
    for (int i = 0; i < 100; ++i) {
        if (re.check_new_order(1.0, 100.0)) {
            re.on_fill(make_fill(i * 100, OrderSide::BUY, 100.0, 1.0));
            ++fills;
        } else {
            break;
        }
    }

    CHECK(fills > 0, "at least one fill succeeded");
    CHECK(fills < 100, "budget stopped further fills");
    CHECK(!re.check_new_order(1.0, 100.0), "budget exhausted after fills");
}

// ===================================================================

int main() {
    test_default_construction();
    test_set_budget();
    test_set_budget_clamps();
    test_set_volatility();

    test_es_computation_example();
    test_es_updates_on_price_change();
    test_es_zero_with_no_position();

    test_check_new_order_within_budget();
    test_check_new_order_exceeds_es_budget();
    test_check_new_order_exceeds_notional_cap();
    test_check_new_order_zero_risk_mult();
    test_check_new_order_no_budget();
    test_check_new_order_cumulative();

    test_get_allowed_size_within_budget();
    test_get_allowed_size_clips_to_budget();
    test_get_allowed_size_clips_to_notional();
    test_get_allowed_size_zero_with_no_budget();

    test_position_sizing_basic();
    test_position_sizing_vol_adjustment();
    test_position_sizing_vol_clamp_low();
    test_position_sizing_vol_clamp_high();
    test_position_sizing_risk_multiplier_throttle();
    test_position_sizing_budget_fraction();
    test_position_sizing_zero_budget();
    test_position_sizing_q_max_cap();
    test_position_sizing_zero_stop_distance();

    test_fill_buy_tracks_position();
    test_fill_sell_tracks_position();
    test_fill_close_position_resets_es();
    test_fill_partial_close();

    test_unrealized_pnl_long_profit();
    test_unrealized_pnl_long_loss();
    test_unrealized_pnl_short_profit();
    test_unrealized_pnl_short_loss();
    test_unrealized_pnl_flat();

    test_budget_exhausted_when_consumed_exceeds();
    test_budget_not_exhausted_when_within();

    test_snapshot_fields();

    test_reset();

    test_determinism();

    test_es_always_non_negative();
    test_position_size_in_valid_range();

    test_zero_price();
    test_negative_quantity();
    test_very_large_position();

    test_cumulative_fill_budget_enforcement();

    std::cout << tests_passed << " / " << tests_run << " tests passed." << std::endl;
    return (tests_passed == tests_run) ? 0 : 1;
}
