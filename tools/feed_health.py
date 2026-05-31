"""Feed health monitor and data quality report.

Answers three questions about the running data feeds:
  1. Is the tick feed alive? (staleness, row counts)
  2. How much data do I have? (date range, file size)
  3. Are there gaps? (contiguous breaks above threshold)

Usage:
    python tools/feed_health.py                       # all reports (human-readable)
    python tools/feed_health.py --report tick
    python tools/feed_health.py --report ohlcv --exchange binance
    python tools/feed_health.py --report all --json
    python tools/feed_health.py --gaps-only --gap-threshold-minutes 10

    # On EC2 against S3 (no download needed)
    DATA_STORE=s3 S3_BUCKET=trading-data-centheos python tools/feed_health.py --report ohlcv
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# Ensure project root is on sys.path so ohlcv_store (and other modules) import correctly
# regardless of where this script is launched from.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TickSymbolHealth:
    symbol: str
    file_path: str
    file_size_bytes: int
    trade_count: int
    depth_update_count: int
    depth_snapshot_count: int
    first_ts_ms: Optional[int]
    last_ts_ms: Optional[int]
    stale_seconds: Optional[float]  # seconds since last file write
    ok: bool
    error: Optional[str] = None

    @property
    def first_dt(self) -> Optional[str]:
        if self.first_ts_ms is None:
            return None
        return _ms_to_str(self.first_ts_ms)

    @property
    def last_dt(self) -> Optional[str]:
        if self.last_ts_ms is None:
            return None
        return _ms_to_str(self.last_ts_ms)

    @property
    def duration_hours(self) -> Optional[float]:
        if self.first_ts_ms is None or self.last_ts_ms is None:
            return None
        return (self.last_ts_ms - self.first_ts_ms) / 3_600_000


@dataclass
class OhlcvGap:
    after_ts_ms: int
    gap_minutes: float

    @property
    def after_dt(self) -> str:
        return _ms_to_str(self.after_ts_ms)


@dataclass
class OhlcvSymbolHealth:
    exchange: str
    symbol: str
    timeframe: str
    row_count: int
    first_ts_ms: Optional[int]
    last_ts_ms: Optional[int]
    file_size_bytes: Optional[int]  # None for S3
    gap_count: int
    largest_gap_minutes: float
    unexpected_gaps: List[OhlcvGap] = field(default_factory=list)
    ok: bool = True
    error: Optional[str] = None

    @property
    def first_dt(self) -> Optional[str]:
        return _ms_to_str(self.first_ts_ms) if self.first_ts_ms else None

    @property
    def last_dt(self) -> Optional[str]:
        return _ms_to_str(self.last_ts_ms) if self.last_ts_ms else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ms_to_str(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TB"


def _fmt_rows(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _is_oanda_weekend_gap(after_ts_ms: int, gap_ms: int) -> bool:
    """Return True if a gap spans a Sat/Sun (Oanda market closed)."""
    start_dt = datetime.fromtimestamp(after_ts_ms / 1000, tz=timezone.utc)
    end_dt = datetime.fromtimestamp((after_ts_ms + gap_ms) / 1000, tz=timezone.utc)
    # Walk through the gap in 1-hour steps and check for Saturday/Sunday
    cursor = start_dt
    while cursor <= end_dt:
        if cursor.weekday() in (5, 6):  # Saturday=5, Sunday=6
            return True
        cursor += timedelta(hours=1)
    return False


def _is_oanda_daily_close(after_ts_ms: int, gap_ms: int) -> bool:
    """Return True if the gap falls within the Oanda daily close window
    (approximately 17:00–17:15 ET = 22:00–22:15 UTC)."""
    start_dt = datetime.fromtimestamp(after_ts_ms / 1000, tz=timezone.utc)
    # Daily close at ~22:00 UTC (17:00 ET)
    close_hour = 22
    gap_minutes = gap_ms / 60_000
    return start_dt.hour == close_hour and gap_minutes <= 20


# ---------------------------------------------------------------------------
# Tick health reader
# ---------------------------------------------------------------------------

def _find_tick_files(data_dir: str) -> List[Tuple[str, str]]:
    """Return list of (symbol, path) for all tick HDF5 files.

    Looks in (current) ``data/ticks/{SYMBOL}_ticks.h5`` first, then
    the two legacy layouts (``data/binance_ticks.h5`` single-file and
    ``data/{SYMBOL}/binance_ticks.h5`` per-symbol-subdir) so older
    deployments still report correctly.
    """
    results: List[Tuple[str, str]] = []

    # Current layout: data/ticks/{SYMBOL}_ticks.h5
    ticks_dir = os.path.join(data_dir, "ticks")
    if os.path.isdir(ticks_dir):
        for entry in sorted(os.listdir(ticks_dir)):
            if entry.endswith("_ticks.h5"):
                sym = entry[:-len("_ticks.h5")]
                results.append((sym, os.path.join(ticks_dir, entry)))

    # Legacy single-file layout: data/binance_ticks.h5
    default = os.path.join(data_dir, "binance_ticks.h5")
    if os.path.exists(default):
        results.append(("DEFAULT", default))
    # Legacy per-symbol-subdir layout: data/{SYMBOL}/binance_ticks.h5
    for entry in sorted(os.listdir(data_dir)):
        candidate = os.path.join(data_dir, entry, "binance_ticks.h5")
        if os.path.isfile(candidate):
            results.append((entry, candidate))
    return results


def _read_tick_health(path: str) -> List[TickSymbolHealth]:
    """Read all symbol groups from a tick HDF5 file."""
    results: List[TickSymbolHealth] = []
    try:
        import h5py
    except ImportError:
        return []

    try:
        stat = os.stat(path)
        file_size = stat.st_size
        stale_seconds = time.time() - stat.st_mtime
    except OSError as exc:
        return [TickSymbolHealth(
            symbol="?", file_path=path, file_size_bytes=0,
            trade_count=0, depth_update_count=0, depth_snapshot_count=0,
            first_ts_ms=None, last_ts_ms=None, stale_seconds=None,
            ok=False, error=str(exc),
        )]

    try:
        with h5py.File(path, "r") as f:
            for sym in f.keys():
                grp = f[sym]
                trades = grp.get("trades")
                depth_u = grp.get("depth_updates")
                depth_s = grp.get("depth_snapshots")
                t_count = trades.shape[0] if trades is not None else 0
                du_count = depth_u.shape[0] if depth_u is not None else 0
                ds_count = depth_s.shape[0] if depth_s is not None else 0

                first_ts: Optional[int] = None
                last_ts: Optional[int] = None
                if trades is not None and t_count > 0:
                    first_ts = int(trades[0][0])
                    last_ts = int(trades[-1][0])

                results.append(TickSymbolHealth(
                    symbol=sym,
                    file_path=path,
                    file_size_bytes=file_size,
                    trade_count=t_count,
                    depth_update_count=du_count,
                    depth_snapshot_count=ds_count,
                    first_ts_ms=first_ts,
                    last_ts_ms=last_ts,
                    stale_seconds=stale_seconds,
                    ok=True,
                ))
    except Exception as exc:  # noqa: BLE001
        results.append(TickSymbolHealth(
            symbol="?", file_path=path, file_size_bytes=file_size,
            trade_count=0, depth_update_count=0, depth_snapshot_count=0,
            first_ts_ms=None, last_ts_ms=None, stale_seconds=stale_seconds,
            ok=False, error=str(exc),
        ))

    return results


def collect_tick_report(data_dir: str = "data") -> List[TickSymbolHealth]:
    tick_files = _find_tick_files(data_dir)
    all_results: List[TickSymbolHealth] = []
    for _label, path in tick_files:
        all_results.extend(_read_tick_health(path))
    return all_results


# ---------------------------------------------------------------------------
# OHLCV health reader
# ---------------------------------------------------------------------------

_TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# Oanda exchanges — use market-hours gap suppression
_OANDA_EXCHANGES = {"oanda"}


def _detect_gaps(
    timestamps_ms: list,
    tf_ms: int,
    gap_threshold_ms: int,
    exchange: str,
) -> List[OhlcvGap]:
    """Return list of unexpected gaps in the timestamp series."""
    gaps: List[OhlcvGap] = []
    is_oanda = exchange.lower() in _OANDA_EXCHANGES
    for i in range(1, len(timestamps_ms)):
        diff = timestamps_ms[i] - timestamps_ms[i - 1]
        if diff <= gap_threshold_ms:
            continue
        # Gap detected — check if it's expected for Oanda
        if is_oanda:
            if _is_oanda_weekend_gap(timestamps_ms[i - 1], diff):
                continue
            if _is_oanda_daily_close(timestamps_ms[i - 1], diff):
                continue
        gaps.append(OhlcvGap(
            after_ts_ms=timestamps_ms[i - 1],
            gap_minutes=diff / 60_000,
        ))
    return gaps


def collect_ohlcv_report(
    exchange_filter: Optional[str] = None,
    timeframe: str = "1m",
    gap_threshold_minutes: float = 5.0,
    oanda_gap_threshold_minutes: float = 120.0,
    data_dir: str = "data",
) -> List[OhlcvSymbolHealth]:
    import pandas as pd

    try:
        from ohlcv_store import get_ohlcv_store
        store = get_ohlcv_store(data_dir=data_dir)
    except Exception as exc:  # noqa: BLE001
        return [OhlcvSymbolHealth(
            exchange="?", symbol="?", timeframe=timeframe,
            row_count=0, first_ts_ms=None, last_ts_ms=None,
            file_size_bytes=None, gap_count=0, largest_gap_minutes=0.0,
            ok=False, error=f"store init failed: {exc}",
        )]

    tf_ms = _TF_MS.get(timeframe, 60_000)
    results: List[OhlcvSymbolHealth] = []

    # Determine exchanges to scan
    backend_root = getattr(store, "root", None)  # local only
    backend_bucket = getattr(store, "bucket", None)  # s3 only

    if backend_root:
        ohlcv_root = os.path.join(backend_root, "ohlcv")
        if not os.path.isdir(ohlcv_root):
            return []
        exchanges = sorted(os.listdir(ohlcv_root))
    elif backend_bucket:
        # List exchanges from S3
        try:
            fs = store._fs()
            entries = fs.ls(f"{backend_bucket}/ohlcv/", detail=False)
            exchanges = [e.rstrip("/").rsplit("/", 1)[-1] for e in entries]
        except Exception:  # noqa: BLE001
            exchanges = []
    else:
        exchanges = []

    if exchange_filter:
        exchanges = [e for e in exchanges if e.lower() == exchange_filter.lower()]

    for exchange in exchanges:
        is_oanda = exchange.lower() in _OANDA_EXCHANGES
        threshold_ms = int(
            (oanda_gap_threshold_minutes if is_oanda else gap_threshold_minutes)
            * 60_000
        )
        symbols = store.list_symbols(exchange, timeframe)
        for symbol in symbols:
            try:
                # Read only the timestamp column for efficiency
                path = store.path_for(exchange, symbol, timeframe)
                if backend_root:
                    df_ts = pd.read_parquet(path, columns=["timestamp"])
                    file_size: Optional[int] = os.path.getsize(path) if os.path.exists(path) else None
                else:
                    df_ts = pd.read_parquet(
                        path, columns=["timestamp"],
                        storage_options={"anon": False}
                    )
                    file_size = None

                if df_ts.empty:
                    results.append(OhlcvSymbolHealth(
                        exchange=exchange, symbol=symbol, timeframe=timeframe,
                        row_count=0, first_ts_ms=None, last_ts_ms=None,
                        file_size_bytes=file_size, gap_count=0,
                        largest_gap_minutes=0.0, ok=True,
                    ))
                    continue

                ts_sorted = sorted(df_ts["timestamp"].tolist())
                row_count = len(ts_sorted)
                first_ts = int(ts_sorted[0])
                last_ts = int(ts_sorted[-1])

                gaps = _detect_gaps(ts_sorted, tf_ms, threshold_ms, exchange)
                largest = max((g.gap_minutes for g in gaps), default=0.0)

                results.append(OhlcvSymbolHealth(
                    exchange=exchange, symbol=symbol, timeframe=timeframe,
                    row_count=row_count,
                    first_ts_ms=first_ts, last_ts_ms=last_ts,
                    file_size_bytes=file_size,
                    gap_count=len(gaps),
                    largest_gap_minutes=largest,
                    unexpected_gaps=gaps[:5],  # top 5 for display
                    ok=True,
                ))
            except Exception as exc:  # noqa: BLE001
                results.append(OhlcvSymbolHealth(
                    exchange=exchange, symbol=symbol, timeframe=timeframe,
                    row_count=0, first_ts_ms=None, last_ts_ms=None,
                    file_size_bytes=None, gap_count=0, largest_gap_minutes=0.0,
                    ok=False, error=str(exc),
                ))

    return results


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

_OK = "✓"
_WARN = "⚠"
_ERR = "✗"


def _stale_str(s: Optional[float]) -> str:
    if s is None:
        return "?"
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    if s < 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


def print_tick_report(items: List[TickSymbolHealth], gaps_only: bool = False) -> None:
    if not items:
        print("  (no tick files found in data/)")
        return
    print("\n══ Tick feed health " + "═" * 40)
    for h in items:
        if gaps_only:
            continue
        if not h.ok:
            print(f"  {h.symbol:<12} {_ERR}  {h.error}")
            continue
        stale_ok = h.stale_seconds is not None and h.stale_seconds < 120
        status = _OK if stale_ok else _WARN
        print(
            f"  {h.symbol:<12}"
            f"  first: {h.first_dt or '—':<16}"
            f"  last: {h.last_dt or '—':<16}"
            f"  trades: {_fmt_rows(h.trade_count):<8}"
            f"  depth: {_fmt_rows(h.depth_update_count):<8}"
            f"  size: {_fmt_bytes(h.file_size_bytes):<8}"
            f"  stale: {_stale_str(h.stale_seconds):<6}"
            f"  {status}"
        )


def print_ohlcv_report(
    items: List[OhlcvSymbolHealth],
    gaps_only: bool = False,
) -> None:
    if not items:
        print("  (no OHLCV Parquet files found)")
        return

    # Group by exchange
    by_exchange: Dict[str, List[OhlcvSymbolHealth]] = {}
    for h in items:
        by_exchange.setdefault(h.exchange, []).append(h)

    for exchange, sym_list in sorted(by_exchange.items()):
        sym_list_filtered = [h for h in sym_list if not gaps_only or h.gap_count > 0]
        if not sym_list_filtered:
            continue
        print(f"\n══ OHLCV health ({exchange}, {sym_list[0].timeframe}) " + "═" * 30)
        for h in sym_list_filtered:
            if not h.ok:
                print(f"  {h.symbol:<14} {_ERR}  {h.error}")
                continue
            status = _OK if h.gap_count == 0 else _WARN
            gap_str = (
                "no gaps"
                if h.gap_count == 0
                else f"gaps: {h.gap_count} (max {h.largest_gap_minutes:.0f}m)"
            )
            size_str = _fmt_bytes(h.file_size_bytes) if h.file_size_bytes else "S3"
            print(
                f"  {h.symbol:<14}"
                f"  {(h.first_dt or '—')[:10]} → {(h.last_dt or '—')[:10]}"
                f"  {_fmt_rows(h.row_count):<9}"
                f"  {size_str:<9}"
                f"  {gap_str:<30}"
                f"  {status}"
            )
            if h.gap_count > 0 and not gaps_only:
                for g in h.unexpected_gaps[:3]:
                    print(f"    {'':16} gap after {g.after_dt}: {g.gap_minutes:.0f}m")


def to_json_report(
    tick: List[TickSymbolHealth],
    ohlcv: List[OhlcvSymbolHealth],
) -> str:
    def _tick_dict(h: TickSymbolHealth) -> dict:
        return {
            "symbol": h.symbol,
            "file_path": h.file_path,
            "file_size_bytes": h.file_size_bytes,
            "trade_count": h.trade_count,
            "depth_update_count": h.depth_update_count,
            "depth_snapshot_count": h.depth_snapshot_count,
            "first_ts_ms": h.first_ts_ms,
            "last_ts_ms": h.last_ts_ms,
            "first_dt": h.first_dt,
            "last_dt": h.last_dt,
            "stale_seconds": round(h.stale_seconds, 1) if h.stale_seconds is not None else None,
            "ok": h.ok,
            "error": h.error,
        }

    def _ohlcv_dict(h: OhlcvSymbolHealth) -> dict:
        return {
            "exchange": h.exchange,
            "symbol": h.symbol,
            "timeframe": h.timeframe,
            "row_count": h.row_count,
            "first_ts_ms": h.first_ts_ms,
            "last_ts_ms": h.last_ts_ms,
            "first_dt": h.first_dt,
            "last_dt": h.last_dt,
            "file_size_bytes": h.file_size_bytes,
            "gap_count": h.gap_count,
            "largest_gap_minutes": round(h.largest_gap_minutes, 1),
            "unexpected_gaps": [
                {"after_dt": g.after_dt, "gap_minutes": round(g.gap_minutes, 1)}
                for g in h.unexpected_gaps
            ],
            "ok": h.ok,
            "error": h.error,
        }

    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "tick": [_tick_dict(h) for h in tick],
        "ohlcv": [_ohlcv_dict(h) for h in ohlcv],
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Feed health monitor and OHLCV data quality report.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--report",
        default="all",
        choices=["tick", "ohlcv", "all"],
        help="Which report(s) to produce.",
    )
    p.add_argument(
        "--exchange",
        default=None,
        help="Filter OHLCV report to a single exchange (e.g. binance, oanda).",
    )
    p.add_argument(
        "--timeframe",
        default="1m",
        choices=list(_TF_MS.keys()),
    )
    p.add_argument(
        "--gap-threshold-minutes",
        type=float,
        default=5.0,
        help="Binance gap alert threshold in minutes.",
    )
    p.add_argument(
        "--oanda-gap-threshold-minutes",
        type=float,
        default=120.0,
        help="Oanda gap alert threshold in minutes (weekends/daily-close are suppressed).",
    )
    p.add_argument(
        "--gaps-only",
        action="store_true",
        help="Print only symbols/files with detected gaps or staleness.",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON instead of human-readable tables.",
    )
    p.add_argument(
        "--data-dir",
        default="data",
        help="Root data directory for local Parquet and HDF5 files.",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()

    tick_items: List[TickSymbolHealth] = []
    ohlcv_items: List[OhlcvSymbolHealth] = []

    if args.report in ("tick", "all"):
        tick_items = collect_tick_report(data_dir=args.data_dir)

    if args.report in ("ohlcv", "all"):
        ohlcv_items = collect_ohlcv_report(
            exchange_filter=args.exchange,
            timeframe=args.timeframe,
            gap_threshold_minutes=args.gap_threshold_minutes,
            oanda_gap_threshold_minutes=args.oanda_gap_threshold_minutes,
            data_dir=args.data_dir,
        )

    if args.json:
        print(to_json_report(tick_items, ohlcv_items))
        return 0

    if args.report in ("tick", "all"):
        print_tick_report(tick_items, gaps_only=args.gaps_only)

    if args.report in ("ohlcv", "all"):
        print_ohlcv_report(ohlcv_items, gaps_only=args.gaps_only)

    print()

    # Exit code: 1 if any item has gaps or is not ok
    any_issue = any(not h.ok or (h.stale_seconds or 0) > 120 for h in tick_items)
    any_issue = any_issue or any(not h.ok or h.gap_count > 0 for h in ohlcv_items)
    return 1 if any_issue else 0


if __name__ == "__main__":
    sys.exit(main())
