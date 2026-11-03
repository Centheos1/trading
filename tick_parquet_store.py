"""Immutable Parquet shard store for live tick collection (greenfield).

Layout::

    {root}/{venue}/{SYMBOL}/{dataset}/date=YYYY-MM-DD/hour=HH/
        part-{start_ms}-{end_ms}-{uuid}.parquet

Shards are write-once: buffer → temp → fsync → atomic rename. A crash
loses at most the open buffer; prior shards are never overwritten.

Schema (trades)::

    venue, timestamp, price, quantity, is_buyer_maker, ts_recv_ms, trade_id

    ``timestamp`` is the exchange time. ``ts_recv_ms`` is the local receipt
    time. ``trade_id`` is the venue trade id when the adapter has one.

Schema (depth_updates)::

    venue, timestamp, side (0=bid 1=ask), price, quantity,
    ts_recv_ms, first_update_id, final_update_id, last_update_id

    ``timestamp`` is the exchange event time. ``first_update_id`` and
    ``final_update_id`` are the packet's ``U`` and ``u``. ``last_update_id``
    is null on a diff.

Schema (depth_snapshots)::

    the same depth columns. ``timestamp`` is null: a REST book has a
    ``last_update_id`` and no exchange event time. ``ts_recv_ms`` is the
    local receipt time and is the shard clock.

Shards written before this schema do not have the new columns. They cannot
be backfilled. A depth event with no usable sequence id is not written into
the book datasets; the levels are stored under ``quarantine`` and counted.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DATASETS: Tuple[str, ...] = ("trades", "depth_snapshots", "depth_updates")

_TRADES_COLS = [
    "venue", "timestamp", "price", "quantity", "is_buyer_maker", "ts_recv_ms", "trade_id",
]
_DEPTH_COLS = [
    "venue", "timestamp", "side", "price", "quantity",
    "ts_recv_ms", "first_update_id", "final_update_id", "last_update_id",
]
_QUARANTINE_COLS = [
    "venue", "kind", "reason", "timestamp", "ts_recv_ms",
    "side", "price", "quantity", "trade_id",
    "first_update_id", "final_update_id", "last_update_id",
]


def _columns_for(dataset: str) -> List[str]:
    if dataset == "trades":
        return list(_TRADES_COLS)
    if dataset == "quarantine":
        return list(_QUARANTINE_COLS)
    return list(_DEPTH_COLS)


def _arrow_schema(dataset: str):
    import pyarrow as pa

    if dataset == "trades":
        return pa.schema([
            ("venue", pa.string()),
            ("timestamp", pa.int64()),
            ("price", pa.float64()),
            ("quantity", pa.float64()),
            ("is_buyer_maker", pa.bool_()),
            pa.field("ts_recv_ms", pa.int64(), nullable=True),
            pa.field("trade_id", pa.int64(), nullable=True),
        ])
    if dataset == "quarantine":
        return pa.schema([
            ("venue", pa.string()),
            ("kind", pa.string()),
            ("reason", pa.string()),
            pa.field("timestamp", pa.int64(), nullable=True),
            pa.field("ts_recv_ms", pa.int64(), nullable=True),
            pa.field("side", pa.int8(), nullable=True),
            pa.field("price", pa.float64(), nullable=True),
            pa.field("quantity", pa.float64(), nullable=True),
            pa.field("trade_id", pa.int64(), nullable=True),
            pa.field("first_update_id", pa.int64(), nullable=True),
            pa.field("final_update_id", pa.int64(), nullable=True),
            pa.field("last_update_id", pa.int64(), nullable=True),
        ])
    return pa.schema([
        ("venue", pa.string()),
        pa.field("timestamp", pa.int64(), nullable=True),
        ("side", pa.int8()),
        ("price", pa.float64()),
        ("quantity", pa.float64()),
        pa.field("ts_recv_ms", pa.int64(), nullable=True),
        pa.field("first_update_id", pa.int64(), nullable=True),
        pa.field("final_update_id", pa.int64(), nullable=True),
        pa.field("last_update_id", pa.int64(), nullable=True),
    ])


def _positive_int(value: Any) -> Optional[int]:
    """A venue id or millisecond clock. Zero and unparsable values are absent."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class PipelineHealth:
    """Result of :func:`evaluate_pipeline_health`."""

    ok: bool
    issues: List[str] = field(default_factory=list)
    summary: List[str] = field(default_factory=list)


