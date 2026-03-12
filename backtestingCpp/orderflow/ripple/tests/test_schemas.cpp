#include <cassert>
#include <cmath>
#include <cstring>
#include <iostream>
#include <set>
#include <string>

#include "../../Schemas.h"

using namespace orderflow;

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

// ======================================================================
// Test: Enum to_string
// ======================================================================

void test_enum_to_string() {
    CHECK(std::string(to_string(TideBias::LONG)) == "LONG", "TideBias::LONG");
    CHECK(std::string(to_string(TideBias::SHORT)) == "SHORT", "TideBias::SHORT");
    CHECK(std::string(to_string(TideBias::NEUTRAL)) == "NEUTRAL", "TideBias::NEUTRAL");

    CHECK(std::string(to_string(VolRegime::LOW)) == "LOW", "VolRegime::LOW");
    CHECK(std::string(to_string(VolRegime::NORMAL)) == "NORMAL", "VolRegime::NORMAL");
    CHECK(std::string(to_string(VolRegime::HIGH)) == "HIGH", "VolRegime::HIGH");
    CHECK(std::string(to_string(VolRegime::CRISIS)) == "CRISIS", "VolRegime::CRISIS");

    CHECK(std::string(to_string(WaveRegime::MEAN_REVERSION)) == "MEAN_REVERSION", "WaveRegime::MEAN_REVERSION");
    CHECK(std::string(to_string(WaveRegime::BREAKOUT)) == "BREAKOUT", "WaveRegime::BREAKOUT");
    CHECK(std::string(to_string(WaveRegime::BREAKDOWN)) == "BREAKDOWN", "WaveRegime::BREAKDOWN");
    CHECK(std::string(to_string(WaveRegime::NEUTRAL)) == "NEUTRAL", "WaveRegime::NEUTRAL");

    CHECK(std::string(to_string(PermissionLevel::FULL)) == "FULL", "PermissionLevel::FULL");
    CHECK(std::string(to_string(PermissionLevel::REDUCED)) == "REDUCED", "PermissionLevel::REDUCED");
    CHECK(std::string(to_string(PermissionLevel::DISABLED)) == "DISABLED", "PermissionLevel::DISABLED");

    CHECK(std::string(to_string(LevelType::WALL_BID)) == "WALL_BID", "LevelType::WALL_BID");
    CHECK(std::string(to_string(LevelType::HVN)) == "HVN", "LevelType::HVN");
    CHECK(std::string(to_string(LevelType::POC)) == "POC", "LevelType::POC");
    CHECK(std::string(to_string(LevelType::VWAP)) == "VWAP", "LevelType::VWAP");
    CHECK(std::string(to_string(LevelType::VOID_BOUNDARY)) == "VOID_BOUNDARY", "LevelType::VOID_BOUNDARY");

    CHECK(std::string(to_string(TradeArchetype::BOUNCE)) == "BOUNCE", "TradeArchetype::BOUNCE");
    CHECK(std::string(to_string(TradeArchetype::BREAKOUT)) == "BREAKOUT", "TradeArchetype::BREAKOUT");

    CHECK(std::string(to_string(TradeSide::LONG)) == "LONG", "TradeSide::LONG");
    CHECK(std::string(to_string(TradeSide::SHORT)) == "SHORT", "TradeSide::SHORT");

    CHECK(std::string(to_string(LifecycleState::SETUP)) == "SETUP", "LifecycleState::SETUP");
    CHECK(std::string(to_string(LifecycleState::ENTRY)) == "ENTRY", "LifecycleState::ENTRY");
    CHECK(std::string(to_string(LifecycleState::CONFIRMATION)) == "CONFIRMATION", "LifecycleState::CONFIRMATION");
    CHECK(std::string(to_string(LifecycleState::EXPANSION)) == "EXPANSION", "LifecycleState::EXPANSION");
    CHECK(std::string(to_string(LifecycleState::MATURATION)) == "MATURATION", "LifecycleState::MATURATION");
    CHECK(std::string(to_string(LifecycleState::EXIT)) == "EXIT", "LifecycleState::EXIT");
    CHECK(std::string(to_string(LifecycleState::COOLDOWN)) == "COOLDOWN", "LifecycleState::COOLDOWN");
    CHECK(std::string(to_string(LifecycleState::CANCELLED)) == "CANCELLED", "LifecycleState::CANCELLED");

    CHECK(std::string(to_string(ExitType::INVALIDATION)) == "INVALIDATION", "ExitType::INVALIDATION");
    CHECK(std::string(to_string(ExitType::TARGET)) == "TARGET", "ExitType::TARGET");
    CHECK(std::string(to_string(ExitType::EXHAUSTION)) == "EXHAUSTION", "ExitType::EXHAUSTION");
    CHECK(std::string(to_string(ExitType::TIME)) == "TIME", "ExitType::TIME");
    CHECK(std::string(to_string(ExitType::RISK_BUDGET)) == "RISK_BUDGET", "ExitType::RISK_BUDGET");

    CHECK(std::string(to_string(OrderSide::BUY)) == "BUY", "OrderSide::BUY");
    CHECK(std::string(to_string(OrderSide::SELL)) == "SELL", "OrderSide::SELL");

    CHECK(std::string(to_string(OrderType::MARKET)) == "MARKET", "OrderType::MARKET");
    CHECK(std::string(to_string(OrderType::LIMIT)) == "LIMIT", "OrderType::LIMIT");

    CHECK(std::string(to_string(IntentType::ENTRY)) == "ENTRY", "IntentType::ENTRY");
    CHECK(std::string(to_string(IntentType::SCALE_IN)) == "SCALE_IN", "IntentType::SCALE_IN");
    CHECK(std::string(to_string(IntentType::SCALE_OUT)) == "SCALE_OUT", "IntentType::SCALE_OUT");
    CHECK(std::string(to_string(IntentType::EXIT)) == "EXIT", "IntentType::EXIT");

    CHECK(std::string(to_string(Urgency::IMMEDIATE)) == "IMMEDIATE", "Urgency::IMMEDIATE");
    CHECK(std::string(to_string(Urgency::NORMAL)) == "NORMAL", "Urgency::NORMAL");

    CHECK(std::string(to_string(ExitReason::INVALIDATION)) == "INVALIDATION", "ExitReason::INVALIDATION");
    CHECK(std::string(to_string(ExitReason::NONE)) == "NONE", "ExitReason::NONE");

    CHECK(std::string(to_string(EventType::TRADE)) == "TRADE", "EventType::TRADE");
    CHECK(std::string(to_string(EventType::DEPTH_UPDATE)) == "DEPTH_UPDATE", "EventType::DEPTH_UPDATE");
    CHECK(std::string(to_string(EventType::DEPTH_SNAPSHOT)) == "DEPTH_SNAPSHOT", "EventType::DEPTH_SNAPSHOT");
}

