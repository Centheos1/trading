#!/usr/bin/env bash
# Hourly S3 sync — Parquet shard upload for the greenfield collector.
#
#   1. Tick shards under data/ticks/{venue}/{SYMBOL}/… → s3://${S3_BUCKET}/ticks/
#   2. Local Parquet cleanup (older than PARQUET_RETENTION_DAYS) after success
#   3. OHLCV tree sync when DATA_STORE != s3
#
# No HDF5 path. Environment from ${APP_DIR}/.env:
#   S3_BUCKET, APP_DIR, PARQUET_RETENTION_DAYS (default 7)

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

PARQUET_RETENTION_DAYS="${PARQUET_RETENTION_DAYS:-7}"

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

# Local retention after confirmed sync
if [ "${PARQUET_SYNC_OK}" -eq 1 ] && [ -d "${APP_DIR}/data/ticks" ]; then
    n_deleted=$(find "${APP_DIR}/data/ticks" -name 'part-*.parquet' \
        -mtime "+${PARQUET_RETENTION_DAYS}" -delete -print 2>/dev/null | wc -l | tr -d ' ')
    if [ "${n_deleted}" -gt 0 ]; then
        log "[OK] pruned ${n_deleted} local shard files older than ${PARQUET_RETENTION_DAYS}d"
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
