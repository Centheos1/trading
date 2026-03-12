// Phase 3 — Liquidity Map and VP/CVD integration tests.
// Validates: level collection, scoring, voids, destinations, scale-out,
//            CVD divergence boost, and determinism.

#include <cassert>
#include <cmath>
#include <cstdio>
#include <vector>
#include <string>
#include <algorithm>

#include "../LiquidityMapEngine.h"
#include "../TradeLifecycleEngine.h"
#include "../RippleEvidenceEngine.h"
#include "../../Schemas.h"

using namespace orderflow;
using namespace orderflow::ripple;

static int tests_run    = 0;
static int checks_total = 0;

#define CHECK(cond) do { \
    ++checks_total; \
    if (!(cond)) { \
        std::fprintf(stderr, "FAIL: %s  [%s:%d]\n", #cond, __FILE__, __LINE__); \
        assert(false); \
    } \
} while(0)

#define RUN(fn) do { \
    std::printf("  %-52s ", #fn); \
    fn(); \
    ++tests_run; \
    std::printf("OK\n"); \
} while(0)

// ===================================================================
//  Helpers
// ===================================================================

static WallCandidate make_wall(double price, double qty, WallSide side) {
    WallCandidate w;
    w.wall_id     = static_cast<uint64_t>(price * 100);
    w.side        = side;
    w.price       = price;
    w.initial_qty = qty;
    w.current_qty = qty;
    w.peak_qty    = qty;
    return w;
}

static WallMetrics make_metrics(double price, double rel_size, WallSide side, bool active = true) {
    WallMetrics m;
    m.wall_id       = static_cast<uint64_t>(price * 100);
    m.side          = side;
    m.price         = price;
    m.relative_size = rel_size;
    m.is_active     = active;
    return m;
}

static VolumeNode make_vnode(double price, double vol) {
    return {price, vol, vol * 0.5, vol * 0.5};
}

static Trade make_trade(double price, double qty, bool is_buyer_maker, int64_t ts = 1000) {
    Trade t;
    t.timestamp      = ts;
    t.price          = price;
    t.quantity       = qty;
    t.is_buyer_maker = is_buyer_maker;
    return t;
}

static LiquidityMapConfig default_cfg() {
    LiquidityMapConfig c;
    c.max_levels = 100;
    return c;
}

// ===================================================================
//  LiquidityMapEngine tests
// ===================================================================

void test_empty_input() {
    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.timestamp = 1000;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    eng.update(in);

    CHECK(eng.level_count() == 0);
    CHECK(eng.snapshot().void_corridors.empty());
    CHECK(eng.snapshot().timestamp == 1000);
}