// ======================================================================
// Test: DefaultTideSnapshot
// ======================================================================

void test_default_tide_snapshot() {
    auto snap = DefaultTideSnapshot::make();
    CHECK(snap.bias == TideBias::NEUTRAL, "default tide bias");
    CHECK(CLOSE(snap.risk_multiplier, 1.0, 1e-9), "default tide risk_multiplier");
    CHECK(CLOSE(snap.max_position_usd, 10000.0, 1e-9), "default tide max_position_usd");
    CHECK(CLOSE(snap.es_budget, 1000.0, 1e-9), "default tide es_budget");
    CHECK(snap.vol_regime == VolRegime::NORMAL, "default tide vol_regime");
    CHECK(snap.timestamp == 0, "default tide timestamp");
    CHECK(CLOSE(snap.consumed_es, 0.0, 1e-9), "default tide consumed_es");
}

// ======================================================================
// Test: DefaultWaveSnapshot
// ======================================================================

void test_default_wave_snapshot() {
    auto snap = DefaultWaveSnapshot::make();
    CHECK(snap.regime == WaveRegime::NEUTRAL, "default wave regime");
    CHECK(CLOSE(snap.trend_efficiency, 0.5, 1e-9), "default wave trend_efficiency");
    CHECK(CLOSE(snap.dispersion, 0.0, 1e-9), "default wave dispersion");
    CHECK(CLOSE(snap.absorption_ratio, 0.5, 1e-9), "default wave absorption_ratio");
    CHECK(snap.timestamp == 0, "default wave timestamp");
    CHECK(snap.permissions.long_bounce == PermissionLevel::FULL, "default wave long_bounce");
    CHECK(snap.permissions.short_bounce == PermissionLevel::FULL, "default wave short_bounce");
    CHECK(snap.permissions.long_breakout == PermissionLevel::FULL, "default wave long_breakout");
    CHECK(snap.permissions.short_breakout == PermissionLevel::FULL, "default wave short_breakout");
}

