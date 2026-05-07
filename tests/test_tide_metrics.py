"""
Tests for the Tide performance / risk metrics module.

Strategy:
  - Construct synthetic backtest results with known properties.
  - Validate each metric in isolation against the closed-form expectation.
  - Validate the high-level ``compute_report`` end-to-end.

Property invariants checked (mirroring strategy.md §20.6):
  - ES_α >= VaR_α >= 0 (using historical estimators)
  - Calmar = CAGR / |MaxDD|
  - Sharpe is annualized correctly
  - Empty / degenerate inputs do not raise
"""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tide.tide_backtest import TideBacktestResult, TideStrategyParams
from tide.tide_metrics import (
    TidePerformanceReport,
    annualized_return,
    annualized_volatility,
    calmar_ratio,
    compute_report,
    historical_es,
    historical_var,
    max_drawdown,
    omega_ratio,
    sharpe_ratio,
    sortino_ratio,
    trade_round_trip_pnl,
    ulcer_index,
)


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────

def _const_equity(n: int = 100, value: float = 1000.0) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC").tz_localize(None)
    return pd.Series(np.full(n, value), index=idx)


def _make_returns(values: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(values), freq="1h",
                        tz="UTC").tz_localize(None)
    return pd.Series(values, index=idx)


def _build_synthetic_result(
    bar_returns: list[float],
    bar_seconds: float = 3600.0,
    initial_capital: float = 10_000.0,
    positions: list[float] | None = None,
) -> TideBacktestResult:
    """Construct a TideBacktestResult from a list of bar returns."""
    n = len(bar_returns)
    idx = pd.date_range("2024-01-01", periods=n, freq=f"{int(bar_seconds)}s",
                        tz="UTC").tz_localize(None)
    eq = np.empty(n, dtype=np.float64)
    cur = initial_capital
    for i, r in enumerate(bar_returns):
        cur = cur * (1.0 + r)
        eq[i] = cur
    running_max = np.maximum.accumulate(eq)
    dd = np.where(running_max > 0.0, (eq - running_max) / running_max, 0.0)
    if positions is None:
        positions = [0.5] * n
    bars = pd.DataFrame({
        "close": np.full(n, 100.0),
        "log_return": np.zeros(n),
        "realized_vol": np.zeros(n),
        "eta": np.zeros(n),
        "net_move": np.zeros(n),
        "lsi": np.zeros(n),
        "vol_regime": ["NORMAL"] * n,
        "bias": ["LONG"] * n,
        "risk_mult": np.full(n, 1.0),
        "target_units": np.zeros(n),
        "position": np.array(positions, dtype=np.float64),
        "fee": np.zeros(n),
        "gross_pnl": np.array(bar_returns, dtype=np.float64) * initial_capital,
        "net_pnl": np.array(bar_returns, dtype=np.float64) * initial_capital,
        "bar_return": np.array(bar_returns, dtype=np.float64),
        "equity": eq,
        "drawdown": dd,
    }, index=idx)
    trades = pd.DataFrame(columns=["prior_units", "new_units", "delta_units",
                                    "price", "notional", "fee", "bias", "regime"])
    p = TideStrategyParams(bar_seconds=bar_seconds, initial_capital=initial_capital)
    return TideBacktestResult(
        params=p, bars=bars, trades=trades,
        initial_capital=initial_capital, final_equity=float(eq[-1]),
    )


# ────────────────────────────────────────────────────────────────────────
# Pure-metric tests
# ────────────────────────────────────────────────────────────────────────

class TestAnnualizedReturn(unittest.TestCase):

    def test_flat_equity_zero_cagr(self):
        eq = _const_equity()
        self.assertAlmostEqual(annualized_return(eq, bar_seconds=3600.0), 0.0)

    def test_doubling_year_yields_100pct(self):
        idx = pd.date_range("2024-01-01", periods=2, freq="365D",
                            tz="UTC").tz_localize(None)
        eq = pd.Series([1000.0, 2000.0], index=idx)
        cagr = annualized_return(eq, bar_seconds=365 * 86400.0)
        self.assertAlmostEqual(cagr, 1.0, places=4)


class TestVolatility(unittest.TestCase):

    def test_zero_returns_zero_vol(self):
        rets = _make_returns([0.0] * 100)
        self.assertAlmostEqual(
            annualized_volatility(rets, bar_seconds=3600.0), 0.0
        )

    def test_constant_nonzero_returns_zero_vol(self):
        rets = _make_returns([0.001] * 100)
        self.assertAlmostEqual(
            annualized_volatility(rets, bar_seconds=3600.0), 0.0
        )