def evaluate_pipeline_health(
    symbol: str,
    *,
    venue: str = "binance",
    exchange: Optional[str] = None,
    latest_shard_mtime: Dict[str, Optional[float]],
    latest_shard_day: Dict[str, Optional[str]],
    now: datetime,
    max_staleness_seconds: float = 300.0,
    max_staleness_hours: float = 2.0,
    datasets: Iterable[str] = DATASETS,
    durable_mtime: Optional[Dict[str, Optional[float]]] = None,
    durable_day: Optional[Dict[str, Optional[str]]] = None,
    **_compat: Any,
) -> PipelineHealth:
    """Parquet-only health: freshness of the newest durable upload or local shard.

    A confirmed S3 upload writes a watermark and then deletes the local file.
    Health stays green from that watermark. A not-yet-uploaded local shard is
    also fresh. Missing both means the dataset has never been flushed.

    Parameters
    ----------
    latest_shard_mtime
        ``dataset -> unix mtime`` of newest local shard, or ``None``.
    latest_shard_day
        ``dataset -> "YYYY-MM-DD"`` of newest partition, or ``None``.
    durable_mtime
        ``dataset -> unix time`` of the last confirmed S3 upload, or ``None``.
    durable_day
        ``dataset -> "YYYY-MM-DD"`` recorded on that upload.
    max_staleness_seconds
        Max age of the freshest signal for a live feed (default 5 min).
    max_staleness_hours
        Grace after UTC midnight for yesterday's day partition.
    """
    ven = (exchange or venue).lower()
    sym = symbol.upper()
    issues: List[str] = []
    summary: List[str] = []
    durable_mtime = durable_mtime or {}
    durable_day = durable_day or {}

    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hours_into_day = (now - midnight).total_seconds() / 3600.0
    today = now.date()
    now_ts = now.timestamp()

    for d in datasets:
        mtime = latest_shard_mtime.get(d)
        day = latest_shard_day.get(d)
        wm_m = durable_mtime.get(d)
        wm_d = durable_day.get(d)
        if wm_m is not None and (mtime is None or float(wm_m) >= float(mtime)):
            mtime = float(wm_m)
            day = wm_d or day
        age_s = None if mtime is None else now_ts - float(mtime)
        summary.append(
            f"{ven}/{sym}/{d}: latest_day={day or 'none'} "
            f"age_s={'?' if age_s is None else f'{age_s:.0f}'}"
        )

        if day is None or mtime is None:
            issues.append(
                f"{ven}/{sym}/{d}: no durable upload or local shard — collector "
                f"has never flushed this dataset"
            )
            continue

        if age_s is not None and age_s > max_staleness_seconds:
            issues.append(
                f"{ven}/{sym}/{d}: newest shard is stale "
                f"(age {age_s:.0f}s > {max_staleness_seconds:.0f}s) — "
                f"feed or flush has stalled"
            )

        try:
            day_date = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            issues.append(
                f"{ven}/{sym}/{d}: unparseable shard day {day!r}"
            )
            continue

        age_days = (today - day_date).days
        day_stale = age_days >= 2 or (
            age_days == 1 and hours_into_day > max_staleness_hours
        )
        if day_stale:
            issues.append(
                f"{ven}/{sym}/{d}: newest shard day {day} is stale "
                f"(age {age_days}d, {hours_into_day:.1f}h into UTC day)"
            )

    return PipelineHealth(ok=not issues, issues=issues, summary=summary)


# ---------------------------------------------------------------------------
# Durable upload: confirm in S3, then delete the local shard
# ---------------------------------------------------------------------------


class DiskFloorError(RuntimeError):
    """Free space is below ``MIN_FREE_DISK_GB``. The shard was not written."""


@dataclass
class SweepStats:
    """Result of :func:`sweep_local_shards`."""

    uploaded: int = 0
    deleted: int = 0
    kept: int = 0
    failed: int = 0


def watermark_path(root: Path, venue: str, symbol: str, dataset: str) -> Path:
    return (
        Path(root)
        / ".durable"
        / venue.lower()
        / symbol.upper()
        / f"{dataset}.json"
    )