// ======================================================================
// Test: PermissionSet logic
// ======================================================================

void test_permission_set() {
    PermissionSet ps;
    ps.long_bounce = PermissionLevel::FULL;
    ps.short_bounce = PermissionLevel::REDUCED;
    ps.long_breakout = PermissionLevel::DISABLED;
    ps.short_breakout = PermissionLevel::FULL;
    ps.reduced_size_fraction = 0.5;

    CHECK(ps.get(TradeArchetype::BOUNCE, TradeSide::LONG) == PermissionLevel::FULL,
          "perm get BOUNCE LONG");
    CHECK(ps.get(TradeArchetype::BOUNCE, TradeSide::SHORT) == PermissionLevel::REDUCED,
          "perm get BOUNCE SHORT");
    CHECK(ps.get(TradeArchetype::BREAKOUT, TradeSide::LONG) == PermissionLevel::DISABLED,
          "perm get BREAKOUT LONG");
    CHECK(ps.get(TradeArchetype::BREAKOUT, TradeSide::SHORT) == PermissionLevel::FULL,
          "perm get BREAKOUT SHORT");

    CHECK(ps.is_allowed(TradeArchetype::BOUNCE, TradeSide::LONG) == true,
          "perm allowed BOUNCE LONG");
    CHECK(ps.is_allowed(TradeArchetype::BREAKOUT, TradeSide::LONG) == false,
          "perm disallowed BREAKOUT LONG");

    CHECK(CLOSE(ps.size_fraction(TradeArchetype::BOUNCE, TradeSide::LONG), 1.0, 1e-9),
          "perm size FULL=1.0");
    CHECK(CLOSE(ps.size_fraction(TradeArchetype::BOUNCE, TradeSide::SHORT), 0.5, 1e-9),
          "perm size REDUCED=0.5");
    CHECK(CLOSE(ps.size_fraction(TradeArchetype::BREAKOUT, TradeSide::LONG), 0.0, 1e-9),
          "perm size DISABLED=0.0");
}

// ======================================================================
// Test: Data contract field defaults
// ======================================================================

