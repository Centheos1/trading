"""Tick data Parquet storage — date-partitioned, durable mirror of the
HDF5 :class:`TickStore`.

Why this exists
---------------
The C++ :class:`TickStore` writes one growing HDF5 file per symbol. That
single file is the canonical store but it has two operational problems:

1. **Fragility** — libhdf5 keeps root-group metadata in its page cache.
   If the process is killed during a write (disk full, OOM, host crash)
   the on-disk superblock can be left inconsistent and the *entire file*
   becomes unreadable (``bad object header version number``).
2. **Coarse granularity** — to read any window of data the consumer must
   download the whole file, which can be many GB after a few weeks.

:class:`TickParquetStore` solves both by periodically draining new rows
from the open HDF5 file into per-day Parquet files. Each daily file is
immutable once the day rolls over, and consumers can fetch only the
dates they need. If the HDF5 file is later corrupted, at worst we lose
the rows since the last successful flush (default: 15 minutes).

Storage layout::

    {root}/
      {exchange}/
        {SYMBOL}/
          trades/           YYYY-MM-DD.parquet
          depth_snapshots/  YYYY-MM-DD.parquet
          depth_updates/    YYYY-MM-DD.parquet
      .watermarks.json      {"binance.BTCUSDT.trades": 12345678, ...}

Schema (matches :func:`notebooks.utils.load_ticks` output)::

    trades            timestamp:int64 ms, price:float64, quantity:float64,
                      is_buyer_maker:bool
    depth_snapshots   timestamp:int64 ms, side:int8 (0=bid 1=ask),
                      price:float64, quantity:float64
    depth_updates     same as depth_snapshots
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DATASETS: Tuple[str, ...] = ("trades", "depth_snapshots", "depth_updates")

# Rows to leave at the tail of each HDF5 dataset on every flush so we never
# read across a half-written C++ append. The C++ TickStore buffers up to
# 10 000 trades before flushing; depth writes go straight to disk per
# message but the network can have a few hundred in flight.
_SAFETY_MARGIN: Dict[str, int] = {
    "trades": 10_000,
    "depth_snapshots": 500,
    "depth_updates": 500,
}

# Parquet column order matches load_ticks() so the DataFrames are
# interchangeable between the HDF5 and Parquet paths.
_TRADES_COLS = ["timestamp", "price", "quantity", "is_buyer_maker"]
_DEPTH_COLS = ["timestamp", "side", "price", "quantity"]

# Number of consecutive failed reads of the *same* HDF5 row range before we
# give up and skip past it (advancing the watermark). Below this threshold a
# read failure is treated as transient — the watermark is left untouched so
# the next flush retries the same range. This prevents a momentary read error
# (writer contention, partial tail write) from permanently dropping rows from
# the durable Parquet mirror, while still guaranteeing forward progress past a
# genuinely unreadable range.
_MAX_READ_FAILURES: int = 5


def _columns_for(dataset: str) -> List[str]:
    return _TRADES_COLS if dataset == "trades" else _DEPTH_COLS


# ---------------------------------------------------------------------------
# Watermark persistence
# ---------------------------------------------------------------------------


class _Watermarks:
    """Persisted ``{exchange}.{SYMBOL}.{dataset}`` -> last-flushed HDF5 row.

    Stored as a single JSON file at the store root. Idempotent: a crash
    between flush and watermark write at worst causes the next flush to
    re-read the same rows; per-day Parquet writes de-duplicate via the
    HDF5 row range, so no duplicates land on disk.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: Dict[str, int] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text() or "{}")
            except Exception as exc:
                logger.warning("Watermark file unreadable, starting fresh: %s", exc)
                self._data = {}

    @staticmethod
    def _key(exchange: str, symbol: str, dataset: str) -> str:
        return f"{exchange}.{symbol.upper()}.{dataset}"

    def get(self, exchange: str, symbol: str, dataset: str) -> int:
        return int(self._data.get(self._key(exchange, symbol, dataset), 0))

    def set(self, exchange: str, symbol: str, dataset: str, row: int) -> None:
        self._data[self._key(exchange, symbol, dataset)] = int(row)

    def flush(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        os.replace(tmp, self._path)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TickParquetStore:
    """Date-partitioned Parquet mirror of the HDF5 tick store.

    Thread-safety: a single instance is intended to be used by one
    flusher thread per process. The watermark file is not concurrency
    safe across processes — run only one ``flush_from_h5`` per symbol.
    """

    def __init__(self, root: str = "data/ticks") -> None:
        self.root = Path(root)
        self._watermarks = _Watermarks(self.root / ".watermarks.json")
        # Consecutive HDF5 read-failure counts keyed by
        # ``{exchange}.{SYMBOL}.{dataset}``. Reset on any successful read.
        self._read_failures: Dict[str, int] = {}

    # -- paths ----------------------------------------------------------

    def _dataset_dir(self, exchange: str, symbol: str, dataset: str) -> Path:
        return self.root / exchange / symbol.upper() / dataset

    def _day_file(
        self, exchange: str, symbol: str, dataset: str, day: str
    ) -> Path:
        return self._dataset_dir(exchange, symbol, dataset) / f"{day}.parquet"

    # -- flush ----------------------------------------------------------

    def flush_from_h5(
        self,
        h5_path: str | os.PathLike,
        symbol: str,
        exchange: str = "binance",
    ) -> Dict[str, int]:
        """Drain new rows from each dataset into daily Parquet files.

        Returns a dict ``{dataset: rows_written}`` for diagnostics.
        Safe to call while the C++ ``TickStore`` is writing the file —
        opens with ``locking=False`` and stops short of the unflushed
        tail via :data:`_SAFETY_MARGIN`.
        """
        import h5py

        h5_path = Path(h5_path)
        if not h5_path.exists():
            logger.debug("HDF5 file %s missing — nothing to flush", h5_path)
            return {d: 0 for d in DATASETS}

        symbol = symbol.upper()
        written: Dict[str, int] = {d: 0 for d in DATASETS}

        try:
            with h5py.File(str(h5_path), "r", locking=False) as f:
                if symbol not in f:
                    logger.debug(
                        "Symbol %s not yet in %s — nothing to flush",
                        symbol,
                        h5_path,
                    )
                    return written
                grp = f[symbol]
                for dataset in DATASETS:
                    if dataset not in grp:
                        continue
                    n = self._flush_dataset(
                        grp[dataset], exchange, symbol, dataset
                    )
                    written[dataset] = n
        except OSError as exc:
            logger.warning("Could not open %s for Parquet flush: %s", h5_path, exc)
            return written

        self._watermarks.flush()
        total = sum(written.values())
        if total:
            logger.info(
                "Parquet flush %s/%s: %s",
                exchange,
                symbol,
                ", ".join(f"{k}={v}" for k, v in written.items() if v),
            )
        return written

    def _flush_dataset(
        self, dataset_obj, exchange: str, symbol: str, dataset: str
    ) -> int:
        n_rows = int(dataset_obj.shape[0])
        margin = _SAFETY_MARGIN.get(dataset, 0)
        end = max(0, n_rows - margin)
        start = self._watermarks.get(exchange, symbol, dataset)
        if start >= end:
            return 0

        fail_key = f"{exchange}.{symbol.upper()}.{dataset}"
        try:
            arr = np.asarray(dataset_obj[start:end])
        except OSError as exc:
            fails = self._read_failures.get(fail_key, 0) + 1
            self._read_failures[fail_key] = fails
            if fails < _MAX_READ_FAILURES:
                # Treat as transient: do NOT advance the watermark, so the
                # next flush retries the same range. Losing rows from the
                # durable mirror on a momentary error would defeat its
                # purpose.
                logger.warning(
                    "%s/%s/%s rows %d:%d HDF5 read failed (attempt %d/%d, "
                    "will retry next flush): %s",
                    exchange, symbol, dataset, start, end,
                    fails, _MAX_READ_FAILURES, exc,
                )
                return 0
            # Persistent failure: skip past the poisoned range as a last
            # resort so the flusher makes forward progress. Logged at ERROR
            # because these rows are dropped from Parquet (the HDF5 may still
            # hold them for offline recovery).
            logger.error(
                "%s/%s/%s rows %d:%d unreadable after %d attempts — SKIPPING "
                "(data lost from Parquet mirror; HDF5 may still hold it): %s",
                exchange, symbol, dataset, start, end, fails, exc,
            )
            self._read_failures[fail_key] = 0
            self._watermarks.set(exchange, symbol, dataset, end)
            return 0

        # Successful read — clear any prior transient-failure streak.
        if self._read_failures.get(fail_key):
            self._read_failures[fail_key] = 0

        if arr.size == 0:
            self._watermarks.set(exchange, symbol, dataset, end)
            return 0

        df = self._array_to_df(arr, dataset)
        if df.empty:
            self._watermarks.set(exchange, symbol, dataset, end)
            return 0

        written = self._append_by_day(df, exchange, symbol, dataset)
        self._watermarks.set(exchange, symbol, dataset, end)
        return written

    @staticmethod
    def _array_to_df(arr: np.ndarray, dataset: str) -> pd.DataFrame:
        cols = _columns_for(dataset)
        if arr.ndim != 2 or arr.shape[1] != len(cols):
            logger.warning(
                "Unexpected shape %s for %s — expected (N,%d), skipping",
                arr.shape,
                dataset,
                len(cols),
            )
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame(arr, columns=cols)
        df["timestamp"] = df["timestamp"].astype("int64")
        df = df[df["timestamp"] > 0]
        if dataset == "trades":
            df["price"] = df["price"].astype("float64")
            df["quantity"] = df["quantity"].astype("float64")
            df["is_buyer_maker"] = df["is_buyer_maker"].astype(bool)
        else:
            df["side"] = df["side"].astype("int8")
            df["price"] = df["price"].astype("float64")
            df["quantity"] = df["quantity"].astype("float64")
        return df.reset_index(drop=True)

    def _append_by_day(
        self, df: pd.DataFrame, exchange: str, symbol: str, dataset: str
    ) -> int:
        # Bucket the slice by UTC date so each day's file accumulates
        # only its own rows. Vectorised conversion ms → date string.
        days = pd.to_datetime(df["timestamp"], unit="ms", utc=True).dt.strftime(
            "%Y-%m-%d"
        )
        total = 0
        for day, idx in df.groupby(days).groups.items():
            chunk = df.loc[idx].reset_index(drop=True)
            path = self._day_file(exchange, symbol, dataset, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._append_parquet(path, chunk, dataset)
            total += len(chunk)
        return total

    @staticmethod
    def _append_parquet(
        path: Path, chunk: pd.DataFrame, dataset: str
    ) -> None:
        """Append rows to a daily Parquet file.

        Parquet is immutable so this is a read-modify-write of the day's
        file. Daily files stay small (a few hundred MB at most for one
        symbol of trades) so RMW is cheap.
        """
        import pyarrow as pa
        import pyarrow.parquet as pq

        if path.exists():
            try:
                existing = pq.read_table(str(path)).to_pandas()
                combined = pd.concat([existing, chunk], ignore_index=True)
            except Exception as exc:
                # The existing day file is unreadable/corrupt. Do NOT silently
                # overwrite it (that would discard whatever rows it still
                # holds). Quarantine it next to the live file so it can be
                # recovered offline, then start a fresh file from this chunk.
                quarantine = path.with_suffix(
                    path.suffix
                    + f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                )
                try:
                    os.replace(path, quarantine)
                    logger.error(
                        "Corrupt Parquet %s (%s) — quarantined to %s; "
                        "starting a fresh file from the new chunk",
                        path, exc, quarantine,
                    )
                except OSError as mv_exc:
                    logger.error(
                        "Corrupt Parquet %s (%s) and quarantine move failed "
                        "(%s) — writing new chunk only; existing rows lost",
                        path, exc, mv_exc,
                    )
                combined = chunk
        else:
            combined = chunk

        # De-duplicate within a day in case a previous flush wrote
        # overlapping rows (idempotent watermark recovery).
        if dataset == "trades":
            subset = ["timestamp", "price", "quantity", "is_buyer_maker"]
        else:
            subset = ["timestamp", "side", "price", "quantity"]
        combined = combined.drop_duplicates(subset=subset, keep="last")
        combined = combined.sort_values("timestamp").reset_index(drop=True)

        table = pa.Table.from_pandas(combined, preserve_index=False)
        tmp = path.with_suffix(".tmp")
        pq.write_table(table, str(tmp), compression="snappy")
        os.replace(tmp, path)

    # -- read -----------------------------------------------------------

    def read(
        self,
        symbol: str,
        dataset: str = "trades",
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        exchange: str = "binance",
    ) -> pd.DataFrame:
        """Read one or more daily Parquet files for ``symbol`` / ``dataset``.

        ``from_date`` / ``to_date`` are inclusive ``YYYY-MM-DD`` strings.
        Returns an empty DataFrame (with correct columns) if no files
        match.  Output schema matches :func:`notebooks.utils.load_ticks`.
        """
        if dataset not in DATASETS:
            raise ValueError(f"Unknown dataset {dataset!r}; expected one of {DATASETS}")

        cols = _columns_for(dataset)
        files = self._files_in_range(exchange, symbol, dataset, from_date, to_date)
        if not files:
            return pd.DataFrame(columns=cols)

        import pyarrow.parquet as pq

        frames: List[pd.DataFrame] = []
        for f in files:
            try:
                frames.append(pq.read_table(str(f)).to_pandas())
            except Exception as exc:
                logger.warning("Skipping unreadable parquet %s: %s", f, exc)
        if not frames:
            return pd.DataFrame(columns=cols)
        df = pd.concat(frames, ignore_index=True)
        df = df.sort_values("timestamp").reset_index(drop=True)
        return df

    def _files_in_range(
        self,
        exchange: str,
        symbol: str,
        dataset: str,
        from_date: Optional[str],
        to_date: Optional[str],
    ) -> List[Path]:
        directory = self._dataset_dir(exchange, symbol, dataset)
        if not directory.is_dir():
            return []
        all_files = sorted(directory.glob("*.parquet"))
        if from_date is None and to_date is None:
            return all_files
        kept: List[Path] = []
        for f in all_files:
            day = f.stem  # YYYY-MM-DD
            if from_date is not None and day < from_date:
                continue
            if to_date is not None and day > to_date:
                continue
            kept.append(f)
        return kept

    def list_days(
        self, symbol: str, dataset: str = "trades", *, exchange: str = "binance"
    ) -> List[str]:
        directory = self._dataset_dir(exchange, symbol, dataset)
        if not directory.is_dir():
            return []
        return sorted(p.stem for p in directory.glob("*.parquet"))


__all__ = ["TickParquetStore", "DATASETS"]
