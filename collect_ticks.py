"""Headless Parquet-only tick collector for EC2 (greenfield).

One process per ``(venue, symbol)``. Writes immutable Parquet shards under
``data/ticks/{venue}/{SYMBOL}/{dataset}/date=.../hour=.../`` and publishes
best-effort to Redis Streams ``md:{venue}:{kind}``.

Usage:
    python collect_ticks.py --symbol BTCUSDT
    python collect_ticks.py --symbols BTCUSDT,ETHUSDT   # forks one child each
    python collect_ticks.py --symbol BTCUSDT --health-check
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from data_service import TickDataCollector
from tick_parquet_store import (
    ImmutableShardWriter,
    S3ShardPublisher,
    evaluate_pipeline_health,
)

logger = logging.getLogger("collect_ticks")


def _setup_logging(level: str, log_dir: str = "logs", symbol: str = "") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    suffix = f"_{symbol.lower()}" if symbol else ""
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, f"collector{suffix}.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _parse_symbols(args: argparse.Namespace) -> List[str]:
    if args.symbols:
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.symbol:
        return [args.symbol.strip().upper()]
    return []


def run_health_check(
    *,
    data_dir: str,
    venue: str,
    symbol: str,
    max_staleness_seconds: float,
) -> int:
    """Exit 0 if Parquet shards for ``symbol`` are fresh; else 1."""
    root = os.path.join(data_dir, "ticks")
    writer = ImmutableShardWriter(root, venue, symbol)
    mtimes, days = writer.latest_shard_info()
    durable_m, durable_d = writer.durable_info()
    report = evaluate_pipeline_health(
        symbol,
        venue=venue,
        latest_shard_mtime=mtimes,
        latest_shard_day=days,
        durable_mtime=durable_m,
        durable_day=durable_d,
        now=datetime.now(timezone.utc),
        max_staleness_seconds=max_staleness_seconds,
        datasets=("trades",),  # depth may be quiet; trades are the heartbeat
    )
    for line in report.summary:
        logger.info("health: %s", line)
    for issue in report.issues:
        logger.error("health: %s", issue)
    if report.ok:
        logger.info("health: OK venue=%s symbol=%s", venue, symbol)
        return 0
    return 1


def _min_free_bytes() -> int:
    """Bytes that must stay free. Default 2 GB. Invalid env fails loud to 2."""
    raw = os.environ.get("MIN_FREE_DISK_GB", "2")
    try:
        gb = float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid MIN_FREE_DISK_GB=%r — using 2", raw)
        gb = 2.0
    if gb < 0:
        logger.warning("Negative MIN_FREE_DISK_GB=%r — using 2", raw)
        gb = 2.0
    return int(gb * 1024 ** 3)


def _run_one(
    *,
    venue: str,
    symbol: str,
    data_dir: str,
    duration: int,
    flush_interval: float,
    futures: bool,
    redis_url: Optional[str],
    s3_bucket: str,
) -> int:
    parquet_root = os.path.join(data_dir, "ticks")
    os.makedirs(parquet_root, exist_ok=True)
    publisher = S3ShardPublisher(s3_bucket) if s3_bucket else None
    writer = ImmutableShardWriter(
        parquet_root,
        venue,
        symbol,
        flush_interval_s=flush_interval,
        publisher=publisher,
        min_free_bytes=_min_free_bytes(),
    )

    stopping = {"flag": False}

    def _handle_sig(signum, _frame) -> None:
        logger.info("Received signal %s — flushing shards", signum)
        stopping["flag"] = True
        writer.stop()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    collector = TickDataCollector(
        exchange=venue,
        futures=futures,
        redis_url=redis_url,
        shard_writer=writer,
    )
    try:
        collector.collect(symbol, duration_seconds=duration)
    except Exception:
        logger.exception("Collector crashed venue=%s symbol=%s", venue, symbol)
        writer.stop()
        return 1
    return 0


def _spawn_children(symbols: List[str], argv_tail: List[str]) -> int:
    """One OS process per symbol so a crash cannot kill sibling feeds."""
    import subprocess

    procs = []
    for sym in symbols:
        cmd = [sys.executable, __file__, "--symbol", sym, *argv_tail]
        logger.info("Spawning isolated collector: %s", " ".join(cmd))
        procs.append(subprocess.Popen(cmd))

    codes = []
    try:
        for p in procs:
            codes.append(p.wait())
    except KeyboardInterrupt:
        for p in procs:
            p.send_signal(signal.SIGTERM)
        for p in procs:
            codes.append(p.wait())
    # Non-zero if any child failed.
    return 0 if codes and all(c == 0 for c in codes) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Parquet-only tick collector")
    p.add_argument("--symbol", default="", help="Single symbol (preferred)")
    p.add_argument(
        "--symbols",
        default="",
        help="Comma-separated; spawns one child process per symbol",
    )
    p.add_argument("--venue", default="binance", help="Venue id (default binance)")
    p.add_argument("--exchange", default="", help=argparse.SUPPRESS)  # alias
    p.add_argument("--data-dir", default="data")
    p.add_argument("--duration", type=int, default=0, help="Seconds; 0=forever")
    p.add_argument(
        "--parquet-flush-interval",
        type=float,
        default=float(os.environ.get("PARQUET_FLUSH_INTERVAL", "60")),
    )
    p.add_argument("--spot", action="store_true", help="Use Binance spot WS")
    p.add_argument("--s3-bucket", default=os.environ.get("S3_BUCKET", ""))
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    p.add_argument("--log-dir", default="logs")
    p.add_argument(
        "--health-check",
        action="store_true",
        help="Check Parquet shard freshness and exit",
    )
    p.add_argument(
        "--max-staleness-seconds",
        type=float,
        default=300.0,
        help="Health-check max shard age (seconds)",
    )
    p.add_argument(
        "--redis-url",
        default=os.environ.get("REDIS_URL", ""),
        help="ElastiCache / Redis URL for md:{venue}:* streams",
    )
    # Removed HDF5 knobs (accepted+ignored so old compose env doesn't crash).
    p.add_argument("--max-h5-gb", type=float, default=0, help=argparse.SUPPRESS)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    venue = (args.exchange or args.venue or "binance").lower()
    symbols = _parse_symbols(args)
    if not symbols:
        parser.error("Provide --symbol or --symbols")

    if args.health_check:
        _setup_logging(args.log_level, args.log_dir)
        # Multi-symbol: all must be healthy.
        rc = 0
        for sym in symbols:
            if run_health_check(
                data_dir=args.data_dir,
                venue=venue,
                symbol=sym,
                max_staleness_seconds=args.max_staleness_seconds,
            ) != 0:
                rc = 1
        return rc

    if len(symbols) > 1:
        _setup_logging(args.log_level, args.log_dir)
        # Re-pass flags except --symbols so each child runs one symbol.
        tail = [
            "--venue", venue,
            "--data-dir", args.data_dir,
            "--duration", str(args.duration),
            "--parquet-flush-interval", str(args.parquet_flush_interval),
            "--log-level", args.log_level,
            "--log-dir", args.log_dir,
        ]
        if args.spot:
            tail.append("--spot")
        if args.redis_url:
            tail.extend(["--redis-url", args.redis_url])
        if args.s3_bucket:
            tail.extend(["--s3-bucket", args.s3_bucket])
        return _spawn_children(symbols, tail)

    symbol = symbols[0]
    _setup_logging(args.log_level, args.log_dir, symbol=symbol)
    if args.s3_bucket:
        logger.info(
            "S3 bucket=%s — each closed shard is uploaded, confirmed, then deleted",
            args.s3_bucket,
        )
    else:
        logger.warning(
            "S3_BUCKET is empty — shards stay on local disk and will fill the volume"
        )
    return _run_one(
        venue=venue,
        symbol=symbol,
        data_dir=args.data_dir,
        duration=args.duration,
        flush_interval=args.parquet_flush_interval,
        futures=not args.spot,
        redis_url=args.redis_url or None,
        s3_bucket=args.s3_bucket or "",
    )


if __name__ == "__main__":
    sys.exit(main())
