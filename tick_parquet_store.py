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
import threading
from collections import defaultdict
from dataclasses import dataclass, field
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

# Maximum HDF5 rows to materialise in one read. A normal 15-minute flush moves
# far fewer rows than this, so steady-state behaviour is unchanged — but if the
# mirror has fallen far behind (a stalled flusher, or a manual --backfill-parquet
# over many days) the unflushed range can be hundreds of millions of rows.
# Reading that in a single np.asarray() call tried to allocate 22.8 GiB during
# the 2026-06 recovery and crashed with MemoryError. Draining in bounded chunks
# keeps peak read memory at ~CHUNK × ncols × 8 bytes (≈64 MB for trades), so the
# backfill makes steady forward progress on a small instance. The watermark is
# advanced and persisted after each chunk so a crash mid-backfill resumes from
# the last completed chunk rather than restarting.
_FLUSH_CHUNK_ROWS: int = 2_000_000


def _columns_for(dataset: str) -> List[str]:
    return _TRADES_COLS if dataset == "trades" else _DEPTH_COLS


def _arrow_schema(dataset: str):
    """Canonical pyarrow schema for a dataset (matches the Parquet columns)."""
    import pyarrow as pa

    if dataset == "trades":
        return pa.schema([
            ("timestamp", pa.int64()),
            ("price", pa.float64()),
            ("quantity", pa.float64()),
            ("is_buyer_maker", pa.bool_()),
        ])
    return pa.schema([
        ("timestamp", pa.int64()),
        ("side", pa.int8()),
        ("price", pa.float64()),
        ("quantity", pa.float64()),
    ])


# ---------------------------------------------------------------------------
# Pipeline health evaluation
# ---------------------------------------------------------------------------
#
# The 2026-06 freeze was invisible because nothing asserted the mirror was
# *advancing* — the container was "healthy", the HDF5 grew, and S3 had files,
# yet no new Parquet day had been written for two weeks. This pure function is
# the guardrail: given the current HDF5 row counts, the stored watermarks, and
# the newest Parquet day on disk, it decides whether the pipeline is producing
# RECENT data. It does no I/O so it is trivially unit-testable; the collector
# and the host cron wrapper feed it real measurements.


@dataclass(frozen=True)
class PipelineHealth:
    """Result of :func:`evaluate_pipeline_health`.

    ``ok`` is True only when there are zero issues. ``issues`` are
    operator-actionable problem strings (logged at ERROR / alerted on);
    ``summary`` are per-dataset status lines for the INFO log.
    """

    ok: bool
    issues: List[str] = field(default_factory=list)
    summary: List[str] = field(default_factory=list)


