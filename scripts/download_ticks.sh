#!/usr/bin/env bash
# Developer-side download helper.
# Downloads the latest tick HDF5 from S3 to data/ before running the HMM campaign.
#
# Usage:
#   bash scripts/download_ticks.sh                   # BTCUSDT only
#   bash scripts/download_ticks.sh BTCUSDT ETHUSDT   # multiple symbols
#
# Requires S3_BUCKET in environment or .env

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${APP_DIR}"

# Source .env if present
if [ -f .env ]; then
    # shellcheck disable=SC1091
    set -o allexport
    source .env
    set +o allexport
fi

if [ -z "${S3_BUCKET:-}" ]; then
    echo "ERROR: S3_BUCKET is not set. Export it or add it to .env." >&2
    exit 1
fi

mkdir -p data

SYMBOLS=("${@:-BTCUSDT}")

for sym in "${SYMBOLS[@]}"; do
    sym_upper="${sym^^}"
    if [ "${sym_upper}" = "BTCUSDT" ] && [ ${#SYMBOLS[@]} -eq 1 ]; then
        # Default path (single symbol, backward compat)
        S3_KEY="ticks/binance_ticks.h5"
        LOCAL="data/binance_ticks.h5"
    else
        S3_KEY="ticks/binance_ticks_${sym_upper}.h5"
        LOCAL="data/binance_ticks_${sym_upper}.h5"
    fi

    echo "Downloading s3://${S3_BUCKET}/${S3_KEY} → ${LOCAL}"
    aws s3 cp "s3://${S3_BUCKET}/${S3_KEY}" "${LOCAL}"
    echo "Done: ${LOCAL}"
done

echo ""
echo "Verify with:"
echo "  python -c \"import h5py; f=h5py.File('data/binance_ticks.h5'); print(list(f.keys()))\""
