"""Stream message construction and serialisation.

The strategy service emits **raw market data only** — no rendered images,
no pre-aggregated depth columns.  The UI service owns all rendering
decisions (tick size, price range, colour mapping, zoom).

Wire format
-----------
All messages are msgpack-encoded objects with a ``"type"`` discriminator.
``msgpack`` is used because:

* It is ~5-10x smaller than JSON for numeric arrays (relevant for the
  high-frequency ``BOOK_UPDATE`` stream).
* The TypeScript client side has well-maintained decoders
  (``@msgpack/msgpack``).
* Binary frames over WebSocket avoid base64 overhead.

Message types
~~~~~~~~~~~~~

* ``TRADE`` — one aggressed trade
  ``{ type: "TRADE", symbol, ts_ms, price, qty, side }``
  ``side`` is ``"BUY"`` if the buyer was the aggressor (i.e. the trade
  lifted the ask), ``"SELL"`` otherwise.

* ``BOOK_UPDATE`` — full order book snapshot at ~10 Hz
  ``{ type: "BOOK_UPDATE", symbol, ts_ms,
      bids: [[price, size], ...], asks: [[price, size], ...] }``

* ``CANDLE`` — OHLCV bucket on close (or on preload)
  ``{ type: "CANDLE", symbol, ts_ms, bucket_ms, o, h, l, c,
      vol, buy_vol, sell_vol }``

* ``CVD_UPDATE`` — cumulative volume delta point
  ``{ type: "CVD_UPDATE", symbol, ts_ms, value }``

* ``VP_UPDATE`` — volume profile bars (rolling window)
  ``{ type: "VP_UPDATE", symbol, ts_ms,
      bars: [{ price, total, buy, sell }, ...] }``

* ``SIGNAL`` — strategy entry / exit
  ``{ type: "SIGNAL", symbol, ts_ms, signal_type, price,
      stop, target, strength }``

* ``HEALTH`` — pipeline status
  ``{ type: "HEALTH", ts_ms, lag_ms, symbols, engine_state }``
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

import msgpack


__all__ = [
    "encode",
    "trade_msg",
    "book_update_msg",
    "candle_msg",
    "cvd_msg",
    "vp_msg",
    "signal_msg",
    "health_msg",
]


def encode(message: Mapping[str, Any]) -> bytes:
    """Serialise a single message dict to msgpack bytes."""
    return msgpack.packb(message, use_bin_type=True, use_single_float=False)


def trade_msg(
    *,
    symbol: str,
    ts_ms: int,
    price: float,
    qty: float,
    is_buyer_maker: bool,
) -> dict[str, Any]:
    """Construct a ``TRADE`` message.

    ``is_buyer_maker`` follows the Binance convention:
        * ``True``  → the buyer was the maker, so the trade lifted the
          bid → sell aggression.
        * ``False`` → the buyer was the taker → buy aggression.
    """
    return {
        "type": "TRADE",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "price": float(price),
        "qty": float(qty),
        "side": "SELL" if is_buyer_maker else "BUY",
    }


def book_update_msg(
    *,
    symbol: str,
    ts_ms: int,
    bids: Iterable[tuple[float, float]],
    asks: Iterable[tuple[float, float]],
) -> dict[str, Any]:
    """Construct a ``BOOK_UPDATE`` message.

    Each side is a list of ``[price, size]`` pairs sorted best-first
    (bids descending, asks ascending) by the caller.
    """
    return {
        "type": "BOOK_UPDATE",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "bids": [[float(p), float(q)] for p, q in bids],
        "asks": [[float(p), float(q)] for p, q in asks],
    }


def candle_msg(
    *,
    symbol: str,
    ts_ms: int,
    bucket_ms: int,
    o: float,
    h: float,
    l: float,
    c: float,
    vol: float,
    buy_vol: float,
    sell_vol: float,
) -> dict[str, Any]:
    """Construct a ``CANDLE`` message (one OHLCV bucket)."""
    return {
        "type": "CANDLE",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "bucket_ms": int(bucket_ms),
        "o": float(o),
        "h": float(h),
        "l": float(l),
        "c": float(c),
        "vol": float(vol),
        "buy_vol": float(buy_vol),
        "sell_vol": float(sell_vol),
    }


def cvd_msg(*, symbol: str, ts_ms: int, value: float) -> dict[str, Any]:
    return {
        "type": "CVD_UPDATE",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "value": float(value),
    }


def vp_msg(
    *,
    symbol: str,
    ts_ms: int,
    bars: Iterable[tuple[float, float, float, float]],
) -> dict[str, Any]:
    """``bars`` items are ``(price, total, buy, sell)``."""
    return {
        "type": "VP_UPDATE",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "bars": [
            {
                "price": float(price),
                "total": float(total),
                "buy": float(buy),
                "sell": float(sell),
            }
            for price, total, buy, sell in bars
        ],
    }


def signal_msg(
    *,
    symbol: str,
    ts_ms: int,
    signal_type: str,
    price: float,
    stop: float = 0.0,
    target: float = 0.0,
    strength: float = 0.0,
) -> dict[str, Any]:
    return {
        "type": "SIGNAL",
        "symbol": symbol,
        "ts_ms": int(ts_ms),
        "signal_type": signal_type,
        "price": float(price),
        "stop": float(stop),
        "target": float(target),
        "strength": float(strength),
    }


def health_msg(
    *,
    ts_ms: int,
    lag_ms: float,
    symbols: Iterable[str],
    engine_state: str,
) -> dict[str, Any]:
    return {
        "type": "HEALTH",
        "ts_ms": int(ts_ms),
        "lag_ms": float(lag_ms),
        "symbols": list(symbols),
        "engine_state": engine_state,
    }
