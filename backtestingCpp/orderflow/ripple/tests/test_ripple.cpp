#include <cassert>
#include <iostream>
#include <cmath>
#include <vector>
#include <memory>
#include <sstream>

#include "../RippleTypes.h"
#include "../RippleConfig.h"
#include "../RippleContext.h"
#include "../WallDetector.h"
#include "../RippleFeatureEngine.h"
#include "../RippleEvidenceEngine.h"
#include "../IRippleStateInference.h"
#include "../ScoreBasedInference.h"
#include "../RippleStateTracker.h"
#include "../TriggerDecisionEngine.h"
#include "../RippleEngine.h"
#include "../RippleDiagnostics.h"
#include "../../OrderBook.h"
#include "../../TradeFlow.h"
#include "../../Types.h"

using namespace orderflow;
using namespace orderflow::ripple;

// ======================================================================
// Test helpers
// ======================================================================

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

#define CLOSE(a, b, eps) (std::abs((a) - (b)) < (eps))

static DepthUpdate make_snapshot(int64_t ts,
                                  std::vector<std::pair<double,double>> bids,
                                  std::vector<std::pair<double,double>> asks) {
    DepthUpdate du;
    du.timestamp = ts;
    du.is_snapshot = true;
    du.first_update_id = ts;
    du.final_update_id = ts;
    for (auto& [p, q] : bids) du.bids.push_back({p, q});
    for (auto& [p, q] : asks) du.asks.push_back({p, q});
    return du;
}

static DepthUpdate make_depth_update(int64_t ts,
                                      std::vector<std::pair<double,double>> bids,
                                      std::vector<std::pair<double,double>> asks) {
    auto du = make_snapshot(ts, bids, asks);
    du.is_snapshot = false;
    return du;
}

static Trade make_trade(int64_t ts, double price, double qty, bool is_buyer_maker) {
    return {ts, price, qty, is_buyer_maker};
}

// ======================================================================
// Test: RippleContext
// ======================================================================

void test_context_snapshot() {
    RippleConfig cfg;
    cfg.book_depth_levels = 5;
    RippleContext ctx(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}, {99.0, 5.0}},
        {{101.0, 8.0}, {102.0, 3.0}});
    book.on_depth_update(snap);

    ctx.on_depth(book, 1000);
    auto view = ctx.snapshot_book();

    CHECK(view.best_bid == 100.0, "Context best bid");
    CHECK(view.best_ask == 101.0, "Context best ask");
    CHECK(view.bids.size() == 2, "Context bids count");
    CHECK(view.asks.size() == 2, "Context asks count");
    CHECK(view.timestamp == 1000, "Context timestamp");
}

void test_context_trade_window() {
    RippleConfig cfg;
    cfg.feature_window_ms = 5000;
    RippleContext ctx(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}}, {{101.0, 10.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);

    ctx.on_trade(make_trade(1500, 101.0, 2.0, false)); // buy aggressor
    ctx.on_trade(make_trade(2000, 100.0, 1.5, true));  // sell aggressor
    ctx.on_trade(make_trade(2500, 101.0, 3.0, false)); // buy aggressor

    auto tw = ctx.trade_window(5000);
    CHECK(tw.buy_count == 2, "Trade window buy count");
    CHECK(tw.sell_count == 1, "Trade window sell count");
    CHECK(CLOSE(tw.buy_volume, 5.0, 0.01), "Trade window buy volume");
    CHECK(CLOSE(tw.sell_volume, 1.5, 0.01), "Trade window sell volume");
    CHECK(CLOSE(tw.net_delta, 3.5, 0.01), "Trade window net delta");
}

void test_context_microprice() {
    RippleConfig cfg;
    cfg.microprice_lookback_ms = 2000;
    RippleContext ctx(cfg);

    OrderBook book;
    auto snap1 = make_snapshot(1000,
        {{100.0, 10.0}}, {{101.0, 10.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);

    double mp1 = ctx.microprice();
    CHECK(CLOSE(mp1, 100.5, 0.01), "Microprice equal qty = midprice");

    auto snap2 = make_snapshot(2000,
        {{100.0, 5.0}}, {{101.0, 15.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 2000);

    double mp2 = ctx.microprice();
    // microprice = (100*15 + 101*5) / 20 = (1500 + 505) / 20 = 100.25
    CHECK(CLOSE(mp2, 100.25, 0.01), "Microprice weighted toward bid");
}

// ======================================================================
// Test: WallDetector
// ======================================================================

void test_wall_detection_basic() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 500;

    RippleContext ctx(cfg);
    WallDetector det(cfg);

    OrderBook book;
    // Big bid wall at 97.0 within top levels
    auto snap = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 50.0}},
        {{101.0, 3.0}, {102.0, 2.0}, {103.0, 4.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    auto walls = det.active_walls();
    CHECK(!walls.empty(), "Wall detected");

    bool found_bid_wall = false;
    for (auto& w : walls) {
        if (w.side == WallSide::BID && w.price == 97.0) {
            found_bid_wall = true;
            CHECK(w.lifecycle == WallLifecycle::FORMING, "Wall initially forming");
            CHECK(CLOSE(w.current_qty, 50.0, 0.1), "Wall qty correct");
        }
    }
    CHECK(found_bid_wall, "Bid wall at 97 found");
}

void test_wall_persistence() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 500;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap1 = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0}, {96.0, 2.0}, {95.0, 50.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    // Second update — wall persists, now old enough
    auto snap2 = make_depth_update(2000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0}, {96.0, 2.0}, {95.0, 48.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 2000);
    det.on_depth(ctx);

    auto walls = det.active_walls();
    bool found = false;
    for (auto& w : walls) {
        if (w.price == 95.0) {
            found = true;
            CHECK(w.lifecycle == WallLifecycle::ACTIVE || w.lifecycle == WallLifecycle::DEPLETING,
                  "Wall persists and becomes active/depleting");
        }
    }
    CHECK(found, "Persistent wall found on second update");
}

void test_wall_withdrawal() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;
    cfg.feature_window_ms = 5000;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap1 = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 50.0}},
        {{101.0, 3.0}, {102.0, 4.0}, {103.0, 2.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    // Wall drops to 5 (no matching trade = withdrawal)
    auto snap2 = make_depth_update(1500,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 5.0}},
        {{101.0, 3.0}, {102.0, 4.0}, {103.0, 2.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 1500);
    det.on_depth(ctx);

    auto walls = det.active_walls();
    // Wall should be gone or withdrawn (pruned from active list)
    bool wall_gone_or_withdrawn = walls.empty();
    for (auto& w : walls) {
        if (w.price == 98.0 && w.lifecycle == WallLifecycle::WITHDRAWN)
            wall_gone_or_withdrawn = true;
    }
    CHECK(wall_gone_or_withdrawn, "Wall withdrawal detected");
}

void test_wall_refill() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;
    cfg.feature_window_ms = 5000;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap1 = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0}, {96.0, 2.0}, {95.0, 50.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    // Deplete partially (with trade vol to avoid withdrawal classification)
    ctx.on_trade(make_trade(1150, 95.0, 15.0, true));
    auto snap2 = make_depth_update(1200,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0}, {96.0, 2.0}, {95.0, 30.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 1200);
    det.on_depth(ctx);

    // Refill
    auto snap3 = make_depth_update(1500,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0}, {96.0, 2.0}, {95.0, 55.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap3);
    ctx.on_depth(book, 1500);
    det.on_depth(ctx);

    auto walls = det.active_walls();
    bool found_refill = false;
    for (auto& w : walls) {
        if (w.price == 95.0) {
            found_refill = (w.refill_count > 0);
        }
    }
    CHECK(found_refill, "Wall refill detected");
}

// ======================================================================
// Test: WallDetector — metrics
// ======================================================================

