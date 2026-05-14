"""Tests for tools/feed_health.py — Feed Health Monitor & Data Quality Report."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import numpy as np
import pandas as pd

# Ensure project root on sys.path
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.feed_health import (
    OhlcvGap,
    OhlcvSymbolHealth,
    TickSymbolHealth,
    _detect_gaps,
    _fmt_bytes,
    _fmt_rows,
    _is_oanda_daily_close,
    _is_oanda_weekend_gap,
    _ms_to_str,
    collect_ohlcv_report,
    collect_tick_report,
    print_ohlcv_report,
    print_tick_report,
    to_json_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_ms(dt_str: str) -> int:
    """Parse 'YYYY-MM-DD HH:MM' (UTC) → epoch ms."""
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _build_tick_h5(path: str, symbol: str = "BTCUSDT") -> None:
    """Write a minimal tick HDF5 file that matches the TickStore layout."""
    import h5py

    trades = np.array([
        [_ts_ms("2026-05-13 12:00"), 65000.0, 0.1, 0.0],
        [_ts_ms("2026-05-13 12:01"), 65001.0, 0.2, 0.0],
        [_ts_ms("2026-05-13 13:00"), 65100.0, 0.5, 1.0],
    ], dtype=np.float64)

    with h5py.File(path, "w") as f:
        grp = f.create_group(symbol)
        grp.create_dataset("trades", data=trades)
        grp.create_dataset("depth_updates", data=np.zeros((10, 4), dtype=np.float64))
        grp.create_dataset("depth_snapshots", data=np.zeros((5, 4), dtype=np.float64))


def _build_ohlcv_parquet(directory: str, exchange: str, symbol: str, timestamps_ms: list) -> str:
    """Write a minimal OHLCV Parquet file for testing."""
    sym_dir = os.path.join(directory, "ohlcv", exchange, symbol)
    os.makedirs(sym_dir, exist_ok=True)
    path = os.path.join(sym_dir, "1m.parquet")
    df = pd.DataFrame({
        "timestamp": timestamps_ms,
        "open": [100.0] * len(timestamps_ms),
        "high": [101.0] * len(timestamps_ms),
        "low": [99.0] * len(timestamps_ms),
        "close": [100.5] * len(timestamps_ms),
        "volume": [10.0] * len(timestamps_ms),
    })
    df.to_parquet(path, index=False)
    return path


def _continuous_ts(start_str: str, count: int) -> list:
    start = _ts_ms(start_str)
    return [start + i * 60_000 for i in range(count)]


# ---------------------------------------------------------------------------
# Tests — helpers
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):
    def test_ms_to_str_format(self):
        ts = _ts_ms("2026-05-13 12:30")
        self.assertEqual(_ms_to_str(ts), "2026-05-13 12:30")

    def test_fmt_bytes(self):
        self.assertEqual(_fmt_bytes(512), "512.0 B")
        self.assertEqual(_fmt_bytes(1024), "1.0 KB")
        self.assertEqual(_fmt_bytes(1024 * 1024), "1.0 MB")

    def test_fmt_rows(self):
        self.assertEqual(_fmt_rows(500), "500")
        self.assertEqual(_fmt_rows(1500), "1.5K")
        self.assertIn("2.50M", _fmt_rows(2_500_000))

    def test_oanda_weekend_gap_saturday(self):
        # Gap starting Friday night, spanning Saturday
        after_ms = _ts_ms("2026-05-15 23:55")
        gap_ms = 3 * 3600 * 1000
        self.assertTrue(_is_oanda_weekend_gap(after_ms, gap_ms))

    def test_oanda_weekend_gap_weekday(self):
        after_ms = _ts_ms("2026-05-11 12:00")
        gap_ms = 60 * 60 * 1000
        self.assertFalse(_is_oanda_weekend_gap(after_ms, gap_ms))

    def test_oanda_daily_close_within_window(self):
        after_ms = _ts_ms("2026-05-13 22:00")
        gap_ms = 10 * 60_000
        self.assertTrue(_is_oanda_daily_close(after_ms, gap_ms))

    def test_oanda_daily_close_outside_window(self):
        after_ms = _ts_ms("2026-05-13 22:00")
        gap_ms = 30 * 60_000
        self.assertFalse(_is_oanda_daily_close(after_ms, gap_ms))


# ---------------------------------------------------------------------------
# Tests — gap detection
# ---------------------------------------------------------------------------

class TestDetectGaps(unittest.TestCase):
    _TF_MS = 60_000

    def test_no_gaps_continuous(self):
        start = _ts_ms("2026-05-13 12:00")
        ts = [start + i * 60_000 for i in range(10)]
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=5 * 60_000, exchange="binance")
        self.assertEqual(gaps, [])

    def test_single_gap_flagged(self):
        start = _ts_ms("2026-05-13 12:00")
        ts = [start + i * 60_000 for i in range(5)]
        ts.append(ts[-1] + 10 * 60_000)
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=5 * 60_000, exchange="binance")
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].gap_minutes, 10.0, places=1)

    def test_gap_below_threshold_not_flagged(self):
        start = _ts_ms("2026-05-13 12:00")
        ts = [start + i * 60_000 for i in range(5)]
        ts.append(ts[-1] + 3 * 60_000)  # 3m gap < 5m threshold
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=5 * 60_000, exchange="binance")
        self.assertEqual(gaps, [])

    def test_oanda_weekend_gap_suppressed(self):
        after = _ts_ms("2026-05-15 16:50")  # Friday
        mon = after + 55 * 3600 * 1000
        ts = [after, mon]
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=120 * 60_000, exchange="oanda")
        self.assertEqual(gaps, [])

    def test_oanda_unexpected_gap_flagged(self):
        after = _ts_ms("2026-05-13 14:00")  # Wednesday
        ts = [after, after + 4 * 3600 * 1000]
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=120 * 60_000, exchange="oanda")
        self.assertEqual(len(gaps), 1)
        self.assertAlmostEqual(gaps[0].gap_minutes, 240.0, places=0)

    def test_multiple_gaps(self):
        start = _ts_ms("2026-05-13 12:00")
        ts = [start + i * 60_000 for i in range(5)]
        ts.append(ts[-1] + 20 * 60_000)
        ts += [ts[-1] + i * 60_000 for i in range(1, 5)]
        ts.append(ts[-1] + 15 * 60_000)
        gaps = _detect_gaps(ts, self._TF_MS, gap_threshold_ms=5 * 60_000, exchange="binance")
        self.assertEqual(len(gaps), 2)
        self.assertAlmostEqual(gaps[0].gap_minutes, 20.0, places=0)
        self.assertAlmostEqual(gaps[1].gap_minutes, 15.0, places=0)


# ---------------------------------------------------------------------------
# Tests — tick report
# ---------------------------------------------------------------------------

class TestTickReport(unittest.TestCase):
    def test_read_tick_health_basic(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            _build_tick_h5(h5_path, symbol="BTCUSDT")
            results = collect_tick_report(data_dir=tmp)
            self.assertEqual(len(results), 1)
            h = results[0]
            self.assertEqual(h.symbol, "BTCUSDT")
            self.assertTrue(h.ok)
            self.assertEqual(h.trade_count, 3)
            self.assertEqual(h.depth_update_count, 10)
            self.assertEqual(h.depth_snapshot_count, 5)
            self.assertEqual(h.first_ts_ms, _ts_ms("2026-05-13 12:00"))
            self.assertEqual(h.last_ts_ms, _ts_ms("2026-05-13 13:00"))
            self.assertIsNotNone(h.stale_seconds)

    def test_read_tick_health_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = collect_tick_report(data_dir=tmp)
            self.assertEqual(results, [])

    def test_tick_report_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            _build_tick_h5(h5_path)
            results = collect_tick_report(data_dir=tmp)
            h = results[0]
            self.assertEqual(h.first_dt, "2026-05-13 12:00")
            self.assertEqual(h.last_dt, "2026-05-13 13:00")
            self.assertAlmostEqual(h.duration_hours, 1.0, delta=0.01)

    def test_tick_report_two_symbols(self):
        import h5py

        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            trades = np.array([[_ts_ms("2026-05-13 12:00"), 100.0, 1.0, 0.0]], dtype=np.float64)
            with h5py.File(h5_path, "w") as f:
                for sym in ("BTCUSDT", "ETHUSDT"):
                    grp = f.create_group(sym)
                    grp.create_dataset("trades", data=trades)
                    grp.create_dataset("depth_updates", data=np.zeros((2, 4)))
                    grp.create_dataset("depth_snapshots", data=np.zeros((1, 4)))

            results = collect_tick_report(data_dir=tmp)
            self.assertEqual(len(results), 2)
            self.assertEqual({h.symbol for h in results}, {"BTCUSDT", "ETHUSDT"})

    def test_print_tick_report_no_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            _build_tick_h5(h5_path)
            items = collect_tick_report(data_dir=tmp)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                print_tick_report(items)
            out = buf.getvalue()
            self.assertIn("BTCUSDT", out)

    def test_print_tick_report_empty(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_tick_report([])
        self.assertIn("no tick files", buf.getvalue())


# ---------------------------------------------------------------------------
# Tests — OHLCV report
# ---------------------------------------------------------------------------

class TestOhlcvReport(unittest.TestCase):
    def test_ohlcv_report_no_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 100)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            results = collect_ohlcv_report(data_dir=tmp)
            self.assertEqual(len(results), 1)
            h = results[0]
            self.assertEqual(h.symbol, "BTCUSDT")
            self.assertEqual(h.exchange, "binance")
            self.assertEqual(h.row_count, 100)
            self.assertEqual(h.gap_count, 0)
            self.assertTrue(h.ok)

    def test_ohlcv_report_with_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 60)
            ts.append(ts[-1] + 30 * 60_000)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            results = collect_ohlcv_report(data_dir=tmp, gap_threshold_minutes=5)
            h = results[0]
            self.assertEqual(h.gap_count, 1)
            self.assertAlmostEqual(h.largest_gap_minutes, 30.0, places=0)
            self.assertEqual(len(h.unexpected_gaps), 1)

    def test_ohlcv_report_exchange_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 10)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            _build_ohlcv_parquet(tmp, "oanda", "EUR_USD", ts)
            results = collect_ohlcv_report(exchange_filter="binance", data_dir=tmp)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].exchange, "binance")

    def test_ohlcv_report_multiple_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 50)
            for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
                _build_ohlcv_parquet(tmp, "binance", sym, ts)
            results = collect_ohlcv_report(data_dir=tmp)
            self.assertEqual(len(results), 3)
            self.assertEqual({h.symbol for h in results}, {"BTCUSDT", "ETHUSDT", "SOLUSDT"})

    def test_ohlcv_report_oanda_weekend_suppressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            friday_ts = _ts_ms("2026-05-15 16:00")
            monday_ts = _ts_ms("2026-05-18 17:00")
            ts = [friday_ts, monday_ts]
            _build_ohlcv_parquet(tmp, "oanda", "EUR_USD", ts)
            results = collect_ohlcv_report(data_dir=tmp, oanda_gap_threshold_minutes=120)
            h = next(r for r in results if r.symbol == "EUR_USD")
            self.assertEqual(h.gap_count, 0)

    def test_ohlcv_report_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            results = collect_ohlcv_report(data_dir=tmp)
            self.assertEqual(results, [])

    def test_print_ohlcv_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 20)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            items = collect_ohlcv_report(data_dir=tmp)
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                print_ohlcv_report(items)
            out = buf.getvalue()
            self.assertIn("BTCUSDT", out)
            self.assertIn("binance", out)

    def test_print_ohlcv_report_empty(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_ohlcv_report([])
        self.assertIn("no OHLCV", buf.getvalue())

    def test_gaps_only_filter(self):
        import io
        from contextlib import redirect_stdout

        with tempfile.TemporaryDirectory() as tmp:
            ts_clean = _continuous_ts("2026-05-13 12:00", 100)
            ts_gapped = list(ts_clean)
            ts_gapped.append(ts_gapped[-1] + 30 * 60_000)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts_clean)
            _build_ohlcv_parquet(tmp, "binance", "SOLUSDT", ts_gapped)
            items = collect_ohlcv_report(data_dir=tmp)

            buf = io.StringIO()
            with redirect_stdout(buf):
                print_ohlcv_report(items, gaps_only=True)
            out = buf.getvalue()
            self.assertIn("SOLUSDT", out)
            self.assertNotIn("BTCUSDT", out)


# ---------------------------------------------------------------------------
# Tests — JSON report
# ---------------------------------------------------------------------------

class TestJsonReport(unittest.TestCase):
    def test_json_valid(self):
        import h5py

        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            trades = np.array(
                [[_ts_ms("2026-05-13 12:00"), 100.0, 1.0, 0.0]], dtype=np.float64
            )
            with h5py.File(h5_path, "w") as f:
                grp = f.create_group("BTCUSDT")
                grp.create_dataset("trades", data=trades)
                grp.create_dataset("depth_updates", data=np.zeros((2, 4)))
                grp.create_dataset("depth_snapshots", data=np.zeros((1, 4)))

            ts = [_ts_ms("2026-05-13 12:00") + i * 60_000 for i in range(5)]
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)

            tick_items = collect_tick_report(data_dir=tmp)
            ohlcv_items = collect_ohlcv_report(data_dir=tmp)
            output = to_json_report(tick_items, ohlcv_items)

            payload = json.loads(output)
            self.assertIn("generated_at", payload)
            self.assertIsInstance(payload["tick"], list)
            self.assertIsInstance(payload["ohlcv"], list)
            self.assertEqual(payload["tick"][0]["symbol"], "BTCUSDT")
            self.assertEqual(payload["ohlcv"][0]["exchange"], "binance")

    def test_json_tick_required_fields(self):
        import h5py

        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            trades = np.array(
                [[_ts_ms("2026-05-13 12:00"), 100.0, 1.0, 0.0]], dtype=np.float64
            )
            with h5py.File(h5_path, "w") as f:
                grp = f.create_group("BTCUSDT")
                grp.create_dataset("trades", data=trades)
                grp.create_dataset("depth_updates", data=np.zeros((1, 4)))
                grp.create_dataset("depth_snapshots", data=np.zeros((1, 4)))

            tick_items = collect_tick_report(data_dir=tmp)
            output = json.loads(to_json_report(tick_items, []))
            t = output["tick"][0]
            for field in ("symbol", "trade_count", "depth_update_count",
                          "first_ts_ms", "last_ts_ms", "first_dt", "last_dt",
                          "stale_seconds", "ok", "file_size_bytes"):
                self.assertIn(field, t)

    def test_json_ohlcv_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = [_ts_ms("2026-05-13 12:00") + i * 60_000 for i in range(5)]
            _build_ohlcv_parquet(tmp, "binance", "ETHUSDT", ts)
            ohlcv_items = collect_ohlcv_report(data_dir=tmp)
            output = json.loads(to_json_report([], ohlcv_items))
            o = output["ohlcv"][0]
            for field in ("exchange", "symbol", "timeframe", "row_count",
                          "first_ts_ms", "last_ts_ms", "first_dt", "last_dt",
                          "gap_count", "largest_gap_minutes", "unexpected_gaps", "ok"):
                self.assertIn(field, o)
            self.assertIsInstance(o["unexpected_gaps"], list)

    def test_json_gaps_included(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = [_ts_ms("2026-05-13 12:00") + i * 60_000 for i in range(5)]
            ts.append(ts[-1] + 20 * 60_000)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            ohlcv_items = collect_ohlcv_report(data_dir=tmp)
            output = json.loads(to_json_report([], ohlcv_items))
            o = output["ohlcv"][0]
            self.assertEqual(o["gap_count"], 1)
            self.assertAlmostEqual(o["largest_gap_minutes"], 20.0, places=0)
            self.assertEqual(len(o["unexpected_gaps"]), 1)
            self.assertIn("after_dt", o["unexpected_gaps"][0])


# ---------------------------------------------------------------------------
# Tests — CLI / main()
# ---------------------------------------------------------------------------

class TestCli(unittest.TestCase):
    def _capture_main(self, argv: list) -> tuple[int, str]:
        """Run main() capturing stdout, return (return_code, stdout_text)."""
        import io
        from contextlib import redirect_stdout
        from tools.feed_health import main

        orig_argv = sys.argv[:]
        sys.argv = argv
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = main()
        finally:
            sys.argv = orig_argv
        return rc, buf.getvalue()

    def test_cli_tick_report(self):
        import h5py

        with tempfile.TemporaryDirectory() as tmp:
            h5_path = os.path.join(tmp, "binance_ticks.h5")
            trades = np.array(
                [[_ts_ms("2026-05-14 01:00"), 100.0, 0.5, 0.0]], dtype=np.float64
            )
            with h5py.File(h5_path, "w") as f:
                grp = f.create_group("BTCUSDT")
                grp.create_dataset("trades", data=trades)
                grp.create_dataset("depth_updates", data=np.zeros((2, 4)))
                grp.create_dataset("depth_snapshots", data=np.zeros((1, 4)))

            _rc, out = self._capture_main(
                ["feed_health.py", "--report", "tick", "--data-dir", tmp]
            )
            self.assertIn("BTCUSDT", out)

    def test_cli_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = [_ts_ms("2026-05-13 12:00") + i * 60_000 for i in range(5)]
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)

            _rc, out = self._capture_main(
                ["feed_health.py", "--report", "ohlcv", "--json", "--data-dir", tmp]
            )
            payload = json.loads(out)
            self.assertIn("ohlcv", payload)
            self.assertEqual(payload["ohlcv"][0]["symbol"], "BTCUSDT")

    def test_cli_gaps_only_suppresses_clean_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 100)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)

            _rc, out = self._capture_main([
                "feed_health.py", "--report", "ohlcv",
                "--gaps-only", "--data-dir", tmp,
            ])
            self.assertNotIn("BTCUSDT", out)

    def test_cli_exchange_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 10)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)
            _build_ohlcv_parquet(tmp, "oanda", "EUR_USD", ts)

            _rc, out = self._capture_main([
                "feed_health.py", "--report", "ohlcv",
                "--exchange", "binance", "--data-dir", tmp,
            ])
            self.assertIn("binance", out)
            self.assertNotIn("oanda", out)

    def test_cli_return_code_no_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 50)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)

            rc, _out = self._capture_main(
                ["feed_health.py", "--report", "ohlcv", "--data-dir", tmp]
            )
            self.assertEqual(rc, 0)

    def test_cli_return_code_with_gaps(self):
        with tempfile.TemporaryDirectory() as tmp:
            ts = _continuous_ts("2026-05-13 12:00", 5)
            ts.append(ts[-1] + 30 * 60_000)
            _build_ohlcv_parquet(tmp, "binance", "BTCUSDT", ts)

            rc, _out = self._capture_main([
                "feed_health.py", "--report", "ohlcv",
                "--gap-threshold-minutes", "5", "--data-dir", tmp,
            ])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