void test_wall_only_levels() {
    std::vector<WallCandidate> walls = {
        make_wall(99.0, 500.0, WallSide::BID),
        make_wall(101.0, 300.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(99.0, 3.0, WallSide::BID),
        make_metrics(101.0, 2.0, WallSide::ASK),
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 2000;
    eng.update(in);

    CHECK(eng.level_count() == 2);

    const auto& snap = eng.snapshot();
    bool found_bid = false, found_ask = false;
    for (const auto& lv : snap.levels) {
        if (lv.type == LevelType::WALL_BID) { found_bid = true; CHECK(lv.price == 99.0); }
        if (lv.type == LevelType::WALL_ASK) { found_ask = true; CHECK(lv.price == 101.0); }
    }
    CHECK(found_bid);
    CHECK(found_ask);
    CHECK(snap.nearest_bid_wall == 99.0);
    CHECK(snap.nearest_ask_wall == 101.0);
}

void test_inactive_walls_excluded() {
    std::vector<WallCandidate> walls = { make_wall(99.0, 500.0, WallSide::BID) };
    std::vector<WallMetrics> mets = { make_metrics(99.0, 3.0, WallSide::BID, false) };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 2000;
    eng.update(in);

    CHECK(eng.level_count() == 0);
}

void test_profile_levels_hvn_lvn_poc() {
    std::vector<VolumeNode> prof = {
        make_vnode(98.0,  50.0),   // low → LVN
        make_vnode(99.0, 200.0),   // high → HVN
        make_vnode(100.0, 350.0),  // POC
        make_vnode(101.0, 120.0),  // mid-range → excluded
        make_vnode(102.0, 250.0),  // high → HVN
    };

    LiquidityMapConfig cfg = default_cfg();
    cfg.hvn_volume_ratio = 1.5;
    cfg.lvn_volume_ratio = 0.5;

    LiquidityMapEngine eng(cfg);
    LiquidityMapInput in;
    in.profile   = &prof;
    in.poc_price = 100.0;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 3000;
    eng.update(in);

    // Median of {50, 120, 200, 250, 350} = 200
    // HVN threshold = 200 * 1.5 = 300 → only 350 (POC at 100.0 already matches POC)
    // LVN threshold = 200 * 0.5 = 100 → only 50 (at 98.0)
    // 99.0 (200) is below 300, above 100 → mid-range → excluded
    // 101.0 (120) → above 100 and below 300 → mid-range → excluded
    // 102.0 (250) → below 300, above 100 → mid-range → excluded
    // So: POC at 100.0, LVN at 98.0, and that's it

    bool found_poc = false, found_lvn = false;
    for (const auto& lv : eng.snapshot().levels) {
        if (lv.type == LevelType::POC) { found_poc = true; CHECK(lv.price == 100.0); }
        if (lv.type == LevelType::LVN) { found_lvn = true; CHECK(lv.price == 98.0); }
    }
    CHECK(found_poc);
    CHECK(found_lvn);
    CHECK(eng.snapshot().poc == 100.0);
}

void test_structural_anchor_vwap() {
    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.vwap      = 100.5;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 4000;
    eng.update(in);

    CHECK(eng.level_count() == 1);
    CHECK(eng.snapshot().levels[0].type == LevelType::VWAP);
    CHECK(eng.snapshot().levels[0].price == 100.5);
    CHECK(eng.snapshot().vwap == 100.5);
}

void test_flow_merged_into_wall_level() {
    std::vector<WallCandidate> walls = { make_wall(100.0, 500.0, WallSide::BID) };
    std::vector<WallMetrics> mets = { make_metrics(100.0, 3.0, WallSide::BID) };

    std::vector<Trade> trades = {
        make_trade(100.0, 10.0, false),  // buy aggression → +10
        make_trade(100.0, 5.0, true),    // sell aggression → -5
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls       = &walls;
    in.metrics     = &mets;
    in.trades_begin = trades.data();
    in.trades_end   = trades.data() + trades.size();
    in.mid_price   = 100.0;
    in.sigma_P     = 0.5;
    in.timestamp   = 5000;
    eng.update(in);

    CHECK(eng.level_count() == 1);
    CHECK(std::abs(eng.snapshot().levels[0].net_flow - 5.0) < 0.01);
}

void test_void_corridor_detection() {
    std::vector<WallCandidate> walls = {
        make_wall(90.0, 500.0, WallSide::BID),
        make_wall(110.0, 500.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(90.0, 3.0, WallSide::BID),
        make_metrics(110.0, 3.0, WallSide::ASK),
    };

    LiquidityMapConfig cfg = default_cfg();
    cfg.void_min_gap_sigma = 3.0;

    LiquidityMapEngine eng(cfg);
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 1.0;  // gap = 20 > 3.0 * 1.0
    in.timestamp = 6000;
    eng.update(in);

    CHECK(!eng.snapshot().void_corridors.empty());
    CHECK(eng.snapshot().void_corridors[0].lo == 90.0);
    CHECK(eng.snapshot().void_corridors[0].hi == 110.0);
}

void test_no_void_when_levels_close() {
    std::vector<WallCandidate> walls = {
        make_wall(100.0, 500.0, WallSide::BID),
        make_wall(100.5, 500.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(100.0, 3.0, WallSide::BID),
        make_metrics(100.5, 3.0, WallSide::ASK),
    };

    LiquidityMapConfig cfg = default_cfg();
    cfg.void_min_gap_sigma = 3.0;

    LiquidityMapEngine eng(cfg);
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 1.0;  // gap = 0.5 < 3.0 * 1.0
    in.timestamp = 7000;
    eng.update(in);

    CHECK(eng.snapshot().void_corridors.empty());
}

void test_hold_score_computation() {
    std::vector<WallCandidate> walls = {
        make_wall(99.0, 1000.0, WallSide::BID),  // large wall
        make_wall(101.0, 100.0, WallSide::ASK),   // small wall
    };
    std::vector<WallMetrics> mets = {
        make_metrics(99.0, 5.0, WallSide::BID),   // high quality
        make_metrics(101.0, 1.0, WallSide::ASK),  // low quality
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.imbalance = 0.0;
    in.timestamp = 8000;
    eng.update(in);

    double bid_hold = eng.get_hold_score(99.0);
    double ask_hold = eng.get_hold_score(101.0);
    CHECK(bid_hold > ask_hold);
    CHECK(bid_hold > 0.0);
    CHECK(bid_hold <= 1.0);
    CHECK(ask_hold >= 0.0);
    CHECK(ask_hold <= 1.0);
}

void test_dest_score_computation() {
    std::vector<VolumeNode> prof = {
        make_vnode(99.0, 30.0),   // LVN → low dest
        make_vnode(100.0, 500.0), // POC → high dest
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.profile   = &prof;
    in.poc_price = 100.0;
    in.vwap      = 100.0;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 9000;
    eng.update(in);

    double poc_dest = eng.get_dest_score(100.0);
    double lvn_dest = eng.get_dest_score(99.0);
    CHECK(poc_dest > lvn_dest);
    CHECK(poc_dest > 0.0);
    CHECK(poc_dest <= 1.0);
}

void test_destinations_in_direction_long() {
    std::vector<WallCandidate> walls = {
        make_wall(98.0, 500.0, WallSide::BID),
        make_wall(103.0, 500.0, WallSide::ASK),
        make_wall(105.0, 500.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(98.0, 3.0, WallSide::BID),
        make_metrics(103.0, 3.0, WallSide::ASK),
        make_metrics(105.0, 3.0, WallSide::ASK),
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 10000;
    eng.update(in);

    auto dests = eng.get_destinations_in_direction(TradeSide::LONG, 100.0);
    CHECK(dests.size() == 2);
    CHECK(dests[0].price == 103.0 || dests[0].price == 105.0);
    for (const auto& d : dests)
        CHECK(d.price > 100.0);
}

void test_destinations_in_direction_short() {
    std::vector<WallCandidate> walls = {
        make_wall(95.0, 500.0, WallSide::BID),
        make_wall(97.0, 500.0, WallSide::BID),
        make_wall(103.0, 500.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(95.0, 3.0, WallSide::BID),
        make_metrics(97.0, 3.0, WallSide::BID),
        make_metrics(103.0, 3.0, WallSide::ASK),
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 10000;
    eng.update(in);

    auto dests = eng.get_destinations_in_direction(TradeSide::SHORT, 100.0);
    CHECK(dests.size() == 2);
    for (const auto& d : dests)
        CHECK(d.price < 100.0);
}

void test_max_levels_trim() {
    std::vector<WallCandidate> walls;
    std::vector<WallMetrics> mets;
    for (int i = 0; i < 200; ++i) {
        double p = 50.0 + i * 0.5;
        walls.push_back(make_wall(p, 100.0, (i % 2 == 0) ? WallSide::BID : WallSide::ASK));
        mets.push_back(make_metrics(p, 2.0, (i % 2 == 0) ? WallSide::BID : WallSide::ASK));
    }

    LiquidityMapConfig cfg = default_cfg();
    cfg.max_levels = 50;

    LiquidityMapEngine eng(cfg);
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 11000;
    eng.update(in);

    CHECK(eng.level_count() <= 50);
}

void test_reset() {
    std::vector<WallCandidate> walls = { make_wall(99.0, 500.0, WallSide::BID) };
    std::vector<WallMetrics> mets = { make_metrics(99.0, 3.0, WallSide::BID) };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls = &walls; in.metrics = &mets;
    in.mid_price = 100.0; in.sigma_P = 0.5; in.timestamp = 1000;
    eng.update(in);
    CHECK(eng.level_count() > 0);

    eng.reset();
    CHECK(eng.level_count() == 0);
    CHECK(eng.snapshot().void_corridors.empty());
}

void test_determinism() {
    std::vector<WallCandidate> walls = {
        make_wall(99.0, 500.0, WallSide::BID),
        make_wall(101.0, 300.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(99.0, 3.0, WallSide::BID),
        make_metrics(101.0, 2.0, WallSide::ASK),
    };
    std::vector<VolumeNode> prof = {
        make_vnode(99.0, 200.0),
        make_vnode(100.0, 400.0),
        make_vnode(101.0, 150.0),
    };
    std::vector<Trade> trades = {
        make_trade(99.0, 10.0, false),
        make_trade(101.0, 5.0, true),
    };

    auto run = [&]() {
        LiquidityMapEngine eng(default_cfg());
        LiquidityMapInput in;
        in.walls   = &walls; in.metrics = &mets;
        in.profile = &prof; in.poc_price = 100.0;
        in.trades_begin = trades.data();
        in.trades_end   = trades.data() + trades.size();
        in.mid_price = 100.0; in.sigma_P = 0.5;
        in.vwap = 100.0; in.timestamp = 1000;
        eng.update(in);
        return eng.snapshot();
    };

    auto s1 = run();
    auto s2 = run();

    CHECK(s1.levels.size() == s2.levels.size());
    for (size_t i = 0; i < s1.levels.size(); ++i) {
        CHECK(s1.levels[i].price      == s2.levels[i].price);
        CHECK(s1.levels[i].hold_score == s2.levels[i].hold_score);
        CHECK(s1.levels[i].dest_score == s2.levels[i].dest_score);
        CHECK(s1.levels[i].net_flow   == s2.levels[i].net_flow);
    }
    CHECK(s1.void_corridors.size() == s2.void_corridors.size());
}

void test_hold_score_zero_for_non_wall() {
    std::vector<VolumeNode> prof = { make_vnode(100.0, 500.0) };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.profile   = &prof;
    in.poc_price = 100.0;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 12000;
    eng.update(in);

    CHECK(eng.get_hold_score(100.0) == 0.0);
}

void test_mixed_wall_and_profile() {
    std::vector<WallCandidate> walls = { make_wall(99.0, 500.0, WallSide::BID) };
    std::vector<WallMetrics> mets = { make_metrics(99.0, 3.0, WallSide::BID) };
    std::vector<VolumeNode> prof = {
        make_vnode(100.0, 500.0),  // POC
        make_vnode(98.0, 20.0),    // LVN
    };

    LiquidityMapEngine eng(default_cfg());
    LiquidityMapInput in;
    in.walls   = &walls; in.metrics = &mets;
    in.profile = &prof; in.poc_price = 100.0;
    in.mid_price = 100.0; in.sigma_P = 0.5;
    in.timestamp = 13000;
    eng.update(in);

    CHECK(eng.level_count() == 3);  // WALL_BID + POC + LVN

    bool has_wall = false, has_poc = false, has_lvn = false;
    for (const auto& lv : eng.snapshot().levels) {
        if (lv.type == LevelType::WALL_BID) has_wall = true;
        if (lv.type == LevelType::POC)      has_poc = true;
        if (lv.type == LevelType::LVN)      has_lvn = true;
    }
    CHECK(has_wall);
    CHECK(has_poc);
    CHECK(has_lvn);
}

// ===================================================================
//  Additional LiquidityMapEngine tests (review fixes)
// ===================================================================

void test_wall_min_quality_filter() {
    std::vector<WallCandidate> walls = {
        make_wall(99.0, 500.0, WallSide::BID),
        make_wall(101.0, 300.0, WallSide::ASK),
    };
    std::vector<WallMetrics> mets = {
        make_metrics(99.0, 0.5, WallSide::BID),   // above threshold
        make_metrics(101.0, 0.1, WallSide::ASK),  // below threshold
    };

    LiquidityMapConfig cfg = default_cfg();
    cfg.wall_min_quality = 0.3;

    LiquidityMapEngine eng(cfg);
    LiquidityMapInput in;
    in.walls     = &walls;
    in.metrics   = &mets;
    in.mid_price = 100.0;
    in.sigma_P   = 0.5;
    in.timestamp = 14000;
    eng.update(in);

    CHECK(eng.level_count() == 1);
    CHECK(eng.snapshot().levels[0].type == LevelType::WALL_BID);
    CHECK(eng.snapshot().levels[0].price == 99.0);
}

// ===================================================================
//  CVD divergence boost tests
// ===================================================================

void test_cvd_divergence_boost_absorption() {
    RippleConfig cfg;
    cfg.weights.cvd_divergence_boost = 0.2;
    RippleEvidenceEngine ev_eng(cfg);

    WallCandidate wall = make_wall(99.0, 500.0, WallSide::BID);
    wall.peak_qty = 500.0;

    RippleFeatures f;
    f.relative_wall_size        = 3.0;
    f.wall_persistence_sec      = 5.0;
    f.aggressive_flow_imbalance = -0.3;

    // Without divergence
    f.cvd_divergence_detected = false;
    auto ev_base = ev_eng.compute(f, wall);

    // With bullish divergence (positive strength)
    f.cvd_divergence_detected  = true;
    f.cvd_divergence_strength  = 0.5;
    auto ev_boost = ev_eng.compute(f, wall);

    CHECK(ev_boost.absorption >= ev_base.absorption);
}

void test_cvd_divergence_boost_exhaustion() {
    RippleConfig cfg;
    cfg.weights.cvd_divergence_boost = 0.2;
    RippleEvidenceEngine ev_eng(cfg);

    WallCandidate wall = make_wall(101.0, 500.0, WallSide::ASK);
    wall.peak_qty = 500.0;

    RippleFeatures f;
    f.relative_wall_size        = 3.0;
    f.aggressive_flow_imbalance = 0.3;

    f.cvd_divergence_detected = false;
    auto ev_base = ev_eng.compute(f, wall);

    // Bearish divergence (negative strength)
    f.cvd_divergence_detected  = true;
    f.cvd_divergence_strength  = -0.5;
    auto ev_boost = ev_eng.compute(f, wall);

    CHECK(ev_boost.exhaustion >= ev_base.exhaustion);
}

void test_cvd_no_divergence_no_change() {
    RippleConfig cfg;
    cfg.weights.cvd_divergence_boost = 0.2;
    RippleEvidenceEngine ev_eng(cfg);

    WallCandidate wall = make_wall(99.0, 500.0, WallSide::BID);
    wall.peak_qty = 500.0;

    RippleFeatures f;
    f.relative_wall_size   = 3.0;
    f.cvd_divergence_detected = false;
    f.cvd_divergence_strength = 0.0;

    auto ev1 = ev_eng.compute(f, wall);
    auto ev2 = ev_eng.compute(f, wall);

    CHECK(ev1.absorption == ev2.absorption);
    CHECK(ev1.exhaustion == ev2.exhaustion);
}

// ===================================================================
//  Scale-out plan tests
// ===================================================================

static TickContext make_tick(double microprice, double sigma_P = 1.0, int64_t ts = 1000) {
    TickContext tc;
    tc.timestamp        = ts;
    tc.microprice       = microprice;
    tc.last_trade_price = microprice;
    tc.cvd_slope        = 0.1;
    tc.trade_rate       = 50.0;
    tc.impact           = 0.001;
    tc.imbalance        = 0.0;
    tc.recent_sigma_P   = sigma_P;
    return tc;
}

static RiskBudgetSnapshot default_risk() {
    RiskBudgetSnapshot r;
    r.es_budget       = 10000.0;
    r.risk_multiplier = 1.0;
    return r;
}

static PermissionSet default_perms() {
    return DefaultWaveSnapshot::make().permissions;
}

void test_scale_out_no_trigger_in_confirmation() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 100.0;  // prevent CONFIRMATION → EXPANSION
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);
    ScaleOutTarget sot[1] = {{103.0, 0.5, 100.0, false}};
    lce.set_scale_out_targets(sot, 1);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION);

    // Tick at scale-out price while still in CONFIRMATION — should NOT trigger
    auto tc = make_tick(103.0, 1.0, 1200);
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION);
    CHECK(!lce.has_pending_intent());
}

void test_first_scale_out_stop_is_entry_price() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);
    ScaleOutTarget sot[2];
    sot[0] = {103.0, 0.5, 99.5, false};  // initial new_stop != entry_price
    sot[1] = {106.0, 0.5, 103.0, false};
    lce.set_scale_out_targets(sot, 2);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    // Fill at 100.0 — first scale-out stop should update to entry_price
    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    // Move to EXPANSION and trigger first scale-out
    lce.on_tick(make_tick(102.0, 1.0, 2000), default_risk(), default_perms());
    lce.on_tick(make_tick(103.0, 1.0, 3000), default_risk(), default_perms());
    lce.consume_pending_intent();

    // Fill the first scale-out
    FillEvent so_fill;
    so_fill.timestamp = 3100; so_fill.quantity = 0.5; so_fill.price = 103.0;
    lce.on_fill(so_fill);

    // Stop should be entry_price (100.0), not the initial 99.5
    CHECK(std::abs(lce.get_stop_price() - 100.0) < 1e-9);
}

void test_set_scale_out_targets() {
    LifecycleConfig lcfg;
    TradeLifecycleEngine lce(lcfg);
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);

    ScaleOutTarget sot[3];
    sot[0] = {103.0, 0.34, 100.0, false};
    sot[1] = {106.0, 0.33, 103.0, false};
    sot[2] = {109.0, 0.33, 106.0, false};
    lce.set_scale_out_targets(sot, 3);

    CHECK(lce.get_scale_out_target_count() == 3);
    CHECK(lce.get_scale_out_idx() == 0);
}

void test_scale_out_triggers_at_target() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);
    ScaleOutTarget sot[2];
    sot[0] = {103.0, 0.5, 100.0, false};
    sot[1] = {106.0, 0.5, 103.0, false};
    lce.set_scale_out_targets(sot, 2);

    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    // Fill entry
    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);
    CHECK(lce.get_lifecycle_state() == LifecycleState::CONFIRMATION);

    // Tick to EXPANSION
    auto tc = make_tick(102.0, 1.0, 2000);
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION);

    // Tick reaching first scale-out target
    tc = make_tick(103.0, 1.0, 3000);
    lce.on_tick(tc, default_risk(), default_perms());
    CHECK(lce.has_pending_intent());

    auto intent = lce.consume_pending_intent();
    CHECK(intent.intent_type == IntentType::EXIT);
    CHECK(intent.exit_reason == ExitReason::TARGET);
    CHECK(intent.order_type  == OrderType::LIMIT);
    CHECK(intent.quantity    == 0.5);
    CHECK(intent.limit_price == 103.0);
}

void test_scale_out_fill_advances_target() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);
    ScaleOutTarget sot[2];
    sot[0] = {103.0, 0.5, 100.0, false};
    sot[1] = {106.0, 0.5, 103.0, false};
    lce.set_scale_out_targets(sot, 2);

    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    // Move to EXPANSION and trigger first scale-out
    lce.on_tick(make_tick(102.0, 1.0, 2000), default_risk(), default_perms());
    lce.on_tick(make_tick(103.0, 1.0, 3000), default_risk(), default_perms());
    lce.consume_pending_intent();

    // Fill the first scale-out
    FillEvent so_fill;
    so_fill.timestamp = 3100; so_fill.quantity = 0.5; so_fill.price = 103.0;
    lce.on_fill(so_fill);

    CHECK(lce.get_scale_out_idx() == 1);
    CHECK(lce.get_stop_price() == 100.0);  // tightened to breakeven
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION);  // still active
}

