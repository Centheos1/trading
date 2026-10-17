"""UTC event-time helpers for research inputs."""

from __future__ import annotations

import pandas as pd


def as_utc_index(index: pd.Index) -> pd.DatetimeIndex:
    """Return a UTC index, or raise when the clock is missing or local."""
    if not isinstance(index, pd.DatetimeIndex):
        raise TypeError(f"expected a DatetimeIndex, got {type(index).__name__}")
    if len(index) == 0:
        raise ValueError("time index is empty")
    if index.tz is None:
        raise ValueError("timestamps are naive; research clocks must be UTC")
    offsets = pd.Index(index.strftime("%z"))
    if not (offsets == "+0000").all():
        found = sorted(set(offsets))
        raise ValueError(f"timestamps must be UTC, found offsets {found}")
    converted = pd.DatetimeIndex(index.tz_convert("UTC"))
    converted.freq = None
    return converted
