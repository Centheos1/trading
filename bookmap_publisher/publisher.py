"""High-level Bookmap publisher.

Sits between :mod:`execution.live_runner` and :mod:`ws_server`. Callers
inside the trading engine never see the WebSocket layer directly: they
invoke :meth:`BookmapPublisher.on_intent`,
:meth:`BookmapPublisher.on_metrics`, and
:meth:`BookmapPublisher.on_health`, and the publisher takes care of
event construction + thread-safe enqueue + best-effort delivery.

The publisher follows the project's V1 invariants:

  * never blocks the caller (all dispatch is via the WS server's
    background loop)
  * never raises into the caller — every public method swallows its own
    exceptions and logs at debug
  * never references strategy / broker logic — it is purely an
    observation channel
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from bookmap_publisher import event_builder
from bookmap_publisher.ws_server import DEFAULT_HOST, DEFAULT_PORT, WsBroadcastServer

logger = logging.getLogger(__name__)


class BookmapPublisher:
    """Thread-safe live publisher of strategy events to the Bookmap add-on.

    The publisher owns a :class:`WsBroadcastServer` listening on
    ``ws://localhost:8765`` by default. Once :meth:`start` returns, any
    of the ``on_*`` callbacks may be invoked from any thread.
    """

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        symbol_default: str = "BTCUSDT",
    ) -> None:
        self._server = WsBroadcastServer(host=host, port=port)
        self._symbol_default = symbol_default.upper()
        self._started = False

    # ----------------------------------------------------------- lifecycle

    def start(self) -> bool:
        if self._started:
            return True
        ok = self._server.start()
        self._started = ok
        if not ok:
            logger.warning(
                "BookmapPublisher failed to start (server not ready); "
                "trading engine will continue without Bookmap visualisation",
            )
        return ok

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self.on_health(self._symbol_default, connected=False)
        except Exception:
            logger.debug("Final HEALTH publish failed", exc_info=True)
        self._server.stop()
        self._started = False

    def is_running(self) -> bool:
        return self._started and self._server.is_running()

    @property
    def port(self) -> int:
        return self._server.port

    # ----------------------------------------------------- engine callbacks

    def on_intent(
        self,
        intent: Any,
        exec_mgr: Any,
        symbol: Optional[str] = None,
    ) -> None:
        """Forward an accepted ``ExecutionIntent`` to the add-on.

        Called from :func:`execution.live_runner._gate_and_dispatch`
        after the broker dispatch path. Maps entry / exit intents to
        ENTRY / EXIT events; cancel / rearm / prepare intents are
        ignored (the Java add-on has no overlay concept for them yet).
        """
        if not self._started or intent is None:
            return
        sym = (symbol or self._symbol_default).upper()
        intent_type = getattr(intent, "intent_type", "") or ""
        try:
            if intent_type == "entry":
                self._server.publish(event_builder.entry_from_intent(intent, sym))
            elif intent_type == "exit":
                self._server.publish(
                    event_builder.exit_from_intent(intent, exec_mgr, sym)
                )
        except Exception:
            logger.debug("BookmapPublisher.on_intent failed", exc_info=True)

    def on_metrics(
        self,
        exec_mgr: Any,
        symbol: Optional[str] = None,
    ) -> None:
        """Publish a METRIC and a POSITION event from the current
        :class:`ExecutionManager` state.

        Called from the periodic status hook (default 30s) inside
        ``run_live_execute``.
        """
        if not self._started:
            return
        sym = (symbol or self._symbol_default).upper()
        try:
            self._server.publish(event_builder.metric_from_exec_mgr(exec_mgr, sym))
            self._server.publish(
                event_builder.position_from_exec_mgr(exec_mgr, sym)
            )
        except Exception:
            logger.debug("BookmapPublisher.on_metrics failed", exc_info=True)

    def on_health(
        self,
        symbol: Optional[str] = None,
        *,
        connected: bool,
    ) -> None:
        """Publish a HEALTH event indicating publisher connectivity."""
        if not self._started:
            return
        sym = (symbol or self._symbol_default).upper()
        try:
            self._server.publish(event_builder.health_event(sym, connected))
        except Exception:
            logger.debug("BookmapPublisher.on_health failed", exc_info=True)

    def publish_raw(self, event: dict) -> None:
        """Escape hatch — push a pre-built event dict.

        Used by :mod:`mock_emitter` and by future code that needs to
        publish events the canonical builders don't yet cover.
        """
        if not self._started:
            return
        try:
            self._server.publish(event)
        except Exception:
            logger.debug("BookmapPublisher.publish_raw failed", exc_info=True)


def make_publisher(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    symbol_default: str = "BTCUSDT",
    start: bool = True,
) -> Optional[BookmapPublisher]:
    """Convenience constructor for the common case.

    Returns a started :class:`BookmapPublisher`, or ``None`` if the WS
    server failed to come up. Callers should treat ``None`` as
    "Bookmap disabled" and continue without it — never raise.
    """
    pub = BookmapPublisher(host=host, port=port, symbol_default=symbol_default)
    if start:
        if not pub.start():
            return None
    return pub
