# Data collection service — Binance WS → HDF5 + Redis pub/sub.
#
# Build context MUST be the repo root so the image can COPY the C++
# engine sources and the existing Python collectors:
#
#   docker build -f data/Dockerfile -t trading-data .
#
# At runtime the service publishes raw trades and depth updates to
# Redis channels ``trades:{SYMBOL}`` and ``depth:{SYMBOL}`` whenever the
# ``REDIS_URL`` environment variable is set.
#
# It also persists ticks to HDF5 on a mounted volume so the strategy
# service can run backtests against historical data.

FROM python:3.12-slim-bookworm AS base

ARG DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# System packages — C++ toolchain for the orderflow_engine extension.
# We need it here because TickDataCollector uses ofe.TickStore / process_*
# to populate HDF5 + the canonical engine state.  No Qt, no GUI libs.
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    pkg-config \
    libboost-all-dev \
    libhdf5-dev \
    libssl-dev \
    nlohmann-json3-dev \
    pybind11-dev \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------------------------------------------------------------------------
# C++ orderflow_engine — built first so pip changes do not bust this layer.
# ---------------------------------------------------------------------------
COPY backtestingCpp/ backtestingCpp/
RUN python -m venv .venv \
    && .venv/bin/pip install --quiet pybind11
RUN bash backtestingCpp/orderflow/build.sh

# ---------------------------------------------------------------------------
# Python deps — headless subset (Redis client + msgpack added).
# ---------------------------------------------------------------------------
COPY data/requirements.txt /tmp/data-requirements.txt
RUN .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r /tmp/data-requirements.txt

ENV PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUNBUFFERED=1

# ---------------------------------------------------------------------------
# Application source.  Only the modules actually used by the collectors
# are copied — keep the image lean.
# ---------------------------------------------------------------------------
COPY collect_ticks.py collect_ohlcv.py data_service.py database.py \
     ohlcv_store.py utils.py schemas.py models.py /app/
COPY data_feed/ /app/data_feed/
COPY exchanges/ /app/exchanges/

# HDF5 storage lives on a mounted volume so it survives container restarts.
VOLUME ["/app/data"]

# Default runs the BTCUSDT tick collector.  Override via docker compose
# `command:` to launch the OHLCV collector instead.
CMD ["python", "collect_ticks.py", "--symbol", "BTCUSDT"]