void test_scale_out_all_done_activates_trailing() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 112.0, 1.0, 1000);
    ScaleOutTarget sot[2];
    sot[0] = {103.0, 0.5, 100.0, false};
    sot[1] = {106.0, 0.5, 103.0, false};
    lce.set_scale_out_targets(sot, 2);

    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    // EXPANSION
    lce.on_tick(make_tick(102.0, 1.0, 2000), default_risk(), default_perms());

    // First scale-out
    lce.on_tick(make_tick(103.0, 1.0, 3000), default_risk(), default_perms());
    lce.consume_pending_intent();
    FillEvent so1; so1.timestamp = 3100; so1.quantity = 0.5; so1.price = 103.0;
    lce.on_fill(so1);

    // Second scale-out
    lce.on_tick(make_tick(106.0, 1.0, 4000), default_risk(), default_perms());
    CHECK(lce.has_pending_intent());
    lce.consume_pending_intent();
    FillEvent so2; so2.timestamp = 4100; so2.quantity = 0.5; so2.price = 106.0;
    lce.on_fill(so2);

    // Position fully exited → cooldown
    CHECK(lce.get_lifecycle_state() == LifecycleState::COOLDOWN);
    CHECK(lce.get_scale_out_idx() == 2);
}

void test_target_exit_deferred_while_scaleout_pending() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    // Wide target so maturation doesn't trigger near the scale-out price
    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 120.0, 1.0, 1000);
    ScaleOutTarget sot[1] = {{103.0, 1.0, 100.0, false}};
    lce.set_scale_out_targets(sot, 1);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    lce.on_tick(make_tick(102.0, 1.0, 2000), default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION);

    // Price at scale-out target — should be scale-out intent, NOT full exit
    lce.on_tick(make_tick(103.0, 1.0, 3000), default_risk(), default_perms());
    CHECK(lce.has_pending_intent());
    auto intent = lce.consume_pending_intent();
    CHECK(intent.exit_reason == ExitReason::TARGET);
    CHECK(intent.order_type  == OrderType::LIMIT);
    // State should still be active (not EXIT)
    CHECK(lce.is_active());
}

