"""Headless live trading engine.

Replaces the old ``ui/live_trading_session.py`` which was tightly coupled
to ``MainWindow`` and Qt widgets.  Here the engine is a pure data pipeline:

  Redis (raw trades + depth)  ──►  C++ OrderFlowEngine  ──►  emit() callback
                                                              │
                                                              ▼
                                                       msgpack messages
                                                       (handed to ws_server)

The engine owns:

* One :class:`orderflow_engine.OrderFlowEngine` per subscribed symbol.
* A simple in-memory candle aggregator (default 60 s buckets, configurable).
* A periodic publisher (~10 Hz) that snapshots the order book, CVD, and
  volume profile.

It is intentionally rendering-agnostic — every message it emits is raw
data (price, size, volume, signal type) and the UI service is responsible
for all display decisions.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

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

from strategy.engine import stream_emitter as se

logger = logging.getLogger(__name__)


# Type aliases
EmitCallback = Callable[[bytes], Awaitable[None]]
"""Async callback that ingests a single msgpack-encoded message."""


# ---------------------------------------------------------------------------
# Candle aggregator
# ---------------------------------------------------------------------------


@dataclass
class _Candle:
    ts: int = 0
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0
    vol: float = 0.0
    buy_vol: float = 0.0
    sell_vol: float = 0.0


_CANDLE_HISTORY_SIZE = 500


@dataclass
class _CandleAggregator:
    """Folds trades into time-bucketed OHLCV candles.

    Emits a CANDLE message once per bucket close (when a trade arrives
    whose timestamp falls into the next bucket).  Live in-bucket updates
    are streamed as individual TRADE messages so the UI can update the
    last candle in real time.
    """

    bucket_ms: int = 60_000
    current: Optional[_Candle] = None
    _history: deque = field(
        default_factory=lambda: deque(maxlen=_CANDLE_HISTORY_SIZE)
    )

    def add_trade(
        self, ts_ms: int, price: float, qty: float, is_buyer_maker: bool
    ) -> Optional[_Candle]:
        """Ingest a trade.  Returns the just-closed candle, if any."""
        bucket_ts = (ts_ms // self.bucket_ms) * self.bucket_ms
        closed: Optional[_Candle] = None
        if self.current is None:
            self.current = _Candle(ts=bucket_ts, o=price, h=price, l=price, c=price)
        elif bucket_ts > self.current.ts:
            closed = self.current
            self._history.append(closed)
            self.current = _Candle(ts=bucket_ts, o=price, h=price, l=price, c=price)

        c = self.current
        c.h = max(c.h, price)
        c.l = min(c.l, price)
        c.c = price
        c.vol += qty
        if is_buyer_maker:
            c.sell_vol += qty
        else:
            c.buy_vol += qty
        return closed

    def get_snapshot(self, limit: int = _CANDLE_HISTORY_SIZE) -> list[_Candle]:
        """Return recent closed candles plus the in-progress candle (if any)."""
        result = list(self._history)[-limit:]
        if self.current is not None:
            result.append(self.current)
        return result


# ---------------------------------------------------------------------------
# Per-symbol engine state
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    symbol: str
    engine: object  # ofe.OrderFlowEngine
    candles: _CandleAggregator
    last_book_emit_ms: float = 0.0
    last_cvd_value: float = 0.0
    book_levels: int = 50
    """Number of book levels (per side) emitted per BOOK_UPDATE.  The UI
    aggregates this raw data; the strategy service has no knowledge of
    display tick size or price range."""


# ---------------------------------------------------------------------------
# LiveEngine
# ---------------------------------------------------------------------------


class LiveEngine:
    """Headless multi-symbol live engine.

    Usage::

        async def emit(blob: bytes) -> None:
            ...   # forward to all connected WebSocket clients

        engine = LiveEngine(emit=emit, symbols=["BTCUSDT"])
        await engine.start()
        ...
        await engine.stop()
    """

    def __init__(
        self,
        *,
        emit: EmitCallback,
        symbols: list[str],
        redis_url: str = "redis://localhost:6379",
        bucket_ms: int = 60_000,
        book_emit_hz: float = 10.0,
        book_levels: int = 50,
    ) -> None:
        if ofe is None:
            raise RuntimeError(
                "orderflow_engine C++ module not built. "
                "Build: cd backtestingCpp/orderflow && ./build.sh"
            ) from _import_error
        self._emit = emit
        self._symbols = [s.upper() for s in symbols]
        self._redis_url = redis_url
        self._bucket_ms = int(bucket_ms)
        self._book_period_s = max(1.0 / max(book_emit_hz, 0.1), 0.01)
        self._book_levels = int(book_levels)

        self._states: dict[str, _SymbolState] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._build_engines()
        # Each symbol gets a trades task, a depth task, and shares a
        # single periodic book emitter.
        for sym in self._symbols:
            self._tasks.append(asyncio.create_task(self._consume_trades(sym)))
            self._tasks.append(asyncio.create_task(self._consume_depth(sym)))
        self._tasks.append(asyncio.create_task(self._periodic_book_emitter()))
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
            self._states[sym] = _SymbolState(
                symbol=sym,
                engine=engine,
                candles=_CandleAggregator(bucket_ms=self._bucket_ms),
                book_levels=self._book_levels,
            )

    def _make_signal_callback(self, symbol: str):
        """Closure that converts a C++ Signal struct into a SIGNAL msg."""

        def _cb(sig) -> None:
            try:
                msg = se.signal_msg(
                    symbol=symbol,
                    ts_ms=int(sig.timestamp),
                    signal_type=sig.type_name(),
                    price=float(sig.price),
                    strength=float(getattr(sig, "strength", 0.0) or 0.0),
                )
                self._schedule_emit(se.encode(msg))
            except Exception:
                logger.exception("Signal callback failed")

        return _cb

    def _schedule_emit(self, blob: bytes) -> None:
        """Schedule emit() from any thread.

        The C++ signal callback may fire from a non-asyncio context, so
        we cannot ``await`` directly.  ``run_coroutine_threadsafe`` keeps
        the path safe.
        """
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._emit(blob), self._loop)
        except RuntimeError:
            # Loop is closed during shutdown — drop the message silently.
            pass

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

        # Forward as raw TRADE message.
        self._schedule_emit(
            se.encode(
                se.trade_msg(
                    symbol=symbol,
                    ts_ms=ts_ms,
                    price=price,
                    qty=qty,
                    is_buyer_maker=is_buyer_maker,
                )
            )
        )

        # Update the candle aggregator; if a bucket closed, emit it.
        closed = state.candles.add_trade(ts_ms, price, qty, is_buyer_maker)
        if closed is not None:
            self._schedule_emit(
                se.encode(
                    se.candle_msg(
                        symbol=symbol,
                        ts_ms=closed.ts,
                        bucket_ms=state.candles.bucket_ms,
                        o=closed.o,
                        h=closed.h,
                        l=closed.l,
                        c=closed.c,
                        vol=closed.vol,
                        buy_vol=closed.buy_vol,
                        sell_vol=closed.sell_vol,
                    )
                )
            )

        # CVD changes on every trade; emit a lightweight CVD_UPDATE.
        try:
            cvd_value = float(state.engine.get_cvd().get_cvd())
        except Exception:
            cvd_value = state.last_cvd_value
        if cvd_value != state.last_cvd_value:
            state.last_cvd_value = cvd_value
            self._schedule_emit(
                se.encode(
                    se.cvd_msg(symbol=symbol, ts_ms=ts_ms, value=cvd_value)
                )
            )

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

        # BOOK_UPDATE messages are throttled to ``book_emit_hz`` so a
        # noisy 100 ms depth feed doesn't saturate slow clients — see
        # _periodic_book_emitter().

    # ------------------------------------------------ periodic publisher

    async def _periodic_book_emitter(self) -> None:
        """Emit BOOK_UPDATE and VP_UPDATE at ``book_emit_hz``."""
        while not self._stopping.is_set():
            try:
                await asyncio.sleep(self._book_period_s)
                ts_ms = int(time.time() * 1000)
                for state in self._states.values():
                    await self._emit_book_and_vp(state, ts_ms)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Periodic emitter error")

    async def _emit_book_and_vp(self, state: _SymbolState, ts_ms: int) -> None:
        """Snapshot the C++ order book and volume profile for ``state``."""
        try:
            snap = state.engine.get_order_book().get_snapshot()
            bids_raw = snap.get_bids()[: state.book_levels]
            asks_raw = snap.get_asks()[: state.book_levels]
        except Exception:
            return
        # Don't broadcast an empty book snapshot — the frontend auto-range
        # guard requires non-empty bids/asks to initialise the price axis.
        if not bids_raw or not asks_raw:
            return
        await self._emit(
            se.encode(
                se.book_update_msg(
                    symbol=state.symbol,
                    ts_ms=ts_ms,
                    bids=bids_raw,
                    asks=asks_raw,
                )
            )
        )

        try:
            vp_rows = state.engine.get_volume_profile().get_profile()
        except Exception:
            vp_rows = []
        # ``vp_rows`` is a list of VolumeNode (price, volume, buy_vol, sell_vol)
        bars: list[tuple[float, float, float, float]] = []
        for node in vp_rows:
            bars.append(
                (
                    float(getattr(node, "price", 0.0)),
                    float(getattr(node, "volume", 0.0)),
                    float(getattr(node, "buy_volume", 0.0)),
                    float(getattr(node, "sell_volume", 0.0)),
                )
            )
        if bars:
            await self._emit(
                se.encode(
                    se.vp_msg(symbol=state.symbol, ts_ms=ts_ms, bars=bars)
                )
            )

    # ---------------------------------------------------- configuration

    def get_candles(
        self, symbol: str, limit: int = 200
    ) -> list[dict]:
        """Return recent OHLCV candles (closed + current) for a symbol.

        Used by the REST ``/api/candles`` endpoint to preload the UI chart
        on first connection rather than waiting for the first candle close.
        """
        state = self._states.get(symbol.upper())
        if state is None:
            return []
        rows = state.candles.get_snapshot(limit)
        return [
            {
                "ts_ms": int(c.ts),
                "o": float(c.o),
                "h": float(c.h),
                "l": float(c.l),
                "c": float(c.c),
                "vol": float(c.vol),
                "buy_vol": float(c.buy_vol),
                "sell_vol": float(c.sell_vol),
            }
            for c in rows
        ]

    def set_bucket_ms(self, bucket_ms: int) -> None:
        bucket_ms = int(bucket_ms)
        if bucket_ms <= 0 or bucket_ms == self._bucket_ms:
            return
        self._bucket_ms = bucket_ms
        for state in self._states.values():
            state.candles = _CandleAggregator(bucket_ms=bucket_ms)
        logger.info("LiveEngine bucket_ms set to %d", bucket_ms)

    def get_status(self) -> dict:
        return {
            "symbols": list(self._symbols),
            "bucket_ms": self._bucket_ms,
            "book_emit_hz": 1.0 / self._book_period_s,
            "book_levels": self._book_levels,
            "engine_running": not self._stopping.is_set(),
        }
