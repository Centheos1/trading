#!/usr/bin/env bash
# Archive existing S3 objects to Glacier Flexible Retrieval, then clean
# Standard live prefixes for greenfield Parquet writers.
#
# Dry-run by default. Destructive steps require --execute.
#
#   ./scripts/s3_archive_and_clean.sh --profile trading \
#       --bucket trading-data-centheos --dry-run
#   ./scripts/s3_archive_and_clean.sh --profile trading \
#       --bucket trading-data-centheos \
#       --expire-versions --apply-lifecycle --execute
#   ./scripts/s3_archive_and_clean.sh --profile trading \
#       --bucket trading-data-centheos \
#       --archive-ticks --execute
#   ./scripts/s3_archive_and_clean.sh --profile trading \
#       --bucket trading-data-centheos \
#       --expire-versions --delete-corrupt-h5 --glacier --clean-live --execute
#
# --profile picks the AWS credentials profile that OWNS the bucket (the
# default chain may resolve to an unrelated account). --skip-running-check
# ignores local docker collector containers.
# --delete-corrupt-h5 permanently deletes all versions of .h5 keys marked
# corrupt in logs/h5_validate/report.json (fallback: keys confirmed corrupt
# on 2026-07-28). Run BEFORE --glacier so junk is not frozen for 90 days.
# --archive-ticks deletes corrupt HDF5, then copy-verifies readable HDF5 and
# tick Parquet into cold-archive/ using Glacier Flexible Retrieval. It deletes
# exact source VersionIds (no delete markers) and proves ohlcv/ was unchanged.
# --apply-lifecycle installs the NoncurrentVersionExpiration rule that stops
# noncurrent versions accumulating again (--noncurrent-days, default 14).
# --expire-versions is the one-off cleanup; --apply-lifecycle is the fix.
# NOTE: this writes the same rule ID as infra/main.tf and a lifecycle PUT
# replaces the WHOLE document. Once Terraform manages the bucket, treat it as
# authoritative and change retention there, not here.

set -euo pipefail

BUCKET="${S3_BUCKET:-trading-data-centheos}"
REGION="${AWS_REGION:-ap-southeast-2}"
PROFILE="${AWS_PROFILE:-}"
DRY_RUN=1
DO_EXPIRE=0
DO_DELETE_CORRUPT_H5=0
DO_ARCHIVE_TICKS=0
DO_LIFECYCLE=0
PRINT_LIFECYCLE=0
DO_GLACIER=0
DO_CLEAN=0
NONCURRENT_DAYS="${NONCURRENT_DAYS:-14}"
SKIP_RUNNING=0
APP_DIR="${APP_DIR:-.}"
INVENTORY_DIR="${APP_DIR}/logs"
H5_REPORT="${APP_DIR}/logs/h5_validate/report.json"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

usage() {
    sed -n '2,33p' "$0" | sed 's/^# \?//'
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bucket) BUCKET="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --h5-report) H5_REPORT="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --execute) DRY_RUN=0; shift ;;
        --expire-versions) DO_EXPIRE=1; shift ;;
        --delete-corrupt-h5) DO_DELETE_CORRUPT_H5=1; shift ;;
        --archive-ticks) DO_ARCHIVE_TICKS=1; shift ;;
        --apply-lifecycle) DO_LIFECYCLE=1; shift ;;
        --print-lifecycle) PRINT_LIFECYCLE=1; shift ;;
        --noncurrent-days) NONCURRENT_DAYS="$2"; shift 2 ;;
        --glacier) DO_GLACIER=1; shift ;;
        --clean-live) DO_CLEAN=1; shift ;;
        --skip-running-check) SKIP_RUNNING=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown arg: $1" >&2; usage ;;
    esac
done

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

if ! [[ "${NONCURRENT_DAYS}" =~ ^[0-9]+$ ]] || [[ "${NONCURRENT_DAYS}" -lt 1 ]]; then
    echo "--noncurrent-days must be a positive integer, got: ${NONCURRENT_DAYS}" >&2
    exit 2
fi

# Built here (not inline at STEP 3) so --print-lifecycle can show the exact
# document that would be applied without touching AWS.
build_lifecycle_cfg() {
    local protect glacier
    protect=$(cat <<JSON
    {
      "ID": "expire-noncurrent-versions",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "NoncurrentVersionExpiration": {"NoncurrentDays": ${NONCURRENT_DAYS}},
      "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}
    }
JSON
)
    if [[ "${DO_GLACIER}" -eq 1 ]]; then
        glacier=$(cat <<'JSON'
    ,{
      "ID": "greenfield-archive-to-glacier",
      "Status": "Enabled",
      "Filter": {"Prefix": ""},
      "Transitions": [
        {"Days": 0, "StorageClass": "GLACIER"}
      ]
    }
JSON
)
    else
        glacier=""
    fi
    printf '{"Rules": [%s%s]}' "${protect}" "${glacier}"
}