void test_struct_defaults() {
    TideSnapshot ts;
    CHECK(ts.timestamp == 0, "TideSnapshot default timestamp");
    CHECK(ts.bias == TideBias::NEUTRAL, "TideSnapshot default bias");

    WaveSnapshot ws;
    CHECK(ws.regime == WaveRegime::NEUTRAL, "WaveSnapshot default regime");
    CHECK(CLOSE(ws.trend_efficiency, 0.5, 1e-9), "WaveSnapshot default trend_eff");

    RiskBudgetSnapshot rbs;
    CHECK(CLOSE(rbs.risk_multiplier, 1.0, 1e-9), "RiskBudgetSnapshot default risk_mult");

    TradeStateSnapshot tss;
    CHECK(tss.state == LifecycleState::SETUP, "TradeStateSnapshot default state");
    CHECK(tss.archetype == TradeArchetype::BOUNCE, "TradeStateSnapshot default archetype");

    FillEvent fe;
    CHECK(fe.side == OrderSide::BUY, "FillEvent default side");
    CHECK(fe.is_maker == false, "FillEvent default is_maker");

    StrategyExecutionIntent sei;
    CHECK(sei.order_type == OrderType::MARKET, "ExecIntent default order_type");
    CHECK(sei.intent_type == IntentType::ENTRY, "ExecIntent default intent_type");
    CHECK(sei.exit_reason == ExitReason::NONE, "ExecIntent default exit_reason");
    CHECK(sei.urgency == Urgency::NORMAL, "ExecIntent default urgency");

    LiquidityLevel ll;
    CHECK(ll.type == LevelType::HVN, "LiquidityLevel default type");
    CHECK(CLOSE(ll.hold_score, 0.0, 1e-9), "LiquidityLevel default hold_score");

    MarketEvent me;
    CHECK(me.event_type == EventType::TRADE, "MarketEvent default event_type");

    TradeEventContract tec;
    CHECK(tec.timestamp == 0, "TradeEventContract default timestamp");
    CHECK(CLOSE(tec.price, 0.0, 1e-9), "TradeEventContract default price");
    CHECK(tec.is_buyer_maker == false, "TradeEventContract default is_buyer_maker");

    DepthUpdateContract duc;
    CHECK(duc.timestamp == 0, "DepthUpdateContract default timestamp");
    CHECK(duc.bids.empty(), "DepthUpdateContract default bids empty");
    CHECK(duc.asks.empty(), "DepthUpdateContract default asks empty");
}

// ======================================================================
// Test: StrategyConfig defaults
// ======================================================================

void test_strategy_config_defaults() {
    StrategyConfig cfg;

    CHECK(cfg.tide.update_interval_ms == 60000, "cfg tide update_interval");
    CHECK(CLOSE(cfg.tide.risk_multiplier_default, 1.0, 1e-9), "cfg tide risk_mult_default");
    CHECK(CLOSE(cfg.tide.es_budget_global, 1000.0, 1e-9), "cfg tide es_budget");
    CHECK(CLOSE(cfg.tide.max_position_usd, 10000.0, 1e-9), "cfg tide max_pos");
    CHECK(CLOSE(cfg.tide.lsi_reduce_threshold, 1.5, 1e-9), "cfg tide lsi_thresh");

    CHECK(cfg.wave.update_interval_ms == 5000, "cfg wave update_interval");
    CHECK(CLOSE(cfg.wave.eta_mr_threshold, 0.3, 1e-9), "cfg wave eta_mr");
    CHECK(CLOSE(cfg.wave.eta_bo_threshold, 0.7, 1e-9), "cfg wave eta_bo");
    CHECK(CLOSE(cfg.wave.ar_critical, 0.85, 1e-9), "cfg wave ar_critical");
    CHECK(CLOSE(cfg.wave.reduced_size_fraction, 0.5, 1e-9), "cfg wave reduced_size");

    CHECK(CLOSE(cfg.risk.es_confidence_level, 0.95, 1e-9), "cfg risk es_confidence");
    CHECK(CLOSE(cfg.risk.target_risk_usd, 50.0, 1e-9), "cfg risk target_risk");
    CHECK(CLOSE(cfg.risk.leverage, 1.0, 1e-9), "cfg risk leverage");
    CHECK(CLOSE(cfg.risk.maker_fee_bps, 2.0, 1e-9), "cfg risk maker_fee");
    CHECK(CLOSE(cfg.risk.taker_fee_bps, 4.0, 1e-9), "cfg risk taker_fee");
    CHECK(CLOSE(cfg.risk.budget_exit_threshold, 0.90, 1e-9), "cfg risk budget_exit");

    CHECK(cfg.execution.bounce_order_type == OrderType::LIMIT, "cfg exec bounce_type");
    CHECK(cfg.execution.breakout_order_type == OrderType::MARKET, "cfg exec breakout_type");
    CHECK(CLOSE(cfg.execution.slippage_tolerance_bps, 5.0, 1e-9), "cfg exec slippage");

    CHECK(CLOSE(cfg.testing.replay_tolerance, 0.0, 1e-9), "cfg test replay_tol");
    CHECK(cfg.testing.benchmark_latency_target_us == 100, "cfg test benchmark_lat");
}

