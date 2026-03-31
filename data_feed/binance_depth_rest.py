"""Binance REST depth snapshot (futures + spot).

HTTP fetch and JSON parsing live here. Conversion to ``orderflow_engine``
types is optional and takes the bound module as an argument so this file
stays importable without the C++ extension (e.g. lightweight tests).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

logger = logging.getLogger(__name__)

BINANCE_FUTURES_USDM_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"
BINANCE_SPOT_DEPTH_URL = "https://api.binance.com/api/v3/depth"


@dataclass(frozen=True, slots=True)
class BinanceDepthBook:
    """Normalized order book from Binance ``GET .../depth``."""

    last_update_id: int
    bids: list[tuple[float, float]]
    asks: list[tuple[float, float]]


def fetch_binance_depth_book(
    base_url: str,
    symbol: str,
    *,
    limit: int = 1000,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> BinanceDepthBook:
    """Fetch full depth via Binance REST and return parsed levels.

    Parameters
    ----------
    base_url:
        ``BINANCE_FUTURES_USDM_DEPTH_URL`` or ``BINANCE_SPOT_DEPTH_URL``
        (or compatible ``.../depth`` endpoint).
    symbol:
        Trading pair, e.g. ``\"BTCUSDT\"`` (Binance expects upper case).
    limit:
        Max levels per side (Binance caps this; 1000 is typical).
    timeout:
        Seconds for the HTTP request.
    session:
        Optional ``requests.Session`` for connection reuse.
    """
    sym = symbol.strip().upper()
    sess = session or requests
    resp = sess.get(
        base_url,
        params={"symbol": sym, "limit": limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    j = resp.json()

    last_id = int(j.get("lastUpdateId", 0))
    bids: list[tuple[float, float]] = []
    for row in j.get("bids", []):
        if len(row) >= 2:
            bids.append((float(row[0]), float(row[1])))
    asks: list[tuple[float, float]] = []
    for row in j.get("asks", []):
        if len(row) >= 2:
            asks.append((float(row[0]), float(row[1])))

    return BinanceDepthBook(
        last_update_id=last_id,
        bids=bids,
        asks=asks,
    )


def depth_book_to_engine_update(
    book: BinanceDepthBook,
    ofe: Any,
    *,
    timestamp_ms: int | None = None,
) -> Any:
    """Build ``orderflow_engine.DepthUpdate`` snapshot from a parsed book.

    ``ofe`` must be the imported ``orderflow_engine`` module (or compatible).
    """
    if timestamp_ms is None:
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    snap = ofe.DepthUpdate()
    snap.timestamp = timestamp_ms
    snap.first_update_id = book.last_update_id
    snap.final_update_id = book.last_update_id
    snap.is_snapshot = True

    bids = []
    for price, qty in book.bids:
        lv = ofe.DepthLevel()
        lv.price = price
        lv.quantity = qty
        bids.append(lv)
    asks = []
    for price, qty in book.asks:
        lv = ofe.DepthLevel()
        lv.price = price
        lv.quantity = qty
        asks.append(lv)
    snap.bids = bids
    snap.asks = asks
    return snap


def fetch_and_build_depth_snapshot(
    ofe: Any,
    base_url: str,
    symbol: str,
    *,
    limit: int = 1000,
    timeout: float = 10.0,
    timestamp_ms: int | None = None,
    session: requests.Session | None = None,
) -> Any:
    """One-shot: REST fetch + ``DepthUpdate`` for ``engine.process_depth``."""
    book = fetch_binance_depth_book(
        base_url, symbol, limit=limit, timeout=timeout, session=session
    )
    return depth_book_to_engine_update(
        book, ofe, timestamp_ms=timestamp_ms
    )
