"""Binance USD-M futures WebSocket trade + depth@100ms feeds (asyncio).

Runs inside a dedicated thread with its own event loop. Parses messages and
invokes hooks that apply data to the C++ engine and UI buffers (owned by the
caller — typically MainWindow).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

from data_feed.stream_health import (
    FEED_RECONNECT_INITIAL_BACKOFF_S,
    FEED_RECONNECT_MAX_BACKOFF_S,
    FEED_STALE_MS,
    FeedState,
    FeedStreamHealth,
)

logger = logging.getLogger(__name__)

BINANCE_USDM_FUTURES_WS_BASE = "wss://fstream.binance.com/ws/"
WS_RECV_TIMEOUT_S = 0.5


def _reconnect_backoff(failures: int) -> float:
    raw = FEED_RECONNECT_INITIAL_BACKOFF_S * (2 ** min(failures, 8))
    return min(raw, FEED_RECONNECT_MAX_BACKOFF_S)


async def _wait_or_stop(
    stop_ev: threading.Event,
    async_ev: asyncio.Event,
    timeout_s: float,
) -> None:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if stop_ev.is_set() or async_ev.is_set():
            return
        await asyncio.sleep(0.25)


def _depth_json_to_update(j: dict, ofe: Any) -> Any:
    u = ofe.DepthUpdate()
    u.timestamp = j.get("E", 0)
    u.first_update_id = j.get("U", 0)
    u.final_update_id = j.get("u", 0)
    u.is_snapshot = False
    bids = []
    for b in j.get("b", []):
        lv = ofe.DepthLevel()
        lv.price = float(b[0])
        lv.quantity = float(b[1])
        bids.append(lv)
    asks = []
    for a in j.get("a", []):
        lv = ofe.DepthLevel()
        lv.price = float(a[0])
        lv.quantity = float(a[1])
        asks.append(lv)
    u.bids = bids
    u.asks = asks
    return u


def run_binance_usdm_futures_ws_feed(
    *,
    symbol_lower: str,
    stop_event: threading.Event,
    ofe: Any,
    trade_health: FeedStreamHealth,
    depth_health: FeedStreamHealth,
    recent_trade_ids: deque,
    on_ws_trade: Callable[[Any, bool], None],
    on_ws_depth: Callable[[Any], None],
    websockets_module: Any,
) -> None:
    """Blocking entrypoint for ``threading.Thread(target=...)``.

    *on_ws_trade* receives ``(ofe.Trade, is_buy_aggressor)`` after dedupe.
    *on_ws_depth* receives ``ofe.DepthUpdate`` (partial book delta).
    """
    if websockets_module is None:
        logger.error("websockets not installed — cannot run WS feed")
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    base = BINANCE_USDM_FUTURES_WS_BASE
    trade_reconnect_event = asyncio.Event()
    depth_reconnect_event = asyncio.Event()

    async def _run_trade_stream() -> None:
        uri = f"{base}{symbol_lower}@trade"
        h = trade_health
        ws_mod = websockets_module

        while not stop_event.is_set():
            ws = None
            try:
                h.state = FeedState.CONNECTING
                ws = await asyncio.wait_for(
                    ws_mod.connect(uri), timeout=15)
                logger.info("Trade WS connected: %s", uri)

                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        continue
                    except ws_mod.ConnectionClosed:
                        break

                    try:
                        j = json.loads(raw)
                        ts = j["T"]
                        price = float(j["p"])
                        qty = float(j["q"])
                        is_buyer_maker = j["m"]
                        trade_id = j["t"]

                        if trade_id in recent_trade_ids:
                            continue
                        recent_trade_ids.append(trade_id)

                        t = ofe.Trade()
                        t.timestamp = ts
                        t.price = price
                        t.quantity = qty
                        t.is_buyer_maker = is_buyer_maker
                        is_buy_aggressor = not is_buyer_maker
                        on_ws_trade(t, is_buy_aggressor)
                        h.on_message(ts)
                    except (KeyError, ValueError, TypeError) as parse_err:
                        logger.warning("Trade parse error: %s", parse_err)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                err_str = f"{type(exc).__name__}: {exc}"
                if not stop_event.is_set():
                    logger.warning("Trade WS error: %s", err_str)
                h.on_disconnect(err_str)
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except BaseException:
                        pass

            if stop_event.is_set():
                return

            h.on_reconnect_start()
            wait = _reconnect_backoff(h.consecutive_failures)
            logger.info(
                "Trade WS reconnect in %.1fs (attempt #%d)",
                wait,
                h.reconnect_count,
            )
            try:
                await asyncio.wait_for(
                    _wait_or_stop(stop_event, trade_reconnect_event, wait),
                    timeout=wait + 1,
                )
            except asyncio.TimeoutError:
                pass
            trade_reconnect_event.clear()

            if h.state == FeedState.FAILED:
                logger.error(
                    "Trade feed FAILED after %d consecutive failures — giving up",
                    h.consecutive_failures,
                )
                return
            if h.state != FeedState.LIVE:
                h.on_reconnect_fail(h.last_error or "retry")

    async def _run_depth_stream() -> None:
        uri = f"{base}{symbol_lower}@depth@100ms"
        h = depth_health
        ws_mod = websockets_module

        while not stop_event.is_set():
            ws = None
            try:
                h.state = FeedState.CONNECTING
                ws = await asyncio.wait_for(
                    ws_mod.connect(uri), timeout=15)
                logger.info("Depth WS connected: %s", uri)

                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        continue
                    except ws_mod.ConnectionClosed:
                        break

                    try:
                        j = json.loads(raw)
                        u = _depth_json_to_update(j, ofe)
                        on_ws_depth(u)
                        h.on_message(u.timestamp)
                    except (KeyError, ValueError, TypeError) as parse_err:
                        logger.warning("Depth parse error: %s", parse_err)

            except asyncio.CancelledError:
                return
            except Exception as exc:
                err_str = f"{type(exc).__name__}: {exc}"
                if not stop_event.is_set():
                    logger.warning("Depth WS error: %s", err_str)
                h.on_disconnect(err_str)
            finally:
                if ws is not None:
                    try:
                        await ws.close()
                    except BaseException:
                        pass

            if stop_event.is_set():
                return

            h.on_reconnect_start()
            wait = _reconnect_backoff(h.consecutive_failures)
            logger.info(
                "Depth WS reconnect in %.1fs (attempt #%d)",
                wait,
                h.reconnect_count,
            )
            try:
                await asyncio.wait_for(
                    _wait_or_stop(stop_event, depth_reconnect_event, wait),
                    timeout=wait + 1,
                )
            except asyncio.TimeoutError:
                pass
            depth_reconnect_event.clear()

            if h.state == FeedState.FAILED:
                logger.error(
                    "Depth feed FAILED after %d consecutive failures — giving up",
                    h.consecutive_failures,
                )
                return
            if h.state != FeedState.LIVE:
                h.on_reconnect_fail(h.last_error or "retry")

    async def _watchdog() -> None:
        stale_threshold_s = FEED_STALE_MS / 1000.0
        while not stop_event.is_set():
            await asyncio.sleep(1.0)

            th = trade_health
            dh = depth_health

            if (th.state == FeedState.LIVE
                    and th.seconds_since_last_msg > stale_threshold_s):
                th.on_stale()
                logger.warning(
                    "Trade feed STALE — no message for %.1fs "
                    "(depth %s, %.1fs ago)",
                    th.seconds_since_last_msg,
                    dh.state.value,
                    dh.seconds_since_last_msg,
                )

            if th.state == FeedState.STALE:
                trade_reconnect_event.set()

    async def _cancel_remaining_tasks() -> None:
        tasks = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _main() -> None:
        tasks = [
            asyncio.create_task(_run_trade_stream()),
            asyncio.create_task(_run_depth_stream()),
            asyncio.create_task(_watchdog()),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        await _cancel_remaining_tasks()

    try:
        loop.run_until_complete(_main())
    except Exception as e:
        if not stop_event.is_set():
            logger.error("WS feed loop error: %s", e)
    finally:
        try:
            loop.run_until_complete(_cancel_remaining_tasks())
        except Exception:
            pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        trade_health.on_disconnect("loop exited")
        depth_health.on_disconnect("loop exited")
