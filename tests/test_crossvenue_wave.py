"""
Phase 8: Cross-venue → Wave integration tests.

Validates that cross-venue features modify Wave regime classification
appropriately, and that the rule-based baseline is preserved when
cross-venue data is not available.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from wave.wave_engine import WaveEngine
from schemas import WaveRegime, WaveConfig, TideBias


class TestCrossVenueWaveIntegration(unittest.TestCase):

    def _make_engine(self, **kwargs):
        cfg = WaveConfig(**kwargs)
        return WaveEngine(cfg)

    def _feed_neutral_prices(self, engine, n=50, base=100.0, start_ts=0):
        """Feed prices that produce eta ~ 0.5 (NEUTRAL territory)."""
        ts = start_ts
        for i in range(n):
            ts += 1000
            p = base + 0.5 * (1 if i % 2 == 0 else -1) * (i % 5) * 0.01
            engine.on_price(p, ts)
        return ts

    def test_no_crossvenue_preserves_baseline(self):
        """Without cross-venue data, regime is purely rule-based."""
        engine = self._make_engine()
        ts = self._feed_neutral_prices(engine, n=30)
        snap = engine.update(ts, TideBias.NEUTRAL)
        self.assertFalse(engine.cv_available)
        self.assertIn(snap.regime, [
            WaveRegime.NEUTRAL, WaveRegime.MEAN_REVERSION,
            WaveRegime.BREAKOUT, WaveRegime.BREAKDOWN,
        ])

    def test_high_correlation_no_breakdown(self):
        """High cross-venue correlation should NOT push toward BREAKDOWN."""
        engine = self._make_engine()
        ts = self._feed_neutral_prices(engine, n=30)
        engine.set_crossvenue_snapshot(
            correlation=0.95, divergence=0.0, lead_lag=0.0
        )
        snap = engine.update(ts + 5000, TideBias.NEUTRAL)
        self.assertTrue(engine.cv_available)
        self.assertNotEqual(snap.regime, WaveRegime.BREAKDOWN)

    def test_low_correlation_boosts_breakdown(self):
        """Low cross-venue correlation should boost effective AR toward BREAKDOWN.

        Engine contract: extreme_stress requires BOTH effective_AR >
        ar_critical AND effective_d > dispersion_threshold. Low
        correlation boosts effective AR (wave_engine.py:396); a small
        dispersion is set so the multi-factor stress predicate fires.
        """
        engine = self._make_engine(ar_critical=0.7, dispersion_threshold=0.02)
        ts = self._feed_neutral_prices(engine, n=30)

        engine.set_absorption_ratio(0.55)
        engine.set_dispersion(0.03)
        engine.set_crossvenue_snapshot(
            correlation=-0.3, divergence=0.0, lead_lag=0.0
        )
        snap = engine.update(ts + 5000, TideBias.NEUTRAL)
        self.assertEqual(snap.regime, WaveRegime.BREAKDOWN)

    def test_large_divergence_boosts_dispersion(self):
        """Large cross-venue divergence should boost effective dispersion."""
        engine = self._make_engine(
            dispersion_critical=0.05,
            ar_critical=0.9,
        )
        ts = self._feed_neutral_prices(engine, n=30)
        engine.set_crossvenue_snapshot(
            correlation=0.5, divergence=0.04, lead_lag=0.0
        )
        snap = engine.update(ts + 5000, TideBias.NEUTRAL)
        self.assertEqual(snap.regime, WaveRegime.BREAKDOWN)

    def test_reset_clears_crossvenue(self):
        engine = self._make_engine()
        engine.set_crossvenue_snapshot(
            correlation=0.1, divergence=0.05, lead_lag=2.0
        )
        self.assertTrue(engine.cv_available)
        engine.reset()
        self.assertFalse(engine.cv_available)
        self.assertAlmostEqual(engine.cv_correlation, 1.0)
        self.assertAlmostEqual(engine.cv_divergence, 0.0)

    def test_crossvenue_accessors(self):
        engine = self._make_engine()
        engine.set_crossvenue_snapshot(
            correlation=0.8, divergence=-0.02, lead_lag=1.5
        )
        self.assertAlmostEqual(engine.cv_correlation, 0.8)
        self.assertAlmostEqual(engine.cv_divergence, -0.02)
        self.assertAlmostEqual(engine.cv_lead_lag, 1.5)

    def test_correlation_clamped(self):
        engine = self._make_engine()
        engine.set_crossvenue_snapshot(correlation=5.0, divergence=0.0)
        self.assertAlmostEqual(engine.cv_correlation, 1.0)
        engine.set_crossvenue_snapshot(correlation=-5.0, divergence=0.0)
        self.assertAlmostEqual(engine.cv_correlation, -1.0)

    def test_crossvenue_determinism(self):
        """Same inputs with cross-venue features → identical regime."""
        def run():
            engine = self._make_engine(ar_critical=0.7)
            for i in range(30):
                engine.on_price(100.0 + i * 0.01, 1000 + i * 1000)
            engine.set_absorption_ratio(0.5)
            engine.set_crossvenue_snapshot(
                correlation=0.2, divergence=0.01, lead_lag=0.0
            )
            return engine.update(31000 + 5000, TideBias.NEUTRAL)

        s1 = run()
        s2 = run()
        self.assertEqual(s1.regime, s2.regime)
        self.assertEqual(s1.trend_efficiency, s2.trend_efficiency)

    def test_neutral_regime_stable_with_moderate_crossvenue(self):
        """Moderate cross-venue signals shouldn't cause spurious transitions."""
        engine = self._make_engine()
        ts = self._feed_neutral_prices(engine, n=30)
        engine.set_crossvenue_snapshot(
            correlation=0.6, divergence=0.005, lead_lag=0.0
        )
        snap = engine.update(ts + 5000, TideBias.NEUTRAL)
        self.assertNotEqual(snap.regime, WaveRegime.BREAKDOWN)

    def test_divergence_boost_override_changes_regime(self):
        """Phase 14D — overriding `crossvenue_divergence_boost` from its
        default (2.0) to a larger value measurably changes effective
        dispersion in `_classify_regime`, flipping the regime decision.

        Setup: divergence=0.018, dispersion_critical=0.05.
        - Default boost 2.0  → effective_d ≈ 0.036 < 0.05  → NOT BREAKDOWN.
        - Override boost 3.0 → effective_d ≈ 0.054 > 0.05  → BREAKDOWN.

        Pins the V1 §22.2 #14 / AGENT_STRATEGY_RULES.md §20 "no magic
        constants" contract: the boost factor MUST be a `WaveConfig`
        parameter, not a hardcoded literal."""
        # Default boost path → no dispersion crisis, no BREAKDOWN.
        eng_default = self._make_engine()
        ts1 = self._feed_neutral_prices(eng_default, n=30)
        eng_default.set_crossvenue_snapshot(
            correlation=1.0, divergence=0.018, lead_lag=0.0
        )
        snap_default = eng_default.update(ts1 + 5000, TideBias.NEUTRAL)
        self.assertNotEqual(snap_default.regime, WaveRegime.BREAKDOWN)

        # Override boost=3.0 → effective dispersion crosses critical → BREAKDOWN.
        eng_boosted = self._make_engine(crossvenue_divergence_boost=3.0)
        ts2 = self._feed_neutral_prices(eng_boosted, n=30)
        eng_boosted.set_crossvenue_snapshot(
            correlation=1.0, divergence=0.018, lead_lag=0.0
        )
        snap_boosted = eng_boosted.update(ts2 + 5000, TideBias.NEUTRAL)
        self.assertEqual(snap_boosted.regime, WaveRegime.BREAKDOWN)

    def test_correlation_boost_override_disables_ar_boost(self):
        """Phase 14D — overriding `crossvenue_correlation_boost` to 0.0
        disables the AR boost entirely. The exact scenario that
        triggers BREAKDOWN in `test_low_correlation_boosts_breakdown`
        (default boost=0.5) must NOT trigger BREAKDOWN when boost=0.0.

        With boost=0.0:
          effective_ar = ar + corr_deficit * 0.0 = ar (unchanged)
          → 0.55 < ar_critical (0.7) → no extreme_stress → no BREAKDOWN."""
        # Identical setup to `test_low_correlation_boosts_breakdown`
        # except boost=0.0 — verifies the AR boost is the only thing
        # that pushes the engine into BREAKDOWN in that scenario.
        engine = self._make_engine(
            ar_critical=0.7,
            dispersion_threshold=0.02,
            crossvenue_correlation_boost=0.0,
        )
        ts = self._feed_neutral_prices(engine, n=30)
        engine.set_absorption_ratio(0.55)
        engine.set_dispersion(0.03)
        engine.set_crossvenue_snapshot(
            correlation=-0.3, divergence=0.0, lead_lag=0.0
        )
        snap = engine.update(ts + 5000, TideBias.NEUTRAL)
        self.assertNotEqual(snap.regime, WaveRegime.BREAKDOWN)


if __name__ == "__main__":
    unittest.main()