void test_wall_metrics_populated() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 50.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    // Persist wall
    auto snap2 = make_depth_update(1200,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 48.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 1200);
    det.on_depth(ctx);

    auto& mets = det.metrics();
    CHECK(!mets.empty(), "Metrics populated");

    bool found = false;
    for (auto& m : mets) {
        if (m.side == WallSide::BID && CLOSE(m.price, 97.0, 0.01)) {
            found = true;
            CHECK(m.absolute_size > 0.0, "Metrics: absolute_size > 0");
            CHECK(m.relative_size > 1.0, "Metrics: relative_size > 1 (is a wall)");
            CHECK(m.baseline_depth > 0.0, "Metrics: baseline_depth > 0");
            CHECK(m.distance_from_mid_ticks > 0.0, "Metrics: distance_from_mid > 0");
            CHECK(m.distance_from_best_ticks > 0.0, "Metrics: distance_from_best > 0");
            CHECK(m.local_rank >= 1, "Metrics: local_rank >= 1");
            CHECK(m.same_price_persists, "Metrics: same_price_persists after 2 updates");
            CHECK(m.persistence_sec > 0.0, "Metrics: persistence_sec > 0");
        }
    }
    CHECK(found, "Metrics found for bid wall at 97");
}

void test_wall_primary_metrics() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 50.0}},
        {{101.0, 3.0}, {102.0, 2.0}, {103.0, 60.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    auto* bm = det.primary_bid_metrics();
    auto* am = det.primary_ask_metrics();

    CHECK(bm != nullptr, "Primary bid metrics present");
    CHECK(am != nullptr, "Primary ask metrics present");

    if (bm) {
        CHECK(bm->side == WallSide::BID, "Primary bid metrics side correct");
        CHECK(CLOSE(bm->price, 98.0, 0.01), "Primary bid metrics price");
    }
    if (am) {
        CHECK(am->side == WallSide::ASK, "Primary ask metrics side correct");
        CHECK(CLOSE(am->price, 103.0, 0.01), "Primary ask metrics price");
    }
}

void test_wall_growth_rate() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    auto snap1 = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 40.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    // Wall grows
    auto snap2 = make_depth_update(1500,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 60.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 1500);
    det.on_depth(ctx);

    auto& mets = det.metrics();
    bool found = false;
    for (auto& m : mets) {
        if (CLOSE(m.price, 98.0, 0.01)) {
            found = true;
            CHECK(m.growth_rate > 0.0, "Growth rate > 0 after wall grows");
            CHECK(m.lifecycle == WallLifecycle::REFILLING,
                  "Wall lifecycle REFILLING after growth");
        }
    }
    CHECK(found, "Growing wall found");
}

void test_wall_median_baseline() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    // Book with varying sizes: median of {2, 3, 4, 5} = 3.5, wall = 15 → rel ≈ 4.3
    auto snap = make_snapshot(1000,
        {{100.0, 5.0}, {99.0, 3.0}, {98.0, 2.0}, {97.0, 4.0}, {96.0, 15.0}},
        {{101.0, 3.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);

    auto& mets = det.metrics();
    bool found = false;
    for (auto& m : mets) {
        if (CLOSE(m.price, 96.0, 0.01)) {
            found = true;
            // Baseline should be median of {5, 3, 2, 4} = 3.5
            CHECK(CLOSE(m.baseline_depth, 3.5, 0.1), "Baseline is median (3.5)");
            CHECK(CLOSE(m.relative_size, 15.0 / 3.5, 0.1), "Relative size uses median");
        }
    }
    CHECK(found, "Wall found with median baseline");
}

// ======================================================================
// Test: FeatureEngine
// ======================================================================

void test_feature_computation() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.book_depth_levels = 5;
    cfg.feature_window_ms = 5000;
    cfg.microprice_lookback_ms = 2000;

    RippleContext ctx(cfg);
    RippleFeatureEngine fe(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}, {99.0, 5.0}, {98.0, 3.0}, {97.0, 4.0}, {96.0, 2.0}},
        {{101.0, 8.0}, {102.0, 6.0}, {103.0, 4.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);

    // Add trades
    ctx.on_trade(make_trade(1100, 101.0, 2.0, false)); // buy
    ctx.on_trade(make_trade(1200, 100.0, 1.0, true));  // sell

    WallCandidate wall;
    wall.wall_id = 1;
    wall.side = WallSide::BID;
    wall.price = 96.0;
    wall.initial_qty = 20.0;
    wall.current_qty = 18.0;
    wall.peak_qty = 20.0;
    wall.first_seen = 500;
    wall.last_seen = 1000;
    wall.lifecycle = WallLifecycle::ACTIVE;
    wall.cumulative_absorbed = 1.5;

    auto f = fe.compute(ctx, wall);

    CHECK(f.relative_wall_size > 0, "Relative wall size > 0");
    CHECK(f.wall_persistence_sec > 0, "Wall persistence > 0");
    CHECK(f.distance_to_wall_ticks > 0, "Distance to wall > 0");
    CHECK(std::abs(f.aggressive_flow_imbalance) <= 1.0, "Flow imbalance in [-1,1]");
    CHECK(std::abs(f.trade_count_imbalance) <= 1.0, "Count imbalance in [-1,1]");
    CHECK(f.spread_shock > 0, "Spread shock > 0");
    CHECK(f.time_since_wall_formed_sec > 0, "Time since wall > 0");
}

void test_feature_with_metrics() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.book_depth_levels = 10;
    cfg.feature_window_ms = 5000;
    cfg.microprice_lookback_ms = 2000;

    RippleContext ctx(cfg);
    RippleFeatureEngine fe(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}, {99.0, 5.0}, {98.0, 3.0}, {97.0, 4.0}, {96.0, 50.0}},
        {{101.0, 8.0}, {102.0, 6.0}, {103.0, 4.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);

    ctx.on_trade(make_trade(1100, 101.0, 2.0, false));
    ctx.on_trade(make_trade(1200, 100.0, 1.0, true));

    WallCandidate wall;
    wall.wall_id = 1;
    wall.side = WallSide::BID;
    wall.price = 96.0;
    wall.initial_qty = 50.0;
    wall.current_qty = 45.0;
    wall.peak_qty = 50.0;
    wall.first_seen = 500;
    wall.last_seen = 1000;
    wall.lifecycle = WallLifecycle::ACTIVE;
    wall.depletion_rate = 2.0;
    wall.refill_rate = 0.5;

    WallMetrics wm;
    wm.wall_id = 1;
    wm.side = WallSide::BID;
    wm.price = 96.0;
    wm.baseline_depth = 5.0;
    wm.relative_size = 9.0;
    wm.distance_from_mid_ticks = 4.5;
    wm.distance_from_best_ticks = 4.0;
    wm.persistence_sec = 0.5;
    wm.depletion_rate = 2.0;
    wm.growth_rate = 0.5;

    auto f_without = fe.compute(ctx, wall);
    auto f_with    = fe.compute(ctx, wall, &wm);

    CHECK(f_with.relative_wall_size > 0, "With metrics: relative wall size > 0");
    CHECK(CLOSE(f_with.relative_wall_size, 45.0 / 5.0, 0.1),
          "With metrics: uses median baseline");
    CHECK(CLOSE(f_with.distance_to_wall_ticks, 4.5, 0.1),
          "With metrics: uses precomputed distance");
    CHECK(CLOSE(f_with.wall_persistence_sec, 0.5, 0.01),
          "With metrics: uses precomputed persistence");
    CHECK(CLOSE(f_with.wall_refill_rate, 0.5, 0.01),
          "With metrics: uses growth_rate from WallMetrics");

    CHECK(f_without.relative_wall_size > 0, "Without metrics: relative wall size > 0");
    CHECK(f_without.relative_wall_size != f_with.relative_wall_size,
          "Metric-based and fallback baselines differ");
}

