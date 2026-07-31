"""Tests for immutable Parquet shard writer (greenfield, no HDF5)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from market_data import EventKind, MarketEvent
from tick_parquet_store import (
    ImmutableShardWriter,
    TickParquetStore,
    evaluate_pipeline_health,
)

_DAY0 = 1_609_459_200_000  # 2021-01-01 UTC


class TestImmutableShardWriter(unittest.TestCase):

    def test_flush_writes_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = ImmutableShardWriter(tmp, "binance", "BTCUSDT", flush_interval_s=3600)
            w.add_trade(_DAY0 + 1000, 100.0, 0.5, False)
            w.add_trade(_DAY0 + 2000, 101.0, 0.25, True)
            n = w.flush()
            self.assertEqual(n, 2)
            parts = list(Path(tmp).rglob("part-*.parquet"))
            self.assertEqual(len(parts), 1)
            self.assertIn("date=2021-01-01", str(parts[0]))
            self.assertIn("hour=00", str(parts[0]))

    def test_crash_leaves_prior_shards(self):
        """Simulated crash: flush once, then lose buffer — prior shard intact."""
        with tempfile.TemporaryDirectory() as tmp:
            w = ImmutableShardWriter(tmp, "binance", "ETHUSDT")
            w.add_trade(_DAY0, 2000.0, 1.0, False)
            w.flush()
            parts_before = list(Path(tmp).rglob("part-*.parquet"))
            self.assertEqual(len(parts_before), 1)
            # Buffer new rows but never flush (crash).
            w.add_trade(_DAY0 + 5000, 2001.0, 1.0, False)
            # New writer instance (restart) must still see prior shard.
            w2 = ImmutableShardWriter(tmp, "binance", "ETHUSDT")
            mtimes, days = w2.latest_shard_info()
            self.assertIsNotNone(mtimes["trades"])
            self.assertEqual(days["trades"], "2021-01-01")
            store = TickParquetStore(tmp)
            df = store.read("binance", "ETHUSDT", "trades")
            self.assertEqual(len(df), 1)
            self.assertEqual(df.iloc[0]["price"], 2000.0)

    def test_reader_dedupes_overlapping_shards(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = ImmutableShardWriter(tmp, "binance", "BTCUSDT")
            w.add_trade(_DAY0, 100.0, 1.0, False)
            w.flush()
            w.add_trade(_DAY0, 100.0, 1.0, False)  # duplicate row
            w.flush()
            df = TickParquetStore(tmp).read("binance", "BTCUSDT", "trades")
            self.assertEqual(len(df), 1)

    def test_on_market_event_trade_and_depth(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = ImmutableShardWriter(tmp, "binance", "BTCUSDT")
            w.on_market_event(
                MarketEvent(
                    venue="binance",
                    symbol="BTCUSDT",
                    kind=EventKind.TRADE,
                    ts_exchange_ms=_DAY0,
                    ts_recv_ms=_DAY0,
                    payload={"price": 1.0, "qty": 2.0, "is_buyer_maker": False},
                )
            )
            w.on_market_event(
                MarketEvent(
                    venue="binance",
                    symbol="BTCUSDT",
                    kind=EventKind.DEPTH_SNAPSHOT,
                    ts_exchange_ms=_DAY0,
                    ts_recv_ms=_DAY0,
                    payload={"bids": [(1.0, 10.0)], "asks": [(1.1, 5.0)]},
                )
            )
            w.flush()
            store = TickParquetStore(tmp)
            self.assertEqual(len(store.read("binance", "BTCUSDT", "trades")), 1)
            depth = store.read("binance", "BTCUSDT", "depth_snapshots")
            self.assertEqual(len(depth), 2)

    def test_symbol_isolation_separate_roots(self):
        """Two writers for different symbols never share buffers/files."""
        with tempfile.TemporaryDirectory() as tmp:
            a = ImmutableShardWriter(tmp, "binance", "BTCUSDT")
            b = ImmutableShardWriter(tmp, "binance", "ETHUSDT")
            a.add_trade(_DAY0, 100.0, 1.0, False)
            b.add_trade(_DAY0, 2000.0, 1.0, False)
            a.flush()
            b.flush()
            btc = TickParquetStore(tmp).read("binance", "BTCUSDT", "trades")
            eth = TickParquetStore(tmp).read("binance", "ETHUSDT", "trades")
            self.assertEqual(len(btc), 1)
            self.assertEqual(len(eth), 1)
            self.assertEqual(btc.iloc[0]["price"], 100.0)
            self.assertEqual(eth.iloc[0]["price"], 2000.0)


class TestPipelineHealth(unittest.TestCase):

    def test_ok_when_fresh(self):
        now = datetime(2021, 1, 1, 12, 0, tzinfo=timezone.utc)
        report = evaluate_pipeline_health(
            "BTCUSDT",
            venue="binance",
            latest_shard_mtime={"trades": now.timestamp() - 30},
            latest_shard_day={"trades": "2021-01-01"},
            now=now,
            max_staleness_seconds=300,
            datasets=("trades",),
        )
        self.assertTrue(report.ok)

    def test_fails_when_stale_mtime(self):
        now = datetime(2021, 1, 1, 12, 0, tzinfo=timezone.utc)
        report = evaluate_pipeline_health(
            "BTCUSDT",
            venue="binance",
            latest_shard_mtime={"trades": now.timestamp() - 900},
            latest_shard_day={"trades": "2021-01-01"},
            now=now,
            max_staleness_seconds=300,
            datasets=("trades",),
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("stale" in i for i in report.issues))

    def test_fails_when_no_shards(self):
        now = datetime.now(timezone.utc)
        report = evaluate_pipeline_health(
            "ETHUSDT",
            venue="binance",
            latest_shard_mtime={"trades": None},
            latest_shard_day={"trades": None},
            now=now,
            datasets=("trades",),
        )
        self.assertFalse(report.ok)


class TestConcurrentFlush(unittest.TestCase):

    def test_flush_under_concurrent_adds(self):
        with tempfile.TemporaryDirectory() as tmp:
            w = ImmutableShardWriter(tmp, "binance", "BTCUSDT", max_buffer_rows=1_000_000)
            stop = threading.Event()

            def producer():
                i = 0
                while not stop.is_set():
                    w.add_trade(_DAY0 + i, 100.0 + i * 0.01, 0.1, False)
                    i += 1

            t = threading.Thread(target=producer, daemon=True)
            t.start()
            time.sleep(0.05)
            w.flush()
            stop.set()
            t.join(timeout=2)
            w.flush()
            df = TickParquetStore(tmp).read("binance", "BTCUSDT", "trades")
            self.assertGreater(len(df), 0)


if __name__ == "__main__":
    unittest.main()
