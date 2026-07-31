"""Normalized multi-venue market-data contract.

Every venue adapter emits :class:`MarketEvent` instances. Durable sinks
(:class:`tick_parquet_store.ImmutableShardWriter`) and the live bus
(:class:`redis_md_bus.RedisMDBus`) are venue-agnostic — adding Oanda /
IB / cTrader means a new adapter, not a new pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterable, Optional, Set


class EventKind(str, Enum):
    TRADE = "trade"
    QUOTE = "quote"
    DEPTH_UPDATE = "depth_update"
    DEPTH_SNAPSHOT = "depth_snapshot"
    CANDLE = "candle"


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """Venue-normalized market-data event."""

    venue: str
    symbol: str
    kind: EventKind
    ts_exchange_ms: int
    ts_recv_ms: int
    payload: Dict[str, Any] = field(default_factory=dict)
    seq: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "venue", str(self.venue).lower())
        object.__setattr__(self, "symbol", str(self.symbol).upper())
        if isinstance(self.kind, str):
            object.__setattr__(self, "kind", EventKind(self.kind))


# Sink callback type used by adapters.
EventHandler = Callable[[MarketEvent], None]


class VenueAdapter(ABC):
    """Connect to one venue and emit :class:`MarketEvent` values."""

    name: str = "venue"

    @abstractmethod
    def capabilities(self) -> Set[str]:
        """Declared products, e.g. ``{'trades', 'depth'}``."""

    @abstractmethod
    def run(
        self,
        symbol: str,
        on_event: EventHandler,
        *,
        duration_seconds: int = 0,
    ) -> None:
        """Block until interrupted / duration elapses, calling ``on_event``."""


def stream_key(venue: str, kind: str) -> str:
    """Redis Stream name: ``md:{venue}:{kind}``."""
    k = kind
    if k in ("depth_update", "depth_snapshot"):
        k = "depth"
    elif k == "trade":
        k = "trades"
    elif k == "quote":
        k = "quotes"
    elif k == "candle":
        k = "candles"
    return f"md:{str(venue).lower()}:{k}"


__all__ = [
    "EventKind",
    "EventHandler",
    "MarketEvent",
    "VenueAdapter",
    "stream_key",
]
