"""WebSocket ``/stream`` endpoint and client broadcaster.

A single :class:`StreamHub` owns the set of connected WebSocket clients
and exposes an async :meth:`StreamHub.broadcast` method that is handed
to :class:`strategy.engine.live_engine.LiveEngine` as its emit callback.

Backpressure handling
---------------------
Each client has its own asyncio queue (bounded).  When a client lags and
its queue fills up the oldest message is dropped.  The strategy service
must never block on a slow consumer — slow clients silently lose
intermediate frames; live data integrity for fast clients is preserved.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from strategy.engine import stream_emitter as se


logger = logging.getLogger(__name__)


_CLIENT_QUEUE_SIZE = 256


class _Client:
    __slots__ = ("ws", "queue", "task")

    def __init__(self, ws: WebSocket) -> None:
        self.ws: WebSocket = ws
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_CLIENT_QUEUE_SIZE)
        self.task: Optional[asyncio.Task] = None


class StreamHub:
    """Fan-out of msgpack messages to all subscribed WebSocket clients."""

    def __init__(self) -> None:
        self._clients: set[_Client] = set()
        self._lock = asyncio.Lock()
        self._engine_status_fn = None  # populated by main.py

    def set_engine_status_fn(self, fn) -> None:
        """Inject a callable that returns the engine status dict.

        Used by the periodic HEALTH emitter to report connected symbols.
        """
        self._engine_status_fn = fn

    # ------------------------------------------------------- client mgmt

    async def register(self, ws: WebSocket) -> _Client:
        await ws.accept()
        client = _Client(ws)
        client.task = asyncio.create_task(self._writer(client))
        async with self._lock:
            self._clients.add(client)
        logger.info("WS client connected (total=%d)", len(self._clients))
        return client

    async def unregister(self, client: _Client) -> None:
        async with self._lock:
            self._clients.discard(client)
        if client.task is not None:
            client.task.cancel()
        logger.info("WS client disconnected (total=%d)", len(self._clients))

    # ------------------------------------------------------------- send

    async def broadcast(self, blob: bytes) -> None:
        """Hand one msgpack-encoded message to every connected client."""
        if not self._clients:
            return
        for client in list(self._clients):
            try:
                client.queue.put_nowait(blob)
            except asyncio.QueueFull:
                # Drop the oldest message and re-queue.  This keeps slow
                # clients from blocking the whole pipeline.
                try:
                    client.queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    client.queue.put_nowait(blob)
                except asyncio.QueueFull:
                    pass

    async def _writer(self, client: _Client) -> None:
        try:
            while True:
                blob = await client.queue.get()
                await client.ws.send_bytes(blob)
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("WS writer error")

    # ---------------------------------------------------------- health

    async def health_loop(self, period_s: float = 2.0) -> None:
        """Periodically emit HEALTH messages so the UI can show status."""
        while True:
            await asyncio.sleep(period_s)
            try:
                status = (
                    self._engine_status_fn() if self._engine_status_fn else {}
                )
                msg = se.health_msg(
                    ts_ms=int(time.time() * 1000),
                    lag_ms=0.0,
                    symbols=status.get("symbols", []) or [],
                    engine_state="running"
                    if status.get("engine_running", False)
                    else "stopped",
                )
                await self.broadcast(se.encode(msg))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Health loop error")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def make_router(hub: StreamHub) -> APIRouter:
    router = APIRouter()

    @router.websocket("/stream")
    async def stream_endpoint(ws: WebSocket) -> None:
        client = await hub.register(ws)
        try:
            # The connection is one-way (server → client); we still need
            # to consume incoming messages so the WS stays alive and so
            # we notice disconnects promptly.
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("WS receive loop error")
        finally:
            await hub.unregister(client)

    return router
