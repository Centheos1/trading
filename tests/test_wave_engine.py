"""Tests for wave.wave_engine — strategy.md §8, §18."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (
    TideBias,
    WaveRegime,
    PermissionLevel,
    TradeArchetype,
    TradeSide,
    PermissionSet,
    WaveSnapshot,
    WaveConfig,
)
from wave.wave_engine import (
    WaveEngine,
    compute_trend_efficiency,
    compute_distance_to_structure,
    lookup_permissions,
)


# ── Trend efficiency (§8.5.4) ─────────────────────────────────────────

class TestTrendEfficiency(unittest.TestCase):
    def test_monotonic_up(self):
        """Perfectly trending up → η = 1.0."""
        prices = [100.0, 101.0, 102.0, 103.0, 104.0]
        self.assertAlmostEqual(compute_trend_efficiency(prices), 1.0)

    def test_monotonic_down(self):
        """Perfectly trending down → η = 1.0."""
        prices = [104.0, 103.0, 102.0, 101.0, 100.0]
        self.assertAlmostEqual(compute_trend_efficiency(prices), 1.0)

    def test_choppy(self):
        """Alternating → η near 0."""
        prices = [100.0, 101.0, 100.0, 101.0, 100.0]
        eta = compute_trend_efficiency(prices)
        self.assertLess(eta, 0.1)

    def test_round_trip(self):
        """Goes up then returns → η = 0."""
        prices = [100.0, 102.0, 104.0, 102.0, 100.0]
        self.assertAlmostEqual(compute_trend_efficiency(prices), 0.0)

    def test_single_price(self):
        """Single observation → 0.0."""
        self.assertAlmostEqual(compute_trend_efficiency([100.0]), 0.0)

    def test_empty(self):
        self.assertAlmostEqual(compute_trend_efficiency([]), 0.0)

    def test_two_prices_up(self):
        """Two prices going up → η = 1.0."""
        self.assertAlmostEqual(compute_trend_efficiency([100.0, 105.0]), 1.0)

    def test_flat_prices(self):
        """All same price → 0.0 (gross = 0)."""
        self.assertAlmostEqual(compute_trend_efficiency([100.0, 100.0, 100.0]), 0.0)


# ── Distance to structure (§8.5.5) ────────────────────────────────────

class TestDistanceToStructure(unittest.TestCase):
    def test_basic(self):
        d = compute_distance_to_structure(
            100.0, session_vwap=95.0, session_high=110.0, session_low=90.0
        )
        self.assertAlmostEqual(d["distance_vwap"], (100.0 - 95.0) / 100.0)
        self.assertAlmostEqual(d["distance_session_high"], (100.0 - 110.0) / 100.0)
        self.assertAlmostEqual(d["distance_session_low"], (100.0 - 90.0) / 100.0)

    def test_zero_price(self):
        d = compute_distance_to_structure(0.0, session_vwap=95.0)
        self.assertEqual(d, {})

    def test_no_structure(self):
        d = compute_distance_to_structure(100.0)
        self.assertEqual(d, {})

    def test_partial_structure(self):
        d = compute_distance_to_structure(100.0, session_vwap=100.0)
        self.assertAlmostEqual(d["distance_vwap"], 0.0)
        self.assertNotIn("distance_session_high", d)


# ── Permissions matrix (§18) ──────────────────────────────────────────

class TestPermissionsMatrix(unittest.TestCase):
    def test_all_cells_defined(self):
        """Every (TideBias × WaveRegime) combination must return a valid PermissionSet."""
        for bias in TideBias:
            for regime in WaveRegime:
                ps = lookup_permissions(bias, regime)
                self.assertIsInstance(ps, PermissionSet)
                for arch in TradeArchetype:
                    for side in TradeSide:
                        level = ps.get(arch, side)
                        self.assertIn(level, list(PermissionLevel))

    def test_breakdown_disables_all_for_long_bias(self):
        ps = lookup_permissions(TideBias.LONG, WaveRegime.BREAKDOWN)
        for arch in TradeArchetype:
            for side in TradeSide:
                self.assertEqual(ps.get(arch, side), PermissionLevel.DISABLED)

    def test_breakdown_disables_all_for_short_bias(self):
        ps = lookup_permissions(TideBias.SHORT, WaveRegime.BREAKDOWN)
        for arch in TradeArchetype:
            for side in TradeSide:
                self.assertEqual(ps.get(arch, side), PermissionLevel.DISABLED)

    def test_long_mean_reversion(self):
        ps = lookup_permissions(TideBias.LONG, WaveRegime.MEAN_REVERSION)
        self.assertEqual(ps.long_bounce, PermissionLevel.FULL)
        self.assertEqual(ps.short_bounce, PermissionLevel.REDUCED)
        self.assertEqual(ps.long_breakout, PermissionLevel.FULL)
        self.assertEqual(ps.short_breakout, PermissionLevel.DISABLED)

    def test_neutral_neutral(self):
        ps = lookup_permissions(TideBias.NEUTRAL, WaveRegime.NEUTRAL)
        self.assertEqual(ps.long_bounce, PermissionLevel.FULL)
        self.assertEqual(ps.short_bounce, PermissionLevel.FULL)
        self.assertEqual(ps.long_breakout, PermissionLevel.REDUCED)
        self.assertEqual(ps.short_breakout, PermissionLevel.REDUCED)

    def test_neutral_breakout(self):
        ps = lookup_permissions(TideBias.NEUTRAL, WaveRegime.BREAKOUT)
        self.assertEqual(ps.long_bounce, PermissionLevel.REDUCED)
        self.assertEqual(ps.short_bounce, PermissionLevel.REDUCED)
        self.assertEqual(ps.long_breakout, PermissionLevel.FULL)
        self.assertEqual(ps.short_breakout, PermissionLevel.FULL)

    def test_neutral_breakdown_reduced_breakouts(self):
        ps = lookup_permissions(TideBias.NEUTRAL, WaveRegime.BREAKDOWN)
        self.assertEqual(ps.long_bounce, PermissionLevel.DISABLED)
        self.assertEqual(ps.short_bounce, PermissionLevel.DISABLED)
        self.assertEqual(ps.long_breakout, PermissionLevel.REDUCED)
        self.assertEqual(ps.short_breakout, PermissionLevel.REDUCED)

    def test_short_mean_reversion(self):
        ps = lookup_permissions(TideBias.SHORT, WaveRegime.MEAN_REVERSION)
        self.assertEqual(ps.long_bounce, PermissionLevel.REDUCED)
        self.assertEqual(ps.short_bounce, PermissionLevel.FULL)
        self.assertEqual(ps.long_breakout, PermissionLevel.DISABLED)
        self.assertEqual(ps.short_breakout, PermissionLevel.FULL)

    def test_short_breakout(self):
        ps = lookup_permissions(TideBias.SHORT, WaveRegime.BREAKOUT)
        self.assertEqual(ps.long_bounce, PermissionLevel.DISABLED)
        self.assertEqual(ps.short_bounce, PermissionLevel.REDUCED)
        self.assertEqual(ps.long_breakout, PermissionLevel.DISABLED)
        self.assertEqual(ps.short_breakout, PermissionLevel.FULL)

    def test_reduced_size_fraction_passthrough(self):
        ps = lookup_permissions(TideBias.NEUTRAL, WaveRegime.NEUTRAL, reduced_size_fraction=0.3)
        self.assertAlmostEqual(ps.reduced_size_fraction, 0.3)

    def test_is_allowed(self):
        ps = lookup_permissions(TideBias.LONG, WaveRegime.BREAKDOWN)
        self.assertFalse(ps.is_allowed(TradeArchetype.BOUNCE, TradeSide.LONG))

    def test_size_fraction_values(self):
        ps = lookup_permissions(TideBias.NEUTRAL, WaveRegime.NEUTRAL)
        self.assertEqual(
            ps.get(TradeArchetype.BOUNCE, TradeSide.LONG),
            PermissionLevel.FULL,
        )
        self.assertAlmostEqual(ps.reduced_size_fraction, 0.5)


# ── Regime classification (§8.8 state machine) ────────────────────────

class TestRegimeStateMachine(unittest.TestCase):
    def _make_engine(self, **kw) -> WaveEngine:
        cfg = WaveConfig(**kw)
        return WaveEngine(cfg)

    def test_starts_neutral(self):
        e = self._make_engine()
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)

    def test_neutral_to_mean_reversion(self):
        e = self._make_engine(eta_mr_threshold=0.3, dispersion_threshold=0.02)
        # Feed prices that produce low trend efficiency (choppy)
        ts = 1000
        for i in range(30):
            price = 100.0 + (1 if i % 2 == 0 else -1) * 0.1
            e.on_price(price, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.MEAN_REVERSION)

    def test_neutral_to_breakout(self):
        e = self._make_engine(eta_bo_threshold=0.7)
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + i * 1.0, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.BREAKOUT)

    def test_neutral_to_breakdown_via_ar(self):
        # Engine contract (wave_engine.py:_classify_regime): BREAKDOWN
        # via the AR pathway requires extreme_stress = (ar > ar_critical
        # AND d > dispersion_threshold). AR alone is not sufficient
        # because AR can sit ~0.9 in normal markets — see the docstring
        # warning about "orderly bull runs". Set both.
        e = self._make_engine(ar_critical=0.85, dispersion_threshold=0.02)
        e.set_absorption_ratio(0.90)
        e.set_dispersion(0.03)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_neutral_to_breakdown_via_dispersion(self):
        e = self._make_engine(dispersion_critical=0.05)
        e.set_dispersion(0.06)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_breakdown_recovery(self):
        e = self._make_engine(ar_critical=0.85, ar_recover=0.70, dispersion_threshold=0.02)
        # Multi-factor stress required to enter BREAKDOWN.
        e.set_absorption_ratio(0.90)
        e.set_dispersion(0.03)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)
        # Recover: AR below recover AND D below threshold (BREAKDOWN exit
        # rule, wave_engine.py:441).
        e.set_absorption_ratio(0.65)
        e.set_dispersion(0.01)
        e.update(6000)
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)

    def test_breakdown_stays_if_ar_still_high(self):
        e = self._make_engine(ar_critical=0.85, ar_recover=0.70,
                              dispersion_threshold=0.02)
        # Enter BREAKDOWN with both AR and dispersion elevated.
        e.set_absorption_ratio(0.90)
        e.set_dispersion(0.03)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)
        # AR drops below ar_recover threshold but dispersion stays
        # elevated → BREAKDOWN exit predicate (wave_engine.py:441) is
        # NOT satisfied, so we stay in BREAKDOWN.
        e.set_absorption_ratio(0.75)
        e.set_dispersion(0.03)
        e.update(6000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_mean_reversion_to_breakout(self):
        e = self._make_engine(eta_mr_threshold=0.3, eta_bo_threshold=0.7,
                              dispersion_threshold=0.02)
        # First get to MEAN_REVERSION with choppy prices
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + (0.05 if i % 2 == 0 else -0.05), ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.MEAN_REVERSION)

        # Now make it strongly trending → BREAKOUT
        e._prices.clear()
        for i in range(30):
            e.on_price(100.0 + i * 1.0, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.BREAKOUT)

    def test_mean_reversion_to_neutral(self):
        e = self._make_engine(eta_mr_threshold=0.3, eta_neutral_threshold=0.5,
                              eta_bo_threshold=0.7)
        # Get to MEAN_REVERSION
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + (0.05 if i % 2 == 0 else -0.05), ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.MEAN_REVERSION)

        # Produce η ≈ 0.6 (above η_neutral=0.5, below η_bo=0.7):
        # Pattern per 5-step cycle: +1, +1, +1, -1, +1 → net=3, gross=5, η=0.6
        e._prices.clear()
        p = 100.0
        for i in range(30):
            if i % 5 == 3:
                p -= 1.0
            else:
                p += 1.0
            e.on_price(p, ts)
            ts += 2000
        e.update(ts)
        eta = e.trend_efficiency
        self.assertGreater(eta, 0.5, "η must exceed η_neutral for this transition")
        self.assertLess(eta, 0.7, "η must be below η_bo to land in NEUTRAL, not BREAKOUT")
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)

    def test_breakout_to_neutral(self):
        e = self._make_engine(eta_bo_threshold=0.7, eta_neutral_threshold=0.5)
        # Get to BREAKOUT
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + i * 1.0, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.BREAKOUT)

        # Now choppy → η drops below neutral threshold → NEUTRAL
        e._prices.clear()
        for i in range(30):
            e.on_price(100.0 + (0.2 if i % 2 == 0 else -0.2), ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)

    def test_breakout_to_breakdown(self):
        e = self._make_engine(ar_critical=0.85, dispersion_threshold=0.02)
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + i * 1.0, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.BREAKOUT)
        # AR + dispersion spike together → extreme_stress fires → BREAKDOWN.
        # AR alone is insufficient (engine docstring: "BOTH AR and
        # dispersion must be elevated").
        e.set_absorption_ratio(0.90)
        e.set_dispersion(0.03)
        e.update(ts + 5000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_mean_reversion_to_breakdown(self):
        e = self._make_engine(ar_critical=0.85, eta_mr_threshold=0.3,
                              dispersion_threshold=0.02)
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + (0.05 if i % 2 == 0 else -0.05), ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.MEAN_REVERSION)
        # Multi-factor stress required.
        e.set_absorption_ratio(0.90)
        e.set_dispersion(0.03)
        e.update(ts + 5000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)


# ── Cadence gating ────────────────────────────────────────────────────

class TestCadenceGating(unittest.TestCase):
    def test_first_update_always_passes(self):
        e = WaveEngine(WaveConfig(update_interval_ms=5000))
        snap = e.update(1000)
        self.assertIsNotNone(snap)

    def test_too_soon_returns_none(self):
        e = WaveEngine(WaveConfig(update_interval_ms=5000))
        e.update(1000)
        snap = e.update(2000)
        self.assertIsNone(snap)

    def test_at_boundary_passes(self):
        e = WaveEngine(WaveConfig(update_interval_ms=5000))
        e.update(1000)
        snap = e.update(6000)
        self.assertIsNotNone(snap)

    def test_after_boundary_passes(self):
        e = WaveEngine(WaveConfig(update_interval_ms=5000))
        e.update(1000)
        snap = e.update(7000)
        self.assertIsNotNone(snap)


# ── WaveEngine integration ────────────────────────────────────────────

class TestWaveEngineIntegration(unittest.TestCase):
    def test_snapshot_has_correct_fields(self):
        e = WaveEngine()
        # Feed prices with moderate trend (η ~ 0.5) to stay NEUTRAL
        ts = 1000
        for i in range(20):
            e.on_price(100.0 + (i % 3) * 0.5, ts)
            ts += 500
        snap = e.update(ts, TideBias.NEUTRAL)
        self.assertIsInstance(snap, WaveSnapshot)
        self.assertEqual(snap.timestamp, ts)
        self.assertIsInstance(snap.permissions, PermissionSet)
        self.assertIn(snap.regime, list(WaveRegime))

    def test_session_structure_distances(self):
        e = WaveEngine()
        e.on_price(100.0, 1000)
        e.set_session_structure(vwap=95.0, high=110.0, low=90.0)
        e.update(1000)
        d = e.distance_to_structure
        self.assertIn("distance_vwap", d)
        self.assertIn("distance_session_high", d)
        self.assertIn("distance_session_low", d)

    def test_get_snapshot_no_cadence(self):
        """get_snapshot() always returns the current state, no cadence gate."""
        e = WaveEngine(WaveConfig(update_interval_ms=5000))
        e.update(1000)
        snap = e.get_snapshot(TideBias.LONG)
        self.assertIsInstance(snap, WaveSnapshot)
        self.assertEqual(snap.timestamp, 1000)

    def test_reset(self):
        e = WaveEngine()
        e.on_price(100.0, 1000)
        e.set_dispersion(0.5)
        e.set_absorption_ratio(0.9)
        e.update(1000)
        e.reset()
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)
        self.assertEqual(e.last_update_ts, 0)
        self.assertAlmostEqual(e.dispersion, 0.0)
        self.assertAlmostEqual(e.absorption_ratio, 0.5)
        self.assertAlmostEqual(e.trend_efficiency, 0.5)

    def test_negative_price_ignored(self):
        e = WaveEngine()
        e.on_price(-5.0, 1000)
        self.assertAlmostEqual(e.trend_efficiency, 0.5)

    def test_price_window_trimming(self):
        e = WaveEngine()
        e.set_price_window_ms(10_000)
        for i in range(100):
            e.on_price(100.0 + i, i * 1000)
        # Prices older than 10s should be trimmed
        self.assertLessEqual(len(e._prices), 12)


# ── Determinism ───────────────────────────────────────────────────────

class TestDeterminism(unittest.TestCase):
    def test_identical_inputs_identical_outputs(self):
        """Same event sequence twice → identical snapshots."""
        def run_once():
            e = WaveEngine(WaveConfig(update_interval_ms=5000))
            results = []
            ts = 0
            for i in range(50):
                e.on_price(100.0 + i * 0.5, ts)
                ts += 1000
                if ts % 5000 == 0:
                    snap = e.update(ts, TideBias.NEUTRAL)
                    if snap:
                        results.append((snap.regime, snap.trend_efficiency,
                                        snap.dispersion, snap.absorption_ratio))
            return results

        r1 = run_once()
        r2 = run_once()
        self.assertEqual(r1, r2)

    def test_determinism_with_regime_transitions(self):
        """Verify determinism through regime transitions."""
        def run_once():
            e = WaveEngine(WaveConfig(update_interval_ms=1000))
            results = []
            ts = 0
            # Phase 1: choppy (→ MEAN_REVERSION)
            for i in range(20):
                e.on_price(100.0 + (0.1 if i % 2 == 0 else -0.1), ts)
                ts += 500
            e.update(ts)
            results.append(e.regime)
            # Phase 2: trending (→ BREAKOUT)
            for i in range(20):
                e.on_price(100.0 + i * 1.0, ts)
                ts += 500
            ts += 500
            e.update(ts)
            results.append(e.regime)
            return results

        self.assertEqual(run_once(), run_once())


# ── Property-based invariants (§20.6) ─────────────────────────────────

class TestPropertyInvariants(unittest.TestCase):
    def test_permissions_never_undefined(self):
        """§20.6: Permissions matrix lookup never returns an undefined state."""
        for bias in TideBias:
            for regime in WaveRegime:
                ps = lookup_permissions(bias, regime)
                for arch in TradeArchetype:
                    for side in TradeSide:
                        level = ps.get(arch, side)
                        self.assertIn(level, [
                            PermissionLevel.FULL,
                            PermissionLevel.REDUCED,
                            PermissionLevel.DISABLED,
                        ])

    def test_trend_efficiency_bounded(self):
        """η ∈ [0, 1] for all inputs."""
        import random
        rng = random.Random(42)
        for _ in range(100):
            n = rng.randint(2, 50)
            prices = [rng.uniform(50.0, 150.0) for _ in range(n)]
            eta = compute_trend_efficiency(prices)
            self.assertGreaterEqual(eta, 0.0)
            self.assertLessEqual(eta, 1.0 + 1e-9)

    def test_regime_is_always_valid(self):
        """After any sequence of updates, regime is a valid WaveRegime."""
        import random
        rng = random.Random(42)
        e = WaveEngine(WaveConfig(update_interval_ms=100))
        ts = 0
        for _ in range(200):
            e.on_price(rng.uniform(90.0, 110.0), ts)
            e.set_dispersion(rng.uniform(0.0, 0.1))
            e.set_absorption_ratio(rng.uniform(0.0, 1.0))
            ts += 100
            if rng.random() > 0.3:
                e.update(ts, rng.choice(list(TideBias)))
            self.assertIn(e.regime, list(WaveRegime))


# ── Boundary / edge cases ─────────────────────────────────────────────

class TestBoundaryEdgeCases(unittest.TestCase):
    def test_zero_dispersion_zero_ar(self):
        """Default V1 single-symbol: D=0, AR=0.5 → regime keys off η only."""
        e = WaveEngine()
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + i, ts)
            ts += 2000
        e.update(ts)
        self.assertEqual(e.regime, WaveRegime.BREAKOUT)

    def test_ar_exactly_at_critical(self):
        """AR == ar_critical should NOT trigger BREAKDOWN (requires >)."""
        e = WaveEngine(WaveConfig(ar_critical=0.85, eta_mr_threshold=0.3))
        # Feed trending prices to keep η high (above mr threshold)
        ts = 1000
        for i in range(30):
            e.on_price(100.0 + i * 0.5, ts)
            ts += 2000
        e.set_absorption_ratio(0.85)
        e.update(ts)
        self.assertNotEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_ar_just_above_critical(self):
        # AR just above critical AND dispersion just above threshold →
        # extreme_stress fires → BREAKDOWN. AR-alone-with-zero-dispersion
        # would NOT trigger BREAKDOWN by design.
        e = WaveEngine(WaveConfig(ar_critical=0.85, dispersion_threshold=0.02))
        e.set_absorption_ratio(0.851)
        e.set_dispersion(0.021)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)

    def test_dispersion_exactly_at_critical(self):
        e = WaveEngine(WaveConfig(dispersion_critical=0.05))
        e.set_dispersion(0.05)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.NEUTRAL)

    def test_dispersion_just_above_critical(self):
        e = WaveEngine(WaveConfig(dispersion_critical=0.05))
        e.set_dispersion(0.051)
        e.update(1000)
        self.assertEqual(e.regime, WaveRegime.BREAKDOWN)


if __name__ == "__main__":
    unittest.main()
