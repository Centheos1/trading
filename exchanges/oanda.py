import logging
from typing import Optional, List
import pandas as pd
from oandapyV20 import API
from oandapyV20.exceptions import V20Error
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.instruments as instruments
# from oandapyV20.definitions.instruments import CandlestickGranularity
import re
from datetime import datetime, timedelta, timezone
import time
import pytz
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger()


class OandaClient:

    def __init__(self):
        self.account_id = os.getenv("OANDA_ACCOUNT_ID")
        self.access_token = os.getenv("OANDA_ACCESS_TOKEN")
        self.account_type = os.getenv("OANDA_ACCOUNT_TYPE")
        self.client = API(access_token=self.access_token)

        # CandlestickGranularity().definitions.keys()
        self.granularities = ['S5', 'S10', 'S15', 'S30', 'M1', 'M2', 'M4', 'M5', 'M10', 'M15', 'M30', 'H1', 'H2', 'H3',
                              'H4', 'H6', 'H8', 'H12', 'D', 'W', 'M']
        self.price = ['M', 'B', 'A', 'BA', 'MBA']

        self.date_input_format = "%Y-%m-%d"
        self.date_output_format = "%Y-%m-%dT%H:%M:%SZ"

        self.symbols = list()
        self.symbol_details = dict()
        self._get_symbols()

    # def get_instruments(self):
    def _get_symbols(self):
        r = accounts.AccountInstruments(accountID=self.account_id)
        rv = self.client.request(r)
        for i in rv.get('instruments'):
            self.symbols.append(i.get('name'))
            self.symbol_details[i.get('name')] = i

    def get_historical_data(self, symbol: str, start_time: Optional[int] = None, end_time: Optional[int] = None,
                            price='MBA', granularity='M1'):

        if not symbol:
            raise ValueError("No symbol provided")

        if start_time is not None:
            from_date = datetime.fromtimestamp(int(start_time / 1000)).strftime(self.date_input_format)
        else:
            # from_date = (datetime.fromtimestamp(int(time.time())) - timedelta(days=1)).strftime(self.date_input_format)
            from_date = "2020-01-01"
        if end_time is not None:
            to_date = datetime.fromtimestamp(int(end_time / 1000)).strftime(self.date_input_format)
        else:
            to_date = datetime.fromtimestamp(int(time.time())).strftime(self.date_input_format)

        logger.info(f"Fetching {symbol} from {from_date} to {to_date}")

        max_retries = 2
        retries = 0
        complete = False

        candles = list()
        for start_str, end_str in self._generate_batches(from_date, to_date):

            logger.info(f"{symbol} from {start_str} to {end_str}")

            if not self._check_date(start_str):
                raise ValueError("Incorrect start_str date format: ", start_str)
            if not self._check_date(end_str):
                raise ValueError("Incorrect end_str date format: ", end_str)

            while not complete:
                if retries >= max_retries:
                    complete = True
                try:
                    r = instruments.InstrumentsCandles(instrument=symbol, params={
                        "from": start_str,
                        "to": end_str,
                        "granularity": granularity,
                        "price": price,
                        # "count": 50,
                        # "smooth": False,
                        # "includeFirst": False,
                        # "dailyAlignment": 0,
                        # "alignmentTimezone": "America/New_York",
                        # "weeklyAlignment": "Friday"
                    })
                    rv = self.client.request(r)
                    # time, open, high, low, close, volume, spread
                    for candle in rv.get('candles', []):
                        t = self._datetime_str_to_millis(candle.get('time'))
                        o = float(candle.get('mid').get('o'))
                        h = float(candle.get('mid').get('h'))
                        l = float(candle.get('mid').get('l'))
                        c = float(candle.get('mid').get('c'))
                        v = int(candle.get('volume'))
                        s = round(float(candle.get('ask').get('c')) - float(candle.get('bid').get('c')), 4)
                        candles.append((t, o, h, l, c, v, s,))
                    complete = True
                except V20Error as ex:
                    logger.error(f"V20Error: {ex}")
                    retries += 1
                    time.sleep(5)
                except Exception as ex:
                    logger.error(f"Exception: {ex}")
                    retries += 1
                    time.sleep(5)
        return candles

    @staticmethod
    def _check_date(s):
        dateFmt = "[\d]{4}-[\d]{2}-[\d]{2}T[\d]{2}:[\d]{2}:[\d]{2}Z"
        if not re.match(dateFmt, s):
            raise ValueError("Incorrect date format: ", s)
        return True

    def _generate_batches(self, from_date_str: str, to_date_str: str, max_minutes=5000):

        # Parse input dates and set time to start and end of day
        from_date = datetime.strptime(from_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        to_date = (datetime.strptime(to_date_str, "%Y-%m-%d") + timedelta(hours=23, minutes=59, seconds=59)).replace(
            tzinfo=timezone.utc)

        if to_date > datetime.now(timezone.utc):
            to_date = datetime.now(timezone.utc)

        current = from_date
        while current < to_date:
            batch_end = min(current + timedelta(minutes=max_minutes), to_date)
            yield current.strftime(self.date_output_format), batch_end.strftime(self.date_output_format)
            current = batch_end

    @staticmethod
    def _datetime_str_to_millis(dt_str):
        # Parse the string assuming ISO 8601 with Z (UTC)
        return int(pd.to_datetime(dt_str).timestamp() * 1000)
