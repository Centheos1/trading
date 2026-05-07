"""
Tests for the Tide-overlay backtest pipeline (features + backtester).

Covers:
  - Feature math invariants (η ∈ [0, 1], realized vol non-negative,
    regime classification thresholds, bias derivation)
  - Backtester input validation
  - Determinism: identical inputs → identical outputs
  - Boundary cases: flat market, all-NaN warmup, no rebalances
  - Sanity: long-only synthetic uptrend produces positive PnL
"""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import TideBias, VolRegime
from tide.tide_backtest import (
    TideBacktester,
    TideStrategyParams,
)
from tide.tide_features import (
    TideFeatureParams,
    build_tide_feature_frame,
    classify_vol_regime,
    derive_bias,
    directional_efficiency,
    liquidity_stress_proxy,
    realized_volatility,
    rolling_directional_efficiency,
    rolling_realized_volatility,
)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _make_uptrend_df(n: int = 500, start_price: float = 100.0,
                     bar_seconds: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq=f"{bar_seconds}s",
                        tz="UTC").tz_localize(None)
    closes = start_price * (1.0 + 0.0005) ** np.arange(n)
    df = pd.DataFrame({
        "open": closes,
        "high": closes * 1.001,
        "low": closes * 0.999,
        "close": closes,
        "volume": np.full(n, 100.0),
    }, index=idx)
    return df


def _make_flat_df(n: int = 200, price: float = 100.0,
                  bar_seconds: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq=f"{bar_seconds}s",
                        tz="UTC").tz_localize(None)
    df = pd.DataFrame({
        "open": np.full(n, price),
        "high": np.full(n, price),
        "low": np.full(n, price),
        "close": np.full(n, price),
        "volume": np.full(n, 100.0),
    }, index=idx)
    return df


# ────────────────────────────────────────────────────────────────────────
# Feature math
# ────────────────────────────────────────────────────────────────────────

class TestRealizedVol(unittest.TestCase):

    def test_zero_returns_yield_zero_vol(self):
        v = realized_volatility([0.0, 0.0, 0.0, 0.0], bar_seconds=60.0)
        self.assertEqual(v, 0.0)

    def test_constant_returns_positive_vol(self):
        v = realized_volatility([0.001, 0.001, 0.001], bar_seconds=60.0)
        self.assertGreater(v, 0.0)

    def test_empty_input(self):
        self.assertEqual(realized_volatility([], bar_seconds=60.0), 0.0)

    def test_rolling_aligns_to_index(self):
        df = _make_uptrend_df(n=100)
        rv = rolling_realized_volatility(df["close"], window=20, bar_seconds=60.0)
        self.assertEqual(len(rv), 100)
        # First 20 bars should be NaN
        self.assertTrue(rv.iloc[:19].isna().all())
        self.assertFalse(np.isnan(rv.iloc[-1]))


class TestDirectionalEfficiency(unittest.TestCase):

    def test_strict_uptrend_returns_one(self):
        prices = np.linspace(100, 200, 50)
        self.assertAlmostEqual(directional_efficiency(prices), 1.0, places=6)

    def test_perfect_oscillation_returns_zero(self):
        prices = [100.0, 101.0, 100.0, 101.0, 100.0]
        self.assertAlmostEqual(directional_efficiency(prices), 0.0, places=6)

    def test_eta_in_unit_interval(self):
        rng = np.random.default_rng(0)
        for _ in range(20):
            prices = 100 + np.cumsum(rng.normal(size=200))
            eta = directional_efficiency(prices)
            self.assertGreaterEqual(eta, 0.0)
            self.assertLessEqual(eta, 1.0)

    def test_rolling_eta_aligns(self):
        df = _make_uptrend_df(n=200)
        eta = rolling_directional_efficiency(df["close"], window=30)
        self.assertEqual(len(eta), 200)
        # Strict uptrend → η ≈ 1 once warm
        self.assertAlmostEqual(eta.iloc[-1], 1.0, places=6)


class TestVolRegimeClassification(unittest.TestCase):

    def test_thresholds(self):
        thr = (0.4, 0.8, 1.5)
        self.assertEqual(classify_vol_regime(0.1, thr), VolRegime.LOW)
        self.assertEqual(classify_vol_regime(0.4, thr), VolRegime.NORMAL)
        self.assertEqual(classify_vol_regime(0.8, thr), VolRegime.HIGH)
        self.assertEqual(classify_vol_regime(1.5, thr), VolRegime.CRISIS)
        self.assertEqual(classify_vol_regime(10.0, thr), VolRegime.CRISIS)

    def test_negative_or_nan_safe(self):
        self.assertEqual(classify_vol_regime(float("nan")), VolRegime.NORMAL)
        self.assertEqual(classify_vol_regime(-1.0), VolRegime.NORMAL)

    def test_threshold_order_enforced(self):
        with self.assertRaises(ValueError):
            classify_vol_regime(0.5, (1.0, 0.5, 2.0))


