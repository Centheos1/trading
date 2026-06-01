import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Tuple
import time

from database import Hdf5Client
from utils import ms_to_dt, dt_to_ms
from exchanges.binance import BinanceClient
from exchanges.oanda import OandaClient
from data_feed import fetch_and_build_depth_snapshot

logger = logging.getLogger()

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
except ImportError:
    ofe = None

# Redis is optional at import time so unit tests / offline runs work
# without the distributed stack.  When ``REDIS_URL`` is set the
# ``TickDataCollector`` publishes raw trades and depth updates to
# ``trades:{symbol}`` and ``depth:{symbol}`` channels for the strategy
# service to consume.
try:
    import msgpack  # type: ignore
except ImportError:  # pragma: no cover
    msgpack = None  # type: ignore

try:
    import redis as _redis  # type: ignore
except ImportError:  # pragma: no cover
    _redis = None  # type: ignore


class DataCollector:
    def __init__(self, exchange: str):
        if exchange == "binance":
            self.client = BinanceClient()
        elif exchange == "oanda":
            self.client = OandaClient()
        else:
            raise ValueError

        self.h5_db = Hdf5Client(exchange=exchange)
        self.exchange = exchange

    def sync_all(self, from_time: int):
        # WIP - Testing
        # for symbol in [
        #                'NATGAS_USD',
        #                'USD_HUF',
        #                'NAS100_USD',
        #                'EUR_CAD',
        #                'USD_ZAR',
        #                'DE30_EUR',
        #                'XPT_USD',
        #                'CORN_USD',
        #                'XAU_EUR',
        #                'SOYBN_USD',
        #                'EUR_DKK',
        #                'USD_DKK',
        #                'NL25_EUR',
        #                'CHINAH_HKD',
        #                'EUR_USD',
        #                'SG30_SGD',
        #                'AUD_NZD',
        #                'XAG_SGD',
        #                'CHF_ZAR',
        #                'AU200_AUD',
        #                'CAD_SGD',
        #                'JP225_USD',
        #                'FR40_EUR',
        #                'USB30Y_USD',
        #                'XAG_CAD',
        #                'NZD_HKD',
        #                'EUR_CHF',
        #                'XAU_AUD',
        #                'WHEAT_USD',
        #                'GBP_CAD',
        #                'UK100_GBP',
        #                'USD_MXN',
        #                'GBP_USD',
        #                'EUR_CZK',
        #                'XAU_CHF',
        #                'XAG_USD',
        #                'UK10YB_GBP',
        #                'AUD_CAD',
        #                'USB05Y_USD',
        #                'EUR_PLN',
        #                'SUGAR_USD',
        #                'GBP_SGD',
        #                'USD_SEK',
        #                'XPD_USD',
        #                'XAU_CAD',
        #                'EUR_HUF',
        #                'DE10YB_EUR',
        #                'GBP_PLN',
        #                'EUR_SEK',
        #                'USD_SGD',
        #                'GBP_NZD',
        #                'USD_TRY',
        #                'GBP_JPY',
        #                'CHF_HKD']:
            # WIP - Testing
        for symbol in self.client.symbols:
            self.collect_all(symbol, from_time)
            time.sleep(30)

    def collect_all(self, symbol: str, from_time: int):
        if self.exchange == "oanda":
            num_cols = 7
        else:
            num_cols = 6

        logger.info(
            f"[DataCollector.collect_all] Start: symbol: {symbol}, from_time: {ms_to_dt(from_time)}, num_cols: {num_cols}")

        self.h5_db.create_dataset(symbol, num_cols)
        oldest_ts, most_recent_ts = self.h5_db.get_first_last_timestamp(symbol)
        data = list()

        # Initial Request
        if oldest_ts is None:
            most_recent_ts = dt_to_ms(datetime.now(timezone.utc)) - 60000
            oldest_ts = from_time
            logger.info(
                f"Initial Request: oldest_ts: {ms_to_dt(oldest_ts)} | most_recent_ts: {ms_to_dt(most_recent_ts)} | from_time: {ms_to_dt(from_time)}\n{'-' * 80}")
            oldest_ts = from_time
            for start, end in self._generate_batches(oldest_ts, most_recent_ts):
                logger.debug(f"Collecting symbol: {symbol}, start: {start}, end: {end}")
                data.extend(self.client.get_historical_data(symbol=symbol, start_time=start, end_time=end))
                if len(data) > 10000:
                    self._write_data(symbol, data)
                    data.clear()
                time.sleep(1.1)

            if len(data) == 0:
                logger.warning(f"{self.exchange} {symbol}: no initial data found")
                return
            else:
                logger.info(
                    f"{self.exchange} {symbol}: Collected {len(data)} initial data from {ms_to_dt(data[0][0])} to {ms_to_dt(data[-1][0])}")

                oldest_ts = data[0][0]
                most_recent_ts = data[-1][0]
                self._write_data(symbol, data)
                data.clear()

        # Most recent data
        logger.info(
            f"Most recent data: oldest_ts: {ms_to_dt(oldest_ts)} | most_recent_ts: {ms_to_dt(most_recent_ts)} | from_time: {ms_to_dt(from_time)}\n{'-' * 80}")
        for start, end in self._generate_batches(int(most_recent_ts + 60000),
                                                 dt_to_ms(datetime.now(timezone.utc)) - 60000):
            logger.debug(f"Collecting symbol: {symbol}, start: {start}, end: {end}")
            data.extend(self.client.get_historical_data(symbol=symbol, start_time=start, end_time=end))
            if len(data) > 10000:
                self._write_data(symbol, data)
                data.clear()
            time.sleep(1.1)

        if len(data):
            logger.info(
                f"{self.exchange} {symbol}: Collected {len(data)} most recent data from {ms_to_dt(data[0][0])} to {ms_to_dt(data[-1][0])}")
        else:
            logger.info(
                f"{self.exchange} {symbol}: Collected {len(data)} most recent data.")

        self._write_data(symbol, data)
        data.clear()

        # Older data
        logger.info(
            f"Older Data: oldest_ts: {ms_to_dt(oldest_ts)} | most_recent_ts: {ms_to_dt(most_recent_ts)} | from_time: {ms_to_dt(from_time)}\n{'-' * 80}")
        for start, end in self._generate_batches(from_time, oldest_ts - 60000):
            logger.debug(f"symbol: {symbol} start: {start} end: {end}")
            data.extend(self.client.get_historical_data(symbol=symbol, start_time=start, end_time=end))
            if len(data) > 10000:
                self._write_data(symbol, data)
                data.clear()
            time.sleep(1.1)

        if len(data):
            logger.info(
                f"{self.exchange} {symbol}: Collected {len(data)} older data from {ms_to_dt(data[0][0])} to {ms_to_dt(data[-1][0])}")
        else:
            logger.info(
                f"{self.exchange} {symbol}: Collected {len(data)} older data.")

        self._write_data(symbol, data)
        data.clear()

    def _write_data(self, symbol, data):
        if len(data):
            batch_size = 10000
            for i in range(0, len(data), batch_size):
                batch = data[i:i + batch_size]
                self.h5_db.write_data(symbol, batch)
        logger.info(f"Wrote {len(data)} rows to {self.exchange} {symbol}\n{'-' * 80}")

    @staticmethod
    def _generate_batches(from_timestamp_ms: int, to_timestamp_ms: int, max_minutes=5000) -> Tuple[int, int]:
        # Convert from and to milliseconds to datetime
        from_date = datetime.fromtimestamp(from_timestamp_ms / 1000, tz=timezone.utc)
        to_date = datetime.fromtimestamp(to_timestamp_ms / 1000, tz=timezone.utc)

        # Extend to end of day for 'to_date'
        to_date = to_date.replace(hour=23, minute=59, second=59)

        current = from_date
        while current < to_date:
            batch_end = min(current + timedelta(minutes=max_minutes), to_date)
            yield int(current.timestamp() * 1000), int(batch_end.timestamp() * 1000)
            current = batch_end