def write_durable_watermark(
    root: Path,
    venue: str,
    symbol: str,
    dataset: str,
    *,
    key: str,
    rows: int,
    day: Optional[str],
    when: Optional[float] = None,
) -> None:
    """Record a confirmed upload. Atomic replace so a crash cannot half-write it."""
    path = watermark_path(root, venue, symbol, dataset)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_upload_unix": float(when if when is not None else datetime.now(timezone.utc).timestamp()),
        "key": key,
        "rows": int(rows),
        "day": day,
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, path)


def read_durable_watermark(
    root: Path, venue: str, symbol: str, dataset: str
) -> Optional[dict]:
    path = watermark_path(root, venue, symbol, dataset)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Unreadable watermark %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or "last_upload_unix" not in data:
        logger.warning("Watermark %s missing last_upload_unix — ignoring", path)
        return None
    return data


def _day_from_shard_path(path: Path) -> Optional[str]:
    for part in path.parts:
        if part.startswith("date="):
            return part.split("=", 1)[1]
    return None


def _dataset_from_shard_path(path: Path) -> Optional[str]:
    parts = path.parts
    for i, part in enumerate(parts):
        if part.startswith("date=") and i >= 1:
            return parts[i - 1]
    return None


def _is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, KeyError):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound"}
    return False


class S3ShardPublisher:
    """Upload a closed shard and confirm the stored byte count."""

    def __init__(self, bucket: str, *, prefix: str = "ticks", client: Any = None) -> None:
        if not bucket:
            raise ValueError("S3ShardPublisher requires a bucket")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = client

    def client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("s3")
        return self._client

    def object_key(self, root: Path, local: Path) -> str:
        rel = Path(local).resolve().relative_to(Path(root).resolve()).as_posix()
        return f"{self.prefix}/{rel}"

    def head_size(self, key: str) -> Optional[int]:
        try:
            head = self.client().head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        return int(head["ContentLength"])

    def publish(self, local: Path, key: str) -> int:
        """Upload ``local`` and raise unless S3 reports the same size."""
        size = Path(local).stat().st_size
        self.client().upload_file(str(local), self.bucket, key)
        remote = self.head_size(key)
        if remote != size:
            raise RuntimeError(
                f"S3 size mismatch key={key} local={size} remote={remote}"
            )
        return int(remote)


def sweep_local_shards(root: Path, publisher: S3ShardPublisher) -> SweepStats:
    """Upload leftover shards and delete a file only when S3 size matches.

    A size mismatch is kept and counted. Upload errors are counted and the
    file stays. Every delete is logged.
    """
    stats = SweepStats()
    root = Path(root)
    if not root.is_dir():
        logger.warning("Sweep root %s does not exist — nothing to do", root)
        return stats
    for path in sorted(root.rglob("part-*.parquet")):
        try:
            local_size = path.stat().st_size
            key = publisher.object_key(root, path)
            remote = publisher.head_size(key)
            if remote is None:
                publisher.publish(path, key)
                stats.uploaded += 1
                remote = local_size
            if remote == local_size:
                path.unlink()
                stats.deleted += 1
                logger.info("Deleted confirmed shard %s (%d bytes)", key, local_size)
            else:
                stats.kept += 1
                logger.error(
                    "Keeping shard %s — size mismatch local=%d remote=%s",
                    key, local_size, remote,
                )
        except Exception:
            stats.failed += 1
            logger.exception("Sweep failed for %s — file kept", path)
    return stats


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _depth_level_rows(
    venue: str,
    ts_ms: Optional[int],
    bids: Sequence,
    asks: Sequence,
    *,
    ts_recv_ms: Optional[int],
    first_update_id: Optional[int] = None,
    final_update_id: Optional[int] = None,
    last_update_id: Optional[int] = None,
) -> List[dict]:
    rows: List[dict] = []
    for side, levels in ((0, bids), (1, asks)):
        for level in levels:
            if isinstance(level, (list, tuple)):
                price, quantity = float(level[0]), float(level[1])
            else:
                price, quantity = float(level.price), float(level.quantity)
            rows.append({
                "venue": venue,
                "timestamp": None if ts_ms is None else int(ts_ms),
                "side": np.int8(side),
                "price": price,
                "quantity": quantity,
                "ts_recv_ms": ts_recv_ms,
                "first_update_id": first_update_id,
                "final_update_id": final_update_id,
                "last_update_id": last_update_id,
            })
    return rows


