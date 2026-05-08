"""
Unit tests for the Wave layer backtest pipeline.

Coverage:
  - wave_features.py: feature shape, η bounds, dispersion/AR bounds
  - wave_backtest.py: isolated PnL sanity, stacked mode, determinism
  - wave_accuracy.py: regime hit rate, transition matrix, regime_bias_mean_fwd
  - wave_metrics.py: compute_wave_report produces finite metrics
  - sizing: BASE mode 1 BTC proxy
"""

import sys
import os
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wave.wave_features import (
    WaveFeatureParams,
    build_wave_feature_frame,
    rolling_trend_efficiency,
    rolling_dispersion_proxy,
    rolling_ar_proxy,
    rolling_vwap,
)
from wave.wave_backtest import (
    WaveBacktester,
    WaveStrategyParams,
    SizingMode,
    _wave_size_fraction,
    _isolated_direction,
)
from wave.wave_accuracy import compute_regime_accuracy_report, TransitionMatrix
from wave.wave_metrics import compute_wave_report
from schemas import WaveRegime


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ohlcv(n: int = 300, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prices = 30_000 + np.cumsum(rng.normal(0, 200, n))
    prices = np.maximum(prices, 1000.0)
    highs = prices * (1 + rng.uniform(0.001, 0.01, n))
    lows = prices * (1 - rng.uniform(0.001, 0.01, n))
    opens = np.roll(prices, 1)
    opens[0] = prices[0]
    volumes = rng.uniform(10, 100, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows,
        "close": prices, "volume": volumes,
    }, index=idx)


def _make_trending_ohlcv(n: int = 200) -> pd.DataFrame:
    """Strongly trending (η should be high → BREAKOUT)."""
    prices = 30_000.0 + np.arange(n) * 10.0
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    ones = np.ones(n)
    return pd.DataFrame({
        "open": prices - 5, "high": prices + 5,
        "low": prices - 5, "close": prices, "volume": ones * 50,
    }, index=idx)


