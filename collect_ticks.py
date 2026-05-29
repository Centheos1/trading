"""Headless tick data collector for EC2 deployment.

Wraps TickDataCollector from data_service.py with:
  - CLI argument parsing (no interactive prompts)
  - Optional S3 upload on clean exit
  - SIGTERM → graceful shutdown (flush → close → upload)
  - Structured logging to stdout + rotating file

Usage:
    python collect_ticks.py --symbol BTCUSDT
    python collect_ticks.py --symbol BTCUSDT --duration 3600 --s3-bucket my-bucket
    python collect_ticks.py --symbols BTCUSDT,ETHUSDT --log-level DEBUG
"""

import argparse
import asyncio
import logging
import logging.handlers
import os
import signal
import sys
import time

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
        import os

        upload_path = local_path

        if collector_running:
            snapshot = local_path + ".snapshot.h5"
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

        if upload_path != local_path and os.path.exists(upload_path):
            os.unlink(upload_path)

        return True
    except Exception as exc:
        logger.error("S3 upload failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Signal handling for SIGTERM (systemd / EC2 stop)
# ---------------------------------------------------------------------------

_shutdown_event: asyncio.Event | None = None


def _handle_sigterm(signum, frame):  # noqa: ANN001
    logger.info("SIGTERM received — initiating graceful shutdown")
    if _shutdown_event is not None:
        _shutdown_event.set()


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
        default="ticks/binance_ticks.h5",
        help="S3 object key for the uploaded HDF5 file.",
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
        help="Directory for HDF5 output files.",
    )
    return p.parse_args()


def main() -> int:
    global _shutdown_event

    args = _parse_args()
    _setup_logging(args.log_level)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",")]
        if args.symbols
        else [args.symbol.upper()]
    )

    logger.info(
        "collect_ticks starting | symbols=%s exchange=%s futures=%s duration=%ss",
        symbols,
        args.exchange,
        args.futures,
        args.duration if args.duration > 0 else "∞",
    )

    os.makedirs(args.data_dir, exist_ok=True)

    store_path = os.path.join(args.data_dir, f"{args.exchange}_ticks.h5")

    collector = TickDataCollector(
        exchange=args.exchange,
        futures=args.futures,
        store_path=store_path,
    )

    exit_code = 0
    try:
        if len(symbols) == 1:
            collector.collect(symbols[0], duration_seconds=args.duration)
        else:
            # Multi-symbol: run each sequentially in the same process.
            # For true parallel collection use multiple systemd service
            # instances via scripts/add_symbol.sh.
            for sym in symbols:
                logger.info("Starting collection for %s", sym)
                collector.collect(sym, duration_seconds=args.duration)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    except Exception as exc:
        logger.exception("Unhandled exception: %s", exc)
        exit_code = 1
    finally:
        logger.info("Flushing and closing tick store")
        try:
            collector.store.flush()
            collector.store.close()
        except Exception as exc:
            logger.error("Error closing tick store: %s", exc)

        if args.s3_bucket:
            # collector_running=False: store was flushed+closed above, so
            # the on-disk file is self-consistent and can be uploaded directly.
            ok = _upload_to_s3(
                store_path, args.s3_bucket, args.s3_key,
                collector_running=False,
            )
            if not ok:
                exit_code = max(exit_code, 2)

        logger.info("collect_ticks exiting with code %d", exit_code)

    return exit_code


def upload_snapshot() -> int:
    """Upload a clean HDF5 snapshot to S3 without stopping the collector.

    Use this from a cron job or systemd timer to push fresh data to S3
    while collect_ticks.py is still running::

        # crontab — upload every 6 hours
        0 */6 * * *  cd /path/to/backtest && python collect_ticks.py --upload-snapshot \\
                         --s3-bucket trading-data-centheos

    Because the collector is running during the upload we use
    :func:`_create_h5_snapshot` to produce a clean copy first.
    """
    import argparse as _ap

    p = _ap.ArgumentParser(description="Upload HDF5 snapshot without stopping collector")
    p.add_argument("--s3-bucket", required=True)
    p.add_argument("--s3-key", default="ticks/binance_ticks.h5")
    p.add_argument("--exchange", default="binance")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    _setup_logging(args.log_level)
    store_path = os.path.join(args.data_dir, f"{args.exchange}_ticks.h5")
    ok = _upload_to_s3(store_path, args.s3_bucket, args.s3_key, collector_running=True)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys as _sys
    if "--upload-snapshot" in _sys.argv:
        _sys.argv.remove("--upload-snapshot")
        _sys.exit(upload_snapshot())
    _sys.exit(main())
