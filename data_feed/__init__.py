"""Data feed adapters: REST/WS ingress, no Qt dependencies.

Binance depth snapshot helpers live here so UI/orchestrators only call
into this layer and apply results to the engine (MVVM-friendly split).

WebSocket runners and per-stream health types live in sibling modules.
"""

from .binance_depth_rest import (
    BINANCE_FUTURES_USDM_DEPTH_URL,
    BINANCE_SPOT_DEPTH_URL,
    BinanceDepthBook,
    depth_book_to_engine_update,
    fetch_and_build_depth_snapshot,
    fetch_binance_depth_book,
)
from .binance_futures_ws import (
    BINANCE_USDM_FUTURES_WS_BASE,
    run_binance_usdm_futures_ws_feed,
)
from .binance_klines_rest import (
    BINANCE_FUTURES_USDM_KLINES_URL,
    BINANCE_SPOT_KLINES_URL,
    DEFAULT_KLINES_LIMIT,
    KLINES_FETCH_TIMEOUT_S,
    fetch_binance_klines,
    interval_ms_to_label,
)
from .stream_health import (
    FEED_MAX_CONSECUTIVE_FAILURES,
    FEED_RECONNECT_INITIAL_BACKOFF_S,
    FEED_RECONNECT_MAX_BACKOFF_S,
    FEED_STALE_MS,
    FeedState,
    FeedStreamHealth,
)

__all__ = [
    "BINANCE_FUTURES_USDM_DEPTH_URL",
    "BINANCE_FUTURES_USDM_KLINES_URL",
    "BINANCE_SPOT_DEPTH_URL",
    "BINANCE_SPOT_KLINES_URL",
    "BINANCE_USDM_FUTURES_WS_BASE",
    "BinanceDepthBook",
    "DEFAULT_KLINES_LIMIT",
    "depth_book_to_engine_update",
    "fetch_and_build_depth_snapshot",
    "fetch_binance_depth_book",
    "fetch_binance_klines",
    "FEED_MAX_CONSECUTIVE_FAILURES",
    "FEED_RECONNECT_INITIAL_BACKOFF_S",
    "FEED_RECONNECT_MAX_BACKOFF_S",
    "FEED_STALE_MS",
    "FeedState",
    "FeedStreamHealth",
    "interval_ms_to_label",
    "KLINES_FETCH_TIMEOUT_S",
    "run_binance_usdm_futures_ws_feed",
]
