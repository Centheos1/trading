"""OHLCV Parquet storage with local-filesystem and S3 backends.

Storage layout (both backends):
    {root}/ohlcv/{exchange}/{symbol}/{timeframe}.parquet

Where ``{root}`` is:
    * Local:   ``./data``                     (LocalParquetStore)
    * S3:      ``s3://{bucket}``              (S3ParquetStore)

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
    Parquet files are immutable once written, so ``append`` performs a
    read-modify-write of the symbol/timeframe file.  Duplicate timestamps
    are removed (last-write-wins) before re-writing.  For OHLCV at 1-minute
    granularity each file is well below 100MB, so RMW is acceptable.
"""

from __future__ import annotations

import logging
import os
import threading
from abc import ABC, abstractmethod
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------


class OhlcvStore(ABC):
    """Abstract OHLCV store. Backend implementations live below."""

    @abstractmethod
    def path_for(self, exchange: str, symbol: str, timeframe: str) -> str:
        """Return the backend-native URI/path for a symbol's Parquet file."""

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
        Returns an empty DataFrame when the file does not exist.
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
        """Append ``rows`` to the symbol's Parquet file.

        ``rows`` is an iterable of 6-tuples ``(ts_ms, o, h, l, c, v)`` or
        7-tuples (with trailing ``spread``).  Duplicate timestamps are
        de-duplicated last-write-wins.  Returns the number of new rows
        actually written (i.e. rows whose ts was not already present).
        """

    @abstractmethod
    def list_symbols(
        self, exchange: str, timeframe: str = "1m"
    ) -> List[str]:
        ...


# ---------------------------------------------------------------------------
# Local-filesystem backend
# ---------------------------------------------------------------------------


