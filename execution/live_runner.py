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
    from execution.models import ripple_decision_to_intent  # noqa: PLC0415

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
            if intent is None:
                return
            try:
                exec_mgr.on_intent(intent)
            except Exception:
                logger.exception("exec_mgr.on_intent failed")

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
            if intent is None:
                return
            try:
                exec_mgr.on_intent(intent)
            except Exception:
                logger.exception("exec_mgr.on_intent failed")

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
