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
    # When True, monthly_returns are absolute PnL changes normalised by
    # initial_capital — used whenever the prior month's equity crosses
    # zero or drops near it (where pct_change() produces nonsense values).
    monthly_returns_normalised: bool = False

    # Liquidation (Phase 9 backtest hardening)
    liquidated_at_bar: Optional[int] = None
    liquidation_equity_pct: float = 0.0

    # Chop / turnover diagnostics (Phase 9 backtest hardening)
    regime_flips: int = 0
    flips_per_bar: float = 0.0
    regime_run_lengths: Dict[str, Dict[str, int]] = field(default_factory=dict)

    # Params echo
    params_echo: Dict = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# Run-length histogram helper
# ────────────────────────────────────────────────────────────────────────

# Bucket boundaries for regime run-length histogram (Phase 9 chop diagnostics).
# Buckets are: 1 bar, 2-5, 6-20, 21-100, 100+.  Tuned so a typical 5m chop
# session (regime flips every 1-3 bars) shows up clearly in the lowest two
# buckets, while genuinely sustained runs land in 21-100 / 100+.
_RUN_LENGTH_BUCKETS: List[tuple[str, int, int]] = [
    ("1",     1, 1),
    ("2-5",   2, 5),
    ("6-20",  6, 20),
    ("21-100", 21, 100),
    ("100+",  101, 10**9),
]


def _compute_regime_run_lengths(regimes: np.ndarray) -> Dict[str, Dict[str, int]]:
    """Compute a per-regime run-length histogram bucketed by length.

    Returns a dict keyed by regime name; each value is a dict mapping the
    bucket label (matches `_RUN_LENGTH_BUCKETS`) to the count of runs whose
    length fell in that bucket.  Regimes with no runs do not appear.
    """
    out: Dict[str, Dict[str, int]] = {}
    if len(regimes) == 0:
        return out
    cur = regimes[0]
    run_len = 1
    runs: list[tuple[str, int]] = []
    for i in range(1, len(regimes)):
        if regimes[i] == cur:
            run_len += 1
        else:
            runs.append((str(cur), run_len))
            cur = regimes[i]
            run_len = 1
    runs.append((str(cur), run_len))
    for reg, length in runs:
        bucket = next(
            (label for label, lo, hi in _RUN_LENGTH_BUCKETS if lo <= length <= hi),
            "100+",
        )
        out.setdefault(reg, {label: 0 for label, _, _ in _RUN_LENGTH_BUCKETS})
        out[reg][bucket] += 1
    return out


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

    # Phase 9 — liquidation passthrough.
    rpt.liquidated_at_bar = getattr(result, "liquidated_at_bar", None)
    if rpt.liquidated_at_bar is not None and result.initial_capital > 0:
        rpt.liquidation_equity_pct = float(
            p.liquidation_equity_frac * 100.0
        )

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
        # Per-regime trade aggregates (Phase 9): join the trades frame to the
        # regime label to get trades / fees / turnover per regime.  Trades
        # already record their `wave_regime` at execution time so this is a
        # simple groupby, no additional bookkeeping required in the loop.
        if not result.trades.empty and "wave_regime" in result.trades.columns:
            trades_by_regime = result.trades.groupby("wave_regime").agg(
                trades=("notional", "count"),
                fees_paid=("fee", "sum"),
                turnover_usd=("notional", "sum"),
            ).to_dict("index")
        else:
            trades_by_regime = {}

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
            tag = trades_by_regime.get(reg, {})
            rpt.per_regime.append({
                "regime": reg,
                "time_pct": time_pct,
                "contribution_pnl": reg_eq_sub,
                "ann_return": reg_cagr,
                "sharpe": sharpe_ratio(reg_returns, bar_seconds=bar_sec),
                "n_bars": int(mask.sum()),
                "trades": int(tag.get("trades", 0) or 0),
                "fees_paid": float(tag.get("fees_paid", 0.0) or 0.0),
                "turnover_usd": float(tag.get("turnover_usd", 0.0) or 0.0),
            })

        # Chop diagnostics: regime flips and run-length distribution.
        regimes = bars[regime_col].to_numpy()
        if len(regimes) > 1:
            flips = int((regimes[1:] != regimes[:-1]).sum())
            rpt.regime_flips = flips
            rpt.flips_per_bar = float(flips) / float(len(regimes))
            rpt.regime_run_lengths = _compute_regime_run_lengths(regimes)

    # Monthly returns
    #
    # The classic `equity.resample("ME").last().pct_change()` formula breaks
    # whenever equity crosses zero or goes deeply negative — the denominator
    # becomes tiny / negative and `pct_change()` produces meaningless values
    # (e.g. +13717% / -601% months on a path that ends with negative equity).
    # When the prior-month equity is near zero or below 10% of initial capital
    # we fall back to an "absolute PnL change normalised by initial capital"
    # formula, which is bounded and interpretable in the same units.
    if (not equity.empty
            and isinstance(equity.index, pd.DatetimeIndex)):
        m_last = equity.resample("ME").last()
        m_prev = m_last.shift(1)
        init_cap = float(result.initial_capital) or 1.0
        threshold = 0.1 * init_cap
        # Use prior-month equity as denominator only when it is comfortably
        # positive; otherwise normalise by initial_capital.
        denom = m_prev.where(m_prev > threshold, init_cap)
        monthly = ((m_last - m_prev) / denom).dropna()
        # If any month had to use the initial-capital fallback, flag it
        # so the report renderer can label the column appropriately.
        rpt.monthly_returns_normalised = bool((m_prev <= threshold).any())
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
        # Phase 9 — backtest hardening flags
        "liquidation_equity_frac": p.liquidation_equity_frac,
        "slippage_bps": p.slippage_bps,
        "slippage_per_unit_bps": p.slippage_per_unit_bps,
    }

    return rpt
