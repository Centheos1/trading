"""
Unit tests for tide/tide_accuracy.py

Tests cover:
  - HorizonMetrics correctness (hit rate, IC, expectancy) on synthetic data
  - Perfect-signal edge case (hit rate should be 100%)
  - All-neutral signal edge case (no active bars)
  - ConfusionMatrix on labelled regime data
  - AccuracyReport round-trip (compute + summary_df shape)
  - PnL proxy sign and magnitude sanity
  - BTC-sizing tests: sizing_mode=BASE with max_position_base=1.0
"""

import sys
import unittest
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tide.tide_accuracy import (
    AccuracyReport,
    ConfusionMatrix,
    HorizonMetrics,
    compute_accuracy_report,
    compute_confusion,
    compute_horizon_metrics,
    render_accuracy_markdown,
    render_accuracy_text,
)
from tide.tide_backtest import SizingMode, TideBacktester, TideStrategyParams


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bars(n: int, bias_seq, close_seq, regime="NORMAL") -> pd.DataFrame:
    """Build a minimal bars DataFrame for accuracy tests."""
    idx = pd.date_range("2024-01-01", periods=n, freq="1h")
    return pd.DataFrame({
        "close": np.asarray(close_seq, dtype=np.float64)[:n],
        "bias": bias_seq[:n],
        "vol_regime": [regime] * n,
        "risk_mult": np.ones(n),
    }, index=idx)


def _flat_close(n: int, start: float = 100.0, step: float = 1.0) -> np.ndarray:
    return np.arange(start, start + n * step, step)[:n]


# ─────────────────────────────────────────────────────────────────────────────
# compute_horizon_metrics
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeHorizonMetrics(unittest.TestCase):

    def test_perfect_long_signal(self):
        """LONG bias on every bar, price always rising → 100% hit rate."""
        n = 50
        close = _flat_close(n, start=100.0, step=1.0)
        bias = ["LONG"] * n
        bars = _bars(n, bias, close)
        hm = compute_horizon_metrics(bars, h=1)
        self.assertIsNotNone(hm)
        self.assertAlmostEqual(hm.hit_rate, 100.0, places=1)
        self.assertGreater(hm.expectancy, 0.0)

    def test_perfect_short_signal(self):
        """SHORT bias, price always falling → 100% hit rate."""
        n = 50
        close = _flat_close(n, start=200.0, step=-1.0)
        bias = ["SHORT"] * n
        bars = _bars(n, bias, close)
        hm = compute_horizon_metrics(bars, h=1)
        self.assertIsNotNone(hm)
        self.assertAlmostEqual(hm.hit_rate, 100.0, places=1)

    def test_all_neutral(self):
        """All-NEUTRAL: n_active == 0, metrics fall back to 0."""
        n = 30
        close = _flat_close(n)
        bias = ["NEUTRAL"] * n
        bars = _bars(n, bias, close)
        hm = compute_horizon_metrics(bars, h=1)
        self.assertIsNotNone(hm)
        self.assertEqual(hm.n_active, 0)
        self.assertEqual(hm.hit_rate, 0.0)
        self.assertEqual(hm.pnl_proxy, 0.0)

    def test_50_pct_signal(self):
        """Alternating LONG/SHORT with flat price → ~0% hit rate
        and near-zero IC (pure noise signal on a flat series)."""
        n = 60
        # Price random-walks but very slightly — small noise
        rng = np.random.default_rng(0)
        close = 100.0 + np.cumsum(rng.normal(0, 0.01, n))
        bias = ["LONG" if i % 2 == 0 else "SHORT" for i in range(n)]
        bars = _bars(n, bias, close)
        hm = compute_horizon_metrics(bars, h=1)
        self.assertIsNotNone(hm)
        # No strong claim on exact value; just check types
        self.assertIsInstance(hm.hit_rate, float)
        self.assertIsInstance(hm.ic, float)

    def test_insufficient_bars(self):
        """Fewer bars than horizon+2 → returns None."""
        bars = _bars(3, ["LONG", "SHORT", "LONG"], [100, 101, 100])
        hm = compute_horizon_metrics(bars, h=5)
        self.assertIsNone(hm)

    def test_pnl_proxy_positive_for_good_signal(self):
        """PnL proxy should be positive when LONG + rising prices."""
        n = 50
        close = _flat_close(n, start=10_000.0, step=100.0)  # BTC-like
        bias = ["LONG"] * n
        bars = _bars(n, bias, close)
        # sizing_base = 1 BTC
        hm = compute_horizon_metrics(bars, h=1, sizing_base=1.0)
        self.assertGreater(hm.pnl_proxy, 0.0)

    def test_pnl_proxy_negative_for_bad_signal(self):
        """PnL proxy should be negative when LONG + falling prices."""
        n = 50
        close = _flat_close(n, start=10_000.0, step=-100.0)
        bias = ["LONG"] * n
        bars = _bars(n, bias, close)
        hm = compute_horizon_metrics(bars, h=1, sizing_base=1.0)
        self.assertLess(hm.pnl_proxy, 0.0)

    def test_h_gt_1(self):
        """Multi-bar forward window: h=4 should work correctly."""
        n = 100
        close = _flat_close(n, start=100.0, step=1.0)
        bias = ["LONG"] * n
        bars = _bars(n, bias, close)
        hm4 = compute_horizon_metrics(bars, h=4)
        self.assertIsNotNone(hm4)
        # At h=4 and linearly rising prices, LONG is still correct
        self.assertAlmostEqual(hm4.hit_rate, 100.0, places=1)