class LocalParquetStore(OhlcvStore):
    """Reads/writes Parquet files under ``{root}/ohlcv/...``."""

    def __init__(self, root: str = "data") -> None:
        self.root = root

    def path_for(self, exchange: str, symbol: str, timeframe: str) -> str:
        return os.path.join(
            self.root, "ohlcv", exchange, symbol, f"{timeframe}.parquet"
        )

    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        return os.path.exists(self.path_for(exchange, symbol, timeframe))

    def read(
        self,
        exchange: str,
        symbol: str,
        timeframe: str = "1m",
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> pd.DataFrame:
        path = self.path_for(exchange, symbol, timeframe)
        if not os.path.exists(path):
            return pd.DataFrame(columns=OHLCV_COLUMNS_6).set_index(
                pd.DatetimeIndex([], name="timestamp")
            )
        df = pd.read_parquet(path)
        return _slice_and_index(df, from_ts, to_ts)

    def get_last_timestamp(
        self, exchange: str, symbol: str, timeframe: str = "1m"
    ) -> Optional[int]:
        path = self.path_for(exchange, symbol, timeframe)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_parquet(path, columns=["timestamp"])
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return None
        if df.empty:
            return None
        return int(df["timestamp"].max())

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
        path = self.path_for(exchange, symbol, timeframe)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock_for(path):
            if os.path.exists(path):
                existing = pd.read_parquet(path)
                n_before = len(existing)
                combined = pd.concat([existing, new_df], ignore_index=True)
                combined = combined.drop_duplicates(
                    subset=["timestamp"], keep="last"
                ).sort_values("timestamp").reset_index(drop=True)
                n_written = len(combined) - n_before
                combined.to_parquet(path, index=False, compression="snappy")
            else:
                deduped = new_df.drop_duplicates(
                    subset=["timestamp"], keep="last"
                ).sort_values("timestamp").reset_index(drop=True)
                deduped.to_parquet(path, index=False, compression="snappy")
                n_written = len(deduped)
        return int(max(n_written, 0))

    def list_symbols(
        self, exchange: str, timeframe: str = "1m"
    ) -> List[str]:
        base = os.path.join(self.root, "ohlcv", exchange)
        if not os.path.isdir(base):
            return []
        result: List[str] = []
        for entry in os.listdir(base):
            f = os.path.join(base, entry, f"{timeframe}.parquet")
            if os.path.isfile(f):
                result.append(entry)
        return sorted(result)


# ---------------------------------------------------------------------------
# S3 backend
# ---------------------------------------------------------------------------


class S3ParquetStore(OhlcvStore):
    """Reads/writes Parquet files in ``s3://{bucket}/ohlcv/...``.

    Uses pyarrow + s3fs.  Credentials are picked up from the standard
    AWS credential chain (env vars, ``~/.aws/credentials``, or — on EC2 —
    the instance profile).
    """

    def __init__(self, bucket: str) -> None:
        if not bucket:
            raise ValueError("S3ParquetStore requires a bucket name")
        self.bucket = bucket
        try:
            import s3fs  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "S3ParquetStore requires the s3fs package. "
                "Install via: pip install s3fs"
            ) from exc

    def _fs(self):
        import s3fs
        return s3fs.S3FileSystem()

    def path_for(self, exchange: str, symbol: str, timeframe: str) -> str:
        return f"s3://{self.bucket}/ohlcv/{exchange}/{symbol}/{timeframe}.parquet"

    def _key(self, exchange: str, symbol: str, timeframe: str) -> str:
        return f"{self.bucket}/ohlcv/{exchange}/{symbol}/{timeframe}.parquet"

    def exists(self, exchange: str, symbol: str, timeframe: str) -> bool:
        return self._fs().exists(self._key(exchange, symbol, timeframe))

    def read(
        self,
        exchange: str,
        symbol: str,
        timeframe: str = "1m",
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
    ) -> pd.DataFrame:
        path = self.path_for(exchange, symbol, timeframe)
        if not self.exists(exchange, symbol, timeframe):
            return pd.DataFrame(columns=OHLCV_COLUMNS_6).set_index(
                pd.DatetimeIndex([], name="timestamp")
            )
        df = pd.read_parquet(path, storage_options={"anon": False})
        return _slice_and_index(df, from_ts, to_ts)

    def get_last_timestamp(
        self, exchange: str, symbol: str, timeframe: str = "1m"
    ) -> Optional[int]:
        if not self.exists(exchange, symbol, timeframe):
            return None
        path = self.path_for(exchange, symbol, timeframe)
        try:
            df = pd.read_parquet(
                path, columns=["timestamp"], storage_options={"anon": False}
            )
        except Exception as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return None
        if df.empty:
            return None
        return int(df["timestamp"].max())

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
        path = self.path_for(exchange, symbol, timeframe)
        with _lock_for(path):
            if self.exists(exchange, symbol, timeframe):
                existing = pd.read_parquet(path, storage_options={"anon": False})
                n_before = len(existing)
                combined = pd.concat([existing, new_df], ignore_index=True)
                combined = combined.drop_duplicates(
                    subset=["timestamp"], keep="last"
                ).sort_values("timestamp").reset_index(drop=True)
                n_written = len(combined) - n_before
                combined.to_parquet(
                    path,
                    index=False,
                    compression="snappy",
                    storage_options={"anon": False},
                )
            else:
                deduped = new_df.drop_duplicates(
                    subset=["timestamp"], keep="last"
                ).sort_values("timestamp").reset_index(drop=True)
                deduped.to_parquet(
                    path,
                    index=False,
                    compression="snappy",
                    storage_options={"anon": False},
                )
                n_written = len(deduped)
        return int(max(n_written, 0))

    def list_symbols(
        self, exchange: str, timeframe: str = "1m"
    ) -> List[str]:
        fs = self._fs()
        prefix = f"{self.bucket}/ohlcv/{exchange}/"
        try:
            entries = fs.ls(prefix, detail=False)
        except FileNotFoundError:
            return []
        symbols: List[str] = []
        for entry in entries:
            symbol = entry.rstrip("/").rsplit("/", 1)[-1]
            target = f"{entry.rstrip('/')}/{timeframe}.parquet"
            if fs.exists(target):
                symbols.append(symbol)
        return sorted(symbols)


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