if [[ "${PRINT_LIFECYCLE}" -eq 1 ]]; then
    build_lifecycle_cfg
    exit 0
fi

if [[ -n "${PROFILE}" ]]; then
    export AWS_PROFILE="${PROFILE}"
fi
export AWS_DEFAULT_REGION="${REGION}"

# ---------------------------------------------------------------------------
# Pre-flight: confirm WHICH account these credentials belong to and that they
# can actually read the bucket. The default credential chain can silently
# resolve to an unrelated account, which surfaces later as a bare
# "AccessDenied" that looks like a bucket-policy problem instead of a
# wrong-profile problem.
# ---------------------------------------------------------------------------
CALLER="$(aws sts get-caller-identity --query '[Account,Arn]' --output text 2>&1)" || {
    log "[ERROR] aws sts get-caller-identity failed — no usable credentials:"
    log "        ${CALLER}"
    exit 1
}
log "[AUTH] profile=${AWS_PROFILE:-<default-chain>} region=${REGION} identity=${CALLER}"

if ! aws s3api list-objects-v2 --bucket "${BUCKET}" --max-items 1 \
        --output text >/dev/null 2>&1; then
    log "[ERROR] cannot list s3://${BUCKET} with the current credentials."
    log "        identity=${CALLER}"
    log "        The bucket is owned by a different account, or this principal"
    log "        lacks s3:ListBucket / s3:ListBucketVersions on it."
    log "        Re-run with the owning profile, e.g.:"
    log "          $0 --profile trading --bucket ${BUCKET} --dry-run"
    if aws configure list-profiles >/dev/null 2>&1; then
        log "        Configured profiles: $(aws configure list-profiles | tr '\n' ' ')"
    fi
    exit 1
fi

if [[ "${SKIP_RUNNING}" -eq 0 ]]; then
    if command -v docker >/dev/null 2>&1; then
        if docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^trading-data' >/dev/null; then
            log "[ERROR] collector containers appear running — refuse archive/clean"
            log "        stop them or pass --skip-running-check"
            exit 2
        fi
    fi
fi

mkdir -p "${INVENTORY_DIR}"
INV_FILE="${INVENTORY_DIR}/s3_cleanup_inventory_${TIMESTAMP}.json"

log "[STEP 1] Inventory s3://${BUCKET} → ${INV_FILE}"
aws s3api list-object-versions --bucket "${BUCKET}" --output json > "${INV_FILE}.raw" || {
    log "[ERROR] list-object-versions failed"
    exit 1
}
python3 - <<'PY' "${INV_FILE}.raw" "${INV_FILE}"
import json, sys
raw_path, out_path = sys.argv[1], sys.argv[2]
with open(raw_path) as f:
    data = json.load(f)
versions = data.get("Versions") or []
markers = data.get("DeleteMarkers") or []
total_bytes = sum(int(v.get("Size") or 0) for v in versions)
current = [v for v in versions if v.get("IsLatest")]
noncurrent = [v for v in versions if not v.get("IsLatest")]
by_class = {}
for v in versions:
    sc = v.get("StorageClass") or "STANDARD"
    by_class[sc] = by_class.get(sc, 0) + int(v.get("Size") or 0)
GB = 1024**3
current_bytes = sum(int(v.get("Size") or 0) for v in current)
noncurrent_bytes = sum(int(v.get("Size") or 0) for v in noncurrent)
summary = {
    "object_versions": len(versions),
    "delete_markers": len(markers),
    "current_objects": len(current),
    "noncurrent_versions": len(noncurrent),
    "current_gb": round(current_bytes / GB, 3),
    "noncurrent_gb": round(noncurrent_bytes / GB, 3),
    "total_gb": round(total_bytes / GB, 3),
    "bytes_by_storage_class": by_class,
}
with open(out_path, "w") as f:
    json.dump(summary, f, indent=2)
print(json.dumps(summary, indent=2))
PY

NONCURRENT_COUNT="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["noncurrent_versions"])' "${INV_FILE}")"
NONCURRENT_GB="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["noncurrent_gb"])' "${INV_FILE}")"