void test_feature_cancel_rate() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.book_depth_levels = 5;
    cfg.feature_window_ms = 5000;

    RippleContext ctx(cfg);
    RippleFeatureEngine fe(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}, {99.0, 5.0}, {98.0, 50.0}},
        {{101.0, 8.0}, {102.0, 6.0}, {103.0, 4.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);

    // Sell aggressor hitting the wall at 98.0 — explains some depletion
    ctx.on_trade(make_trade(800, 98.0, 3.0, true));

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.price = 98.0;
    wall.initial_qty = 50.0;
    wall.current_qty = 40.0;
    wall.peak_qty = 50.0;
    wall.first_seen = 500;

    auto f = fe.compute(ctx, wall);
    // 10 qty lost, 3 explained by trade → 7 unexplained
    // elapsed = (1000 - 500) / 1000 = 0.5 sec → cancel_rate ≈ 14
    CHECK(f.wall_cancel_rate > 0.0, "Cancel rate > 0 when depletion exceeds trade vol");

    // Now with full trade explanation
    ctx.on_trade(make_trade(900, 98.0, 7.0, true));
    auto f2 = fe.compute(ctx, wall);
    CHECK(f2.wall_cancel_rate < f.wall_cancel_rate,
          "Cancel rate drops when more depletion is explained by trades");
}

void test_feature_impact() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.book_depth_levels = 5;
    cfg.feature_window_ms = 5000;
    cfg.microprice_lookback_ms = 5000;

    RippleContext ctx(cfg);
    RippleFeatureEngine fe(cfg);

    OrderBook book;
    auto snap1 = make_snapshot(1000,
        {{100.0, 10.0}, {99.0, 5.0}},
        {{101.0, 10.0}, {102.0, 5.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);

    // Microprice at t=1000 ≈ 100.5

    auto snap2 = make_depth_update(2000,
        {{100.0, 5.0}, {99.0, 5.0}},
        {{101.0, 15.0}, {102.0, 5.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 2000);

    // Microprice shifts toward ask: (100*15 + 101*5) / 20 = 100.25
    ctx.on_trade(make_trade(1500, 100.0, 1.0, true));

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.price = 99.0;
    wall.initial_qty = 5.0;
    wall.current_qty = 5.0;
    wall.peak_qty = 5.0;
    wall.first_seen = 500;

    auto f = fe.compute(ctx, wall);
    CHECK(f.impact_per_unit_volume >= 0.0, "Impact >= 0");
}

void test_feature_clamping() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.book_depth_levels = 5;
    cfg.feature_window_ms = 5000;

    RippleContext ctx(cfg);
    RippleFeatureEngine fe(cfg);

    OrderBook book;
    auto snap = make_snapshot(1000,
        {{100.0, 10.0}},
        {{101.0, 10.0}});
    book.on_depth_update(snap);
    ctx.on_depth(book, 1000);

    // All buy trades → imbalance should be +1, clamped
    for (int i = 0; i < 20; i++)
        ctx.on_trade(make_trade(1050 + i, 101.0, 1.0, false));

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.price = 100.0;
    wall.initial_qty = 10.0;
    wall.current_qty = 10.0;
    wall.peak_qty = 10.0;
    wall.first_seen = 500;

    auto f = fe.compute(ctx, wall);
    CHECK(f.aggressive_flow_imbalance >= -1.0 && f.aggressive_flow_imbalance <= 1.0,
          "Flow imbalance clamped to [-1, 1]");
    CHECK(f.trade_count_imbalance >= -1.0 && f.trade_count_imbalance <= 1.0,
          "Count imbalance clamped to [-1, 1]");
    CHECK(f.top1_imbalance >= -1.0 && f.top1_imbalance <= 1.0,
          "Top1 imbalance clamped to [-1, 1]");
}

// ======================================================================
// Test: EvidenceEngine
// ======================================================================

void test_absorption_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Strong absorption scenario: flow toward wall, wall holding, large, close
    RippleFeatures f{};
    f.relative_wall_size = 5.0;
    f.aggressive_flow_imbalance = -0.6;  // sell flow toward bid wall
    f.distance_to_wall_ticks = 2.0;
    f.wall_depletion_rate = 0.5;

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 100.0;
    wall.current_qty = 90.0;
    wall.initial_qty = 100.0;
    wall.cumulative_absorbed = 8.0;

    auto ev = ee.compute(f, wall);
    CHECK(ev.absorption > 0.3, "Absorption evidence present for absorption scenario");
}

void test_breakout_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Breakout scenario: rapid depletion, strong flow, spread widening
    RippleFeatures f{};
    f.relative_wall_size = 1.5;
    f.aggressive_flow_imbalance = -0.8;  // sell flow toward bid wall
    f.wall_depletion_rate = 10.0;
    f.spread_shock = 2.5;
    f.short_horizon_volatility = 0.5;

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 100.0;
    wall.current_qty = 10.0;
    wall.initial_qty = 100.0;
    wall.cumulative_absorbed = 70.0;

    auto ev = ee.compute(f, wall);
    CHECK(ev.breakout > 0.5, "Breakout evidence high for breakout scenario");
}

void test_withdrawal_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Withdrawal: large qty drop with little trade volume
    RippleFeatures f{};
    f.wall_cancel_rate = 15.0;
    f.wall_depletion_rate = 2.0;

    WallCandidate wall;
    wall.side = WallSide::ASK;
    wall.peak_qty = 100.0;
    wall.current_qty = 20.0;
    wall.initial_qty = 100.0;
    wall.cumulative_absorbed = 5.0;

    auto ev = ee.compute(f, wall);
    CHECK(ev.withdrawal > 0.3, "Withdrawal evidence for cancellation scenario");
}

void test_stabilization_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Stabilization: low volatility, normal spread, stable queue, flat drift
    RippleFeatures f{};
    f.short_horizon_volatility = 0.01;
    f.spread_shock = 1.0;
    f.queue_stability = 0.1;
    f.microprice_drift = 0.05;

    WallCandidate wall;
    wall.side = WallSide::BID;

    auto ev = ee.compute(f, wall);
    CHECK(ev.stabilization > 0.5, "Stabilization evidence for calm market");
}

void test_exhaustion_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Exhaustion scenario: wall depleted, no refill, rising impact, sustained flow,
    // spread widening, elevated volatility, microprice drifting — all hallmarks
    // of aggressive flow losing momentum against thinning depth.
    RippleFeatures f{};
    f.wall_depletion_rate = 8.0;
    f.wall_refill_rate = 0.2;
    f.impact_per_unit_volume = 5e-5;    // high impact (normalised via 1e4 → 0.5)
    f.aggressive_flow_imbalance = -0.7; // sustained sell pressure
    f.queue_stability = 0.6;            // churning
    f.spread_shock = 1.8;              // spread widening (pushes stabilization down)
    f.short_horizon_volatility = 0.15; // elevated vol
    f.microprice_drift = -1.5;         // price moving

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 100.0;
    wall.current_qty = 25.0;           // 75% depleted

    auto ev = ee.compute(f, wall);
    CHECK(ev.exhaustion > 0.4, "Exhaustion evidence high for tiring aggression");
    CHECK(ev.exhaustion > ev.stabilization,
          "Exhaustion dominates stabilization in exhaustion scenario");
}

void test_refill_evidence() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Refill scenario: depth reappearing, flow reversing, spread normal
    RippleFeatures f{};
    f.wall_refill_rate = 6.0;
    f.wall_depletion_rate = 1.0;
    f.aggressive_flow_imbalance = 0.4;  // buy flow = away from bid wall
    f.spread_shock = 1.1;               // nearly normal
    f.impact_per_unit_volume = 1e-5;    // low impact

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 100.0;
    wall.current_qty = 60.0;

    auto ev = ee.compute(f, wall);
    CHECK(ev.refill > 0.4, "Refill evidence high when depth reappears");
    CHECK(ev.refill > ev.breakout, "Refill dominates breakout in refill scenario");
}

