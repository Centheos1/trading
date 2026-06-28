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
from datetime import datetime, timezone

import numpy as np

# tick_parquet_store lives in the project root, not in tests/.
_ROOT = os.path.dirname(os.path.dirname(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import tick_parquet_store as tps
from tick_parquet_store import TickParquetStore, evaluate_pipeline_health


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


class TestRotationResetsWatermark(unittest.TestCase):
    """A rotated/replaced HDF5 (row count below the watermark) must reset the
    watermark and re-flush, not freeze the mirror."""

    def test_watermark_reset_when_file_rotated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            # Stale pre-rotation watermark far beyond the fresh file's rows.
            store._watermarks.set("binance", "BTCUSDT", "trades", 45_000_000)

            n = tps._SAFETY_MARGIN["trades"] + 20  # fresh, small file

            class _FreshDataset:
                shape = (n, 4)

                def __getitem__(self, slc):
                    start = slc.start or 0
                    stop = slc.stop if slc.stop is not None else n
                    k = max(0, stop - start)
                    return np.array(
                        [_trade_row(_DAY0 + (start + i) * 1000) for i in range(k)],
                        dtype="float64",
                    )

            written = store._flush_dataset(
                _FreshDataset(), "binance", "BTCUSDT", "trades"
            )
            # Re-flushed from row 0 (would be 0 if the stale watermark stuck).
            self.assertEqual(written, 20)
            self.assertEqual(
                store._watermarks.get("binance", "BTCUSDT", "trades"),
                n - tps._SAFETY_MARGIN["trades"],
            )

    def test_growing_file_does_not_reset(self):
        """A normally growing file (watermark below row count) is untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            n = tps._SAFETY_MARGIN["trades"] + 100
            store._watermarks.set("binance", "BTCUSDT", "trades", 30)

            class _GrowingDataset:
                shape = (n, 4)

                def __getitem__(self, slc):
                    start = slc.start or 0
                    stop = slc.stop if slc.stop is not None else n
                    k = max(0, stop - start)
                    return np.array(
                        [_trade_row(_DAY0 + (start + i) * 1000) for i in range(k)],
                        dtype="float64",
                    )

            store._flush_dataset(_GrowingDataset(), "binance", "BTCUSDT", "trades")
            # Flushed only rows 30..(n-margin), i.e. resumed from the watermark.
            self.assertEqual(
                store._watermarks.get("binance", "BTCUSDT", "trades"),
                n - tps._SAFETY_MARGIN["trades"],
            )
            df = store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 70)  # 100 - 30, not re-read from 0


class TestChunkedFlush(unittest.TestCase):
    """A large unflushed range must drain in bounded chunks, never in one
    giant np.asarray() (the 2026-06 22.8 GiB MemoryError)."""

    def test_large_range_drains_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            margin = tps._SAFETY_MARGIN["trades"]
            unflushed = 50
            n = margin + unflushed
            max_width_seen = {"w": 0}

            class _BigDataset:
                shape = (n, 4)

                def __getitem__(self, slc):
                    start = slc.start or 0
                    stop = slc.stop if slc.stop is not None else n
                    k = max(0, stop - start)
                    max_width_seen["w"] = max(max_width_seen["w"], k)
                    return np.array(
                        [_trade_row(_DAY0 + (start + i) * 1000) for i in range(k)],
                        dtype="float64",
                    )

            original = tps._FLUSH_CHUNK_ROWS
            tps._FLUSH_CHUNK_ROWS = 7
            try:
                written = store._flush_dataset(
                    _BigDataset(), "binance", "BTCUSDT", "trades"
                )
            finally:
                tps._FLUSH_CHUNK_ROWS = original

            # All unflushed rows landed, watermark reached the end, and no single
            # read ever exceeded the chunk cap.
            self.assertEqual(written, unflushed)
            self.assertEqual(
                store._watermarks.get("binance", "BTCUSDT", "trades"), n - margin
            )
            self.assertLessEqual(max_width_seen["w"], 7)
            df = store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), unflushed)


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


class TestLatestParquetDate(unittest.TestCase):

    def test_none_when_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            self.assertIsNone(
                store.latest_parquet_date("binance", "BTCUSDT", "trades")
            )

    def test_returns_max_day_ignoring_junk(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = TickParquetStore(root=os.path.join(tmp, "ticks"))
            ddir = store._dataset_dir("binance", "BTCUSDT", "trades")
            ddir.mkdir(parents=True)
            for name in ("2026-06-01.parquet", "2026-06-03.parquet",
                         "2026-05-30.parquet", "not-a-date.parquet"):
                (ddir / name).write_bytes(b"")
            self.assertEqual(
                store.latest_parquet_date("binance", "BTCUSDT", "trades"),
                "2026-06-03",
            )


class TestEvaluatePipelineHealth(unittest.TestCase):
    """Pure health-evaluation logic — the 2026-06 freeze guardrail."""

    NOW = datetime(2026, 6, 17, 5, 0, tzinfo=timezone.utc)  # 5h into the UTC day

    def _eval(self, *, n_rows=None, watermarks=None, latest=None, now=None,
              max_staleness_hours=2.0):
        ds = tps.DATASETS
        return evaluate_pipeline_health(
            "BTCUSDT",
            exchange="binance",
            n_rows=n_rows if n_rows is not None else {d: 1000 for d in ds},
            watermarks=watermarks if watermarks is not None else {d: 990 for d in ds},
            latest_parquet_day=(
                latest if latest is not None else {d: "2026-06-17" for d in ds}
            ),
            now=now or self.NOW,
            max_staleness_hours=max_staleness_hours,
        )

    def test_healthy_today(self):
        report = self._eval()
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.issues, [])

    def test_stale_parquet_flags_issue(self):
        report = self._eval(
            latest={d: "2026-06-01" for d in tps.DATASETS}
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("stale" in i for i in report.issues))

    def test_stranded_watermark_flags_issue(self):
        # watermark above the current row count = rotation not yet healed.
        report = self._eval(
            n_rows={d: 100 for d in tps.DATASETS},
            watermarks={d: 5000 for d in tps.DATASETS},
        )
        self.assertFalse(report.ok)
        self.assertTrue(any("exceeds HDF5 row count" in i for i in report.issues))

    def test_missing_hdf5_dataset_flags_issue(self):
        report = self._eval(n_rows={d: None for d in tps.DATASETS})
        self.assertFalse(report.ok)
        self.assertTrue(any("HDF5 dataset absent" in i for i in report.issues))

    def test_no_parquet_flags_issue(self):
        report = self._eval(latest={d: None for d in tps.DATASETS})
        self.assertFalse(report.ok)
        self.assertTrue(any("never flushed" in i for i in report.issues))

    def test_yesterday_within_grace_is_ok(self):
        # 1h into the UTC day, yesterday's file, 2h grace → still fresh.
        now = datetime(2026, 6, 17, 1, 0, tzinfo=timezone.utc)
        report = self._eval(
            latest={d: "2026-06-16" for d in tps.DATASETS},
            now=now,
        )
        self.assertTrue(report.ok, report.issues)

    def test_yesterday_past_grace_is_stale(self):
        # 5h into the UTC day, yesterday's file, 2h grace → stale.
        report = self._eval(latest={d: "2026-06-16" for d in tps.DATASETS})
        self.assertFalse(report.ok)
        self.assertTrue(any("stale" in i for i in report.issues))


class TestLiveParquetMirror(unittest.TestCase):
    """The decoupled mirror writes Parquet straight from the feed — no HDF5
    on the durability path (the fix for the recurring 2026-06 freezes). The
    writer is append-only with an in-memory per-day accumulator (bounded
    per-flush cost); the reader sorts + de-duplicates."""

    def test_add_trade_then_flush_writes_parquet(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(
                root=os.path.join(tmp, "ticks"), exchange="binance"
            )
            for i in range(5):
                mirror.add_trade(
                    "btcusdt", _DAY0 + i * 1000, 100.0 + i, 1.5, i % 2 == 0
                )
            written = mirror.flush()

            self.assertEqual(written.get("BTCUSDT.trades.2021-01-01"), 5)
            df = mirror._store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 5)
            self.assertListEqual(
                list(df.columns),
                ["timestamp", "price", "quantity", "is_buyer_maker"],
            )
            self.assertEqual(df["timestamp"].dtype, np.dtype("int64"))
            self.assertEqual(df["is_buyer_maker"].dtype, np.dtype("bool"))

    def test_add_depth_expands_bids_and_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            mirror.add_depth(
                "BTCUSDT", _DAY0,
                bids=[["100.0", "2.0"], ["99.5", "1.0"]],
                asks=[["100.5", "3.0"]],
                is_snapshot=False,
            )
            written = mirror.flush()

            self.assertEqual(written.get("BTCUSDT.depth_updates.2021-01-01"), 3)
            df = mirror._store.read(
                "BTCUSDT", "depth_updates", exchange="binance"
            )
            self.assertEqual(len(df), 3)
            self.assertEqual(set(df["side"]), {0, 1})
            self.assertEqual(int((df["side"] == 0).sum()), 2)  # two bids
            self.assertEqual(int((df["side"] == 1).sum()), 1)  # one ask

    def test_incremental_flushes_accumulate_into_one_day_file(self):
        # Successive flushes must extend the same day file, not overwrite it
        # with only the latest batch (the truncation foot-gun of an
        # overwrite-from-memory writer).
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            for i in range(3):
                mirror.add_trade("BTCUSDT", _DAY0 + i * 1000, 100.0 + i, 1.0, True)
            mirror.flush()
            for i in range(3, 7):
                mirror.add_trade("BTCUSDT", _DAY0 + i * 1000, 100.0 + i, 1.0, True)
            mirror.flush()

            df = mirror._store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 7)
            self.assertEqual(len(mirror._store.list_days("BTCUSDT", "trades")), 1)

    def test_read_dedups_exact_duplicates(self):
        # The append-only writer may persist a duplicate (e.g. a WS reconnect
        # redelivery); the read boundary must collapse exact duplicates.
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            for _ in range(2):
                mirror.add_trade("BTCUSDT", _DAY0, 100.0, 1.0, True)
            mirror.flush()
            df = mirror._store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 1)

    def test_restart_reloads_day_and_does_not_truncate(self):
        # A fresh mirror (simulating a process restart) must reload the
        # existing day file before overwriting, so a flush of new rows appends
        # rather than wiping the morning's data.
        root = None
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "ticks")
            m1 = tps.LiveParquetMirror(root=root)
            for i in range(4):
                m1.add_trade("BTCUSDT", _DAY0 + i * 1000, 100.0, 1.0, True)
            m1.flush()

            # New instance, same root — the previous day's rows are on disk.
            m2 = tps.LiveParquetMirror(root=root)
            for i in range(4, 6):
                m2.add_trade("BTCUSDT", _DAY0 + i * 1000, 101.0, 1.0, True)
            m2.flush()

            df = m2._store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 6)

    def test_empty_flush_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            self.assertEqual(mirror.flush(), {})

    def test_rows_span_two_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            mirror.add_trade("BTCUSDT", _DAY0, 100.0, 1.0, True)
            mirror.add_trade("BTCUSDT", _DAY0 + 86_400_000, 101.0, 1.0, False)
            mirror.flush()
            days = mirror._store.list_days("BTCUSDT", "trades")
            self.assertEqual(len(days), 2)
            self.assertEqual(mirror.latest_parquet_date("BTCUSDT"), max(days))

    def test_past_day_accumulator_is_evicted(self):
        # After the UTC day rolls over, the prior day's accumulator must be
        # dropped from memory (bounded footprint), but its file stays intact.
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            mirror.add_trade("BTCUSDT", _DAY0, 100.0, 1.0, True)         # day 0
            mirror.flush()
            mirror.add_trade("BTCUSDT", _DAY0 + 86_400_000, 101.0, 1.0, True)  # day 1
            mirror.flush()

            keys = [k for k in mirror._acc if k[1] == "trades"]
            self.assertEqual(keys, [("BTCUSDT", "trades", "2021-01-02")])
            # Day 0's file survives eviction.
            df = mirror._store.read(
                "BTCUSDT", "trades", exchange="binance",
                from_date="2021-01-01", to_date="2021-01-01",
            )
            self.assertEqual(len(df), 1)

    def test_failed_write_keeps_rows_and_retries(self):
        # A transient write failure must NOT drop rows: they stay in the
        # accumulator and land on the next successful flush.
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(root=os.path.join(tmp, "ticks"))
            for i in range(4):
                mirror.add_trade("BTCUSDT", _DAY0 + i * 1000, 100.0, 1.0, True)

            calls = {"n": 0}
            real_write = mirror._store.write_day_table

            def flaky(exchange, symbol, dataset, day, table):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("disk hiccup")
                return real_write(exchange, symbol, dataset, day, table)

            mirror._store.write_day_table = flaky  # type: ignore[assignment]
            self.assertEqual(mirror.flush(), {})        # write failed
            mirror.flush()                              # retried (no new rows)
            df = mirror._store.read("BTCUSDT", "trades", exchange="binance")
            self.assertEqual(len(df), 4)

    def test_buffer_cap_drops_oldest(self):
        with tempfile.TemporaryDirectory() as tmp:
            mirror = tps.LiveParquetMirror(
                root=os.path.join(tmp, "ticks"), max_buffer_rows=10
            )
            for i in range(25):
                mirror.add_trade("BTCUSDT", _DAY0 + i * 1000, 100.0, 1.0, True)
            written = mirror.flush()
            # Only the cap survives; oldest were dropped to bound memory.
            self.assertEqual(written.get("BTCUSDT.trades.2021-01-01"), 10)


if __name__ == "__main__":
    unittest.main()
