"""Tests for greenfield collect_ticks.py (Parquet-only, no HDF5)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import collect_ticks as ct
from tick_parquet_store import ImmutableShardWriter


class TestArgParsing(unittest.TestCase):

    def test_symbol_required(self):
        with self.assertRaises(SystemExit):
            ct.main([])

    def test_parse_symbols_list(self):
        args = ct.build_parser().parse_args(
            ["--symbols", "btcusdt, ethusdt", "--venue", "binance"]
        )
        self.assertEqual(ct._parse_symbols(args), ["BTCUSDT", "ETHUSDT"])

    def test_health_check_flag(self):
        args = ct.build_parser().parse_args(
            ["--symbol", "BTCUSDT", "--health-check"]
        )
        self.assertTrue(args.health_check)


class TestHealthCheck(unittest.TestCase):

    def test_health_fails_without_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = ct.run_health_check(
                data_dir=tmp,
                venue="binance",
                symbol="BTCUSDT",
                max_staleness_seconds=300,
            )
            self.assertEqual(rc, 1)

    def test_health_ok_with_fresh_shard(self):
        with tempfile.TemporaryDirectory() as tmp:
            ticks = os.path.join(tmp, "ticks")
            w = ImmutableShardWriter(ticks, "binance", "BTCUSDT")
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            w.add_trade(now_ms, 100.0, 1.0, False)
            w.flush()
            rc = ct.run_health_check(
                data_dir=tmp,
                venue="binance",
                symbol="BTCUSDT",
                max_staleness_seconds=300,
            )
            self.assertEqual(rc, 0)


class TestIsolationSpawn(unittest.TestCase):

    def test_spawn_children_invokes_one_process_per_symbol(self):
        pops = []

        class FakeProc:
            def __init__(self, cmd):
                pops.append(cmd)

            def wait(self):
                return 0

            def send_signal(self, _sig):
                pass

        with patch("subprocess.Popen", side_effect=lambda cmd: FakeProc(cmd)):
            rc = ct._spawn_children(
                ["BTCUSDT", "ETHUSDT"],
                ["--venue", "binance", "--log-level", "INFO"],
            )
        self.assertEqual(rc, 0)
        self.assertEqual(len(pops), 2)
        self.assertIn("--symbol", pops[0])
        self.assertIn("BTCUSDT", pops[0])
        self.assertIn("ETHUSDT", pops[1])


class TestRunOneFlushOnSignal(unittest.TestCase):

    def test_run_one_stops_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            mock_adapter_run = MagicMock()

            def fake_collect(symbol, duration_seconds=0):
                # Simulate adapter writing via the collector's writer.
                pass

            with patch("collect_ticks.TickDataCollector") as MockCol:
                inst = MockCol.return_value
                inst.collect.side_effect = fake_collect
                rc = ct._run_one(
                    venue="binance",
                    symbol="BTCUSDT",
                    data_dir=tmp,
                    duration=0,
                    flush_interval=60,
                    futures=True,
                    redis_url=None,
                )
            self.assertEqual(rc, 0)
            MockCol.assert_called_once()
            kwargs = MockCol.call_args.kwargs
            self.assertEqual(kwargs["exchange"], "binance")
            self.assertIsNotNone(kwargs["shard_writer"])


if __name__ == "__main__":
    unittest.main()
