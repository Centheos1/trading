"""Headless tick data collector for EC2 deployment.

Wraps TickDataCollector from data_service.py with:
  - CLI argument parsing (no interactive prompts)
  - Per-symbol HDF5 store at ``data/ticks/{SYMBOL}_ticks.h5``
  - Background Parquet flush thread (durable, queryable mirror in
    ``data/ticks/{exchange}/{SYMBOL}/{dataset}/YYYY-MM-DD.parquet``)
  - Optional S3 upload on clean exit
  - HDF5 size-based rotation: archives the old file to S3 and starts
    fresh so the local disk never fills indefinitely
  - SIGTERM → graceful shutdown (flush → close → upload)
  - Structured logging to stdout + rotating file

Usage:
    python collect_ticks.py --symbol BTCUSDT
    python collect_ticks.py --symbol BTCUSDT --duration 3600 --s3-bucket my-bucket
    python collect_ticks.py --symbols BTCUSDT,ETHUSDT --log-level DEBUG
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
from datetime import datetime, timezone

# Ensure the C++ module in the build directory is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                "backtestingCpp", "orderflow", "build"))

try:
    import orderflow_engine as ofe  # noqa: F401 — import validates build
except ImportError:
    print(
        "ERROR: orderflow_engine C++ module not found.\n"
        "Build it first: cd backtestingCpp/orderflow && bash build.sh",
        file=sys.stderr,
    )
    sys.exit(1)

from data_service import TickDataCollector  # noqa: E402 — after sys.path patch
from tick_parquet_store import TickParquetStore  # noqa: E402

# Exit code that signals "rotation requested — please restart me".
# Docker ``restart: unless-stopped`` and systemd ``Restart=on-failure``
# both restart on any non-zero exit, which is exactly what we want.
EXIT_ROTATE = 75


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(level: str, log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.DEBUG)
    root.addHandler(sh)

    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "collector.log"),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)


logger = logging.getLogger("collect_ticks")


# ---------------------------------------------------------------------------
# S3 upload helper
# ---------------------------------------------------------------------------

def _create_h5_snapshot(src_path: str, dst_path: str) -> bool:
    """Copy an open HDF5 tick file to a clean, self-consistent snapshot.

    When the C++ TickStore has ``src_path`` open for writing, the HDF5
    root-group navigation metadata (object header, symbol table nodes, local
    heap) lives in libhdf5's internal page cache and may not have been
    flushed to the OS-level file.  Uploading the raw file to S3 in that
    state produces an unreadable copy.

    On Linux, a second h5py process opening the same file in read-only mode
    reads through the OS page cache, which *does* contain the unflushed
    writes from the C++ process.  We exploit this to copy all datasets to a
    freshly created file that has fully consistent on-disk metadata.

    Returns True on success, False if the read or write failed.
    """
    try:
        import h5py

        with h5py.File(src_path, "r", locking=False) as src, \
             h5py.File(dst_path, "w") as dst:
            for name in src.keys():
                src.copy(name, dst, expand_soft=True, expand_external=True)

        logger.info("HDF5 snapshot written to %s", dst_path)
        return True
    except Exception as exc:
        logger.error("HDF5 snapshot failed: %s", exc)
        return False


def _upload_to_s3(local_path: str, bucket: str, key: str,
                  *, collector_running: bool = False) -> bool:
    """Upload a tick HDF5 file to S3.

    When ``collector_running=True`` the C++ TickStore still has the file
    open, so a direct upload would capture an inconsistent on-disk state
    (unflushed HDF5 metadata).  In that case a clean snapshot is created
    first via :func:`_create_h5_snapshot` and the snapshot is uploaded
    instead.  The original file is left untouched.

    When ``collector_running=False`` (i.e. called after the store has been
    flushed and closed) the file is uploaded directly.
    """
    try:
        import boto3
    except ImportError as exc:
        logger.error("boto3 unavailable — cannot upload to S3: %s", exc)
        return False

    # Resolve the snapshot path upfront so the finally block can always find
    # it — regardless of whether the upload succeeds or raises.
    snapshot: str | None = (local_path + ".snapshot.h5") if collector_running else None
    upload_path = local_path

    try:
        if collector_running:
            logger.info(
                "Collector is running — creating HDF5 snapshot before upload"
            )
            if _create_h5_snapshot(local_path, snapshot):
                upload_path = snapshot
            else:
                logger.warning(
                    "Snapshot failed; uploading raw file (may be unreadable)"
                )

        s3 = boto3.client("s3")
        logger.info("Uploading %s → s3://%s/%s", upload_path, bucket, key)
        s3.upload_file(upload_path, bucket, key)
        logger.info("S3 upload complete: s3://%s/%s", bucket, key)
        return True

    except Exception as exc:
        logger.error("S3 upload failed: %s", exc)
        return False

    finally:
        # Always remove the snapshot — on success, on upload failure, and on
        # any unexpected exception — so failed uploads never accumulate files.
        if snapshot and os.path.exists(snapshot):
            os.unlink(snapshot)


# ---------------------------------------------------------------------------
# Signal handling for SIGTERM (systemd / Docker stop)
# ---------------------------------------------------------------------------
#
# Two co-operating flags coordinate clean shutdown and rotation:
#
#   _shutdown_requested  — set on SIGTERM; main loop stops iterating
#   _rotation_requested  — set by the Parquet flush thread when the live
#                          HDF5 file exceeds --max-h5-gb; main loop
#                          archives + deletes the file and exits with
#                          EXIT_ROTATE so the container is restarted
#
# Both signal handlers re-raise as SIGINT (via ``os.kill``) which Python
# translates to KeyboardInterrupt in the main thread, interrupting the
# asyncio loop inside ``TickDataCollector.collect()``.

_shutdown_requested = threading.Event()
_rotation_requested = threading.Event()


def _handle_sigterm(signum, frame):  # noqa: ANN001
    """Translate SIGTERM → KeyboardInterrupt so collect() unwinds cleanly."""
    if _shutdown_requested.is_set():
        return  # already handled — avoid recursive signal storm
    logger.info("SIGTERM received — initiating graceful shutdown")
    _shutdown_requested.set()
    try:
        os.kill(os.getpid(), signal.SIGINT)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Path / key helpers — single source of truth for tick file naming
# ---------------------------------------------------------------------------

def _ticks_h5_path(data_dir: str, symbol: str) -> str:
    """Canonical HDF5 path: ``{data_dir}/ticks/{SYMBOL}_ticks.h5``."""
    ticks_dir = os.path.join(data_dir, "ticks")
    os.makedirs(ticks_dir, exist_ok=True)
    return os.path.join(ticks_dir, f"{symbol.upper()}_ticks.h5")


def _default_s3_key(symbol: str) -> str:
    return f"ticks/{symbol.upper()}_ticks.h5"


def _archive_s3_key(exchange: str, symbol: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"ticks/archive/{exchange}/{symbol.upper()}_ticks_{ts}.h5"


# ---------------------------------------------------------------------------
# Parquet flush worker — background mirror of HDF5 → daily Parquet files
# ---------------------------------------------------------------------------

def _parquet_flush_worker(
    h5_path: str,
    symbol: str,
    exchange: str,
    parquet_root: str,
    interval_sec: int,
    stop_event: threading.Event,
    max_h5_bytes: int,
) -> None:
    """Drain new HDF5 rows into Parquet on a fixed interval.

    Also enforces the size cap on the live HDF5 file by raising SIGINT
    (which collect() handles as KeyboardInterrupt) when the file grows
    past ``max_h5_bytes``.  The main thread then archives + deletes the
    file and exits so the container is restarted clean.
    """
    store = TickParquetStore(root=parquet_root)
    logger.info(
        "Parquet flush thread started for %s/%s (every %ds, rotation @ %s)",
        exchange,
        symbol,
        interval_sec,
        f"{max_h5_bytes / 1024**3:.1f} GB" if max_h5_bytes > 0 else "disabled",
    )
    while not stop_event.wait(interval_sec):
        try:
            store.flush_from_h5(h5_path, symbol, exchange)
        except Exception as exc:
            logger.warning("Parquet flush failed: %s", exc)

        if max_h5_bytes > 0 and os.path.exists(h5_path):
            try:
                size = os.path.getsize(h5_path)
            except OSError:
                continue
            if size >= max_h5_bytes:
                logger.warning(
                    "HDF5 file %s reached %.2f GB (limit %.2f GB) — "
                    "requesting rotation",
                    h5_path,
                    size / 1024**3,
                    max_h5_bytes / 1024**3,
                )
                _rotation_requested.set()
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                except OSError:
                    pass
                return

    # Final flush on clean shutdown so we capture rows the main thread is
    # about to flush + close.
    try:
        store.flush_from_h5(h5_path, symbol, exchange)
    except Exception as exc:
        logger.warning("Final Parquet flush failed: %s", exc)


def _archive_and_remove_h5(
    h5_path: str, bucket: str | None, exchange: str, symbol: str
) -> None:
    """Upload ``h5_path`` to the archive prefix in S3 (if a bucket is
    configured) and delete the local file."""
    if not os.path.exists(h5_path):
        return
    if bucket:
        key = _archive_s3_key(exchange, symbol)
        ok = _upload_to_s3(h5_path, bucket, key, collector_running=False)
        if not ok:
            logger.error(
                "Archive upload failed — leaving %s in place for manual recovery",
                h5_path,
            )
            return
    try:
        os.unlink(h5_path)
        logger.info("Removed local HDF5 %s after archive", h5_path)
    except OSError as exc:
        logger.error("Failed to remove %s: %s", h5_path, exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Headless Binance tick data collector for EC2 deployment.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--symbol",
        default="BTCUSDT",
        help="Single symbol to collect (use --symbols for multiple).",
    )
    p.add_argument(
        "--symbols",
        default=None,
        help="Comma-separated list of symbols, e.g. BTCUSDT,ETHUSDT. "
             "Overrides --symbol when set.",
    )
    p.add_argument(
        "--exchange",
        default="binance",
        choices=["binance"],
        help="Exchange to collect from.",
    )
    p.add_argument(
        "--futures",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use Binance USD-M Futures streams (default: true).",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Collection duration in seconds. 0 = run until SIGTERM/Ctrl+C.",
    )
    p.add_argument(
        "--s3-bucket",
        default=None,
        help="If set, upload the HDF5 file to this S3 bucket on clean exit.",
    )
    p.add_argument(
        "--s3-key",
        default=None,
        help="S3 object key for the uploaded HDF5 file. "
             "Defaults to ticks/{SYMBOL}_ticks.h5 per symbol.",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    p.add_argument(
        "--data-dir",
        default="data",
        help="Root directory for tick data. HDF5 lives under "
             "{data-dir}/ticks/ and Parquet mirror under the same tree.",
    )
    p.add_argument(
        "--parquet-flush-interval",
        type=int,
        default=900,
        help="Seconds between HDF5 → Parquet flushes (0 = disable mirror).",
    )
    p.add_argument(
        "--max-h5-gb",
        type=float,
        default=8.0,
        help="Rotate the live HDF5 file when it exceeds this many GB. "
             "0 = no rotation (HDF5 grows unbounded — not recommended).",
    )
    return p.parse_args()


def _collect_one(args: argparse.Namespace, symbol: str) -> int:
    """Collect one symbol with its own HDF5 store and Parquet flush thread.

    Returns the per-symbol exit code:

      * ``0``            — clean exit (SIGTERM or duration elapsed)
      * ``EXIT_ROTATE``  — rotation was requested; archive + delete done
      * ``1``            — unhandled exception
      * ``2``            — S3 upload failure
    """
    exchange = args.exchange
    store_path = _ticks_h5_path(args.data_dir, symbol)
    s3_key = args.s3_key or _default_s3_key(symbol)
    max_h5_bytes = int(args.max_h5_gb * 1024**3) if args.max_h5_gb > 0 else 0
    parquet_root = os.path.join(args.data_dir, "ticks")

    _rotation_requested.clear()

    collector = TickDataCollector(
        exchange=exchange,
        futures=args.futures,
        store_path=store_path,
    )

    flush_stop = threading.Event()
    flush_thread: threading.Thread | None = None
    if args.parquet_flush_interval > 0:
        flush_thread = threading.Thread(
            target=_parquet_flush_worker,
            args=(
                store_path, symbol, exchange, parquet_root,
                args.parquet_flush_interval, flush_stop, max_h5_bytes,
            ),
            name=f"parquet-flush-{symbol}",
            daemon=True,
        )
        flush_thread.start()

    exit_code = 0
    try:
        # collect() owns the store lifecycle — it calls store.flush() and
        # store.close() in its own finally block before returning.  Do NOT
        # call flush/close here; doing so would be a double-close of the
        # C++ TickStore which is undefined behaviour.
        collector.collect(symbol, duration_seconds=args.duration)
    except KeyboardInterrupt:
        if _rotation_requested.is_set():
            logger.info("Collection stopped for HDF5 rotation")
        else:
            logger.info("Keyboard interrupt — shutting down")
    except Exception as exc:
        logger.exception("Unhandled exception for %s: %s", symbol, exc)
        exit_code = 1
    finally:
        # At this point data_service.collect() has already called
        # store.flush() + store.close().  The HDF5 file is closed and
        # self-consistent on disk — safe to read with locking=False.

        # 1. Stop the Parquet flush thread and wait for it to drain.
        flush_stop.set()
        if flush_thread is not None:
            flush_thread.join(timeout=30)

        # 2. Final Parquet flush: captures any rows the background thread
        #    missed between its last wake-up and store.close().
        try:
            TickParquetStore(root=parquet_root).flush_from_h5(
                store_path, symbol, exchange
            )
        except Exception as exc:
            logger.warning("Final Parquet flush failed: %s", exc)

        # 3. Either archive+rotate or upload the closed HDF5 to S3.
        if _rotation_requested.is_set():
            _archive_and_remove_h5(store_path, args.s3_bucket, exchange, symbol)
            exit_code = EXIT_ROTATE
        elif args.s3_bucket:
            ok = _upload_to_s3(
                store_path, args.s3_bucket, s3_key, collector_running=False,
            )
            if not ok:
                exit_code = max(exit_code, 2)

    return exit_code


def _build_child_argv(symbol: str) -> list:
    """Reconstruct sys.argv for a single-symbol child process.

    Replaces ``--symbols A,B,...`` (or ``--symbols=A,B,...``) with
    ``--symbol <symbol>`` so each spawned child uses the well-tested
    single-symbol code path and manages its own signal / store lifecycle.
    """
    argv = list(sys.argv)
    result: list = []
    i = 0
    while i < len(argv):
        if argv[i] == "--symbols" and i + 1 < len(argv):
            result += ["--symbol", symbol]
            i += 2
        elif argv[i].startswith("--symbols="):
            result += [f"--symbol={symbol}"]
            i += 1
        else:
            result.append(argv[i])
            i += 1
    return result


def _run_multi_symbol(symbols: list) -> int:
    """Spawn one child process per symbol and run them all in parallel.

    Each child is a fresh invocation of this script with ``--symbol <X>``
    so it owns its own HDF5 store, Parquet flush thread, SIGTERM handler,
    and S3 upload.  The parent only forwards signals and collects exit codes.

    Returns the maximum exit code of all children (so rotation exit code 75
    propagates correctly to Docker/systemd restart logic).
    """
    import subprocess

    children: dict = {}
    for sym in symbols:
        child_argv = _build_child_argv(sym)
        p = subprocess.Popen(child_argv)
        children[sym] = p
        logger.info("Spawned collector for %s (pid=%d)", sym, p.pid)

    def _forward_signal(signum, frame):  # noqa: ANN001
        name = signal.Signals(signum).name
        logger.info(
            "%s received in parent — forwarding to %d child collectors",
            name, len(children),
        )
        for p in children.values():
            if p.poll() is None:
                try:
                    p.send_signal(signum)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, _forward_signal)
    signal.signal(signal.SIGINT, _forward_signal)

    max_exit = 0
    for sym, p in children.items():
        rc = p.wait()
        rc = rc if rc is not None else 1
        max_exit = max(max_exit, rc)
        logger.info("Collector for %s exited (rc=%d)", sym, rc)

    return max_exit


def main() -> int:
    args = _parse_args()
    _setup_logging(args.log_level)

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else [args.symbol.upper()]
    )

    logger.info(
        "collect_ticks starting | symbols=%s exchange=%s futures=%s duration=%ss "
        "parquet_flush=%ss max_h5=%sGB",
        symbols,
        args.exchange,
        args.futures,
        args.duration if args.duration > 0 else "∞",
        args.parquet_flush_interval,
        args.max_h5_gb,
    )

    # Multiple symbols: each runs in its own subprocess so they collect in
    # parallel and each handles its own signal / HDF5 / S3 lifecycle.
    if len(symbols) > 1:
        overall_exit = _run_multi_symbol(symbols)
        logger.info("collect_ticks (parent) exiting with code %d", overall_exit)
        return overall_exit

    # Single symbol: run directly in this process.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    overall_exit = 0
    sym = symbols[0]
    logger.info("Starting collection for %s", sym)
    overall_exit = _collect_one(args, sym)
    if overall_exit == EXIT_ROTATE:
        logger.info(
            "Exiting with code %d so the supervisor restarts us with a "
            "fresh HDF5 file",
            EXIT_ROTATE,
        )

    logger.info("collect_ticks exiting with code %d", overall_exit)
    return overall_exit


def upload_snapshot() -> int:
    """Upload a clean HDF5 snapshot to S3 without stopping the collector.

    Use this from a cron job or systemd timer to push fresh data to S3
    while collect_ticks.py is still running::

        # crontab — upload every 6 hours
        0 */6 * * *  cd /path/to/backtest && python collect_ticks.py --upload-snapshot \\
                         --symbol BTCUSDT --s3-bucket trading-data-centheos

    Because the collector is running during the upload we use
    :func:`_create_h5_snapshot` to produce a clean copy first.

    The Parquet mirror is the preferred durable store going forward —
    this snapshot upload is kept for ad-hoc full-HDF5 sync needs.
    """
    import argparse as _ap

    p = _ap.ArgumentParser(description="Upload HDF5 snapshot without stopping collector")
    p.add_argument("--s3-bucket", required=True)
    p.add_argument("--symbol", default="BTCUSDT",
                   help="Symbol whose HDF5 file to snapshot.")
    p.add_argument("--s3-key", default=None,
                   help="S3 key override (default: ticks/{SYMBOL}_ticks.h5).")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    _setup_logging(args.log_level)
    symbol = args.symbol.upper()
    store_path = _ticks_h5_path(args.data_dir, symbol)
    s3_key = args.s3_key or _default_s3_key(symbol)
    ok = _upload_to_s3(store_path, args.s3_bucket, s3_key, collector_running=True)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys as _sys
    if "--upload-snapshot" in _sys.argv:
        _sys.argv.remove("--upload-snapshot")
        _sys.exit(upload_snapshot())
    _sys.exit(main())
