#!/usr/bin/env bash
# Hourly S3 sync — intended for /etc/cron.hourly/ on the EC2 instance.
#
# Three things happen on every invocation:
#
#   1. HDF5 tick files (data/ticks/{SYMBOL}_ticks.h5) → s3://${S3_BUCKET}/ticks/
#      Strategy depends on whether the writer container is running:
#        running     → in-container snapshot (h5py copy of the open file)
#        not running → direct aws s3 cp (file is closed, safe to upload raw)
#
#   2. Parquet tick mirror (data/ticks/{exchange}/{SYMBOL}/{dataset}/*.parquet)
#      → s3://${S3_BUCKET}/ticks-parquet/   (always — written by the
#      in-process flush thread, no container interaction needed)
#
#   3. Local Parquet cleanup — files older than 7 days are deleted
#      AFTER a successful S3 sync, keeping the EC2 disk bounded.
#
# OHLCV Parquet is also synced when DATA_STORE != s3 (legacy local mode).
#
# Environment variables (sourced from /home/ubuntu/app/trading/.env if present):
#   S3_BUCKET           — required; target bucket name
#   APP_DIR             — defaults to /home/ubuntu/app/trading
#   TICK_CONTAINER      — primary writer container (default: trading-data)
#   PARQUET_RETENTION_DAYS — local Parquet retention in days (default: 7)

set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/app/trading}"
LOG_FILE="${APP_DIR}/logs/s3_sync.log"

cd "${APP_DIR}"

mkdir -p "$(dirname "${LOG_FILE}")"

# Source .env for S3_BUCKET if it exists
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    source .env
    set +o allexport
fi

PARQUET_RETENTION_DAYS="${PARQUET_RETENTION_DAYS:-7}"

if [ -z "${S3_BUCKET:-}" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [WARN] S3_BUCKET not set — skipping sync" >> "${LOG_FILE}"
    exit 0
fi

UPLOADED=0
FAILED=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "${LOG_FILE}"
}

is_running() {
    docker inspect --format '{{.State.Running}}' "$1" 2>/dev/null | grep -q true
}

container_for_symbol() {
    # Prefer per-symbol container (created by scripts/add_symbol.sh) over the
    # default trading-data container. Returns empty string if neither exists.
    local sym="$1"
    local default_container="${TICK_CONTAINER:-trading-data}"
    if is_running "trading-data-${sym}"; then
        echo "trading-data-${sym}"
    elif is_running "${default_container}"; then
        echo "${default_container}"
    else
        echo ""
    fi
}

# ---------------------------------------------------------------------------
# HDF5 sync — one upload per local *_ticks.h5 file
# ---------------------------------------------------------------------------
#
# When the writer container is running we use the in-container snapshot
# path so the upload sees a consistent on-disk image (h5py reads through
# the OS page cache, which contains the libhdf5 writes not yet flushed
# to disk).  When the container is stopped the file is closed and safe
# to upload directly.

sync_tick_file() {
    local h5_path="$1"           # e.g. data/ticks/BTCUSDT_ticks.h5
    local sym
    sym="$(basename "${h5_path}" _ticks.h5)"
    local s3_key="ticks/${sym}_ticks.h5"

    if [ ! -f "${h5_path}" ]; then
        log "[SKIP] ${h5_path} not present"
        return 0
    fi

    local container
    container="$(container_for_symbol "${sym}")"

    if [ -n "${container}" ]; then
        # Writer is running — take an in-container snapshot.
        if docker exec "${container}" python collect_ticks.py \
                --upload-snapshot \
                --symbol "${sym}" \
                --s3-bucket "${S3_BUCKET}" \
                --s3-key "${s3_key}" \
                --data-dir /app/data >> "${LOG_FILE}" 2>&1; then
            log "[OK] snapshot ${sym} → s3://${S3_BUCKET}/${s3_key}"
            UPLOADED=$((UPLOADED + 1))
        else
            log "[FAIL] snapshot ${sym} via container ${container}"
            FAILED=$((FAILED + 1))
        fi
    else
        # No writer running — file is closed, upload directly.
        if aws s3 cp "${h5_path}" "s3://${S3_BUCKET}/${s3_key}" \
                --only-show-errors >> "${LOG_FILE}" 2>&1; then
            log "[OK] direct ${sym} → s3://${S3_BUCKET}/${s3_key}  ($(stat -c%s "${h5_path}" 2>/dev/null || stat -f%z "${h5_path}") bytes)"
            UPLOADED=$((UPLOADED + 1))
        else
            log "[FAIL] direct upload ${sym} (${h5_path})"
            FAILED=$((FAILED + 1))
        fi
    fi
}

if [ -d "${APP_DIR}/data/ticks" ]; then
    # Iterate every per-symbol HDF5 file under data/ticks/.
    shopt -s nullglob
    for h5 in "${APP_DIR}"/data/ticks/*_ticks.h5; do
        sync_tick_file "${h5}"
    done
    shopt -u nullglob
else
    log "[INFO] no data/ticks/ directory — first run or rotated away"
fi

# ---------------------------------------------------------------------------
# Parquet tick mirror sync
# ---------------------------------------------------------------------------
# Always runs.  The flush thread inside the collector writes daily Parquet
# files locally; this pushes them to S3 so notebooks can fetch by date.

PARQUET_SYNC_OK=0
if [ -d "${APP_DIR}/data/ticks" ]; then
    if aws s3 sync "${APP_DIR}/data/ticks" "s3://${S3_BUCKET}/ticks-parquet" \
            --exclude "*" --include "*.parquet" --only-show-errors \
            >> "${LOG_FILE}" 2>&1; then
        n_files=$(find "${APP_DIR}/data/ticks" -name '*.parquet' 2>/dev/null | wc -l | tr -d ' ')
        log "[OK] ticks-parquet/ tree synced (${n_files} parquet files)"
        UPLOADED=$((UPLOADED + 1))
        PARQUET_SYNC_OK=1
    else
        log "[FAIL] ticks-parquet/ tree sync"
        FAILED=$((FAILED + 1))
    fi
fi

# ---------------------------------------------------------------------------
# Local Parquet cleanup — bounded retention on EC2
# ---------------------------------------------------------------------------
# Only delete after a confirmed successful S3 sync; otherwise we could
# permanently lose data on a transient AWS failure.

if [ "${PARQUET_SYNC_OK}" -eq 1 ] && [ -d "${APP_DIR}/data/ticks" ]; then
    n_deleted=$(find "${APP_DIR}/data/ticks" -name '*.parquet' \
        -mtime "+${PARQUET_RETENTION_DAYS}" -delete -print 2>/dev/null | wc -l | tr -d ' ')
    if [ "${n_deleted}" -gt 0 ]; then
        log "[OK] pruned ${n_deleted} local parquet files older than ${PARQUET_RETENTION_DAYS}d"
    fi
fi

# ---------------------------------------------------------------------------
# OHLCV Parquet tree (legacy local mode — DATA_STORE != s3)
# ---------------------------------------------------------------------------
# When DATA_STORE=s3 the collector writes Parquet straight to S3, so this
# block is skipped.

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
