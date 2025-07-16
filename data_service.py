import logging
from datetime import datetime, timezone, timedelta
from typing import Tuple
import time

from database import Hdf5Client
from utils import ms_to_dt, dt_to_ms
from exchanges.binance import BinanceClient
from exchanges.oanda import OandaClient

logger = logging.getLogger()


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

        logger.info(
            f"{self.exchange} {symbol}: Collected {len(data)} most recent data from {ms_to_dt(data[0][0])} to {ms_to_dt(data[-1][0])}")

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
        from_date = datetime.utcfromtimestamp(from_timestamp_ms / 1000)
        to_date = datetime.utcfromtimestamp(to_timestamp_ms / 1000)

        # Extend to end of day for 'to_date'
        to_date = to_date.replace(hour=23, minute=59, second=59)

        current = from_date
        while current < to_date:
            batch_end = min(current + timedelta(minutes=max_minutes), to_date)
            yield int(current.timestamp() * 1000), int(batch_end.timestamp() * 1000)
            current = batch_end

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
    #     from_date = datetime.utcfromtimestamp(from_timestamp_ms / 1000)
    #     to_date = datetime.utcfromtimestamp(to_timestamp_ms / 1000)
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
