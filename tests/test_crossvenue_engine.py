"""
Phase 8: CrossVenueEngine tests.

Validates lead/lag, divergence, and correlation features computed from
paired L1 price streams, plus storage/replay determinism.
"""

import sys
import os
import math
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from crossvenue.crossvenue_engine import (
    CrossVenueEngine,
    CrossVenueConfig,
    CrossVenueSnapshot,
    _pearson,
    _mean,
)
from crossvenue.crossvenue_store import (
    save_crossvenue_prices,
    load_crossvenue_prices,
    replay_crossvenue,
)


class TestPearson(unittest.TestCase):

    def test_perfect_positive(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [2.0, 4.0, 6.0, 8.0, 10.0]
        self.assertAlmostEqual(_pearson(xs, ys), 1.0, places=10)

    def test_perfect_negative(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [10.0, 8.0, 6.0, 4.0, 2.0]
        self.assertAlmostEqual(_pearson(xs, ys), -1.0, places=10)

    def test_uncorrelated(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        ys = [3.0, 1.0, 4.0, 1.0, 5.0]
        c = _pearson(xs, ys)
        self.assertTrue(-1.0 <= c <= 1.0)

    def test_degenerate_input(self):
        self.assertEqual(_pearson([], []), 0.0)
        self.assertEqual(_pearson([1.0], [2.0]), 0.0)
        self.assertEqual(_pearson([1.0, 1.0, 1.0], [2.0, 3.0, 4.0]), 0.0)

    def test_mean(self):
        self.assertEqual(_mean([]), 0.0)
        self.assertAlmostEqual(_mean([1.0, 2.0, 3.0]), 2.0)


class TestCrossVenueEngine(unittest.TestCase):

    def _make_engine(self, **kwargs):
        cfg = CrossVenueConfig(
            window_ms=30_000,
            cadence_ms=1_000,
            max_lag_steps=3,
            min_observations=5,
            **kwargs,
        )
        return CrossVenueEngine(cfg, primary="binance", secondary="oanda")

    def _feed_correlated(self, engine, n=30, base=100.0, start_ts=0):
        """Feed correlated price series to both venues (same timestamps)."""
        import math
        ts = start_ts
        for i in range(n):
            ts += 1000
            p = base + 0.5 * math.sin(i * 0.3)
            engine.on_price("binance", p, ts)
            engine.on_price("oanda", p + 0.5, ts)
        return ts

    def _feed_divergent(self, engine, n=30, base=100.0, start_ts=0):
        """Feed anti-correlated price series (one zigs, other zags)."""
        import math
        ts = start_ts
        for i in range(n):
            ts += 1000
            move = 0.5 * math.sin(i * 0.5)
            engine.on_price("binance", base + move, ts)
            engine.on_price("oanda", base - move, ts)
        return ts

    def test_initial_snapshot(self):
        engine = self._make_engine()
        snap = engine.get_snapshot()
        self.assertEqual(snap.correlation, 0.0)
        self.assertEqual(snap.divergence, 0.0)
        self.assertEqual(snap.lead_lag, 0.0)
        self.assertEqual(snap.n_paired, 0)

    def test_cadence_gating(self):
        engine = self._make_engine()
        engine.on_price("binance", 100.0, 1000)
        snap = engine.update(500)
        self.assertIsNone(snap)
        snap = engine.update(2000)
        self.assertIsNotNone(snap)

    def test_correlated_prices_high_correlation(self):
        engine = self._make_engine()
        self._feed_correlated(engine, n=30)
        snap = engine.update(31000)
        self.assertIsNotNone(snap)
        self.assertGreater(snap.correlation, 0.8)
        self.assertGreater(snap.n_paired, 5)

    def test_divergent_prices_low_correlation(self):
        engine = self._make_engine()
        self._feed_divergent(engine, n=30)
        snap = engine.update(31000)
        self.assertIsNotNone(snap)
        self.assertLess(snap.correlation, 0.0)

    def test_divergence_sign(self):
        engine = self._make_engine()
        self._feed_divergent(engine, n=30)
        snap = engine.update(31000)
        self.assertIsNotNone(snap)
        # Primary rises, secondary falls → positive divergence
        self.assertGreater(snap.divergence, 0.0)

    def test_lead_lag_zero_for_synced(self):
        engine = self._make_engine()
        self._feed_correlated(engine, n=30)
        snap = engine.update(31000)
        self.assertIsNotNone(snap)
        self.assertAlmostEqual(snap.lead_lag, 0.0, places=0)

    def test_reset(self):
        engine = self._make_engine()
        self._feed_correlated(engine, n=20)
        engine.update(21000)
        engine.reset()
        snap = engine.get_snapshot()
        self.assertEqual(snap.correlation, 0.0)
        self.assertEqual(snap.n_paired, 0)

    def test_unknown_venue_ignored(self):
        engine = self._make_engine()
        engine.on_price("unknown_venue", 100.0, 1000)
        snap = engine.get_snapshot()
        self.assertEqual(snap.n_paired, 0)

    def test_negative_price_ignored(self):
        engine = self._make_engine()
        engine.on_price("binance", -5.0, 1000)
        engine.on_price("binance", 0.0, 2000)
        snap = engine.get_snapshot()
        self.assertEqual(snap.n_paired, 0)

    def test_determinism(self):
        """Same inputs → identical outputs."""
        def run():
            engine = self._make_engine()
            self._feed_correlated(engine, n=25)
            self._feed_divergent(engine, n=15, start_ts=25000)
            return engine.update(41000)

        s1 = run()
        s2 = run()
        self.assertEqual(s1.correlation, s2.correlation)
        self.assertEqual(s1.divergence, s2.divergence)
        self.assertEqual(s1.lead_lag, s2.lead_lag)
        self.assertEqual(s1.n_paired, s2.n_paired)

    def test_venue_names_in_snapshot(self):
        engine = self._make_engine()
        snap = engine.get_snapshot()
        self.assertEqual(snap.primary_venue, "binance")
        self.assertEqual(snap.secondary_venue, "oanda")

    def test_window_trimming(self):
        engine = self._make_engine()
        for i in range(100):
            ts = i * 1000
            engine.on_price("binance", 100.0 + i * 0.01, ts)
            engine.on_price("oanda", 100.0 + i * 0.01, ts + 50)
        snap = engine.update(100000)
        self.assertIsNotNone(snap)
        self.assertLessEqual(snap.n_paired, 35)


class TestCrossVenueStore(unittest.TestCase):

    def test_save_load_round_trip(self):
        records = [
            (1000, "binance", 100.123456),
            (1000, "oanda", 100.567890),
            (2000, "binance", 100.200000),
            (2000, "oanda", 100.600000),
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            save_crossvenue_prices(records, path)
            loaded = load_crossvenue_prices(path)
            self.assertEqual(len(loaded), 4)
            for (ts1, v1, p1), (ts2, v2, p2) in zip(records, loaded):
                self.assertEqual(ts1, ts2)
                self.assertEqual(v1, v2)
                self.assertAlmostEqual(p1, p2, places=6)
        finally:
            os.unlink(path)

    def test_load_nonexistent(self):
        records = load_crossvenue_prices("/nonexistent/path.csv")
        self.assertEqual(records, [])

    def test_replay_determinism(self):
        records = []
        for i in range(50):
            ts = i * 1000
            records.append((ts, "binance", 100.0 + i * 0.1))
            records.append((ts + 50, "oanda", 100.0 + i * 0.1 + 0.5))

        cfg = CrossVenueConfig(
            window_ms=30_000, cadence_ms=1_000, min_observations=5,
        )
        e1 = CrossVenueEngine(cfg, primary="binance", secondary="oanda")
        e2 = CrossVenueEngine(cfg, primary="binance", secondary="oanda")

        snaps1 = replay_crossvenue(records, e1)
        snaps2 = replay_crossvenue(records, e2)

        self.assertEqual(len(snaps1), len(snaps2))
        for s1, s2 in zip(snaps1, snaps2):
            self.assertEqual(s1.correlation, s2.correlation)
            self.assertEqual(s1.divergence, s2.divergence)
            self.assertEqual(s1.lead_lag, s2.lead_lag)

    def test_sorted_on_load(self):
        records = [
            (3000, "binance", 100.0),
            (1000, "oanda", 99.0),
            (2000, "binance", 100.5),
        ]
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
            path = f.name
        try:
            save_crossvenue_prices(records, path)
            loaded = load_crossvenue_prices(path)
            timestamps = [ts for ts, _, _ in loaded]
            self.assertEqual(timestamps, sorted(timestamps))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