# ---------------------------------------------------------------------------
# Ordering guard. Noncurrent versions must be expired BEFORE archiving, for
# two reasons:
#   * Glacier Flexible Retrieval bills a 90-day minimum per object, so
#     freezing junk versions locks in months of cost.
#   * With versioning ON, --clean-live's `aws s3 rm` only writes delete
#     markers; the "removed" objects survive as noncurrent versions and keep
#     billing. Expiring first is what actually reclaims the space.
#
# Corrupt .h5 files must also be purged BEFORE any archive / clean operation:
# clean-live syncs live prefixes into cold-archive/ (Glacier), which would
# freeze unreadable HDF5 for 90 days. Auto-enable --delete-corrupt-h5 when
# any archive/clean flag is set.
# ---------------------------------------------------------------------------
if [[ "${DO_GLACIER}" -eq 1 ]] || [[ "${DO_CLEAN}" -eq 1 ]] || [[ "${DO_ARCHIVE_TICKS}" -eq 1 ]]; then
    if [[ "${DO_DELETE_CORRUPT_H5}" -eq 0 ]]; then
        log "[INFO] enabling --delete-corrupt-h5 (required before tick archive/glacier/clean-live)"
        DO_DELETE_CORRUPT_H5=1
    fi
fi

if [[ "${DO_ARCHIVE_TICKS}" -eq 1 ]] && { [[ "${DO_GLACIER}" -eq 1 ]] || [[ "${DO_CLEAN}" -eq 1 ]]; }; then
    log "[ERROR] --archive-ticks is the selective path that preserves ohlcv/."
    log "        Do not combine it with --glacier or --clean-live, which affect ohlcv/."
    exit 2
fi

if [[ "${DO_EXPIRE}" -eq 0 ]] && {
    [[ "${DO_GLACIER}" -eq 1 ]] ||
    [[ "${DO_CLEAN}" -eq 1 ]] ||
    [[ "${DO_ARCHIVE_TICKS}" -eq 1 ]];
}; then
    if [[ "${NONCURRENT_COUNT}" -gt 0 ]]; then
        log "[ERROR] refusing archive/clean with ${NONCURRENT_COUNT} noncurrent"
        log "        versions (${NONCURRENT_GB} GB) still present."
        log "        Archiving or 'deleting' first would freeze/retain that junk:"
        log "          - Glacier bills a 90-day minimum per object"
        log "          - versioned deletes only add delete markers"
        log "        Add --expire-versions to the same invocation."
        exit 1
    fi
fi

if [[ "${DO_EXPIRE}" -eq 1 ]]; then
    log "[STEP 2] Expire noncurrent versions + delete markers"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[DRY-RUN] would batch-delete noncurrent versions from inventory"
    else
        python3 - <<'PY' "${INV_FILE}.raw" "${BUCKET}" "${REGION}"
import json, sys, boto3
raw_path, bucket, region = sys.argv[1], sys.argv[2], sys.argv[3]
with open(raw_path) as f:
    data = json.load(f)
to_delete = []
for v in data.get("Versions") or []:
    if not v.get("IsLatest"):
        to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
for m in data.get("DeleteMarkers") or []:
    to_delete.append({"Key": m["Key"], "VersionId": m["VersionId"]})
client = boto3.client("s3", region_name=region)
batch = 1000
deleted = 0
for i in range(0, len(to_delete), batch):
    chunk = to_delete[i:i+batch]
    if not chunk:
        continue
    resp = client.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
    deleted += len(chunk) - len(resp.get("Errors") or [])
    for err in resp.get("Errors") or []:
        print("DELETE ERROR", err, file=sys.stderr)
print(f"deleted_objects={deleted}")
PY
    fi
fi

# ---------------------------------------------------------------------------
# STEP 2b — permanently delete corrupt HDF5 (all versions + delete markers).
# Source of truth: logs/h5_validate/report.json entries with ok=false.
# Fallback: keys confirmed unreadable on 2026-07-28 (h5py open+sample).
# ---------------------------------------------------------------------------
if [[ "${DO_DELETE_CORRUPT_H5}" -eq 1 ]]; then
    log "[STEP 2b] Delete corrupt .h5 objects (all versions)"
    python3 - <<'PY' "${BUCKET}" "${REGION}" "${H5_REPORT}" "${DRY_RUN}"
import json, sys
from pathlib import Path

import boto3

bucket, region, report_path, dry_run_s = sys.argv[1:5]
dry_run = dry_run_s == "1"

