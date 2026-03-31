"""Per-stream connection health for Binance (and similar) WS feeds.

Used by the data_feed asyncio runners and by the UI status panel.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class FeedState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    LIVE = "live"
    STALE = "stale"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


FEED_STALE_MS = 10_000
FEED_RECONNECT_INITIAL_BACKOFF_S = 1.0
FEED_RECONNECT_MAX_BACKOFF_S = 30.0
FEED_MAX_CONSECUTIVE_FAILURES = 10


@dataclass
class FeedStreamHealth:
    """Per-stream (trade or depth) health snapshot."""

    state: FeedState = FeedState.DISCONNECTED
    symbol: str = ""
    last_msg_wall_clock: float = 0.0
    last_msg_exchange_ts: int = 0
    messages_received: int = 0
    reconnect_count: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    last_reconnect_wall_clock: float = 0.0

    @property
    def seconds_since_last_msg(self) -> float:
        if self.last_msg_wall_clock <= 0:
            return 999.0
        return time.monotonic() - self.last_msg_wall_clock

    @property
    def is_healthy(self) -> bool:
        return self.state == FeedState.LIVE

    def on_message(self, exchange_ts: int = 0):
        self.last_msg_wall_clock = time.monotonic()
        if exchange_ts > 0:
            self.last_msg_exchange_ts = exchange_ts
        self.messages_received += 1
        if self.state != FeedState.LIVE:
            self.state = FeedState.LIVE
            self.consecutive_failures = 0

    def on_disconnect(self, error: str = ""):
        self.state = FeedState.DISCONNECTED
        if error:
            self.last_error = error

    def on_reconnect_start(self):
        self.state = FeedState.RECONNECTING
        self.last_reconnect_wall_clock = time.monotonic()
        self.reconnect_count += 1

    def on_reconnect_fail(self, error: str):
        self.consecutive_failures += 1
        self.last_error = error
        if self.consecutive_failures >= FEED_MAX_CONSECUTIVE_FAILURES:
            self.state = FeedState.FAILED
        else:
            self.state = FeedState.DISCONNECTED

    def on_stale(self):
        if self.state == FeedState.LIVE:
            self.state = FeedState.STALE

    @property
    def short_status(self) -> str:
        s = self.state.value.upper()
        if self.state == FeedState.LIVE:
            return s
        if self.state == FeedState.STALE:
            return f"{s} {self.seconds_since_last_msg:.0f}s"
        if self.state == FeedState.RECONNECTING:
            return f"{s} #{self.reconnect_count}"
        if self.state == FeedState.FAILED:
            return f"{s} ({self.consecutive_failures}x)"
        return s
