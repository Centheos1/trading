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

import collect_ohlcv as co
from ohlcv_store import LocalParquetStore, OHLCV_COLUMNS_6, OHLCV_COLUMNS_7


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


if __name__ == "__main__":
    unittest.main()
