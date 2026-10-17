"""Load the BTCUSDT research bars from the legacy archive and yearly shards."""

from __future__ import annotations

import logging

import pandas as pd

from research.manifest import CANONICAL_BUCKET, CANONICAL_PROFILE, CANONICAL_REGION, DatasetObject, list_ohlcv_objects

logger = logging.getLogger(__name__)


def load_research_bars(
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    bucket: str = CANONICAL_BUCKET,
    profile: str = CANONICAL_PROFILE,
    region: str = CANONICAL_REGION,
) -> tuple[pd.DataFrame, tuple[DatasetObject, ...]]:
    """Read every object that belongs to one research series.

    The collector's yearly reader does not open the legacy single-file
    archive. This loader does, then appends the yearly shards. Timestamps
    stored as epoch milliseconds are treated as UTC.
    """
    objects = list_ohlcv_objects(
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        bucket=bucket,
        profile=profile,
        region=region,
    )
    frames = []
    for item in objects:
        uri = f"s3://{bucket}/{item.key}"
        try:
            frame = pd.read_parquet(uri, storage_options={"profile": profile})
        except Exception as exc:
            raise RuntimeError(f"failed to read {uri}: {exc}") from exc
        frames.append(normalize_bars(frame))
        logger.info("Read %s rows from %s", len(frames[-1]), uri)
    combined = pd.concat(frames).sort_index()
    combined.index.name = "timestamp"
    logger.info("Combined %d rows from %d research objects", len(combined), len(objects))
    return combined, objects


def normalize_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Return OHLCV indexed by a timezone-aware UTC clock."""
    if frame.empty:
        raise ValueError("research bar object is empty")
    working = frame.copy()
    if "timestamp" in working.columns:
        column = working["timestamp"]
        if pd.api.types.is_datetime64_any_dtype(column):
            index = pd.to_datetime(column, utc=True)
        else:
            index = pd.to_datetime(column.to_numpy(dtype="int64"), unit="ms", utc=True)
        working = working.drop(columns=["timestamp"])
        working.index = pd.DatetimeIndex(index)
    elif isinstance(working.index, pd.DatetimeIndex):
        if working.index.tz is None:
            working.index = working.index.tz_localize("UTC")
        else:
            working.index = working.index.tz_convert("UTC")
    else:
        raise TypeError("research bars need a timestamp column or a datetime index")
    working.index.name = "timestamp"
    return working.sort_index()
