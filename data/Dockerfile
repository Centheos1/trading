# Data collection service — venue WS → immutable Parquet shards + Redis Streams.
#
# Build context MUST be the repo root:
#
#   docker build -f data/Dockerfile -t trading-data .
#
# Greenfield: no C++ TickStore / libhdf5 in this image.

FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY data/requirements.txt /tmp/data-requirements.txt
RUN python -m venv .venv \
    && .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r /tmp/data-requirements.txt

ENV PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUNBUFFERED=1

COPY collect_ticks.py collect_ohlcv.py data_service.py \
     ohlcv_store.py tick_parquet_store.py market_data.py redis_md_bus.py \
     utils.py schemas.py models.py /app/
COPY adapters/ /app/adapters/
COPY data_feed/ /app/data_feed/
COPY exchanges/ /app/exchanges/

VOLUME ["/app/data"]

CMD ["python", "collect_ticks.py", "--symbol", "BTCUSDT"]