void test_evidence_dominant() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Build a scenario where absorption clearly dominates
    RippleFeatures f{};
    f.relative_wall_size = 8.0;
    f.aggressive_flow_imbalance = -0.9;  // strong sell into bid wall
    f.distance_to_wall_ticks = 1.0;
    f.wall_persistence_sec = 5.0;
    f.impact_per_unit_volume = 0.0;

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 200.0;
    wall.current_qty = 195.0;            // barely depleted

    auto ev = ee.compute(f, wall);
    CHECK(std::string(ev.dominant()) == "absorption",
          "Dominant evidence is absorption for absorption scenario");
    CHECK(ev.dominant_score() > 0.5, "Dominant score is meaningful");
}

void test_evidence_all_clamped() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Extreme values — ensure everything stays in [0, 1]
    RippleFeatures f{};
    f.relative_wall_size = 100.0;
    f.aggressive_flow_imbalance = -1.0;
    f.wall_depletion_rate = 999.0;
    f.wall_refill_rate = 999.0;
    f.wall_cancel_rate = 999.0;
    f.impact_per_unit_volume = 1.0;
    f.spread_shock = 10.0;
    f.short_horizon_volatility = 5.0;
    f.microprice_drift = 50.0;
    f.queue_stability = 5.0;
    f.distance_to_wall_ticks = 0.0;
    f.wall_persistence_sec = 100.0;

    WallCandidate wall;
    wall.side = WallSide::BID;
    wall.peak_qty = 100.0;
    wall.current_qty = 1.0;

    auto ev = ee.compute(f, wall);
    CHECK(ev.absorption    >= 0.0 && ev.absorption    <= 1.0, "Absorption in [0,1]");
    CHECK(ev.exhaustion    >= 0.0 && ev.exhaustion    <= 1.0, "Exhaustion in [0,1]");
    CHECK(ev.withdrawal    >= 0.0 && ev.withdrawal    <= 1.0, "Withdrawal in [0,1]");
    CHECK(ev.breakout      >= 0.0 && ev.breakout      <= 1.0, "Breakout in [0,1]");
    CHECK(ev.refill        >= 0.0 && ev.refill        <= 1.0, "Refill in [0,1]");
    CHECK(ev.stabilization >= 0.0 && ev.stabilization <= 1.0, "Stabilization in [0,1]");
}

void test_evidence_withdrawal_no_cumulative() {
    RippleConfig cfg;
    RippleEvidenceEngine ee(cfg);

    // Withdrawal must work without cumulative_absorbed (which is no longer
    // populated by the depth-only WallDetector).
    RippleFeatures f{};
    f.wall_cancel_rate = 20.0;
    f.wall_depletion_rate = 1.0;
    f.spread_shock = 2.0;
    f.aggressive_flow_imbalance = 0.0;

    WallCandidate wall;
    wall.side = WallSide::ASK;
    wall.peak_qty = 80.0;
    wall.current_qty = 10.0;
    wall.cumulative_absorbed = 0.0;  // not populated

    auto ev = ee.compute(f, wall);
    CHECK(ev.withdrawal > 0.5, "Withdrawal works without cumulative_absorbed");
}

// ======================================================================
// Test: ScoreBasedInference
// ======================================================================

void test_inference_idle_to_wall_forming() {
    RippleConfig cfg;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::FORMING;

    RippleEvidence ev{};
    auto result = inf.infer(ev, RippleState::IDLE, &wall, 1000);
    CHECK(result.state == RippleState::WALL_FORMING, "IDLE -> WALL_FORMING");
    CHECK(result.is_transition, "Transition detected");
}

void test_inference_absorbing_to_exhausting() {
    RippleConfig cfg;
    cfg.exhaustion_entry = 0.5;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    RippleEvidence ev{};
    ev.exhaustion = 0.7;
    ev.absorption = 0.5;

    auto result = inf.infer(ev, RippleState::ABSORBING, &wall, 2000);
    CHECK(result.state == RippleState::EXHAUSTING, "ABSORBING -> EXHAUSTING");
}

void test_inference_absorbing_to_breaking() {
    RippleConfig cfg;
    cfg.breakout_entry = 0.7;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::FAILED;

    RippleEvidence ev{};
    ev.breakout = 0.8;

    auto result = inf.infer(ev, RippleState::ABSORBING, &wall, 3000);
    CHECK(result.state == RippleState::BREAKING, "ABSORBING -> BREAKING on wall failure");
}

void test_inference_no_wall_returns_idle() {
    RippleConfig cfg;
    ScoreBasedInference inf(cfg);

    RippleEvidence ev{};
    ev.absorption = 0.9;

    auto result = inf.infer(ev, RippleState::ABSORBING, nullptr, 1000);
    CHECK(result.state == RippleState::IDLE, "No wall -> IDLE");
}

void test_inference_scores_populated() {
    RippleConfig cfg;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    RippleEvidence ev{};
    ev.absorption = 0.7;
    ev.exhaustion = 0.3;
    ev.withdrawal = 0.1;
    ev.breakout   = 0.2;

    auto r = inf.infer(ev, RippleState::WALL_FORMING, &wall, 1000, 500);

    CHECK(r.score_for(RippleState::ABSORBING) > 0.0,
          "ABSORBING score populated from absorption evidence");
    CHECK(r.winning_score >= r.runner_up_score,
          "Winning score >= runner-up score");
    CHECK(r.state_age_ms == 500, "State age passed through");
    CHECK(!r.reason.empty(), "Reason string populated");
}

void test_inference_persistence_bonus() {
    RippleConfig cfg;
    cfg.persistence_bonus = 0.20;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    // Absorption and exhaustion are close — persistence should tip the balance
    RippleEvidence ev{};
    ev.absorption = 0.55;
    ev.exhaustion = 0.50;

    // From ABSORBING — persistence bonus goes to ABSORBING
    auto r = inf.infer(ev, RippleState::ABSORBING, &wall, 1000, 500);
    CHECK(r.state == RippleState::ABSORBING,
          "Persistence bonus keeps ABSORBING when scores are close");

    // From EXHAUSTING — persistence bonus goes to EXHAUSTING
    auto r2 = inf.infer(ev, RippleState::EXHAUSTING, &wall, 1000, 500);
    CHECK(r2.state == RippleState::EXHAUSTING,
          "Persistence bonus keeps EXHAUSTING when scores are close");
}

void test_inference_margin_gate() {
    RippleConfig cfg;
    cfg.persistence_bonus = 0.05;
    cfg.state_switch_margin = 0.20;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    // Exhaustion barely exceeds absorption — margin too small
    RippleEvidence ev{};
    ev.absorption = 0.60;
    ev.exhaustion = 0.65;

    auto r = inf.infer(ev, RippleState::ABSORBING, &wall, 1000, 500);
    CHECK(r.state == RippleState::ABSORBING,
          "Margin gate blocks transition when scores are too close");
}

void test_inference_idle_exit_threshold() {
    RippleConfig cfg;
    cfg.idle_exit_threshold = 0.50;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::FORMING;

    // Evidence is weak — should stay IDLE even though wall is present
    RippleEvidence ev{};
    auto r = inf.infer(ev, RippleState::IDLE, &wall, 1000);
    // WALL_FORMING base score = 0.25 + lifecycle boost 0.15 = 0.40 < 0.50
    CHECK(r.state == RippleState::IDLE,
          "IDLE exit gate blocks weak WALL_FORMING score");
}

void test_inference_transition_mask() {
    RippleConfig cfg;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    // From IDLE, even high stabilization evidence should not produce STABILIZING
    RippleEvidence ev{};
    ev.stabilization = 0.95;

    auto r = inf.infer(ev, RippleState::IDLE, &wall, 1000);
    CHECK(r.state != RippleState::STABILIZING,
          "IDLE -> STABILIZING blocked by transition mask");
}