// ======================================================================
// Test: Feature registry — completeness against §16
// ======================================================================

void test_feature_registry() {
    CHECK(FEATURE_REGISTRY_SIZE > 0, "registry not empty");

    // Build a set of all registered feature names
    std::set<std::string> registered;
    for (size_t i = 0; i < FEATURE_REGISTRY_SIZE; ++i) {
        registered.insert(FEATURE_REGISTRY[i].full_name);
    }

    // Every canonical §16 feature must be present
    for (size_t i = 0; i < CANONICAL_FEATURES_S16_SIZE; ++i) {
        std::string name(CANONICAL_FEATURES_S16[i]);
        CHECK(registered.count(name) == 1,
              (std::string("§16 feature missing from registry: ") + name).c_str());
    }
}

void test_feature_registry_properties() {
    // All names must contain a dot and start with their namespace
    for (size_t i = 0; i < FEATURE_REGISTRY_SIZE; ++i) {
        auto& fd = FEATURE_REGISTRY[i];
        std::string name(fd.full_name);
        std::string ns(fd.ns);
        CHECK(name.find('.') != std::string::npos,
              (name + " must contain a dot").c_str());
        CHECK(name.substr(0, ns.size() + 1) == ns + ".",
              (name + " must start with " + ns + ".").c_str());
    }

    // No duplicate names
    std::set<std::string> seen;
    for (size_t i = 0; i < FEATURE_REGISTRY_SIZE; ++i) {
        std::string name(FEATURE_REGISTRY[i].full_name);
        CHECK(seen.insert(name).second,
              (std::string("duplicate feature: ") + name).c_str());
    }

    // All six namespaces must be represented
    std::set<std::string> namespaces;
    for (size_t i = 0; i < FEATURE_REGISTRY_SIZE; ++i) {
        namespaces.insert(FEATURE_REGISTRY[i].ns);
    }
    CHECK(namespaces.count("tide") == 1, "namespace tide present");
    CHECK(namespaces.count("wave") == 1, "namespace wave present");
    CHECK(namespaces.count("ripple") == 1, "namespace ripple present");
    CHECK(namespaces.count("liquidity_map") == 1, "namespace liquidity_map present");
    CHECK(namespaces.count("risk") == 1, "namespace risk present");
    CHECK(namespaces.count("trade") == 1, "namespace trade present");
}

void test_feature_registry_specific() {
    bool found_imbalance = false;
    bool found_vpin = false;
    bool found_impact = false;
    bool found_wall_quality = false;

    for (size_t i = 0; i < FEATURE_REGISTRY_SIZE; ++i) {
        auto& fd = FEATURE_REGISTRY[i];
        if (std::string(fd.full_name) == "ripple.imbalance") {
            found_imbalance = true;
            CHECK(fd.type == FeatureType::FLOAT64, "imbalance type");
            CHECK(fd.cadence == FeatureCadence::PER_EVENT, "imbalance cadence");
            CHECK(fd.category == FeatureCategory::DERIVED, "imbalance category");
        }
        if (std::string(fd.full_name) == "ripple.vpin") {
            found_vpin = true;
            CHECK(fd.type == FeatureType::FLOAT64, "vpin type");
            CHECK(fd.cadence == FeatureCadence::PER_SECOND, "vpin cadence");
        }
        if (std::string(fd.full_name) == "ripple.impact") {
            found_impact = true;
            CHECK(fd.type == FeatureType::FLOAT64, "impact type");
            CHECK(fd.cadence == FeatureCadence::MS_100, "impact cadence");
        }
        if (std::string(fd.full_name) == "ripple.best_bid_wall_quality") {
            found_wall_quality = true;
            CHECK(fd.type == FeatureType::FLOAT64, "best_bid_wall_quality type");
            CHECK(fd.cadence == FeatureCadence::PER_EVENT, "best_bid_wall_quality cadence");
        }
    }

    CHECK(found_imbalance, "registry contains ripple.imbalance");
    CHECK(found_vpin, "registry contains ripple.vpin");
    CHECK(found_impact, "registry contains ripple.impact");
    CHECK(found_wall_quality, "registry contains ripple.best_bid_wall_quality");
}

