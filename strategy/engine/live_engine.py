"""Headless live trading engine (Phase 21 — V1-compliant live path).

  Redis (trades + depth)  ──►  C++ OrderFlowEngine  ──►  set_ripple_callback
                                      │                          │
                                      │                          ▼
                                      │                  ExecutionBridge gate
                                      │                  (intent_risk_block_reason)
                                      │                          │
                          layered push thread                    ▼
                  (Tide 60s / Wave 5s / RV 1s)        OBSERVE | PAPER | LIVE
                                                       (ExecutionManager → broker)

The engine owns one :class:`orderflow_engine.OrderFlowEngine` per
subscribed symbol and feeds it from the Redis pub/sub bus.  Ripple
decisions are routed through :class:`strategy.engine.execution_bridge.ExecutionBridge`
to the V1-compliant execution path (gate → broker), mirroring the
monolith's ``execution/live_runner.py``.  By default the service runs in
``OBSERVE`` mode (``ARM_EXECUTION=false``) — decisions are gated and
logged but no orders are placed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

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

from execution.models import SizingConfig, SizingMode  # noqa: E402
from strategy.engine.execution_bridge import (  # noqa: E402
    ExecutionBridge,
    ExecutionMode,
    resolve_mode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-symbol engine state
# ---------------------------------------------------------------------------


@dataclass
class _SymbolState:
    symbol: str
    engine: object  # ofe.OrderFlowEngine
    bridge: Optional[ExecutionBridge] = None
    target: Any = None  # ExecutionManager | PaperEngine | None
    tide_engine: Any = None
    wave_engine: Any = None
    rv_price_buf: deque = field(default_factory=lambda: deque(maxlen=60))
    last_trade_ts_holder: list = field(default_factory=lambda: [0])
    push_stop_event: Optional[threading.Event] = None
    push_thread: Optional[threading.Thread] = None


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

    Live execution is opt-in via ``arm_execution`` (env ``ARM_EXECUTION``).
    With the default ``arm_execution=False`` the engine runs in OBSERVE
    mode: it gates and logs Ripple decisions but never places an order.
    """

    def __init__(
        self,
        *,
        symbols: list[str],
        redis_url: str = "redis://localhost:6379",
        bucket_ms: int = 60_000,
        arm_execution: bool = False,
        execution_broker: str = "paper",
        max_position_usd: float = 0.0,
        sizing_value: float = 0.001,
        max_position_qty: float = 0.01,
        cooldown_s: float = 5.0,
        enable_layered_strategy: bool = True,
    ) -> None:
        if ofe is None:
            raise RuntimeError(
                "orderflow_engine C++ module not built. "
                "Build: cd backtestingCpp/orderflow && ./build.sh"
            ) from _import_error
        self._symbols = [s.upper() for s in symbols]
        self._redis_url = redis_url
        self._bucket_ms = int(bucket_ms)

        self._arm_execution = bool(arm_execution)
        self._execution_broker = execution_broker
        self._mode = resolve_mode(self._arm_execution, self._execution_broker)
        self._max_position_usd = float(max_position_usd)
        self._sizing = SizingConfig(
            mode=SizingMode.FIXED_QTY,
            value=float(sizing_value),
            max_position=float(max_position_qty),
        )
        self._cooldown_s = float(cooldown_s)
        self._enable_layered_strategy = bool(enable_layered_strategy)

        self._states: dict[str, _SymbolState] = {}
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # --------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._build_engines()
        # Connecting the broker (LIVE mode) blocks; do it off the event loop.
        if self._mode is not ExecutionMode.OBSERVE:
            await self._loop.run_in_executor(None, self._start_execution_targets)
        self._start_layered_push()
        for sym in self._symbols:
            self._tasks.append(asyncio.create_task(self._consume_trades(sym)))
            self._tasks.append(asyncio.create_task(self._consume_depth(sym)))
        logger.info(
            "LiveEngine started: symbols=%s mode=%s broker=%s",
            self._symbols, self._mode.value, self._execution_broker,
        )

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        # Stop layered-push threads and tear down execution targets.
        for state in self._states.values():
            if state.push_stop_event is not None:
                state.push_stop_event.set()
        for state in self._states.values():
            if state.push_thread is not None and state.push_thread.is_alive():
                state.push_thread.join(timeout=5.0)
        if self._mode is not ExecutionMode.OBSERVE and self._loop is not None:
            await self._loop.run_in_executor(None, self._stop_execution_targets)
        logger.info("LiveEngine stopped")

    # ----------------------------------------------------------- setup

    def _build_engines(self) -> None:
        for sym in self._symbols:
            engine = ofe.OrderFlowEngine()
            state = _SymbolState(symbol=sym, engine=engine)

            # Layered-strategy engines (best-effort — the service still
            # runs without Tide/Wave push, falling back to engine defaults).
            if self._enable_layered_strategy:
                try:
                    from tide.tide_engine import TideEngine
                    from wave.wave_engine import WaveEngine
                    state.tide_engine = TideEngine()
                    state.wave_engine = WaveEngine()
                except Exception:
                    logger.exception(
                        "layered-strategy init failed for %s — "
                        "running without Tide/Wave push", sym,
                    )
                    state.tide_engine = None
                    state.wave_engine = None

            target = self._make_execution_target(sym)
            bridge = ExecutionBridge(
                symbol=sym,
                ofe_module=ofe,
                mode=self._mode,
                target=target,
                sizing=self._sizing,
            )
            state.target = target
            state.bridge = bridge

            # Ripple-driven, risk-gated execution (V1 §22.2 #4/#8/#9/#12).
            try:
                engine.set_ripple_callback(bridge.make_ripple_callback(engine))
            except Exception:
                logger.exception("set_ripple_callback failed for %s", sym)
            # Observation-only signal log (never routes orders).
            engine.set_signal_callback(self._make_signal_callback(sym))

            self._states[sym] = state

    def _make_execution_target(self, symbol: str) -> Any:
        """Construct the execution sink for the current mode.

        OBSERVE → ``None``. PAPER → :class:`PaperEngine`. LIVE →
        :class:`ExecutionManager` backed by :class:`BinanceBroker`.

        Any failure constructing a LIVE/PAPER target degrades the symbol
        to observe-only (``None``) rather than crashing the service.
        """
        if self._mode is ExecutionMode.OBSERVE:
            return None
        try:
            if self._mode is ExecutionMode.PAPER:
                from execution.paper_engine import PaperEngine
                return PaperEngine(symbol, self._sizing)
            from execution.binance_broker import BinanceBroker
            from execution.execution_manager import ExecutionManager
            return ExecutionManager(
                BinanceBroker(), symbol, self._sizing,
                cooldown_s=self._cooldown_s,
            )
        except Exception:
            logger.exception(
                "execution target init failed for %s — degrading to OBSERVE",
                symbol,
            )
            return None

    def _start_execution_targets(self) -> None:
        """Connect + arm any live ``ExecutionManager`` targets (blocking)."""
        for state in self._states.values():
            target = state.target
            if target is None:
                continue
            # PaperEngine has no broker lifecycle; only ExecutionManager
            # needs start() (connect) before arm().
            if hasattr(target, "start"):
                try:
                    ok = target.start()
                    if not ok:
                        logger.error(
                            "ExecutionManager.start() failed for %s — "
                            "degrading to OBSERVE", state.symbol,
                        )
                        state.target = None
                        if state.bridge is not None:
                            state.bridge.mode = ExecutionMode.OBSERVE
                            state.bridge._target = None  # noqa: SLF001
                        continue
                except Exception:
                    logger.exception(
                        "ExecutionManager.start() raised for %s — "
                        "degrading to OBSERVE", state.symbol,
                    )
                    state.target = None
                    if state.bridge is not None:
                        state.bridge.mode = ExecutionMode.OBSERVE
                        state.bridge._target = None  # noqa: SLF001
                    continue
            if state.bridge is not None and state.bridge.armed:
                state.bridge.set_armed(True)

    def _stop_execution_targets(self) -> None:
        for state in self._states.values():
            target = state.target
            if target is not None and hasattr(target, "stop"):
                try:
                    target.stop()
                except Exception:
                    logger.exception("execution target stop failed for %s", state.symbol)

    def _start_layered_push(self) -> None:
        """Spawn the Tide/Wave/RV push thread per symbol (Phase 14B)."""
        if not self._enable_layered_strategy:
            return
        from execution.live_runner import _run_layered_push_loop
        for state in self._states.values():
            if state.tide_engine is None or state.wave_engine is None:
                continue
            state.push_stop_event = threading.Event()
            state.push_thread = threading.Thread(
                target=_run_layered_push_loop,
                kwargs=dict(
                    stop_event=state.push_stop_event,
                    engine=state.engine,
                    tide_engine=state.tide_engine,
                    wave_engine=state.wave_engine,
                    rv_price_buf=state.rv_price_buf,
                    ofe_module=ofe,
                    last_trade_ts_holder=state.last_trade_ts_holder,
                ),
                daemon=True,
                name=f"strategy-layered-{state.symbol}",
            )
            state.push_thread.start()

    def _make_signal_callback(self, symbol: str):
        """Closure that logs each generated signal.

        Observation-only: signals are NOT routed to execution (the live
        path is Ripple-driven via ``set_ripple_callback``). Kept for
        operator visibility / log scraping.
        """

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

        # Phase 14B/21 — feed the layered-strategy buffers + paper book.
        if self._enable_layered_strategy and state.wave_engine is not None:
            try:
                state.wave_engine.on_price(price, ts_ms)
                state.rv_price_buf.append(price)
                state.last_trade_ts_holder[0] = ts_ms
            except Exception as exc:
                logger.warning("wave on_price failed (%s): %s", symbol, exc)

        # PaperEngine needs trade ticks to fill LIMIT orders / mark price.
        target = state.target
        if target is not None and hasattr(target, "on_trade"):
            try:
                target.on_trade(price, qty, ts_ms)
            except Exception:
                logger.exception("paper on_trade failed (%s)", symbol)

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
            "execution_mode": self._mode.value,
            "arm_execution": self._arm_execution,
        }

    # ----------------------------------------------------- execution API

    def get_execution_status(self) -> dict:
        """Per-symbol execution diagnostics for ``GET /api/execution``."""
        symbols = {}
        for sym, state in self._states.items():
            if state.bridge is None:
                continue
            symbols[sym] = state.bridge.diagnostics(state.engine)
        return {
            "mode": self._mode.value,
            "arm_execution": self._arm_execution,
            "broker": self._execution_broker,
            "max_position_usd": self._max_position_usd,
            "symbols": symbols,
        }

    def set_armed(self, armed: bool) -> dict:
        """Arm / disarm all symbols' live targets (``POST /api/execution/arm``).

        No-op in OBSERVE mode; callers should treat the returned
        ``execution_status`` as the source of truth.
        """
        for state in self._states.values():
            if state.bridge is not None:
                state.bridge.set_armed(armed)
        return self.get_execution_status()
