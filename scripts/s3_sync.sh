#!/usr/bin/env bash
# Hourly S3 sync — backstop for the in-process upload-confirm-delete path.
#
#   1. Upload any leftover tick shards under data/ticks/ → s3://${S3_BUCKET}/ticks/
#   2. Delete a local shard only when the S3 object size matches
#   3. OHLCV tree sync when DATA_STORE != s3
#
# The collector deletes a shard as soon as HeadObject confirms it. This cron
# catches files left behind by a crash or a failed upload. It does not keep a
# multi-day local copy.
#
# No HDF5 path. Environment from ${APP_DIR}/.env:
#   S3_BUCKET, APP_DIR

set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/app/trading}"
LOG_FILE="${APP_DIR}/logs/s3_sync.log"

cd "${APP_DIR}"
mkdir -p "$(dirname "${LOG_FILE}")"

if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    source .env
    set +o allexport
fi

if [ -z "${S3_BUCKET:-}" ]; then
    msg="S3_BUCKET not set — NOT syncing. Set S3_BUCKET in ${APP_DIR}/.env"
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [ERROR] ${msg}" | tee -a "${LOG_FILE}" >&2
    exit 1
fi

UPLOADED=0
FAILED=0

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "${LOG_FILE}"
}

# ---------------------------------------------------------------------------
# Tick Parquet shards → s3://…/ticks/
# ---------------------------------------------------------------------------

PARQUET_SYNC_OK=0
if [ -d "${APP_DIR}/data/ticks" ]; then
    if aws s3 sync "${APP_DIR}/data/ticks" "s3://${S3_BUCKET}/ticks" \
            --exclude "*" --include "*.parquet" --exclude "*.tmp" \
            --only-show-errors \
            >> "${LOG_FILE}" 2>&1; then
        n_files=$(find "${APP_DIR}/data/ticks" -name 'part-*.parquet' 2>/dev/null | wc -l | tr -d ' ')
        log "[OK] ticks/ shard tree synced (${n_files} part-*.parquet files)"
        UPLOADED=$((UPLOADED + 1))
        PARQUET_SYNC_OK=1
    else
        log "[FAIL] ticks/ shard tree sync"
        FAILED=$((FAILED + 1))
    fi
else
    log "[INFO] no data/ticks/ directory — first run"
fi

# Delete local shards only after S3 size matches. Prefer a running collector
# container (boto3 + this repo). Fall back to host python3.
if [ "${PARQUET_SYNC_OK}" -eq 1 ] && [ -d "${APP_DIR}/data/ticks" ]; then
    SWEEP_PY='import os, sys
from pathlib import Path
from tick_parquet_store import S3ShardPublisher, sweep_local_shards
root = Path(os.environ["TICK_ROOT"])
stats = sweep_local_shards(root, S3ShardPublisher(os.environ["S3_BUCKET"]))
print(
    f"sweep uploaded={stats.uploaded} deleted={stats.deleted} "
    f"kept={stats.kept} failed={stats.failed}"
)
sys.exit(1 if stats.failed or stats.kept else 0)
'
    SWEEP_OK=0
    for c in trading-data-BTCUSDT trading-data-ETHUSDT trading-data; do
        if docker inspect --format '{{.State.Running}}' "$c" 2>/dev/null | grep -q true; then
            if docker exec -e S3_BUCKET="${S3_BUCKET}" -e TICK_ROOT=/app/data/ticks \
                    "$c" python -c "${SWEEP_PY}" >> "${LOG_FILE}" 2>&1; then
                log "[OK] confirmed-shard sweep via ${c}"
                SWEEP_OK=1
                break
            else
                log "[FAIL] confirmed-shard sweep via ${c}"
            fi
        fi
    done
    if [ "${SWEEP_OK}" -eq 0 ]; then
        if TICK_ROOT="${APP_DIR}/data/ticks" S3_BUCKET="${S3_BUCKET}" \
                python3 -c "${SWEEP_PY}" >> "${LOG_FILE}" 2>&1; then
            log "[OK] confirmed-shard sweep via host python3"
            SWEEP_OK=1
        else
            log "[FAIL] confirmed-shard sweep — local shards kept"
            FAILED=$((FAILED + 1))
        fi
    fi
fi

# ---------------------------------------------------------------------------
# OHLCV (local mode only)
# ---------------------------------------------------------------------------

if [ -d "${APP_DIR}/data/ohlcv" ] && [ "${DATA_STORE:-local_parquet}" != "s3" ]; then
    if aws s3 sync "${APP_DIR}/data/ohlcv" "s3://${S3_BUCKET}/ohlcv" \
            --exclude "*" --include "*.parquet" --only-show-errors \
            >> "${LOG_FILE}" 2>&1; then
        n_files=$(find "${APP_DIR}/data/ohlcv" -name '*.parquet' 2>/dev/null | wc -l | tr -d ' ')
        log "[OK] ohlcv/ tree synced (${n_files} parquet files)"
        UPLOADED=$((UPLOADED + 1))
    else
        log "[FAIL] ohlcv/ tree sync"
        FAILED=$((FAILED + 1))
    fi
fi

log "[DONE] uploaded=${UPLOADED} failed=${FAILED}"

if [ "${FAILED}" -gt 0 ]; then
    exit 1
fi