// ======================================================================
// Test: LiquidityMapSnapshot
// ======================================================================

void test_liquidity_map_snapshot() {
    LiquidityMapSnapshot lms;
    lms.timestamp = 1000;
    lms.poc = 50000.0;
    lms.vwap = 49800.0;
    lms.nearest_bid_wall = 49500.0;
    lms.nearest_ask_wall = 50500.0;

    LiquidityLevel lvl;
    lvl.price = 50000.0;
    lvl.depth = 100.0;
    lvl.hold_score = 0.8;
    lvl.dest_score = 0.3;
    lvl.type = LevelType::POC;
    lms.levels.push_back(lvl);

    VoidCorridor vc;
    vc.lo = 49800.0;
    vc.hi = 49900.0;
    lms.void_corridors.push_back(vc);

    CHECK(lms.levels.size() == 1, "lmap levels size");
    CHECK(lms.void_corridors.size() == 1, "lmap void_corridors size");
    CHECK(lms.levels[0].type == LevelType::POC, "lmap level type");
    CHECK(CLOSE(lms.void_corridors[0].lo, 49800.0, 1e-9), "lmap void lo");
}

// ======================================================================
// Test: StrategySnapshot
// ======================================================================

void test_strategy_snapshot_default() {
    auto ss = StrategySnapshot::make_default();
    CHECK(ss.timestamp == 0, "strategy snapshot default timestamp");
    CHECK(ss.tide.bias == TideBias::NEUTRAL, "strategy snapshot tide bias");
    CHECK(CLOSE(ss.tide.risk_multiplier, 1.0, 1e-9), "strategy snapshot tide risk_mult");
    CHECK(CLOSE(ss.tide.max_position_usd, 10000.0, 1e-9), "strategy snapshot tide max_pos");
    CHECK(CLOSE(ss.tide.es_budget, 1000.0, 1e-9), "strategy snapshot tide es_budget");
    CHECK(ss.tide.vol_regime == VolRegime::NORMAL, "strategy snapshot tide vol_regime");
    CHECK(ss.wave.regime == WaveRegime::NEUTRAL, "strategy snapshot wave regime");
    CHECK(CLOSE(ss.wave.trend_efficiency, 0.5, 1e-9), "strategy snapshot wave trend_eff");
    CHECK(ss.wave.permissions.long_bounce == PermissionLevel::FULL, "strategy snapshot wave perm");
    CHECK(ss.trade.state == LifecycleState::SETUP, "strategy snapshot trade state");
    CHECK(CLOSE(ss.risk.consumed_es, 0.0, 1e-9), "strategy snapshot risk consumed_es");
}

void test_strategy_snapshot_composition() {
    StrategySnapshot ss;
    ss.timestamp = 5000;

    ss.tide.bias = TideBias::LONG;
    ss.tide.risk_multiplier = 0.8;
    ss.tide.es_budget = 2000.0;

    ss.wave.regime = WaveRegime::MEAN_REVERSION;
    ss.wave.permissions.long_bounce = PermissionLevel::FULL;
    ss.wave.permissions.short_breakout = PermissionLevel::DISABLED;

    ss.ripple.liquidity_state = ripple::RippleState::ABSORBING;
    ss.ripple.microprice = 50001.5;
    ss.ripple.imbalance = 0.25;

    ss.trade.archetype = TradeArchetype::BOUNCE;
    ss.trade.side = TradeSide::LONG;
    ss.trade.state = LifecycleState::CONFIRMATION;
    ss.trade.entry_price = 50000.0;
    ss.trade.stop_price = 49800.0;

    ss.risk.es_budget = 2000.0;
    ss.risk.consumed_es = 150.0;

    CHECK(ss.timestamp == 5000, "composed snapshot timestamp");
    CHECK(ss.tide.bias == TideBias::LONG, "composed tide bias");
    CHECK(ss.wave.regime == WaveRegime::MEAN_REVERSION, "composed wave regime");
    CHECK(ss.wave.permissions.is_allowed(TradeArchetype::BOUNCE, TradeSide::LONG), "composed perm check");
    CHECK(!ss.wave.permissions.is_allowed(TradeArchetype::BREAKOUT, TradeSide::SHORT), "composed perm disabled");
    CHECK(ss.ripple.liquidity_state == ripple::RippleState::ABSORBING, "composed ripple state");
    CHECK(ss.trade.state == LifecycleState::CONFIRMATION, "composed trade state");
    CHECK(CLOSE(ss.risk.consumed_es, 150.0, 1e-9), "composed risk consumed_es");
}

