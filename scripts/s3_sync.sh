#!/usr/bin/env bash
# Hourly S3 sync — intended for /etc/cron.hourly/ on the EC2 instance.
#
# Copies data/binance_ticks.h5 (and any per-symbol files under data/) to S3.
# Logs to logs/s3_sync.log.
#
# Environment variables (sourced from /app/.env if present):
#   S3_BUCKET   — required; target bucket name
#   APP_DIR     — defaults to /app

set -euo pipefail

APP_DIR="${APP_DIR:-/app}"
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

# Default single-symbol file
sync_file "data/binance_ticks.h5" "ticks/binance_ticks.h5"

# Per-symbol files from multi-instance setup (data/<SYMBOL>/binance_ticks.h5)
for dir in data/*/; do
    sym=$(basename "${dir}")
    f="${dir}binance_ticks.h5"
    sync_file "${f}" "ticks/binance_ticks_${sym}.h5"
done

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) [DONE] uploaded=${UPLOADED} failed=${FAILED}" >> "${LOG_FILE}"

if [ "${FAILED}" -gt 0 ]; then
    exit 1
fi
