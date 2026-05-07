"""
Wave-layer performance metrics and reporting.

Wraps the shared metric helpers from tide_metrics.py and adds
Wave-specific regime accuracy computed by wave_accuracy.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from tide.tide_metrics import (
    _periods_per_year,
    _safe_div,
    annualized_return,
    annualized_volatility,
    annualized_downside_volatility,
    max_drawdown,
    sharpe_ratio,
    sortino_ratio,
    calmar_ratio,
    historical_var,
    historical_es,
    ulcer_index,
    skew_kurt,
    trade_round_trip_pnl,
)
from wave.wave_backtest import WaveBacktestResult, SizingMode
from wave.wave_accuracy import (
    RegimeAccuracyReport,
    compute_regime_accuracy_report,
)


# ────────────────────────────────────────────────────────────────────────
# Report container
# ────────────────────────────────────────────────────────────────────────

@dataclass
class WavePerformanceReport:
    """Complete quantitative report for a Wave backtest run."""

    # Run metadata
    symbol: str = ""
    exchange: str = ""
    timeframe: str = ""
    mode: str = "isolated"        # "isolated" | "stacked"
    bar_seconds: float = 3600.0
    n_bars: int = 0
    start: Optional[pd.Timestamp] = None
    end: Optional[pd.Timestamp] = None

    # Returns / growth
    total_return: float = 0.0
    cagr: float = 0.0
    initial_capital: float = 0.0
    final_equity: float = 0.0

    # Risk
    ann_vol: float = 0.0
    ann_downside_vol: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    var_95: float = 0.0
    var_99: float = 0.0
    es_95: float = 0.0
    es_99: float = 0.0
    ulcer_index: float = 0.0
    skewness: float = 0.0
    kurtosis: float = 0.0

    # Risk-adjusted
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0

    # Trade / activity
    num_trades: int = 0
    turnover_per_year: float = 0.0
    exposure_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_bar: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0

    # Regime breakdown
    per_regime: List[Dict] = field(default_factory=list)

    # Accuracy
    regime_accuracy: Optional[RegimeAccuracyReport] = None

    # Monthly returns
    monthly_returns: Optional[pd.Series] = None
    best_month: float = 0.0
    worst_month: float = 0.0
    pct_positive_months: float = 0.0

    # Params echo
    params_echo: Dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# Main compute function
# ────────────────────────────────────────────────────────────────────────

def compute_wave_report(
    result: WaveBacktestResult,
    *,
    compute_accuracy: bool = True,
) -> WavePerformanceReport:
    """Compute a full WavePerformanceReport from a WaveBacktestResult."""
    p = result.params
    bars = result.bars
    returns = result.returns

    bar_sec = p.bar_seconds
    ppy = _periods_per_year(bar_sec)
    equity = result.equity_curve

    rpt = WavePerformanceReport()
    rpt.symbol = p.symbol
    rpt.exchange = p.exchange
    rpt.timeframe = p.timeframe
    rpt.mode = result.mode
    rpt.bar_seconds = bar_sec
    rpt.n_bars = len(bars)
    rpt.start = bars.index[0] if len(bars) > 0 else None
    rpt.end = bars.index[-1] if len(bars) > 0 else None
    rpt.initial_capital = result.initial_capital
    rpt.final_equity = result.final_equity

    ret_arr = returns.dropna().to_numpy(dtype=np.float64)

    # Returns / growth
    if result.initial_capital > 0:
        rpt.total_return = (result.final_equity - result.initial_capital) / result.initial_capital
    rpt.cagr = annualized_return(equity, bar_seconds=bar_sec)

    # Risk
    rpt.ann_vol = annualized_volatility(returns, bar_seconds=bar_sec)
    rpt.ann_downside_vol = annualized_downside_volatility(returns, bar_seconds=bar_sec)
    mdd, mdd_dur, _ = max_drawdown(equity)
    rpt.max_drawdown = mdd
    rpt.max_drawdown_duration = mdd_dur
    rpt.var_95 = historical_var(returns, alpha=0.95)
    rpt.var_99 = historical_var(returns, alpha=0.99)
    rpt.es_95 = historical_es(returns, alpha=0.95)
    rpt.es_99 = historical_es(returns, alpha=0.99)
    rpt.ulcer_index = ulcer_index(equity)
    rpt.skewness, rpt.kurtosis = skew_kurt(returns)

    # Risk-adjusted
    rpt.sharpe = sharpe_ratio(returns, bar_seconds=bar_sec)
    rpt.sortino = sortino_ratio(returns, bar_seconds=bar_sec)
    rpt.calmar = calmar_ratio(equity, bar_seconds=bar_sec)

    # Trade / activity
    rpt.num_trades = result.num_trades
    notional_turnover = (
        result.trades["notional"].sum() if not result.trades.empty else 0.0
    )
    n_years = len(bars) * bar_sec / (365.0 * 24.0 * 3600.0)
    rpt.turnover_per_year = _safe_div(notional_turnover, max(n_years, 1e-9))
    exposed = (bars["position"].abs() > 1e-9).sum()
    rpt.exposure_pct = _safe_div(exposed * 100.0, len(bars))

    trip_pnl = trade_round_trip_pnl(bars)
    if trip_pnl:
        wins = [x for x in trip_pnl if x > 0]
        losses = [x for x in trip_pnl if x <= 0]
        rpt.win_rate = _safe_div(len(wins), len(trip_pnl))
        total_win = sum(wins)
        total_loss = abs(sum(losses))
        rpt.profit_factor = _safe_div(total_win, total_loss) if total_loss > 0 else float("inf")
        rpt.avg_win = _safe_div(total_win, len(wins)) if wins else 0.0
        rpt.avg_loss = _safe_div(sum(losses), len(losses)) if losses else 0.0
    rpt.expectancy_per_bar = float(np.mean(ret_arr)) if ret_arr.size > 0 else 0.0

    # Per-regime breakdown
    if "wave_regime" in bars.columns:
        regime_col = "wave_regime"
        for reg in ["BREAKOUT", "MEAN_REVERSION", "BREAKDOWN", "NEUTRAL"]:
            mask = bars[regime_col] == reg
            if not mask.any():
                continue
            reg_returns = returns[mask].dropna()
            reg_eq_sub = bars.loc[mask, "net_pnl"].sum()
            time_pct = _safe_div(mask.sum() * 100.0, len(bars))
            reg_cagr = annualized_return(
                equity[mask].reset_index(drop=True), bar_seconds=bar_sec
            ) if mask.sum() > 1 else 0.0
            rpt.per_regime.append({
                "regime": reg,
                "time_pct": time_pct,
                "contribution_pnl": reg_eq_sub,
                "ann_return": reg_cagr,
                "sharpe": sharpe_ratio(reg_returns, bar_seconds=bar_sec),
                "n_bars": int(mask.sum()),
            })

    # Monthly returns
    if not equity.empty:
        monthly = (
            equity.resample("ME").last().pct_change().dropna()
            if isinstance(equity.index, pd.DatetimeIndex) else pd.Series(dtype=float)
        )
        if not monthly.empty:
            rpt.monthly_returns = monthly
            rpt.best_month = float(monthly.max())
            rpt.worst_month = float(monthly.min())
            rpt.pct_positive_months = _safe_div((monthly > 0).sum() * 100.0, len(monthly))

    # Regime accuracy
    if compute_accuracy and len(bars) > max(p.accuracy_horizons, default=0) + 1:
        rpt.regime_accuracy = compute_regime_accuracy_report(
            bars,
            horizons=list(p.accuracy_horizons),
            bar_seconds=p.bar_seconds,
        )

    # Params echo
    rpt.params_echo = {
        "symbol": p.symbol,
        "exchange": p.exchange,
        "timeframe": p.timeframe,
        "mode": result.mode,
        "sizing_mode": p.sizing_mode.value,
        "max_position_base": p.max_position_base,
        "max_position_usd": p.max_position_usd,
        "initial_capital": p.initial_capital,
        "leverage": p.leverage,
        "taker_fee_bps": p.taker_fee_bps,
        "eta_window": p.eta_window,
        "vwap_window": p.vwap_window,
        "disp_window": p.disp_window,
        "ar_window": p.ar_window,
        "disp_scale": p.disp_scale,
        "eta_mr_threshold": p.eta_mr_threshold,
        "eta_bo_threshold": p.eta_bo_threshold,
        "eta_neutral_threshold": p.eta_neutral_threshold,
        "dispersion_threshold": p.dispersion_threshold,
        "dispersion_critical": p.dispersion_critical,
        "ar_critical": p.ar_critical,
        "ar_recover": p.ar_recover,
        "reduced_size_fraction": p.reduced_size_fraction,
        "update_interval_ms": p.update_interval_ms,
        "accuracy_horizons": list(p.accuracy_horizons),
        "bar_seconds": p.bar_seconds,
    }

    return rpt