void test_strategy_snapshot_determinism() {
    auto a = StrategySnapshot::make_default();
    auto b = StrategySnapshot::make_default();

    CHECK(a.tide.bias == b.tide.bias, "deterministic tide bias");
    CHECK(CLOSE(a.tide.risk_multiplier, b.tide.risk_multiplier, 1e-15), "deterministic tide risk_mult");
    CHECK(CLOSE(a.tide.es_budget, b.tide.es_budget, 1e-15), "deterministic tide es_budget");
    CHECK(a.wave.regime == b.wave.regime, "deterministic wave regime");
    CHECK(CLOSE(a.wave.trend_efficiency, b.wave.trend_efficiency, 1e-15), "deterministic wave trend_eff");
    CHECK(a.wave.permissions.long_bounce == b.wave.permissions.long_bounce, "deterministic perm");
    CHECK(a.trade.state == b.trade.state, "deterministic trade state");
}

// ======================================================================
// Test: TradeEventContract / DepthUpdateContract
// ======================================================================

void test_data_contract_trade_event() {
    TradeEventContract te;
    te.timestamp = 1710000000000;
    te.symbol = "BTCUSDT";
    te.price = 65432.10;
    te.quantity = 0.5;
    te.is_buyer_maker = true;

    CHECK(te.timestamp == 1710000000000, "trade event timestamp");
    CHECK(te.symbol == "BTCUSDT", "trade event symbol");
    CHECK(CLOSE(te.price, 65432.10, 1e-6), "trade event price");
    CHECK(CLOSE(te.quantity, 0.5, 1e-9), "trade event quantity");
    CHECK(te.is_buyer_maker == true, "trade event is_buyer_maker");
}

void test_data_contract_depth_update() {
    DepthUpdateContract du;
    du.timestamp = 1710000000000;
    du.symbol = "BTCUSDT";
    du.bids.push_back({65000.0, 1.5});
    du.bids.push_back({64999.0, 2.0});
    du.asks.push_back({65001.0, 0.8});
    du.first_update_id = 100;
    du.last_update_id = 105;

    CHECK(du.bids.size() == 2, "depth update bids count");
    CHECK(du.asks.size() == 1, "depth update asks count");
    CHECK(CLOSE(du.bids[0].first, 65000.0, 1e-9), "depth update best bid");
    CHECK(du.first_update_id == 100, "depth update first_update_id");
    CHECK(du.last_update_id == 105, "depth update last_update_id");
}

// ======================================================================
// main
// ======================================================================

int main() {
    test_enum_to_string();
    test_default_tide_snapshot();
    test_default_wave_snapshot();
    test_permission_set();
    test_struct_defaults();
    test_strategy_config_defaults();
    test_feature_registry();
    test_feature_registry_properties();
    test_feature_registry_specific();
    test_liquidity_map_snapshot();
    test_strategy_snapshot_default();
    test_strategy_snapshot_composition();
    test_strategy_snapshot_determinism();
    test_data_contract_trade_event();
    test_data_contract_depth_update();

    std::cout << tests_passed << " / " << tests_run << " tests passed." << std::endl;
    return (tests_passed == tests_run) ? 0 : 1;
}
