"""Shared helpers for the Tide / Wave / Ripple research notebooks.

The notebooks under this directory are for research and are NOT used in
deployment.  This module wraps the project's data loaders behind a single
import surface so every notebook can fetch OHLCV or tick data with one
function call.

Backend selection
-----------------
``load_ohlcv`` uses :func:`ohlcv_store.get_ohlcv_store` which honours the
``DATA_STORE`` environment variable:

* ``DATA_STORE=local_parquet`` (default) → ``<project_root>/data/ohlcv/...``
* ``DATA_STORE=s3``                       → ``s3://${S3_BUCKET}/ohlcv/...``

The data directory is **always resolved relative to the project root**
(the directory that contains ``schemas.py``), not relative to the current
working directory.  This means notebooks run correctly whether Jupyter was
launched from ``notebooks/``, from the project root, or from anywhere else.

If no Parquet OHLCV is found for the requested symbol/timeframe the
loader transparently falls back to the legacy HDF5 store
(``<project_root>/data/{exchange}.h5``) via :class:`database.Hdf5Client`.

Quick S3 setup (one cell at the top of any notebook)::

    import os
    os.environ["DATA_STORE"] = "s3"
    os.environ["S3_BUCKET"]  = "your-bucket-name"
    # then call load_ohlcv(...) as normal

``load_ticks`` opens ``<project_root>/data/{exchange}_ticks.h5`` directly
with h5py and returns DataFrames for trades, depth snapshots, and depth
updates.  The column conventions mirror the C++ ``TickStore`` writer
(``backtestingCpp/orderflow/TickStore.cpp``).
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project root — the directory that contains schemas.py.
# Resolved from *this file's* location so it is always correct regardless of
# what the caller's cwd is.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_DATA_DIR: Path = _ROOT / "data"


@contextlib.contextmanager
def _at_root():
    """Temporarily change working directory to ``_ROOT``.

    Needed for legacy helpers (``database.Hdf5Client``) that open files with
    relative paths like ``data/{exchange}.h5``.
    """
    old = os.getcwd()
    try:
        os.chdir(str(_ROOT))
        yield
    finally:
        os.chdir(old)


# ──────────────────────────────────────────────────────────────────────
# OHLCV
# ──────────────────────────────────────────────────────────────────────


def load_ohlcv(
    exchange: str = "binance",
    symbol: str = "BTCUSDT",
    timeframe: str = "1m",
    *,
    from_ts: Optional[int] = None,
    to_ts: Optional[int] = None,
    backend: Optional[str] = None,
) -> pd.DataFrame:
    """Return an OHLCV DataFrame indexed by a UTC ``DatetimeIndex``.

    Load order
    ----------
    1. Parquet store — local (``<project_root>/data/ohlcv/…``) or S3,
       driven by the ``DATA_STORE`` env var.  Set ``DATA_STORE=s3`` and
       ``S3_BUCKET=<bucket>`` to pull from S3.
    2. Legacy HDF5 — ``<project_root>/data/{exchange}.h5``  (e.g. the
       ``binance.h5`` file collected by the data-collector service).

    The data directory is always resolved from the **project root**, never
    from the notebook's working directory.

    Returned columns: ``open, high, low, close, volume`` (plus ``spread``
    for Oanda data).
    """
    from ohlcv_store import get_ohlcv_store

    # Always pass the absolute data directory so the call is cwd-independent.
    store = get_ohlcv_store(backend=backend, data_dir=str(_DATA_DIR))
    df = store.read(exchange, symbol, timeframe, from_ts=from_ts, to_ts=to_ts)
    if not df.empty:
        return df

    # S3 returned nothing — tell the caller explicitly rather than silently
    # falling through to a possibly-stale local file.
    effective_backend = (backend or os.environ.get("DATA_STORE") or "local_parquet").lower()
    if effective_backend == "s3":
        import warnings
        warnings.warn(
            f"S3 store returned no rows for {exchange}/{symbol}/{timeframe}. "
            "Check that S3_BUCKET is set and the data has been collected.",
            stacklevel=2,
        )
        return df

    # Local Parquet was empty — try the legacy HDF5 file.
    try:
        from database import Hdf5Client
    except Exception:
        return df

    try:
        with _at_root():
            client = Hdf5Client(exchange)
            first, last = client.get_first_last_timestamp(symbol)
        if first is None:
            return df
        ft = int(from_ts) if from_ts is not None else int(first)
        tt = int(to_ts) if to_ts is not None else int(last)
        with _at_root():
            h5_df = client.get_data(symbol, ft, tt)
        return h5_df if h5_df is not None else df
    except Exception as exc:
        import warnings
        warnings.warn(f"HDF5 fallback failed for {exchange}/{symbol}: {exc}", stacklevel=2)
        return df


def list_ohlcv(
    exchange: str = "binance", timeframe: str = "1m", *, backend: Optional[str] = None
) -> Dict[str, list]:
    """Inventory what is available across Parquet and HDF5 stores."""
    from ohlcv_store import get_ohlcv_store

    out: Dict[str, list] = {"parquet": [], "hdf5": []}
    try:
        store = get_ohlcv_store(backend=backend, data_dir=str(_DATA_DIR))
        out["parquet"] = store.list_symbols(exchange, timeframe)
    except Exception as exc:
        out["parquet_error"] = [str(exc)]

    h5_path = _DATA_DIR / f"{exchange}.h5"
    if h5_path.exists():
        try:
            import h5py

            with h5py.File(str(h5_path), "r") as f:
                out["hdf5"] = sorted(list(f.keys()))
        except Exception as exc:
            out["hdf5_error"] = [str(exc)]
    return out


# ──────────────────────────────────────────────────────────────────────
# Ticks (trades + depth)
# ──────────────────────────────────────────────────────────────────────


# Legacy single-file HDF5 layout left here for back-compat with
# ``load_ticks``.  New code should prefer ``load_ticks_parquet`` which
# reads the per-day Parquet mirror under ``data/ticks/{exchange}/...``.
# The collector writes per-symbol HDF5 to ``data/ticks/{SYMBOL}_ticks.h5``;
# the legacy lookup below resolves that path at call time.
_TICKS_LEGACY_PATH = "data/binance_ticks.h5"


def _safe_read_2d(dataset, cap: Optional[int]) -> np.ndarray:
    """Read a 2-D HDF5 dataset chunk-by-chunk, skipping corrupt blocks.

    The local tick HDF5 may have been truncated mid-flush during ad-hoc
    collection runs, leaving the last chunk unreadable.  Reading one HDF5
    native chunk at a time (rather than a fixed 4096-row window) means a
    single corrupt chunk is skipped individually — adjacent valid chunks
    are still recovered.

    Key behaviour
    -------------
    * Uses ``dataset.chunks[0]`` as the step so each iteration corresponds
      to exactly one on-disk HDF5 chunk.  An OSError on chunk N skips only
      those rows; chunk N+1 is still attempted.
    * For non-chunked (contiguous) datasets falls back to a single read.
    """
    n_rows, n_cols = int(dataset.shape[0]), int(dataset.shape[1])
    limit = n_rows if cap is None else min(int(cap), n_rows)
    if limit == 0:
        return np.zeros((0, n_cols))

    # Align to HDF5's native chunk row-count; each read = one on-disk chunk.
    native = dataset.chunks
    step = int(native[0]) if native is not None else limit

    parts: list[np.ndarray] = []
    pos = 0
    while pos < limit:
        end = min(pos + step, limit)
        try:
            with _suppress_hdf5_errors():
                parts.append(dataset[pos:end])
            pos = end
        except OSError:
            pos = end  # skip the bad chunk and carry on
            continue
    if not parts:
        return np.zeros((0, n_cols))
    return np.concatenate(parts, axis=0)


class _suppress_hdf5_errors:
    """Context manager that disables libhdf5's auto-printed error stack."""

    def __enter__(self):
        try:
            from h5py import h5e  # type: ignore[attr-defined]

            self._h5e = h5e
            self._stack = h5e.get_auto()
            h5e.set_auto(None, None)
        except Exception:
            self._h5e = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._h5e is not None:
            try:
                self._h5e.set_auto(*self._stack)
            except Exception:
                pass
        return False


