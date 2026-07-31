"""Binance futures/spot WebSocket adapter → :class:`MarketEvent`."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Set

from market_data import EventHandler, EventKind, MarketEvent, VenueAdapter

logger = logging.getLogger(__name__)


class BinanceAdapter(VenueAdapter):
    """Live Binance trades + L2 depth (no HDF5 / TickStore)."""

    name = "binance"

    def __init__(self, *, futures: bool = True) -> None:
        self.futures = futures
        if futures:
            self.ws_base = "wss://fstream.binance.com/ws/"
            self.rest_base = "https://fapi.binance.com/fapi/v1/depth"
        else:
            self.ws_base = "wss://stream.binance.com:9443/ws/"
            self.rest_base = "https://api.binance.com/api/v3/depth"

    def capabilities(self) -> Set[str]:
        return {"trades", "depth"}

    def run(
        self,
        symbol: str,
        on_event: EventHandler,
        *,
        duration_seconds: int = 0,
    ) -> None:
        symbol_upper = symbol.upper()
        self._emit_depth_snapshot(symbol_upper, on_event)
        try:
            asyncio.run(
                self._run_async(symbol_upper, on_event, duration_seconds)
            )
        except KeyboardInterrupt:
            logger.info("BinanceAdapter interrupted for %s", symbol_upper)

    def _emit_depth_snapshot(self, symbol: str, on_event: EventHandler) -> None:
        try:
            from data_feed.binance_depth_rest import fetch_binance_depth_book

            book = fetch_binance_depth_book(
                self.rest_base, symbol, limit=1000, timeout=10.0
            )
            now_ms = int(time.time() * 1000)
            on_event(
                MarketEvent(
                    venue=self.name,
                    symbol=symbol,
                    kind=EventKind.DEPTH_SNAPSHOT,
                    ts_exchange_ms=now_ms,
                    ts_recv_ms=now_ms,
                    payload={
                        "bids": book.bids,
                        "asks": book.asks,
                        "last_update_id": book.last_update_id,
                        "is_snapshot": True,
                    },
                    seq=book.last_update_id,
                )
            )
            logger.info(
                "Depth snapshot %s: %d bids, %d asks",
                symbol, len(book.bids), len(book.asks),
            )
        except Exception:
            logger.exception("Failed to fetch depth snapshot for %s", symbol)

    async def _run_async(
        self,
        symbol: str,
        on_event: EventHandler,
        duration_seconds: int,
    ) -> None:
        import websockets

        symbol_lower = symbol.lower()
        trade_uri = f"{self.ws_base}{symbol_lower}@trade"
        depth_uri = f"{self.ws_base}{symbol_lower}@depth@100ms"
        trade_count = 0
        depth_count = 0

        async def read_trades() -> None:
            nonlocal trade_count
            async for ws in websockets.connect(trade_uri):
                try:
                    async for msg in ws:
                        j = json.loads(msg)
                        recv_ms = int(time.time() * 1000)
                        ts_ms = int(j["T"])
                        on_event(
                            MarketEvent(
                                venue=self.name,
                                symbol=symbol,
                                kind=EventKind.TRADE,
                                ts_exchange_ms=ts_ms,
                                ts_recv_ms=recv_ms,
                                payload={
                                    "price": float(j["p"]),
                                    "qty": float(j["q"]),
                                    "is_buyer_maker": bool(j["m"]),
                                },
                                seq=int(j.get("t") or 0) or None,
                            )
                        )
                        trade_count += 1
                except websockets.ConnectionClosed:
                    logger.warning("Trade WS reconnecting for %s...", symbol)
                    continue

        async def read_depth() -> None:
            nonlocal depth_count
            async for ws in websockets.connect(depth_uri):
                try:
                    async for msg in ws:
                        j = json.loads(msg)
                        recv_ms = int(time.time() * 1000)
                        ts_ms = int(j.get("E", 0))
                        first_id = int(j.get("U", 0))
                        final_id = int(j.get("u", 0))
                        raw_bids = j.get("b", []) or []
                        raw_asks = j.get("a", []) or []
                        on_event(
                            MarketEvent(
                                venue=self.name,
                                symbol=symbol,
                                kind=EventKind.DEPTH_UPDATE,
                                ts_exchange_ms=ts_ms,
                                ts_recv_ms=recv_ms,
                                payload={
                                    "bids": [
                                        (float(b[0]), float(b[1])) for b in raw_bids
                                    ],
                                    "asks": [
                                        (float(a[0]), float(a[1])) for a in raw_asks
                                    ],
                                    "first_update_id": first_id,
                                    "final_update_id": final_id,
                                    "is_snapshot": False,
                                },
                                seq=final_id,
                            )
                        )
                        depth_count += 1
                except websockets.ConnectionClosed:
                    logger.warning("Depth WS reconnecting for %s...", symbol)
                    continue

        async def status() -> None:
            while True:
                await asyncio.sleep(10)
                logger.info(
                    "Binance %s: %d trades, %d depth updates",
                    symbol, trade_count, depth_count,
                )

        tasks = [
            asyncio.create_task(read_trades()),
            asyncio.create_task(read_depth()),
            asyncio.create_task(status()),
        ]
        try:
            if duration_seconds > 0:
                await asyncio.sleep(duration_seconds)
            else:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.info(
                "BinanceAdapter done %s: %d trades, %d depth",
                symbol, trade_count, depth_count,
            )


__all__ = ["BinanceAdapter"]
