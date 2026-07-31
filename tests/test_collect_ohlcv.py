"""Tests for collect_ohlcv.py.

Covers:
  - _check_storage_safety() abort logic
  - OandaAdapter candle tuple schema (7-tuple with spread)
  - OandaAdapter rows survive the full store.append() / store.read() round-trip
  - _build_adapters() credential-missing path emits ERROR and returns []
  - _build_adapters() with valid credentials succeeds
  - Oanda rows written by OhlcvStore are readable with the correct schema
  - Duplicate-timestamp idempotency in OhlcvStore.append()
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------

# collect_ohlcv lives in the project root, not in tests/.
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd

import collect_ohlcv as co
from ohlcv_store import LocalParquetStore, OHLCV_COLUMNS_6, OHLCV_COLUMNS_7

# Year boundaries (UTC, ms) used by the yearly-partition tests.
_TS_2021 = 1_609_459_200_000   # 2021-01-01 00:00:00Z
_TS_2022 = 1_640_995_200_000   # 2022-01-01 00:00:00Z
_TS_2023 = 1_672_531_200_000   # 2023-01-01 00:00:00Z


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oanda_candle(ts_ms: int, o=1.1, h=1.2, lo=1.0, cl=1.15, vol=100, spread=0.00010):
    """Return a 7-tuple Oanda candle."""
    return (ts_ms, o, h, lo, cl, float(vol), spread)


# ---------------------------------------------------------------------------
# _check_storage_safety
# ---------------------------------------------------------------------------

class TestCheckStorageSafety(unittest.TestCase):

    def _args(self, data_store=None, all_symbols=True, symbols=None, s3_bucket=None):
        return SimpleNamespace(
            data_store=data_store,
            all_symbols=all_symbols,
            symbols=symbols,
            s3_bucket=s3_bucket,
        )

    def test_local_parquet_all_symbols_with_bucket_aborts(self):
        """LOCAL + --all-symbols + S3_BUCKET configured must abort."""
        args = self._args(data_store="local_parquet", all_symbols=True, s3_bucket="my-bucket")
        with self.assertRaises(SystemExit) as cm:
            co._check_storage_safety(args)
        self.assertEqual(cm.exception.code, 1)

    def test_local_parquet_all_symbols_no_bucket_also_aborts(self):
        """LOCAL + --all-symbols with no bucket is equally dangerous."""
        args = self._args(data_store="local_parquet", all_symbols=True, s3_bucket=None)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                co._check_storage_safety(args)

    def test_s3_all_symbols_allowed(self):
        """S3 backend with --all-symbols is fine — no abort."""
        args = self._args(data_store="s3", all_symbols=True, s3_bucket="my-bucket")
        co._check_storage_safety(args)  # must not raise

    def test_local_parquet_specific_symbols_allowed(self):
        """Local storage is fine when a specific (small) symbol list is given."""
        args = self._args(
            data_store="local_parquet",
            all_symbols=False,
            symbols="BTCUSDT,ETHUSDT",
        )
        co._check_storage_safety(args)  # must not raise

    def test_env_var_backend_respected(self):
        """DATA_STORE env var is read when args.data_store is None."""
        args = self._args(data_store=None, all_symbols=True, s3_bucket="b")
        with patch.dict(os.environ, {"DATA_STORE": "local_parquet"}, clear=False):
            with self.assertRaises(SystemExit):
                co._check_storage_safety(args)


# ---------------------------------------------------------------------------
# OandaAdapter candle schema via OhlcvStore
# ---------------------------------------------------------------------------

class TestOandaCandleSchema(unittest.TestCase):
    """Oanda 7-tuple candles must survive LocalParquetStore append/read."""

    def test_seven_tuple_columns_present(self):
        rows = [
            _oanda_candle(1_700_000_000_000, spread=0.00012),
            _oanda_candle(1_700_000_060_000, spread=0.00015),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalParquetStore(root=tmpdir)
            n = store.append("oanda", "EUR_USD", rows, "1m")
            self.assertEqual(n, 2)
            df = store.read("oanda", "EUR_USD", "1m")
        self.assertFalse(df.empty)
        for col in ["open", "high", "low", "close", "volume", "spread"]:
            self.assertIn(col, df.columns, f"Column '{col}' missing from Oanda Parquet")

    def test_spread_values_are_correct(self):
        rows = [_oanda_candle(1_700_000_000_000, spread=0.00012)]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalParquetStore(root=tmpdir)
            store.append("oanda", "GBP_USD", rows, "1m")
            df = store.read("oanda", "GBP_USD", "1m")
        self.assertAlmostEqual(df["spread"].iloc[0], 0.00012, places=6)

    def test_binance_six_tuple_has_no_spread_column(self):
        """Binance rows (6-tuple) must NOT add a spread column."""
        rows = [(1_700_000_000_000, 30000.0, 30100.0, 29900.0, 30050.0, 12345.0)]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalParquetStore(root=tmpdir)
            store.append("binance", "BTCUSDT", rows, "1m")
            df = store.read("binance", "BTCUSDT", "1m")
        self.assertNotIn("spread", df.columns)

    def test_oanda_rows_idempotent_on_duplicate_timestamps(self):
        """Re-appending the same candles must not create duplicates."""
        rows = [_oanda_candle(1_700_000_000_000 + i * 60_000) for i in range(5)]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalParquetStore(root=tmpdir)
            store.append("oanda", "USD_JPY", rows, "1m")
            n2 = store.append("oanda", "USD_JPY", rows, "1m")
            df = store.read("oanda", "USD_JPY", "1m")
        self.assertEqual(len(df), 5, "Duplicate rows must be de-duplicated")
        self.assertEqual(n2, 0, "Re-append of existing rows must return 0 new rows")

    def test_get_last_timestamp_returns_max(self):
        ts_a = 1_700_000_000_000
        ts_b = 1_700_000_060_000
        rows = [_oanda_candle(ts_a), _oanda_candle(ts_b)]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalParquetStore(root=tmpdir)
            store.append("oanda", "AUD_USD", rows, "1m")
            last = store.get_last_timestamp("oanda", "AUD_USD", "1m")
        self.assertEqual(last, ts_b)


# ---------------------------------------------------------------------------
# _build_adapters: credential-missing path
# ---------------------------------------------------------------------------

class TestBuildAdapters(unittest.TestCase):

    def test_missing_credentials_returns_empty_list(self):
        """If Oanda credentials are absent, _build_adapters returns [] (no Binance)."""
        with patch.dict(os.environ, {
            "OANDA_ACCOUNT_ID": "",
            "OANDA_ACCESS_TOKEN": "",
        }, clear=False):
            adapters = co._build_adapters("oanda")
        self.assertEqual(adapters, [])

    def test_missing_credentials_logs_error(self):
        with patch.dict(os.environ, {
            "OANDA_ACCOUNT_ID": "",
            "OANDA_ACCESS_TOKEN": "",
        }, clear=False):
            with self.assertLogs("collect_ohlcv", level="ERROR") as log:
                co._build_adapters("oanda")
        self.assertTrue(
            any("credentials" in m.lower() for m in log.output),
            "Expected an ERROR mentioning credentials",
        )

    def test_binance_only_exchange_succeeds(self):
        """Binance adapter has no auth — must always be constructed."""
        with patch.object(co, "BinanceAdapter") as mock_ba:
            mock_ba.return_value = MagicMock(name="binance")
            adapters = co._build_adapters("binance")
        self.assertEqual(len(adapters), 1)

    def test_oanda_with_valid_credentials_added(self):
        """With credentials mocked, OandaAdapter is appended."""
        mock_adapter = MagicMock(name="oanda_adapter")
        with patch.object(co, "OandaAdapter", return_value=mock_adapter):
            adapters = co._build_adapters("oanda")
        self.assertIn(mock_adapter, adapters)

    def test_all_exchange_includes_both_when_creds_ok(self):
        mock_oanda = MagicMock(name="oanda_adapter")
        mock_binance = MagicMock(name="binance_adapter")
        with patch.object(co, "BinanceAdapter", return_value=mock_binance):
            with patch.object(co, "OandaAdapter", return_value=mock_oanda):
                adapters = co._build_adapters("all")
        self.assertIn(mock_binance, adapters)
        self.assertIn(mock_oanda, adapters)


# ---------------------------------------------------------------------------
# OandaClient environment routing
# ---------------------------------------------------------------------------

class TestOandaClientEnvironment(unittest.TestCase):
    """OandaClient must route to the correct API host.

    We patch ``exchanges.oanda.API`` (the name already imported into the
    module) and stub ``_get_symbols`` to avoid real network calls.
    """

    def _make_client(self, account_type: str):
        env = {
            "OANDA_ACCOUNT_ID": "123-456-789",
            "OANDA_ACCESS_TOKEN": "tok-abc",
            "OANDA_ACCOUNT_TYPE": account_type,
        }
        import exchanges.oanda as oanda_mod
        with patch.dict(os.environ, env, clear=False):
            with patch("exchanges.oanda.API") as mock_api_cls:
                mock_http_client = MagicMock()
                mock_http_client.request.return_value = {"instruments": []}
                mock_api_cls.return_value = mock_http_client
                with patch.object(oanda_mod.OandaClient, "_get_symbols",
                                  return_value=None):
                    client = oanda_mod.OandaClient()
                return mock_api_cls, client

    def test_practice_account_uses_practice_environment(self):
        mock_api_cls, _ = self._make_client("practice")
        _, kwargs = mock_api_cls.call_args
        self.assertEqual(kwargs.get("environment"), "practice")

    def test_live_account_uses_live_environment(self):
        mock_api_cls, _ = self._make_client("live")
        _, kwargs = mock_api_cls.call_args
        self.assertEqual(kwargs.get("environment"), "live")

    def test_missing_account_id_raises(self):
        env = {
            "OANDA_ACCOUNT_ID": "",
            "OANDA_ACCESS_TOKEN": "tok",
            "OANDA_ACCOUNT_TYPE": "practice",
        }
        import exchanges.oanda as oanda_mod
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(EnvironmentError):
                oanda_mod.OandaClient()

    def test_missing_access_token_raises(self):
        env = {
            "OANDA_ACCOUNT_ID": "123",
            "OANDA_ACCESS_TOKEN": "",
            "OANDA_ACCOUNT_TYPE": "practice",
        }
        import exchanges.oanda as oanda_mod
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(EnvironmentError):
                oanda_mod.OandaClient()


# ---------------------------------------------------------------------------
# _collect_one fetch retry (no silent gaps)
# ---------------------------------------------------------------------------

class _FlakyAdapter:
    """Adapter whose fetch_candles fails ``fail_times`` then succeeds."""

    name = "binance"

    def __init__(self, fail_times: int, rows):
        self._fail_times = fail_times
        self._rows = rows
        self.calls = 0

    def list_symbols(self):
        return ["BTCUSDT"]

    def fetch_candles(self, symbol, timeframe, from_ts, to_ts):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError("simulated transient API error")
        return self._rows


class TestCollectOneRetry(unittest.TestCase):
    """A transient fetch error must be retried, not turned into a silent gap."""

    def setUp(self):
        # Make backoff instant so the test is fast.
        self._orig_backoff = co._FETCH_BACKOFF_BASE_S
        co._FETCH_BACKOFF_BASE_S = 0.0
        co._shutdown.clear()

    def tearDown(self):
        co._FETCH_BACKOFF_BASE_S = self._orig_backoff

    def test_transient_failure_is_retried_then_succeeds(self):
        rows = [(_DAY := 1_609_459_200_000, 1.0, 2.0, 0.5, 1.5, 10.0)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            adapter = _FlakyAdapter(fail_times=co._FETCH_MAX_ATTEMPTS - 1, rows=rows)
            n = co._collect_one(
                adapter, store, "BTCUSDT", "1m",
                default_from_ts=_DAY, to_ts=_DAY + 60_000,
            )
            self.assertEqual(n, 1)
            self.assertEqual(adapter.calls, co._FETCH_MAX_ATTEMPTS)

    def test_persistent_failure_returns_zero_after_exhausting_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            adapter = _FlakyAdapter(fail_times=99, rows=[])
            _DAY = 1_609_459_200_000
            n = co._collect_one(
                adapter, store, "BTCUSDT", "1m",
                default_from_ts=_DAY, to_ts=_DAY + 60_000,
            )
            self.assertEqual(n, 0)
            self.assertEqual(adapter.calls, co._FETCH_MAX_ATTEMPTS)


# ---------------------------------------------------------------------------
# Yearly-partitioned store layout
# ---------------------------------------------------------------------------

def _binance_candle(ts_ms: int, c=100.0):
    return (ts_ms, c, c + 1, c - 1, c + 0.5, 10.0)


class TestYearlyPartitionedStore(unittest.TestCase):
    """LocalParquetStore must split/read/resume across yearly files only."""

    def _year_file(self, root, exchange, symbol, year, tf="1m"):
        return os.path.join(root, "ohlcv", exchange, symbol, tf, f"{year}.parquet")

    def _legacy_file(self, root, exchange, symbol, tf="1m"):
        return os.path.join(root, "ohlcv", exchange, symbol, f"{tf}.parquet")

    def test_append_splits_rows_into_year_files(self):
        rows = [
            _binance_candle(_TS_2021),
            _binance_candle(_TS_2021 + 60_000),
            _binance_candle(_TS_2022),
            _binance_candle(_TS_2023),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            n = store.append("binance", "BTCUSDT", rows, "1m")
            self.assertEqual(n, 4)
            for year in (2021, 2022, 2023):
                self.assertTrue(
                    os.path.isfile(self._year_file(tmp, "binance", "BTCUSDT", year)),
                    f"missing {year}.parquet",
                )
            # No legacy single-file should be created by the new writer.
            self.assertFalse(
                os.path.isfile(self._legacy_file(tmp, "binance", "BTCUSDT"))
            )

    def test_read_concats_across_years(self):
        rows = [
            _binance_candle(_TS_2021),
            _binance_candle(_TS_2022),
            _binance_candle(_TS_2023),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", rows, "1m")
            df = store.read("binance", "BTCUSDT", "1m")
        self.assertEqual(len(df), 3)
        self.assertTrue(df.index.is_monotonic_increasing)

    def test_read_range_filters_by_year(self):
        rows = [
            _binance_candle(_TS_2021),
            _binance_candle(_TS_2022),
            _binance_candle(_TS_2023),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", rows, "1m")
            df = store.read("binance", "BTCUSDT", "1m", from_ts=_TS_2022)
        self.assertEqual(len(df), 2)

    def test_get_last_timestamp_across_years(self):
        rows = [
            _binance_candle(_TS_2021),
            _binance_candle(_TS_2023 + 120_000),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", rows, "1m")
            last = store.get_last_timestamp("binance", "BTCUSDT", "1m")
        self.assertEqual(last, _TS_2023 + 120_000)

    def test_append_to_existing_year_is_idempotent(self):
        rows = [_binance_candle(_TS_2022 + i * 60_000) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", rows, "1m")
            n2 = store.append("binance", "BTCUSDT", rows, "1m")
            df = store.read("binance", "BTCUSDT", "1m")
        self.assertEqual(n2, 0)
        self.assertEqual(len(df), 3)

    def test_list_symbols_finds_yearly_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", [_binance_candle(_TS_2022)], "1m")
            store.append("binance", "ETHUSDT", [_binance_candle(_TS_2022)], "1m")
            symbols = store.list_symbols("binance", "1m")
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT"])

    # -- legacy single-file ignored (greenfield) ----------------------------
    def _write_legacy(self, root, exchange, symbol, tss, tf="1m"):
        path = self._legacy_file(root, exchange, symbol, tf)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pd.DataFrame(
            [_binance_candle(t) for t in tss], columns=OHLCV_COLUMNS_6
        ).to_parquet(path, index=False)
        return path

    def test_legacy_file_is_ignored(self):
        """Greenfield: single-file layout is not read or listed."""
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            self._write_legacy(tmp, "binance", "LEG", [_TS_2021, _TS_2021 + 60_000])
            df = store.read("binance", "LEG", "1m")
            last = store.get_last_timestamp("binance", "LEG", "1m")
            symbols = store.list_symbols("binance", "1m")
        self.assertEqual(len(df), 0)
        self.assertIsNone(last)
        self.assertNotIn("LEG", symbols)

    def test_legacy_does_not_affect_yearly_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            self._write_legacy(tmp, "binance", "BTCUSDT", [_TS_2021, _TS_2022])
            last = store.get_last_timestamp("binance", "BTCUSDT", "1m")
            self.assertIsNone(last)
            store.append("binance", "BTCUSDT", [_binance_candle(_TS_2023)], "1m")
            df = store.read("binance", "BTCUSDT", "1m")
            self.assertTrue(
                os.path.isfile(self._year_file(tmp, "binance", "BTCUSDT", 2023))
            )
        self.assertEqual(len(df), 1)

    # -- corrupt-file quarantine --------------------------------------------
    def test_corrupt_year_file_is_quarantined_on_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            bad = self._year_file(tmp, "binance", "BTCUSDT", 2022)
            os.makedirs(os.path.dirname(bad), exist_ok=True)
            with open(bad, "wb") as f:
                f.write(b"this is definitely not a parquet file")

            n = store.append("binance", "BTCUSDT", [_binance_candle(_TS_2022)], "1m")
            self.assertEqual(n, 1)
            # A quarantine copy exists and the rewritten file is readable.
            quarantined = [
                e for e in os.listdir(os.path.dirname(bad))
                if ".corrupt-" in e
            ]
            self.assertTrue(quarantined, "corrupt file should be quarantined")
            df = store.read("binance", "BTCUSDT", "1m")
        self.assertEqual(len(df), 1)

    def test_corrupt_year_file_is_skipped_on_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            store.append("binance", "BTCUSDT", [_binance_candle(_TS_2021)], "1m")
            bad = self._year_file(tmp, "binance", "BTCUSDT", 2022)
            with open(bad, "wb") as f:
                f.write(b"corrupt")
            df = store.read("binance", "BTCUSDT", "1m")
            # 2022 quarantined, 2021 still returned.
            self.assertFalse(os.path.isfile(bad))
        self.assertEqual(len(df), 1)


# ---------------------------------------------------------------------------
# Year-by-year incremental backfill
# ---------------------------------------------------------------------------

class _YearRecordingAdapter:
    """Adapter that records each fetch window and returns one candle per call
    at the window start."""

    name = "binance"

    def __init__(self):
        self.windows = []

    def list_symbols(self):
        return ["BTCUSDT"]

    def fetch_candles(self, symbol, timeframe, from_ts, to_ts):
        self.windows.append((from_ts, to_ts))
        return [_binance_candle(from_ts)]


class TestCollectOneYearByYear(unittest.TestCase):

    def setUp(self):
        co._shutdown.clear()

    def test_backfill_iterates_one_window_per_year(self):
        adapter = _YearRecordingAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            n = co._collect_one(
                adapter, store, "BTCUSDT", "1m",
                default_from_ts=_TS_2021, to_ts=_TS_2023 + 60_000,
            )
        # 2021, 2022, 2023 → three windows, three candles persisted.
        self.assertEqual(len(adapter.windows), 3)
        self.assertEqual(n, 3)
        # Each window stays within a single calendar year.
        for w_from, w_to in adapter.windows:
            self.assertEqual(
                co._year_of_ms(w_from), co._year_of_ms(w_to),
                "fetch window must not straddle a year boundary",
            )

    def test_resume_skips_already_collected_years(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalParquetStore(root=tmp)
            # Pretend 2021 + 2022 already collected.
            store.append("binance", "BTCUSDT", [_binance_candle(_TS_2022)], "1m")
            adapter = _YearRecordingAdapter()
            co._collect_one(
                adapter, store, "BTCUSDT", "1m",
                default_from_ts=_TS_2021, to_ts=_TS_2023 + 60_000,
            )
        # Resume point is just after _TS_2022, so only 2022 (remainder) + 2023.
        years = sorted({co._year_of_ms(w[0]) for w in adapter.windows})
        self.assertEqual(years, [2022, 2023])


if __name__ == "__main__":
    unittest.main()