def _table_for_schema(frame: pd.DataFrame, schema):
    import pyarrow as pa

    arrays = [
        pa.array(frame[field.name].tolist(), type=field.type, from_pandas=True)
        for field in schema
    ]
    return pa.Table.from_arrays(arrays, schema=schema)


def _placement_clock(frame: pd.DataFrame) -> pd.Series:
    """Exchange time, or receipt time when the exchange time is absent."""
    clock = pd.to_numeric(frame["timestamp"], errors="coerce")
    if "ts_recv_ms" in frame.columns:
        clock = clock.fillna(pd.to_numeric(frame["ts_recv_ms"], errors="coerce"))
    return clock


def shard_dir(
    root: Path, venue: str, symbol: str, dataset: str, ts_ms: int
) -> Path:
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    return (
        root
        / venue.lower()
        / symbol.upper()
        / dataset
        / f"date={dt.strftime('%Y-%m-%d')}"
        / f"hour={dt.strftime('%H')}"
    )


def shard_filename(start_ms: int, end_ms: int, part_id: Optional[str] = None) -> str:
    uid = part_id or uuid.uuid4().hex[:12]
    return f"part-{start_ms}-{end_ms}-{uid}.parquet"


# ---------------------------------------------------------------------------
# Immutable shard writer
# ---------------------------------------------------------------------------


