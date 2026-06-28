"""Tests for collect_ticks.py CLI entrypoint.

These tests stub out the C++ engine and network dependencies so they run
headlessly on any platform (macOS, Linux CI, Docker).
"""

import argparse
import importlib
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers to load the module with the C++ import stubbed out
# ---------------------------------------------------------------------------

def _load_module():
    """Import collect_ticks with orderflow_engine stubbed."""
    ofe_stub = MagicMock()
    ofe_stub.TickStore = MagicMock
    ofe_stub.OrderFlowEngine = MagicMock
    ofe_stub.Trade = MagicMock
    ofe_stub.DepthUpdate = MagicMock
    ofe_stub.DepthLevel = MagicMock

    with patch.dict(sys.modules, {"orderflow_engine": ofe_stub}):
        # Also stub data_service's TickDataCollector so we don't need a real store
        import collect_ticks  # noqa: PLC0415
        importlib.reload(collect_ticks)
        return collect_ticks


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

class TestArgParsing(unittest.TestCase):

    def setUp(self):
        self.ct = _load_module()

    def test_defaults(self):
        # Call with empty argv
        with patch("sys.argv", ["collect_ticks.py"]):
            args = self.ct._parse_args()
        self.assertEqual(args.symbol, "BTCUSDT")
        self.assertIsNone(args.symbols)
        self.assertEqual(args.exchange, "binance")
        self.assertTrue(args.futures)
        self.assertEqual(args.duration, 0)
        self.assertIsNone(args.s3_bucket)
        # --s3-key defaults to None now; the per-symbol key is computed
        # by _default_s3_key(symbol) at upload time.
        self.assertIsNone(args.s3_key)
        self.assertEqual(args.log_level, "INFO")
        self.assertEqual(args.parquet_flush_interval, 60)
        self.assertEqual(args.max_h5_gb, 8.0)

    def test_default_s3_key_per_symbol(self):
        self.assertEqual(self.ct._default_s3_key("BTCUSDT"),
                         "ticks/BTCUSDT_ticks.h5")
        # Symbol is upper-cased so the key is stable regardless of input case.
        self.assertEqual(self.ct._default_s3_key("ethusdt"),
                         "ticks/ETHUSDT_ticks.h5")


