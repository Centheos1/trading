"""OHLCV adapter retries must raise after N failures (no infinite SSL loop)."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import collect_ohlcv as co


class TestBinanceBoundedRetry(unittest.TestCase):

    def test_raises_after_max_consecutive_failures(self):
        adapter = co.BinanceAdapter()
        adapter._rl = MagicMock()
        adapter._rl.acquire = MagicMock()

        resp = MagicMock()
        resp.status_code = 500
        resp.raise_for_status.side_effect = ConnectionError("SSL EOF")

        with patch.object(adapter, "_requests") as req:
            req.get.return_value = resp
            with patch.object(co.time, "sleep", return_value=None):
                with self.assertRaises(RuntimeError) as ctx:
                    adapter.fetch_candles(
                        "BTCUSDT",
                        "1m",
                        from_ts=1_600_000_000_000,
                        to_ts=1_600_000_060_000,
                    )
        self.assertIn("after", str(ctx.exception).lower())
        self.assertGreaterEqual(req.get.call_count, co._ADAPTER_MAX_RETRIES)

    def test_fetch_with_retries_returns_none_after_raise(self):
        adapter = MagicMock()
        adapter.name = "binance"
        adapter.fetch_candles.side_effect = RuntimeError("wedged SSL")

        with patch.object(co._shutdown, "wait", return_value=False):
            out = co._fetch_with_retries(
                adapter, "BTCUSDT", "1m", 1, 2
            )
        self.assertIsNone(out)
        self.assertEqual(adapter.fetch_candles.call_count, co._FETCH_MAX_ATTEMPTS)


class TestWorkersDefault(unittest.TestCase):

    def test_cli_workers_default_is_two(self):
        env = {k: v for k, v in os.environ.items() if k != "OHLCV_WORKERS"}
        with patch.dict(os.environ, env, clear=True):
            with patch("sys.argv", ["collect_ohlcv.py", "--exchange", "binance"]):
                args = co._parse_args()
        self.assertEqual(args.workers, 2)


if __name__ == "__main__":
    unittest.main()
