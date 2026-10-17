"""Causal log-return labels. A label exists only when both bars were observed."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index

logger = logging.getLogger(__name__)

HORIZONS_MINUTES = (5, 15, 60)


@dataclass(frozen=True)
class ReturnBuild:
    """Log returns indexed by the decision time, plus every exclusion count."""

    returns: pd.Series
    horizon_minutes: int
    gap_exclusions: int
    nonpositive_exclusions: int
    endpoint_exclusions: int


def horizon_log_returns(close: pd.Series, horizon_minutes: int) -> ReturnBuild:
    """Build ``log(close[t + h]) - log(close[t])`` only for exact observed pairs.

    The series is indexed at ``t``, the time the decision may use the
    information. Its outcome is not known until ``t + h``. A missing bar
    is an exclusion, not a return over a longer interval.
    """
    if horizon_minutes < 1:
        raise ValueError(f"horizon must be at least one minute, got {horizon_minutes}")
    if close.empty:
        raise ValueError("close series is empty")
    index = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=index, name="close").sort_index()
    if values.index.has_duplicates:
        raise ValueError("close series has duplicate timestamps; audit it before building labels")

    horizon = pd.Timedelta(minutes=horizon_minutes)
    target = values.index + horizon
    future = pd.Series(values.to_numpy(), index=values.index).reindex(target)
    future.index = values.index
    beyond = target > values.index[-1]
    missing = ~beyond & future.isna()
    nonpositive = ~beyond & ~missing & (
        ~np.isfinite(values.to_numpy())
        | ~np.isfinite(future.to_numpy())
        | (values.to_numpy() <= 0)
        | (future.to_numpy() <= 0)
    )
    usable = ~beyond & ~missing & ~nonpositive
    returns = pd.Series(
        np.log(future.loc[usable].to_numpy()) - np.log(values.loc[usable].to_numpy()),
        index=values.index[usable],
        name=f"log_return_{horizon_minutes}m",
    )
    gap_exclusions = int(missing.sum())
    nonpositive_exclusions = int(nonpositive.sum())
    endpoint_exclusions = int(beyond.sum())
    if gap_exclusions or nonpositive_exclusions:
        logger.warning(
            "Excluded %d gap and %d non-positive %d-minute returns",
            gap_exclusions,
            nonpositive_exclusions,
            horizon_minutes,
        )
    logger.info(
        "Built %d %d-minute log returns; endpoint_exclusions=%d",
        len(returns),
        horizon_minutes,
        endpoint_exclusions,
    )
    return ReturnBuild(
        returns=returns,
        horizon_minutes=horizon_minutes,
        gap_exclusions=gap_exclusions,
        nonpositive_exclusions=nonpositive_exclusions,
        endpoint_exclusions=endpoint_exclusions,
    )