class TestBiasDerivation(unittest.TestCase):

    def test_long_when_strong_up(self):
        self.assertEqual(derive_bias(eta=0.9, net_move=10.0), TideBias.LONG)

    def test_short_when_strong_down(self):
        self.assertEqual(derive_bias(eta=0.9, net_move=-10.0), TideBias.SHORT)

    def test_neutral_when_eta_low(self):
        self.assertEqual(derive_bias(eta=0.1, net_move=10.0,
                                     eta_threshold=0.3), TideBias.NEUTRAL)

    def test_neutral_inside_deadband(self):
        self.assertEqual(
            derive_bias(eta=0.9, net_move=0.5, deadband=1.0),
            TideBias.NEUTRAL,
        )


class TestLiquidityStressProxy(unittest.TestCase):

    def test_returns_series_of_correct_length(self):
        df = _make_uptrend_df(n=300)
        lsi = liquidity_stress_proxy(df, window=60, bar_seconds=60.0)
        self.assertEqual(len(lsi), 300)

    def test_missing_columns_raise(self):
        df = pd.DataFrame({"close": [1, 2, 3]})
        with self.assertRaises(ValueError):
            liquidity_stress_proxy(df, window=2, bar_seconds=60.0)


class TestBuildFeatureFrame(unittest.TestCase):

    def test_columns_present(self):
        df = _make_uptrend_df(n=300)
        feats = build_tide_feature_frame(df)
        for col in [
            "open", "high", "low", "close", "volume",
            "log_return", "realized_vol", "eta", "net_move",
            "vol_regime", "bias", "lsi",
        ]:
            self.assertIn(col, feats.columns)

    def test_uptrend_eventually_long_bias(self):
        df = _make_uptrend_df(n=400)
        feats = build_tide_feature_frame(df, TideFeatureParams(
            vol_window=30, eta_window=30, lsi_window=60,
            bar_seconds=60.0, eta_threshold=0.2,
        ))
        self.assertEqual(feats["bias"].iloc[-1], TideBias.LONG)


# ────────────────────────────────────────────────────────────────────────
# Backtester
# ────────────────────────────────────────────────────────────────────────

class TestBacktesterValidation(unittest.TestCase):

    def test_missing_columns(self):
        df = pd.DataFrame(
            {"close": [1.0, 2.0]},
            index=pd.date_range("2024-01-01", periods=2, freq="1min"),
        )
        with self.assertRaises(ValueError):
            TideBacktester(TideStrategyParams()).run(df)

    def test_empty_df(self):
        idx = pd.DatetimeIndex([])
        df = pd.DataFrame({c: pd.Series(dtype=float) for c in
                           ["open", "high", "low", "close", "volume"]},
                          index=idx)
        with self.assertRaises(ValueError):
            TideBacktester(TideStrategyParams()).run(df)


class TestBacktesterDeterminism(unittest.TestCase):

    def test_replay_determinism(self):
        df = _make_uptrend_df(n=400)
        p = TideStrategyParams(
            vol_window=30, eta_window=30, lsi_window=60,
            bar_seconds=60.0, eta_threshold=0.2, update_interval_ms=60_000,
        )
        r1 = TideBacktester(p).run(df)
        r2 = TideBacktester(p).run(df)
        self.assertAlmostEqual(r1.final_equity, r2.final_equity, places=8)
        pd.testing.assert_series_equal(r1.equity_curve, r2.equity_curve)


class TestBacktesterFlatMarket(unittest.TestCase):

    def test_flat_market_no_pnl(self):
        df = _make_flat_df(n=300)
        p = TideStrategyParams(
            vol_window=30, eta_window=30, lsi_window=60,
            bar_seconds=60.0, eta_threshold=0.1,
        )
        result = TideBacktester(p).run(df)
        # Flat closes → zero log returns → η=0 → NEUTRAL bias → no position →
        # no fees, equity == initial.
        self.assertAlmostEqual(result.final_equity, p.initial_capital, places=6)
        self.assertEqual(result.num_trades, 0)


class TestBacktesterUptrendSanity(unittest.TestCase):

    def test_uptrend_long_overlay_makes_money(self):
        df = _make_uptrend_df(n=600)
        p = TideStrategyParams(
            vol_window=30, eta_window=30, lsi_window=60,
            bar_seconds=60.0, eta_threshold=0.1,
            risk_mult_by_regime=(1.0, 1.0, 1.0, 1.0),  # always full size
            taker_fee_bps=0.0, maker_fee_bps=0.0,
        )
        result = TideBacktester(p).run(df)
        self.assertGreater(result.final_equity, p.initial_capital)
        self.assertGreater(result.num_trades, 0)


class TestBacktesterRiskMultiplierBound(unittest.TestCase):

    def test_risk_mult_in_unit_interval(self):
        df = _make_uptrend_df(n=300)
        p = TideStrategyParams(vol_window=30, eta_window=30, lsi_window=60,
                               bar_seconds=60.0, eta_threshold=0.2)
        result = TideBacktester(p).run(df)
        rm = result.bars["risk_mult"].dropna().to_numpy()
        self.assertTrue((rm >= 0.0).all())
        self.assertTrue((rm <= 1.0).all())


if __name__ == "__main__":
    unittest.main()