class TestRotationReason(unittest.TestCase):
    """Rotation fires on EITHER the size cap or the free-disk floor — the
    disk floor is the guard against the 2026-06 disk-full incident."""

    def setUp(self):
        self.ct = _load_module()

    def test_no_file_no_rotation(self):
        self.assertIsNone(self.ct._rotation_reason("/no/such/file.h5", 1))

    def test_size_cap_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.h5")
            with open(p, "wb") as f:
                f.write(b"\0" * 2048)
            with patch.dict(os.environ, {"MIN_FREE_DISK_GB": "0"}):
                reason = self.ct._rotation_reason(p, 1024)
            self.assertIsNotNone(reason)
            self.assertIn("reached", reason)

    def test_under_size_cap_no_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.h5")
            with open(p, "wb") as f:
                f.write(b"\0" * 100)
            # Disable disk floor so only the size cap is in play.
            with patch.dict(os.environ, {"MIN_FREE_DISK_GB": "0"}):
                self.assertIsNone(self.ct._rotation_reason(p, 1024**3))

    def test_disk_floor_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.h5")
            with open(p, "wb") as f:
                f.write(b"\0" * 100)
            # Floor far above any real free space → must trigger.
            with patch.dict(os.environ, {"MIN_FREE_DISK_GB": "1000000"}):
                reason = self.ct._rotation_reason(p, 0)
            self.assertIsNotNone(reason)
            self.assertIn("Free disk", reason)

    def test_stat_failure_forces_rotation(self):
        # An existing-but-unstattable HDF5 must NOT be coerced to size=0 (a
        # success-shaped value that would suppress size-based rotation and let
        # a large, unreadable file accumulate until the disk fills). It must
        # instead force rotation. Disk floor disabled to isolate the size path.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.h5")
            with open(p, "wb") as f:
                f.write(b"\0" * 2048)
            with patch.dict(os.environ, {"MIN_FREE_DISK_GB": "0"}), \
                    patch("os.path.getsize", side_effect=OSError("boom")):
                reason = self.ct._rotation_reason(p, 1024)
            self.assertIsNotNone(reason)
            self.assertIn("forcing rotation", reason)

    def test_disk_usage_failure_logs_and_skips_floor(self):
        # If free space cannot be read we cannot assert the floor, so the check
        # is skipped — but loudly (logged), never silently. Size cap disabled
        # (max_h5_bytes=0) to isolate the disk-floor path.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "x.h5")
            with open(p, "wb") as f:
                f.write(b"\0" * 100)
            with patch.dict(os.environ, {"MIN_FREE_DISK_GB": "1000000"}), \
                    patch("shutil.disk_usage", side_effect=OSError("boom")):
                with self.assertLogs(self.ct.logger, level="WARNING") as cm:
                    reason = self.ct._rotation_reason(p, 0)
            self.assertIsNone(reason)
            self.assertTrue(any("Cannot read free disk" in m for m in cm.output))

    def test_ticks_h5_path_uses_ticks_subdir(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.ct._ticks_h5_path(tmp, "BTCUSDT")
            self.assertEqual(
                path, os.path.join(tmp, "ticks", "BTCUSDT_ticks.h5")
            )
            # The ticks/ subdir is created eagerly so the C++ store can
            # open the file without further setup.
            self.assertTrue(os.path.isdir(os.path.join(tmp, "ticks")))

    def test_custom_symbol(self):
        with patch("sys.argv", ["collect_ticks.py", "--symbol", "ETHUSDT"]):
            args = self.ct._parse_args()
        self.assertEqual(args.symbol, "ETHUSDT")

    def test_symbols_list(self):
        with patch("sys.argv",
                   ["collect_ticks.py", "--symbols", "BTCUSDT,ETHUSDT"]):
            args = self.ct._parse_args()
        self.assertEqual(args.symbols, "BTCUSDT,ETHUSDT")

    def test_duration(self):
        with patch("sys.argv",
                   ["collect_ticks.py", "--duration", "60"]):
            args = self.ct._parse_args()
        self.assertEqual(args.duration, 60)

    def test_s3_bucket(self):
        with patch("sys.argv",
                   ["collect_ticks.py", "--s3-bucket", "my-bucket"]):
            args = self.ct._parse_args()
        self.assertEqual(args.s3_bucket, "my-bucket")

    def test_no_futures(self):
        with patch("sys.argv", ["collect_ticks.py", "--no-futures"]):
            args = self.ct._parse_args()
        self.assertFalse(args.futures)


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class TestLoggingSetup(unittest.TestCase):

    def setUp(self):
        self.ct = _load_module()

    def test_setup_logging_creates_log_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = os.path.join(tmpdir, "logs")
            self.ct._setup_logging("INFO", log_dir=log_dir)
            self.assertTrue(os.path.isdir(log_dir))

    def test_setup_logging_invalid_level_falls_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Should not raise even with an unknown level string
            self.ct._setup_logging("BOGUS", log_dir=tmpdir)


# ---------------------------------------------------------------------------
# S3 upload helper
# ---------------------------------------------------------------------------

class TestS3Upload(unittest.TestCase):
    """boto3 is an EC2-only dependency; stub it via sys.modules so tests
    run without the package installed locally."""

    def _s3_context(self, mock_s3_client):
        boto3_stub = MagicMock()
        boto3_stub.client.return_value = mock_s3_client
        return patch.dict(sys.modules, {"boto3": boto3_stub})

    def setUp(self):
        self.ct = _load_module()

    def test_upload_success(self):
        mock_s3 = MagicMock()
        with self._s3_context(mock_s3):
            with tempfile.NamedTemporaryFile() as f:
                result = self.ct._upload_to_s3(f.name, "bucket", "key/file.h5")
        self.assertTrue(result)
        mock_s3.upload_file.assert_called_once()

    def test_upload_failure_boto3_import_error_returns_false(self):
        """If boto3 raises on import (not installed), return False gracefully."""
        with patch.dict(sys.modules, {"boto3": None}):
            result = self.ct._upload_to_s3("/nonexistent/path.h5",
                                           "bucket", "key.h5")
        self.assertFalse(result)

    def test_upload_s3_error_returns_false(self):
        mock_s3 = MagicMock()
        mock_s3.upload_file.side_effect = Exception("access denied")
        with self._s3_context(mock_s3):
            with tempfile.NamedTemporaryFile() as f:
                result = self.ct._upload_to_s3(f.name, "bucket", "key.h5")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------

class TestSigtermHandler(unittest.TestCase):

    def setUp(self):
        self.ct = _load_module()
        self.ct._shutdown_requested.clear()

    def tearDown(self):
        self.ct._shutdown_requested.clear()

    def test_sigterm_sets_shutdown_flag(self):
        # os.kill is patched so the handler doesn't actually signal the
        # test process (which would terminate the runner).
        with patch.object(self.ct.os, "kill"):
            self.ct._handle_sigterm(15, None)
        self.assertTrue(self.ct._shutdown_requested.is_set())

    def test_sigterm_idempotent(self):
        # A second invocation while shutdown is already pending must be
        # a no-op (no recursive os.kill storm).
        with patch.object(self.ct.os, "kill") as mock_kill:
            self.ct._handle_sigterm(15, None)
            self.ct._handle_sigterm(15, None)
        mock_kill.assert_called_once()


# ---------------------------------------------------------------------------
# Main function integration (with TickDataCollector stubbed)
# ---------------------------------------------------------------------------
#
# All tests pass ``--parquet-flush-interval 0`` to disable the background
# Parquet flusher — the tests stub the C++ store entirely so there's
# nothing to flush, and a running flush thread can delay test teardown.

class TestMainFunction(unittest.TestCase):

    def _make_mock_collector(self):
        collector = MagicMock()
        collector.store = MagicMock()
        return collector

    def _argv(self, tmpdir, *extra):
        return [
            "collect_ticks.py",
            "--symbol", "BTCUSDT",
            "--duration", "1",
            "--data-dir", tmpdir,
            "--parquet-flush-interval", "0",
            *extra,
        ]

    def test_main_single_symbol_collects_and_exits_0(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()

        with patch.object(ct, "TickDataCollector",
                          return_value=mock_collector):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch("sys.argv", self._argv(tmpdir)):
                    rc = ct.main()
        self.assertEqual(rc, 0)
        mock_collector.collect.assert_called_once_with("BTCUSDT",
                                                       duration_seconds=1)
        # store.flush() and store.close() are owned exclusively by
        # data_service.TickDataCollector.collect() — NOT by collect_ticks.py.
        # The mock's collect() is a no-op, so these must be zero here.
        # A double-close regression would cause these to be > 0.
        mock_collector.store.flush.assert_not_called()
        mock_collector.store.close.assert_not_called()

    def test_main_s3_upload_called_on_clean_exit(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()
        upload_calls = []

        def mock_upload(path, bucket, key, **kwargs):
            upload_calls.append((path, bucket, key))
            return True

        with patch.object(ct, "TickDataCollector", return_value=mock_collector):
            with patch.object(ct, "_upload_to_s3", side_effect=mock_upload):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch(
                        "sys.argv",
                        self._argv(tmpdir, "--s3-bucket", "my-bucket"),
                    ):
                        rc = ct.main()

        self.assertEqual(rc, 0)
        self.assertEqual(len(upload_calls), 1)
        _, bucket, key = upload_calls[0]
        self.assertEqual(bucket, "my-bucket")
        # Per-symbol key with the new naming convention.
        self.assertEqual(key, "ticks/BTCUSDT_ticks.h5")

    def test_main_s3_upload_failure_returns_exit_2(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()

        with patch.object(ct, "TickDataCollector", return_value=mock_collector):
            with patch.object(ct, "_upload_to_s3", return_value=False):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch(
                        "sys.argv",
                        self._argv(tmpdir, "--s3-bucket", "bad-bucket"),
                    ):
                        rc = ct.main()

        self.assertEqual(rc, 2)

    def test_main_exception_returns_exit_1(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()
        mock_collector.collect.side_effect = RuntimeError("boom")

        with patch.object(ct, "TickDataCollector", return_value=mock_collector):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch("sys.argv", self._argv(tmpdir)):
                    rc = ct.main()

        self.assertEqual(rc, 1)
        # Even on an unhandled exception collect() still owns flush/close.
        # collect_ticks must not call them.
        mock_collector.store.flush.assert_not_called()
        mock_collector.store.close.assert_not_called()

    def test_main_no_s3_upload_when_bucket_not_set(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()
        upload_calls = []

        with patch.object(ct, "TickDataCollector", return_value=mock_collector):
            with patch.object(
                ct, "_upload_to_s3",
                side_effect=lambda *a, **kw: upload_calls.append(a) or True,
            ):
                with tempfile.TemporaryDirectory() as tmpdir:
                    with patch("sys.argv", self._argv(tmpdir)):
                        ct.main()

        self.assertEqual(len(upload_calls), 0)

    def test_main_multi_symbol_spawns_subprocesses(self):
        """With --symbols A,B main() dispatches to _run_multi_symbol(),
        which spawns one subprocess per symbol.  We stub _run_multi_symbol
        to verify it is called with the correct symbol list and that main()
        propagates its return value.
        """
        ct = _load_module()

        with patch.object(ct, "_run_multi_symbol", return_value=0) as mock_rms:
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch(
                    "sys.argv",
                    [
                        "collect_ticks.py",
                        "--symbols", "BTCUSDT,ETHUSDT",
                        "--duration", "1",
                        "--data-dir", tmpdir,
                        "--parquet-flush-interval", "0",
                    ],
                ):
                    rc = ct.main()

        self.assertEqual(rc, 0)
        mock_rms.assert_called_once_with(["BTCUSDT", "ETHUSDT"])

    def test_build_child_argv_replaces_symbols_with_symbol(self):
        """_build_child_argv must prepend sys.executable, use an absolute
        script path, and replace --symbols X,Y with --symbol X."""
        ct = _load_module()
        with patch("sys.argv", [
            "collect_ticks.py",
            "--symbols", "BTCUSDT,ETHUSDT",
            "--s3-bucket", "my-bucket",
            "--log-level", "DEBUG",
        ]):
            argv = ct._build_child_argv("ETHUSDT")

        # First element must be the Python interpreter so Popen can run it.
        self.assertEqual(argv[0], sys.executable)
        # Second element must be an absolute path to the script.
        self.assertTrue(os.path.isabs(argv[1]), f"script path not absolute: {argv[1]}")
        # --symbols must be replaced with --symbol <symbol>
        self.assertIn("--symbol", argv)
        self.assertIn("ETHUSDT", argv)
        self.assertNotIn("--symbols", argv)
        self.assertNotIn("BTCUSDT,ETHUSDT", argv)
        # Other args must be passed through unchanged.
        self.assertIn("--s3-bucket", argv)
        self.assertIn("my-bucket", argv)

    def test_main_keyboard_interrupt_exits_0(self):
        ct = _load_module()
        mock_collector = self._make_mock_collector()
        mock_collector.collect.side_effect = KeyboardInterrupt()

        with patch.object(ct, "TickDataCollector", return_value=mock_collector):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch(
                    "sys.argv",
                    [
                        "collect_ticks.py",
                        "--symbol", "BTCUSDT",
                        "--data-dir", tmpdir,
                        "--parquet-flush-interval", "0",
                    ],
                ):
                    rc = ct.main()

        self.assertEqual(rc, 0)
        # KeyboardInterrupt is caught and treated as clean exit.
        # store lifecycle is owned by data_service — not called here.
        mock_collector.store.flush.assert_not_called()
        mock_collector.store.close.assert_not_called()

    def test_store_lifecycle_owned_by_data_service_not_collect_ticks(self):
        """collect_ticks must never call store.flush() or store.close().

        The C++ TickStore must be closed exactly once — inside
        data_service.TickDataCollector.collect(). Calling it a second
        time from _collect_one() is a double-close and is undefined
        behaviour that can corrupt the HDF5 file.

        This test asserts the contract: no matter which shutdown path
        (normal, KeyboardInterrupt, unhandled exception) is taken,
        collect_ticks never touches store.flush or store.close.
        """
        ct = _load_module()

        for side_effect, label in [
            (None,                  "clean exit"),
            (KeyboardInterrupt(),   "KeyboardInterrupt"),
            (RuntimeError("boom"),  "unhandled exception"),
        ]:
            with self.subTest(shutdown=label):
                mock_collector = self._make_mock_collector()
                mock_collector.collect.side_effect = side_effect

                with patch.object(ct, "TickDataCollector",
                                  return_value=mock_collector):
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with patch("sys.argv", self._argv(tmpdir)):
                            ct.main()

                mock_collector.store.flush.assert_not_called()
                mock_collector.store.close.assert_not_called()


# ---------------------------------------------------------------------------
# TickDataCollector store_path parameter
# ---------------------------------------------------------------------------

class TestTickDataCollectorStorePath(unittest.TestCase):

    def test_default_store_path_is_data_dir(self):
        """TickDataCollector default store path is data/{exchange}_ticks.h5."""
        import importlib
        ofe_stub = MagicMock()
        ofe_stub.TickStore = MagicMock(return_value=MagicMock())
        ofe_stub.OrderFlowEngine = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"orderflow_engine": ofe_stub}):
            import data_service
            importlib.reload(data_service)
            # Patch makedirs so no real directory is created
            with patch("os.makedirs"):
                data_service.TickDataCollector(exchange="binance")
            # The TickStore should have been called with the relative default path
            call_arg = ofe_stub.TickStore.call_args[0][0]
            self.assertEqual(call_arg, os.path.join("data", "binance_ticks.h5"))

    def test_custom_store_path_is_respected(self):
        """store_path kwarg overrides the default path."""
        ofe_stub = MagicMock()
        ofe_stub.TickStore = MagicMock(return_value=MagicMock())
        ofe_stub.OrderFlowEngine = MagicMock(return_value=MagicMock())

        with patch.dict(sys.modules, {"orderflow_engine": ofe_stub}):
            import data_service
            importlib.reload(data_service)
            with tempfile.TemporaryDirectory() as tmpdir:
                custom = os.path.join(tmpdir, "my_ticks.h5")
                data_service.TickDataCollector(
                    exchange="binance", store_path=custom
                )
                ofe_stub.TickStore.assert_called_with(custom)


if __name__ == "__main__":
    unittest.main()
