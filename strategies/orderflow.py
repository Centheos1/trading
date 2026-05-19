import sys
import os
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Tuple, Dict, List, Any, Callable, Optional

logger = logging.getLogger(__name__)

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
except ImportError:
    logger.warning(
        "orderflow_engine C++ module not found. "
        "Build it first: cd backtestingCpp/orderflow && mkdir build && cd build "
        "&& cmake .. && make"
    )
    ofe = None


_RIPPLE_MAP: Dict[str, str] = {
    "wall_min_relative_size": "wall_min_relative_size",
    "absorption_entry": "absorption_entry",
    "exhaustion_entry": "exhaustion_entry",
    "breakout_entry": "breakout_entry",
    "idle_exit_threshold": "idle_exit_threshold",
    "bounce_max_break_risk": "bounce_max_break_risk",
    "feature_window_ms": "feature_window_ms",
    # Phase 7V: surface HMM toggles via the params dict so the A/B
    # validation harness (and any other caller) can flip backends
    # without monkey-patching the engine after construction.
    "hmm_enabled": "hmm_enabled",
    "hmm_model_path": "hmm_model_path",
}

_LIFECYCLE_KEYS: Tuple[str, ...] = (
    "confirmation_window_ms",
    "max_hold_time_ms",
    "trailing_stop_sigma",
    "target_distance_sigma",
)


def _build_config(params: Dict) -> "ofe.EngineConfig":
    config = ofe.EngineConfig()
    config.tick_size = params.get("tick_size", 0.01)

    sp = ofe.SignalParams()
    sp.imbalance_threshold = params.get("imbalance_threshold", 3.0)
    sp.stacked_imbalance_levels = params.get("stacked_imbalance_levels", 3)
    sp.absorption_volume_ratio = params.get("absorption_volume_ratio", 5.0)
    sp.cvd_divergence_lookback = params.get("cvd_divergence_lookback", 100)
    sp.exhaustion_lookback_bars = params.get("exhaustion_lookback_bars", 5)
    sp.signal_strength_min = params.get("signal_strength_min", 0.3)
    config.signal_params = sp

    rcfg = config.ripple
    rcfg.tick_size = config.tick_size
    rcfg.paper_fills = params.get("paper_fills", False)
    # Phase 13Y: keys in ``_RIPPLE_MAP`` must exist as attributes on the C++
    # ``RippleConfig`` (set by pybind11 ``def_readwrite``). If a key is
    # declared in ``STRAT_PARAMS["orderflow"]`` but the C++ binding is
    # missing or stale, ``setattr`` would raise ``AttributeError`` and
    # crash the whole backtest. Skip + warn instead so the run still
    # completes and the missing binding is surfaced clearly.
    for param_key, attr_name in _RIPPLE_MAP.items():
        if param_key not in params:
            continue
        if not hasattr(rcfg, attr_name):
            logger.warning(
                "RippleConfig has no attribute %r (param %r); "
                "skipping. Likely a missing pybind11 binding in "
                "backtestingCpp/orderflow/bindings.cpp.",
                attr_name, param_key,
            )
            continue
        setattr(rcfg, attr_name, params[param_key])

    lc = rcfg.lifecycle
    for k in _LIFECYCLE_KEYS:
        if k not in params:
            continue
        if not hasattr(lc, k):
            logger.warning(
                "LifecycleConfig has no attribute %r; skipping.", k)
            continue
        setattr(lc, k, params[k])
    rcfg.lifecycle = lc

    config.ripple = rcfg
    return config