def evaluate_pipeline_health(
    symbol: str,
    *,
    exchange: str = "binance",
    n_rows: Dict[str, Optional[int]],
    watermarks: Dict[str, int],
    latest_parquet_day: Dict[str, Optional[str]],
    now: datetime,
    max_staleness_hours: float = 2.0,
    datasets: Iterable[str] = DATASETS,
) -> PipelineHealth:
    """Decide whether the HDF5 → Parquet mirror is healthy and current.

    Parameters
    ----------
    n_rows
        ``dataset -> current HDF5 row count`` (``None`` if the HDF5 file or the
        dataset within it is absent — e.g. collector not yet writing it).
    watermarks
        ``dataset -> last-flushed HDF5 row index`` from ``.watermarks.json``.
    latest_parquet_day
        ``dataset -> "YYYY-MM-DD"`` of the newest local Parquet day-file, or
        ``None`` if the mirror has never written that dataset.
    now
        Current UTC time (injected so the check is deterministic / testable).
    max_staleness_hours
        Grace period just after 00:00 UTC during which yesterday's day-file is
        still acceptable (today's file appears on the first flush of the day).

    Detects the failure modes that have actually occurred:
      * stranded watermark above the live row count (rotation not yet healed);
      * no Parquet day-file at all (mirror never flushed);
      * newest Parquet day older than today (flush or S3 sync stalled — the
        2026-06 freeze).
    """
    sym = symbol.upper()
    issues: List[str] = []
    summary: List[str] = []

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_into_day = (now - midnight).total_seconds() / 3600.0
    today = now.date()

    for d in datasets:
        nr = n_rows.get(d)
        wm = int(watermarks.get(d, 0))
        day = latest_parquet_day.get(d)
        summary.append(
            f"{exchange}/{sym}/{d}: rows="
            f"{'?' if nr is None else nr} watermark={wm} "
            f"latest_parquet={day or 'none'}"
        )

        if nr is None:
            issues.append(
                f"{exchange}/{sym}/{d}: HDF5 dataset absent — collector not "
                f"writing {d} (or the HDF5 file is missing/unreadable)"
            )
            continue

        if wm > nr:
            issues.append(
                f"{exchange}/{sym}/{d}: watermark {wm} exceeds HDF5 row count "
                f"{nr} — rotation/reset not yet healed (the next flush should "
                f"reset it to 0; if it persists, the deployed code predates the "
                f"rotation self-heal)"
            )

        if day is None:
            issues.append(
                f"{exchange}/{sym}/{d}: no Parquet day-files on disk — the "
                f"mirror has never flushed this dataset"
            )
            continue

        try:
            day_date = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            issues.append(
                f"{exchange}/{sym}/{d}: unparseable Parquet day-file name "
                f"{day!r}"
            )
            continue

        age_days = (today - day_date).days
        stale = age_days >= 2 or (
            age_days == 1 and hours_into_day > max_staleness_hours
        )
        if stale:
            issues.append(
                f"{exchange}/{sym}/{d}: newest Parquet day {day} is stale "
                f"(age {age_days}d, {hours_into_day:.1f}h into the UTC day) — "
                f"the flush thread or the S3 sync has stalled"
            )

    return PipelineHealth(ok=not issues, issues=issues, summary=summary)


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

    # -- introspection (for health checks / monitoring) -----------------

    def watermark(self, exchange: str, symbol: str, dataset: str) -> int:
        """Last-flushed HDF5 row index for this dataset (0 if never flushed)."""
        return self._watermarks.get(exchange, symbol, dataset)

    def latest_parquet_date(
        self, exchange: str, symbol: str, dataset: str
    ) -> Optional[str]:
        """Newest ``YYYY-MM-DD`` day-file on disk, or ``None`` if none exist.

        Reflects only the local mirror (what the flush thread has written),
        independent of whether the S3 sync has run.
        """
        ddir = self._dataset_dir(exchange, symbol, dataset)
        if not ddir.is_dir():
            return None
        days: List[str] = []
        for p in ddir.glob("*.parquet"):
            try:
                datetime.strptime(p.stem, "%Y-%m-%d")
            except ValueError:
                continue
            days.append(p.stem)
        return max(days) if days else None

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

        # Rotation / replacement detection. Within the lifetime of one HDF5
        # file the row count only ever grows (the C++ TickStore appends), so a
        # stored watermark beyond the current row count means the live file was
        # replaced with a fresh one starting at row 0 — e.g. the --max-h5-gb
        # rotation in collect_ticks (archive + delete + restart), a redeploy,
        # or a fresh data volume. Without this reset the watermark stays pinned
        # at the pre-rotation row index, `start >= end` holds forever, and the
        # Parquet mirror silently freezes (the 2026-06-01 stall). Re-flush from
        # row 0; per-day de-duplication keeps the re-read idempotent.
        if start > n_rows:
            logger.warning(
                "%s/%s/%s watermark %d exceeds current row count %d — HDF5 "
                "rotated/reset; re-flushing from row 0",
                exchange, symbol, dataset, start, n_rows,
            )
            start = 0
            self._watermarks.set(exchange, symbol, dataset, 0)

        if start >= end:
            return 0

        # Drain [start, end) in bounded chunks. Reading the whole range at once
        # is fine for a steady-state flush (a few thousand rows) but blows up
        # memory when the mirror has fallen days behind — see _FLUSH_CHUNK_ROWS.
        fail_key = f"{exchange}.{symbol.upper()}.{dataset}"
        written = 0
        chunk_start = start
        while chunk_start < end:
            chunk_end = min(chunk_start + _FLUSH_CHUNK_ROWS, end)
            try:
                arr = np.asarray(dataset_obj[chunk_start:chunk_end])
            except (OSError, MemoryError) as exc:
                fails = self._read_failures.get(fail_key, 0) + 1
                self._read_failures[fail_key] = fails
                if fails < _MAX_READ_FAILURES:
                    # Treat as transient: do NOT advance the watermark past this
                    # chunk and stop here so the next flush retries the same
                    # range. Losing rows from the durable mirror on a momentary
                    # error would defeat its purpose.
                    logger.warning(
                        "%s/%s/%s rows %d:%d HDF5 read failed (attempt %d/%d, "
                        "will retry next flush): %s",
                        exchange, symbol, dataset, chunk_start, chunk_end,
                        fails, _MAX_READ_FAILURES, exc,
                    )
                    return written
                # Persistent failure: skip past the poisoned chunk as a last
                # resort so the flusher makes forward progress, then continue
                # with the next chunk (later rows may still be readable). Logged
                # at ERROR because these rows are dropped from Parquet (the HDF5
                # may still hold them for offline recovery).
                logger.error(
                    "%s/%s/%s rows %d:%d unreadable after %d attempts — "
                    "SKIPPING (data lost from Parquet mirror; HDF5 may still "
                    "hold it): %s",
                    exchange, symbol, dataset, chunk_start, chunk_end,
                    fails, exc,
                )
                self._read_failures[fail_key] = 0
                self._advance_watermark(exchange, symbol, dataset, chunk_end)
                chunk_start = chunk_end
                continue

            # Successful read — clear any prior transient-failure streak.
            if self._read_failures.get(fail_key):
                self._read_failures[fail_key] = 0

            if arr.size:
                df = self._array_to_df(arr, dataset)
                if not df.empty:
                    written += self._append_by_day(df, exchange, symbol, dataset)

            # Persist progress after every chunk so a crash mid-backfill resumes
            # from here; per-day de-duplication keeps any re-read idempotent.
            self._advance_watermark(exchange, symbol, dataset, chunk_end)
            chunk_start = chunk_end

        return written

    def _advance_watermark(
        self, exchange: str, symbol: str, dataset: str, row: int
    ) -> None:
        self._watermarks.set(exchange, symbol, dataset, row)
        self._watermarks.flush()

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

    def read_day_table(self, exchange: str, symbol: str, dataset: str, day: str):
        """Read one day's Parquet file as a pyarrow ``Table`` (empty if absent).

        A corrupt file is **quarantined** (renamed aside) rather than raising,
        so the live mirror can recover by starting that day fresh instead of
        crash-looping. Used by :class:`LiveParquetMirror` to reload the
        in-memory day accumulator once after a restart. Arrow-native to avoid a
        pandas round-trip on the hot path.
        """
        if dataset not in DATASETS:
            raise ValueError(f"Unknown dataset {dataset!r}; expected one of {DATASETS}")
        import pyarrow as pa
        import pyarrow.parquet as pq

        schema = _arrow_schema(dataset)
        path = self._day_file(exchange, symbol, dataset, day)
        if not path.exists():
            return schema.empty_table()
        try:
            return pq.read_table(str(path))
        except Exception as exc:
            quarantine = path.with_suffix(
                path.suffix
                + f".corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            )
            try:
                os.replace(path, quarantine)
                logger.error(
                    "Corrupt Parquet %s (%s) — quarantined to %s; the mirror "
                    "will start this day fresh", path, exc, quarantine,
                )
            except OSError as mv_exc:
                logger.error(
                    "Corrupt Parquet %s (%s) and quarantine move failed (%s)",
                    path, exc, mv_exc,
                )
            return schema.empty_table()

    def write_day_table(
        self, exchange: str, symbol: str, dataset: str, day: str, table
    ) -> int:
        """Atomically (over)write one day's Parquet file from a pyarrow ``Table``.

        The durable-mirror hot path. The caller (:class:`LiveParquetMirror`)
        holds the full day as a chunked Arrow table, so this is a bounded write
        — **no read, no RMW** — unlike the backfill path (:meth:`flush_from_h5`
        → :meth:`_append_by_day`). Written via temp-file + ``os.replace`` so a
        crash mid-write can never leave a torn day file (a reader sees either
        the old or the new complete file). Rows need not be
        pre-sorted/de-duplicated; :meth:`read` does both.
        """
        if dataset not in DATASETS:
            raise ValueError(f"Unknown dataset {dataset!r}; expected one of {DATASETS}")
        import pyarrow.parquet as pq

        path = self._day_file(exchange, symbol, dataset, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".parquet.tmp-{os.getpid()}")
        pq.write_table(table, str(tmp), compression="snappy")
        os.replace(tmp, path)
        return table.num_rows

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
        # De-duplicate at the read boundary. The live mirror's writer is
        # append-only (no per-flush dedup, for bounded cost), and forward-only
        # feeds don't normally repeat — but a WS reconnect or an overlapping
        # backfill could, so collapse exact duplicates here so consumers always
        # see clean, sorted data regardless of how it was written.
        df = df.drop_duplicates(subset=cols, keep="last")
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


# ---------------------------------------------------------------------------
# Live mirror — fed directly from the feed (no HDF5 on the durability path)
# ---------------------------------------------------------------------------


class LiveParquetMirror:
    """Durable Parquet mirror fed DIRECTLY from the live tick feed.

    Why this exists
    ---------------
    The original mirror tailed the C++ ``TickStore`` HDF5 file with a
    separate reader (:meth:`TickParquetStore.flush_from_h5`). That coupled
    durability to a single, ever-growing, corruption-prone file: when the
    HDF5 superblock/B-tree corrupted (EOA overflow under sustained append
    load) the reader silently returned empty/garbage, the watermark advanced
    without writing, and the Parquet mirror froze for days with no alert
    (the 2026-06 freezes — see docs/DEPLOYMENT.md).

    This class removes that dependency. The collector hands every trade and
    depth message to :meth:`add_trade` / :meth:`add_depth` as it arrives off
    the WebSocket. A periodic :meth:`flush` (driven by the collector's flush
    thread) moves the buffered rows into an in-memory **per-day accumulator**
    and atomically (over)writes that day's single Parquet file. HDF5 is no
    longer on the durability path — it can corrupt, rotate, or be deleted
    without affecting the mirror.

    Write-cost design (the second 2026-06 hardening)
    ------------------------------------------------
    The naive approach — read-modify-write the day file every flush — re-reads,
    re-dedups and re-sorts the *entire* day on every flush, so its cost grows
    through the day. At a 60 s cadence on a busy ``depth_updates`` stream that
    is a recurring multi-hundred-MB memory spike, an OOM risk on a small box.
    Instead we keep the current day resident as a **chunked pyarrow table** and
    overwrite the file from it each flush: appending is ``pa.concat_tables``
    (adds a chunk, no full copy) and writing is a bounded snappy serialise with
    **no per-flush read, dedup or sort**. (A pandas accumulator was measured at
    ~2.6 GB peak RSS for a 6 M-row day from ``pd.concat`` reallocation; the Arrow
    accumulator holds the same day at ~0.3 GB.) The reader
    (:meth:`TickParquetStore.read`) sorts and de-duplicates, so the writer stays
    append-only. Past-day accumulators are evicted once the UTC day rolls over.

    Durability / crash semantics
    ----------------------------
    * A hard crash loses at most the in-memory rows since the last flush
      (``--parquet-flush-interval`` seconds, default 60).
    * On restart the accumulator for a day is reloaded once from its file
      (:meth:`TickParquetStore.read_day_table`), so overwriting never truncates
      a day that already has rows on disk.
    * A failed write keeps the rows in the accumulator and is retried on the
      next flush — data is never dropped on a transient error.

    Memory is bounded: the incoming buffer is capped at ``max_buffer_rows``
    (oldest dropped with a loud ERROR if flushing stalls), and only the
    current (plus briefly the just-rolled) UTC day is held per dataset.

    Thread-safety: ``add_*`` runs on the asyncio feed thread and ``flush`` on
    the collector's flush thread. The shared inbound buffer is lock-guarded;
    the accumulators are touched only inside ``flush`` (single thread).
    """

    def __init__(
        self,
        root: str = "data/ticks",
        *,
        exchange: str = "binance",
        max_buffer_rows: int = 2_000_000,
        warn_day_rows: int = 15_000_000,
    ) -> None:
        self._store = TickParquetStore(root=root)
        self.exchange = exchange
        self._max_buffer_rows = max_buffer_rows
        self._warn_day_rows = warn_day_rows
        self._lock = threading.Lock()
        # Inbound buffer (producer = feed thread, consumer = flush thread).
        self._buffers: Dict[Tuple[str, str], List[List[float]]] = defaultdict(list)
        self._dropped = 0
        # In-memory per-day accumulators (chunked pyarrow tables), touched only
        # inside flush().
        self._acc: Dict[Tuple[str, str, str], object] = {}
        self._dirty: set = set()      # accumulators with unwritten changes
        self._loaded: set = set()     # accumulators reloaded from disk already
        self._warned_big: set = set()
        self._max_day: Optional[str] = None

    # -- ingest ---------------------------------------------------------

    def add_trade(
        self, symbol: str, ts_ms: int, price: float, quantity: float,
        is_buyer_maker: bool,
    ) -> None:
        self._extend(
            symbol, "trades",
            [[float(ts_ms), float(price), float(quantity),
              1.0 if is_buyer_maker else 0.0]],
        )

    def add_depth(
        self, symbol: str, ts_ms: int, bids: Iterable, asks: Iterable,
        *, is_snapshot: bool = False,
    ) -> None:
        dataset = "depth_snapshots" if is_snapshot else "depth_updates"
        rows: List[List[float]] = []
        for level in bids:
            rows.append([float(ts_ms), 0.0, float(level[0]), float(level[1])])
        for level in asks:
            rows.append([float(ts_ms), 1.0, float(level[0]), float(level[1])])
        if rows:
            self._extend(symbol, dataset, rows)

    def _extend(self, symbol: str, dataset: str, rows: List[List[float]]) -> None:
        key = (symbol.upper(), dataset)
        with self._lock:
            buf = self._buffers[key]
            buf.extend(rows)
            overflow = len(buf) - self._max_buffer_rows
            if overflow > 0:
                del buf[:overflow]
                self._dropped += overflow

    # -- flush ----------------------------------------------------------

    @staticmethod
    def _table_for_day(dataset: str, ts: np.ndarray, arr: np.ndarray):
        """Build a single-chunk pyarrow table for one day's rows."""
        import pyarrow as pa

        if dataset == "trades":
            return pa.table(
                {
                    "timestamp": pa.array(ts, type=pa.int64()),
                    "price": pa.array(arr[:, 1], type=pa.float64()),
                    "quantity": pa.array(arr[:, 2], type=pa.float64()),
                    "is_buyer_maker": pa.array(arr[:, 3] != 0.0, type=pa.bool_()),
                },
                schema=_arrow_schema(dataset),
            )
        return pa.table(
            {
                "timestamp": pa.array(ts, type=pa.int64()),
                "side": pa.array(arr[:, 1].astype("int8"), type=pa.int8()),
                "price": pa.array(arr[:, 2], type=pa.float64()),
                "quantity": pa.array(arr[:, 3], type=pa.float64()),
            },
            schema=_arrow_schema(dataset),
        )

    def flush(self) -> Dict[str, int]:
        """Persist buffered rows to per-day Parquet files.

        Returns ``{f"{SYMBOL}.{dataset}.{day}": rows_added_this_flush}`` for
        the datasets that were written. A write failure keeps the rows in the
        accumulator (retried next flush), so data is never dropped.
        """
        import pyarrow as pa

        with self._lock:
            if self._dropped:
                logger.error(
                    "LiveParquetMirror dropped %d buffered rows (cap %d) — "
                    "flush has been failing; investigate disk/permissions",
                    self._dropped, self._max_buffer_rows,
                )
                self._dropped = 0
            snapshot = {k: v for k, v in self._buffers.items() if v}
            self._buffers.clear()

        # 1. Merge new rows into per-day accumulators (chunked Arrow tables).
        added: Dict[Tuple[str, str, str], int] = defaultdict(int)
        for (symbol, dataset), rows in snapshot.items():
            arr = np.asarray(rows, dtype="float64")
            ts = arr[:, 0].astype("int64")
            valid = ts > 0
            if not valid.any():
                continue
            arr, ts = arr[valid], ts[valid]
            day_idx = ts // 86_400_000  # whole UTC days since epoch
            for d in np.unique(day_idx):
                m = day_idx == d
                day = datetime.fromtimestamp(
                    int(d) * 86_400, tz=timezone.utc
                ).strftime("%Y-%m-%d")
                key = (symbol, dataset, day)
                table = self._table_for_day(dataset, ts[m], arr[m])
                self._ensure_loaded(key)
                self._acc[key] = (
                    table
                    if self._acc[key].num_rows == 0
                    else pa.concat_tables([self._acc[key], table])
                )
                self._dirty.add(key)
                added[key] += table.num_rows
                self._max_day = day if self._max_day is None else max(self._max_day, day)

        # 2. Write every dirty accumulator (includes earlier failed writes,
        #    even if they received no new rows this flush).
        written: Dict[str, int] = {}
        for key in sorted(self._dirty):
            symbol, dataset, day = key
            acc = self._acc[key]
            if acc.num_rows > self._warn_day_rows and key not in self._warned_big:
                logger.warning(
                    "Parquet day accumulator %s holds %d rows (> %d) — high "
                    "memory; review symbols-per-instance sizing",
                    key, acc.num_rows, self._warn_day_rows,
                )
                self._warned_big.add(key)
            try:
                self._store.write_day_table(self.exchange, symbol, dataset, day, acc)
                self._dirty.discard(key)
                if added[key]:
                    written[f"{symbol}.{dataset}.{day}"] = added[key]
            except Exception as exc:
                logger.warning(
                    "LiveParquetMirror write failed for %s (%d rows kept in "
                    "memory, retried next flush): %s", key, acc.num_rows, exc,
                )

        # 3. Evict fully-written accumulators for past UTC days.
        self._evict_past_days()

        if written:
            logger.info(
                "Parquet mirror flush: %s",
                ", ".join(f"{k}=+{v}" for k, v in written.items()),
            )
        return written

    def _ensure_loaded(self, key: Tuple[str, str, str]) -> None:
        """Load a day's accumulator from disk once (restart recovery)."""
        if key in self._loaded:
            return
        symbol, dataset, day = key
        self._acc[key] = self._store.read_day_table(
            self.exchange, symbol, dataset, day
        )
        self._loaded.add(key)

    def _evict_past_days(self) -> None:
        if self._max_day is None:
            return
        for key in list(self._acc):
            if key[2] < self._max_day and key not in self._dirty:
                del self._acc[key]
                self._loaded.discard(key)
                self._warned_big.discard(key)

    def latest_parquet_date(
        self, symbol: str, dataset: str = "trades"
    ) -> Optional[str]:
        return self._store.latest_parquet_date(self.exchange, symbol, dataset)


__all__ = ["TickParquetStore", "LiveParquetMirror", "DATASETS"]