class ImmutableShardWriter:
    """Buffer MarketEvent rows and flush immutable Parquet shards.

    Thread-safe for concurrent ``add_*`` from WS reader threads and a
    periodic flush thread.
    """

    def __init__(
        self,
        root: str | Path,
        venue: str,
        symbol: str,
        *,
        flush_interval_s: float = 60.0,
        max_buffer_rows: int = 50_000,
        publisher: Optional[S3ShardPublisher] = None,
        min_free_bytes: int = 0,
        free_bytes: Optional[Callable[[], int]] = None,
    ) -> None:
        self.root = Path(root)
        self.venue = venue.lower()
        self.symbol = symbol.upper()
        self.flush_interval_s = float(flush_interval_s)
        self.max_buffer_rows = int(max_buffer_rows)
        self._publisher = publisher
        self.min_free_bytes = int(min_free_bytes)
        self._free_bytes_fn = free_bytes
        self._lock = threading.Lock()
        self._bufs: Dict[str, List[dict]] = defaultdict(list)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_event_ms: Dict[str, int] = {}
        self._shards_written = 0
        self._rows_written = 0
        self._rows_dropped = 0
        self._rows_quarantined = 0
        self._events_without_levels = 0

    # -- ingest -------------------------------------------------------------

    def add_trade(
        self,
        ts_ms: int,
        price: float,
        quantity: float,
        is_buyer_maker: bool,
        *,
        ts_recv_ms: Optional[int] = None,
        trade_id: Optional[int] = None,
    ) -> None:
        row = {
            "venue": self.venue,
            "timestamp": int(ts_ms),
            "price": float(price),
            "quantity": float(quantity),
            "is_buyer_maker": bool(is_buyer_maker),
            "ts_recv_ms": ts_recv_ms,
            "trade_id": trade_id,
        }
        with self._lock:
            self._bufs["trades"].append(row)
            self._last_event_ms["trades"] = int(ts_ms)
            overflow = len(self._bufs["trades"]) >= self.max_buffer_rows
        if overflow:
            self.flush()

    def add_depth(
        self,
        ts_ms: Optional[int],
        bids: Sequence,
        asks: Sequence,
        *,
        is_snapshot: bool = False,
        ts_recv_ms: Optional[int] = None,
        first_update_id: Optional[int] = None,
        final_update_id: Optional[int] = None,
        last_update_id: Optional[int] = None,
    ) -> None:
        dataset = "depth_snapshots" if is_snapshot else "depth_updates"
        rows = _depth_level_rows(
            self.venue,
            ts_ms,
            bids,
            asks,
            ts_recv_ms=ts_recv_ms,
            first_update_id=first_update_id,
            final_update_id=final_update_id,
            last_update_id=last_update_id,
        )
        if not rows:
            with self._lock:
                self._events_without_levels += 1
            logger.warning(
                "Depth event has no levels venue=%s symbol=%s snapshot=%s "
                "first_update_id=%s final_update_id=%s last_update_id=%s",
                self.venue, self.symbol, is_snapshot,
                first_update_id, final_update_id, last_update_id,
            )
            return
        placed = ts_recv_ms if ts_ms is None else int(ts_ms)
        with self._lock:
            self._bufs[dataset].extend(rows)
            self._last_event_ms[dataset] = int(placed)
            overflow = len(self._bufs[dataset]) >= self.max_buffer_rows
        if overflow:
            self.flush()

    def on_market_event(self, event) -> None:
        """Ingest a :class:`market_data.MarketEvent`.

        A depth event without its sequence id is quarantined. A REST snapshot
        does not receive an exchange timestamp; receipt time places the shard.
        """
        from market_data import EventKind

        payload = event.payload or {}
        recv = _positive_int(event.ts_recv_ms)
        if event.kind == EventKind.TRADE:
            exchange = _positive_int(event.ts_exchange_ms)
            if exchange is None or recv is None:
                self._quarantine(
                    kind="trade",
                    reason="missing_receipt_or_exchange_timestamp",
                    rows=[{
                        "timestamp": event.ts_exchange_ms,
                        "ts_recv_ms": event.ts_recv_ms,
                        "price": payload.get("price"),
                        "quantity": payload.get("qty", payload.get("quantity")),
                        "trade_id": event.seq,
                    }],
                )
                return
            self.add_trade(
                exchange,
                float(payload["price"]),
                float(payload.get("qty", payload.get("quantity", 0))),
                bool(payload.get("is_buyer_maker", False)),
                ts_recv_ms=recv,
                trade_id=_positive_int(event.seq),
            )
            return
        if event.kind not in (EventKind.DEPTH_UPDATE, EventKind.DEPTH_SNAPSHOT):
            logger.error(
                "Unhandled market event kind=%s venue=%s symbol=%s",
                event.kind, event.venue, event.symbol,
            )
            return
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        snapshot = event.kind == EventKind.DEPTH_SNAPSHOT
        if snapshot:
            last = _positive_int(payload.get("last_update_id")) or _positive_int(event.seq)
            if recv is None or last is None:
                self._quarantine(
                    kind="depth_snapshot",
                    reason="missing_snapshot_update_id" if last is None else "missing_receipt_timestamp",
                    rows=_depth_level_rows(
                        self.venue, None, bids, asks, ts_recv_ms=event.ts_recv_ms, last_update_id=last,
                    ) or [{"timestamp": None, "ts_recv_ms": event.ts_recv_ms, "last_update_id": last}],
                )
                return
            self.add_depth(
                None, bids, asks,
                is_snapshot=True, ts_recv_ms=recv, last_update_id=last,
            )
            return
        exchange = _positive_int(event.ts_exchange_ms)
        first = _positive_int(payload.get("first_update_id"))
        final = _positive_int(payload.get("final_update_id"))
        if recv is None or exchange is None or first is None or final is None or final < first:
            if first is None or final is None or (first is not None and final is not None and final < first):
                reason = "missing_depth_sequence_ids"
            elif exchange is None:
                reason = "missing_exchange_timestamp"
            else:
                reason = "missing_receipt_timestamp"
            self._quarantine(
                kind="depth_update",
                reason=reason,
                rows=_depth_level_rows(
                    self.venue, exchange, bids, asks,
                    ts_recv_ms=event.ts_recv_ms,
                    first_update_id=first,
                    final_update_id=final,
                ) or [{
                    "timestamp": exchange,
                    "ts_recv_ms": event.ts_recv_ms,
                    "first_update_id": first,
                    "final_update_id": final,
                }],
            )
            return
        self.add_depth(
            exchange, bids, asks,
            is_snapshot=False,
            ts_recv_ms=recv,
            first_update_id=first,
            final_update_id=final,
        )

    def _quarantine(self, *, kind: str, reason: str, rows: List[dict]) -> None:
        """Keep a rejected event out of the book datasets and count it."""
        stored = []
        for row in rows:
            item = {"venue": self.venue, "kind": kind, "reason": reason}
            item.update(row)
            item["venue"] = self.venue
            item["kind"] = kind
            item["reason"] = reason
            stored.append(item)
        logger.error(
            "Quarantined %d %s rows venue=%s symbol=%s reason=%s",
            len(stored), kind, self.venue, self.symbol, reason,
        )
        with self._lock:
            self._rows_quarantined += len(stored)
            self._bufs["quarantine"].extend(stored)
            overflow = len(self._bufs["quarantine"]) >= self.max_buffer_rows
        if overflow:
            self.flush()

    # -- flush --------------------------------------------------------------

    def flush(self) -> int:
        """Write buffered rows to shards. Returns rows written, not rows dropped.

        Leftover local shards (upload failed, or a crash between write and
        delete) are retried first. A disk-floor refusal or write error
        re-queues the rows. Past ``2 * max_buffer_rows`` the overflow is
        dropped and counted — that flush did not succeed for those rows.
        """
        self._publish_leftovers()
        with self._lock:
            snapshot = {k: v for k, v in self._bufs.items() if v}
            self._bufs = defaultdict(list)
        written = 0
        for dataset, rows in snapshot.items():
            try:
                n = self._write_shard(dataset, rows)
                written += n
            except DiskFloorError:
                logger.error(
                    "Shard write refused venue=%s symbol=%s dataset=%s rows=%d "
                    "— free disk below floor; re-buffering",
                    self.venue, self.symbol, dataset, len(rows),
                )
                self._rebuffer(dataset, rows)
            except Exception:
                logger.exception(
                    "Shard write failed venue=%s symbol=%s dataset=%s rows=%d "
                    "— re-buffering",
                    self.venue, self.symbol, dataset, len(rows),
                )
                self._rebuffer(dataset, rows)
        return written

    def _rebuffer(self, dataset: str, rows: List[dict]) -> None:
        cap = self.max_buffer_rows * 2
        with self._lock:
            merged = list(rows) + self._bufs[dataset]
            if len(merged) > cap:
                dropped = len(merged) - cap
                merged = merged[-cap:]
                self._rows_dropped += dropped
                logger.error(
                    "Dropped %d rows venue=%s symbol=%s dataset=%s buffer_cap=%d "
                    "— flush did not succeed",
                    dropped, self.venue, self.symbol, dataset, cap,
                )
            self._bufs[dataset] = merged

    def _free_bytes(self) -> int:
        if self._free_bytes_fn is not None:
            return int(self._free_bytes_fn())
        path = self.root if self.root.exists() else self.root.parent
        if not path.exists():
            path = Path(".")
        return shutil.disk_usage(path).free

    def _write_shard(self, dataset: str, rows: List[dict]) -> int:
        if not rows:
            return 0
        free = self._free_bytes()
        if self.min_free_bytes > 0 and free < self.min_free_bytes:
            raise DiskFloorError(
                f"free {free} bytes < floor {self.min_free_bytes} "
                f"venue={self.venue} symbol={self.symbol} dataset={dataset}"
            )
        import pyarrow.parquet as pq

        cols = _columns_for(dataset)
        df = pd.DataFrame(rows)
        for col in cols:
            if col not in df.columns:
                df[col] = pd.NA
        df = df.loc[:, cols]
        clock = _placement_clock(df)
        if clock.isna().any():
            missing = int(clock.isna().sum())
            if dataset != "quarantine":
                raise ValueError(
                    f"{dataset} shard has {missing} rows with no exchange or receipt time"
                )
            placed = int(datetime.now(timezone.utc).timestamp() * 1000)
            logger.error(
                "Quarantine shard has %d rows with no clock; placing the file at %d "
                "without writing that value into the row",
                missing, placed,
            )
            clock = clock.fillna(placed)
        start_ms = int(clock.min())
        end_ms = int(clock.max())
        out_dir = shard_dir(self.root, self.venue, self.symbol, dataset, start_ms)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = shard_filename(start_ms, end_ms)
        final = out_dir / name
        tmp = out_dir / f".{name}.tmp"

        table = _table_for_schema(df, _arrow_schema(dataset))
        pq.write_table(table, tmp, compression="zstd")
        # fsync file then atomic rename
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        self._shards_written += 1
        self._rows_written += len(rows)
        logger.info(
            "Wrote shard %s (%d rows, %d–%d ms)",
            final, len(rows), start_ms, end_ms,
        )
        self._publish_one(final, dataset, len(rows))
        return len(rows)

    def _publish_one(self, final: Path, dataset: str, rows: int) -> bool:
        """Confirm the shard in S3, record the watermark, then delete it.

        Upload failure leaves the file for the next flush. The watermark is
        written only after S3 reports the same byte count.
        """
        if self._publisher is None or not final.is_file():
            return False
        key = self._publisher.object_key(self.root, final)
        try:
            self._publisher.publish(final, key)
        except Exception:
            logger.exception(
                "Shard upload failed %s — keeping local file for retry", final,
            )
            return False
        day = _day_from_shard_path(final)
        try:
            write_durable_watermark(
                self.root, self.venue, self.symbol, dataset,
                key=key, rows=rows, day=day,
            )
        except Exception:
            logger.exception(
                "Watermark write failed for %s — keeping local file", key,
            )
            return False
        try:
            final.unlink()
        except OSError:
            logger.exception(
                "Confirmed shard %s but unlink failed — sweeper will retry", key,
            )
        else:
            logger.info("Deleted local shard after confirm %s", key)
        return True

    def _publish_leftovers(self) -> int:
        if self._publisher is None:
            return 0
        base = self.root / self.venue / self.symbol
        if not base.is_dir():
            return 0
        published = 0
        for path in sorted(base.rglob("part-*.parquet")):
            dataset = _dataset_from_shard_path(path)
            if dataset is None:
                logger.error("Cannot parse dataset from leftover shard %s — keeping", path)
                continue
            if self._publish_one(path, dataset, rows=0):
                published += 1
        return published

    # -- background flush ---------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._publish_leftovers()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name=f"shard-flush-{self.venue}-{self.symbol}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.flush_interval_s + 5)
            self._thread = None
        self.flush()

    def _loop(self) -> None:
        while not self._stop.wait(self.flush_interval_s):
            try:
                self.flush()
            except Exception:
                logger.exception("Background shard flush error")

    # -- inspection ---------------------------------------------------------

    def buffered_rows(self) -> Dict[str, int]:
        with self._lock:
            return {k: len(v) for k, v in self._bufs.items()}

    def latest_shard_info(self) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[str]]]:
        """Return (mtime_by_dataset, day_by_dataset) for health checks."""
        mtimes: Dict[str, Optional[float]] = {}
        days: Dict[str, Optional[str]] = {}
        base = self.root / self.venue / self.symbol
        for d in DATASETS:
            best_mtime: Optional[float] = None
            best_day: Optional[str] = None
            ds_root = base / d
            if ds_root.is_dir():
                for p in ds_root.rglob("part-*.parquet"):
                    try:
                        mt = p.stat().st_mtime
                    except OSError:
                        continue
                    if best_mtime is None or mt > best_mtime:
                        best_mtime = mt
                        # date=YYYY-MM-DD is a parent dir
                        for part in p.parts:
                            if part.startswith("date="):
                                best_day = part.split("=", 1)[1]
                    elif best_mtime is not None and mt == best_mtime:
                        pass
            wm = read_durable_watermark(self.root, self.venue, self.symbol, d)
            if wm is not None:
                wm_m = float(wm["last_upload_unix"])
                if best_mtime is None or wm_m >= best_mtime:
                    best_mtime = wm_m
                    best_day = wm.get("day") or best_day
            mtimes[d] = best_mtime
            days[d] = best_day
        return mtimes, days

    def durable_info(self) -> Tuple[Dict[str, Optional[float]], Dict[str, Optional[str]]]:
        """Return watermark ``(mtime, day)`` per dataset. Missing keys are None."""
        mtimes: Dict[str, Optional[float]] = {}
        days: Dict[str, Optional[str]] = {}
        for d in DATASETS:
            wm = read_durable_watermark(self.root, self.venue, self.symbol, d)
            if wm is None:
                mtimes[d] = None
                days[d] = None
            else:
                mtimes[d] = float(wm["last_upload_unix"])
                days[d] = wm.get("day")
        return mtimes, days

    def stats(self) -> Dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "shards_written": self._shards_written,
            "rows_written": self._rows_written,
            "rows_dropped": self._rows_dropped,
            "rows_quarantined": self._rows_quarantined,
            "events_without_levels": self._events_without_levels,
            "buffered": self.buffered_rows(),
        }