# ─────────────────────────────────────────────────────────────────────────────
# compute_confusion
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeConfusion(unittest.TestCase):

    def test_basic_confusion(self):
        """With two regimes and two biases, pivot should have both dimensions."""
        n = 40
        close = _flat_close(n, start=100.0, step=1.0)
        bias = ["LONG"] * 20 + ["SHORT"] * 20
        regime = ["LOW"] * 20 + ["HIGH"] * 20
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        bars = pd.DataFrame(
            {"close": close, "bias": bias, "vol_regime": regime},
            index=idx,
        )
        cm = compute_confusion(bars)
        self.assertIsNotNone(cm)
        self.assertFalse(cm.pivot_mean.empty)
        self.assertFalse(cm.pivot_hit.empty)

    def test_no_regime_col(self):
        """Missing vol_regime column → returns None gracefully."""
        n = 20
        close = _flat_close(n)
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        bars = pd.DataFrame({"close": close, "bias": ["LONG"] * n}, index=idx)
        cm = compute_confusion(bars)
        self.assertIsNone(cm)


# ─────────────────────────────────────────────────────────────────────────────
# compute_accuracy_report
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeAccuracyReport(unittest.TestCase):

    def _make_bars(self, n: int = 200) -> pd.DataFrame:
        rng = np.random.default_rng(1)
        close = 30_000.0 + np.cumsum(rng.normal(0, 100, n))
        bias = np.where(rng.random(n) > 0.5, "LONG", "SHORT")
        regime = np.where(close > close.mean(), "HIGH", "LOW")
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        return pd.DataFrame(
            {"close": close, "bias": bias, "vol_regime": regime,
             "risk_mult": np.ones(n)},
            index=idx,
        )

    def test_report_shape(self):
        bars = self._make_bars()
        rep = compute_accuracy_report(bars, horizons=[1, 2, 4])
        self.assertEqual(len(rep.horizons), 3)
        self.assertEqual(rep.summary_df.shape[0], 3)
        self.assertIn("hit_rate%", rep.summary_df.columns)
        self.assertIn("ic", rep.summary_df.columns)

    def test_layer_label(self):
        bars = self._make_bars()
        rep = compute_accuracy_report(bars, layer="wave")
        self.assertEqual(rep.layer, "wave")

    def test_render_text(self):
        bars = self._make_bars()
        rep = compute_accuracy_report(bars)
        txt = render_accuracy_text(rep)
        self.assertIn(rep.layer.upper(), txt.upper())
        self.assertIn("horizon", txt.lower())

    def test_render_markdown(self):
        bars = self._make_bars()
        rep = compute_accuracy_report(bars)
        md = render_accuracy_markdown(rep)
        self.assertIn("##", md)
        self.assertIn("Hit %", md)

    def test_sizing_base_scales_pnl(self):
        """PnL proxy should scale linearly with sizing_base."""
        bars = self._make_bars()
        rep1 = compute_accuracy_report(bars, horizons=[1], sizing_base=1.0)
        rep10 = compute_accuracy_report(bars, horizons=[1], sizing_base=10.0)
        if rep1.horizons and rep10.horizons:
            pnl1 = rep1.horizons[0].pnl_proxy
            pnl10 = rep10.horizons[0].pnl_proxy
            self.assertAlmostEqual(pnl10, pnl1 * 10.0, places=4)


