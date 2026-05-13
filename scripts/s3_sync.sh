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

sync_file() {
    local local_path="$1"
    local s3_key="$2"
    if [ ! -f "${local_path}" ]; then
        return 0
    fi
    if aws s3 cp "${local_path}" "s3://${S3_BUCKET}/${s3_key}" --only-show-errors; then
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [OK] ${local_path} → s3://${S3_BUCKET}/${s3_key}" >> "${LOG_FILE}"
        UPLOADED=$((UPLOADED + 1))
    else
        echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [FAIL] ${local_path}" >> "${LOG_FILE}"
        FAILED=$((FAILED + 1))
    fi
}

# Default single-symbol tick file
sync_file "data/binance_ticks.h5" "ticks/binance_ticks.h5"

# Per-symbol tick files from multi-instance setup (data/<SYMBOL>/binance_ticks.h5)
# Skip data/ohlcv/ — that's a directory of Parquet files synced separately below.
for dir in data/*/; do
    sym=$(basename "${dir}")
    if [ "${sym}" = "ohlcv" ]; then
        continue
    fi
    f="${dir}binance_ticks.h5"
    sync_file "${f}" "ticks/binance_ticks_${sym}.h5"
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