def backtest(exchange: str, symbol: str, from_time: int, to_time: int,
             params: Dict) -> Tuple[float, float, int, float, float]:
    if ofe is None:
        raise RuntimeError(
            "orderflow_engine C++ module not built. "
            "Run: cd backtestingCpp/orderflow && mkdir -p build && cd build "
            "&& cmake .. && make"
        )

    config = _build_config(params)

    # Enable paper fills for Ripple-based metrics
    rcfg = config.ripple
    rcfg.paper_fills = True
    config.ripple = rcfg

    engine = ofe.OrderFlowEngine(config)

    tick_store_path = os.path.join("data", f"{exchange}_ticks.h5")
    if not os.path.exists(tick_store_path):
        raise FileNotFoundError(
            f"Tick data file not found: {tick_store_path}. "
            "Collect tick data first using the data collection mode."
        )

    store = ofe.TickStore(tick_store_path)
    replay = ofe.ReplayFeed(store, 0.0)
    replay.set_time_range(from_time, to_time)

    ofe.connect_feed(engine, replay)
    # ``engine.start(symbol)`` wires the trade/depth callbacks onto the
    # feed, calls ``replay.subscribe_trades/_depth(symbol)``, and then
    # spawns a worker thread that runs ``replay_thread_func()``. The
    # legacy code below this point ALSO called ``replay.run_sync()``,
    # which executed the same loop synchronously on the main thread —
    # so every trade/depth event was processed twice (Phase 13X regression
    # probe confirmed an exact 2× volume profile / CVD count). Drop the
    # redundant ``run_sync()`` and wait for the background thread to
    # complete instead.
    engine.start(symbol)
    import time as _time  # noqa: PLC0415
    _deadline = _time.monotonic() + 600.0  # 10-minute safety stop
    while not replay.is_complete():
        if _time.monotonic() > _deadline:
            logger.warning(
                "backtest replay did not complete within 600 s; "
                "forcing engine.stop()")
            break
        _time.sleep(0.05)

    ripple = engine.get_ripple()
    ripple_trades = ripple.completed_trades()
    ripple_pnl = ripple.cumulative_pnl()
    ripple_dd = ripple.lifecycle_max_drawdown()

    sig_engine = engine.get_signal_engine()
    sig_pnl = sig_engine.get_pnl()
    sig_dd = sig_engine.get_max_drawdown()
    sig_trades = sig_engine.get_num_trades()

    # Use Ripple metrics when trades completed, else fall back to SignalEngine
    if ripple_trades > 0:
        pnl = ripple_pnl
        max_drawdown = ripple_dd
        num_trades = int(ripple_trades)
    else:
        pnl = sig_pnl
        max_drawdown = sig_dd
        num_trades = sig_trades

    returns = sig_engine.get_returns()
    sharpe_ratio = _compute_sharpe(returns)
    cagr = _compute_cagr(returns, from_time, to_time)

    engine.stop()
    store.close()

    return pnl, max_drawdown, num_trades, sharpe_ratio, cagr


