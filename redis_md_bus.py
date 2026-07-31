"""Live market-data bus via Redis Streams (``md:{venue}:{kind}``).

Publish is best-effort: failures are logged and never abort Parquet writes.
Streams are trimmed with approximate ``MAXLEN`` so Redis is not a warehouse.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from market_data import EventKind, MarketEvent, stream_key

logger = logging.getLogger(__name__)

# Keep roughly this many entries per stream (~minutes of busy depth).
_DEFAULT_MAXLEN = 200_000


class RedisMDBus:
    """XADD normalized events to venue-namespaced Redis Streams."""

    def __init__(
        self,
        redis_url: Optional[str] = None,
        *,
        maxlen: int = _DEFAULT_MAXLEN,
    ) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL")
        self._maxlen = int(maxlen)
        self._client = None
        if not self._url:
            return
        try:
            import msgpack  # noqa: F401
            import redis
        except ImportError:
            logger.warning(
                "REDIS_URL set but redis/msgpack not installed — live bus disabled"
            )
            return
        try:
            self._client = redis.from_url(
                self._url, socket_timeout=2.0, socket_keepalive=True
            )
            self._client.ping()
            logger.info("RedisMDBus connected to %s", self._url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("RedisMDBus connection failed (%s) — live bus disabled", exc)
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def publish(self, event: MarketEvent) -> None:
        if self._client is None:
            return
        try:
            import msgpack

            key = stream_key(event.venue, event.kind.value)
            body = {
                "venue": event.venue,
                "symbol": event.symbol,
                "kind": event.kind.value,
                "ts_exchange_ms": int(event.ts_exchange_ms),
                "ts_recv_ms": int(event.ts_recv_ms),
                "seq": int(event.seq) if event.seq is not None else -1,
                **event.payload,
            }
            # Flatten depth lists for msgpack field; store whole payload as one blob.
            packed = msgpack.packb(body, use_bin_type=True)
            self._client.xadd(
                key,
                {"symbol": event.symbol, "payload": packed},
                maxlen=self._maxlen,
                approximate=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "RedisMDBus publish failed venue=%s symbol=%s kind=%s",
                event.venue, event.symbol, event.kind.value,
            )


def unpack_stream_payload(fields: Dict[Any, Any]) -> Dict[str, Any]:
    """Decode an XREAD entry's fields into the market-data dict."""
    import msgpack

    raw = fields.get(b"payload") or fields.get("payload")
    if raw is None:
        raise ValueError("stream entry missing payload")
    return msgpack.unpackb(raw, raw=False)


__all__ = ["RedisMDBus", "unpack_stream_payload"]