void test_scale_out_short_direction() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BREAKOUT, TradeSide::SHORT, 101.0, 103.0, 90.0, 1.0, 1000);
    ScaleOutTarget sot[1] = {{97.0, 1.0, 100.0, false}};
    lce.set_scale_out_targets(sot, 1);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    // Move to EXPANSION (price drops favorably)
    lce.on_tick(make_tick(98.0, 1.0, 2000), default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXPANSION);

    // Scale-out triggers at 97.0 for short
    lce.on_tick(make_tick(97.0, 1.0, 3000), default_risk(), default_perms());
    CHECK(lce.has_pending_intent());
    auto intent = lce.consume_pending_intent();
    CHECK(intent.side == OrderSide::BUY);  // closing short = buy
    CHECK(intent.limit_price == 97.0);
}

void test_invalidation_exits_during_scale_out() {
    LifecycleConfig lcfg;
    lcfg.expand_threshold_sigma = 1.0;
    TradeLifecycleEngine lce(lcfg);

    lce.try_setup(TradeArchetype::BOUNCE, TradeSide::LONG, 99.0, 97.0, 110.0, 1.0, 1000);
    ScaleOutTarget sot[2] = {{103.0, 0.5, 100.0, false}, {106.0, 0.5, 103.0, false}};
    lce.set_scale_out_targets(sot, 2);
    lce.confirm_entry(1000);
    lce.consume_pending_intent();

    FillEvent fill;
    fill.timestamp = 1100; fill.quantity = 1.0; fill.price = 100.0;
    lce.on_fill(fill);

    // Move to EXPANSION
    lce.on_tick(make_tick(102.0, 1.0, 2000), default_risk(), default_perms());

    // Price reverses and hits stop
    lce.on_tick(make_tick(96.0, 1.0, 3000), default_risk(), default_perms());
    CHECK(lce.get_lifecycle_state() == LifecycleState::EXIT);
    CHECK(lce.get_last_exit_type() == ExitType::INVALIDATION);
}