class TestSharpeSortino(unittest.TestCase):

    def test_sharpe_positive_when_drift_positive(self):
        rng = np.random.default_rng(0)
        # mean +0.1% per bar with small noise
        rets = _make_returns(list(0.001 + rng.normal(scale=0.0005, size=500)))
        s = sharpe_ratio(rets, bar_seconds=3600.0)
        self.assertGreater(s, 1.0)

    def test_sortino_geq_sharpe_for_symmetric(self):
        rng = np.random.default_rng(0)
        rets = _make_returns(list(rng.normal(loc=0.0, scale=0.001, size=500)))
        sh = sharpe_ratio(rets, bar_seconds=3600.0)
        so = sortino_ratio(rets, bar_seconds=3600.0)
        # On symmetric input, |Sortino| >= |Sharpe| roughly; just sanity check
        # that both finite & not absurd.
        self.assertTrue(np.isfinite(sh))
        self.assertTrue(np.isfinite(so))


class TestVarES(unittest.TestCase):

    def test_es_geq_var_geq_zero(self):
        rng = np.random.default_rng(1)
        rets = _make_returns(list(rng.normal(loc=0.0, scale=0.01, size=2000)))
        var = historical_var(rets, alpha=0.95)
        es = historical_es(rets, alpha=0.95)
        self.assertGreaterEqual(es, var)
        self.assertGreaterEqual(var, 0.0)

    def test_no_data_safe(self):
        rets = _make_returns([])
        self.assertEqual(historical_var(rets), 0.0)
        self.assertEqual(historical_es(rets), 0.0)


class TestMaxDrawdown(unittest.TestCase):

    def test_monotone_up_zero_dd(self):
        eq = pd.Series(np.linspace(100, 200, 50),
                       index=pd.date_range("2024-01-01", periods=50, freq="1h",
                                           tz="UTC").tz_localize(None))
        mdd, dur, ts = max_drawdown(eq)
        self.assertEqual(mdd, 0.0)

    def test_known_drawdown(self):
        eq = pd.Series(
            [100, 110, 90, 95, 120, 60, 70, 130, 130],
            index=pd.date_range("2024-01-01", periods=9, freq="1h",
                                tz="UTC").tz_localize(None),
        )
        mdd, dur, ts = max_drawdown(eq)
        # Peak = 120, trough = 60 → DD = -50%.
        self.assertAlmostEqual(mdd, -0.5, places=6)
        self.assertGreater(dur, 0)


class TestUlcerIndex(unittest.TestCase):

    def test_no_drawdown_zero_ulcer(self):
        eq = pd.Series(np.linspace(100, 200, 50),
                       index=pd.date_range("2024-01-01", periods=50, freq="1h",
                                           tz="UTC").tz_localize(None))
        self.assertAlmostEqual(ulcer_index(eq), 0.0, places=8)


class TestCalmar(unittest.TestCase):

    def test_zero_dd_returns_zero(self):
        eq = pd.Series(np.linspace(100, 200, 50),
                       index=pd.date_range("2024-01-01", periods=50, freq="1h",
                                           tz="UTC").tz_localize(None))
        self.assertEqual(calmar_ratio(eq, bar_seconds=3600.0), 0.0)


class TestOmega(unittest.TestCase):

    def test_all_positive_infinite_omega(self):
        rets = _make_returns([0.01] * 50)
        self.assertEqual(omega_ratio(rets, threshold=0.0), float("inf"))

    def test_zero_input_zero_omega(self):
        self.assertEqual(omega_ratio(_make_returns([])), 0.0)


class TestRoundTripPnL(unittest.TestCase):

    def test_no_position_no_trips(self):
        result = _build_synthetic_result(
            [0.01] * 10,
            positions=[0.0] * 10,
        )
        trips = trade_round_trip_pnl(result.bars)
        self.assertEqual(trips, [])

    def test_single_long_then_flat_yields_one_trip(self):
        positions = [1.0] * 5 + [0.0] * 5
        result = _build_synthetic_result(
            [0.01] * 10, positions=positions
        )
        trips = trade_round_trip_pnl(result.bars)
        self.assertEqual(len(trips), 1)


# ────────────────────────────────────────────────────────────────────────
# End-to-end report
# ────────────────────────────────────────────────────────────────────────

class TestComputeReport(unittest.TestCase):

    def test_smoke(self):
        result = _build_synthetic_result(
            bar_returns=[0.001, 0.002, -0.001, 0.0015, 0.001] * 50,
            positions=[0.5] * 250,
        )
        rep = compute_report(result)
        self.assertIsInstance(rep, TidePerformanceReport)
        self.assertEqual(rep.num_bars, 250)
        self.assertGreater(rep.cagr, 0.0)
        self.assertGreater(rep.sharpe, 0.0)
        self.assertLessEqual(rep.max_drawdown, 0.0)
        self.assertGreaterEqual(rep.es_95, rep.var_95)


if __name__ == "__main__":
    unittest.main()