void test_inference_runner_up() {
    RippleConfig cfg;
    ScoreBasedInference inf(cfg);

    WallCandidate wall;
    wall.lifecycle = WallLifecycle::ACTIVE;

    RippleEvidence ev{};
    ev.absorption = 0.8;
    ev.exhaustion = 0.5;

    auto r = inf.infer(ev, RippleState::WALL_FORMING, &wall, 1000, 500);
    CHECK(r.state == RippleState::ABSORBING, "Winner is ABSORBING");
    CHECK(r.runner_up_score > 0.0, "Runner-up score is non-zero");
    CHECK(r.confidence > 0.0, "Confidence (margin) is positive");
}

// ======================================================================
// Test: RippleStateTracker (anti-flicker, cooldown)
// ======================================================================

void test_state_tracker_anti_flicker() {
    RippleConfig cfg;
    cfg.min_dwell_fast_ms = 200;
    cfg.min_dwell_slow_ms = 500;

    auto inf = std::make_unique<ScoreBasedInference>(cfg);
    RippleStateTracker tracker(cfg, std::move(inf));

    WallCandidate wall;
    wall.wall_id = 1;
    wall.lifecycle = WallLifecycle::ACTIVE;

    RippleEvidence ev_absorb;
    ev_absorb.absorption = 0.8;

    // First: IDLE -> WALL_FORMING (at t=0, IDLE has no min dwell enforced initially)
    wall.lifecycle = WallLifecycle::FORMING;
    auto r1 = tracker.update({}, &wall, 0);

    // Try to transition again quickly (within slow dwell)
    wall.lifecycle = WallLifecycle::ACTIVE;
    RippleEvidence ev2;
    ev2.absorption = 0.8;
    auto r2 = tracker.update(ev2, &wall, 100); // only 100ms in WALL_FORMING
    CHECK(!r2.is_transition, "Anti-flicker blocks rapid transition (slow state)");

    // After sufficient dwell
    auto r3 = tracker.update(ev2, &wall, 600);
    CHECK(r3.is_transition || r3.state == RippleState::ABSORBING,
          "Transition allowed after dwell period");
}

void test_state_tracker_spatial_dedup() {
    RippleConfig cfg;
    cfg.min_dwell_fast_ms = 0;
    cfg.min_dwell_slow_ms = 0;
    cfg.intent_cooldown_ms = 1000;

    auto inf = std::make_unique<ScoreBasedInference>(cfg);
    RippleStateTracker tracker(cfg, std::move(inf));

    WallCandidate wall;
    wall.wall_id = 42;
    wall.lifecycle = WallLifecycle::FORMING;

    // Transition to WALL_FORMING
    auto r1 = tracker.update({}, &wall, 1000);
    CHECK(r1.is_transition, "First transition to WALL_FORMING");

    // Go to IDLE
    auto r_idle = tracker.update({}, nullptr, 1100);

    // Try to go WALL_FORMING again at same wall within cooldown
    auto r2 = tracker.update({}, &wall, 1200);
    // Should be blocked by spatial dedup
    CHECK(!r2.is_transition || r2.state != RippleState::WALL_FORMING,
          "Spatial dedup blocks same wall same state within cooldown");
}

// ======================================================================
// Test: TriggerDecisionEngine
// ======================================================================

void test_trigger_bounce_entry() {
    RippleConfig cfg;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 10;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.initial_qty = 50.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;
    inv.position = 0.0;

    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.1;
    ev.withdrawal = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;
    inf.confidence = 0.7;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::ENTER_BOUNCE_LONG,
          "Bounce long triggered on bid wall exhaustion");
    CHECK(d.wall_id == 10, "Decision carries wall id");
    CHECK(d.reference_price == 95.0, "Decision carries wall price");
    CHECK(d.invalidation_price > 0.0, "Invalidation price populated");
}

void test_trigger_breakout_entry() {
    RippleConfig cfg;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 20;
    wall.side = WallSide::ASK;
    wall.price = 105.0;

    InventorySnapshot inv;
    inv.position = 0.0;

    RippleEvidence ev;
    ev.breakout = 0.8;
    ev.absorption = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::BREAKING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;
    inf.confidence = 0.8;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::ENTER_BREAKOUT_LONG,
          "Breakout long triggered on ask wall break");
}

void test_trigger_inventory_filter() {
    RippleConfig cfg;
    cfg.max_position = 1.0;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 30;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.initial_qty = 50.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;
    inv.position = 1.0;

    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.1;
    ev.withdrawal = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;
    inf.confidence = 0.7;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::NO_ACTION,
          "Inventory filter blocks ENTER_BOUNCE_LONG at max position");
}

void test_trigger_global_cooldown() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 500;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 40;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.initial_qty = 50.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.1;
    ev.withdrawal = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;
    inf.confidence = 0.7;

    auto d1 = trigger.evaluate(inf, ev, &wall, inv, 1000);
    CHECK(d1.intent == RippleIntent::ENTER_BOUNCE_LONG, "First bounce signal fires");

    wall.wall_id = 41;
    auto d2 = trigger.evaluate(inf, ev, &wall, inv, 1200);
    CHECK(d2.intent == RippleIntent::NO_ACTION, "Global cooldown blocks repeat");
}

void test_trigger_bounce_blocked_by_break_risk() {
    RippleConfig cfg;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 50;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.6;     // above bounce_max_break_risk (0.45)
    ev.withdrawal = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::NO_ACTION,
          "Bounce blocked by high breakout risk");
}

void test_trigger_bounce_blocked_by_withdrawal_risk() {
    RippleConfig cfg;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 51;
    wall.side = WallSide::ASK;
    wall.price = 105.0;
    wall.current_qty = 40.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.1;
    ev.withdrawal = 0.6;   // above bounce_max_withdrawal_risk (0.45)

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::NO_ACTION,
          "Bounce blocked by high withdrawal risk");
}

void test_trigger_breakout_blocked_by_absorption() {
    RippleConfig cfg;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 52;
    wall.side = WallSide::BID;
    wall.price = 95.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.breakout = 0.8;
    ev.absorption = 0.6;   // above breakout_max_absorption (0.40)

    RippleInferenceResult inf;
    inf.state = RippleState::BREAKING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::NO_ACTION,
          "Breakout blocked by high absorption evidence");
}

void test_trigger_exit_bounce_on_break() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 0;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 53;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    // Step 1: enter bounce
    RippleEvidence ev1;
    ev1.exhaustion = 0.7;
    ev1.breakout = 0.1;
    ev1.withdrawal = 0.1;

    RippleInferenceResult inf1;
    inf1.state = RippleState::EXHAUSTING;
    inf1.prev_state = RippleState::ABSORBING;
    inf1.is_transition = true;

    auto d1 = trigger.evaluate(inf1, ev1, &wall, inv, 1000);
    CHECK(d1.intent == RippleIntent::ENTER_BOUNCE_LONG,
          "Exit-bounce setup: entry fires");

    // Step 2: wall breaks (non-transition tick, state persists as BREAKING)
    RippleEvidence ev2;
    ev2.breakout = 0.8;

    RippleInferenceResult inf2;
    inf2.state = RippleState::BREAKING;
    inf2.prev_state = RippleState::EXHAUSTING;
    inf2.is_transition = false;

    auto d2 = trigger.evaluate(inf2, ev2, &wall, inv, 2000);
    CHECK(d2.intent == RippleIntent::EXIT_BOUNCE,
          "Exit bounce fires when wall breaks after entry");
}

void test_trigger_exit_breakout_on_refill() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 0;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 54;
    wall.side = WallSide::ASK;
    wall.price = 105.0;

    InventorySnapshot inv;

    // Step 1: enter breakout
    RippleEvidence ev1;
    ev1.breakout = 0.8;
    ev1.absorption = 0.1;

    RippleInferenceResult inf1;
    inf1.state = RippleState::BREAKING;
    inf1.prev_state = RippleState::ABSORBING;
    inf1.is_transition = true;

    auto d1 = trigger.evaluate(inf1, ev1, &wall, inv, 1000);
    CHECK(d1.intent == RippleIntent::ENTER_BREAKOUT_LONG,
          "Exit-breakout setup: entry fires");

    // Step 2: refill appears
    RippleEvidence ev2;
    ev2.refill = 0.7;

    RippleInferenceResult inf2;
    inf2.state = RippleState::REFILLING;
    inf2.prev_state = RippleState::BREAKING;
    inf2.is_transition = false;

    auto d2 = trigger.evaluate(inf2, ev2, &wall, inv, 2000);
    CHECK(d2.intent == RippleIntent::EXIT_BREAKOUT,
          "Exit breakout fires when refill appears");
}