// ===================================================================
//  VP features integration
// ===================================================================

void test_vp_features_in_ripple_features() {
    RippleFeatures f;
    f.poc_price = 100.0;
    f.vah       = 101.5;
    f.val       = 98.5;
    f.distance_to_poc_ticks = 20.0;

    CHECK(f.poc_price == 100.0);
    CHECK(f.vah == 101.5);
    CHECK(f.val == 98.5);
    CHECK(f.distance_to_poc_ticks == 20.0);
}

void test_cvd_features_in_ripple_features() {
    RippleFeatures f;
    f.cvd_divergence_detected = true;
    f.cvd_divergence_strength = -0.3;

    CHECK(f.cvd_divergence_detected == true);
    CHECK(f.cvd_divergence_strength == -0.3);
}

// ===================================================================
//  main
// ===================================================================

int main() {
    std::printf("=== test_liquidity_map ===\n");

    // LiquidityMapEngine
    RUN(test_empty_input);
    RUN(test_wall_only_levels);
    RUN(test_inactive_walls_excluded);
    RUN(test_profile_levels_hvn_lvn_poc);
    RUN(test_structural_anchor_vwap);
    RUN(test_flow_merged_into_wall_level);
    RUN(test_void_corridor_detection);
    RUN(test_no_void_when_levels_close);
    RUN(test_hold_score_computation);
    RUN(test_dest_score_computation);
    RUN(test_destinations_in_direction_long);
    RUN(test_destinations_in_direction_short);
    RUN(test_max_levels_trim);
    RUN(test_reset);
    RUN(test_determinism);
    RUN(test_hold_score_zero_for_non_wall);
    RUN(test_mixed_wall_and_profile);
    RUN(test_wall_min_quality_filter);

    // Scale-out guards
    RUN(test_scale_out_no_trigger_in_confirmation);
    RUN(test_first_scale_out_stop_is_entry_price);

    // CVD divergence boost
    RUN(test_cvd_divergence_boost_absorption);
    RUN(test_cvd_divergence_boost_exhaustion);
    RUN(test_cvd_no_divergence_no_change);

    // Scale-out plan
    RUN(test_set_scale_out_targets);
    RUN(test_scale_out_triggers_at_target);
    RUN(test_scale_out_fill_advances_target);
    RUN(test_scale_out_all_done_activates_trailing);
    RUN(test_target_exit_deferred_while_scaleout_pending);
    RUN(test_scale_out_short_direction);
    RUN(test_invalidation_exits_during_scale_out);

    // VP/CVD feature fields
    RUN(test_vp_features_in_ripple_features);
    RUN(test_cvd_features_in_ripple_features);

    std::printf("\nAll %d tests passed (%d checks).\n", tests_run, checks_total);
    return 0;
}
