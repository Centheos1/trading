#!/usr/bin/env bash
# Hourly S3 sync — intended for /etc/cron.hourly/ on the EC2 instance.
#
# Copies data/binance_ticks.h5 (and any per-symbol files under data/) to S3.
# Logs to logs/s3_sync.log.
#
# Environment variables (sourced from /home/ubuntu/app/trading/.env if present):
#   S3_BUCKET   — required; target bucket name
#   APP_DIR     — defaults to /home/ubuntu/app/trading

set -euo pipefail

APP_DIR="${APP_DIR:-/home/ubuntu/app/trading}"
LOG_FILE="${APP_DIR}/logs/s3_sync.log"

cd "${APP_DIR}"

# Source .env for S3_BUCKET if it exists
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    source .env
    set +o allexport
fi

if [ -z "${S3_BUCKET:-}" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [WARN] S3_BUCKET not set — skipping sync" >> "${LOG_FILE}"
    exit 0
fi

UPLOADED=0
FAILED=0

# ---------------------------------------------------------------------------
# Tick files — snapshot upload via the running Docker container.
#
# A raw "aws s3 cp" of an open HDF5 file copies it mid-write: the C++
# TickStore's root-group metadata is still in libhdf5's page cache and has
# never been flushed to the OS-level file.  The resulting S3 object is
# structurally unreadable by h5py.
#
# Instead we exec into the container (where h5py can read through the OS
# page cache and see the complete in-memory state) and run the snapshot
# command, which:
#   1. Copies all datasets to a clean temporary HDF5 file.
#   2. Uploads that file to S3.
#   3. Deletes the temp file.
# The running collector is never paused or restarted.
# ---------------------------------------------------------------------------

CONTAINER="${TICK_CONTAINER:-trading-data}"

sync_tick_snapshot() {
    local container="$1"
    local s3_key="$2"           # e.g. ticks/binance_ticks.h5
    local data_dir="${3:-/app/data}"

    if ! docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null | grep -q true; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [WARN] container ${container} not running — skipping tick snapshot" >> "${LOG_FILE}"
        return 1
    fi

    if docker exec "${container}" python collect_ticks.py \
            --upload-snapshot \
            --s3-bucket "${S3_BUCKET}" \
            --s3-key "${s3_key}" \
            --data-dir "${data_dir}" >> "${LOG_FILE}" 2>&1; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [OK] tick snapshot → s3://${S3_BUCKET}/${s3_key}" >> "${LOG_FILE}"
        UPLOADED=$((UPLOADED + 1))
    else
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [FAIL] tick snapshot for ${container}" >> "${LOG_FILE}"
        FAILED=$((FAILED + 1))
    fi
}

# Default single-symbol container → ticks/binance_ticks.h5
sync_tick_snapshot "${CONTAINER}" "ticks/binance_ticks.h5"

# Multi-symbol: per-symbol containers named trading-data-{SYMBOL}
# (created by scripts/add_symbol.sh). Skip trading-data itself — already done.
for container in $(docker ps --format '{{.Names}}' | grep '^trading-data-'); do
    sym="${container#trading-data-}"
    sync_tick_snapshot "${container}" "ticks/binance_ticks_${sym}.h5"
done

# OHLCV Parquet tree (data/ohlcv/{exchange}/{symbol}/{tf}.parquet → ohlcv/…)
# Only syncs when DATA_STORE != s3 (when s3, the collector writes directly).
if [ -d "data/ohlcv" ] && [ "${DATA_STORE:-local_parquet}" != "s3" ]; then
    if aws s3 sync data/ohlcv "s3://${S3_BUCKET}/ohlcv" \
            --exclude "*" --include "*.parquet" --only-show-errors; then
        n_files=$(find data/ohlcv -name '*.parquet' | wc -l | tr -d ' ')
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [OK] ohlcv/ tree synced (${n_files} parquet files)" >> "${LOG_FILE}"
        UPLOADED=$((UPLOADED + 1))
    else
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [FAIL] ohlcv/ tree sync" >> "${LOG_FILE}"
        FAILED=$((FAILED + 1))
    fi
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [DONE] uploaded=${UPLOADED} failed=${FAILED}" >> "${LOG_FILE}"

if [ "${FAILED}" -gt 0 ]; then
    exit 1
fi