# Keys confirmed corrupt by h5py on 2026-07-28 (download + open + sample).
FALLBACK_CORRUPT = [
    "ticks/ETHUSDT_ticks.h5",
    "ticks/archive/binance/BTCUSDT_ticks_2026-06-17T14-05-36Z.h5",
    "ticks/archive/binance/BTCUSDT_ticks_2026-07-14T16-44-17Z.h5",
    "ticks/archive/binance/ETHUSDT_ticks_2026-07-06T02-44-19Z.h5",
]

corrupt_keys: list[str] = []
src = "fallback"
report = Path(report_path)
if report.is_file():
    try:
        data = json.loads(report.read_text())
        corrupt_keys = [
            str(e["key"]) for e in data
            if isinstance(e, dict) and e.get("ok") is False and e.get("key")
        ]
        if corrupt_keys:
            src = str(report)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[WARN] could not parse {report}: {exc}; using fallback list",
            file=sys.stderr,
        )
if not corrupt_keys:
    corrupt_keys = list(FALLBACK_CORRUPT)

corrupt_keys = sorted(set(corrupt_keys))
print(f"corrupt_h5_keys={len(corrupt_keys)} source={src}")
for k in corrupt_keys:
    print(f"  - {k}")

# Always live-list versions for these keys. Do not trust the inventory dump:
# --expire-versions may have already removed noncurrent VersionIds.
client = boto3.client("s3", region_name=region)
to_delete: list[dict] = []
bytes_current = 0
missing = []
for key in corrupt_keys:
    kwargs: dict = {"Bucket": bucket, "Prefix": key}
    found = False
    while True:
        resp = client.list_object_versions(**kwargs)
        for v in resp.get("Versions") or []:
            if v.get("Key") != key:
                continue
            found = True
            to_delete.append({"Key": key, "VersionId": v["VersionId"]})
            if v.get("IsLatest"):
                bytes_current += int(v.get("Size") or 0)
        for m in resp.get("DeleteMarkers") or []:
            if m.get("Key") != key:
                continue
            found = True
            to_delete.append({"Key": key, "VersionId": m["VersionId"]})
        if not resp.get("IsTruncated"):
            break
        kwargs["KeyMarker"] = resp.get("NextKeyMarker")
        kwargs["VersionIdMarker"] = resp.get("NextVersionIdMarker")
    if not found:
        missing.append(key)

gb = bytes_current / (1024 ** 3)
print(
    f"versions_to_delete={len(to_delete)} "
    f"current_corrupt_gb={gb:.3f} "
    f"already_absent={len(missing)}"
)
for k in missing:
    print(f"  (absent) {k}")

if dry_run:
    print("[DRY-RUN] would permanently delete the versions listed above")
    raise SystemExit(0)

if not to_delete:
    print("nothing to delete")
    raise SystemExit(0)

batch = 1000
deleted = 0
errors = 0
for i in range(0, len(to_delete), batch):
    chunk = to_delete[i:i + batch]
    resp = client.delete_objects(
        Bucket=bucket, Delete={"Objects": chunk, "Quiet": True}
    )
    errs = resp.get("Errors") or []
    errors += len(errs)
    deleted += len(chunk) - len(errs)
    for err in errs:
        print("DELETE ERROR", err, file=sys.stderr)
print(f"deleted_corrupt_h5_versions={deleted} errors={errors}")
if errors:
    raise SystemExit(1)
PY
fi

# ---------------------------------------------------------------------------
# STEP 2c — archive readable tick data only; preserve OHLCV in Standard.
#
# The helper copy-verifies each exact current VersionId into
# cold-archive/{ticks,ticks-parquet}/ with GLACIER storage, then permanently
# deletes that source VersionId. This avoids creating delete markers and a
# fresh noncurrent copy. The helper aborts on destination conflicts and proves
# the ohlcv/ count and byte total did not change.
# ---------------------------------------------------------------------------
if [[ "${DO_ARCHIVE_TICKS}" -eq 1 ]]; then
    log "[STEP 2c] Archive readable tick data only; preserve ohlcv/"
    ARCHIVE_ARGS=(
        "${APP_DIR}/scripts/s3_archive_ticks.py"
        --bucket "${BUCKET}"
        --region "${REGION}"
        --corrupt-report "${H5_REPORT}"
    )
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        ARCHIVE_ARGS+=(--dry-run)
    fi
    python3 "${ARCHIVE_ARGS[@]}"
fi

