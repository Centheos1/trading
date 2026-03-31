"""
Phase 4 tests for TideEngine (Python).

Covers:
  - Risk multiplier computation from vol_regime + LSI (§7.4.7)
  - ES budget and max_position_usd passthrough from config
  - Cadence gating (event-time)
  - Snapshot consistency
  - Determinism
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (
    TideBias,
    TideConfig,
    TideSnapshot,
    RiskBudgetSnapshot,
    VolRegime,
)
from tide.tide_engine import TideEngine


class TestTideEngineConstruction(unittest.TestCase):

    def test_default_construction(self):
        eng = TideEngine()
        snap = eng.get_snapshot()
        self.assertEqual(snap.bias, TideBias.NEUTRAL)
        self.assertAlmostEqual(snap.risk_multiplier, 1.0)
        self.assertAlmostEqual(snap.es_budget, 1000.0)
        self.assertAlmostEqual(snap.max_position_usd, 10000.0)
        self.assertEqual(snap.vol_regime, VolRegime.NORMAL)

    def test_custom_config(self):
        cfg = TideConfig(es_budget_global=500.0, max_position_usd=2000.0)
        eng = TideEngine(cfg)
        snap = eng.get_snapshot()
        self.assertAlmostEqual(snap.es_budget, 500.0)
        self.assertAlmostEqual(snap.max_position_usd, 2000.0)


class TestRiskMultiplierComputation(unittest.TestCase):

    def test_low_vol_regime(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.LOW)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 1.0)

    def test_normal_vol_regime(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.NORMAL)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.8)

    def test_high_vol_regime(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.HIGH)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.5)

    def test_crisis_vol_regime(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.CRISIS)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.0)

    def test_lsi_stress_penalty(self):
        """§7.4.7: LSI above threshold reduces risk multiplier."""
        cfg = TideConfig(lsi_reduce_threshold=1.5, lsi_reduce_slope=0.2)
        eng = TideEngine(cfg)
        eng.set_vol_regime(VolRegime.LOW)   # base = 1.0
        eng.set_liquidity_stress(2.5)       # 1.0 above threshold → penalty = 0.2
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.8)

    def test_lsi_stress_penalty_saturates(self):
        """Stress penalty cannot push multiplier below 0."""
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.LOW)   # base = 1.0
        eng.set_liquidity_stress(100.0)     # massive stress
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.0)

    def test_lsi_below_threshold_no_penalty(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.LOW)
        eng.set_liquidity_stress(1.0)       # below default threshold of 1.5
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 1.0)

    def test_combined_regime_and_lsi(self):
        """HIGH regime (0.5) + LSI penalty."""
        cfg = TideConfig(lsi_reduce_threshold=1.5, lsi_reduce_slope=0.2)
        eng = TideEngine(cfg)
        eng.set_vol_regime(VolRegime.HIGH)  # base = 0.5
        eng.set_liquidity_stress(2.0)       # 0.5 above threshold → penalty = 0.1
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().risk_multiplier, 0.4)


class TestCadenceGating(unittest.TestCase):

    def test_first_update_always_runs(self):
        eng = TideEngine()
        eng.set_vol_regime(VolRegime.HIGH)
        snap = eng.update(1000)
        self.assertAlmostEqual(snap.risk_multiplier, 0.5)

    def test_update_within_interval_skipped(self):
        cfg = TideConfig(update_interval_ms=60000)
        eng = TideEngine(cfg)
        eng.set_vol_regime(VolRegime.HIGH)
        eng.update(1000)

        eng.set_vol_regime(VolRegime.CRISIS)
        snap = eng.update(30000)  # only 29s later, skipped
        self.assertAlmostEqual(snap.risk_multiplier, 0.5, msg="skipped: still HIGH")

    def test_update_after_interval_runs(self):
        cfg = TideConfig(update_interval_ms=60000)
        eng = TideEngine(cfg)
        eng.set_vol_regime(VolRegime.HIGH)
        eng.update(1000)

        eng.set_vol_regime(VolRegime.CRISIS)
        snap = eng.update(70000)  # 69s later, beyond interval
        self.assertAlmostEqual(snap.risk_multiplier, 0.0, msg="updated to CRISIS")


class TestRiskBudgetSnapshot(unittest.TestCase):

    def test_risk_budget_snapshot_fields(self):
        cfg = TideConfig(es_budget_global=750.0, max_position_usd=5000.0)
        eng = TideEngine(cfg)
        eng.set_vol_regime(VolRegime.HIGH)
        eng.set_bias(TideBias.LONG)
        eng.update(2000)

        snap = eng.get_risk_budget()
        self.assertIsInstance(snap, RiskBudgetSnapshot)
        self.assertAlmostEqual(snap.es_budget, 750.0)
        self.assertAlmostEqual(snap.max_position_usd, 5000.0)
        self.assertAlmostEqual(snap.risk_multiplier, 0.5)
        self.assertEqual(snap.bias, TideBias.LONG)
        self.assertEqual(snap.vol_regime, VolRegime.HIGH)
        self.assertAlmostEqual(snap.consumed_es, 0.0)


class TestDeterminism(unittest.TestCase):

    def test_replay_determinism(self):
        def run():
            cfg = TideConfig(es_budget_global=1000.0)
            eng = TideEngine(cfg)
            results = []
            regimes = [VolRegime.LOW, VolRegime.NORMAL, VolRegime.HIGH, VolRegime.CRISIS]
            for i, regime in enumerate(regimes):
                eng.set_vol_regime(regime)
                eng.set_liquidity_stress(0.5 * i)
                snap = eng.update(i * 100000)
                results.append(snap.risk_multiplier)
            return results

        r1 = run()
        r2 = run()
        self.assertEqual(r1, r2, "identical inputs must produce identical outputs")


class TestBiasAndVolPassthrough(unittest.TestCase):

    def test_bias_passthrough(self):
        eng = TideEngine()
        eng.set_bias(TideBias.SHORT)
        eng.update(1000)
        self.assertEqual(eng.get_snapshot().bias, TideBias.SHORT)

    def test_realized_vol_passthrough(self):
        eng = TideEngine()
        eng.set_realized_vol(0.45)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().realized_vol, 0.45)

    def test_lsi_passthrough(self):
        eng = TideEngine()
        eng.set_liquidity_stress(2.3)
        eng.update(1000)
        self.assertAlmostEqual(eng.get_snapshot().liquidity_stress, 2.3)


class TestInvariants(unittest.TestCase):

    def test_risk_multiplier_always_in_0_1(self):
        """Property: risk_multiplier ∈ [0, 1] for all regime/LSI combos."""
        eng = TideEngine()
        for regime in VolRegime:
            for lsi in [0.0, 0.5, 1.0, 1.5, 2.0, 5.0, 100.0]:
                eng.set_vol_regime(regime)
                eng.set_liquidity_stress(lsi)
                # Force update by advancing past interval
                snap = eng.update(int(1e12))
                self.assertGreaterEqual(snap.risk_multiplier, 0.0,
                    f"regime={regime}, lsi={lsi}")
                self.assertLessEqual(snap.risk_multiplier, 1.0,
                    f"regime={regime}, lsi={lsi}")
                eng._last_update_ts = 0  # reset cadence gate


if __name__ == "__main__":
    unittest.main()