def _compute_sharpe(returns: List[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    import numpy as np
    r = np.diff(returns)
    if np.std(r) == 0:
        return 0.0
    return float((np.mean(r) - risk_free) / np.std(r))


def _compute_cagr(returns: List[float], from_time: int, to_time: int,
                  initial: float = 10000.0) -> float:
    if not returns or from_time >= to_time:
        return 0.0

    final_value = initial * (1.0 + returns[-1] / 100.0)
    years = (to_time - from_time) / (365.25 * 24 * 3600 * 1000)
    if years <= 0 or final_value <= 0:
        return 0.0

    return float((final_value / initial) ** (1.0 / years) - 1.0) * 100.0


@dataclass
class LiveSession:
    """Handle for a running ``run_live`` session.

    Wraps the engine, the WS feed thread, and a ``stop_event`` so the
    caller can shut everything down deterministically. Returned by
    :func:`run_live`.
    """

    engine: Any
    stop_event: threading.Event
    thread: threading.Thread
    trade_health: Any
    depth_health: Any
    _stopped: bool = field(default=False, init=False)

    def stop(self, *, join_timeout_s: float = 5.0) -> None:
        """Signal the WS thread to stop, join it, and stop the engine.

        Idempotent — safe to call multiple times.
        """
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=join_timeout_s)
        try:
            self.engine.stop()
        except BaseException:
            pass

    def __enter__(self) -> "LiveSession":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


def _default_signal_logger(signal) -> None:
    logger.info(
        "SIGNAL: %s @ %.2f strength=%.2f - %s",
        signal.type_name(), signal.price, signal.strength, signal.description,
    )


def run_live(
    symbol: str,
    exchange: str = "binance",
    futures: bool = True,
    params: Dict = None,
    tick_store_path: str = None,
    *,
    websockets_module: Any = None,
    ofe_module: Any = None,
    rest_session: Any = None,
    apply_rest_snapshot: bool = True,
    signal_callback: Optional[Callable[[Any], None]] = _default_signal_logger,
    rest_depth_url: Optional[str] = None,
    recorder: Any = None,
) -> LiveSession:
    """Start a live Binance USD-M futures session driving the C++ engine.

    Replaces the legacy C++ ``BinanceWsFeed`` path with the Python WS
    adapter ``data_feed.run_binance_usdm_futures_ws_feed``. The engine
    itself is unchanged — trades and depth updates land via
    ``engine.process_trade`` / ``engine.process_depth`` from a worker
    thread, with `FeedStreamHealth` tracking reconnects and stale
    streams.

    Parameters
    ----------
    symbol : str
        Trading symbol (e.g. ``"BTCUSDT"``).
    exchange : str, optional
        Reserved for future multi-exchange support; only Binance is
        wired today.
    futures : bool, optional
        Reserved. The Python WS path is USD-M futures only; ``False``
        triggers a warning and the call still uses USD-M futures.
    params : dict, optional
        ``EngineConfig`` overrides forwarded to ``_build_config``.
    tick_store_path : str, optional
        If provided, opens a ``TickStore`` and registers it on the
        engine via ``engine.set_tick_store``. Pass ``None`` to skip.
    websockets_module, ofe_module, rest_session : optional
        Test-only injection points. ``ofe_module`` defaults to the
        module-level ``ofe``; ``websockets_module`` defaults to a
        runtime ``import websockets``; ``rest_session`` is forwarded
        to the depth-snapshot fetch.
    apply_rest_snapshot : bool, optional
        When ``True`` (default) fetches a 1000-level depth snapshot
        via REST and applies it before the WS thread starts. Disable
        for tests that don't have network access.
    signal_callback : callable, optional
        Forwarded to ``engine.set_signal_callback``. Defaults to a
        log-only callback. Pass ``None`` to leave the existing
        callback untouched.
    rest_depth_url : str, optional
        Override for the REST depth endpoint (defaults to the
        Binance USD-M futures URL from ``data_feed``).
    recorder : SessionRecorder, optional
        Phase 13 hook. When provided, the recorder is wired onto the
        engine to capture signals + ripple decisions for later
        deterministic replay. The caller is responsible for opening the
        recorder, calling ``write_header`` (the runner does this if the
        header has not yet been written), and closing it on shutdown.

    Returns
    -------
    LiveSession
        Handle exposing ``engine``, ``stop()``, and the per-stream
        ``FeedStreamHealth`` references.
    """
    ofe_mod = ofe_module if ofe_module is not None else ofe
    if ofe_mod is None:
        raise RuntimeError(
            "orderflow_engine C++ module not built. "
            "Run: cd backtestingCpp/orderflow && ./build.sh")

    if not futures:
        logger.warning(
            "run_live: 'futures=False' is no longer supported by the "
            "Python WS path; using USD-M futures stream regardless.")

    if exchange != "binance":
        logger.warning(
            "run_live: exchange=%r is unsupported; using Binance.",
            exchange)

    if websockets_module is None:
        try:
            import websockets as _ws_module  # noqa: PLC0415
        except ImportError:
            _ws_module = None
        websockets_module = _ws_module
    if websockets_module is None:
        raise RuntimeError(
            "Python `websockets` package is required by run_live. "
            "Install it with `pip install websockets`.")

    params = params or {}
    config = _build_config(params)
    engine = ofe_mod.OrderFlowEngine(config)

    if signal_callback is not None:
        engine.set_signal_callback(signal_callback)

    if tick_store_path:
        store = ofe_mod.TickStore(tick_store_path)
        engine.set_tick_store(store, symbol)

    if recorder is not None:
        if not getattr(recorder, "_header_written", False):
            try:
                recorder.write_header(symbol=symbol, engine_config=config)
            except Exception:
                logger.exception("recorder.write_header failed")
        try:
            recorder.attach_to_engine(engine)
        except Exception:
            logger.exception("recorder.attach_to_engine failed")

    if apply_rest_snapshot:
        try:
            from data_feed import (  # noqa: PLC0415
                BINANCE_FUTURES_USDM_DEPTH_URL,
                fetch_and_build_depth_snapshot,
            )
            url = rest_depth_url or BINANCE_FUTURES_USDM_DEPTH_URL
            snap = fetch_and_build_depth_snapshot(
                ofe_mod, url, symbol, session=rest_session)
            engine.process_depth(snap)
            logger.info(
                "Depth snapshot primed: %d bids, %d asks",
                len(snap.bids), len(snap.asks))
        except Exception as exc:
            logger.warning("REST depth snapshot failed: %s", exc)

    from data_feed import run_binance_usdm_futures_ws_feed  # noqa: PLC0415
    from data_feed.stream_health import FeedStreamHealth  # noqa: PLC0415

    trade_health = FeedStreamHealth()
    depth_health = FeedStreamHealth()
    recent_trade_ids: deque = deque(maxlen=4096)
    stop_event = threading.Event()

    def on_ws_trade(t: Any, _is_buy_aggressor: bool) -> None:
        try:
            engine.process_trade(t)
        except BaseException as exc:
            logger.warning("process_trade failed: %s", exc)

    def on_ws_depth(u: Any) -> None:
        try:
            engine.process_depth(u)
        except BaseException as exc:
            logger.warning("process_depth failed: %s", exc)

    def _ws_target() -> None:
        run_binance_usdm_futures_ws_feed(
            symbol_lower=symbol.lower(),
            stop_event=stop_event,
            ofe=ofe_mod,
            trade_health=trade_health,
            depth_health=depth_health,
            recent_trade_ids=recent_trade_ids,
            on_ws_trade=on_ws_trade,
            on_ws_depth=on_ws_depth,
            websockets_module=websockets_module,
        )

    thread = threading.Thread(target=_ws_target, daemon=True,
                              name=f"run_live-ws-{symbol}")
    engine.start(symbol)  # No data_feed_ wired; engine.start just flips running_
    thread.start()

    return LiveSession(
        engine=engine,
        stop_event=stop_event,
        thread=thread,
        trade_health=trade_health,
        depth_health=depth_health,
    )