class TickDataCollector:
    """Collects real-time tick and depth data from Binance via Python WebSocket
    and stores it in HDF5 via the C++ engine for order flow backtesting.

    Distributed-architecture extension:
        When ``REDIS_URL`` is set (or ``redis_url`` is passed) every trade
        and depth update is also published to the Redis pub/sub channels
        ``trades:{SYMBOL}`` and ``depth:{SYMBOL}`` as msgpack payloads.
        The strategy service subscribes to these channels via
        :class:`strategy.engine.live_engine.LiveEngine`.
    """

    def __init__(
        self,
        exchange: str = "binance",
        futures: bool = True,
        store_path: str | None = None,
        redis_url: str | None = None,
    ):
        if ofe is None:
            raise RuntimeError(
                "orderflow_engine C++ module not built. "
                "Build: cd backtestingCpp/orderflow && ./build.sh"
            )

        self.exchange = exchange
        self.futures = futures

        if store_path is None:
            os.makedirs("data", exist_ok=True)
            store_path = os.path.join("data", f"{exchange}_ticks.h5")
        else:
            os.makedirs(os.path.dirname(os.path.abspath(store_path)), exist_ok=True)
        self.store = ofe.TickStore(store_path)
        self.engine = ofe.OrderFlowEngine()
        self.engine.set_tick_store(self.store, "")

        if futures:
            self.ws_base = "wss://fstream.binance.com/ws/"
            self.rest_base = "https://fapi.binance.com/fapi/v1/depth"
        else:
            self.ws_base = "wss://stream.binance.com:9443/ws/"
            self.rest_base = "https://api.binance.com/api/v3/depth"

        # Optional Redis publisher.  We use the sync client because the
        # WS reader threads are already in tight per-message loops; the
        # publish is a single ``socket.send`` and the overhead is
        # negligible compared to msgpack encoding.
        self._redis_url = redis_url or os.getenv("REDIS_URL")
        self._redis = None
        if self._redis_url:
            if _redis is None or msgpack is None:
                logger.warning(
                    "REDIS_URL set but 'redis'/'msgpack' not installed — "
                    "data will not be published to the strategy service"
                )
            else:
                try:
                    self._redis = _redis.from_url(
                        self._redis_url, socket_timeout=2.0, socket_keepalive=True
                    )
                    self._redis.ping()
                    logger.info("TickDataCollector connected to Redis %s",
                                self._redis_url)
                except Exception as exc:  # pragma: no cover - depends on env
                    logger.warning("Redis connection failed (%s); "
                                   "publishing disabled", exc)
                    self._redis = None

    # ------------------------------------------------------ pub helpers

    def _publish_trade(
        self, symbol: str, ts_ms: int, price: float, qty: float,
        is_buyer_maker: bool,
    ) -> None:
        if self._redis is None or msgpack is None:
            return
        try:
            payload = msgpack.packb(
                {
                    "ts_ms": int(ts_ms),
                    "price": float(price),
                    "qty": float(qty),
                    "is_buyer_maker": bool(is_buyer_maker),
                },
                use_bin_type=True,
            )
            self._redis.publish(f"trades:{symbol}", payload)
        except Exception:
            # Publishing is best-effort — the HDF5 store remains canonical.
            logger.exception("Redis trade publish failed")

    def _publish_depth(
        self, symbol: str, ts_ms: int, bids: list, asks: list,
        first_update_id: int = 0, final_update_id: int = 0,
        is_snapshot: bool = False,
    ) -> None:
        if self._redis is None or msgpack is None:
            return
        try:
            payload = msgpack.packb(
                {
                    "ts_ms": int(ts_ms),
                    "is_snapshot": bool(is_snapshot),
                    "first_update_id": int(first_update_id),
                    "final_update_id": int(final_update_id),
                    "bids": [[float(p), float(q)] for p, q in bids],
                    "asks": [[float(p), float(q)] for p, q in asks],
                },
                use_bin_type=True,
            )
            self._redis.publish(f"depth:{symbol}", payload)
        except Exception:
            logger.exception("Redis depth publish failed")

    def collect(self, symbol: str, duration_seconds: int = 0):
        import asyncio
        import json as pyjson

        symbol_upper = symbol.upper()
        symbol_lower = symbol.lower()

        self.engine.set_tick_store(self.store, symbol_upper)

        logger.info(f"Starting tick data collection for {symbol_upper}")

        self._fetch_depth_snapshot(symbol_upper)

        trade_count = 0
        depth_count = 0

        async def _run():
            nonlocal trade_count, depth_count
            import websockets

            trade_uri = f"{self.ws_base}{symbol_lower}@trade"
            depth_uri = f"{self.ws_base}{symbol_lower}@depth@100ms"

            async def read_trades():
                nonlocal trade_count
                async for ws in websockets.connect(trade_uri):
                    try:
                        async for msg in ws:
                            j = pyjson.loads(msg)
                            ts_ms = int(j["T"])
                            price = float(j["p"])
                            qty = float(j["q"])
                            is_buyer_maker = bool(j["m"])
                            trade = ofe.Trade()
                            trade.timestamp = ts_ms
                            trade.price = price
                            trade.quantity = qty
                            trade.is_buyer_maker = is_buyer_maker
                            self.engine.process_trade(trade)
                            # Publish to Redis for the strategy service.
                            self._publish_trade(
                                symbol_upper, ts_ms, price, qty,
                                is_buyer_maker,
                            )
                            trade_count += 1
                    except websockets.ConnectionClosed:
                        logger.warning("Trade WS reconnecting...")
                        continue

            async def read_depth():
                nonlocal depth_count
                async for ws in websockets.connect(depth_uri):
                    try:
                        async for msg in ws:
                            j = pyjson.loads(msg)
                            ts_ms = int(j.get("E", 0))
                            first_id = int(j.get("U", 0))
                            final_id = int(j.get("u", 0))
                            raw_bids = j.get("b", []) or []
                            raw_asks = j.get("a", []) or []
                            update = ofe.DepthUpdate()
                            update.timestamp = ts_ms
                            update.first_update_id = first_id
                            update.final_update_id = final_id
                            update.is_snapshot = False
                            bids = []
                            for b in raw_bids:
                                lv = ofe.DepthLevel()
                                lv.price = float(b[0])
                                lv.quantity = float(b[1])
                                bids.append(lv)
                            asks = []
                            for a in raw_asks:
                                lv = ofe.DepthLevel()
                                lv.price = float(a[0])
                                lv.quantity = float(a[1])
                                asks.append(lv)
                            update.bids = bids
                            update.asks = asks
                            self.engine.process_depth(update)
                            # Publish to Redis for the strategy service.
                            self._publish_depth(
                                symbol_upper, ts_ms, raw_bids, raw_asks,
                                first_update_id=first_id,
                                final_update_id=final_id,
                                is_snapshot=False,
                            )
                            depth_count += 1
                    except websockets.ConnectionClosed:
                        logger.warning("Depth WS reconnecting...")
                        continue

            async def status_printer():
                while True:
                    await asyncio.sleep(10)
                    logger.info(f"Collected {trade_count} trades, {depth_count} depth updates")

            tasks = [
                asyncio.create_task(read_trades()),
                asyncio.create_task(read_depth()),
                asyncio.create_task(status_printer()),
            ]

            if duration_seconds > 0:
                await asyncio.sleep(duration_seconds)
            else:
                logger.info("Collecting... Press Ctrl+C to stop.")
                done = asyncio.Event()
                try:
                    await done.wait()
                except asyncio.CancelledError:
                    pass

            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            logger.info("Collection interrupted by user")
        finally:
            self.store.flush()
            self.store.close()
            logger.info(
                f"Tick data collection complete for {symbol_upper}: "
                f"{trade_count} trades, {depth_count} depth updates"
            )

    def _fetch_depth_snapshot(self, symbol: str):
        """REST bootstrap; HTTP parsing in ``data_feed``."""
        try:
            ts_ms = int(time.time() * 1000)
            snapshot = fetch_and_build_depth_snapshot(
                ofe,
                self.rest_base,
                symbol,
                limit=1000,
                timeout=10.0,
                timestamp_ms=ts_ms,
            )
            self.engine.process_depth(snapshot)
            logger.info(
                "Loaded depth snapshot: %d bids, %d asks",
                len(snapshot.bids),
                len(snapshot.asks),
            )
            # Publish the initial snapshot so a freshly-started strategy
            # service has a full book before the diff stream starts.
            self._publish_depth(
                symbol,
                ts_ms,
                [(lv.price, lv.quantity) for lv in snapshot.bids],
                [(lv.price, lv.quantity) for lv in snapshot.asks],
                is_snapshot=True,
            )
        except Exception as e:
            logger.error("Failed to fetch depth snapshot: %s", e)


    # WIP - test this, if it works turn the filter back on in database
    # from typing import Generator, Tuple
    # @staticmethod
    # def _generate_batches(
    #         from_timestamp_ms: int,
    #         to_timestamp_ms: int,
    #         max_minutes=5000,
    #         reversed: bool = False
    # ) -> Generator[Tuple[int, int], None, None]:
    #     # Convert from and to milliseconds to datetime
    #     from_date = datetime.fromtimestamp(from_timestamp_ms / 1000, tz=timezone.utc)
    #     to_date = datetime.fromtimestamp(to_timestamp_ms / 1000, tz=timezone.utc)
    #
    #     # Extend to end of day for 'to_date'
    #     to_date = to_date.replace(hour=23, minute=59, second=59)
    #
    #     if not reversed:
    #         current = from_date
    #         while current < to_date:
    #             batch_end = min(current + timedelta(minutes=max_minutes), to_date)
    #             yield int(current.timestamp() * 1000), int(batch_end.timestamp() * 1000)
    #             current = batch_end
    #     else:
    #         current = to_date
    #         while current > from_date:
    #             batch_start = max(current - timedelta(minutes=max_minutes), from_date)
    #             yield int(batch_start.timestamp() * 1000), int(current.timestamp() * 1000)
    #             current = batch_start
