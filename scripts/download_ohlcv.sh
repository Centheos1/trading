#!/usr/bin/env bash
# Developer-side helper to download OHLCV Parquet files from S3 to data/ohlcv/
# for local backtesting / PCA work.
#
# Usage:
#   bash scripts/download_ohlcv.sh                                # all exchanges, all symbols (large!)
#   bash scripts/download_ohlcv.sh --exchange binance             # only binance
#   bash scripts/download_ohlcv.sh --exchange binance --symbol BTCUSDT
#   bash scripts/download_ohlcv.sh --exchange oanda --symbol EUR_USD --timeframe 1m
#
# Requires S3_BUCKET in environment or .env.

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

EXCHANGE=""
SYMBOL=""
TIMEFRAME="1m"

while [ $# -gt 0 ]; do
    case "$1" in
        --exchange)   EXCHANGE="$2"; shift 2;;
        --symbol)     SYMBOL="$2"; shift 2;;
        --timeframe)  TIMEFRAME="$2"; shift 2;;
        -h|--help)
            head -n 12 "$0" | tail -n 10
            exit 0;;
        *)
            echo "Unknown arg: $1" >&2
            exit 1;;
    esac
done

mkdir -p data/ohlcv

if [ -n "${EXCHANGE}" ] && [ -n "${SYMBOL}" ]; then
    KEY="ohlcv/${EXCHANGE}/${SYMBOL}/${TIMEFRAME}.parquet"
    LOCAL="data/${KEY}"
    mkdir -p "$(dirname "${LOCAL}")"
    echo "Downloading s3://${S3_BUCKET}/${KEY} → ${LOCAL}"
    aws s3 cp "s3://${S3_BUCKET}/${KEY}" "${LOCAL}"
elif [ -n "${EXCHANGE}" ]; then
    echo "Downloading all ${EXCHANGE} ${TIMEFRAME} parquet files…"
    aws s3 sync \
        "s3://${S3_BUCKET}/ohlcv/${EXCHANGE}" \
        "data/ohlcv/${EXCHANGE}" \
        --exclude "*" --include "*/${TIMEFRAME}.parquet"
else
    echo "Downloading all OHLCV ${TIMEFRAME} parquet files (this may be large)…"
    aws s3 sync \
        "s3://${S3_BUCKET}/ohlcv" \
        "data/ohlcv" \
        --exclude "*" --include "*/${TIMEFRAME}.parquet"
fi

echo ""
echo "Verify with:"
echo "  python -c \"import pandas as pd; df=pd.read_parquet('data/ohlcv/binance/BTCUSDT/1m.parquet'); print(df.tail())\""