_TICKS_CACHE_DIR: Path = _DATA_DIR / "cache"

_HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"
_SUPERBLOCK_FLAGS_OFFSET = 20   # bytes 20-23 in a v0 superblock = file consistency flags
_HDF5_WRITE_IN_PROGRESS  = 0x1  # bit 0: set while file is open for writing


def _clear_hdf5_open_flag(path: Path) -> bool:
    """Clear the HDF5 superblock write-in-progress bit on a local copy.

    When ``collect_ticks.py`` uploads the HDF5 to S3 while the C++
    ``TickStore`` is still running, the file's consistency flag (superblock
    bytes 20–23, bit 0) is left set to 1.  HDF5 libraries refuse to open
    files in that state as a safety guard against concurrent writers.

    Our downloaded copy is a read-only snapshot — clearing bit 0 is safe
    because the underlying data is intact and we have no writer attached.

    Returns True if the flag was cleared, False if it was already clean.
    """
    with open(path, "r+b") as fh:
        sig = fh.read(8)
        if sig != _HDF5_SIGNATURE:
            raise ValueError(f"Not a valid HDF5 file: {path}")
        sb_version = fh.read(1)[0]   # byte 8
        if sb_version != 0:
            return False             # only superblock v0 has this specific layout
        fh.seek(_SUPERBLOCK_FLAGS_OFFSET)
        raw = fh.read(4)
        flags = int.from_bytes(raw, "little")
        if not (flags & _HDF5_WRITE_IN_PROGRESS):
            return False             # already clean
        fh.seek(_SUPERBLOCK_FLAGS_OFFSET)
        fh.write((flags & ~_HDF5_WRITE_IN_PROGRESS).to_bytes(4, "little"))
        return True

