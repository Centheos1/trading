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

def _upload_to_s3(local_path: str, bucket: str, key: str) -> bool:
    """Upload a file to S3 using boto3. Returns True on success."""
    try:
        import boto3
        s3 = boto3.client("s3")
        logger.info("Uploading %s → s3://%s/%s", local_path, bucket, key)
        s3.upload_file(local_path, bucket, key)
        logger.info("S3 upload complete: s3://%s/%s", bucket, key)
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
            ok = _upload_to_s3(store_path, args.s3_bucket, args.s3_key)
            if not ok:
                exit_code = max(exit_code, 2)

        logger.info("collect_ticks exiting with code %d", exit_code)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
