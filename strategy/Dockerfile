# Strategy service — headless OrderFlowEngine + FastAPI REST.
#
# Build context MUST be the repo root:
#
#   docker build -f strategy/Dockerfile -t trading-strategy .
#
# The image:
#   * compiles backtestingCpp/orderflow into a Python extension
#   * installs FastAPI/uvicorn/redis/msgpack + the platform deps used by
#     the backtester / optimiser
#   * runs ``uvicorn strategy.main:app --host 0.0.0.0 --port 8000``
#
# It is pure compute.  HDF5 storage is mounted read-only at /app/data so
# backtests can replay historical ticks.

FROM python:3.12-slim-bookworm AS base

ARG DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------
# System packages — C++ toolchain for orderflow_engine.
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
# C++ orderflow_engine.
# ---------------------------------------------------------------------------
COPY backtestingCpp/ backtestingCpp/
RUN python -m venv .venv \
    && .venv/bin/pip install --quiet pybind11
RUN bash backtestingCpp/orderflow/build.sh

# ---------------------------------------------------------------------------
# Python deps.
# ---------------------------------------------------------------------------
COPY strategy/requirements.txt /tmp/strategy-requirements.txt
RUN .venv/bin/pip install --upgrade pip setuptools wheel \
    && .venv/bin/pip install -r /tmp/strategy-requirements.txt

ENV PATH="/app/.venv/bin:${PATH}" \
    VIRTUAL_ENV="/app/.venv" \
    PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    REDIS_URL=redis://redis:6379 \
    SYMBOLS=BTCUSDT

# ---------------------------------------------------------------------------
# Application source — copy only what the strategy service needs.
# ---------------------------------------------------------------------------
COPY strategy/ /app/strategy/
COPY strategies/ /app/strategies/
COPY data_feed/ /app/data_feed/
COPY exchanges/ /app/exchanges/
COPY execution/ /app/execution/
COPY backtester.py optimiser.py database.py ohlcv_store.py \
     utils.py schemas.py models.py strategy_store.py /app/

# HDF5 storage (read-only for backtests).
VOLUME ["/app/data"]

EXPOSE 8000

# Default to production-like config; docker-compose overrides for dev.
CMD ["uvicorn", "strategy.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
