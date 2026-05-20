"""Bookmap publisher.

Lightweight WebSocket bridge that forwards live execution events from the
Python / C++ trading engine to the Bookmap Java add-on running locally.

The publisher is:

  * visualisation-only — it never sends commands back to the engine
  * thread-safe — callers may invoke ``on_intent`` / ``on_metrics`` from
    any thread; the publisher owns a background asyncio loop
  * decoupled — when no Bookmap client is connected, events are silently
    dropped (or buffered briefly) so the trading engine is never blocked
  * optional — ``execution.live_runner.run_live_execute`` accepts a
    ``bookmap_publisher`` kwarg; passing ``None`` disables Bookmap
    publishing entirely with zero behavioural impact

Two entry-points:

  * :class:`BookmapPublisher` — real publisher driven by the execution
    engine; see :func:`make_publisher` for a convenience constructor
  * ``python -m bookmap_publisher.mock_emitter`` — standalone demo that
    emits all five MVP event types without the real trading stack
"""

from bookmap_publisher.publisher import BookmapPublisher, make_publisher

__all__ = ["BookmapPublisher", "make_publisher"]