# Back-compat alias used by older call sites / notebooks during transition.
LiveParquetMirror = ImmutableShardWriter


# ---------------------------------------------------------------------------
# Reader (notebooks / research)
# ---------------------------------------------------------------------------


class TickParquetStore:
    """Read immutable shards (and legacy flat day files if present)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _dataset_root(self, venue: str, symbol: str, dataset: str) -> Path:
        return self.root / venue.lower() / symbol.upper() / dataset

    def list_days(self, venue: str, symbol: str, dataset: str) -> List[str]:
        root = self._dataset_root(venue, symbol, dataset)
        days: set[str] = set()
        if not root.is_dir():
            return []
        for p in root.iterdir():
            if p.is_dir() and p.name.startswith("date="):
                days.add(p.name.split("=", 1)[1])
            elif p.suffix == ".parquet" and len(p.stem) == 10:
                days.add(p.stem)
        return sorted(days)

    def read(
        self,
        venue: str,
        symbol: str | None = None,
        dataset: str | None = None,
        *,
        exchange: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> pd.DataFrame:
        """Concatenate shards, sort, and de-duplicate by full row.

        Call as ``read(venue, symbol, dataset)`` or the older notebook form
        ``read(symbol, dataset, exchange=venue)``.
        """
        # Compat: read(symbol, dataset, exchange=...)
        if dataset is None and exchange is not None:
            dataset = symbol  # type: ignore[assignment]
            symbol = venue
            venue = exchange
        if symbol is None or dataset is None:
            raise TypeError("read() requires venue, symbol, dataset")
        if from_date and start_ms is None:
            start_ms = int(
                datetime.strptime(from_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
                * 1000
            )
        if to_date and end_ms is None:
            end_ms = int(
                datetime.strptime(to_date, "%Y-%m-%d")
                .replace(tzinfo=timezone.utc)
                .timestamp()
                * 1000
            ) + 86_400_000 - 1
        root = self._dataset_root(venue, symbol, dataset)
        if not root.is_dir():
            return pd.DataFrame(columns=_columns_for(dataset))

        frames: List[pd.DataFrame] = []
        paths = list(root.rglob("part-*.parquet")) + list(root.glob("????-??-??.parquet"))
        for path in sorted(paths):
            try:
                df = pd.read_parquet(path)
            except Exception as exc:
                logger.warning("Skipping unreadable shard %s: %s", path, exc)
                continue
            if "venue" not in df.columns:
                df.insert(0, "venue", venue.lower())
            frames.append(df)

        if not frames:
            return pd.DataFrame(columns=_columns_for(dataset))

        out = pd.concat(frames, ignore_index=True)
        clock = _placement_clock(out) if "timestamp" in out.columns else pd.Series(dtype="float64")
        if start_ms is not None and len(out):
            out = out.loc[clock >= start_ms]
            clock = clock.loc[out.index]
        if end_ms is not None and len(out):
            out = out.loc[clock <= end_ms]
            clock = clock.loc[out.index]
        if len(out):
            out = out.assign(_clock=clock).sort_values("_clock").drop(columns="_clock")
            out = out.drop_duplicates().reset_index(drop=True)
        return out

    def latest_day(self, venue: str, symbol: str, dataset: str) -> Optional[str]:
        days = self.list_days(venue, symbol, dataset)
        return days[-1] if days else None


__all__ = [
    "DATASETS",
    "DiskFloorError",
    "ImmutableShardWriter",
    "LiveParquetMirror",
    "PipelineHealth",
    "S3ShardPublisher",
    "SweepStats",
    "TickParquetStore",
    "evaluate_pipeline_health",
    "shard_dir",
    "shard_filename",
    "sweep_local_shards",
]