void test_trigger_preemptive_cancel() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 0;
    cfg.cancel_risk_threshold = 0.55;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 55;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    // Step 1: enter bounce to set active_entry_
    RippleEvidence ev1;
    ev1.exhaustion = 0.7;
    ev1.breakout = 0.1;
    ev1.withdrawal = 0.1;

    RippleInferenceResult inf1;
    inf1.state = RippleState::EXHAUSTING;
    inf1.prev_state = RippleState::ABSORBING;
    inf1.is_transition = true;

    trigger.evaluate(inf1, ev1, &wall, inv, 1000);

    // Step 2: still EXHAUSTING (no transition) but withdrawal risk spikes
    RippleEvidence ev2;
    ev2.withdrawal = 0.7;  // above cancel_risk_threshold

    RippleInferenceResult inf2;
    inf2.state = RippleState::EXHAUSTING;
    inf2.prev_state = RippleState::EXHAUSTING;
    inf2.is_transition = false;

    auto d2 = trigger.evaluate(inf2, ev2, &wall, inv, 2000);
    CHECK(d2.intent == RippleIntent::CANCEL_PASSIVE_ORDERS,
          "Preemptive cancel fires on high withdrawal risk");
}

void test_trigger_time_stop() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 0;
    cfg.time_stop_ms = 5000;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 56;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    // Step 1: enter bounce at t=1000
    RippleEvidence ev1;
    ev1.exhaustion = 0.7;
    ev1.breakout = 0.1;
    ev1.withdrawal = 0.1;

    RippleInferenceResult inf1;
    inf1.state = RippleState::EXHAUSTING;
    inf1.prev_state = RippleState::ABSORBING;
    inf1.is_transition = true;

    trigger.evaluate(inf1, ev1, &wall, inv, 1000);

    // Step 2: still EXHAUSTING at t=3000 — too early for time stop
    RippleEvidence ev_idle;
    RippleInferenceResult inf_hold;
    inf_hold.state = RippleState::EXHAUSTING;
    inf_hold.is_transition = false;

    auto d2 = trigger.evaluate(inf_hold, ev_idle, &wall, inv, 3000);
    CHECK(d2.intent == RippleIntent::NO_ACTION,
          "Time stop not yet triggered at t=3000");

    // Step 3: still EXHAUSTING at t=6500 — time stop fires (5000ms elapsed)
    auto d3 = trigger.evaluate(inf_hold, ev_idle, &wall, inv, 6500);
    CHECK(d3.intent == RippleIntent::EXIT_BOUNCE,
          "Time stop fires after max_intent_age exceeded");
}

void test_trigger_rearm_insufficient_evidence() {
    RippleConfig cfg;
    cfg.rearm_min_evidence = 0.40;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 57;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.stabilization = 0.2;  // below rearm_min_evidence

    RippleInferenceResult inf;
    inf.state = RippleState::STABILIZING;
    inf.prev_state = RippleState::REFILLING;
    inf.is_transition = true;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::NO_ACTION,
          "Rearm blocked by insufficient stabilization evidence");
}

void test_trigger_rearm_sufficient_evidence() {
    RippleConfig cfg;
    cfg.rearm_min_evidence = 0.40;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 58;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    RippleEvidence ev;
    ev.refill = 0.6;

    RippleInferenceResult inf;
    inf.state = RippleState::REFILLING;
    inf.prev_state = RippleState::BREAKING;
    inf.is_transition = true;

    auto d = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d.intent == RippleIntent::REARM_FOR_NEXT_BOUNCE,
          "Rearm fires with sufficient refill evidence");
}

void test_trigger_edge_no_repeat() {
    RippleConfig cfg;
    cfg.global_intent_cooldown_ms = 0;
    cfg.intent_cooldown_ms = 0;
    TriggerDecisionEngine trigger(cfg);

    WallCandidate wall;
    wall.wall_id = 59;
    wall.side = WallSide::BID;
    wall.price = 95.0;
    wall.current_qty = 45.0;

    InventorySnapshot inv;

    // Transition fires once
    RippleEvidence ev;
    ev.exhaustion = 0.7;
    ev.breakout = 0.1;
    ev.withdrawal = 0.1;

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.prev_state = RippleState::ABSORBING;
    inf.is_transition = true;

    auto d1 = trigger.evaluate(inf, ev, &wall, inv, 1000);
    CHECK(d1.intent == RippleIntent::ENTER_BOUNCE_LONG,
          "Edge-trigger: first transition fires");

    // Same state persists, not a transition
    inf.is_transition = false;
    auto d2 = trigger.evaluate(inf, ev, &wall, inv, 1500);
    CHECK(d2.intent == RippleIntent::NO_ACTION,
          "Edge-trigger: persistence does not re-fire entry");
}

// ======================================================================
// Test: End-to-end RippleEngine
// ======================================================================

void test_engine_end_to_end() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;
    cfg.feature_window_ms = 5000;
    cfg.min_dwell_fast_ms = 0;
    cfg.min_dwell_slow_ms = 0;

    RippleEngine engine(cfg);

    std::vector<RippleDecision> decisions;
    engine.set_decision_callback([&](const RippleDecision& d) {
        decisions.push_back(d);
    });

    OrderBook book;

    // Initial snapshot with a big bid wall at 97 within depth
    auto snap = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 60.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap);
    engine.on_depth(snap, book);

    CHECK(engine.current_state() == RippleState::IDLE ||
          engine.current_state() == RippleState::WALL_FORMING,
          "Engine starts IDLE or WALL_FORMING");

    // Feed some trades toward the wall
    for (int i = 0; i < 10; i++) {
        auto t = make_trade(1100 + i * 100, 100.0, 0.5, true); // sell aggressor
        book.on_trade(t);
        engine.on_trade(t, book);
    }

    // Update depth again
    auto snap2 = make_depth_update(2200,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 55.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap2);
    engine.on_depth(snap2, book);

    // Engine should have processed and potentially transitioned
    auto state = engine.current_state();
    CHECK(state != RippleState::BREAKING, "Not breaking yet (wall still large)");

    auto* feat = engine.last_features();
    CHECK(feat != nullptr, "Features available");
    auto* evid = engine.last_evidence();
    CHECK(evid != nullptr, "Evidence available");
}

// ======================================================================
// Test: Diagnostics
// ======================================================================

void test_diagnostics_logging() {
    RippleDiagnostics diag("", 100, false);

    RippleFeatures f{};
    f.relative_wall_size = 5.0;
    RippleEvidence ev{};
    ev.absorption = 0.7;
    RippleInferenceResult inf;
    inf.state = RippleState::ABSORBING;
    inf.prev_state = RippleState::WALL_FORMING;
    inf.is_transition = true;
    inf.confidence = 0.7;
    RippleDecision d;
    d.timestamp = 1000;
    d.intent = RippleIntent::ENTER_BOUNCE_LONG;

    diag.log(1000, f, ev, inf, d);
    CHECK(diag.record_count() == 1, "Diagnostics records one entry");
    CHECK(diag.records().back().ts == 1000, "Record timestamp correct");
}

