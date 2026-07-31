"""OHLCV Parquet storage with local-filesystem and S3 backends.

Storage layout (both backends), yearly-partitioned:
    {root}/ohlcv/{exchange}/{symbol}/{timeframe}/{year}.parquet

Where ``{root}`` is:
    * Local:   ``./data``                     (LocalParquetStore)
    * S3:      ``s3://{bucket}``              (S3ParquetStore)

Yearly partitioning keeps each individual Parquet file small (~one year of
1-minute candles ≈ 525k rows / a few tens of MB).  This bounds the memory and
I/O cost of the read-modify-write ``append`` (the collector only ever rewrites
the current year, never the entire multi-year history) and lets a backfill
persist progress incrementally so it survives process restarts.

Greenfield: yearly files only. Legacy single-file
``{timeframe}.parquet`` paths are not read or written.

Backend selection:
    The ``get_ohlcv_store()`` factory returns a backend based on the
    ``DATA_STORE`` environment variable:
        ``DATA_STORE=local_parquet`` (default)  → LocalParquetStore
        ``DATA_STORE=s3``                        → S3ParquetStore

Parquet schema:
    timestamp : int64    (epoch milliseconds, UTC)
    open      : float64
    high      : float64
    low       : float64
    close     : float64
    volume    : float64
    spread    : float64  (optional — only present for Oanda)

Idempotent append:
    ``append`` performs a read-modify-write of the affected year file(s).
    Duplicate timestamps are removed (last-write-wins) before re-writing.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-path append locks
# ---------------------------------------------------------------------------
#
# ``append`` is a read-modify-write of a single Parquet file. The continuous
# collector runs it from a ThreadPoolExecutor; the driver dispatches one job
# per unique (exchange, symbol) so distinct paths never collide in practice,
# but a per-path lock makes the RMW safe even if the same path is ever
# submitted twice (duplicate jobs, multi-adapter overlap). Cheap insurance
# against silent last-write-wins data loss.

_PATH_LOCKS: Dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[path] = lock
        return lock


# ---------------------------------------------------------------------------
# Canonical column schema
# ---------------------------------------------------------------------------

OHLCV_COLUMNS_6 = ["timestamp", "open", "high", "low", "close", "volume"]
OHLCV_COLUMNS_7 = OHLCV_COLUMNS_6 + ["spread"]


def _normalise_rows(rows: Sequence[Sequence[float]]) -> pd.DataFrame:
    """Coerce an iterable of OHLCV tuples to a DataFrame with canonical cols."""
    if len(rows) == 0:
        return pd.DataFrame(columns=OHLCV_COLUMNS_6)
    width = len(rows[0])
    cols = OHLCV_COLUMNS_7 if width == 7 else OHLCV_COLUMNS_6
    df = pd.DataFrame(rows, columns=cols)
    df["timestamp"] = df["timestamp"].astype("int64")
    for c in cols[1:]:
        df[c] = df[c].astype("float64")
    return df


def _dedup_sort(df: pd.DataFrame) -> pd.DataFrame:
    """De-duplicate on timestamp (last wins) and sort ascending."""
    return (
        df.drop_duplicates(subset=["timestamp"], keep="last")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def _year_of(ts_ms: int) -> int:
    """UTC calendar year for an epoch-millisecond timestamp."""
    return int(pd.Timestamp(int(ts_ms), unit="ms", tz="UTC").year)


def _split_rows_by_year(df: pd.DataFrame) -> Iterator[Tuple[int, pd.DataFrame]]:
    """Yield ``(year, sub_df)`` partitions of ``df`` by UTC calendar year."""
    years = pd.DatetimeIndex(
        pd.to_datetime(df["timestamp"].to_numpy(dtype="int64"), unit="ms")
    ).year
    for year in sorted(set(int(y) for y in years)):
        mask = years == year
        yield year, df[mask].reset_index(drop=True)


def _empty_indexed() -> pd.DataFrame:
    return pd.DataFrame(columns=OHLCV_COLUMNS_6).set_index(
        pd.DatetimeIndex([], name="timestamp")
    )


# ---------------------------------------------------------------------------
# Resilience helpers (primarily for the S3 backend)
# ---------------------------------------------------------------------------

# Substrings of transient S3/network error messages that are worth retrying.
# These are the exact failure modes observed in the deployed collector logs
# (intermittent connection drops and truncated uploads under load).
_S3_RETRYABLE_SUBSTRINGS = (
    "could not connect to the endpoint",
    "connection",
    "content-length",
    "errno 22",
    "timed out",
    "timeout",
    "throttl",
    "slow down",
    "503",
    "500",
    "service unavailable",
    "internal error",
    "ssl",
    "ssleof",
    "unexpected_eof",
    "broken pipe",
)


def _is_corruption_error(exc: Exception) -> bool:
    """True if ``exc`` indicates a corrupt / non-parquet file.

    A corrupt object can never succeed on retry, so callers quarantine it
    and rewrite from scratch instead of failing forever.
    """
    msg = str(exc).lower()
    return (
        "magic bytes" in msg
        or "not a parquet file" in msg
        or "could not open parquet" in msg
        or "could not read schema" in msg
    )


def _with_retry(fn, *, attempts: int = 4, base_delay: float = 1.0):
    """Run ``fn`` with bounded exponential backoff on transient errors.

    Corruption errors are never retried (they are deterministic) and are
    re-raised immediately so the caller can quarantine the bad file.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if _is_corruption_error(exc):
                raise
            msg = str(exc).lower()
            retryable = any(s in msg for s in _S3_RETRYABLE_SUBSTRINGS)
            last_exc = exc
            if attempt >= attempts or not retryable:
                raise
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "S3 op failed (attempt %d/%d, retry in %.1fs): %s",
                attempt, attempts, delay, exc,
            )
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class OhlcvStore(ABC):
    """Abstract OHLCV store. Backend implementations live below."""

    @abstractmethod
    def path_for(self, exchange: str, symbol: str, timeframe: str) -> str:
        """Return a representative yearly locator (current UTC year)."""

    @abstractmethod
    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        ...

    @abstractmethod
    def read(
        self,
        exchange: str,
        symbol: str,
        timeframe: str = "1m",
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> pd.DataFrame:
        """Read all (or a slice of) rows for a symbol/timeframe.

        Returns a DataFrame indexed by ``DatetimeIndex`` (UTC, tz-naive)
        with columns ``open, high, low, close, volume[, spread]``.
        Returns an empty DataFrame when no data exists.
        """

    @abstractmethod
    def get_last_timestamp(
        self, exchange: str, symbol: str, timeframe: str = "1m"
    ) -> Optional[int]:
        """Return the maximum stored timestamp in milliseconds, or ``None``."""

    @abstractmethod
    def append(
        self,
        exchange: str,
        symbol: str,
        rows: Iterable[Sequence[float]],
        timeframe: str = "1m",
    ) -> int:
        """Append ``rows`` to the symbol's yearly Parquet file(s).

        ``rows`` is an iterable of 6-tuples ``(ts_ms, o, h, l, c, v)`` or
        7-tuples (with trailing ``spread``).  Rows are partitioned by UTC
        year and written to the matching ``{year}.parquet`` files.  Duplicate
        timestamps are de-duplicated last-write-wins.  Returns the number of
        new rows actually written.
        """

    @abstractmethod
    def list_symbols(
        self, exchange: str, timeframe: str = "1m"
    ) -> List[str]:
        ...


# ---------------------------------------------------------------------------
# Shared year-partitioned orchestration
# ---------------------------------------------------------------------------


class _YearPartitionedMixin:
    """Implements the public :class:`OhlcvStore` API on top of small,
    backend-specific I/O primitives so the local and S3 backends share the
    yearly-partitioning, dedup and corruption-handling logic.

    Backends must implement:
        _year_locator(e, s, tf, year)       -> locator for a year file
        _locator_exists(locator)            -> bool
        _read_df(locator, columns=None)     -> DataFrame (raises on missing/corrupt)
        _write_df(df, locator)              -> None
        _list_year_entries(e, s, tf)        -> sorted [(year, locator)]
        _quarantine(locator)                -> None
        _list_symbol_dirs(e)                -> list[str]
    """

    # -- primitives (overridden by backends) --------------------------------
    def _year_locator(
        self, exchange: str, symbol: str, timeframe: str, year: int
    ) -> str:
        raise NotImplementedError

    def _locator_exists(self, locator: str) -> bool:
        raise NotImplementedError

    def _read_df(self, locator: str, columns: Optional[List[str]] = None) -> pd.DataFrame:
        raise NotImplementedError

    def _write_df(self, df: pd.DataFrame, locator: str) -> None:
        raise NotImplementedError

    def _list_year_entries(
        self, exchange: str, symbol: str, timeframe: str
    ) -> List[Tuple[int, str]]:
        raise NotImplementedError

    def _quarantine(self, locator: str) -> None:
        raise NotImplementedError

    def _list_symbol_dirs(self, exchange: str) -> List[str]:
        raise NotImplementedError

    # -- shared helpers -----------------------------------------------------
    def _safe_read(
        self, locator: str, columns: Optional[List[str]] = None
    ) -> Optional[pd.DataFrame]:
        """Read a parquet locator, returning ``None`` on failure.

        Corrupt files are quarantined (renamed aside) so a single bad object
        cannot wedge the symbol forever — the next ``append`` rewrites it.
        """
        try:
            return self._read_df(locator, columns=columns)
        except Exception as exc:  # noqa: BLE001
            if _is_corruption_error(exc):
                logger.error(
                    "Corrupt parquet %s — quarantining and skipping: %s",
                    locator, exc,
                )
                try:
                    self._quarantine(locator)
                except Exception as q_exc:  # noqa: BLE001
                    logger.warning("Quarantine failed for %s: %s", locator, q_exc)
                return None
            logger.warning("Could not read %s: %s", locator, exc)
            return None

    def _symbol_has_data(self, exchange: str, symbol: str, timeframe: str) -> bool:
        return len(self._list_year_entries(exchange, symbol, timeframe)) > 0

    # -- public API ---------------------------------------------------------
    def path_for(self, exchange: str, symbol: str, timeframe: str) -> str:
        year = datetime.now(timezone.utc).year
        return self._year_locator(exchange, symbol, timeframe, year)

    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        return self._symbol_has_data(exchange, symbol, timeframe)

    def read(
        self,
        exchange: str,
        symbol: str,
        timeframe: str = "1m",
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> pd.DataFrame:
        y_from = _year_of(from_ts) if from_ts is not None else None
        y_to = _year_of(to_ts) if to_ts is not None else None

        frames: List[pd.DataFrame] = []
        for year, locator in self._list_year_entries(exchange, symbol, timeframe):
            if y_from is not None and year < y_from:
                continue
            if y_to is not None and year > y_to:
                continue
            df = self._safe_read(locator)
            if df is not None and not df.empty:
                frames.append(df)

        if not frames:
            return _empty_indexed()
        combined = _dedup_sort(pd.concat(frames, ignore_index=True))
        return _slice_and_index(combined, from_ts, to_ts)

    def get_last_timestamp(
        self, exchange: str, symbol: str, timeframe: str = "1m"
    ) -> Optional[int]:
        candidates: List[int] = []

        # Highest readable year file holds the latest yearly data.
        for year, locator in reversed(
            self._list_year_entries(exchange, symbol, timeframe)
        ):
            df = self._safe_read(locator, columns=["timestamp"])
            if df is not None and not df.empty:
                candidates.append(int(df["timestamp"].max()))
                break

        return max(candidates) if candidates else None

    def append(
        self,
        exchange: str,
        symbol: str,
        rows: Iterable[Sequence[float]],
        timeframe: str = "1m",
    ) -> int:
        new_df = _normalise_rows(list(rows))
        if new_df.empty:
            return 0

        total_written = 0
        for year, year_df in _split_rows_by_year(new_df):
            locator = self._year_locator(exchange, symbol, timeframe, year)
            with _lock_for(locator):
                existing = None
                if self._locator_exists(locator):
                    # Corrupt year file → quarantined → treated as empty and
                    # rewritten fresh from the incoming rows.
                    existing = self._safe_read(locator)
                if existing is not None and not existing.empty:
                    n_before = len(existing)
                    combined = _dedup_sort(
                        pd.concat([existing, year_df], ignore_index=True)
                    )
                    n_written = len(combined) - n_before
                else:
                    combined = _dedup_sort(year_df)
                    n_written = len(combined)
                self._write_df(combined, locator)
                total_written += max(n_written, 0)
        return int(total_written)

    def list_symbols(self, exchange: str, timeframe: str = "1m") -> List[str]:
        result: List[str] = []
        for symbol in self._list_symbol_dirs(exchange):
            if self._symbol_has_data(exchange, symbol, timeframe):
                result.append(symbol)
        return sorted(result)


# ---------------------------------------------------------------------------
# Local-filesystem backend
# ---------------------------------------------------------------------------


class LocalParquetStore(_YearPartitionedMixin, OhlcvStore):
    """Reads/writes Parquet files under ``{root}/ohlcv/...``."""

    def __init__(self, root: str = "data") -> None:
        self.root = root

    def _year_dir(self, exchange: str, symbol: str, timeframe: str) -> str:
        return os.path.join(self.root, "ohlcv", exchange, symbol, timeframe)

    def _year_locator(
        self, exchange: str, symbol: str, timeframe: str, year: int
    ) -> str:
        return os.path.join(self._year_dir(exchange, symbol, timeframe), f"{year}.parquet")

    def _locator_exists(self, locator: str) -> bool:
        return os.path.isfile(locator)

    def _read_df(self, locator: str, columns: Optional[List[str]] = None) -> pd.DataFrame:
        return pd.read_parquet(locator, columns=columns)

    def _write_df(self, df: pd.DataFrame, locator: str) -> None:
        os.makedirs(os.path.dirname(locator), exist_ok=True)
        df.to_parquet(locator, index=False, compression="snappy")

    def _list_year_entries(
        self, exchange: str, symbol: str, timeframe: str
    ) -> List[Tuple[int, str]]:
        directory = self._year_dir(exchange, symbol, timeframe)
        if not os.path.isdir(directory):
            return []
        out: List[Tuple[int, str]] = []
        for entry in os.listdir(directory):
            if not entry.endswith(".parquet"):
                continue
            stem = entry[: -len(".parquet")]
            if stem.isdigit():
                out.append((int(stem), os.path.join(directory, entry)))
        return sorted(out)

    def _quarantine(self, locator: str) -> None:
        if os.path.exists(locator):
            os.rename(locator, f"{locator}.corrupt-{int(time.time())}")

    def _list_symbol_dirs(self, exchange: str) -> List[str]:
        base = os.path.join(self.root, "ohlcv", exchange)
        if not os.path.isdir(base):
            return []
        return [
            entry
            for entry in os.listdir(base)
            if os.path.isdir(os.path.join(base, entry))
        ]


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------


class S3ParquetStore(_YearPartitionedMixin, OhlcvStore):
    """Reads/writes Parquet files in ``s3://{bucket}/ohlcv/...``.

    Uses pyarrow + s3fs.  Credentials are picked up from the standard
    AWS credential chain (env vars, ``~/.aws/credentials``, or — on EC2 —
    the instance profile).  Transient network errors are retried with
    backoff and corrupt objects are quarantined rather than re-read forever.
    """

    def __init__(self, bucket: str) -> None:
        if not bucket:
            raise ValueError("S3ParquetStore requires a bucket name")
        self.bucket = bucket
        self._fs_cache = None
        try:
            import s3fs  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "S3ParquetStore requires the s3fs package. "
                "Install via: pip install s3fs"
            ) from exc

    def _fs(self):
        import s3fs
        if self._fs_cache is None:
            self._fs_cache = s3fs.S3FileSystem()
        return self._fs_cache

    # -- locators -----------------------------------------------------------
    def _year_dir_key(self, exchange: str, symbol: str, timeframe: str) -> str:
        return f"{self.bucket}/ohlcv/{exchange}/{symbol}/{timeframe}"

    def _year_locator(
        self, exchange: str, symbol: str, timeframe: str, year: int
    ) -> str:
        return f"s3://{self.bucket}/ohlcv/{exchange}/{symbol}/{timeframe}/{year}.parquet"

    @staticmethod
    def _key_of(locator: str) -> str:
        return locator[len("s3://"):] if locator.startswith("s3://") else locator

    # -- primitives ---------------------------------------------------------
    def _locator_exists(self, locator: str) -> bool:
        key = self._key_of(locator)
        return bool(_with_retry(lambda: self._fs().exists(key)))

    def _read_df(self, locator: str, columns: Optional[List[str]] = None) -> pd.DataFrame:
        return _with_retry(
            lambda: pd.read_parquet(
                locator, columns=columns, storage_options={"anon": False}
            )
        )

    def _write_df(self, df: pd.DataFrame, locator: str) -> None:
        _with_retry(
            lambda: df.to_parquet(
                locator,
                index=False,
                compression="snappy",
                storage_options={"anon": False},
            )
        )

    def _list_year_entries(
        self, exchange: str, symbol: str, timeframe: str
    ) -> List[Tuple[int, str]]:
        fs = self._fs()
        directory = self._year_dir_key(exchange, symbol, timeframe)
        try:
            entries = _with_retry(lambda: fs.ls(directory, detail=False))
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list %s: %s", directory, exc)
            return []
        out: List[Tuple[int, str]] = []
        for entry in entries:
            name = entry.rstrip("/").rsplit("/", 1)[-1]
            if not name.endswith(".parquet"):
                continue
            stem = name[: -len(".parquet")]
            if stem.isdigit():
                out.append((int(stem), f"s3://{entry}"))
        return sorted(out)

    def _quarantine(self, locator: str) -> None:
        fs = self._fs()
        src = self._key_of(locator)
        dst = f"{src}.corrupt-{int(time.time())}"
        try:
            _with_retry(lambda: fs.mv(src, dst))
        except Exception as exc:  # noqa: BLE001
            logger.warning("S3 quarantine mv failed for %s: %s", src, exc)

    def _list_symbol_dirs(self, exchange: str) -> List[str]:
        fs = self._fs()
        prefix = f"{self.bucket}/ohlcv/{exchange}/"
        try:
            entries = _with_retry(lambda: fs.ls(prefix, detail=False))
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not list %s: %s", prefix, exc)
            return []
        return [entry.rstrip("/").rsplit("/", 1)[-1] for entry in entries]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _slice_and_index(
    df: pd.DataFrame,
    from_ts: Optional[int],
    to_ts: Optional[int],
) -> pd.DataFrame:
    """Apply [from_ts, to_ts] slice and convert ts → DatetimeIndex."""
    if df.empty:
        return df.set_index(pd.DatetimeIndex([], name="timestamp"))
    if from_ts is not None:
        df = df[df["timestamp"] >= int(from_ts)]
    if to_ts is not None:
        df = df[df["timestamp"] <= int(to_ts)]
    df = df.sort_values("timestamp").reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(
        df["timestamp"].values.astype("int64"), unit="ms"
    )
    return df.set_index("timestamp", drop=True)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


_DEFAULT_BACKEND = "local_parquet"


def get_ohlcv_store(
    backend: Optional[str] = None,
    *,
    data_dir: str = "data",
    s3_bucket: Optional[str] = None,
) -> OhlcvStore:
    """Return an OHLCV store instance based on backend / env vars.

    Resolution order:
      1. ``backend`` argument (if given)
      2. ``DATA_STORE`` environment variable
      3. ``local_parquet`` (default)

    For S3:
      * ``s3_bucket`` argument (if given)
      * ``S3_BUCKET`` environment variable
    """
    chosen = (backend or os.environ.get("DATA_STORE") or _DEFAULT_BACKEND).lower()

    if chosen in ("local", "local_parquet", "parquet"):
        return LocalParquetStore(root=data_dir)
    if chosen == "s3":
        bucket = s3_bucket or os.environ.get("S3_BUCKET")
        if not bucket:
            raise ValueError(
                "DATA_STORE=s3 but no bucket given via S3_BUCKET env var "
                "or s3_bucket argument."
            )
        return S3ParquetStore(bucket=bucket)
    raise ValueError(
        f"Unknown DATA_STORE backend: '{chosen}'. "
        "Expected one of: local_parquet, s3"
    )


__all__ = [
    "OhlcvStore",
    "LocalParquetStore",
    "S3ParquetStore",
    "get_ohlcv_store",
    "OHLCV_COLUMNS_6",
    "OHLCV_COLUMNS_7",
]