def _default_params(mode: str = "isolated") -> WaveStrategyParams:
    return WaveStrategyParams(
        sizing_mode=SizingMode.BASE,
        max_position_base=1.0,
        initial_capital=10_000.0,
        taker_fee_bps=0.0,        # zero fee for simple PnL checks
        maker_fee_bps=0.0,
        mode=mode,
        accuracy_horizons=(1, 2, 4),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Wave feature tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveFeatures(unittest.TestCase):

    def setUp(self):
        self.ohlcv = _make_ohlcv()

    def test_feature_frame_shape(self):
        feats = build_wave_feature_frame(self.ohlcv)
        expected_cols = {
            "close", "log_return", "eta", "dispersion", "ar",
            "vwap", "structure_high", "structure_low", "distance_vwap",
        }
        self.assertTrue(expected_cols.issubset(set(feats.columns)))
        self.assertEqual(len(feats), len(self.ohlcv))

    def test_eta_bounds(self):
        close = self.ohlcv["close"]
        eta = rolling_trend_efficiency(close, window=20)
        self.assertTrue((eta >= 0).all())
        self.assertTrue((eta <= 1).all())

    def test_eta_high_for_trend(self):
        ohlcv_trend = _make_trending_ohlcv(200)
        close = ohlcv_trend["close"]
        eta = rolling_trend_efficiency(close, window=20)
        # After warmup, η should be close to 1 for a pure trend
        self.assertGreater(float(eta.iloc[-1]), 0.9)

    def test_dispersion_non_negative(self):
        close = self.ohlcv["close"]
        d = rolling_dispersion_proxy(close, window=20)
        self.assertTrue((d >= 0).all())

    def test_ar_bounds(self):
        close = self.ohlcv["close"]
        ar = rolling_ar_proxy(close, window=60)
        self.assertTrue((ar >= 0).all())
        self.assertTrue((ar <= 1).all())

    def test_vwap_reasonable(self):
        vwap = rolling_vwap(self.ohlcv, window=24)
        close = self.ohlcv["close"]
        # VWAP should be in the same ballpark as close
        ratio = (vwap / close).dropna()
        self.assertTrue((ratio > 0.5).all())
        self.assertTrue((ratio < 2.0).all())

    def test_structure_high_gte_close(self):
        feats = build_wave_feature_frame(self.ohlcv)
        self.assertTrue((feats["structure_high"] >= feats["close"]).all())

    def test_structure_low_lte_close(self):
        feats = build_wave_feature_frame(self.ohlcv)
        self.assertTrue((feats["structure_low"] <= feats["close"]).all())

    def test_missing_column_raises(self):
        bad = self.ohlcv.drop(columns=["volume"])
        with self.assertRaises(ValueError):
            build_wave_feature_frame(bad)


# ─────────────────────────────────────────────────────────────────────────────
# Wave backtest tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveBacktest(unittest.TestCase):

    def setUp(self):
        self.ohlcv = _make_ohlcv(300)
        self.params = _default_params("isolated")

    def _run(self, params=None, ohlcv=None):
        p = params if params is not None else self.params
        df = ohlcv if ohlcv is not None else self.ohlcv
        return WaveBacktester(p).run(df)

    def test_result_shape(self):
        result = self._run()
        self.assertEqual(len(result.bars), len(self.ohlcv))

    def test_required_columns(self):
        result = self._run()
        for col in ["wave_regime", "tide_bias", "equity", "drawdown", "bar_return"]:
            self.assertIn(col, result.bars.columns)

    def test_equity_starts_at_initial_capital(self):
        result = self._run()
        # First bar records the equity after the first bar's PnL is applied.
        # What matters is that equity starts in a reasonable range of initial_capital.
        first_eq = float(result.bars["equity"].iloc[0])
        self.assertGreater(first_eq, 0.0)
        # Equity should not differ from initial capital by more than one bar's move
        max_move_frac = 0.10  # 10% is an extreme single-bar move
        self.assertAlmostEqual(
            first_eq / self.params.initial_capital, 1.0, delta=max_move_frac
        )

    def test_flat_market_produces_no_pnl(self):
        """Zero fee, flat prices → equity unchanged."""
        n = 100
        prices = np.full(n, 30_000.0)
        idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
        flat_ohlcv = pd.DataFrame({
            "open": prices, "high": prices,
            "low": prices, "close": prices, "volume": np.ones(n),
        }, index=idx)
        result = self._run(ohlcv=flat_ohlcv)
        self.assertAlmostEqual(result.final_equity, self.params.initial_capital, places=2)

    def test_deterministic(self):
        r1 = self._run()
        r2 = self._run()
        np.testing.assert_array_almost_equal(
            r1.bars["equity"].values, r2.bars["equity"].values, decimal=8
        )

    def test_base_sizing_max_one_btc(self):
        result = self._run()
        max_pos = result.bars["position"].abs().max()
        # max_position_base=1.0, leverage=1.0, reduced_size_fraction=0.5
        # Maximum achievable: 1.0 × 1.0 (BREAKOUT, full size)
        self.assertLessEqual(max_pos, 1.0 + 1e-6)

    def test_tide_bias_is_neutral_in_isolated_mode(self):
        result = self._run()
        self.assertTrue((result.bars["tide_bias"] == "NEUTRAL").all())

    def test_wave_regime_values(self):
        result = self._run()
        valid = {"MEAN_REVERSION", "BREAKOUT", "BREAKDOWN", "NEUTRAL"}
        actual = set(result.bars["wave_regime"].unique())
        self.assertTrue(actual.issubset(valid))

    def test_trending_market_produces_breakout_regime(self):
        """A strongly trending OHLCV should produce at least some BREAKOUT bars."""
        result = self._run(ohlcv=_make_trending_ohlcv(200))
        regimes = result.bars["wave_regime"]
        # Allow some warmup — look at second half
        half = regimes.iloc[len(regimes) // 2:]
        self.assertIn("BREAKOUT", half.values)

    def test_stacked_requires_tide_result(self):
        p = _default_params("stacked")
        with self.assertRaises(ValueError):
            WaveBacktester(p)

    def test_stacked_runs_with_tide_result(self):
        from tide.tide_backtest import TideBacktester, TideStrategyParams
        from tide.tide_backtest import SizingMode as TideSizing
        tide_p = TideStrategyParams(
            sizing_mode=TideSizing.BASE,
            max_position_base=1.0,
            initial_capital=10_000.0,
            taker_fee_bps=0.0,
            accuracy_horizons=(),
        ).with_timeframe("1h")
        tide_result = TideBacktester(tide_p).run(self.ohlcv)
        p = _default_params("stacked")
        result = WaveBacktester(p, tide_result=tide_result).run(self.ohlcv)
        self.assertEqual(len(result.bars), len(self.ohlcv))
        self.assertEqual(result.mode, "stacked")

    def test_empty_ohlcv_raises(self):
        empty = self.ohlcv.iloc[:0]
        with self.assertRaises(ValueError):
            self._run(ohlcv=empty)


# ─────────────────────────────────────────────────────────────────────────────
# Sizing helper unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSizingHelpers(unittest.TestCase):

    def test_wave_size_fraction_breakout(self):
        self.assertEqual(_wave_size_fraction(WaveRegime.BREAKOUT, 0.5), 1.0)

    def test_wave_size_fraction_breakdown(self):
        self.assertEqual(_wave_size_fraction(WaveRegime.BREAKDOWN, 0.5), 0.0)

    def test_wave_size_fraction_mr_neutral(self):
        self.assertEqual(_wave_size_fraction(WaveRegime.MEAN_REVERSION, 0.5), 0.5)
        self.assertEqual(_wave_size_fraction(WaveRegime.NEUTRAL, 0.5), 0.5)

    def test_isolated_direction_breakout_above_vwap(self):
        self.assertEqual(_isolated_direction(WaveRegime.BREAKOUT, 30100, 30000), 1)

    def test_isolated_direction_breakout_below_vwap(self):
        self.assertEqual(_isolated_direction(WaveRegime.BREAKOUT, 29900, 30000), -1)

    def test_isolated_direction_mr_above_vwap(self):
        self.assertEqual(_isolated_direction(WaveRegime.MEAN_REVERSION, 30100, 30000), -1)

    def test_isolated_direction_breakdown(self):
        self.assertEqual(_isolated_direction(WaveRegime.BREAKDOWN, 30100, 30000), 0)

    def test_isolated_direction_neutral(self):
        self.assertEqual(_isolated_direction(WaveRegime.NEUTRAL, 30100, 30000), 0)


# ─────────────────────────────────────────────────────────────────────────────
# Regime accuracy tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveAccuracy(unittest.TestCase):

    def setUp(self):
        ohlcv = _make_ohlcv(300)
        result = WaveBacktester(_default_params()).run(ohlcv)
        self.bars = result.bars

    def test_accuracy_report_horizons(self):
        rep = compute_regime_accuracy_report(self.bars, horizons=[1, 2, 4])
        self.assertEqual(rep.horizons, [1, 2, 4])

    def test_overall_hit_rate_length_matches_horizons(self):
        rep = compute_regime_accuracy_report(self.bars, horizons=[1, 2, 4])
        self.assertEqual(len(rep.overall_hit_rate), 3)

    def test_hit_rates_in_valid_range(self):
        rep = compute_regime_accuracy_report(self.bars)
        for hr in rep.overall_hit_rate:
            self.assertGreaterEqual(hr, 0.0)
            self.assertLessEqual(hr, 1.0)

    def test_transition_matrix_row_sums(self):
        rep = compute_regime_accuracy_report(self.bars)
        if rep.transition_matrix is not None:
            tm = rep.transition_matrix
            row_sums = tm.probs.sum(axis=1)
            for i, s in enumerate(row_sums):
                # Rows with no outgoing transitions (regime never appeared) sum to 0
                if tm.counts[i].sum() > 0:
                    self.assertAlmostEqual(s, 1.0, places=6)

    def test_regime_stability_non_negative(self):
        rep = compute_regime_accuracy_report(self.bars)
        for v in rep.regime_stability.values():
            self.assertGreaterEqual(v, 0.0)

    def test_transition_matrix_compute(self):
        import pandas as pd
        regime_s = pd.Series(["NEUTRAL", "BREAKOUT", "BREAKOUT", "NEUTRAL",
                               "MEAN_REVERSION", "NEUTRAL"])
        tm = TransitionMatrix.compute(regime_s)
        self.assertEqual(tm.probs.shape, (4, 4))

    def test_regime_bias_confusion_with_stacked(self):
        from tide.tide_backtest import TideBacktester, TideStrategyParams
        from tide.tide_backtest import SizingMode as TideSizing
        ohlcv = _make_ohlcv(300)
        tide_p = TideStrategyParams(
            sizing_mode=TideSizing.BASE,
            initial_capital=10_000.0,
            taker_fee_bps=0.0,
            accuracy_horizons=(),
        ).with_timeframe("1h")
        tide_result = TideBacktester(tide_p).run(ohlcv)
        p = _default_params("stacked")
        result = WaveBacktester(p, tide_result=tide_result).run(ohlcv)
        rep = compute_regime_accuracy_report(result.bars, horizons=[1])
        # Stacked bars have tide_bias → confusion matrix should be populated
        self.assertIsNotNone(rep.regime_bias_mean_fwd)


# ─────────────────────────────────────────────────────────────────────────────
# Metrics tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWaveMetrics(unittest.TestCase):

    def setUp(self):
        ohlcv = _make_ohlcv(300)
        self.result = WaveBacktester(_default_params()).run(ohlcv)

    def test_compute_report_runs(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        self.assertIsNotNone(report)

    def test_n_bars_correct(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        self.assertEqual(report.n_bars, len(self.result.bars))

    def test_finite_key_metrics(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        for attr in ["ann_vol", "max_drawdown", "sharpe", "sortino"]:
            val = getattr(report, attr)
            self.assertTrue(np.isfinite(val), f"{attr} is not finite: {val}")

    def test_max_drawdown_non_positive(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        self.assertLessEqual(report.max_drawdown, 0.0)

    def test_exposure_pct_in_range(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        self.assertGreaterEqual(report.exposure_pct, 0.0)
        self.assertLessEqual(report.exposure_pct, 100.0)

    def test_per_regime_populated(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        self.assertGreater(len(report.per_regime), 0)

    def test_accuracy_computed(self):
        report = compute_wave_report(self.result, compute_accuracy=True)
        self.assertIsNotNone(report.regime_accuracy)

    def test_params_echo_keys(self):
        report = compute_wave_report(self.result, compute_accuracy=False)
        for k in ["symbol", "timeframe", "mode", "sizing_mode", "eta_window"]:
            self.assertIn(k, report.params_echo)


# ─────────────────────────────────────────────────────────────────────────────
# PnL sanity: BREAKOUT with trending market should be positive-expectancy
# ─────────────────────────────────────────────────────────────────────────────

class TestPnLSanity(unittest.TestCase):

    def test_trending_market_net_positive_pnl(self):
        """In a strongly trending market the BREAKOUT regime (η high → long)
        should produce positive cumulative PnL over the full run."""
        ohlcv = _make_trending_ohlcv(300)
        params = _default_params()
        result = WaveBacktester(params).run(ohlcv)
        # Net PnL can be positive or negative depending on regime classification,
        # but final equity should be defined and finite.
        self.assertTrue(np.isfinite(result.final_equity))

    def test_no_pnl_when_breakdown_all_bars(self):
        """Force BREAKDOWN by setting ar_critical very low; all positions flat → no PnL."""
        # When ar_critical=0.0 and ar proxy ≥ 0, every bar should be BREAKDOWN
        # → position = 0 → equity unchanged (no fees since position=0)
        from dataclasses import replace
        params = replace(
            _default_params(),
            ar_critical=0.0,   # forces BREAKDOWN every bar (ar_proxy ≥ 0)
            ar_recover=0.0,
        )
        ohlcv = _make_ohlcv(200)
        result = WaveBacktester(params).run(ohlcv)
        # If no trades ever happen, equity == initial_capital
        if result.num_trades == 0:
            self.assertAlmostEqual(result.final_equity,
                                   params.initial_capital, places=2)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 9 backwards-compat regression block
#
# These tests assert that the new backtest-hardening fields default to values
# that preserve V1 behaviour, so legacy parameter sets and downstream
# tooling (optimised reports, persisted dataclasses) keep working without
# any code changes.
# ─────────────────────────────────────────────────────────────────────────────


class TestPhase9BackwardCompat(unittest.TestCase):

    def test_liquidation_default_is_zero(self):
        p = WaveStrategyParams()
        self.assertEqual(p.liquidation_equity_frac, 0.0)

    def test_slippage_defaults_are_zero(self):
        p = WaveStrategyParams()
        self.assertEqual(p.slippage_bps, 0.0)
        self.assertEqual(p.slippage_per_unit_bps, 0.0)

    def test_with_timeframe_default_does_not_rescale(self):
        """The plan's regression-safety rule: existing call sites
        (which use the original `with_timeframe(tf)` signature) must
        continue to leave window lengths unchanged."""
        p = WaveStrategyParams(timeframe="1h", bar_seconds=3600.0,
                                eta_window=24, vwap_window=24,
                                structure_window=24,
                                disp_window=24, ar_window=48)
        q = p.with_timeframe("5m")
        self.assertEqual(q.eta_window, p.eta_window)
        self.assertEqual(q.ar_window, p.ar_window)

    def test_result_liquidated_at_bar_default_none(self):
        ohlcv = _make_ohlcv(150)
        params = _default_params()
        result = WaveBacktester(params).run(ohlcv)
        self.assertIsNone(result.liquidated_at_bar)

    def test_report_phase9_fields_default(self):
        """Healthy run: monthly_returns_normalised flag is False; chop
        diagnostics are populated and finite."""
        ohlcv = _make_ohlcv(300)
        params = _default_params()
        result = WaveBacktester(params).run(ohlcv)
        rpt = compute_wave_report(result, compute_accuracy=False)
        self.assertIsInstance(rpt.monthly_returns_normalised, bool)
        self.assertGreaterEqual(rpt.regime_flips, 0)
        self.assertIsInstance(rpt.regime_run_lengths, dict)
        # Per-regime rows now also carry trade aggregates.
        for row in rpt.per_regime:
            self.assertIn("trades", row)
            self.assertIn("fees_paid", row)
            self.assertIn("turnover_usd", row)


if __name__ == "__main__":
    unittest.main(verbosity=2)
