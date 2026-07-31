#!/usr/bin/env python3
"""Move current tick objects to Glacier without touching OHLCV.

The move is implemented as copy-verify-delete of the exact source VersionId.
That avoids the delete markers and billable noncurrent source copies produced
by ``aws s3 rm`` on a versioned bucket. Destination metadata makes retries
idempotent: a matching prior copy is reused, while a conflicting destination
aborts rather than overwriting a Glacier object and creating another version.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import boto3
from botocore.exceptions import ClientError

TICK_PREFIXES = ("ticks/", "ticks-parquet/")
ARCHIVE_PREFIX = "cold-archive/"
GLACIER_STORAGE_CLASS = "GLACIER"
FALLBACK_CORRUPT_H5 = {
    "ticks/ETHUSDT_ticks.h5",
    "ticks/archive/binance/BTCUSDT_ticks_2026-06-17T14-05-36Z.h5",
    "ticks/archive/binance/BTCUSDT_ticks_2026-07-14T16-44-17Z.h5",
    "ticks/archive/binance/ETHUSDT_ticks_2026-07-06T02-44-19Z.h5",
}


@dataclass(frozen=True)
class VersionedObject:
    key: str
    version_id: str
    size: int
    etag: str

    @property
    def destination_key(self) -> str:
        return f"{ARCHIVE_PREFIX}{self.key}"


def _client_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def list_current_versions(
    client: Any,
    bucket: str,
    prefixes: Sequence[str] = TICK_PREFIXES,
) -> list[VersionedObject]:
    """Return current object versions under the requested prefixes."""
    objects: list[VersionedObject] = []
    paginator = client.get_paginator("list_object_versions")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for item in page.get("Versions") or []:
                key = str(item["Key"])
                if not item.get("IsLatest") or not key.startswith(prefix):
                    continue
                objects.append(
                    VersionedObject(
                        key=key,
                        version_id=str(item["VersionId"]),
                        size=int(item.get("Size") or 0),
                        etag=str(item.get("ETag") or "").strip('"'),
                    )
                )
    return sorted(objects, key=lambda item: item.key)


def current_prefix_totals(client: Any, bucket: str, prefix: str) -> tuple[int, int]:
    """Return current object count and bytes for a prefix."""
    count = 0
    size = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents") or []:
            count += 1
            size += int(item.get("Size") or 0)
    return count, size


def _archive_metadata(source: VersionedObject) -> dict[str, str]:
    return {
        "source-version-id": source.version_id,
        "source-etag": source.etag,
    }


def load_corrupt_keys(report_path: str | None) -> set[str]:
    """Load validated corrupt HDF5 keys, with the dated fallback used by shell."""
    if not report_path:
        return set(FALLBACK_CORRUPT_H5)
    path = Path(report_path)
    if not path.is_file():
        print(f"[WARN] corrupt-HDF5 report absent: {path}; using dated fallback")
        return set(FALLBACK_CORRUPT_H5)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] corrupt-HDF5 report unreadable: {exc}; using dated fallback")
        return set(FALLBACK_CORRUPT_H5)
    keys = {
        str(item["key"])
        for item in data
        if isinstance(item, dict) and item.get("ok") is False and item.get("key")
    }
    if not keys:
        print(f"[WARN] no corrupt keys found in {path}; using dated fallback")
        return set(FALLBACK_CORRUPT_H5)
    return keys


def _destination_matches(
    client: Any,
    bucket: str,
    source: VersionedObject,
) -> bool:
    try:
        head = client.head_object(Bucket=bucket, Key=source.destination_key)
    except ClientError as exc:
        if _client_error_code(exc) in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise

    metadata = head.get("Metadata") or {}
    matches = (
        int(head.get("ContentLength") or 0) == source.size
        and head.get("StorageClass") == GLACIER_STORAGE_CLASS
        and metadata.get("source-version-id") == source.version_id
        and metadata.get("source-etag") == source.etag
    )
    if not matches:
        raise RuntimeError(
            "archive destination conflict: "
            f"s3://{bucket}/{source.destination_key} exists but does not "
            f"match source {source.key} version {source.version_id}"
        )
    return True


def archive_one(client: Any, bucket: str, source: VersionedObject) -> str:
    """Copy, verify, then permanently delete one exact source version."""
    if _destination_matches(client, bucket, source):
        disposition = "reused"
    else:
        client.copy(
            {
                "Bucket": bucket,
                "Key": source.key,
                "VersionId": source.version_id,
            },
            bucket,
            source.destination_key,
            ExtraArgs={
                "StorageClass": GLACIER_STORAGE_CLASS,
                "MetadataDirective": "REPLACE",
                "Metadata": _archive_metadata(source),
            },
        )
        if not _destination_matches(client, bucket, source):
            raise RuntimeError(
                f"archive verification failed for s3://{bucket}/{source.key}"
            )
        disposition = "copied"

    response = client.delete_object(
        Bucket=bucket,
        Key=source.key,
        VersionId=source.version_id,
    )
    if response.get("DeleteMarker"):
        raise RuntimeError(
            f"version-specific delete unexpectedly created a delete marker: {source.key}"
        )
    return disposition


def archive_ticks(
    client: Any,
    bucket: str,
    *,
    dry_run: bool,
    corrupt_keys: set[str] | None = None,
) -> dict[str, int]:
    """Archive all current tick objects and prove OHLCV was unchanged."""
    ohlcv_before = current_prefix_totals(client, bucket, "ohlcv/")
    all_sources = list_current_versions(client, bucket)
    corrupt_keys = corrupt_keys or set()
    corrupt_sources = [source for source in all_sources if source.key in corrupt_keys]
    sources = [source for source in all_sources if source.key not in corrupt_keys]
    if corrupt_sources and not dry_run:
        sample = ", ".join(source.key for source in corrupt_sources[:5])
        raise RuntimeError(
            "refusing to archive corrupt HDF5 that should have been deleted first: "
            f"{sample}"
        )
    total_bytes = sum(source.size for source in sources)
    print(
        f"tick_objects={len(sources)} "
        f"tick_gb={total_bytes / 1024**3:.3f} "
        f"corrupt_tick_objects_excluded={len(corrupt_sources)} "
        f"corrupt_tick_gb_excluded="
        f"{sum(source.size for source in corrupt_sources) / 1024**3:.3f} "
        f"ohlcv_objects={ohlcv_before[0]} "
        f"ohlcv_gb={ohlcv_before[1] / 1024**3:.3f}"
    )

    if dry_run:
        for source in sources:
            print(
                f"[DRY-RUN] archive {source.key} version={source.version_id} "
                f"size={source.size} -> {source.destination_key}"
            )
        return {
            "planned": len(sources),
            "copied": 0,
            "reused": 0,
            "deleted": 0,
            "bytes": total_bytes,
        }

    copied = 0
    reused = 0
    deleted = 0
    for index, source in enumerate(sources, start=1):
        disposition = archive_one(client, bucket, source)
        copied += disposition == "copied"
        reused += disposition == "reused"
        deleted += 1
        print(
            f"[{index}/{len(sources)}] {disposition} and deleted source "
            f"{source.key} ({source.size / 1024**3:.3f} GB)"
        )

    remaining = list_current_versions(client, bucket)
    if remaining:
        sample = ", ".join(item.key for item in remaining[:5])
        raise RuntimeError(
            f"{len(remaining)} current tick objects remain after archive: {sample}"
        )

    ohlcv_after = current_prefix_totals(client, bucket, "ohlcv/")
    if ohlcv_after != ohlcv_before:
        raise RuntimeError(
            "OHLCV changed during tick-only archive: "
            f"before={ohlcv_before} after={ohlcv_after}"
        )

    print(
        f"archive_complete copied={copied} reused={reused} deleted={deleted} "
        f"ohlcv_unchanged_objects={ohlcv_after[0]} "
        f"ohlcv_unchanged_gb={ohlcv_after[1] / 1024**3:.3f}"
    )
    return {
        "planned": len(sources),
        "copied": copied,
        "reused": reused,
        "deleted": deleted,
        "bytes": total_bytes,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--corrupt-report")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = boto3.client("s3", region_name=args.region)
    archive_ticks(
        client,
        args.bucket,
        dry_run=args.dry_run,
        corrupt_keys=load_corrupt_keys(args.corrupt_report),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
