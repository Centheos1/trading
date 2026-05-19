"""FastAPI entry point for the strategy service.

Wires together:

* :class:`strategy.engine.live_engine.LiveEngine` — consumes Redis,
  runs the C++ engine, processes signals.
* :mod:`strategy.api.rest_routes` — control plane (backtest, optimise,
  strategy parameters).

Run with::

    uvicorn strategy.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from strategy.api import rest_routes
from strategy.engine.live_engine import LiveEngine


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT").split(",") if s.strip()]
BUCKET_MS = int(os.getenv("CANDLE_BUCKET_MS", "60000"))


# ---------------------------------------------------------------------------
# Engine parameter manager
# ---------------------------------------------------------------------------


class _ParamManager:
    """Holds the current SignalParams and pushes them into every per-symbol
    OrderFlowEngine when patched."""

    def __init__(self, engine: LiveEngine) -> None:
        self._engine = engine
        self._params: dict = {}

    def get(self) -> dict:
        return dict(self._params)

    def patch(self, update) -> dict:
        applied = update.model_dump(exclude_none=True)
        if not applied:
            return self.get()
        # Apply to each C++ engine's SignalParams.
        for state in self._engine._states.values():  # noqa: SLF001
            try:
                signal_engine = state.engine.get_signal_engine()
                params = signal_engine.get_params()
                for k, v in applied.items():
                    if hasattr(params, k):
                        setattr(params, k, v)
                signal_engine.set_params(params)
            except Exception:
                logger.exception("Failed to apply params to %s", state.symbol)
        self._params.update(applied)
        return self.get()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


engine: LiveEngine | None = None
params_mgr: _ParamManager | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, params_mgr

    engine = LiveEngine(
        symbols=SYMBOLS,
        redis_url=REDIS_URL,
        bucket_ms=BUCKET_MS,
    )
    params_mgr = _ParamManager(engine)

    await engine.start()
    logger.info(
        "Strategy service running — symbols=%s redis=%s",
        SYMBOLS, REDIS_URL,
    )

    try:
        yield
    finally:
        if engine is not None:
            await engine.stop()


app = FastAPI(title="Strategy Service", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict:
    return {
        "service": "strategy",
        "endpoints": {
            "rest": [
                "GET  /api/health",
                "GET  /api/strategy",
                "PATCH /api/strategy",
                "POST /api/backtest",
                "POST /api/optimise",
                "GET  /api/optimise/{job_id}",
            ],
        },
    }


# REST router — bound to engine accessors via closures.
app.include_router(
    rest_routes.make_router(
        engine_status_fn=lambda: (engine.get_status() if engine else {}),
        get_engine_params_fn=lambda: (params_mgr.get() if params_mgr else {}),
        set_engine_params_fn=lambda update: (
            params_mgr.patch(update) if params_mgr else {}
        ),
    )
)
