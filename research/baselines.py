"""Causal one-minute baselines. A position at t earns the next exact minute."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index

logger = logging.getLogger(__name__)

MINUTES_PER_YEAR = 365.25 * 24 * 60


@dataclass(frozen=True)
class Performance:
    observations: int
    total_simple_return: float
    sharpe: float
    max_drawdown: float
    average_turnover: float
    average_absolute_position: float


def consecutive_simple_returns(close: pd.Series) -> tuple[pd.Series, int]:
    """Return the next one-minute simple return, indexed at the decision time.

    Rows whose following bar is not exactly one minute later are excluded
    and counted. They are not converted into multi-minute returns.
    """
    if close.empty:
        raise ValueError("close series is empty")
    index = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=index).sort_index()
    if values.index.has_duplicates:
        raise ValueError("close series has duplicate timestamps")
    future = values.shift(-1)
    step = values.index.to_series().diff().shift(-1)
    exact = step.eq(pd.Timedelta(minutes=1))
    returns = future / values - 1.0
    usable = exact & np.isfinite(returns) & (values > 0) & (future > 0)
    excluded = int((~usable.fillna(False)).sum())
    if excluded:
        logger.warning("Excluded %d close rows from one-minute baseline returns", excluded)
    result = returns.loc[usable.fillna(False)]
    result.name = "simple_return_1m"
    return result, excluded


def momentum_position(close: pd.Series, lookback_minutes: int) -> pd.Series:
    """Sign of the trailing exact lookback log return, known at the close."""
    return _directional_position(close, lookback_minutes, reverse=False)


def mean_reversion_position(close: pd.Series, lookback_minutes: int) -> pd.Series:
    """Opposite sign of the trailing exact lookback log return."""
    return _directional_position(close, lookback_minutes, reverse=True)


def buy_and_hold_position(index: pd.Index) -> pd.Series:
    """A constant long position. It carries no forecast."""
    clock = as_utc_index(index)
    return pd.Series(1.0, index=clock, name="position")


def volatility_scale(
    close: pd.Series,
    lookback_minutes: int,
    target_annual_volatility: float,
    *,
    max_gross: float = 1.0,
) -> pd.Series:
    """Scale exposure toward a target using only returns known at that close."""
    if lookback_minutes < 2:
        raise ValueError("volatility lookback must cover at least two returns")
    if target_annual_volatility <= 0 or max_gross <= 0:
        raise ValueError("target volatility and gross cap must be positive")
    returns, _ = consecutive_simple_returns(close)
    # Index the completed return at the later close, which is when it is known.
    known = pd.Series(returns.to_numpy(), index=returns.index + pd.Timedelta(minutes=1))
    known = known.reindex(pd.Series(close.to_numpy(), index=as_utc_index(close.index)).sort_index().index)
    realised = known.rolling(lookback_minutes, min_periods=lookback_minutes).std(ddof=1)
    span = known.index.to_series().diff(lookback_minutes - 1)
    realised = realised.where(span.eq(pd.Timedelta(minutes=lookback_minutes - 1)))
    annualised = realised * np.sqrt(MINUTES_PER_YEAR)
    scale = target_annual_volatility / annualised
    scale = scale.where(np.isfinite(scale), 0.0).clip(lower=0.0, upper=max_gross)
    scale.name = "volatility_scale"
    return scale


def apply_costs(position: pd.Series, bar_returns: pd.Series, cost_bps: float) -> pd.Series:
    """Charge cost on the absolute position change, then earn the next-bar return.

    ``position`` at time t may depend on the close at t. It multiplies the
    return from t to the next minute. The position is zero wherever the
    two series do not share a timestamp.
    """
    if cost_bps < 0:
        raise ValueError(f"cost must be non-negative, got {cost_bps}")
    if bar_returns.empty:
        raise ValueError("bar returns are empty")
    aligned = position.reindex(bar_returns.index).fillna(0.0).astype(float)
    previous = aligned.shift(1).fillna(0.0)
    turnover = (aligned - previous).abs()
    net = aligned * bar_returns.astype(float) - turnover * (cost_bps / 10_000.0)
    net.name = "net_simple_return"
    return net


def performance(net: pd.Series, position: pd.Series) -> Performance:
    """Summarise a net simple-return path. A zero-variance path has a null Sharpe."""
    if len(net) < 2:
        raise ValueError(f"performance needs at least two returns, got {len(net)}")
    if not np.isfinite(net.to_numpy(dtype=float)).all():
        raise ValueError("net returns contain a non-finite value")
    wealth = (1.0 + net.astype(float)).cumprod()
    drawdown = wealth / wealth.cummax() - 1.0
    deviation = float(net.std(ddof=1))
    sharpe = float("nan") if deviation == 0.0 else float(net.mean() / deviation * np.sqrt(MINUTES_PER_YEAR))
    aligned = position.reindex(net.index).fillna(0.0).astype(float)
    turnover = (aligned - aligned.shift(1).fillna(0.0)).abs()
    return Performance(
        observations=int(len(net)),
        total_simple_return=float(wealth.iloc[-1] - 1.0),
        sharpe=sharpe,
        max_drawdown=float(drawdown.min()),
        average_turnover=float(turnover.mean()),
        average_absolute_position=float(aligned.abs().mean()),
    )


def _directional_position(close: pd.Series, lookback_minutes: int, *, reverse: bool) -> pd.Series:
    if lookback_minutes < 1:
        raise ValueError(f"lookback must be at least one minute, got {lookback_minutes}")
    index = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=index).sort_index()
    past = values.shift(lookback_minutes)
    exact = values.index.to_series().diff(lookback_minutes).eq(pd.Timedelta(minutes=lookback_minutes))
    change = np.log(values) - np.log(past)
    usable = exact & np.isfinite(change) & (values > 0) & (past > 0) & (change != 0)
    sign = np.sign(change.where(usable, 0.0))
    if reverse:
        sign = -sign
    position = sign.fillna(0.0)
    position.name = "position"
    return position