def _ticks_s3_key(exchange: str, symbol: str) -> str:
    """Resolve the S3 key the collector uploads to for ``symbol``.

    Current convention (May 2026): one HDF5 file per symbol at
    ``ticks/{SYMBOL}_ticks.h5``.  The legacy BTCUSDT-only key
    (``ticks/binance_ticks.h5``) is no longer written by the collector.
    """
    return f"ticks/{symbol.upper()}_ticks.h5"


def _legacy_ticks_s3_key(exchange: str, symbol: str) -> str:
    """Pre-May-2026 S3 key for ``symbol``. Used as a fallback when the
    new key is missing so older S3 archives remain readable."""
    base = f"ticks/{exchange}_ticks"
    if symbol.upper() == "BTCUSDT":
        return f"{base}.h5"
    return f"{base}_{symbol.upper()}.h5"


def _sync_ticks_from_s3(
    exchange: str,
    symbol: str,
    *,
    s3_bucket: Optional[str] = None,
    aws_profile: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Download the HDF5 tick file from S3 to a local cache.

    Uses ETag comparison — the file is only re-downloaded when S3 has a
    newer version.  A ``tqdm`` progress bar is shown if the package is
    available.

    The cache lives in ``<project_root>/data/cache/``.
    """
    import boto3

    bucket = s3_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError(
            "S3 bucket not specified. Set S3_BUCKET env var or pass s3_bucket=."
        )
    profile = aws_profile or os.environ.get("AWS_PROFILE")
    s3_key = _ticks_s3_key(exchange, symbol)

    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    # Try the current per-symbol key first; fall back to the legacy
    # key so historical archives still resolve.
    from botocore.exceptions import ClientError
    try:
        head = s3.head_object(Bucket=bucket, Key=s3_key)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            legacy_key = _legacy_ticks_s3_key(exchange, symbol)
            if legacy_key != s3_key:
                print(
                    f"  s3://{bucket}/{s3_key} not found — falling back to "
                    f"legacy key s3://{bucket}/{legacy_key}"
                )
                s3_key = legacy_key
                head = s3.head_object(Bucket=bucket, Key=s3_key)
            else:
                raise
        else:
            raise
    s3_etag = head["ETag"].strip('"')
    s3_size_bytes = head["ContentLength"]
    s3_mtime = head["LastModified"]

    _TICKS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    local_path = _TICKS_CACHE_DIR / Path(s3_key).name
    etag_path  = local_path.with_suffix(".etag")

    if not force and local_path.exists() and etag_path.exists():
        local_size = local_path.stat().st_size
        etag_match = etag_path.read_text().strip() == s3_etag
        # Guard: if the cached file is less than 10 % of the S3 size the
        # cache is corrupt / a stub — force a real download regardless of ETag.
        size_ok = local_size >= s3_size_bytes * 0.10
        if etag_match and size_ok:
            print(
                f"  cache hit: {local_path.name}  "
                f"({local_size / 1024**3:.2f} GB, S3 last modified {s3_mtime:%Y-%m-%d %H:%M})"
            )
            return local_path
        if etag_match and not size_ok:
            print(
                f"  cache corrupt ({local_size/1024**2:.1f} MB vs "
                f"{s3_size_bytes/1024**3:.2f} GB on S3) — re-downloading."
            )

    size_gb = s3_size_bytes / 1024**3
    print(f"  Downloading s3://{bucket}/{s3_key}  ({size_gb:.2f} GB) …")

    tmp_path = local_path.with_suffix(".tmp")
    try:
        from tqdm.auto import tqdm  # type: ignore[import]

        with tqdm(total=s3_size_bytes, unit="B", unit_scale=True, unit_divisor=1024,
                  desc=Path(s3_key).name) as bar:
            s3.download_file(
                bucket, s3_key, str(tmp_path),
                Callback=lambda n: bar.update(n),
            )
    except ImportError:
        s3.download_file(bucket, s3_key, str(tmp_path))

    tmp_path.rename(local_path)
    etag_path.write_text(s3_etag)
    print(f"  Saved → {local_path}  ({local_path.stat().st_size / 1024**3:.2f} GB)")

    # The collector uploads the HDF5 while it is still open on EC2, so the
    # write-in-progress bit (superblock byte 20, bit 0) is always set in the
    # downloaded copy.  Clear it so h5py can open the file.
    if _clear_hdf5_open_flag(local_path):
        print("  Cleared HDF5 write-in-progress flag on cached copy.")

    return local_path


def load_ticks(
    symbol: str = "BTCUSDT",
    *,
    exchange: str = "binance",
    path: Optional[str] = None,
    max_trades: Optional[int] = None,
    max_depth_snapshots: Optional[int] = None,
    max_depth_updates: Optional[int] = None,
    refresh: bool = False,
) -> Dict[str, pd.DataFrame]:
    """Load trades / depth snapshots / depth updates for one symbol.

    Data source — resolution order
    ------------------------------
    1. ``path`` kwarg — use this HDF5 file directly (no S3 involved).
    2. ``refresh=True`` — sync from S3 first (ETag check; only downloads
       if S3 has a newer file), then read from the local cache.
    3. S3 cache — ``<project_root>/data/cache/{exchange}_ticks.h5`` — used
       if it already exists (result of a previous ``refresh=True`` call).
    4. Local file — ``<project_root>/data/{exchange}_ticks.h5``.

    The default (``refresh=False``) never touches the network, so notebooks
    stay fast on every run.  Call ``load_ticks(..., refresh=True)`` once
    when you want to pull the latest data from S3 (the collector uploads
    continuously, so the S3 file is usually more recent than the cache).

    ``S3_BUCKET`` and ``AWS_PROFILE`` env vars are required for S3 access.

    Returns a dict with three DataFrames:

    * ``trades``           — columns ``timestamp, price, quantity, is_buyer_maker``
    * ``depth_snapshots``  — columns ``timestamp, side, price, quantity``
    * ``depth_updates``    — columns ``timestamp, side, price, quantity``

    ``side`` is ``0`` for bids and ``1`` for asks (matches the C++
    ``TickStore`` writer).  Timestamps are kept as ``int64`` ms-since-epoch.
    """
    import h5py

    # ── resolve HDF5 path ────────────────────────────────────────────
    if path:
        target = Path(path)
    else:
        # New per-symbol layout (written by collect_ticks.py since May 2026)
        new_local  = _ROOT / "data" / "ticks" / f"{symbol.upper()}_ticks.h5"
        # Legacy single-file layout (kept for back-compat)
        legacy_local = _ROOT / _TICKS_LEGACY_PATH
        new_cached    = _TICKS_CACHE_DIR / f"{symbol.upper()}_ticks.h5"
        legacy_cached = _TICKS_CACHE_DIR / f"{exchange}_ticks.h5"

        if refresh:
            # Explicit refresh: sync from S3 (ETag-gated, only downloads if newer)
            target = _sync_ticks_from_s3(exchange, symbol)
        elif new_cached.exists():
            target = new_cached
        elif new_local.exists():
            target = new_local
        elif legacy_cached.exists():
            target = legacy_cached
        else:
            target = legacy_local

    if not target.exists():
        raise FileNotFoundError(
            f"Tick file not found at {target}.\n"
            "  • To pull from S3: call load_ticks(..., refresh=True)\n"
            "    (requires S3_BUCKET and AWS_PROFILE env vars).\n"
            "  • To collect locally: run collect_ticks.py."
        )

    out: Dict[str, pd.DataFrame] = {}
    with h5py.File(str(target), "r") as f:
        if symbol not in f:
            raise KeyError(
                f"Symbol {symbol!r} not in {target}. Available: {list(f.keys())}"
            )
        grp = f[symbol]

        # Read chunk-by-chunk so a single corrupt/truncated chunk (the
        # collector uploads to S3 mid-write) is skipped rather than aborting
        # the whole read — matches the depth datasets below.
        trades_arr = (
            _safe_read_2d(grp["trades"], max_trades)
            if "trades" in grp
            else np.zeros((0, 4))
        )
        trades = pd.DataFrame(
            trades_arr, columns=["timestamp", "price", "quantity", "is_buyer_maker"]
        )
        trades["timestamp"] = trades["timestamp"].astype("int64")
        trades["is_buyer_maker"] = trades["is_buyer_maker"].astype(bool)
        trades = trades[trades["timestamp"] > 0].reset_index(drop=True)
        out["trades"] = trades

        for name, cap in (
            ("depth_snapshots", max_depth_snapshots),
            ("depth_updates", max_depth_updates),
        ):
            if name in grp:
                arr = _safe_read_2d(grp[name], cap)
                df = pd.DataFrame(
                    arr, columns=["timestamp", "side", "price", "quantity"]
                )
                df["timestamp"] = df["timestamp"].astype("int64")
                df["side"] = df["side"].astype("int8")
                df = df[df["timestamp"] > 0].reset_index(drop=True)
            else:
                df = pd.DataFrame(
                    columns=["timestamp", "side", "price", "quantity"]
                )
            out[name] = df

    return out


# ──────────────────────────────────────────────────────────────────────
# Ticks (Parquet — preferred since the May-2026 collector rewrite)
# ──────────────────────────────────────────────────────────────────────
#
# The live collector flushes HDF5 → daily Parquet every 15 minutes and the
# hourly cron syncs those files to ``s3://${S3_BUCKET}/ticks-parquet/``.
# Notebooks should prefer ``load_ticks_parquet`` over ``load_ticks`` for
# any new analysis — it downloads only the day-files needed for the
# requested window, not the full multi-GB HDF5 archive.

_TICKS_PARQUET_CACHE_DIR: Path = _DATA_DIR / "cache" / "ticks_parquet"

_TICK_DATASETS: Tuple[str, ...] = ("trades", "depth_snapshots", "depth_updates")
_TRADES_COLS = ["timestamp", "price", "quantity", "is_buyer_maker"]
_DEPTH_COLS = ["timestamp", "side", "price", "quantity"]


def _parquet_columns_for(dataset: str) -> list:
    return _TRADES_COLS if dataset == "trades" else _DEPTH_COLS


def _sync_ticks_parquet_from_s3(
    exchange: str,
    symbol: str,
    datasets: Tuple[str, ...],
    *,
    from_date: Optional[str],
    to_date: Optional[str],
    s3_bucket: Optional[str] = None,
    aws_profile: Optional[str] = None,
) -> Path:
    """Download per-day Parquet files for ``symbol`` from S3 into the local cache.

    Only files within ``[from_date, to_date]`` are downloaded, and any
    file already present locally is skipped (ETag is not checked because
    daily files become immutable once the day rolls over).

    Returns the local cache directory ``{cache}/{exchange}/{SYMBOL}/``.
    """
    import boto3
    from botocore.exceptions import ClientError

    bucket = s3_bucket or os.environ.get("S3_BUCKET")
    if not bucket:
        raise ValueError(
            "S3 bucket not specified. Set S3_BUCKET env var or pass s3_bucket=."
        )
    profile = aws_profile or os.environ.get("AWS_PROFILE")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    s3 = session.client("s3")

    sym = symbol.upper()
    local_root = _TICKS_PARQUET_CACHE_DIR / exchange / sym
    n_downloaded = 0
    n_skipped = 0
    n_failed = 0

    for dataset in datasets:
        if dataset not in _TICK_DATASETS:
            raise ValueError(
                f"Unknown dataset {dataset!r}; expected one of {_TICK_DATASETS}"
            )
        prefix = f"ticks-parquet/{exchange}/{sym}/{dataset}/"
        local_dir = local_root / dataset
        local_dir.mkdir(parents=True, exist_ok=True)

        keys: list[str] = []
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []) or []:
                    key = obj["Key"]
                    day = key.rsplit("/", 1)[-1].removesuffix(".parquet")
                    if not key.endswith(".parquet"):
                        continue
                    if from_date is not None and day < from_date:
                        continue
                    if to_date is not None and day > to_date:
                        continue
                    keys.append(key)
        except ClientError as exc:
            print(f"  [WARN] could not list s3://{bucket}/{prefix}: {exc}")
            continue

        for key in sorted(keys):
            local_path = local_dir / Path(key).name
            if local_path.exists() and local_path.stat().st_size > 0:
                n_skipped += 1
                continue
            tmp = local_path.with_suffix(".tmp")
            try:
                s3.download_file(bucket, key, str(tmp))
                tmp.rename(local_path)
                n_downloaded += 1
            except ClientError as exc:
                print(f"  [WARN] download failed s3://{bucket}/{key}: {exc}")
                n_failed += 1

    summary = f"  ticks-parquet sync {exchange}/{sym}: " \
              f"downloaded={n_downloaded} cached={n_skipped} failed={n_failed}"
    if from_date or to_date:
        summary += f"  (window {from_date or 'start'}…{to_date or 'now'})"
    print(summary)
    return local_root


def load_ticks_parquet(
    symbol: str = "BTCUSDT",
    *,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    exchange: str = "binance",
    datasets: Tuple[str, ...] = ("trades", "depth_snapshots", "depth_updates"),
    refresh: bool = False,
) -> Dict[str, pd.DataFrame]:
    """Load trades / depth from the Parquet tick mirror.

    Use this instead of :func:`load_ticks` for any new analysis. It
    fetches only the daily Parquet files needed for the requested
    ``[from_date, to_date]`` window (inclusive ``YYYY-MM-DD`` strings),
    not the entire multi-GB HDF5 archive.

    Data source — resolution order
    ------------------------------
    1. ``refresh=True`` — download any missing day-files for the window
       from ``s3://${S3_BUCKET}/ticks-parquet/`` to the local cache.
    2. Existing cache — ``<project_root>/data/cache/ticks_parquet/…``.
    3. Live local store — ``<project_root>/data/ticks/…`` (the location
       the collector writes to; useful when running notebooks on the
       collector host itself).

    ``S3_BUCKET`` and ``AWS_PROFILE`` env vars are required for S3 sync.

    Returns a dict with one DataFrame per dataset (defaults to all three):

    * ``trades``           — columns ``timestamp, price, quantity, is_buyer_maker``
    * ``depth_snapshots``  — columns ``timestamp, side, price, quantity``
    * ``depth_updates``    — columns ``timestamp, side, price, quantity``

    Schema matches :func:`load_ticks`. Timestamps are ``int64`` ms-since-epoch.
    """
    from tick_parquet_store import TickParquetStore  # late import keeps notebook startup quick

    for dataset in datasets:
        if dataset not in _TICK_DATASETS:
            raise ValueError(
                f"Unknown dataset {dataset!r}; expected one of {_TICK_DATASETS}"
            )

    if refresh:
        _sync_ticks_parquet_from_s3(
            exchange, symbol, datasets,
            from_date=from_date, to_date=to_date,
        )

    # Resolve which root to read from. Prefer the freshly synced cache,
    # fall back to the live store on the collector host.
    sym = symbol.upper()
    cache_root = _TICKS_PARQUET_CACHE_DIR
    live_root  = _ROOT / "data" / "ticks"

    candidate_roots: list[Path] = []
    if (cache_root / exchange / sym).is_dir():
        candidate_roots.append(cache_root)
    if (live_root / exchange / sym).is_dir():
        candidate_roots.append(live_root)

    out: Dict[str, pd.DataFrame] = {}
    for dataset in datasets:
        frames: list[pd.DataFrame] = []
        for root in candidate_roots:
            store = TickParquetStore(root=str(root))
            df = store.read(
                sym, dataset,
                from_date=from_date, to_date=to_date, exchange=exchange,
            )
            if not df.empty:
                frames.append(df)
        if frames:
            merged = pd.concat(frames, ignore_index=True)
            if dataset == "trades":
                subset = ["timestamp", "price", "quantity", "is_buyer_maker"]
            else:
                subset = ["timestamp", "side", "price", "quantity"]
            merged = (
                merged.drop_duplicates(subset=subset, keep="last")
                      .sort_values("timestamp")
                      .reset_index(drop=True)
            )
            out[dataset] = merged
        else:
            out[dataset] = pd.DataFrame(columns=_parquet_columns_for(dataset))

    return out


def latest_book(
    depth_snapshots: pd.DataFrame, *, max_levels: int = 50
) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    """Reconstruct the most-recent depth-snapshot from a snapshots frame.

    Returns ``(bids, asks, timestamp_ms)``.  Each side is sorted with the
    best price first.  Walks backwards through unique timestamps until it
    finds the first snapshot that actually contains both sides — useful
    when the very last snapshot was a partial flush.
    """
    if depth_snapshots.empty:
        empty = pd.DataFrame(columns=["price", "quantity"])
        return empty, empty, 0
    unique_ts = sorted(depth_snapshots["timestamp"].unique(), reverse=True)
    for ts in unique_ts:
        snap = depth_snapshots[depth_snapshots["timestamp"] == int(ts)]
        bids = (
            snap[snap["side"] == 0][["price", "quantity"]]
            .sort_values("price", ascending=False)
            .head(max_levels)
            .reset_index(drop=True)
        )
        asks = (
            snap[snap["side"] == 1][["price", "quantity"]]
            .sort_values("price", ascending=True)
            .head(max_levels)
            .reset_index(drop=True)
        )
        if not bids.empty and not asks.empty:
            return bids, asks, int(ts)
    empty = pd.DataFrame(columns=["price", "quantity"])
    return empty, empty, int(unique_ts[0]) if unique_ts else 0


# ──────────────────────────────────────────────────────────────────────
# Plot helpers
# ──────────────────────────────────────────────────────────────────────


def plot_ohlcv(
    df: pd.DataFrame,
    *,
    title: str = "OHLCV",
    figsize: Tuple[float, float] = (12, 5),
    volume: bool = True,
):
    """Lightweight candlestick + volume plot using matplotlib.

    Avoids mplfinance to keep the dependency footprint small.  Returns the
    matplotlib ``Figure`` so the caller can decorate further.
    """
    import matplotlib.pyplot as plt

    if df.empty:
        raise ValueError("plot_ohlcv: empty DataFrame")

    fig, ax_price = plt.subplots(figsize=figsize)
    ax_price.plot(df.index, df["close"], color="#1f77b4", linewidth=1.0, label="close")
    ax_price.fill_between(
        df.index, df["low"], df["high"], color="#1f77b4", alpha=0.15, label="hi-lo band"
    )
    ax_price.set_title(title)
    ax_price.set_ylabel("price")
    ax_price.grid(alpha=0.3)
    ax_price.legend(loc="upper left")

    if volume and "volume" in df.columns:
        ax_vol = ax_price.twinx()
        ax_vol.bar(
            df.index,
            df["volume"],
            width=(df.index[1] - df.index[0]) if len(df) > 1 else 1,
            color="#888",
            alpha=0.25,
            label="volume",
        )
        ax_vol.set_ylabel("volume", color="#666")
        ax_vol.tick_params(axis="y", labelcolor="#666")

    fig.tight_layout()
    return fig


def plot_equity_curve(
    equity: pd.Series,
    *,
    title: str = "Equity curve",
    figsize: Tuple[float, float] = (12, 4),
):
    """Plot an equity curve plus a drawdown subplot."""
    import matplotlib.pyplot as plt

    if equity.empty:
        raise ValueError("plot_equity_curve: empty series")

    eq = equity.astype(float)
    peak = eq.cummax()
    dd = (eq - peak) / peak.replace(0, np.nan)

    fig, (ax_eq, ax_dd) = plt.subplots(
        2, 1, figsize=figsize, sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax_eq.plot(eq.index, eq.values, color="#1f77b4", linewidth=1.2)
    ax_eq.set_title(title)
    ax_eq.set_ylabel("equity")
    ax_eq.grid(alpha=0.3)

    ax_dd.fill_between(dd.index, dd.values, 0, color="#d62728", alpha=0.4)
    ax_dd.set_ylabel("drawdown")
    ax_dd.set_ylim(min(dd.min() * 1.1, -0.01), 0.02)
    ax_dd.grid(alpha=0.3)

    fig.tight_layout()
    return fig


# ──────────────────────────────────────────────────────────────────────
# Misc convenience
# ──────────────────────────────────────────────────────────────────────


def configure_pandas() -> None:
    """Apply notebook-friendly pandas display options."""
    pd.set_option("display.max_columns", 50)
    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda x: f"{x:,.6f}")


def env_summary() -> pd.DataFrame:
    """Snapshot of the active data-source configuration.

    Shows env vars, the resolved project root, what data exists locally,
    and whether S3 is reachable — so you can see at a glance why data
    might or might not be loading.
    """
    rows: list[dict] = []

    # Env vars
    for key in ("DATA_STORE", "S3_BUCKET", "AWS_PROFILE", "AWS_DEFAULT_REGION", "AWS_ACCESS_KEY_ID"):
        rows.append({"item": key, "value": os.environ.get(key, "<unset>")})

    # Derived
    effective = (os.environ.get("DATA_STORE") or "local_parquet").lower()
    rows.append({"item": "effective backend", "value": effective})
    rows.append({"item": "project root",      "value": str(_ROOT)})
    rows.append({"item": "data dir",          "value": str(_DATA_DIR)})

    # Local files
    h5_files = sorted(_DATA_DIR.glob("*.h5")) if _DATA_DIR.exists() else []
    parquet_dir = _DATA_DIR / "ohlcv"
    rows.append({"item": "local HDF5 files",  "value": ", ".join(f.name for f in h5_files) or "<none>"})
    rows.append({"item": "local parquet dir", "value": str(parquet_dir) + (" (exists)" if parquet_dir.exists() else " (missing)")})

    # Tick S3 cache
    cache_files = sorted(_TICKS_CACHE_DIR.glob("*.h5")) if _TICKS_CACHE_DIR.exists() else []
    cache_info = []
    for cf in cache_files:
        etag_f = cf.with_suffix(".etag")
        tag = etag_f.read_text().strip()[:8] if etag_f.exists() else "?"
        cache_info.append(f"{cf.name} ({cf.stat().st_size/1024**3:.2f} GB, etag={tag}…)")
    rows.append({"item": "tick S3 cache", "value": ", ".join(cache_info) or "<empty>"})

    return pd.DataFrame(rows).set_index("item")


__all__ = [
    "load_ohlcv",
    "list_ohlcv",
    "load_ticks",
    "load_ticks_parquet",
    "latest_book",
    "plot_ohlcv",
    "plot_equity_curve",
    "configure_pandas",
    "env_summary",
]
