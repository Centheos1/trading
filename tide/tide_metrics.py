"""
Standard quantitative performance and risk metrics for Tide backtests.

All formulas use bar-level returns (``result.returns``) and the equity
curve.  Annualization assumes a continuous crypto trading year:
``periods_per_year = (365 * 24 * 3600) / bar_seconds``.

The metrics computed here are the same set a quant team would expect on
any production strategy report:

  Returns / growth
    total_return, cagr, ann_return

  Risk
    ann_vol, ann_downside_vol, max_drawdown, max_drawdown_duration,
    var_95, var_99, es_95 (CVaR), es_99 (CVaR), ulcer_index,
    skewness, kurtosis (excess)

  Risk-adjusted
    sharpe, sortino, calmar, mar_ratio, omega (threshold = 0)

  Trade / activity
    num_trades, turnover_per_year, exposure_pct, avg_position_abs,
    win_rate, profit_factor, expectancy_per_bar, avg_win, avg_loss

  Regime / bias breakdowns
    per-regime: time_pct, contribution, return, hit_rate
    per-bias:   time_pct, contribution, return, hit_rate

  Stability
    monthly_returns, best_month, worst_month, pct_positive_months

Every metric is finite-safe (NaN-aware, divide-by-zero-safe) so a flat
or degenerate backtest does not crash the report path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from tide.tide_backtest import TideBacktestResult, SizingMode


# ────────────────────────────────────────────────────────────────────────
# Core helpers (all NaN- and zero-safe)
# ────────────────────────────────────────────────────────────────────────

def _periods_per_year(bar_seconds: float) -> float:
    if bar_seconds <= 0:
        return 0.0
    return (365.0 * 24.0 * 3600.0) / bar_seconds


def _safe_std(arr: np.ndarray, ddof: int = 1) -> float:
    if arr.size <= ddof:
        return 0.0
    val = float(np.std(arr, ddof=ddof))
    return val if np.isfinite(val) else 0.0


def _safe_div(num: float, den: float) -> float:
    if den == 0 or not np.isfinite(den):
        return 0.0
    out = num / den
    return out if np.isfinite(out) else 0.0


def annualized_return(equity: pd.Series, *, bar_seconds: float) -> float:
    """CAGR computed from first → last equity."""
    if equity.empty:
        return 0.0
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    if start <= 0 or end <= 0:
        return 0.0
    n_bars = len(equity) - 1
    if n_bars <= 0:
        return 0.0
    years = n_bars * bar_seconds / (365.0 * 24.0 * 3600.0)
    if years <= 0.0:
        return 0.0
    try:
        cagr = float((end / start) ** (1.0 / years) - 1.0)
    except (OverflowError, ZeroDivisionError):
        # Equity ratio is astronomically large (overfit on a short window).
        # Return a finite sentinel so downstream formatters don't crash.
        return 1e6 if end > start else -1.0
    import math
    return cagr if math.isfinite(cagr) else (1e6 if end > start else -1.0)


def annualized_volatility(returns: pd.Series, *, bar_seconds: float) -> float:
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size < 2:
        return 0.0
    return _safe_std(arr) * float(np.sqrt(_periods_per_year(bar_seconds)))


def annualized_downside_volatility(
    returns: pd.Series, *, bar_seconds: float, mar: float = 0.0
) -> float:
    """Downside deviation against ``mar`` (per-bar threshold), annualized."""
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size == 0:
        return 0.0
    downside = np.minimum(arr - mar, 0.0)
    rms = float(np.sqrt(np.mean(downside ** 2)))
    return rms * float(np.sqrt(_periods_per_year(bar_seconds)))


def sharpe_ratio(
    returns: pd.Series, *, bar_seconds: float, risk_free_annual: float = 0.0
) -> float:
    """Annualized Sharpe — excess return per unit of total volatility."""
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size < 2:
        return 0.0
    ppy = _periods_per_year(bar_seconds)
    rf_bar = risk_free_annual / ppy if ppy > 0 else 0.0
    excess = arr - rf_bar
    sd = _safe_std(excess)
    if sd == 0.0:
        return 0.0
    return float(np.mean(excess) / sd) * float(np.sqrt(ppy))


def sortino_ratio(
    returns: pd.Series, *, bar_seconds: float, mar: float = 0.0
) -> float:
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size < 2:
        return 0.0
    ppy = _periods_per_year(bar_seconds)
    excess = arr - (mar / ppy if ppy > 0 else 0.0)
    downside = np.minimum(arr - (mar / ppy if ppy > 0 else 0.0), 0.0)
    dd = float(np.sqrt(np.mean(downside ** 2)))
    if dd == 0.0:
        return 0.0
    return float(np.mean(excess) / dd) * float(np.sqrt(ppy))


def max_drawdown(equity: pd.Series) -> Tuple[float, int, Optional[pd.Timestamp]]:
    """Returns (max_drawdown_fraction, duration_in_bars, trough_timestamp).

    Drawdown is reported as a *negative fraction* (e.g. -0.27 = 27% DD).
    Duration is the longest run of bars from peak to recovery (or end).
    """
    if equity.empty:
        return 0.0, 0, None
    eq = equity.to_numpy(dtype=np.float64)
    running_max = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(running_max > 0.0, (eq - running_max) / running_max, 0.0)
    if dd.size == 0:
        return 0.0, 0, None
    trough_idx = int(np.argmin(dd))
    peak_idx = int(np.argmax(running_max[: trough_idx + 1] if trough_idx > 0 else [0]))
    max_dd = float(dd[trough_idx])

    # Recovery / duration: from peak_idx forward, find first index where
    # equity exceeds running_max[peak_idx]; if none, duration = end - peak.
    peak_value = running_max[trough_idx]
    recovery_idx = trough_idx
    for j in range(trough_idx + 1, eq.size):
        if eq[j] >= peak_value:
            recovery_idx = j
            break
    else:
        recovery_idx = eq.size - 1
    duration_bars = int(recovery_idx - peak_idx)
    trough_ts = (
        equity.index[trough_idx]
        if isinstance(equity.index, pd.DatetimeIndex)
        else None
    )
    return max_dd, duration_bars, trough_ts


def historical_var(returns: pd.Series, alpha: float = 0.95) -> float:
    """Historical VaR at confidence ``alpha`` (positive = loss magnitude)."""
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size == 0:
        return 0.0
    losses = -arr
    return float(np.quantile(losses, alpha))


def historical_es(returns: pd.Series, alpha: float = 0.95) -> float:
    """Historical Expected Shortfall (CVaR) at confidence ``alpha``."""
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size == 0:
        return 0.0
    losses = -arr
    var = float(np.quantile(losses, alpha))
    tail = losses[losses >= var]
    if tail.size == 0:
        return var
    return float(np.mean(tail))


def ulcer_index(equity: pd.Series) -> float:
    """RMS of percent drawdowns — penalizes deep + prolonged drawdowns."""
    if equity.empty:
        return 0.0
    eq = equity.to_numpy(dtype=np.float64)
    running_max = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        dd = np.where(running_max > 0.0, (eq - running_max) / running_max, 0.0)
    return float(np.sqrt(np.mean(dd ** 2)))


def skew_kurt(returns: pd.Series) -> Tuple[float, float]:
    arr = returns.dropna().to_numpy(dtype=np.float64)
    if arr.size < 4:
        return 0.0, 0.0
    mean = float(np.mean(arr))
    sd = _safe_std(arr, ddof=0)
    if sd == 0.0:
        return 0.0, 0.0
    n = arr.size
    skew = float(np.mean(((arr - mean) / sd) ** 3))
    kurt = float(np.mean(((arr - mean) / sd) ** 4)) - 3.0
    return skew, kurt


def calmar_ratio(equity: pd.Series, *, bar_seconds: float) -> float:
    cagr = annualized_return(equity, bar_seconds=bar_seconds)
    mdd, _, _ = max_drawdown(equity)
    if mdd == 0:
        return 0.0
    return _safe_div(cagr, abs(mdd))


def omega_ratio(returns: pd.Series, threshold: float = 0.0) -> float:
    arr = returns.dropna().to_numpy(dtype=np.float64) - threshold
    if arr.size == 0:
        return 0.0
    gains = float(np.sum(arr[arr > 0]))
    losses = float(-np.sum(arr[arr < 0]))
    if losses == 0.0:
        return float("inf") if gains > 0 else 0.0
    return _safe_div(gains, losses)


# ────────────────────────────────────────────────────────────────────────
# Trade-stat metrics (computed off the trades DataFrame + bars)
# ────────────────────────────────────────────────────────────────────────

def trade_round_trip_pnl(bars: pd.DataFrame) -> List[float]:
    """Per-round-trip realized PnL (entry → exit / flip).

    A *round trip* runs from the bar a non-zero position is opened until
    the bar the position is closed or flipped to the opposite side.
    PnL is only accumulated while we believe we are *in* a position —
    this is intentionally defensive so a pathological input (PnL accruing
    while flat) cannot inflate the trip count.
    """
    pos = bars["position"].to_numpy(dtype=np.float64)
    pnl = bars["net_pnl"].to_numpy(dtype=np.float64)
    n = pos.size
    trips: List[float] = []
    if n == 0:
        return trips

    cur_pnl = 0.0
    in_position = False
    last_sign = 0
    for i in range(n):
        cur_sign = 0 if pos[i] == 0.0 else (1 if pos[i] > 0.0 else -1)
        if not in_position:
            if cur_sign != 0:
                in_position = True
                last_sign = cur_sign
                cur_pnl = float(pnl[i])
            # else: still flat, ignore PnL on this bar
        else:
            if cur_sign == 0:
                # Flatten — close the open trip; pnl[i] should be 0 for a
                # well-formed backtester (position zero ⇒ no MTM), but if
                # not, it's stale PnL from prior bar held in the input.
                trips.append(cur_pnl)
                cur_pnl = 0.0
                in_position = False
                last_sign = 0
            elif cur_sign == last_sign:
                cur_pnl += float(pnl[i])
            else:
                # Sign flip — close prior trip, immediately open new one
                trips.append(cur_pnl)
                cur_pnl = float(pnl[i])
                last_sign = cur_sign
                in_position = True
    if in_position:
        trips.append(cur_pnl)
    return trips


def monthly_returns(equity: pd.Series) -> pd.Series:
    """Monthly compounded returns from an equity curve."""
    if equity.empty or not isinstance(equity.index, pd.DatetimeIndex):
        return pd.Series(dtype=np.float64)
    monthly = equity.resample("ME").last()
    return monthly.pct_change().dropna()


# ────────────────────────────────────────────────────────────────────────
# Top-level report
# ────────────────────────────────────────────────────────────────────────

@dataclass
class TidePerformanceReport:
    """Comprehensive performance report for a Tide backtest."""

    # ── Run metadata ──
    symbol: str = ""
    exchange: str = ""
    timeframe: str = ""
    bar_seconds: float = 0.0
    start: Optional[pd.Timestamp] = None
    end: Optional[pd.Timestamp] = None
    num_bars: int = 0
    initial_capital: float = 0.0
    final_equity: float = 0.0

    # ── Returns / growth ──
    total_return: float = 0.0
    cagr: float = 0.0
    ann_return: float = 0.0
    avg_bar_return: float = 0.0

    # ── Risk ──
    ann_volatility: float = 0.0
    ann_downside_volatility: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration_bars: int = 0
    max_drawdown_trough: Optional[pd.Timestamp] = None
    var_95: float = 0.0
    var_99: float = 0.0
    es_95: float = 0.0
    es_99: float = 0.0
    ulcer_index: float = 0.0
    return_skewness: float = 0.0
    return_kurtosis_excess: float = 0.0

    # ── Risk-adjusted ──
    sharpe: float = 0.0
    sortino: float = 0.0
    calmar: float = 0.0
    omega: float = 0.0

    # ── Activity / trades ──
    num_rebalances: int = 0
    num_round_trips: int = 0
    turnover_per_year: float = 0.0
    exposure_pct: float = 0.0
    avg_position_abs: float = 0.0
    fees_paid: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy_per_trip: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    # ── Breakdowns ──
    regime_breakdown: pd.DataFrame = field(default_factory=pd.DataFrame)
    bias_breakdown: pd.DataFrame = field(default_factory=pd.DataFrame)

    # ── Stability ──
    monthly_returns: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    best_month: float = 0.0
    worst_month: float = 0.0
    pct_positive_months: float = 0.0

    # ── Signal accuracy (forward-looking) ──
    accuracy: Optional[Any] = field(default=None)   # AccuracyReport | None

    # ── Param echo (for traceability in saved reports) ──
    params: Dict = field(default_factory=dict)


def _breakdown(
    bars: pd.DataFrame, group_col: str, bar_seconds: float
) -> pd.DataFrame:
    """Build a per-group activity / contribution table."""
    if bars.empty or group_col not in bars.columns:
        return pd.DataFrame()
    grouped = bars.groupby(group_col, observed=True)
    rows = []
    total_bars = float(len(bars))
    for name, sub in grouped:
        n = float(len(sub))
        net = float(sub["net_pnl"].sum())
        gross = float(sub["gross_pnl"].sum())
        rets = sub["bar_return"].dropna().to_numpy(dtype=np.float64)
        win = float((rets > 0).mean()) if rets.size else 0.0
        sd = _safe_std(rets)
        ppy = _periods_per_year(bar_seconds)
        sharpe = (
            float(np.mean(rets) / sd) * float(np.sqrt(ppy))
            if rets.size > 1 and sd > 0
            else 0.0
        )
        rows.append({
            "name": name,
            "bars": int(n),
            "time_pct": _safe_div(n, total_bars) * 100.0,
            "net_pnl": net,
            "gross_pnl": gross,
            "fees": float(sub["fee"].sum()),
            "hit_rate_pct": win * 100.0,
            "sharpe": sharpe,
        })
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("net_pnl", ascending=False).reset_index(drop=True)
    return df


def compute_report(result: TideBacktestResult) -> TidePerformanceReport:
    """Build a full :class:`TidePerformanceReport` from a backtest result."""
    p = result.params
    bars = result.bars
    bar_seconds = float(p.bar_seconds)

    rep = TidePerformanceReport()
    rep.symbol = p.symbol
    rep.exchange = p.exchange
    rep.timeframe = p.timeframe
    rep.bar_seconds = bar_seconds
    if isinstance(bars.index, pd.DatetimeIndex) and not bars.empty:
        rep.start = bars.index[0]
        rep.end = bars.index[-1]
    rep.num_bars = int(len(bars))
    rep.initial_capital = float(result.initial_capital)
    rep.final_equity = float(result.final_equity)

    rets = bars["bar_return"]
    eq = bars["equity"]

    rep.total_return = _safe_div(rep.final_equity - rep.initial_capital,
                                  rep.initial_capital)
    rep.cagr = annualized_return(eq, bar_seconds=bar_seconds)
    rep.ann_return = rep.cagr
    rep.avg_bar_return = float(rets.mean()) if not rets.empty else 0.0

    rep.ann_volatility = annualized_volatility(rets, bar_seconds=bar_seconds)
    rep.ann_downside_volatility = annualized_downside_volatility(
        rets, bar_seconds=bar_seconds
    )
    mdd, mdd_dur, mdd_ts = max_drawdown(eq)
    rep.max_drawdown = mdd
    rep.max_drawdown_duration_bars = mdd_dur
    rep.max_drawdown_trough = mdd_ts
    rep.var_95 = historical_var(rets, alpha=0.95)
    rep.var_99 = historical_var(rets, alpha=0.99)
    rep.es_95 = historical_es(rets, alpha=0.95)
    rep.es_99 = historical_es(rets, alpha=0.99)
    rep.ulcer_index = ulcer_index(eq)
    skew, kurt = skew_kurt(rets)
    rep.return_skewness = skew
    rep.return_kurtosis_excess = kurt

    rep.sharpe = sharpe_ratio(rets, bar_seconds=bar_seconds)
    rep.sortino = sortino_ratio(rets, bar_seconds=bar_seconds)
    rep.calmar = calmar_ratio(eq, bar_seconds=bar_seconds)
    rep.omega = omega_ratio(rets)

    rep.num_rebalances = int(len(result.trades))
    fees = float(bars["fee"].sum())
    rep.fees_paid = fees
    abs_pos = bars["position"].abs()
    rep.avg_position_abs = float(abs_pos.mean()) if not abs_pos.empty else 0.0
    in_market = (bars["position"] != 0.0)
    rep.exposure_pct = float(in_market.mean()) * 100.0 if not bars.empty else 0.0

    n_bars = float(len(bars))
    bars_per_year = _periods_per_year(bar_seconds)
    if n_bars > 0 and bars_per_year > 0:
        rep.turnover_per_year = rep.num_rebalances * bars_per_year / n_bars

    trips = trade_round_trip_pnl(bars)
    if trips:
        arr = np.asarray(trips, dtype=np.float64)
        wins = arr[arr > 0]
        losses = arr[arr < 0]
        rep.num_round_trips = int(arr.size)
        rep.win_rate = float(wins.size / arr.size) * 100.0
        rep.avg_win = float(wins.mean()) if wins.size else 0.0
        rep.avg_loss = float(losses.mean()) if losses.size else 0.0
        rep.largest_win = float(wins.max()) if wins.size else 0.0
        rep.largest_loss = float(losses.min()) if losses.size else 0.0
        gross_w = float(wins.sum()) if wins.size else 0.0
        gross_l = float(-losses.sum()) if losses.size else 0.0
        rep.profit_factor = (
            float("inf") if gross_l == 0 and gross_w > 0
            else _safe_div(gross_w, gross_l)
        )
        rep.expectancy_per_trip = float(arr.mean())

    rep.regime_breakdown = _breakdown(bars, "vol_regime", bar_seconds)
    rep.bias_breakdown = _breakdown(bars, "bias", bar_seconds)

    mret = monthly_returns(eq)
    rep.monthly_returns = mret
    if not mret.empty:
        rep.best_month = float(mret.max())
        rep.worst_month = float(mret.min())
        rep.pct_positive_months = float((mret > 0).mean()) * 100.0

    # Echo params for traceability in markdown / disk reports.
    rep.params = {
        "vol_window": p.vol_window,
        "eta_window": p.eta_window,
        "lsi_window": p.lsi_window,
        "timeframe": p.timeframe,
        "bar_seconds": p.bar_seconds,
        "vol_regime_thresholds": list(p.vol_regime_thresholds),
        "eta_threshold": p.eta_threshold,
        "bias_deadband": p.bias_deadband,
        "lsi_weights": list(p.lsi_weights),
        "risk_mult_by_regime": list(p.risk_mult_by_regime),
        "lsi_reduce_threshold": p.lsi_reduce_threshold,
        "lsi_reduce_slope": p.lsi_reduce_slope,
        "es_budget_global": p.es_budget_global,
        "sizing_mode": p.sizing_mode.value,
        "max_position_usd": p.max_position_usd,
        "max_position_base": p.max_position_base,
        "leverage": p.leverage,
        "taker_fee_bps": p.taker_fee_bps,
        "maker_fee_bps": p.maker_fee_bps,
        "rebalance_threshold": p.rebalance_threshold,
    }

    # ── Signal accuracy (forward-looking) ─────────────────────────────
    try:
        from tide.tide_accuracy import compute_accuracy_report
        base_mode = p.sizing_mode == SizingMode.BASE
        sizing_base = (
            float(p.max_position_base) if base_mode else float(p.max_position_usd)
        )
        rep.accuracy = compute_accuracy_report(
            bars,
            horizons=list(p.accuracy_horizons),
            sizing_base=sizing_base,
            scale_by_price=base_mode,
            layer="tide",
        )
    except Exception as exc:  # never crash the report path
        import logging
        logging.getLogger(__name__).warning("accuracy report skipped: %s", exc)

    return rep