# ─────────────────────────────────────────────────────────────────────────────
# BTC-sizing tests (integration-level, purely synthetic)
# ─────────────────────────────────────────────────────────────────────────────

class TestBTCSizing(unittest.TestCase):
    """Test that SizingMode.BASE with max_position_base=1.0 runs end-to-end
    and produces sensible target_units (≤ 1.0 in absolute value)."""

    def _make_ohlcv(self, n: int = 500) -> pd.DataFrame:
        rng = np.random.default_rng(42)
        close = 30_000.0 + np.cumsum(rng.normal(0, 200, n))
        close = np.maximum(close, 1.0)
        vol = np.abs(rng.normal(1000, 200, n))
        idx = pd.date_range("2024-01-01", periods=n, freq="1h")
        return pd.DataFrame({
            "open": close - rng.uniform(0, 100, n),
            "high": close + rng.uniform(0, 200, n),
            "low": close - rng.uniform(0, 200, n),
            "close": close,
            "volume": vol,
        }, index=idx)

    def test_base_sizing_target_units_bounded(self):
        ohlcv = self._make_ohlcv()
        params = TideStrategyParams(
            sizing_mode=SizingMode.BASE,
            max_position_base=1.0,
            bar_seconds=3600.0,
            timeframe="1h",
        )
        result = TideBacktester(params).run(ohlcv)
        abs_pos = result.bars["target_units"].abs()
        # All target positions should be in [0, 1.0] (risk_mult ≤ 1)
        self.assertTrue((abs_pos <= 1.001).all(),
                        f"Some target_units > 1: {abs_pos.max()}")

    def test_base_sizing_pnl_in_btc_terms(self):
        """PnL should be expressed in BTC-price-move × units terms."""
        ohlcv = self._make_ohlcv(n=200)
        params = TideStrategyParams(
            sizing_mode=SizingMode.BASE,
            max_position_base=1.0,
            bar_seconds=3600.0,
            timeframe="1h",
        )
        result = TideBacktester(params).run(ohlcv)
        # gross_pnl[i] = position[i] * (close[i+1] - close[i])
        # With 1 BTC max, |gross_pnl| should be ≤ price move
        close = ohlcv["close"].to_numpy()
        bars = result.bars
        max_price_move = float(np.abs(np.diff(close)).max())
        max_gross = float(bars["gross_pnl"].abs().max())
        self.assertLessEqual(max_gross, max_price_move * 1.001 + 1e-6)

    def test_usd_vs_base_different_equity_paths(self):
        """USD and BASE modes should produce different equity paths
        (since USD sizing recomputes units each bar via price)."""
        ohlcv = self._make_ohlcv(n=300)
        p_usd = TideStrategyParams(
            sizing_mode=SizingMode.USD,
            max_position_usd=30_000.0,
            bar_seconds=3600.0, timeframe="1h",
        )
        p_base = TideStrategyParams(
            sizing_mode=SizingMode.BASE,
            max_position_base=1.0,
            bar_seconds=3600.0, timeframe="1h",
        )
        r_usd = TideBacktester(p_usd).run(ohlcv)
        r_base = TideBacktester(p_base).run(ohlcv)
        # Different final equity is enough to confirm modes are distinct
        eq_usd = r_usd.bars["equity"].to_numpy()
        eq_base = r_base.bars["equity"].to_numpy()
        # Allow them to be identical only if both have zero activity
        if r_usd.num_trades > 0 or r_base.num_trades > 0:
            self.assertFalse(
                np.allclose(eq_usd, eq_base, rtol=1e-6),
                "USD and BASE equity paths are unexpectedly identical",
            )


if __name__ == "__main__":
    unittest.main()
