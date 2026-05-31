#!/usr/bin/env bash
# Developer-side download helper.
# Downloads the latest tick HDF5 from S3 to data/ticks/ before running
# the HMM campaign or other tick-driven analyses.
#
# Usage:
#   bash scripts/download_ticks.sh                   # BTCUSDT only
#   bash scripts/download_ticks.sh BTCUSDT ETHUSDT   # multiple symbols
#
# Requires S3_BUCKET in environment or .env.
#
# For windowed reads, prefer the date-partitioned Parquet mirror via
# notebooks/utils.py :: load_ticks_parquet — this script is intended
# for the few cases where the entire HDF5 is needed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${APP_DIR}"

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

mkdir -p data/ticks

SYMBOLS=("${@:-BTCUSDT}")

for sym in "${SYMBOLS[@]}"; do
    sym_upper="${sym^^}"
    S3_KEY="ticks/${sym_upper}_ticks.h5"
    LOCAL="data/ticks/${sym_upper}_ticks.h5"

    echo "Downloading s3://${S3_BUCKET}/${S3_KEY} → ${LOCAL}"
    if aws s3 cp "s3://${S3_BUCKET}/${S3_KEY}" "${LOCAL}"; then
        echo "Done: ${LOCAL}"
    else
        # Fall back to the pre-May-2026 S3 layout so we can still recover
        # archives created before the rename.
        if [ "${sym_upper}" = "BTCUSDT" ]; then
            legacy_key="ticks/binance_ticks.h5"
        else
            legacy_key="ticks/binance_ticks_${sym_upper}.h5"
        fi
        echo "  primary key missing — trying legacy s3://${S3_BUCKET}/${legacy_key}"
        aws s3 cp "s3://${S3_BUCKET}/${legacy_key}" "${LOCAL}"
        echo "Done (from legacy key): ${LOCAL}"
    fi
done

echo ""
echo "Verify with:"
echo "  python -c \"import h5py; f=h5py.File('data/ticks/BTCUSDT_ticks.h5'); print(list(f.keys()))\""
