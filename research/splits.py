"""Rolling development windows and a sealed final calendar block."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from research.clock import as_utc_index

logger = logging.getLogger(__name__)

SEALED_MONTHS = 12
TRAIN_MONTHS = 24
VALIDATION_MONTHS = 6
STEP_MONTHS = 6


@dataclass(frozen=True)
class Origin:
    """One train/validation pair that ends before the sealed test."""

    name: str
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    validation_end: pd.Timestamp


def sealed_boundary(index: pd.DatetimeIndex, months: int = SEALED_MONTHS) -> pd.Timestamp:
    """Return the UTC midnight where the final sealed calendar block begins."""
    if months < 1:
        raise ValueError(f"sealed block must cover at least one month, got {months}")
    clock = as_utc_index(index)
    end = clock.max()
    boundary = (end - pd.DateOffset(months=months)).normalize()
    if boundary.tzinfo is None:
        boundary = boundary.tz_localize("UTC")
    if boundary <= clock.min() or boundary >= end:
        raise ValueError(
            f"history from {clock.min()} to {end} cannot reserve a {months}-month sealed test"
        )
    logger.info("Sealed test begins at %s", boundary.isoformat())
    return boundary


def rolling_origins(
    index: pd.DatetimeIndex,
    boundary: pd.Timestamp,
    *,
    train_months: int = TRAIN_MONTHS,
    validation_months: int = VALIDATION_MONTHS,
    step_months: int = STEP_MONTHS,
) -> tuple[Origin, ...]:
    """Build every development window that finishes at or before ``boundary``."""
    if min(train_months, validation_months, step_months) < 1:
        raise ValueError("train, validation, and step lengths must be positive")
    clock = as_utc_index(index)
    if boundary.tzinfo is None:
        raise ValueError("sealed boundary must be UTC")
    boundary = boundary.tz_convert("UTC")
    cursor = clock.min().normalize()
    origins: list[Origin] = []
    while True:
        train_end = cursor + pd.DateOffset(months=train_months)
        validation_end = train_end + pd.DateOffset(months=validation_months)
        if validation_end > boundary:
            break
        origins.append(
            Origin(
                name=train_end.strftime("%Y%m%d"),
                train_start=cursor,
                train_end=train_end,
                validation_end=validation_end,
            )
        )
        cursor = cursor + pd.DateOffset(months=step_months)
    if not origins:
        raise ValueError("no rolling origin fits before the sealed test")
    logger.info("Built %d rolling origins before %s", len(origins), boundary.isoformat())
    return tuple(origins)


def select_available(
    returns: pd.Series,
    horizon_minutes: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.Series:
    """Keep decisions in ``[start, end)`` whose label is also known before ``end``.

    A return decided near the cutoff uses a price after the cutoff. That
    label belongs to the next block, so it is excluded here.
    """
    if horizon_minutes < 1:
        raise ValueError(f"horizon must be at least one minute, got {horizon_minutes}")
    if returns.empty:
        raise ValueError("return series is empty")
    index = as_utc_index(returns.index)
    start = pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end).tz_convert("UTC")
    if start >= end:
        raise ValueError(f"window start {start} is not before end {end}")
    label_time = index + pd.Timedelta(minutes=horizon_minutes)
    chosen = (index >= start) & (label_time < end)
    selected = pd.Series(returns.to_numpy(), index=index).loc[chosen]
    selected.name = returns.name
    return selected


def development_returns(
    returns: pd.Series,
    horizon_minutes: int,
    boundary: pd.Timestamp,
) -> tuple[pd.Series, int]:
    """Remove every return whose outcome falls inside the sealed test."""
    index = as_utc_index(returns.index)
    boundary = pd.Timestamp(boundary).tz_convert("UTC")
    label_time = index + pd.Timedelta(minutes=horizon_minutes)
    blocked = label_time >= boundary
    held_out = int(blocked.sum())
    if held_out:
        logger.info(
            "Held %d %d-minute returns out of development because their labels cross %s",
            held_out,
            horizon_minutes,
            boundary.isoformat(),
        )
    kept = pd.Series(returns.to_numpy(), index=index).loc[~blocked]
    kept.name = returns.name
    return kept, held_out


def sealed_returns(
    returns: pd.Series,
    horizon_minutes: int,
    boundary: pd.Timestamp,
    *,
    confirmatory: bool,
) -> pd.Series:
    """Return the sealed sample only for an explicitly confirmatory evaluation."""
    if not confirmatory:
        raise RuntimeError("sealed test requested without confirmatory=True")
    index = as_utc_index(returns.index)
    boundary = pd.Timestamp(boundary).tz_convert("UTC")
    label_time = index + pd.Timedelta(minutes=horizon_minutes)
    chosen = label_time >= boundary
    if not bool(chosen.any()):
        raise ValueError("sealed test contains no labels")
    sample = pd.Series(returns.to_numpy(), index=index).loc[chosen]
    sample.name = returns.name
    logger.warning(
        "Confirmatory evaluation is reading %d sealed %d-minute labels",
        len(sample),
        horizon_minutes,
    )
    return sample