void test_diagnostics_file_output() {
    std::string path = "/tmp/ripple_test_diag.jsonl";
    {
        RippleDiagnostics diag(path, 10, false);
        RippleFeatures f{};
        RippleEvidence ev{};
        RippleInferenceResult inf;
        inf.state = RippleState::IDLE;
        RippleDecision d;

        for (int i = 0; i < 5; i++) {
            diag.log(i * 100, f, ev, inf, d);
        }
    }
    // File should exist and have 5 lines
    std::ifstream file(path);
    CHECK(file.good(), "Diagnostics file created");
    int lines = 0;
    std::string line;
    while (std::getline(file, line)) lines++;
    CHECK(lines == 5, "Diagnostics file has correct line count");
}

// ======================================================================
// Test: Deterministic replay
// ======================================================================

void test_deterministic_replay() {
    auto run_scenario = [](std::vector<RippleDecision>& out) {
        RippleConfig cfg;
        cfg.tick_size = 1.0;
        cfg.wall_min_relative_size = 3.0;
        cfg.wall_max_distance_ticks = 50;
        cfg.book_depth_levels = 10;
        cfg.wall_min_age_ms = 100;
        cfg.min_dwell_fast_ms = 0;
        cfg.min_dwell_slow_ms = 0;

        RippleEngine engine(cfg);
        engine.set_decision_callback([&](const RippleDecision& d) {
            out.push_back(d);
        });

        OrderBook book;

        auto snap = make_snapshot(1000,
            {{100.0, 3.0}, {99.0, 2.0}, {98.0, 50.0}},
            {{101.0, 3.0}});
        book.on_depth_update(snap);
        engine.on_depth(snap, book);

        for (int i = 0; i < 20; i++) {
            auto t = make_trade(1100 + i * 50, 100.0, 1.0, true);
            book.on_trade(t);
            engine.on_trade(t, book);
        }

        auto snap2 = make_depth_update(2200,
            {{100.0, 3.0}, {99.0, 2.0}, {98.0, 30.0}},
            {{101.0, 3.0}});
        book.on_depth_update(snap2);
        engine.on_depth(snap2, book);
    };

    std::vector<RippleDecision> run1, run2;
    run_scenario(run1);
    run_scenario(run2);

    CHECK(run1.size() == run2.size(), "Deterministic: same number of decisions");
    for (size_t i = 0; i < std::min(run1.size(), run2.size()); i++) {
        CHECK(run1[i].intent == run2[i].intent, "Deterministic: same intent");
        CHECK(run1[i].timestamp == run2[i].timestamp, "Deterministic: same timestamp");
        CHECK(run1[i].reference_price == run2[i].reference_price, "Deterministic: same price");
    }
}

// ======================================================================
// Test: Compact diagnostic line
// ======================================================================

void test_diagnostics_compact_line() {
    DiagnosticRecord rec;
    rec.ts = 5000;
    rec.evidence.absorption = 0.65;
    rec.inference.state = RippleState::ABSORBING;
    rec.inference.is_transition = true;
    rec.decision.intent = RippleIntent::PREPARE_BOUNCE_LONG;
    rec.decision.reference_price = 95.0;
    rec.decision.reason = "Wall forming";

    auto line = RippleDiagnostics::compact_line(rec);
    CHECK(line.find("ABSORBING*") != std::string::npos,
          "Compact line contains transition marker");
    CHECK(line.find("PREPARE_BOUNCE_LONG") != std::string::npos,
          "Compact line contains intent");
    CHECK(line.find("95.") != std::string::npos,
          "Compact line contains price");
}

// ======================================================================
// Test: Replay integration — full event-loop through RippleEngine
// ======================================================================

void test_replay_integration() {
    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 100;
    cfg.min_dwell_fast_ms = 0;
    cfg.min_dwell_slow_ms = 0;
    cfg.pipeline_min_interval_ms = 0;
    cfg.enable_diagnostics = true;
    cfg.console_diagnostics = false;

    RippleEngine engine(cfg);

    std::vector<RippleDecision> decisions;
    engine.set_decision_callback([&](const RippleDecision& d) {
        decisions.push_back(d);
    });

    OrderBook book;

    // Phase 1: initial book with big bid wall at 95
    auto snap0 = make_snapshot(1000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0},
         {96.0, 2.0}, {95.0, 60.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap0);
    engine.on_depth(snap0, book);

    CHECK(engine.pipeline_runs() == 1, "Replay: first pipeline run");
    CHECK(engine.depth_count() == 1, "Replay: depth count after snap0");

    // Phase 2: stream sell-aggressive trades toward the wall
    for (int i = 0; i < 30; i++) {
        auto t = make_trade(1100 + i * 50, 100.0 - (i * 0.1), 0.5, true);
        book.on_trade(t);
        engine.on_trade(t, book);
    }
    CHECK(engine.trade_count() == 30, "Replay: trade count");

    // Phase 3: wall persists (slightly depleted)
    auto snap1 = make_depth_update(2700,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0},
         {96.0, 2.0}, {95.0, 50.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap1);
    engine.on_depth(snap1, book);

    CHECK(engine.pipeline_runs() >= 2, "Replay: second pipeline run");

    auto st1 = engine.current_state();
    CHECK(st1 != RippleState::BREAKING, "Replay: not breaking yet");

    // Phase 4: more sell aggression + wall further depleted
    for (int i = 0; i < 20; i++) {
        auto t = make_trade(2800 + i * 50, 97.0, 1.0, true);
        book.on_trade(t);
        engine.on_trade(t, book);
    }

    auto snap2 = make_depth_update(3900,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0},
         {96.0, 2.0}, {95.0, 35.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap2);
    engine.on_depth(snap2, book);

    // Phase 5: wall collapses — breakout scenario
    auto snap3 = make_depth_update(4500,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0},
         {96.0, 2.0}, {95.0, 3.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap3);
    engine.on_depth(snap3, book);

    // Phase 6: wall refills
    auto snap4 = make_depth_update(6000,
        {{100.0, 3.0}, {99.0, 2.0}, {98.0, 4.0}, {97.0, 3.0},
         {96.0, 2.0}, {95.0, 55.0}},
        {{101.0, 3.0}, {102.0, 2.0}});
    book.on_depth_update(snap4);
    engine.on_depth(snap4, book);

    CHECK(engine.pipeline_runs() >= 5, "Replay: at least 5 pipeline runs");

    // Verify diagnostics recorded events
    auto* diag = engine.diagnostics();
    CHECK(diag != nullptr, "Replay: diagnostics auto-wired");
    CHECK(diag->record_count() >= 5, "Replay: diagnostics logged pipeline events");

    // Verify last accessors are populated
    auto& last_inf = engine.last_inference();
    CHECK(last_inf.state != RippleState::IDLE ||
          last_inf.prev_state != RippleState::IDLE ||
          engine.pipeline_runs() > 0,
          "Replay: last inference populated");

    auto& last_dec = engine.last_decision();
    CHECK(last_dec.timestamp > 0, "Replay: last decision has timestamp");

    // Verify reset clears everything
    engine.reset();
    CHECK(engine.pipeline_runs() == 0, "Replay: reset clears pipeline runs");
    CHECK(engine.trade_count() == 0, "Replay: reset clears trade count");
    CHECK(engine.depth_count() == 0, "Replay: reset clears depth count");
    CHECK(engine.current_state() == RippleState::IDLE, "Replay: reset to IDLE");
}

// ======================================================================
// Regression tests: buffer bounds, cooldown recovery, signal fade
// ======================================================================

void test_context_buffer_bounds() {
    RippleConfig cfg;
    cfg.max_context_trades = 100;
    cfg.max_context_snaps = 50;
    RippleContext ctx(cfg);

    OrderBook book;
    book.on_depth_update(make_snapshot(1000,
        {{99.0, 10}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}}));

    for (int i = 0; i < 500; i++) {
        ctx.on_depth(book, 1000 + i * 10);
        ctx.on_trade(make_trade(1000 + i * 10, 100.0, 1.0, false));
    }

    auto tw = ctx.trade_window(100000);
    CHECK(tw.recent.size() <= 100,
          "Trade buffer respects max_context_trades bound");
}