# ---------------------------------------------------------------------------
# STEP 3 — lifecycle. This is the RECURRENCE FIX, not a cleanup step.
#
# Versioning is enabled on this bucket. Without a NoncurrentVersionExpiration
# rule every overwrite is retained forever, which is how ~6.6 TB of hidden
# noncurrent versions accumulated behind ~47 GB of visible objects. Expiring
# versions once does NOT prevent it happening again — the rule does.
#
# put-bucket-lifecycle-configuration REPLACES the whole config, so the rule
# list is built from the enabled flags in one call: --apply-lifecycle gives
# protection only; --glacier adds the archival transition on top of it.
# ---------------------------------------------------------------------------
if [[ "${DO_LIFECYCLE}" -eq 1 ]] || [[ "${DO_GLACIER}" -eq 1 ]]; then
    if [[ "${DO_GLACIER}" -eq 1 ]]; then
        log "[STEP 3] Lifecycle: expire noncurrent (${NONCURRENT_DAYS}d) + GLACIER transition"
    else
        log "[STEP 3] Lifecycle: expire noncurrent versions after ${NONCURRENT_DAYS}d (no Glacier)"
    fi
    CFG="$(build_lifecycle_cfg)"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "[DRY-RUN] would put-bucket-lifecycle-configuration:"
        echo "${CFG}" | python3 -m json.tool
    else
        aws s3api put-bucket-lifecycle-configuration \
            --bucket "${BUCKET}" \
            --lifecycle-configuration "${CFG}"
        log "[OK] lifecycle applied — noncurrent versions now expire after ${NONCURRENT_DAYS}d"
    fi
fi

if [[ "${DO_CLEAN}" -eq 1 ]]; then
    log "[STEP 4] Clean live Standard prefixes for greenfield writers"
    # After Glacier transition, current keys may still exist as Glacier.
    # For a hard clean of live prefixes we delete Standard-class objects only
    # under ticks/, ticks-parquet/, ohlcv/ — or all current versions when
    # --execute after archive.
    PREFIXES=("ticks/" "ticks-parquet/" "ohlcv/")
    for pfx in "${PREFIXES[@]}"; do
        if [[ "${DRY_RUN}" -eq 1 ]]; then
            count=$(aws s3 ls "s3://${BUCKET}/${pfx}" --recursive 2>/dev/null | wc -l | tr -d ' ')
            log "[DRY-RUN] would remove Standard objects under s3://${BUCKET}/${pfx} (listed≈${count})"
        else
            # Move to cold-archive/ then delete from live prefix when still STANDARD.
            log "[INFO] syncing ${pfx} → cold-archive/${pfx} (storage-class GLACIER)"
            aws s3 sync "s3://${BUCKET}/${pfx}" "s3://${BUCKET}/cold-archive/${pfx}" \
                --storage-class GLACIER --only-show-errors || true
            aws s3 rm "s3://${BUCKET}/${pfx}" --recursive --only-show-errors || true
            log "[OK] cleaned live prefix ${pfx}"
        fi
    done
fi

log "[STEP 5] Verification summary"
aws s3api list-objects-v2 --bucket "${BUCKET}" --max-items 5 --output json \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print("sample_keys=", [x["Key"] for x in d.get("Contents") or []])'

# Read back the live config rather than trusting that our put succeeded. A
# versioned bucket with no NoncurrentVersionExpiration rule silently re-grows
# its hidden version tail, so surface that as a loud warning every run.
if aws s3api get-bucket-versioning --bucket "${BUCKET}" --output text 2>/dev/null \
        | grep -q "Enabled"; then
    LIFECYCLE_JSON="$(aws s3api get-bucket-lifecycle-configuration \
        --bucket "${BUCKET}" --output json 2>/dev/null || echo '{}')"
    if echo "${LIFECYCLE_JSON}" | grep -q "NoncurrentVersionExpiration"; then
        log "[OK] versioning enabled AND noncurrent-version expiry rule present"
    else
        log "[WARN] ${BUCKET} has versioning ENABLED but NO NoncurrentVersionExpiration"
        log "       rule. Every overwrite will be retained forever and billed —"
        log "       this is what grew the hidden tail to ~6.6 TB."
        log "       Fix: re-run with --apply-lifecycle --execute"
    fi
fi

# The raw version listing is ~44 MB per run on this bucket; the summary is the
# durable artifact. Drop the raw dump so repeated runs cannot fill the disk.
rm -f "${INV_FILE}.raw"

log "[DONE] inventory=${INV_FILE} dry_run=${DRY_RUN}"
log "Next: selectively restore usable tick Parquet via scripts/s3_restore_tick_parquet.sh"
log "IAM note: scope TradingCollectorRole S3 actions to arn:aws:s3:::${BUCKET} and /*"
log "Do NOT start collector EC2 until go-live gate in docs/DEPLOYMENT.md passes."
