"""Live-execute orchestrator (Phase 10C).

Owns the live-trading loop that ``main.py:execute`` previously inlined.
Wires the C++ ``OrderFlowEngine`` to the Phase 10 hardened Python WS
adapter (`run_binance_usdm_futures_ws_feed`) and routes engine signals
into the shared :class:`ExecutionManager` for live broker submission.

Designed for testability — every external dependency (the
``orderflow_engine`` module, the ``websockets`` package, the REST
session, the depth-snapshot fetcher) is injectable so tests can drive
the function with stubs and an in-process fake WebSocket without
spawning real threads/network.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


def _default_status_printer(
    *,
    exec_mgr: Any,
    trade_health: Any,
    depth_health: Any,
) -> None:
    """Default per-tick log line for the main-thread status loop."""
    side = exec_mgr.current_side
    pos_str = (
        f"{side.value} {exec_mgr.current_qty:.6f}" if side else "Flat"
    )
    logger.info(
        "Position: %s | Orders: %d | trade=%s depth=%s",
        pos_str,
        len(exec_mgr.orders),
        getattr(trade_health, "short_status", "?"),
        getattr(depth_health, "short_status", "?"),
    )


def _layered_push_step(
    *,
    state: dict,
    engine: Any,
    tide_engine: Any,
    wave_engine: Any,
    rv_price_buf: deque,
    ofe_module: Any,
    last_trade_ts_holder: list,
    rv_every: int = 1,
    wave_every: int = 5,
    tide_every: int = 60,
) -> None:
    """Phase 14B — one push iteration. Extracted as a free function so
    tests can drive cadence exhaustively without spawning a real thread.

    ``state`` carries the per-loop counters and per-setter success
    counts. Mutates in place. ``last_trade_ts_holder`` is a 1-element
    list updated by the WS callback so this function can read the
    latest event-time without taking a lock.
    """
    from execution.models import (  # noqa: PLC0415
        compute_realized_vol_from_prices, wave_snapshot_to_ofe,
    )

    state["rv_counter"] = state.get("rv_counter", 0) + 1
    state["wave_counter"] = state.get("wave_counter", 0) + 1
    state["tide_counter"] = state.get("tide_counter", 0) + 1

    try:
        ripple = engine.get_ripple()
    except Exception as exc:
        logger.warning("get_ripple failed (layered push skipped): %s", exc)
        return

    last_ts = int(last_trade_ts_holder[0]) if last_trade_ts_holder else 0

    if state["rv_counter"] >= rv_every:
        state["rv_counter"] = 0
        try:
            rv = compute_realized_vol_from_prices(list(rv_price_buf))
            tide_engine.set_realized_vol(rv)
            ripple.set_realized_vol(rv)
            state["pushes_rv"] = state.get("pushes_rv", 0) + 1
        except Exception as exc:
            logger.warning("set_realized_vol failed: %s", exc)

    if state["wave_counter"] >= wave_every:
        state["wave_counter"] = 0
        try:
            tide_snap = tide_engine.get_snapshot()
            wave_snap = wave_engine.get_snapshot(bias=tide_snap.bias)
            if last_ts > 0:
                wave_engine.update(last_ts, bias=tide_snap.bias)
            ofe_ws = wave_snapshot_to_ofe(wave_snap, ofe_module)
            ripple.set_wave_snapshot(ofe_ws)
            state["pushes_wave"] = state.get("pushes_wave", 0) + 1
        except Exception as exc:
            logger.warning("set_wave_snapshot failed: %s", exc)

    if state["tide_counter"] >= tide_every:
        state["tide_counter"] = 0
        try:
            if last_ts > 0:
                tide_engine.update(last_ts)
            snap = tide_engine.get_snapshot()
            ripple.set_risk_budget(
                snap.es_budget,
                snap.max_position_usd,
                snap.risk_multiplier,
            )
            state["pushes_tide"] = state.get("pushes_tide", 0) + 1
        except Exception as exc:
            logger.warning("set_risk_budget failed: %s", exc)


def _run_layered_push_loop(
    *,
    stop_event: threading.Event,
    engine: Any,
    tide_engine: Any,
    wave_engine: Any,
    rv_price_buf: deque,
    ofe_module: Any,
    last_trade_ts_holder: list,
    push_interval_s: float = 1.0,
    state: Optional[dict] = None,
) -> None:
    """Phase 14B push-thread target. Calls :func:`_layered_push_step`
    once per ``push_interval_s`` and exits when ``stop_event`` is set.
    Wakes promptly on shutdown via ``Event.wait(timeout=...)``."""
    state = state if state is not None else {}
    while not stop_event.is_set():
        try:
            _layered_push_step(
                state=state,
                engine=engine,
                tide_engine=tide_engine,
                wave_engine=wave_engine,
                rv_price_buf=rv_price_buf,
                ofe_module=ofe_module,
                last_trade_ts_holder=last_trade_ts_holder,
            )
        except Exception:
            logger.exception("layered push step raised")
        if stop_event.wait(timeout=push_interval_s):
            break


def run_live_execute(
    *,
    symbol: str,
    exec_mgr: Any,
    ofe_module: Any,
    websockets_module: Any,
    fetch_depth_snapshot: Optional[Callable[..., Any]] = None,
    rest_depth_url: Optional[str] = None,
    status_interval_s: float = 30.0,
    join_timeout_s: float = 5.0,
    disarm_grace_s: float = 2.0,
    status_printer: Optional[Callable[..., None]] = None,
    stop_event: Optional[threading.Event] = None,
    interrupt_event: Optional[threading.Event] = None,
    tick_size: float = 0.01,
    apply_rest_snapshot: bool = True,
    engine_factory: Optional[Callable[[Any], Any]] = None,
    recorder: Any = None,
    tide_engine: Any = None,
    wave_engine: Any = None,
    enable_layered_strategy: bool = True,
) -> int:
    """Run the live-execute loop. Returns the process exit code.

    Parameters
    ----------
    symbol :
        Symbol to subscribe to (e.g. ``"BTCUSDT"``).
    exec_mgr :
        Already-started :class:`ExecutionManager`. The caller is
        responsible for ``start()`` (so connect/balance errors can be
        handled in the prompt loop) and ``arm()``.
    ofe_module :
        The imported ``orderflow_engine`` C++ module.
    websockets_module :
        The ``websockets`` package (or a fake for testing).
    fetch_depth_snapshot :
        Optional override for the REST snapshot helper. Defaults to
        :func:`data_feed.fetch_and_build_depth_snapshot` when ``None``.
    rest_depth_url :
        Optional override for the depth REST endpoint. Defaults to the
        Binance USD-M futures URL.
    status_interval_s :
        How often to call ``status_printer`` from the main thread.
    join_timeout_s :
        Seconds to wait for the WS thread to exit on shutdown.
    disarm_grace_s :
        Seconds to wait between ``exec_mgr.disarm(close_position=True)``
        and ``exec_mgr.stop()`` so the close-position order has a
        chance to round-trip the broker.
    status_printer :
        Optional override for the per-tick log line. Defaults to
        :func:`_default_status_printer`.
    stop_event, interrupt_event :
        Test injection points. ``stop_event`` is forwarded to the WS
        runner; ``interrupt_event`` lets a test signal the main loop
        to drop out without raising ``KeyboardInterrupt``.
    tick_size :
        Forwarded to ``EngineConfig.tick_size`` when no
        ``engine_factory`` is supplied. Default mirrors the legacy
        ``main.py:execute`` value.
    apply_rest_snapshot :
        When ``True`` (default) fetch and apply a depth snapshot
        before the WS thread starts.
    engine_factory :
        Optional ``Callable[[ofe_module], engine]``. Defaults to a
        minimal ``EngineConfig`` with ``tick_size``.
    recorder :
        Optional ``SessionRecorder`` (Phase 13). When provided, it is
        attached to the engine to capture signals + ripple decisions
        for later deterministic replay. The runner writes the header
        if it has not already been written; the caller owns
        ``recorder.close()`` on shutdown.
    tide_engine, wave_engine :
        Phase 14B — optional ``TideEngine`` / ``WaveEngine`` instances
        for the layered-strategy push thread. When ``None`` and
        ``enable_layered_strategy`` is ``True``, the runner instantiates
        defaults. Pass explicit instances from tests / when downstream
        callers need to inspect state.
    enable_layered_strategy :
        Phase 14B — when ``True`` (default), the runner spawns a
        background thread that pushes Tide budgets, Wave snapshots,
        and realized vol into the C++ engine at the cadences defined
        by `strategy.md` §5.3 + AGENT_STRATEGY_RULES.md §7.5
        (Tide 60 s, Wave 5 s, RV 1 s). Set to ``False`` to restore
        pre-14B behaviour exactly — the C++ engine then runs against
        ``DefaultTideSnapshot`` / ``DefaultWaveSnapshot`` for the whole
        session (V1-incomplete; only kept for regression compatibility).

    Returns
    -------
    int
        ``0`` on clean shutdown.
    """
    if status_printer is None:
        status_printer = _default_status_printer
    if stop_event is None:
        stop_event = threading.Event()
    if interrupt_event is None:
        interrupt_event = threading.Event()
    if engine_factory is None:
        def engine_factory(ofe_mod: Any) -> Any:
            cfg = ofe_mod.EngineConfig()
            cfg.tick_size = tick_size
            return ofe_mod.OrderFlowEngine(cfg)

    engine = engine_factory(ofe_module)

    # Phase 14A — live execution topology contract
    # (AGENT_STRATEGY_RULES.md §7.4): the execution manager subscribes
    # to Ripple decisions, NOT raw SignalEngine signals. The Ripple
    # decision has already passed through the lifecycle FSM, the Wave
    # permissions matrix, and the RiskEngine ES throttle on the C++
    # side. Routing the live broker through raw signals would bypass
    # all three.
    #
    # The signal callback is preserved as an OBSERVATION channel only:
    # when a recorder is attached, signals are written to the sidecar
    # for downstream analysis. They never drive ``ExecutionManager``.
    from execution.models import (  # noqa: PLC0415
        intent_risk_block_reason, ripple_decision_to_intent,
    )

    def _gate_and_dispatch(intent: Any) -> None:
        """Phase 14C — V1 §22.2 #12 gate.

        Mirrors the C++ ``RippleEngine`` Wave/Risk gate on the Python
        side so the broker never sees an order when engine state
        already decided to block the trade. Computes a coarse
        ``current_position_usd`` from the live exec_mgr state.
        """
        if intent is None:
            return
        try:
            qty = float(getattr(exec_mgr, "current_qty", 0.0) or 0.0)
            ref = float(getattr(intent, "reference_price", 0.0) or 0.0)
            pos_usd = abs(qty) * ref
        except Exception:
            pos_usd = 0.0
        try:
            block = intent_risk_block_reason(
                intent, engine,
                current_position_usd=pos_usd,
                ofe_module=ofe_module,
            )
        except Exception:
            logger.exception("intent_risk_block_reason raised")
            block = None
        if block is not None:
            logger.warning(
                "V1 §22.2 #12 gate blocked intent: action=%s reason=%s",
                getattr(intent, "action", ""), block,
            )
            return
        try:
            exec_mgr.on_intent(intent)
        except Exception:
            logger.exception("exec_mgr.on_intent failed")

    if recorder is not None:
        if not getattr(recorder, "_header_written", False):
            try:
                recorder.write_header(
                    symbol=symbol,
                    engine_config=engine.get_config(),
                )
            except Exception:
                logger.exception("recorder.write_header failed")

        def _signal_cb(sig: Any) -> None:
            try:
                recorder.record_signal(sig)
            except Exception:
                logger.exception("recorder.record_signal failed")

        engine.set_signal_callback(_signal_cb)

        def _ripple_cb(decision: Any) -> None:
            try:
                recorder.record_ripple_decision(decision)
            except Exception:
                logger.exception("recorder.record_ripple_decision failed")
            try:
                intent = ripple_decision_to_intent(decision)
            except Exception:
                logger.exception("ripple_decision_to_intent failed")
                intent = None
            _gate_and_dispatch(intent)

        try:
            engine.set_ripple_callback(_ripple_cb)
        except Exception:
            logger.exception("set_ripple_callback failed")
    else:
        def _ripple_cb(decision: Any) -> None:
            try:
                intent = ripple_decision_to_intent(decision)
            except Exception:
                logger.exception("ripple_decision_to_intent failed")
                return
            _gate_and_dispatch(intent)

        engine.set_ripple_callback(_ripple_cb)

    if apply_rest_snapshot:
        try:
            if fetch_depth_snapshot is None:
                from data_feed import fetch_and_build_depth_snapshot  # noqa: PLC0415
                fetch_depth_snapshot = fetch_and_build_depth_snapshot
            url = rest_depth_url
            if url is None:
                from data_feed import BINANCE_FUTURES_USDM_DEPTH_URL  # noqa: PLC0415
                url = BINANCE_FUTURES_USDM_DEPTH_URL
            snap = fetch_depth_snapshot(ofe_module, url, symbol)
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

    # Phase 14B — layered-strategy setup. The push thread reads from
    # ``rv_price_buf`` (fed by the WS trade callback below) and
    # ``last_trade_ts_holder`` (1-element list for lock-free event-time
    # propagation from the WS thread to the push thread).
    rv_price_buf: deque = deque(maxlen=60)
    last_trade_ts_holder = [0]
    push_thread: Optional[threading.Thread] = None
    push_stop_event = threading.Event()
    push_state: dict = {}
    _tide_engine_inst: Any = None
    _wave_engine_inst: Any = None
    if enable_layered_strategy:
        try:
            from tide.tide_engine import TideEngine  # noqa: PLC0415
            from wave.wave_engine import WaveEngine  # noqa: PLC0415
            _tide_engine_inst = tide_engine if tide_engine is not None else TideEngine()
            _wave_engine_inst = wave_engine if wave_engine is not None else WaveEngine()
        except Exception:
            logger.exception(
                "layered-strategy engine init failed — "
                "running without Tide/Wave push (engine will use Defaults)")
            _tide_engine_inst = None
            _wave_engine_inst = None

    def on_ws_trade(t: Any, _is_buy_aggressor: bool) -> None:
        try:
            engine.process_trade(t)
        except BaseException as exc:
            logger.warning("process_trade failed: %s", exc)
        # Phase 14B — feed the layered-strategy buffers. Wrapped so a
        # WaveEngine binding error cannot kill the live feed thread.
        if enable_layered_strategy and _wave_engine_inst is not None:
            try:
                price = float(getattr(t, "price", 0.0) or 0.0)
                ts = int(getattr(t, "timestamp", 0) or 0)
                if price > 0 and ts > 0:
                    _wave_engine_inst.on_price(price, ts)
                    rv_price_buf.append(price)
                    last_trade_ts_holder[0] = ts
            except Exception as exc:
                logger.warning("wave on_price failed: %s", exc)

    def on_ws_depth(u: Any) -> None:
        try:
            engine.process_depth(u)
        except BaseException as exc:
            logger.warning("process_depth failed: %s", exc)

    def _ws_target() -> None:
        run_binance_usdm_futures_ws_feed(
            symbol_lower=symbol.lower(),
            stop_event=stop_event,
            ofe=ofe_module,
            trade_health=trade_health,
            depth_health=depth_health,
            recent_trade_ids=recent_trade_ids,
            on_ws_trade=on_ws_trade,
            on_ws_depth=on_ws_depth,
            websockets_module=websockets_module,
        )

    ws_thread = threading.Thread(
        target=_ws_target,
        daemon=True,
        name=f"live_execute-ws-{symbol}",
    )
    engine.start(symbol)
    ws_thread.start()

    if (enable_layered_strategy
            and _tide_engine_inst is not None
            and _wave_engine_inst is not None):
        push_thread = threading.Thread(
            target=_run_layered_push_loop,
            kwargs=dict(
                stop_event=push_stop_event,
                engine=engine,
                tide_engine=_tide_engine_inst,
                wave_engine=_wave_engine_inst,
                rv_price_buf=rv_price_buf,
                ofe_module=ofe_module,
                last_trade_ts_holder=last_trade_ts_holder,
                state=push_state,
            ),
            daemon=True,
            name=f"live_execute-layered-{symbol}",
        )
        push_thread.start()

    interrupted = False
    try:
        while not interrupt_event.is_set():
            # Sleep in small increments so an interrupt_event flip is
            # picked up promptly. Real KeyboardInterrupt still works
            # because it's raised on the main thread by Python.
            slept = 0.0
            while slept < status_interval_s and not interrupt_event.is_set():
                time.sleep(min(0.5, status_interval_s - slept))
                slept += 0.5
            if interrupt_event.is_set():
                break
            try:
                status_printer(
                    exec_mgr=exec_mgr,
                    trade_health=trade_health,
                    depth_health=depth_health,
                )
            except BaseException as exc:
                logger.warning("status_printer raised: %s", exc)
    except KeyboardInterrupt:
        interrupted = True
        print("\nShutting down...")

    stop_event.set()
    push_stop_event.set()
    if push_thread is not None and push_thread.is_alive():
        push_thread.join(timeout=join_timeout_s)
    if ws_thread.is_alive():
        ws_thread.join(timeout=join_timeout_s)
    try:
        exec_mgr.disarm(close_position=True)
    except BaseException as exc:
        logger.warning("disarm(close_position=True) raised: %s", exc)
    if disarm_grace_s > 0:
        time.sleep(disarm_grace_s)
    try:
        exec_mgr.stop()
    except BaseException as exc:
        logger.warning("exec_mgr.stop raised: %s", exc)
    try:
        engine.stop()
    except BaseException as exc:
        logger.debug("engine.stop raised: %s", exc)

    if interrupted:
        print("Execution stopped.")
    return 0
