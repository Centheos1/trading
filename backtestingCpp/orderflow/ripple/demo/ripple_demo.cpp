#include <iostream>
#include <iomanip>

#include "../RippleTypes.h"
#include "../RippleConfig.h"
#include "../RippleContext.h"
#include "../WallDetector.h"
#include "../RippleFeatureEngine.h"
#include "../RippleEvidenceEngine.h"
#include "../ScoreBasedInference.h"
#include "../RippleStateTracker.h"

#include "../IWallDetector.h"
#include "../IRippleFeatureEngine.h"
#include "../IRippleEvidenceEngine.h"
#include "../IRippleStateInference.h"
#include "../ITriggerDecisionEngine.h"
#include "../TriggerDecisionEngine.h"

#include "../../OrderBook.h"
#include "../../Types.h"

using namespace orderflow;
using namespace orderflow::ripple;

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

static void print_section(const char* title) {
    std::cout << "\n--- " << title << " ---\n";
}

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

static void print_walls(const WallDetector& det) {
    auto& walls = det.active_walls();
    auto& mets  = det.metrics();
    if (walls.empty()) {
        std::cout << "  (no walls)\n";
        return;
    }
    for (size_t i = 0; i < walls.size(); ++i) {
        auto& w = walls[i];
        auto& m = mets[i];
        std::cout << "  wall_id=" << w.wall_id
                  << " side=" << to_string(w.side)
                  << " price=" << w.price
                  << " qty=" << w.current_qty
                  << " peak=" << w.peak_qty
                  << " lifecycle=" << to_string(w.lifecycle)
                  << " updates=" << w.update_count
                  << "\n";
        std::cout << "    metrics: rel_size=" << m.relative_size
                  << " baseline=" << m.baseline_depth
                  << " mid_dist=" << m.distance_from_mid_ticks
                  << " best_dist=" << m.distance_from_best_ticks
                  << " rank=" << m.local_rank
                  << " persist=" << m.persistence_sec << "s"
                  << " depl_rate=" << m.depletion_rate
                  << " growth_rate=" << m.growth_rate
                  << " depl_pct=" << m.depletion_pct
                  << " persists=" << m.same_price_persists
                  << "\n";
    }
    auto* pb = det.primary_bid_wall();
    auto* pa = det.primary_ask_wall();
    if (pb) std::cout << "  primary_bid: id=" << pb->wall_id << " price=" << pb->price << "\n";
    if (pa) std::cout << "  primary_ask: id=" << pa->wall_id << " price=" << pa->price << "\n";
}

// ---------------------------------------------------------------
// Demo: walk through the wall lifecycle
// ---------------------------------------------------------------

