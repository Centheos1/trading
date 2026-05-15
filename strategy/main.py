"""FastAPI entry point for the strategy service.

Wires together:

* :class:`strategy.engine.live_engine.LiveEngine` — consumes Redis,
  runs the C++ engine, emits msgpack messages.
* :class:`strategy.api.ws_server.StreamHub` — fans out those messages
  to connected WebSocket clients.
* :mod:`strategy.api.rest_routes` — control plane (backtest, optimise,
  strategy parameters).

Run with::

    uvicorn strategy.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from strategy.api import rest_routes, ws_server
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
BOOK_EMIT_HZ = float(os.getenv("BOOK_EMIT_HZ", "10"))
BOOK_LEVELS = int(os.getenv("BOOK_LEVELS", "50"))


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


hub = ws_server.StreamHub()
engine: LiveEngine | None = None
params_mgr: _ParamManager | None = None
_health_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, params_mgr, _health_task

    engine = LiveEngine(
        emit=hub.broadcast,
        symbols=SYMBOLS,
        redis_url=REDIS_URL,
        bucket_ms=BUCKET_MS,
        book_emit_hz=BOOK_EMIT_HZ,
        book_levels=BOOK_LEVELS,
    )
    params_mgr = _ParamManager(engine)
    hub.set_engine_status_fn(engine.get_status)

    await engine.start()
    _health_task = asyncio.create_task(hub.health_loop())
    logger.info(
        "Strategy service running — symbols=%s redis=%s ws_port=8000",
        SYMBOLS, REDIS_URL,
    )

    try:
        yield
    finally:
        if _health_task is not None:
            _health_task.cancel()
        if engine is not None:
            await engine.stop()


app = FastAPI(title="Strategy Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in os.getenv(
            "ALLOWED_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000",
        ).split(",")
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root() -> dict:
    return {
        "service": "strategy",
        "endpoints": {
            "websocket": "/stream",
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


# WebSocket router
app.include_router(ws_server.make_router(hub))

# REST router — bound to engine accessors via closures.
app.include_router(
    rest_routes.make_router(
        engine_status_fn=lambda: (engine.get_status() if engine else {}),
        get_engine_params_fn=lambda: (params_mgr.get() if params_mgr else {}),
        set_engine_params_fn=lambda update: (
            params_mgr.patch(update) if params_mgr else {}
        ),
        get_engine_candles_fn=lambda symbol, limit: (
            engine.get_candles(symbol, limit) if engine else []
        ),
    )
)
