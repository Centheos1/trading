"""Versioned experiment records. A notebook result does not move a status by itself."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

LIFECYCLE = (
    "Proposed",
    "Exploratory",
    "Implemented",
    "Research validated",
    "Integration validated",
    "Paper ready",
    "Paper observation",
    "Paper validated",
    "Live eligible",
    "Rejected",
    "Revised",
    "Deferred",
)
DECISIONS = ("", "retain", "reject", "revise", "defer")
_OPEN_STATUSES = {"Proposed", "Deferred"}


@dataclass(frozen=True)
class ExperimentRecord:
    """One hypothesis identifier and the evidence currently attached to it."""

    experiment_id: str
    hypothesis: str
    null: str
    parent_hypothesis: str | None
    dataset_id: str
    dataset_digest: str
    code_version: str
    config_digest: str
    status: str
    result: str
    decision: str
    component: str
    notebook: str
    horizons: tuple[int, ...]
    sealed_access: bool
    run_id: str


def code_version(root: Path | str) -> str:
    """Return the git revision. A dirty tree is marked, not hidden."""
    directory = str(root)
    revision = _git(directory, "rev-parse", "HEAD")
    dirty = _git(directory, "status", "--porcelain")
    if not revision:
        raise RuntimeError(f"git returned an empty revision in {directory}")
    token = revision + ("+dirty" if dirty else "")
    if dirty:
        logger.warning("Research code version is dirty: %s", token)
    return token


def require_committed(version: str) -> str:
    """Refuse a confirmatory identity built from uncommitted research code."""
    if version.endswith("+dirty") or not version:
        raise RuntimeError(f"confirmatory run refused on code version {version!r}")
    return version


def config_digest(config: dict) -> str:
    """Digest a JSON-compatible configuration independently of key order."""
    try:
        raw = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    except TypeError as exc:
        raise ValueError(f"experiment configuration is not JSON-compatible: {exc}") from exc
    return hashlib.sha256(raw).hexdigest()


def make_run_id(
    experiment_id: str,
    dataset_digest: str,
    version: str,
    configuration_digest: str,
) -> str:
    """Identity of one dataset, code revision, and configuration."""
    if not all((experiment_id, dataset_digest, version, configuration_digest)):
        raise ValueError("run identity needs an experiment, dataset, code version, and configuration")
    raw = f"{experiment_id}|{dataset_digest}|{version}|{configuration_digest}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def validate_record(record: ExperimentRecord) -> None:
    """Reject a record that claims confirmation it has not earned."""
    if not record.experiment_id.startswith("EXP-"):
        raise ValueError(f"experiment id {record.experiment_id!r} must start with EXP-")
    if record.status not in LIFECYCLE:
        raise ValueError(f"unknown lifecycle status {record.status!r}")
    if record.decision not in DECISIONS:
        raise ValueError(f"unknown decision {record.decision!r}")
    if record.decision == "retain" and not record.sealed_access:
        raise ValueError(f"{record.experiment_id} cannot be retained without recorded sealed-test access")
    if record.decision == "retain" and record.status in _OPEN_STATUSES:
        raise ValueError(f"{record.experiment_id} cannot be retained from status {record.status}")
    if record.sealed_access and not record.run_id:
        raise ValueError(f"{record.experiment_id} sealed access must name its run")
    if record.status not in _OPEN_STATUSES and not record.dataset_digest:
        raise ValueError(f"{record.experiment_id} status {record.status} needs a dataset digest")
    if not record.horizons:
        raise ValueError(f"{record.experiment_id} needs at least one horizon")


def save_record(path: Path | str, record: ExperimentRecord) -> None:
    """Atomically write a valid experiment record."""
    validate_record(record)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(_payload(record), indent=2, sort_keys=True))
    os.replace(temporary, target)
    logger.info("Saved experiment %s with status %s", record.experiment_id, record.status)


def load_record(path: Path | str) -> ExperimentRecord:
    """Load one record and apply the same validation as a save."""
    payload = json.loads(Path(path).read_text())
    try:
        record = ExperimentRecord(
            experiment_id=payload["experiment_id"],
            hypothesis=payload["hypothesis"],
            null=payload["null"],
            parent_hypothesis=payload["parent_hypothesis"],
            dataset_id=payload["dataset_id"],
            dataset_digest=payload["dataset_digest"],
            code_version=payload["code_version"],
            config_digest=payload["config_digest"],
            status=payload["status"],
            result=payload["result"],
            decision=payload["decision"],
            component=payload["component"],
            notebook=payload["notebook"],
            horizons=tuple(payload["horizons"]),
            sealed_access=bool(payload["sealed_access"]),
            run_id=payload["run_id"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"experiment record {path} is incomplete: {exc}") from exc
    validate_record(record)
    return record


def _payload(record: ExperimentRecord) -> dict:
    payload = asdict(record)
    payload["horizons"] = list(record.horizons)
    return payload


def _git(directory: str, *args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        raise RuntimeError(f"git {' '.join(args)} failed in {directory}: {detail}") from exc
    return completed.stdout.strip()
