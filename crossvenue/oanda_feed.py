"""
Oanda L1 feed — collects L1 price data from Oanda for cross-venue confirmation.

Uses Oanda's REST API with S5 (5-second) candles as an L1 proxy. The mid-price
(average of bid/ask close) provides a venue-independent price signal at Wave cadence.

For live use, a streaming approach (oandapyV20 streaming API) could replace polling.
For backtest, stored S5 candle data is replayed deterministically.
"""

from __future__ import annotations

import os
import time
import logging
from typing import List, Tuple, Optional, Callable

logger = logging.getLogger(__name__)

PriceCallback = Callable[[str, float, int], None]  # (venue, price, timestamp_ms)


class OandaL1Feed:
    """Polls Oanda S5 candles to produce L1 mid-price observations.

    Args:
        symbol: Oanda instrument name (e.g., "BTC_USD", "EUR_USD").
        poll_interval_s: seconds between REST polls (default 5).
        on_price: callback invoked for each new price observation.
    """

    def __init__(
        self,
        symbol: str,
        poll_interval_s: float = 5.0,
        on_price: Optional[PriceCallback] = None,
    ):
        self._symbol = symbol
        self._poll_interval = poll_interval_s
        self._on_price = on_price
        self._running = False
        self._last_ts: int = 0

        self._account_id = os.getenv("OANDA_ACCOUNT_ID", "")
        self._access_token = os.getenv("OANDA_ACCESS_TOKEN", "")
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return
        try:
            from oandapyV20 import API
            self._client = API(access_token=self._access_token)
        except ImportError:
            logger.warning("oandapyV20 not installed; OandaL1Feed will not function")

    def fetch_recent(self, count: int = 1) -> List[Tuple[int, float]]:
        """Fetch recent S5 candles and return (timestamp_ms, mid_close) pairs."""
        self._ensure_client()
        if self._client is None:
            return []

        try:
            import oandapyV20.endpoints.instruments as instruments
            params = {
                "granularity": "S5",
                "count": count,
                "price": "MBA",
            }
            r = instruments.InstrumentsCandles(
                instrument=self._symbol, params=params
            )
            rv = self._client.request(r)
            results = []
            for candle in rv.get("candles", []):
                if not candle.get("complete", False) and count > 1:
                    continue
                ts_str = candle.get("time", "")
                try:
                    import pandas as pd
                    ts_ms = int(pd.to_datetime(ts_str).timestamp() * 1000)
                except Exception:
                    continue
                mid_close = float(candle["mid"]["c"])
                results.append((ts_ms, mid_close))
            return results
        except Exception as ex:
            logger.error(f"OandaL1Feed fetch error: {ex}")
            return []

    def poll_once(self) -> Optional[Tuple[int, float]]:
        """Fetch the latest S5 candle and return (ts_ms, mid_close), or None."""
        candles = self.fetch_recent(count=2)
        if not candles:
            return None
        ts, price = candles[-1]
        if ts <= self._last_ts:
            return None
        self._last_ts = ts
        if self._on_price:
            self._on_price("oanda", price, ts)
        return ts, price

    def fetch_historical(
        self,
        start_time_ms: int,
        end_time_ms: int,
        granularity: str = "S5",
    ) -> List[Tuple[int, float]]:
        """Fetch historical S5 candles for backtest storage.

        Returns list of (timestamp_ms, mid_close).
        """
        self._ensure_client()
        if self._client is None:
            return []

        try:
            import oandapyV20.endpoints.instruments as instruments
            import pandas as pd
            from datetime import datetime, timezone

            start_str = datetime.fromtimestamp(
                start_time_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            end_str = datetime.fromtimestamp(
                end_time_ms / 1000, tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")

            params = {
                "from": start_str,
                "to": end_str,
                "granularity": granularity,
                "price": "MBA",
            }
            r = instruments.InstrumentsCandles(
                instrument=self._symbol, params=params
            )
            rv = self._client.request(r)
            results = []
            for candle in rv.get("candles", []):
                ts_ms = int(pd.to_datetime(candle["time"]).timestamp() * 1000)
                mid_close = float(candle["mid"]["c"])
                results.append((ts_ms, mid_close))
            return results
        except Exception as ex:
            logger.error(f"OandaL1Feed historical fetch error: {ex}")
            return []
