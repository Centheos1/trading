"""WebSocket server for the Bookmap publisher.

Hosts a single fan-out broadcast server on ``ws://localhost:8765``. Any
number of Bookmap clients may connect; each connected client receives
every JSON event enqueued via :meth:`WsBroadcastServer.publish`.

Design notes
------------

* Runs on its own asyncio event loop in a background thread. Public API
  is thread-safe via ``asyncio.run_coroutine_threadsafe``.
* Per-client send is wrapped in a per-client queue so a slow consumer
  cannot back-pressure the engine. Slow clients are dropped silently.
* No authentication / TLS — the server binds to localhost only and is
  intended for local IPC with a Bookmap instance on the same host.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 8765
_CLIENT_QUEUE_MAX = 256


class WsBroadcastServer:
    """Background-thread WebSocket fan-out server.

    Usage::

        srv = WsBroadcastServer()
        srv.start()
        srv.publish({"type": "HEALTH", "symbol": "BTCUSDT", ...})
        srv.stop()

    All public methods are safe to call from any thread.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._host = host
        self._port = port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None
        self._clients: Set[Any] = set()
        self._ready = threading.Event()
        self._stopped = threading.Event()

    # ----------------------------------------------------------- lifecycle

    def start(self, *, ready_timeout_s: float = 5.0) -> bool:
        """Start the server on a background thread.

        Returns ``True`` once the server is accepting connections, or
        ``False`` if startup timed out (e.g. ``websockets`` package
        missing or port already bound).
        """
        if self._thread and self._thread.is_alive():
            return True
        self._ready.clear()
        self._stopped.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"bookmap-ws-server-{self._port}",
        )
        self._thread.start()
        if not self._ready.wait(timeout=ready_timeout_s):
            logger.warning(
                "Bookmap WS server did not become ready within %.1fs",
                ready_timeout_s,
            )
            return False
        return True

    def stop(self, *, timeout_s: float = 5.0) -> None:
        """Stop the server and join its background thread."""
        if not self._loop:
            return
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stopped.set)
        if self._thread:
            self._thread.join(timeout=timeout_s)
            self._thread = None
        self._loop = None
        self._server = None
        self._clients.clear()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------ publish

    def publish(self, event: dict) -> None:
        """Thread-safe enqueue of a single event for broadcast.

        ``event`` should be a JSON-serialisable dict (see
        ``event_builder.py``). Encoding errors are logged at debug and
        swallowed so the caller (engine) is never disturbed.
        """
        if not self._loop or not self._loop.is_running():
            return
        try:
            payload = json.dumps(event, default=_json_default)
        except Exception:
            logger.debug("Bookmap publish: JSON encode failed", exc_info=True)
            return
        self._loop.call_soon_threadsafe(self._fanout, payload)

    # ----------------------------------------------------------- internals

    def _run(self) -> None:
        try:
            import websockets  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "Bookmap WS server: 'websockets' package not installed; "
                "publisher disabled",
            )
            return

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def _main() -> None:
            server = await websockets.serve(
                self._handle_client,
                self._host,
                self._port,
            )
            self._server = server
            logger.info(
                "Bookmap WS server listening on ws://%s:%d",
                self._host,
                self._port,
            )
            self._ready.set()
            try:
                while not self._stopped.is_set():
                    await asyncio.sleep(0.25)
            finally:
                server.close()
                await server.wait_closed()
                logger.info("Bookmap WS server stopped")

        try:
            loop.run_until_complete(_main())
        except Exception:
            logger.exception("Bookmap WS server crashed")
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            except Exception:
                pass
            loop.close()
            self._ready.set()

    async def _handle_client(self, ws: Any) -> None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=_CLIENT_QUEUE_MAX)
        ws._bm_queue = queue  # type: ignore[attr-defined]
        self._clients.add(ws)
        peer = getattr(ws, "remote_address", None)
        logger.info("Bookmap client connected from %s", peer)
        # Track why the send loop ended so we can diagnose client-side
        # disconnects from the server logs alone. Without this, all
        # disconnects (clean close, RST, write timeout) look identical.
        end_reason: str = "queue drained"
        end_exc: Optional[BaseException] = None
        try:
            while True:
                payload = await queue.get()
                if payload is None:
                    end_reason = "stop sentinel"
                    break
                await ws.send(payload)
        except BaseException as exc:  # noqa: BLE001 -- want to log everything
            end_reason = f"{type(exc).__name__}: {exc}"
            end_exc = exc
        finally:
            self._clients.discard(ws)
            if end_exc is not None:
                logger.info(
                    "Bookmap client disconnected from %s (%s)",
                    peer,
                    end_reason,
                    exc_info=end_exc,
                )
            else:
                logger.info(
                    "Bookmap client disconnected from %s (%s)",
                    peer,
                    end_reason,
                )

    def _fanout(self, payload: str) -> None:
        for ws in list(self._clients):
            queue = getattr(ws, "_bm_queue", None)
            if queue is None:
                continue
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Slow consumer — drop the message rather than block the
                # publisher. Clients can reconnect to resync.
                logger.debug("Bookmap client queue full; dropping event")


def _json_default(value: Any) -> Any:
    """Fallback JSON encoder for enum / dataclass-like values."""
    name = getattr(value, "value", None)
    if isinstance(name, (str, int, float, bool)):
        return name
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return name
    return str(value)
