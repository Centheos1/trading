"""Audit one-minute bars without inventing the minutes that are absent."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.manifest import QualityExclusions

logger = logging.getLogger(__name__)

_PRICE_COLUMNS = ("open", "high", "low", "close")


@dataclass(frozen=True)
class QualityReport:
    """What the audit kept, removed, and refused to fill."""

    rows_in: int
    rows_out: int
    exclusions: QualityExclusions
    largest_gap_minutes: int


def audit_minute_bars(bars: pd.DataFrame) -> tuple[pd.DataFrame, QualityReport]:
    """Quarantine bad rows, count gaps, and return the kept minute bars.

    A missing minute stays missing. The next observed return is not a
    one-minute return, and this function does not manufacture one.
    """
    missing = set(_PRICE_COLUMNS) - set(bars.columns)
    if missing:
        raise ValueError(f"minute bars are missing columns: {sorted(missing)}")
    index = as_utc_index(bars.index)
    frame = bars.copy()
    frame.index = index
    frame = frame.sort_index()
    rows_in = int(len(frame))

    duplicated = frame.index.duplicated(keep="last")
    duplicate_timestamps = int(duplicated.sum())
    if duplicate_timestamps:
        logger.warning(
            "Quarantined %d duplicate minute timestamps; the last row is kept",
            duplicate_timestamps,
        )
        frame = frame.loc[~duplicated]

    off_grid = (
        (frame.index.second != 0)
        | (frame.index.microsecond != 0)
        | (frame.index.nanosecond != 0)
    )
    off_grid_timestamps = int(off_grid.sum())
    if off_grid_timestamps:
        logger.warning("Quarantined %d timestamps that are not on a minute boundary", off_grid_timestamps)
        frame = frame.loc[~off_grid]
    if frame.empty:
        raise RuntimeError("quality audit removed every row before price checks")

    prices = frame.loc[:, list(_PRICE_COLUMNS)].to_numpy(dtype=float)
    open_, high, low, close = (prices[:, column] for column in range(4))
    bad_price = ~np.isfinite(prices).all(axis=1) | (prices <= 0).any(axis=1)
    inconsistent = (
        (high + 1e-12 < np.maximum.reduce([open_, close, low]))
        | (low - 1e-12 > np.minimum.reduce([open_, close, high]))
    ) & ~bad_price
    nonpositive_prices = int(bad_price.sum())
    inconsistent_ohlc = int(inconsistent.sum())
    if nonpositive_prices or inconsistent_ohlc:
        logger.warning(
            "Quarantined %d non-positive or non-finite price rows and %d inconsistent OHLC rows",
            nonpositive_prices,
            inconsistent_ohlc,
        )
        frame = frame.loc[~(bad_price | inconsistent)]
    if frame.empty:
        raise RuntimeError("quality audit quarantined every price row")

    expected = pd.date_range(frame.index[0], frame.index[-1], freq="min", tz="UTC")
    missing_minutes = int(len(expected.difference(frame.index)))
    steps = frame.index.to_series().diff().dt.total_seconds().div(60)
    largest_gap = int(steps.max()) if len(frame) > 1 else 0
    if missing_minutes:
        logger.warning(
            "Minute series is missing %d minutes; largest observed step is %d minutes",
            missing_minutes,
            largest_gap,
        )
    exclusions = QualityExclusions(
        duplicate_timestamps=duplicate_timestamps,
        off_grid_timestamps=off_grid_timestamps,
        nonpositive_prices=nonpositive_prices,
        inconsistent_ohlc=inconsistent_ohlc,
        missing_minutes=missing_minutes,
    )
    report = QualityReport(
        rows_in=rows_in,
        rows_out=int(len(frame)),
        exclusions=exclusions,
        largest_gap_minutes=largest_gap,
    )
    logger.info(
        "Minute audit kept %d of %d rows; missing_minutes=%d",
        report.rows_out,
        report.rows_in,
        missing_minutes,
    )
    return frame, report
