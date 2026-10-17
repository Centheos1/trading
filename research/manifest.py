"""Immutable identity of one research dataset."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "minute-bars-v1"
CANONICAL_BUCKET = "trading-data-centheos"
CANONICAL_PROFILE = "trading"
CANONICAL_REGION = "ap-southeast-2"


@dataclass(frozen=True)
class DatasetObject:
    """One stored object that contributes rows to a dataset."""

    key: str
    etag: str
    size: int
    version_id: str | None = None


@dataclass(frozen=True)
class QualityExclusions:
    """Counted rows and gaps removed or revealed before an experiment sees data."""

    duplicate_timestamps: int = 0
    off_grid_timestamps: int = 0
    nonpositive_prices: int = 0
    inconsistent_ohlc: int = 0
    missing_minutes: int = 0


@dataclass(frozen=True)
class DatasetManifest:
    """The exact inputs and exclusions behind a dataset identifier."""

    dataset_id: str
    venue: str
    symbol: str
    timeframe: str
    bucket: str
    profile: str
    objects: tuple[DatasetObject, ...]
    start: str
    end: str
    schema_version: str
    exclusions: QualityExclusions
    digest: str


def _s3_client(profile: str, region: str):
    try:
        import boto3
    except ImportError as exc:
        raise ImportError("listing the research bucket requires boto3") from exc
    return boto3.Session(profile_name=profile, region_name=region).client("s3")


def _identify_object(client, bucket: str, key: str) -> DatasetObject | None:
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        response = getattr(exc, "response", None)
        code = ""
        if isinstance(response, dict):
            code = str(response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound"}:
            logger.info("Research object s3://%s/%s is absent", bucket, key)
            return None
        raise RuntimeError(f"failed to identify s3://{bucket}/{key}: {exc}") from exc
    etag = str(head.get("ETag", "")).strip('"')
    if not etag:
        raise RuntimeError(f"s3://{bucket}/{key} has no ETag")
    return DatasetObject(
        key=key,
        etag=etag,
        size=int(head["ContentLength"]),
        version_id=head.get("VersionId"),
    )


def list_ohlcv_objects(
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    bucket: str = CANONICAL_BUCKET,
    profile: str = CANONICAL_PROFILE,
    region: str = CANONICAL_REGION,
    client=None,
) -> tuple[DatasetObject, ...]:
    """List the legacy file and yearly Parquet objects for one OHLCV series.

    The historical archive is ``ohlcv/{venue}/{symbol}/{timeframe}.parquet``.
    The collector continues in ``.../{timeframe}/{year}.parquet``. Both are
    part of the research dataset. The profile is explicit because the default
    credential chain can resolve to an account that does not own the bucket.
    """
    if not bucket or not venue or not symbol or not timeframe:
        raise ValueError("bucket, venue, symbol, and timeframe are required")
    s3 = client if client is not None else _s3_client(profile, region)
    keys = [f"ohlcv/{venue}/{symbol}/{timeframe}.parquet"]
    prefix = f"ohlcv/{venue}/{symbol}/{timeframe}/"
    token: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            page = s3.list_objects_v2(**kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"failed to list s3://{bucket}/{prefix} with profile {profile!r}: {exc}"
            ) from exc
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            if key.endswith(".parquet"):
                keys.append(key)
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            raise RuntimeError(f"truncated listing for s3://{bucket}/{prefix} has no continuation token")
    found = []
    for key in dict.fromkeys(keys):
        identified = _identify_object(s3, bucket, key)
        if identified is not None:
            found.append(identified)
    if not found:
        raise RuntimeError(
            f"no Parquet objects for {venue}/{symbol}/{timeframe} in s3://{bucket} using profile {profile!r}"
        )
    objects = tuple(sorted(found, key=lambda item: item.key))
    logger.info("Identified %d objects for %s/%s/%s", len(objects), venue, symbol, timeframe)
    return objects


def build_manifest(
    *,
    dataset_id: str,
    venue: str,
    symbol: str,
    timeframe: str,
    bucket: str,
    profile: str,
    objects: tuple[DatasetObject, ...] | list[DatasetObject],
    start: str,
    end: str,
    exclusions: QualityExclusions,
    schema_version: str = SCHEMA_VERSION,
) -> DatasetManifest:
    """Freeze a dataset and attach the digest of its canonical payload."""
    if not dataset_id:
        raise ValueError("dataset_id is required")
    if not objects:
        raise ValueError("a dataset manifest needs at least one object")
    if start >= end:
        raise ValueError(f"dataset start {start} is not before end {end}")
    manifest = DatasetManifest(
        dataset_id=dataset_id,
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        bucket=bucket,
        profile=profile,
        objects=tuple(sorted(objects, key=lambda item: item.key)),
        start=start,
        end=end,
        schema_version=schema_version,
        exclusions=exclusions,
        digest="",
    )
    digest = content_digest(manifest)
    return DatasetManifest(
        dataset_id=manifest.dataset_id,
        venue=manifest.venue,
        symbol=manifest.symbol,
        timeframe=manifest.timeframe,
        bucket=manifest.bucket,
        profile=manifest.profile,
        objects=manifest.objects,
        start=manifest.start,
        end=manifest.end,
        schema_version=manifest.schema_version,
        exclusions=manifest.exclusions,
        digest=digest,
    )


def canonical_payload(manifest: DatasetManifest) -> dict:
    """Return the digest input. The digest itself is not part of that input."""
    payload = asdict(manifest)
    payload.pop("digest", None)
    payload["objects"] = [asdict(item) for item in manifest.objects]
    payload["exclusions"] = asdict(manifest.exclusions)
    return payload


def content_digest(manifest: DatasetManifest) -> str:
    raw = json.dumps(canonical_payload(manifest), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def verify_manifest(manifest: DatasetManifest) -> None:
    """Raise when a loaded manifest does not match its recorded digest."""
    actual = content_digest(manifest)
    if actual != manifest.digest:
        raise ValueError(
            f"dataset {manifest.dataset_id} digest mismatch: recorded {manifest.digest} actual {actual}"
        )


def save_manifest(path: Path | str, manifest: DatasetManifest) -> None:
    """Atomically write a verified manifest."""
    verify_manifest(manifest)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(canonical_payload(manifest) | {"digest": manifest.digest}, indent=2, sort_keys=True))
    os.replace(temporary, target)


def load_manifest(path: Path | str) -> DatasetManifest:
    """Load a manifest and reject a digest that no longer matches."""
    payload = json.loads(Path(path).read_text())
    try:
        objects = tuple(DatasetObject(**item) for item in payload["objects"])
        exclusions = QualityExclusions(**payload["exclusions"])
        manifest = DatasetManifest(
            dataset_id=payload["dataset_id"],
            venue=payload["venue"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            bucket=payload["bucket"],
            profile=payload["profile"],
            objects=objects,
            start=payload["start"],
            end=payload["end"],
            schema_version=payload["schema_version"],
            exclusions=exclusions,
            digest=payload["digest"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"dataset manifest {path} is incomplete: {exc}") from exc
    verify_manifest(manifest)
    return manifest
