"""Load the BTCUSDT research bars from the legacy archive and yearly shards."""

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from research.manifest import (
    CANONICAL_BUCKET,
    CANONICAL_PROFILE,
    CANONICAL_REGION,
    DatasetObject,
    list_ohlcv_objects,
    list_parquet_objects,
)

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


def load_research_panel(
    *,
    venue: str,
    symbols: Iterable[str],
    timeframe: str,
    bucket: str = CANONICAL_BUCKET,
    profile: str = CANONICAL_PROFILE,
    region: str = CANONICAL_REGION,
) -> tuple[dict[str, pd.DataFrame], dict[str, tuple[DatasetObject, ...]]]:
    """Load every declared panel member and fail if the declaration is invalid."""
    declared = tuple(symbols)
    if len(declared) < 2 or len(set(declared)) != len(declared):
        raise ValueError("research panel needs at least two unique symbols")
    frames: dict[str, pd.DataFrame] = {}
    objects: dict[str, tuple[DatasetObject, ...]] = {}
    for symbol in declared:
        frame, identified = load_research_bars(
            venue=venue,
            symbol=symbol,
            timeframe=timeframe,
            bucket=bucket,
            profile=profile,
            region=region,
        )
        frames[symbol] = frame
        objects[symbol] = identified
    logger.info("Loaded research panel symbols=%s", list(declared))
    return frames, objects


def load_research_series(
    series: Iterable[tuple[str, str]],
    *,
    timeframe: str,
    bucket: str = CANONICAL_BUCKET,
    profile: str = CANONICAL_PROFILE,
    region: str = CANONICAL_REGION,
) -> tuple[dict[str, pd.DataFrame], dict[str, tuple[DatasetObject, ...]]]:
    """Load a declared (venue, symbol) series list and preserve its order.

    Symbols must be unique across venues. A missing series fails the load;
    it is not dropped from the panel.
    """
    declared = tuple(series)
    symbols = tuple(symbol for _, symbol in declared)
    if len(declared) < 2 or len(set(symbols)) != len(symbols):
        raise ValueError("research series need at least two unique symbols")
    frames: dict[str, pd.DataFrame] = {}
    objects: dict[str, tuple[DatasetObject, ...]] = {}
    for venue, symbol in declared:
        if not venue or not symbol:
            raise ValueError(f"research series entry {(venue, symbol)!r} is incomplete")
        frame, identified = load_research_bars(
            venue=venue,
            symbol=symbol,
            timeframe=timeframe,
            bucket=bucket,
            profile=profile,
            region=region,
        )
        frames[symbol] = frame
        objects[symbol] = identified
    logger.info("Loaded research series=%s", list(declared))
    return frames, objects


def list_h7_trade_objects(settings: dict, *, client=None) -> tuple[DatasetObject, ...]:
    """List the prospective trade objects in every registered layout."""
    found: list[DatasetObject] = []
    for prefix in settings["trade_prefixes"]:
        key_prefix = f"{prefix}/{settings['venue']}/{settings['symbol']}/trades/"
        found.extend(
            list_parquet_objects(
                bucket=settings["bucket"],
                prefix=key_prefix,
                profile=settings["profile"],
                region=settings["region"],
                client=client,
            )
        )
    objects = tuple(sorted(found, key=lambda item: item.key))
    if not objects:
        raise RuntimeError(
            f"no prospective trade objects for {settings['symbol']} in {list(settings['trade_prefixes'])}"
        )
    logger.info("H7 trade manifest contains %d objects", len(objects))
    return objects


def list_h7_depth_inventory(settings: dict, *, client=None) -> tuple[int, int]:
    """Count depth-diff objects without downloading them."""
    count = 0
    size = 0
    for prefix in settings["trade_prefixes"]:
        key_prefix = f"{prefix}/{settings['venue']}/{settings['symbol']}/depth_updates/"
        objects = list_parquet_objects(
            bucket=settings["bucket"],
            prefix=key_prefix,
            profile=settings["profile"],
            region=settings["region"],
            client=client,
        )
        count += len(objects)
        size += sum(item.size for item in objects)
    if count == 0:
        logger.warning("H7 depth-diff inventory is empty; diffs stay out of the model either way")
    else:
        logger.info("H7 depth-diff inventory objects=%d bytes=%d", count, size)
    return count, size


