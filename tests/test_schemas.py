"""
Phase 1 tests for schemas.py — data contracts, enums, defaults, config load/save,
feature registry, StrategySnapshot, and deterministic snapshot construction.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (
    CANONICAL_FEATURES_S16,
    DEFAULT_STRATEGY_SNAPSHOT,
    DEFAULT_TIDE_SNAPSHOT,
    DEFAULT_WAVE_SNAPSHOT,
    FEATURE_REGISTRY,
    DepthUpdateContract,
    EventType,
    ExecutionConfig,
    ExitReason,
    ExitType,
    FeatureCadence,
    FeatureCategory,
    FeatureDescriptor,
    FeatureSnapshot,
    FeatureType,
    FillEvent,
    IntentType,
    LevelType,
    LifecycleState,
    LiquidityLevel,
    LiquidityMapSnapshot,
    MarketEvent,
    OrderSide,
    OrderType,
    PermissionLevel,
    PermissionSet,
    RippleConfig,
    RippleFeatures,
    RippleStateSnapshot,
    RiskBudgetSnapshot,
    RiskConfig,
    StrategyConfig,
    StrategyExecutionIntent,
    StrategySnapshot,
    TestingConfig,
    TideConfig,
    TideBias,
    TideSnapshot,
    TradeArchetype,
    TradeEventContract,
    TradeSide,
    TradeStateSnapshot,
    Urgency,
    VolRegime,
    VoidCorridor,
    WallSide,
    WallView,
    WaveConfig,
    WaveRegime,
    WaveSnapshot,
)


class TestEnumValues(unittest.TestCase):
    """Verify all enum members exist with correct string values."""

    def test_tide_bias(self):
        self.assertEqual(TideBias.LONG.value, "LONG")
        self.assertEqual(TideBias.SHORT.value, "SHORT")
        self.assertEqual(TideBias.NEUTRAL.value, "NEUTRAL")

    def test_vol_regime(self):
        self.assertEqual(VolRegime.LOW.value, "LOW")
        self.assertEqual(VolRegime.CRISIS.value, "CRISIS")
        self.assertEqual(len(VolRegime), 4)

    def test_wave_regime(self):
        self.assertEqual(WaveRegime.MEAN_REVERSION.value, "MEAN_REVERSION")
        self.assertEqual(WaveRegime.BREAKDOWN.value, "BREAKDOWN")
        self.assertEqual(len(WaveRegime), 4)

    def test_permission_level(self):
        self.assertEqual(len(PermissionLevel), 3)

    def test_level_type(self):
        self.assertEqual(LevelType.WALL_BID.value, "WALL_BID")
        self.assertEqual(LevelType.VOID_BOUNDARY.value, "VOID_BOUNDARY")
        self.assertEqual(len(LevelType), 7)

    def test_trade_archetype(self):
        self.assertEqual(len(TradeArchetype), 2)
        self.assertEqual(TradeArchetype.BOUNCE.value, "BOUNCE")

    def test_trade_side(self):
        self.assertEqual(TradeSide.LONG.value, "LONG")
        self.assertEqual(TradeSide.SHORT.value, "SHORT")

    def test_lifecycle_state(self):
        self.assertEqual(len(LifecycleState), 8)
        self.assertEqual(LifecycleState.SETUP.value, "SETUP")
        self.assertEqual(LifecycleState.CANCELLED.value, "CANCELLED")

    def test_exit_type(self):
        self.assertEqual(len(ExitType), 5)
        self.assertEqual(ExitType.RISK_BUDGET.value, "RISK_BUDGET")

    def test_order_side(self):
        self.assertEqual(OrderSide.BUY.value, "BUY")
        self.assertEqual(OrderSide.SELL.value, "SELL")

    def test_order_type(self):
        self.assertEqual(OrderType.MARKET.value, "MARKET")
        self.assertEqual(OrderType.LIMIT.value, "LIMIT")

    def test_intent_type(self):
        self.assertEqual(len(IntentType), 4)
        self.assertEqual(IntentType.SCALE_IN.value, "SCALE_IN")

    def test_urgency(self):
        self.assertEqual(Urgency.IMMEDIATE.value, "IMMEDIATE")
        self.assertEqual(Urgency.NORMAL.value, "NORMAL")

    def test_exit_reason(self):
        self.assertEqual(len(ExitReason), 6)
        self.assertEqual(ExitReason.NONE.value, "NONE")

    def test_event_type(self):
        self.assertEqual(EventType.TRADE.value, "TRADE")
        self.assertEqual(EventType.DEPTH_SNAPSHOT.value, "DEPTH_SNAPSHOT")


class TestDefaultSnapshots(unittest.TestCase):
    """Verify pre-phase defaults match strategy.md §7.5."""

    def test_default_tide(self):
        s = DEFAULT_TIDE_SNAPSHOT
        self.assertEqual(s.bias, TideBias.NEUTRAL)
        self.assertAlmostEqual(s.risk_multiplier, 1.0)
        self.assertAlmostEqual(s.max_position_usd, 10000.0)
        self.assertAlmostEqual(s.es_budget, 1000.0)
        self.assertEqual(s.vol_regime, VolRegime.NORMAL)
        self.assertEqual(s.timestamp, 0)

    def test_default_wave(self):
        s = DEFAULT_WAVE_SNAPSHOT
        self.assertEqual(s.regime, WaveRegime.NEUTRAL)
        self.assertAlmostEqual(s.trend_efficiency, 0.5)
        self.assertAlmostEqual(s.dispersion, 0.0)
        self.assertAlmostEqual(s.absorption_ratio, 0.5)
        self.assertEqual(s.timestamp, 0)
        self.assertEqual(s.permissions.long_bounce, PermissionLevel.FULL)
        self.assertEqual(s.permissions.short_breakout, PermissionLevel.FULL)


class TestPermissionSet(unittest.TestCase):
    """Verify PermissionSet logic."""

    def test_get(self):
        ps = PermissionSet(
            long_bounce=PermissionLevel.FULL,
            short_bounce=PermissionLevel.REDUCED,
            long_breakout=PermissionLevel.DISABLED,
            short_breakout=PermissionLevel.FULL,
        )
        self.assertEqual(ps.get(TradeArchetype.BOUNCE, TradeSide.LONG), PermissionLevel.FULL)
        self.assertEqual(ps.get(TradeArchetype.BOUNCE, TradeSide.SHORT), PermissionLevel.REDUCED)
        self.assertEqual(ps.get(TradeArchetype.BREAKOUT, TradeSide.LONG), PermissionLevel.DISABLED)
        self.assertEqual(ps.get(TradeArchetype.BREAKOUT, TradeSide.SHORT), PermissionLevel.FULL)

    def test_is_allowed(self):
        ps = PermissionSet(long_breakout=PermissionLevel.DISABLED)
        self.assertTrue(ps.is_allowed(TradeArchetype.BOUNCE, TradeSide.LONG))
        self.assertFalse(ps.is_allowed(TradeArchetype.BREAKOUT, TradeSide.LONG))

    def test_size_fraction(self):
        ps = PermissionSet(
            long_bounce=PermissionLevel.FULL,
            short_bounce=PermissionLevel.REDUCED,
            long_breakout=PermissionLevel.DISABLED,
            reduced_size_fraction=0.5,
        )
        self.assertAlmostEqual(ps.size_fraction(TradeArchetype.BOUNCE, TradeSide.LONG), 1.0)
        self.assertAlmostEqual(ps.size_fraction(TradeArchetype.BOUNCE, TradeSide.SHORT), 0.5)
        self.assertAlmostEqual(ps.size_fraction(TradeArchetype.BREAKOUT, TradeSide.LONG), 0.0)


class TestDataContractDefaults(unittest.TestCase):
    """Verify data contract dataclass field defaults."""

    def test_tide_snapshot(self):
        ts = TideSnapshot()
        self.assertEqual(ts.bias, TideBias.NEUTRAL)
        self.assertAlmostEqual(ts.risk_multiplier, 1.0)

    def test_wave_snapshot(self):
        ws = WaveSnapshot()
        self.assertEqual(ws.regime, WaveRegime.NEUTRAL)

    def test_trade_state_snapshot(self):
        tss = TradeStateSnapshot()
        self.assertEqual(tss.state, LifecycleState.SETUP)
        self.assertEqual(tss.archetype, TradeArchetype.BOUNCE)

    def test_fill_event(self):
        fe = FillEvent()
        self.assertEqual(fe.side, OrderSide.BUY)
        self.assertFalse(fe.is_maker)

    def test_strategy_execution_intent(self):
        sei = StrategyExecutionIntent()
        self.assertEqual(sei.order_type, OrderType.MARKET)
        self.assertEqual(sei.intent_type, IntentType.ENTRY)
        self.assertEqual(sei.exit_reason, ExitReason.NONE)
        self.assertEqual(sei.urgency, Urgency.NORMAL)

    def test_liquidity_level(self):
        ll = LiquidityLevel()
        self.assertEqual(ll.type, LevelType.HVN)

    def test_market_event(self):
        me = MarketEvent()
        self.assertEqual(me.event_type, EventType.TRADE)

    def test_risk_budget_snapshot(self):
        rbs = RiskBudgetSnapshot()
        self.assertAlmostEqual(rbs.risk_multiplier, 1.0)

    def test_trade_event_contract(self):
        te = TradeEventContract()
        self.assertEqual(te.timestamp, 0)
        self.assertAlmostEqual(te.price, 0.0)
        self.assertFalse(te.is_buyer_maker)

    def test_depth_update_contract(self):
        du = DepthUpdateContract()
        self.assertEqual(du.timestamp, 0)
        self.assertEqual(len(du.bids), 0)
        self.assertEqual(len(du.asks), 0)

    def test_trade_event_contract_populated(self):
        te = TradeEventContract(
            timestamp=1710000000000,
            symbol="BTCUSDT",
            price=65432.10,
            quantity=0.5,
            is_buyer_maker=True,
        )
        self.assertEqual(te.symbol, "BTCUSDT")
        self.assertAlmostEqual(te.price, 65432.10)
        self.assertTrue(te.is_buyer_maker)

    def test_depth_update_contract_populated(self):
        du = DepthUpdateContract(
            timestamp=1710000000000,
            symbol="BTCUSDT",
            bids=[(65000.0, 1.5), (64999.0, 2.0)],
            asks=[(65001.0, 0.8)],
            first_update_id=100,
            last_update_id=105,
        )
        self.assertEqual(len(du.bids), 2)
        self.assertEqual(len(du.asks), 1)
        self.assertAlmostEqual(du.bids[0][0], 65000.0)
        self.assertEqual(du.first_update_id, 100)
        self.assertEqual(du.last_update_id, 105)


class TestStrategyConfig(unittest.TestCase):
    """Verify StrategyConfig defaults and JSON round-trip."""

    def test_defaults(self):
        cfg = StrategyConfig()
        self.assertEqual(cfg.tide.update_interval_ms, 60000)
        self.assertAlmostEqual(cfg.tide.es_budget_global, 1000.0)
        self.assertEqual(cfg.wave.update_interval_ms, 5000)
        self.assertAlmostEqual(cfg.wave.eta_mr_threshold, 0.3)
        self.assertAlmostEqual(cfg.risk.es_confidence_level, 0.95)
        self.assertAlmostEqual(cfg.risk.leverage, 1.0)
        self.assertEqual(cfg.execution.bounce_order_type, "LIMIT")
        self.assertEqual(cfg.execution.breakout_order_type, "MARKET")
        self.assertAlmostEqual(cfg.testing.replay_tolerance, 0.0)

    def test_json_round_trip(self):
        cfg = StrategyConfig()
        cfg.tide.es_budget_global = 2000.0
        cfg.risk.leverage = 5.0
        cfg.wave.ar_critical = 0.90

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name

        try:
            cfg.save(path)
            loaded = StrategyConfig.load(path)

            self.assertAlmostEqual(loaded.tide.es_budget_global, 2000.0)
            self.assertAlmostEqual(loaded.risk.leverage, 5.0)
            self.assertAlmostEqual(loaded.wave.ar_critical, 0.90)
            self.assertEqual(loaded.tide.update_interval_ms, 60000)
            self.assertEqual(loaded.execution.bounce_order_type, "LIMIT")
        finally:
            os.unlink(path)

    def test_load_partial_json(self):
        partial = {"tide": {"es_budget_global": 5000.0}, "risk": {"leverage": 10.0}}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            json.dump(partial, f)
            path = f.name

        try:
            loaded = StrategyConfig.load(path)
            self.assertAlmostEqual(loaded.tide.es_budget_global, 5000.0)
            self.assertAlmostEqual(loaded.risk.leverage, 10.0)
            self.assertEqual(loaded.tide.update_interval_ms, 60000)
            self.assertEqual(loaded.wave.update_interval_ms, 5000)
        finally:
            os.unlink(path)

    def test_load_default_config_file(self):
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config.json"
        )
        if os.path.exists(config_path):
            cfg = StrategyConfig.load(config_path)
            self.assertAlmostEqual(cfg.tide.es_budget_global, 1000.0)
            self.assertAlmostEqual(cfg.risk.leverage, 1.0)
            self.assertAlmostEqual(cfg.wave.reduced_size_fraction, 0.5)


class TestFeatureRegistry(unittest.TestCase):
    """Verify feature registry completeness and correctness."""

    def test_not_empty(self):
        self.assertGreater(len(FEATURE_REGISTRY), 0)

    def test_all_have_required_fields(self):
        for fd in FEATURE_REGISTRY:
            self.assertIsInstance(fd, FeatureDescriptor)
            self.assertTrue(len(fd.full_name) > 0)
            self.assertTrue(len(fd.ns) > 0)
            self.assertIn(".", fd.full_name)
            self.assertTrue(fd.full_name.startswith(fd.ns + "."))

    def test_unique_names(self):
        names = [fd.full_name for fd in FEATURE_REGISTRY]
        self.assertEqual(len(names), len(set(names)), "duplicate feature names found")

    def test_all_six_namespaces_present(self):
        namespaces = {fd.ns for fd in FEATURE_REGISTRY}
        for ns in ("tide", "wave", "ripple", "liquidity_map", "risk", "trade"):
            self.assertIn(ns, namespaces, f"namespace {ns} missing")

    def test_canonical_s16_completeness(self):
        """Every canonical feature from strategy.md §16 must be in the registry."""
        registered = {fd.full_name for fd in FEATURE_REGISTRY}
        for name in CANONICAL_FEATURES_S16:
            self.assertIn(name, registered, f"§16 feature missing: {name}")

    def test_canonical_s16_count(self):
        self.assertEqual(
            len(CANONICAL_FEATURES_S16), 49,
            "§16 defines exactly 49 canonical features"
        )

    def test_specific_features_present(self):
        names = {fd.full_name for fd in FEATURE_REGISTRY}
        self.assertIn("ripple.imbalance", names)
        self.assertIn("ripple.microprice", names)
        self.assertIn("ripple.vpin", names)
        self.assertIn("ripple.impact", names)
        self.assertIn("ripple.best_bid_wall_quality", names)
        self.assertIn("ripple.best_ask_wall_quality", names)
        self.assertIn("tide.bias", names)
        self.assertIn("tide.risk_multiplier", names)
        self.assertIn("tide.max_position_usd", names)
        self.assertIn("tide.es_budget", names)
        self.assertIn("tide.vol_regime", names)
        self.assertIn("wave.residual_dislocation", names)
        self.assertIn("wave.distance_vwap", names)
        self.assertIn("wave.distance_session_high", names)
        self.assertIn("wave.distance_session_low", names)
        self.assertIn("wave.permissions", names)
        self.assertIn("wave.regime", names)
        self.assertIn("trade.state", names)
        self.assertIn("risk.consumed_es", names)
        self.assertIn("liquidity_map.poc", names)

    def test_imbalance_metadata(self):
        fd = next(f for f in FEATURE_REGISTRY if f.full_name == "ripple.imbalance")
        self.assertEqual(fd.type, FeatureType.FLOAT64)
        self.assertEqual(fd.cadence, FeatureCadence.PER_EVENT)
        self.assertEqual(fd.category, FeatureCategory.DERIVED)
        self.assertEqual(fd.ns, "ripple")

    def test_vpin_metadata(self):
        fd = next(f for f in FEATURE_REGISTRY if f.full_name == "ripple.vpin")
        self.assertEqual(fd.type, FeatureType.FLOAT64)
        self.assertEqual(fd.cadence, FeatureCadence.PER_SECOND)
        self.assertEqual(fd.category, FeatureCategory.DERIVED)

    def test_impact_metadata(self):
        fd = next(f for f in FEATURE_REGISTRY if f.full_name == "ripple.impact")
        self.assertEqual(fd.type, FeatureType.FLOAT64)
        self.assertEqual(fd.cadence, FeatureCadence.MS_100)

    def test_tide_bias_metadata(self):
        fd = next(f for f in FEATURE_REGISTRY if f.full_name == "tide.bias")
        self.assertEqual(fd.type, FeatureType.ENUM)
        self.assertEqual(fd.category, FeatureCategory.MODEL_OUTPUT)


class TestLiquidityMapSnapshot(unittest.TestCase):
    """Verify LiquidityMapSnapshot construction."""

    def test_construction(self):
        lms = LiquidityMapSnapshot(
            timestamp=1000,
            levels=[
                LiquidityLevel(price=50000.0, hold_score=0.8, type=LevelType.POC),
                LiquidityLevel(price=49500.0, hold_score=0.3, type=LevelType.WALL_BID),
            ],
            void_corridors=[VoidCorridor(lo=49800.0, hi=49900.0)],
            poc=50000.0,
            vwap=49800.0,
        )
        self.assertEqual(len(lms.levels), 2)
        self.assertEqual(len(lms.void_corridors), 1)
        self.assertEqual(lms.levels[0].type, LevelType.POC)
        self.assertAlmostEqual(lms.void_corridors[0].lo, 49800.0)


class TestStrategySnapshot(unittest.TestCase):
    """Verify StrategySnapshot composite and deterministic construction."""

    def test_default_construction(self):
        ss = StrategySnapshot()
        self.assertEqual(ss.timestamp, 0)
        self.assertEqual(ss.tide.bias, TideBias.NEUTRAL)
        self.assertEqual(ss.wave.regime, WaveRegime.NEUTRAL)
        self.assertEqual(ss.trade.state, LifecycleState.SETUP)

    def test_make_default(self):
        ss = StrategySnapshot.make_default()
        self.assertEqual(ss.tide.bias, TideBias.NEUTRAL)
        self.assertAlmostEqual(ss.tide.risk_multiplier, 1.0)
        self.assertAlmostEqual(ss.tide.max_position_usd, 10000.0)
        self.assertAlmostEqual(ss.tide.es_budget, 1000.0)
        self.assertEqual(ss.tide.vol_regime, VolRegime.NORMAL)
        self.assertEqual(ss.wave.regime, WaveRegime.NEUTRAL)
        self.assertAlmostEqual(ss.wave.trend_efficiency, 0.5)
        self.assertEqual(ss.wave.permissions.long_bounce, PermissionLevel.FULL)

    def test_default_strategy_snapshot_module_constant(self):
        ss = DEFAULT_STRATEGY_SNAPSHOT
        self.assertEqual(ss.tide.bias, TideBias.NEUTRAL)
        self.assertEqual(ss.wave.regime, WaveRegime.NEUTRAL)

    def test_composition(self):
        ss = StrategySnapshot()
        ss.timestamp = 5000

        ss.tide.bias = TideBias.LONG
        ss.tide.risk_multiplier = 0.8
        ss.tide.es_budget = 2000.0

        ss.wave.regime = WaveRegime.MEAN_REVERSION
        ss.wave.permissions.long_bounce = PermissionLevel.FULL
        ss.wave.permissions.short_breakout = PermissionLevel.DISABLED

        ss.ripple.liquidity_state = "ABSORBING"
        ss.ripple.microprice = 50001.5
        ss.ripple.imbalance = 0.25

        ss.trade.archetype = TradeArchetype.BOUNCE
        ss.trade.side = TradeSide.LONG
        ss.trade.state = LifecycleState.CONFIRMATION
        ss.trade.entry_price = 50000.0
        ss.trade.stop_price = 49800.0

        ss.risk.es_budget = 2000.0
        ss.risk.consumed_es = 150.0

        self.assertEqual(ss.timestamp, 5000)
        self.assertEqual(ss.tide.bias, TideBias.LONG)
        self.assertEqual(ss.wave.regime, WaveRegime.MEAN_REVERSION)
        self.assertTrue(ss.wave.permissions.is_allowed(TradeArchetype.BOUNCE, TradeSide.LONG))
        self.assertFalse(ss.wave.permissions.is_allowed(TradeArchetype.BREAKOUT, TradeSide.SHORT))
        self.assertEqual(ss.trade.state, LifecycleState.CONFIRMATION)
        self.assertAlmostEqual(ss.risk.consumed_es, 150.0)

    def test_determinism(self):
        """Two default snapshots must be identical — deterministic construction."""
        a = StrategySnapshot.make_default()
        b = StrategySnapshot.make_default()

        self.assertEqual(a.tide.bias, b.tide.bias)
        self.assertAlmostEqual(a.tide.risk_multiplier, b.tide.risk_multiplier, places=15)
        self.assertAlmostEqual(a.tide.es_budget, b.tide.es_budget, places=15)
        self.assertEqual(a.wave.regime, b.wave.regime)
        self.assertAlmostEqual(a.wave.trend_efficiency, b.wave.trend_efficiency, places=15)
        self.assertEqual(a.wave.permissions.long_bounce, b.wave.permissions.long_bounce)
        self.assertEqual(a.trade.state, b.trade.state)

    def test_all_layers_accessible(self):
        ss = StrategySnapshot.make_default()
        self.assertIsInstance(ss.tide, TideSnapshot)
        self.assertIsInstance(ss.wave, WaveSnapshot)
        self.assertIsInstance(ss.ripple, RippleStateSnapshot)
        self.assertIsInstance(ss.trade, TradeStateSnapshot)
        self.assertIsInstance(ss.liquidity_map, LiquidityMapSnapshot)
        self.assertIsInstance(ss.risk, RiskBudgetSnapshot)
        self.assertIsInstance(ss.features, FeatureSnapshot)


class TestStrategySnapshotSerialization(unittest.TestCase):
    """Verify that StrategySnapshot can be round-tripped through a dict."""

    def test_snapshot_to_dict_and_back(self):
        from dataclasses import asdict
        ss = StrategySnapshot.make_default()
        ss.timestamp = 12345
        ss.trade.entry_price = 50000.0
        ss.risk.consumed_es = 75.0

        d = asdict(ss)
        self.assertEqual(d["timestamp"], 12345)
        self.assertAlmostEqual(d["trade"]["entry_price"], 50000.0)
        self.assertAlmostEqual(d["risk"]["consumed_es"], 75.0)
        self.assertEqual(d["tide"]["bias"], TideBias.NEUTRAL)
        self.assertEqual(d["wave"]["regime"], WaveRegime.NEUTRAL)

    def test_snapshot_json_round_trip(self):
        from dataclasses import asdict
        ss = StrategySnapshot.make_default()
        ss.timestamp = 99999
        ss.tide.bias = TideBias.LONG
        ss.trade.state = LifecycleState.EXPANSION

        d = asdict(ss)
        json_str = json.dumps(d, default=str)
        loaded = json.loads(json_str)

        self.assertEqual(loaded["timestamp"], 99999)
        self.assertEqual(loaded["tide"]["bias"], "TideBias.LONG")
        self.assertEqual(loaded["trade"]["state"], "LifecycleState.EXPANSION")


if __name__ == "__main__":
    unittest.main()