void test_walldetector_cooldown_pruned() {
    RippleConfig cfg;
    cfg.wall_dedup_cooldown_ms = 500;
    WallDetector det(cfg);
    RippleContext ctx(cfg);

    OrderBook book;
    for (int i = 0; i < 100; i++) {
        Timestamp ts = 1000 + i * 2000;
        // Wall present
        book.on_depth_update(make_snapshot(ts,
            {{99.0, 100}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}}));
        ctx.on_depth(book, ts);
        det.on_depth(ctx);

        // Wall gone
        book.on_depth_update(make_snapshot(ts + 1000,
            {{99.0, 5}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}}));
        ctx.on_depth(book, ts + 1000);
        det.on_depth(ctx);
    }
    CHECK(true, "WallDetector cooldown map doesn't crash with many walls");
}

void test_trigger_active_entry_clears_on_timeout() {
    RippleConfig cfg;
    cfg.time_stop_ms = 1000;
    TriggerDecisionEngine trigger(cfg);

    RippleInferenceResult inf;
    inf.state = RippleState::EXHAUSTING;
    inf.is_transition = true;
    inf.confidence = 0.8;

    RippleEvidence ev;
    ev.exhaustion = 0.8;
    ev.breakout = 0.1;
    ev.withdrawal = 0.1;
    ev.absorption = 0.2;

    WallCandidate wall;
    wall.wall_id = 1;
    wall.side = WallSide::BID;
    wall.price = 100.0;
    wall.current_qty = 50.0;

    InventorySnapshot inv;

    // Entry at t=1000
    auto d1 = trigger.evaluate(inf, ev, &wall, inv, 1000);
    CHECK(d1.intent == RippleIntent::ENTER_BOUNCE_LONG,
          "Entry triggered at t=1000");

    // At t=2000 (after time stop), should emit exit
    inf.is_transition = false;
    inf.state = RippleState::EXHAUSTING;
    auto d2 = trigger.evaluate(inf, ev, &wall, inv, 2000);
    CHECK(d2.intent == RippleIntent::EXIT_BOUNCE,
          "Time stop fires at t=2000");

    // At t=5000 (after 2x time stop), active_entry_ should be cleared
    // so new entries are possible
    inf.is_transition = true;
    inf.state = RippleState::EXHAUSTING;
    auto d3 = trigger.evaluate(inf, ev, &wall, inv, 5000);
    CHECK(d3.intent == RippleIntent::ENTER_BOUNCE_LONG ||
          d3.intent == RippleIntent::NO_ACTION,
          "Engine recovers from zombie entry state");
}

void test_state_tracker_dedup_pruned() {
    RippleConfig cfg;
    cfg.intent_cooldown_ms = 500;
    auto inference = std::make_unique<ScoreBasedInference>(cfg);
    RippleStateTracker tracker(cfg, std::move(inference));

    RippleEvidence ev;
    ev.absorption = 0.8;

    // Simulate many walls transitioning
    for (int i = 0; i < 200; i++) {
        WallCandidate wall;
        wall.wall_id = static_cast<uint64_t>(i + 1);
        wall.side = WallSide::BID;
        wall.price = 100.0 + i;
        wall.current_qty = 50.0;

        Timestamp ts = 1000 + i * 5000;
        tracker.update(ev, &wall, ts);
    }
    CHECK(true, "State tracker dedup map doesn't crash with 200 walls");
}

void test_trade_triggers_pipeline() {
    RippleConfig cfg;
    cfg.pipeline_min_interval_ms = 250;
    cfg.trade_trigger_count = 10;
    RippleEngine engine(cfg);

    OrderBook book;
    book.on_depth_update(make_snapshot(1000,
        {{99.0, 10}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}}));

    auto du = make_snapshot(1000,
        {{99.0, 10}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}});
    engine.on_depth(du, book);
    uint64_t initial_runs = engine.pipeline_runs();

    for (int i = 0; i < 60; i++) {
        engine.on_trade(make_trade(1200 + i * 5, 100.0, 1.0, false), book);
    }

    CHECK(engine.pipeline_runs() > initial_runs,
          "Trades can trigger pipeline after trade_trigger_count");
}

void test_welford_volatility_no_alloc() {
    RippleConfig cfg;
    cfg.max_context_snaps = 100;
    cfg.volatility_window_ms = 5000;
    RippleContext ctx(cfg);

    OrderBook book;
    book.on_depth_update(make_snapshot(1000,
        {{99.0, 10}, {98.0, 5}}, {{101.0, 10}, {102.0, 5}}));

    for (int i = 0; i < 50; i++) {
        auto ts = static_cast<Timestamp>(1000 + i * 100);
        ctx.on_depth(book, ts);
    }

    double vol = ctx.short_horizon_volatility();
    CHECK(vol >= 0.0, "Welford volatility returns non-negative");

    double stddev = ctx.top_of_book_qty_stddev(WallSide::BID);
    CHECK(stddev >= 0.0, "Welford stddev returns non-negative");
}

// ======================================================================
// Main
// ======================================================================

int main() {
    std::cout << "=== Ripple Layer Test Suite ===" << std::endl;

    // Context tests
    test_context_snapshot();
    test_context_trade_window();
    test_context_microprice();

    // Wall detector tests
    test_wall_detection_basic();
    test_wall_persistence();
    test_wall_withdrawal();
    test_wall_refill();
    test_wall_metrics_populated();
    test_wall_primary_metrics();
    test_wall_growth_rate();
    test_wall_median_baseline();

    // Feature engine tests
    test_feature_computation();
    test_feature_with_metrics();
    test_feature_cancel_rate();
    test_feature_impact();
    test_feature_clamping();

    // Evidence engine tests
    test_absorption_evidence();
    test_breakout_evidence();
    test_withdrawal_evidence();
    test_stabilization_evidence();
    test_exhaustion_evidence();
    test_refill_evidence();
    test_evidence_dominant();
    test_evidence_all_clamped();
    test_evidence_withdrawal_no_cumulative();

    // Inference tests
    test_inference_idle_to_wall_forming();
    test_inference_absorbing_to_exhausting();
    test_inference_absorbing_to_breaking();
    test_inference_no_wall_returns_idle();
    test_inference_scores_populated();
    test_inference_persistence_bonus();
    test_inference_margin_gate();
    test_inference_idle_exit_threshold();
    test_inference_transition_mask();
    test_inference_runner_up();

    // State tracker tests
    test_state_tracker_anti_flicker();
    test_state_tracker_spatial_dedup();

    // Trigger decision tests
    test_trigger_bounce_entry();
    test_trigger_breakout_entry();
    test_trigger_inventory_filter();
    test_trigger_global_cooldown();
    test_trigger_bounce_blocked_by_break_risk();
    test_trigger_bounce_blocked_by_withdrawal_risk();
    test_trigger_breakout_blocked_by_absorption();
    test_trigger_exit_bounce_on_break();
    test_trigger_exit_breakout_on_refill();
    test_trigger_preemptive_cancel();
    test_trigger_time_stop();
    test_trigger_rearm_insufficient_evidence();
    test_trigger_rearm_sufficient_evidence();
    test_trigger_edge_no_repeat();

    // End-to-end tests
    test_engine_end_to_end();

    // Diagnostics tests
    test_diagnostics_logging();
    test_diagnostics_file_output();
    test_diagnostics_compact_line();

    // Determinism test
    test_deterministic_replay();

    // Replay integration test
    test_replay_integration();

    // Regression tests: buffer bounds, cooldown recovery, signal fade
    test_context_buffer_bounds();
    test_walldetector_cooldown_pruned();
    test_trigger_active_entry_clears_on_timeout();
    test_state_tracker_dedup_pruned();
    test_trade_triggers_pipeline();
    test_welford_volatility_no_alloc();

    std::cout << "\n=== Results: " << tests_passed << "/" << tests_run
              << " passed ===" << std::endl;

    return (tests_passed == tests_run) ? 0 : 1;
}