def select_trade_objects(
    objects: tuple[DatasetObject, ...] | list[DatasetObject],
) -> tuple[tuple[DatasetObject, ...], int]:
    """Drop a flat daily file when that day already has immutable shards."""
    shard_days = {
        day
        for item in objects
        if "/date=" in item.key and (day := _day_from_trade_key(item.key))
    }
    kept: list[DatasetObject] = []
    skipped = 0
    for item in objects:
        day = _day_from_trade_key(item.key)
        if "/date=" not in item.key and day in shard_days:
            skipped += 1
            logger.warning("Skipping daily trade file superseded by shards: %s", item.key)
            continue
        kept.append(item)
    return tuple(kept), skipped


def load_trade_tape(
    objects: tuple[DatasetObject, ...] | list[DatasetObject],
    *,
    bucket: str,
    profile: str,
    region: str = CANONICAL_REGION,
) -> tuple[pd.DataFrame, tuple[DatasetObject, ...], int]:
    """Read trade prints. An unreadable object is counted and left out of the digest."""
    if not objects:
        raise ValueError("trade tape load needs at least one object")
    selected, skipped = select_trade_objects(objects)
    if skipped:
        logger.warning("Skipped %d daily trade files superseded by shards", skipped)
    if not selected:
        raise RuntimeError("trade object selection removed every file")
    columns = ["timestamp", "price", "quantity", "is_buyer_maker"]
    frames, readable, unreadable = _read_trade_objects(
        selected,
        bucket=bucket,
        profile=profile,
        region=region,
        columns=columns,
    )
    if not readable:
        raise RuntimeError(f"every trade object failed to read; unreadable={unreadable}")
    combined = pd.concat(frames, ignore_index=True)
    logger.info(
        "Combined %d trades from %d objects; unreadable=%d skipped_daily=%d",
        len(combined),
        len(readable),
        unreadable,
        skipped,
    )
    return combined, tuple(readable), unreadable


def _day_from_trade_key(key: str) -> str:
    if "date=" in key:
        return key.split("date=", 1)[1][:10]
    name = key.rsplit("/", 1)[-1].removesuffix(".parquet")
    if len(name) == 10 and name[4] == "-" and name[7] == "-":
        return name
    return ""


def _read_trade_objects(
    objects: tuple[DatasetObject, ...],
    *,
    bucket: str,
    profile: str,
    region: str,
    columns: list[str],
) -> tuple[list[pd.DataFrame], list[DatasetObject], int]:
    import io
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    import pyarrow.parquet as pq

    local = threading.local()

    def client():
        cached = getattr(local, "client", None)
        if cached is None:
            import boto3

            cached = boto3.Session(profile_name=profile, region_name=region).client("s3")
            local.client = cached
        return cached

    def read_one(item: DatasetObject) -> tuple[DatasetObject, pd.DataFrame]:
        import time

        from botocore.exceptions import BotoCoreError, ClientError

        last_error: Exception | None = None
        for attempt in range(6):
            try:
                body = client().get_object(Bucket=bucket, Key=item.key)["Body"].read()
                table = pq.read_table(io.BytesIO(body), columns=columns)
                frame = table.to_pandas()
                break
            except (ClientError, BotoCoreError, TimeoutError, OSError) as exc:
                last_error = exc
                code = ""
                if isinstance(exc, ClientError):
                    code = str(exc.response.get("Error", {}).get("Code", ""))
                retryable = code in {"SlowDown", "503", "500", "RequestTimeout", "Throttling"} or not isinstance(
                    exc, ClientError
                )
                if not retryable or attempt == 5:
                    raise RuntimeError(f"{item.key}: {exc}") from exc
                time.sleep(min(2**attempt, 8))
        else:
            raise RuntimeError(f"{item.key}: {last_error}")
        missing = set(columns) - set(frame.columns)
        if missing:
            raise RuntimeError(f"{item.key} missing columns {sorted(missing)}")
        return item, frame.loc[:, columns]

    frames: list[pd.DataFrame] = []
    readable: list[DatasetObject] = []
    unreadable = 0
    workers = min(32, len(objects))
    pending: set = set()
    items = iter(objects)
    finished = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        def submit_next() -> bool:
            item = next(items, None)
            if item is None:
                return False
            pending.add(pool.submit(read_one, item))
            return True

        for _ in range(workers):
            if not submit_next():
                break
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                pending.discard(future)
                finished += 1
                try:
                    item, frame = future.result()
                except Exception as exc:
                    unreadable += 1
                    logger.error("Quarantined unreadable trade object: %s", exc)
                else:
                    frames.append(frame)
                    readable.append(item)
                    if len(frames) >= 2000:
                        frames = [pd.concat(frames, ignore_index=True)]
                if finished % 5000 == 0 or finished == len(objects):
                    logger.info("Read %d/%d trade objects", finished, len(objects))
                submit_next()
    return frames, readable, unreadable


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
