"""Tests for tick_parquet_store.TickParquetStore.

Covers the HDF5 → Parquet flush round-trip plus the two data-loss fixes
from the 2026-06-01 data-collection hardening pass:

  - A transient HDF5 read failure does NOT advance the watermark (rows are
    retried on the next flush) until ``_MAX_READ_FAILURES`` is reached, at
    which point the poisoned range is skipped so the flusher makes forward
    progress.
  - A corrupt existing daily Parquet file is quarantined (not silently
    overwritten) so its bytes survive for offline recovery.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

import numpy as np

# tick_parquet_store lives in the project root, not in tests/.
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import tick_parquet_store as tps
from tick_parquet_store import TickParquetStore


def _write_h5_trades(path: str, symbol: str, rows: np.ndarray) -> None:
    """Create a synthetic HDF5 tick file with a ``{symbol}/trades`` dataset."""
    import h5py

    with h5py.File(path, "w") as f:
        grp = f.create_group(symbol)
        grp.create_dataset(
            "trades",
            data=rows.astype("float64"),
            maxshape=(None, rows.shape[1]),
            chunks=True,
        )


def _trade_row(ts_ms: int, price: float = 100.0, qty: float = 1.0,
               is_buyer_maker: float = 0.0):
    return [float(ts_ms), float(price), float(qty), float(is_buyer_maker)]


# Day 2021-01-01 00:00:00 UTC in ms.
_DAY0 = 1_609_459_200_000


class TestFlushRoundTrip(unittest.TestCase):

    def test_basic_flush_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5 = os.path.join(tmp, "BTCUSDT_ticks.h5")
            # More than the trades safety margin so something is flushed.
            n = tps._SAFETY_MARGIN["trades"] + 50
            rows = np.array(
                [_trade_row(_DAY0 + i * 1000, 100 + i) for i in range(n)],
                dtype="float64",
            )
            _write_h5_trades(h5, "BTCUSDT", rows)

            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            written = store.flush_from_h5(h5, "BTCUSDT", "binance")

            self.assertEqual(written["trades"], 50)
            df = store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 50)
            self.assertListEqual(
                list(df.columns),
                ["timestamp", "price", "quantity", "is_buyer_maker"],
            )

    def test_idempotent_reflush_no_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5 = os.path.join(tmp, "BTCUSDT_ticks.h5")
            n = tps._SAFETY_MARGIN["trades"] + 30
            rows = np.array(
                [_trade_row(_DAY0 + i * 1000) for i in range(n)],
                dtype="float64",
            )
            _write_h5_trades(h5, "BTCUSDT", rows)
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            store.flush_from_h5(h5, "BTCUSDT", "binance")
            # Reset the watermark to force a re-read of the same rows.
            store._watermarks.set("binance", "BTCUSDT", "trades", 0)
            store.flush_from_h5(h5, "BTCUSDT", "binance")
            df = store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 30)  # de-duplicated, not 60


class TestReadFailureWatermark(unittest.TestCase):
    """A transient read failure must not silently drop rows."""

    def _store_with_failing_dataset(self, tmp, fail_times):
        store = TickParquetStore(root=os.path.join(tmp, "ticks"))

        class _FailingDataset:
            shape = (tps._SAFETY_MARGIN["trades"] + 100, 4)

            def __init__(self):
                self.calls = 0

            def __getitem__(self, _slc):
                self.calls += 1
                if self.calls <= fail_times:
                    raise OSError("simulated HDF5 read failure")
                # Return a valid slice once failures are exhausted.
                start = _slc.start or 0
                stop = _slc.stop or 0
                k = max(0, stop - start)
                return np.array(
                    [_trade_row(_DAY0 + i * 1000) for i in range(k)],
                    dtype="float64",
                )

        return store, _FailingDataset()

    def test_transient_failure_does_not_advance_watermark(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, ds = self._store_with_failing_dataset(tmp, fail_times=1)
            n = store._flush_dataset(ds, "binance", "BTCUSDT", "trades")
            self.assertEqual(n, 0)
            # Watermark untouched — the range will be retried next flush.
            self.assertEqual(
                store._watermarks.get("binance", "BTCUSDT", "trades"), 0
            )
            # A subsequent attempt now succeeds (failure was transient).
            n2 = store._flush_dataset(ds, "binance", "BTCUSDT", "trades")
            self.assertGreater(n2, 0)
            self.assertGreater(
                store._watermarks.get("binance", "BTCUSDT", "trades"), 0
            )

    def test_persistent_failure_eventually_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, ds = self._store_with_failing_dataset(
                tmp, fail_times=tps._MAX_READ_FAILURES + 5
            )
            # First _MAX_READ_FAILURES-1 attempts: watermark stays at 0.
            for _ in range(tps._MAX_READ_FAILURES - 1):
                store._flush_dataset(ds, "binance", "BTCUSDT", "trades")
                self.assertEqual(
                    store._watermarks.get("binance", "BTCUSDT", "trades"), 0
                )
            # The threshold attempt skips the poisoned range (forward progress).
            store._flush_dataset(ds, "binance", "BTCUSDT", "trades")
            self.assertGreater(
                store._watermarks.get("binance", "BTCUSDT", "trades"), 0
            )


class TestCorruptParquetQuarantine(unittest.TestCase):

    def test_corrupt_daily_file_is_quarantined_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5 = os.path.join(tmp, "BTCUSDT_ticks.h5")
            n = tps._SAFETY_MARGIN["trades"] + 40
            rows = np.array(
                [_trade_row(_DAY0 + i * 1000) for i in range(n)],
                dtype="float64",
            )
            _write_h5_trades(h5, "BTCUSDT", rows)
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            store.flush_from_h5(h5, "BTCUSDT", "binance")

            # Corrupt the written daily Parquet file.
            day_dir = os.path.join(
                tmp, "ticks", "binance", "BTCUSDT", "trades"
            )
            day_files = [f for f in os.listdir(day_dir) if f.endswith(".parquet")]
            self.assertTrue(day_files)
            corrupt_path = os.path.join(day_dir, day_files[0])
            with open(corrupt_path, "wb") as fh:
                fh.write(b"not a parquet file")

            # Force a re-flush of the same rows into the corrupt day file.
            store._watermarks.set("binance", "BTCUSDT", "trades", 0)
            store.flush_from_h5(h5, "BTCUSDT", "binance")

            # The corrupt bytes must have been quarantined, not discarded.
            quarantined = [
                f for f in os.listdir(day_dir) if ".corrupt-" in f
            ]
            self.assertTrue(
                quarantined,
                "corrupt parquet should be quarantined to a .corrupt-* file",
            )
            # And the live file is now readable again with the fresh rows.
            df = store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 40)


if __name__ == "__main__":
    unittest.main()