int main() {
    std::cout << "=== Ripple Layer — Stage 2 WallDetector Demo ===\n";

    RippleConfig cfg;
    cfg.tick_size = 1.0;
    cfg.wall_min_relative_size = 3.0;
    cfg.wall_max_distance_ticks = 50;
    cfg.book_depth_levels = 10;
    cfg.wall_min_age_ms = 500;

    RippleContext ctx(cfg);
    WallDetector det(cfg);
    OrderBook book;

    std::cout << std::fixed << std::setprecision(2);

    // ----- Step 1: No wall -----
    print_section("Step 1: No wall (uniform book)");
    auto snap1 = make_snapshot(1000,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 5.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}, {104.0, 5.0}, {105.0, 4.0}});
    book.on_depth_update(snap1);
    ctx.on_depth(book, 1000);
    det.on_depth(ctx);
    print_walls(det);

    // ----- Step 2: Wall forming -----
    print_section("Step 2: Wall forming (large bid at 97.0)");
    auto snap2 = make_depth_update(1200,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 60.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap2);
    ctx.on_depth(book, 1200);
    det.on_depth(ctx);
    print_walls(det);

    // ----- Step 3: Wall persisting (becomes ACTIVE after min_age) -----
    print_section("Step 3: Wall persisting -> ACTIVE");
    auto snap3 = make_depth_update(2000,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 58.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap3);
    ctx.on_depth(book, 2000);
    det.on_depth(ctx);
    print_walls(det);

    // ----- Step 4: Wall growing (refill) -----
    print_section("Step 4: Wall growing (refill)");
    auto snap4 = make_depth_update(2500,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 80.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap4);
    ctx.on_depth(book, 2500);
    det.on_depth(ctx);
    print_walls(det);

    // ----- Step 5: Wall shrinking (gradual depletion) -----
    print_section("Step 5: Wall shrinking (gradual depletion)");
    auto snap5 = make_depth_update(3000,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 50.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap5);
    ctx.on_depth(book, 3000);
    det.on_depth(ctx);
    print_walls(det);

    auto snap5b = make_depth_update(3500,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 35.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap5b);
    ctx.on_depth(book, 3500);
    det.on_depth(ctx);
    std::cout << "  (after further depletion)\n";
    print_walls(det);

    // ----- Step 6: Wall disappearing abruptly (withdrawn) -----
    print_section("Step 6: Wall disappearing (withdrawn — large single-tick drop)");
    auto snap6 = make_depth_update(4000,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}, {97.0, 4.0}, {96.0, 4.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 6.0}});
    book.on_depth_update(snap6);
    ctx.on_depth(book, 4000);
    det.on_depth(ctx);
    print_walls(det);
    std::cout << "  (wall pruned — withdrawn/failed or no longer qualifies)\n";

    // ----- Ask wall demo -----
    print_section("Step 7: Ask wall forming + persisting");
    auto snap7 = make_depth_update(5000,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 70.0}, {104.0, 5.0}, {105.0, 4.0}});
    book.on_depth_update(snap7);
    ctx.on_depth(book, 5000);
    det.on_depth(ctx);
    print_walls(det);

    auto snap7b = make_depth_update(5800,
        {{100.0, 5.0}, {99.0, 4.0}, {98.0, 6.0}},
        {{101.0, 5.0}, {102.0, 4.0}, {103.0, 72.0}, {104.0, 5.0}, {105.0, 4.0}});
    book.on_depth_update(snap7b);
    ctx.on_depth(book, 5800);
    det.on_depth(ctx);
    std::cout << "  (ask wall persists)\n";
    print_walls(det);

    // ===================================================================
    //  Stage 3: RippleFeatureEngine demo
    // ===================================================================

    print_section("Stage 3: FeatureEngine — compute features from live wall");

    RippleFeatureEngine fe(cfg);

    // Reset book state for a clean feature demo
    OrderBook book2;
    auto fsnap = make_snapshot(10000,
        {{100.0, 10.0}, {99.0, 5.0}, {98.0, 3.0}, {97.0, 80.0}, {96.0, 4.0}},
        {{101.0, 8.0},  {102.0, 6.0}, {103.0, 4.0}, {104.0, 3.0}, {105.0, 2.0}});
    book2.on_depth_update(fsnap);

    RippleContext ctx2(cfg);
    WallDetector det2(cfg);
    ctx2.on_depth(book2, 10000);
    det2.on_depth(ctx2);

    // Inject some trades for flow features
    Trade t1{10100, 101.0, 2.5, false};  // buy aggressor
    Trade t2{10200, 101.0, 1.0, false};  // buy aggressor
    Trade t3{10300, 100.0, 0.8, true};   // sell aggressor
    Trade t4{10400, 97.0,  0.3, true};   // sell hitting the bid wall
    ctx2.on_trade(t1);
    ctx2.on_trade(t2);
    ctx2.on_trade(t3);
    ctx2.on_trade(t4);

    // Advance the book so wall persists
    auto fsnap2 = make_depth_update(11000,
        {{100.0, 10.0}, {99.0, 5.0}, {98.0, 3.0}, {97.0, 78.0}, {96.0, 4.0}},
        {{101.0, 8.0},  {102.0, 6.0}, {103.0, 4.0}, {104.0, 3.0}, {105.0, 2.0}});
    book2.on_depth_update(fsnap2);
    ctx2.on_depth(book2, 11000);
    det2.on_depth(ctx2);

    auto* bw = det2.primary_bid_wall();
    auto* bm = det2.primary_bid_metrics();
    if (bw) {
        std::cout << "  Primary bid wall: price=" << bw->price
                  << " qty=" << bw->current_qty
                  << " lifecycle=" << to_string(bw->lifecycle) << "\n";

        auto feats = fe.compute(ctx2, *bw, bm);

        std::cout << "\n  RippleFeatures:\n";
        std::cout << "    relative_wall_size        = " << feats.relative_wall_size << "\n";
        std::cout << "    wall_persistence_sec      = " << feats.wall_persistence_sec << "\n";
        std::cout << "    wall_depletion_rate       = " << feats.wall_depletion_rate << "\n";
        std::cout << "    wall_refill_rate          = " << feats.wall_refill_rate << "\n";
        std::cout << "    wall_cancel_rate          = " << feats.wall_cancel_rate << "\n";
        std::cout << "    distance_to_wall_ticks    = " << feats.distance_to_wall_ticks << "\n";
        std::cout << "    queue_stability           = " << feats.queue_stability << "\n";
        std::cout << "    aggressive_flow_imbalance = " << feats.aggressive_flow_imbalance << "\n";
        std::cout << "    trade_count_imbalance     = " << feats.trade_count_imbalance << "\n";
        std::cout << "    microprice_drift          = " << feats.microprice_drift << "\n";
        std::cout << "    top1_imbalance            = " << feats.top1_imbalance << "\n";
        std::cout << "    top5_imbalance            = " << feats.top5_imbalance << "\n";
        std::cout << "    impact_per_unit_volume    = " << feats.impact_per_unit_volume << "\n";
        std::cout << "    spread_shock              = " << feats.spread_shock << "\n";
        std::cout << "    short_horizon_volatility  = " << feats.short_horizon_volatility << "\n";
        std::cout << "    time_since_wall_formed_sec= " << feats.time_since_wall_formed_sec << "\n";
    } else {
        std::cout << "  (no bid wall detected for feature demo)\n";
    }

    // Show features without WallMetrics (fallback path)
    print_section("Stage 3: FeatureEngine — fallback (no WallMetrics)");
    WallCandidate mock_wall;
    mock_wall.wall_id = 99;
    mock_wall.side = WallSide::ASK;
    mock_wall.price = 103.0;
    mock_wall.initial_qty = 20.0;
    mock_wall.current_qty = 18.0;
    mock_wall.peak_qty = 20.0;
    mock_wall.first_seen = 10000;
    mock_wall.last_seen = 11000;
    mock_wall.lifecycle = WallLifecycle::ACTIVE;
    mock_wall.depletion_rate = 1.5;
    mock_wall.refill_rate = 0.3;

    auto feats_fb = fe.compute(ctx2, mock_wall);
    std::cout << "    relative_wall_size (fallback) = " << feats_fb.relative_wall_size << "\n";
    std::cout << "    wall_cancel_rate   (fallback) = " << feats_fb.wall_cancel_rate << "\n";
    std::cout << "    distance_to_wall_ticks        = " << feats_fb.distance_to_wall_ticks << "\n";

    // ===================================================================
    //  Stage 4: RippleEvidenceEngine demo
    // ===================================================================

    RippleEvidenceEngine ee(cfg);

    auto print_evidence = [](const char* label, const RippleEvidence& ev) {
        std::cout << "  " << label << ":\n";
        std::cout << "    absorption    = " << ev.absorption    << "\n";
        std::cout << "    exhaustion    = " << ev.exhaustion    << "\n";
        std::cout << "    withdrawal    = " << ev.withdrawal    << "\n";
        std::cout << "    breakout      = " << ev.breakout      << "\n";
        std::cout << "    refill        = " << ev.refill        << "\n";
        std::cout << "    stabilization = " << ev.stabilization << "\n";
        std::cout << "    dominant      = " << ev.dominant()
                  << " (" << ev.dominant_score() << ")\n";
    };

    // Scenario A: Clean absorption — aggressive flow hitting the wall
    // but the wall holds.  Spread is slightly elevated (active attack),
    // volatility non-zero, and microprice barely moves.
    print_section("Stage 4: Evidence — Clean Absorption");
    {
        RippleFeatures f{};
        f.relative_wall_size = 8.0;
        f.wall_persistence_sec = 5.0;
        f.aggressive_flow_imbalance = -0.8;  // sell into bid wall
        f.distance_to_wall_ticks = 2.0;
        f.impact_per_unit_volume = 0.0;      // no price movement
        f.wall_depletion_rate = 0.5;
        f.wall_refill_rate = 0.3;
        f.spread_shock = 1.3;               // slightly elevated (active attack)
        f.short_horizon_volatility = 0.04;  // mild vol
        f.microprice_drift = -0.1;          // barely drifting

        WallCandidate w;
        w.side = WallSide::BID;
        w.peak_qty = 100.0;
        w.current_qty = 92.0;

        auto ev = ee.compute(f, w);
        print_evidence("Absorption", ev);
    }

    // Scenario B: Likely withdrawal
    print_section("Stage 4: Evidence — Likely Withdrawal");
    {
        RippleFeatures f{};
        f.wall_cancel_rate = 20.0;
        f.wall_depletion_rate = 1.0;
        f.spread_shock = 2.5;
        f.aggressive_flow_imbalance = 0.0;  // no aggression

        WallCandidate w;
        w.side = WallSide::ASK;
        w.peak_qty = 80.0;
        w.current_qty = 10.0;

        auto ev = ee.compute(f, w);
        print_evidence("Withdrawal", ev);
    }

    // Scenario C: Breakout
    print_section("Stage 4: Evidence — Breakout");
    {
        RippleFeatures f{};
        f.aggressive_flow_imbalance = -0.9;  // strong sell into bid wall
        f.spread_shock = 2.0;
        f.short_horizon_volatility = 0.3;
        f.microprice_drift = -2.0;           // price falling through
        f.impact_per_unit_volume = 8e-5;
        f.wall_depletion_rate = 15.0;

        WallCandidate w;
        w.side = WallSide::BID;
        w.peak_qty = 100.0;
        w.current_qty = 8.0;

        auto ev = ee.compute(f, w);
        print_evidence("Breakout", ev);
    }

    // Scenario D: Refill & stabilization
    print_section("Stage 4: Evidence — Refill then Stabilization");
    {
        RippleFeatures f{};
        f.wall_refill_rate = 5.0;
        f.wall_depletion_rate = 0.5;
        f.aggressive_flow_imbalance = 0.3;   // flow reversing (away from bid wall)
        f.spread_shock = 1.1;
        f.impact_per_unit_volume = 1e-5;
        f.short_horizon_volatility = 0.02;
        f.queue_stability = 0.1;
        f.microprice_drift = 0.05;

        WallCandidate w;
        w.side = WallSide::BID;
        w.peak_qty = 100.0;
        w.current_qty = 60.0;

        auto ev = ee.compute(f, w);
        print_evidence("Refill", ev);
    }

    // ===================================================================
    //  Stage 5: ScoreBasedInference demo — state sequence
    // ===================================================================

    print_section("Stage 5: Score-based inference — state sequence");

    auto inf_ptr = std::make_unique<ScoreBasedInference>(cfg);
    RippleStateTracker tracker(cfg, std::move(inf_ptr));

    auto print_inf = [](int step, Timestamp ts, const RippleInferenceResult& r) {
        std::cout << "  [" << step << "] t=" << ts
                  << " state=" << to_string(r.state)
                  << " prev=" << to_string(r.prev_state)
                  << " transition=" << r.is_transition
                  << " conf=" << r.confidence
                  << " age=" << r.state_age_ms << "ms"
                  << "\n       scores: IDLE=" << r.score_for(RippleState::IDLE)
                  << " WF=" << r.score_for(RippleState::WALL_FORMING)
                  << " ABS=" << r.score_for(RippleState::ABSORBING)
                  << " EXH=" << r.score_for(RippleState::EXHAUSTING)
                  << " WTH=" << r.score_for(RippleState::WITHDRAWING)
                  << " BRK=" << r.score_for(RippleState::BREAKING)
                  << " REF=" << r.score_for(RippleState::REFILLING)
                  << " STB=" << r.score_for(RippleState::STABILIZING)
                  << "\n       reason: " << r.reason << "\n";
    };

    WallCandidate demo_wall;
    demo_wall.wall_id = 100;
    demo_wall.side = WallSide::BID;
    demo_wall.price = 97.0;
    demo_wall.peak_qty = 80.0;
    demo_wall.current_qty = 80.0;

    // Step 1: Wall forming — no evidence yet
    demo_wall.lifecycle = WallLifecycle::FORMING;
    {
        RippleEvidence ev{};
        auto r = tracker.update(ev, &demo_wall, 1000);
        print_inf(1, 1000, r);
    }

    // Step 2: Wall active, absorption builds (allow dwell to pass)
    demo_wall.lifecycle = WallLifecycle::ACTIVE;
    {
        RippleEvidence ev{};
        ev.absorption = 0.75;
        ev.exhaustion = 0.15;
        auto r = tracker.update(ev, &demo_wall, 2000);
        print_inf(2, 2000, r);
    }

    // Step 3: Exhaustion rises while absorption drops
    demo_wall.current_qty = 30.0;
    {
        RippleEvidence ev{};
        ev.absorption = 0.30;
        ev.exhaustion = 0.65;
        auto r = tracker.update(ev, &demo_wall, 3000);
        print_inf(3, 3000, r);
    }

    // Step 4: Wall breaks — lifecycle FAILED, breakout evidence surges
    demo_wall.lifecycle = WallLifecycle::FAILED;
    demo_wall.current_qty = 5.0;
    {
        RippleEvidence ev{};
        ev.breakout = 0.85;
        ev.exhaustion = 0.30;
        auto r = tracker.update(ev, &demo_wall, 3500);
        print_inf(4, 3500, r);
    }

    // Step 5: Refill after break
    demo_wall.lifecycle = WallLifecycle::REFILLING;
    demo_wall.current_qty = 40.0;
    {
        RippleEvidence ev{};
        ev.refill = 0.70;
        ev.breakout = 0.10;
        auto r = tracker.update(ev, &demo_wall, 5000);
        print_inf(5, 5000, r);
    }

    // Step 6: Stabilization
    {
        RippleEvidence ev{};
        ev.stabilization = 0.80;
        ev.refill = 0.30;
        auto r = tracker.update(ev, &demo_wall, 6000);
        print_inf(6, 6000, r);
    }

    // Step 7: Wall gone — fall to IDLE
    {
        RippleEvidence ev{};
        auto r = tracker.update(ev, nullptr, 7000);
        print_inf(7, 7000, r);
    }

    std::cout << "\n=== Stage 5 demo complete ===\n";

    // ==============================================================
    //  STAGE 6 — TriggerDecisionEngine demo
    // ==============================================================
    std::cout << "\n========================================\n"
              << " Stage 6: TriggerDecisionEngine\n"
              << "========================================\n";

    auto print_decision = [](int step, Timestamp ts,
                             const RippleDecision& d) {
        std::cout << "  Step " << step << " (t=" << ts << "): "
                  << to_string(d.intent)
                  << "  side=" << to_string(d.reference_side)
                  << "  refP=" << d.reference_price
                  << "  invP=" << d.invalidation_price
                  << "  conf=" << std::fixed << std::setprecision(2)
                  << d.confidence
                  << "\n    reason: " << d.reason << "\n";
    };

    RippleConfig tcfg;
    tcfg.global_intent_cooldown_ms = 0;
    tcfg.intent_cooldown_ms = 0;
    tcfg.time_stop_ms = 10000;
    tcfg.rearm_min_evidence = 0.40;
    TriggerDecisionEngine trig(tcfg);
    InventorySnapshot inv;
    inv.position = 0.0;

    // --- Scenario A: Absorption -> Bounce Long ---
    std::cout << "\n--- A: Absorption bounce long (bid wall) ---\n";
    {
        WallCandidate w;
        w.wall_id = 100; w.side = WallSide::BID;
        w.price = 95.0; w.current_qty = 50.0;

        RippleEvidence ev; ev.exhaustion = 0.7;
        ev.absorption = 0.5; ev.breakout = 0.1; ev.withdrawal = 0.05;

        RippleInferenceResult ir;
        ir.state = RippleState::EXHAUSTING;
        ir.prev_state = RippleState::ABSORBING;
        ir.is_transition = true; ir.confidence = 0.7;

        auto d = trig.evaluate(ir, ev, &w, inv, 10000);
        print_decision(1, 10000, d);
    }

    // --- Scenario B: Ask wall rejection -> Bounce Short ---
    std::cout << "\n--- B: Ask wall rejection (bounce short) ---\n";
    {
        WallCandidate w;
        w.wall_id = 200; w.side = WallSide::ASK;
        w.price = 105.0; w.current_qty = 60.0;

        RippleEvidence ev; ev.exhaustion = 0.8;
        ev.absorption = 0.6; ev.breakout = 0.05; ev.withdrawal = 0.05;

        RippleInferenceResult ir;
        ir.state = RippleState::EXHAUSTING;
        ir.prev_state = RippleState::ABSORBING;
        ir.is_transition = true; ir.confidence = 0.75;

        auto d = trig.evaluate(ir, ev, &w, inv, 20000);
        print_decision(2, 20000, d);
    }

    // --- Scenario C: Bid wall failure -> Breakout Short ---
    std::cout << "\n--- C: Bid wall failure -> breakout short ---\n";
    {
        WallCandidate w;
        w.wall_id = 300; w.side = WallSide::BID;
        w.price = 94.0; w.current_qty = 10.0;

        RippleEvidence ev; ev.breakout = 0.85;
        ev.absorption = 0.05; ev.withdrawal = 0.15;

        RippleInferenceResult ir;
        ir.state = RippleState::BREAKING;
        ir.prev_state = RippleState::EXHAUSTING;
        ir.is_transition = true; ir.confidence = 0.80;

        auto d = trig.evaluate(ir, ev, &w, inv, 30000);
        print_decision(3, 30000, d);
    }

    // --- Scenario D: Refill / Stabilization -> Rearm ---
    std::cout << "\n--- D: Refill / stabilization -> rearm ---\n";
    {
        WallCandidate w;
        w.wall_id = 400; w.side = WallSide::BID;
        w.price = 94.5; w.current_qty = 40.0;
        w.lifecycle = WallLifecycle::REFILLING;

        RippleEvidence ev; ev.refill = 0.65;
        ev.stabilization = 0.50;

        RippleInferenceResult ir;
        ir.state = RippleState::REFILLING;
        ir.prev_state = RippleState::BREAKING;
        ir.is_transition = true; ir.confidence = 0.6;

        auto d = trig.evaluate(ir, ev, &w, inv, 40000);
        print_decision(4, 40000, d);
    }

    // --- Scenario E: Passive orders cancelled due to withdrawal risk ---
    std::cout << "\n--- E: Cancel passive orders (withdrawal risk) ---\n";
    {
        WallCandidate w;
        w.wall_id = 500; w.side = WallSide::ASK;
        w.price = 106.0; w.current_qty = 20.0;

        RippleEvidence ev; ev.withdrawal = 0.75;

        RippleInferenceResult ir;
        ir.state = RippleState::WITHDRAWING;
        ir.prev_state = RippleState::ABSORBING;
        ir.is_transition = true; ir.confidence = 0.70;

        auto d = trig.evaluate(ir, ev, &w, inv, 50000);
        print_decision(5, 50000, d);
    }

    std::cout << "\n=== Stage 6 demo complete ===\n";
    return 0;
}
