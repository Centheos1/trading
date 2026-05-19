"""Headless live trading engine.

  Redis (raw trades + depth)  ──►  C++ OrderFlowEngine  ──►  signal callback
                                                              │
                                                              ▼
                                                       logger / execution

The engine owns one :class:`orderflow_engine.OrderFlowEngine` per
subscribed symbol and feeds it from the Redis pub/sub bus.  Generated
signals are logged; downstream execution is handled by separate
services (see :mod:`execution`).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Optional

import msgpack

# Locate the compiled C++ orderflow_engine extension before import.
_ENGINE_BUILD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backtestingCpp",
    "orderflow",
    "build",
)
if _ENGINE_BUILD_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_BUILD_DIR)

try:
    import orderflow_engine as ofe  # type: ignore
except ImportError as exc:  # pragma: no cover - C++ module unavailable at lint time
    ofe = None
    _import_error: Optional[Exception] = exc
else:
    _import_error = None

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-symbol engine state
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    symbol: str
    engine: object  # ofe.OrderFlowEngine


# ---------------------------------------------------------------------------
# LiveEngine
# ---------------------------------------------------------------------------


class LiveEngine:
    """Headless multi-symbol live engine.

    Usage::

        engine = LiveEngine(symbols=["BTCUSDT"])
        await engine.start()
        ...
        await engine.stop()
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        redis_url: str = "redis://localhost:6379",
        bucket_ms: int = 60_000,
    ) -> None:
        if ofe is None:
            raise RuntimeError(
                "orderflow_engine C++ module not built. "
                "Build: cd backtestingCpp/orderflow && ./build.sh"
            ) from _import_error
        self._symbols = [s.upper() for s in symbols]
        self._redis_url = redis_url
        self._bucket_ms = int(bucket_ms)

        self._states: dict[str, _SymbolState] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._build_engines()
        for sym in self._symbols:
            self._tasks.append(asyncio.create_task(self._consume_trades(sym)))
            self._tasks.append(asyncio.create_task(self._consume_depth(sym)))
        logger.info("LiveEngine started: symbols=%s", self._symbols)

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("LiveEngine stopped")

    # ----------------------------------------------------------- setup

    def _build_engines(self) -> None:
        for sym in self._symbols:
            engine = ofe.OrderFlowEngine()
            engine.set_signal_callback(self._make_signal_callback(sym))
            self._states[sym] = _SymbolState(symbol=sym, engine=engine)

    def _make_signal_callback(self, symbol: str):
        """Closure that logs each generated signal."""

        def _cb(sig) -> None:
            try:
                logger.info(
                    "signal symbol=%s ts_ms=%d type=%s price=%.6f strength=%.4f",
                    symbol,
                    int(sig.timestamp),
                    sig.type_name(),
                    float(sig.price),
                    float(getattr(sig, "strength", 0.0) or 0.0),
                )
            except Exception:
                logger.exception("Signal callback failed")

        return _cb

    # ---------------------------------------------------- Redis consumers

    async def _consume_trades(self, symbol: str) -> None:
        """Subscribe to ``trades:{symbol}`` and feed the C++ engine."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.error("redis package missing; cannot consume trades")
            return

        channel = f"trades:{symbol}"
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                client = aioredis.from_url(self._redis_url)
                pubsub = client.pubsub()
                await pubsub.subscribe(channel)
                logger.info("Subscribed to %s", channel)
                backoff = 1.0
                async for raw in pubsub.listen():
                    if raw.get("type") != "message":
                        continue
                    self._handle_trade_payload(symbol, raw.get("data") or b"")
                    if self._stopping.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Trade subscription error on %s", channel)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    async def _consume_depth(self, symbol: str) -> None:
        """Subscribe to ``depth:{symbol}`` and feed the C++ engine."""
        try:
            import redis.asyncio as aioredis
        except ImportError:
            logger.error("redis package missing; cannot consume depth")
            return

        channel = f"depth:{symbol}"
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                client = aioredis.from_url(self._redis_url)
                pubsub = client.pubsub()
                await pubsub.subscribe(channel)
                logger.info("Subscribed to %s", channel)
                backoff = 1.0
                async for raw in pubsub.listen():
                    if raw.get("type") != "message":
                        continue
                    self._handle_depth_payload(symbol, raw.get("data") or b"")
                    if self._stopping.is_set():
                        break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Depth subscription error on %s", channel)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, 30.0)

    # --------------------------------------------------- payload handlers

    def _handle_trade_payload(self, symbol: str, payload: bytes) -> None:
        try:
            obj = msgpack.unpackb(payload, raw=False)
        except Exception:
            logger.exception("Bad trade payload on %s", symbol)
            return

        state = self._states.get(symbol)
        if state is None:
            return

        ts_ms = int(obj.get("ts_ms") or 0)
        price = float(obj.get("price") or 0.0)
        qty = float(obj.get("qty") or 0.0)
        is_buyer_maker = bool(obj.get("is_buyer_maker", False))
        if price <= 0 or qty <= 0:
            return

        trade = ofe.Trade()
        trade.timestamp = ts_ms
        trade.price = price
        trade.quantity = qty
        trade.is_buyer_maker = is_buyer_maker
        state.engine.process_trade(trade)

    def _handle_depth_payload(self, symbol: str, payload: bytes) -> None:
        try:
            obj = msgpack.unpackb(payload, raw=False)
        except Exception:
            logger.exception("Bad depth payload on %s", symbol)
            return

        state = self._states.get(symbol)
        if state is None:
            return

        ts_ms = int(obj.get("ts_ms") or 0)
        update = ofe.DepthUpdate()
        update.timestamp = ts_ms
        update.first_update_id = int(obj.get("first_update_id") or 0)
        update.final_update_id = int(obj.get("final_update_id") or 0)
        update.is_snapshot = bool(obj.get("is_snapshot", False))
        bids = []
        for b in obj.get("bids", []):
            lv = ofe.DepthLevel()
            lv.price = float(b[0])
            lv.quantity = float(b[1])
            bids.append(lv)
        asks = []
        for a in obj.get("asks", []):
            lv = ofe.DepthLevel()
            lv.price = float(a[0])
            lv.quantity = float(a[1])
            asks.append(lv)
        update.bids = bids
        update.asks = asks
        state.engine.process_depth(update)

    # ---------------------------------------------------- configuration

    def set_bucket_ms(self, bucket_ms: int) -> None:
        bucket_ms = int(bucket_ms)
        if bucket_ms <= 0 or bucket_ms == self._bucket_ms:
            return
        self._bucket_ms = bucket_ms
        logger.info("LiveEngine bucket_ms set to %d", bucket_ms)

    def get_status(self) -> dict:
        return {
            "symbols": list(self._symbols),
            "bucket_ms": self._bucket_ms,
            "engine_running": not self._stopping.is_set(),
        }
