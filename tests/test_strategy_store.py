"""Tests for Phase 4: HDF5 Strategy Persistence.

Validates:
- E1: Schema version written to new files
- E2: Signal write/read roundtrip
- E3: Snapshot buffered write/read roundtrip
- E4: Session event write/read roundtrip
- E5: Read path filtering by time range
- E6: Legacy file backward compatibility
- Counts, flush, close lifecycle
"""
import sys
import os
import tempfile
import shutil
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import h5py
import numpy as np

from strategy_store import StrategyStore, SCHEMA_VERSION
from execution.models import SignalCategory, SignalEntry

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


# ====================================================================
# Helpers
# ====================================================================

_tmpdir = None


def setup():
    global _tmpdir
    _tmpdir = tempfile.mkdtemp(prefix="test_strategy_store_")


def teardown():
    global _tmpdir
    if _tmpdir and os.path.exists(_tmpdir):
        shutil.rmtree(_tmpdir)


# ====================================================================
# A. Schema version (E1)
# ====================================================================

def test_schema_version_written():
    print("test_schema_version_written")
    store = StrategyStore("BTCUSDT", data_dir=_tmpdir)
    check(store.schema_version == SCHEMA_VERSION,
          f"schema_version is {SCHEMA_VERSION} (got {store.schema_version})")
    check(os.path.exists(store.path), "HDF5 file created")

    with h5py.File(store.path, "r") as f:
        check(int(f.attrs.get("schema_version", 0)) == SCHEMA_VERSION,
              "schema_version attribute in HDF5 root")
        check("BTCUSDT" in f, "symbol group exists")
        check("strategy_signals" in f["BTCUSDT"], "strategy_signals dataset exists")
        check("strategy_snapshots" in f["BTCUSDT"], "strategy_snapshots dataset exists")
        check("session_events" in f["BTCUSDT"], "session_events dataset exists")
    store.close()


# ====================================================================
# B. Signal write/read roundtrip (E2)
# ====================================================================

def test_signal_write_read_roundtrip():
    print("test_signal_write_read_roundtrip")
    store = StrategyStore("ETHUSDT", data_dir=_tmpdir)

    now_ms = int(time.time() * 1000)
    entry = SignalEntry(
        timestamp=now_ms,
        signal_type="STRATEGY_ARM",
        source="strategy",
        category=SignalCategory.STRATEGY_ARM,
        description="Strategy armed in observe mode",
        lifecycle_state="armed_waiting",
        wave_regime="NEUTRAL",
        tide_bias="LONG",
        risk_budget_pct=25.0,
        archetype="BOUNCE",
    )
    store.write_signal(entry)
    check(store.signal_count() == 1, f"signal_count is 1 (got {store.signal_count()})")

    signals = store.read_signals()
    check(len(signals) == 1, f"read back 1 signal (got {len(signals)})")

    s = signals[0]
    check(s["timestamp"] == now_ms, f"timestamp matches (got {s['timestamp']})")
    check(s["signal_type"] == "STRATEGY_ARM", f"signal_type matches (got {s['signal_type']})")
    check(s["category"] == "STRATEGY_ARM", f"category matches (got {s['category']})")
    check(s["lifecycle"] == "armed_waiting", f"lifecycle matches (got {s['lifecycle']})")
    check(s["wave_regime"] == "NEUTRAL", f"wave_regime matches (got {s['wave_regime']})")
    check(s["tide_bias"] == "LONG", f"tide_bias matches (got {s['tide_bias']})")
    check(s["archetype"] == "BOUNCE", f"archetype matches (got {s['archetype']})")
    check(abs(s["risk_pct"] - 25.0) < 0.01, f"risk_pct matches (got {s['risk_pct']})")
    check("armed in observe" in s["description"],
          f"description matches (got {s['description']})")
    store.close()


def test_multiple_signals():
    print("test_multiple_signals")
    store = StrategyStore("BTCUSDT_MULTI", data_dir=_tmpdir)

    base_ts = int(time.time() * 1000)
    for i in range(10):
        entry = SignalEntry(
            timestamp=base_ts + i * 1000,
            signal_type=f"SIGNAL_{i}",
            source="strategy",
            category=SignalCategory.STRATEGY_STATE_CHANGE,
            description=f"Signal {i}",
        )
        store.write_signal(entry)

    check(store.signal_count() == 10, f"10 signals written (got {store.signal_count()})")

    signals = store.read_signals()
    check(len(signals) == 10, f"10 signals read back (got {len(signals)})")
    check(signals[0]["signal_type"] == "SIGNAL_0", "first signal correct")
    check(signals[9]["signal_type"] == "SIGNAL_9", "last signal correct")
    store.close()


# ====================================================================
# C. Snapshot buffered write/read roundtrip (E3)
# ====================================================================

def test_snapshot_buffered_write():
    print("test_snapshot_buffered_write")
    store = StrategyStore("SNAPTEST", data_dir=_tmpdir)

    class MockTide:
        bias = "LONG"
    class MockWave:
        regime = "MEAN_REVERSION"
    class MockRipple:
        liquidity_state = "STABLE"
        microprice = 83456.789
        imbalance = 0.15
    class MockTrade:
        state = "ENTRY"
        unrealized_pnl = 0.005
        cumulative_pnl = 0.01
    class MockRisk:
        consumed_es = 40.0
        es_budget = 100.0
    class MockSnap:
        tide = MockTide()
        wave = MockWave()
        ripple = MockRipple()
        trade = MockTrade()
        risk = MockRisk()

    base_ts = int(time.time() * 1000)
    for i in range(5):
        store.buffer_snapshot(base_ts + i * 5000, MockSnap())

    check(store.snapshot_count() == 0,
          "snapshots not flushed yet (buffered)")

    store.flush_snapshots()
    check(store.snapshot_count() == 5,
          f"5 snapshots after flush (got {store.snapshot_count()})")

    snaps = store.read_snapshots()
    check(len(snaps) == 5, f"5 snapshots read back (got {len(snaps)})")

    s = snaps[0]
    check(s["wave_regime"] == "MEAN_REVERSION",
          f"wave_regime correct (got {s['wave_regime']})")
    check(s["tide_bias"] == "LONG", f"tide_bias correct (got {s['tide_bias']})")
    check(s["liquidity_state"] == "STABLE",
          f"liquidity_state correct (got {s['liquidity_state']})")
    check(s["lifecycle_state"] == "ENTRY",
          f"lifecycle_state correct (got {s['lifecycle_state']})")
    check(abs(s["microprice"] - 83456.789) < 0.01,
          f"microprice correct (got {s['microprice']})")
    check(abs(s["consumed_es"] - 40.0) < 0.01,
          f"consumed_es correct (got {s['consumed_es']})")
    store.close()


def test_snapshot_auto_flush():
    print("test_snapshot_auto_flush")
    store = StrategyStore("AUTOFLUSH", data_dir=_tmpdir)
    store._FLUSH_BATCH = 10

    class MockSnap:
        class tide:
            bias = "NEUTRAL"
        class wave:
            regime = "NEUTRAL"
        class ripple:
            liquidity_state = "STABLE"
            microprice = 50000.0
            imbalance = 0.0
        class trade:
            state = "IDLE"
            unrealized_pnl = 0.0
            cumulative_pnl = 0.0
        class risk:
            consumed_es = 0.0
            es_budget = 100.0

    base_ts = int(time.time() * 1000)
    for i in range(10):
        store.buffer_snapshot(base_ts + i * 1000, MockSnap())

    check(store.snapshot_count() == 10,
          f"auto-flushed at batch size (got {store.snapshot_count()})")
    store.close()


# ====================================================================
# D. Session event write/read roundtrip (E4)
# ====================================================================

def test_session_event_persistence():
    print("test_session_event_persistence")
    store = StrategyStore("EVENTTEST", data_dir=_tmpdir)

    now_ms = int(time.time() * 1000)
    store.write_event(now_ms, "CONNECT", {"symbol": "BTCUSDT", "mode": "Live"})
    store.write_event(now_ms + 1000, "ARM", {"mode": "observe"})
    store.write_event(now_ms + 60000, "DISARM", {"was_active": True})
    store.write_event(now_ms + 61000, "DISCONNECT")

    check(store.event_count() == 4, f"4 events (got {store.event_count()})")

    events = store.read_events()
    check(len(events) == 4, f"4 events read back (got {len(events)})")

    check(events[0]["event_type"] == "CONNECT", "first event is CONNECT")
    check(events[1]["event_type"] == "ARM", "second event is ARM")
    check(events[2]["event_type"] == "DISARM", "third event is DISARM")
    check(events[3]["event_type"] == "DISCONNECT", "fourth event is DISCONNECT")

    check('"symbol": "BTCUSDT"' in events[0]["details"],
          f"CONNECT details contain symbol (got {events[0]['details']})")
    check('"mode": "observe"' in events[1]["details"],
          f"ARM details contain mode (got {events[1]['details']})")
    check('"was_active": true' in events[2]["details"],
          f"DISARM details contain was_active (got {events[2]['details']})")
    store.close()


# ====================================================================
# E. Read path time-range filtering (E5)
# ====================================================================

def test_read_signals_time_filter():
    print("test_read_signals_time_filter")
    store = StrategyStore("TIMEFILTER", data_dir=_tmpdir)

    base_ts = 1_700_000_000_000
    for i in range(5):
        entry = SignalEntry(
            timestamp=base_ts + i * 10_000,
            signal_type=f"SIG_{i}",
            source="strategy",
            category=SignalCategory.STRATEGY_STATE_CHANGE,
        )
        store.write_signal(entry)

    all_sigs = store.read_signals()
    check(len(all_sigs) == 5, "all 5 signals without filter")

    filtered = store.read_signals(from_ms=base_ts + 15_000, to_ms=base_ts + 35_000)
    check(len(filtered) == 2,
          f"2 signals in [15s, 35s] range (got {len(filtered)})")
    check(filtered[0]["signal_type"] == "SIG_2", "first filtered is SIG_2")
    check(filtered[1]["signal_type"] == "SIG_3", "second filtered is SIG_3")
    store.close()


def test_read_snapshots_time_filter():
    print("test_read_snapshots_time_filter")
    store = StrategyStore("SNAPFILTER", data_dir=_tmpdir)

    class MockSnap:
        class tide:
            bias = "NEUTRAL"
        class wave:
            regime = "NEUTRAL"
        class ripple:
            liquidity_state = "STABLE"
            microprice = 50000.0
            imbalance = 0.0
        class trade:
            state = "IDLE"
            unrealized_pnl = 0.0
            cumulative_pnl = 0.0
        class risk:
            consumed_es = 0.0
            es_budget = 100.0

    base_ts = 1_700_000_000_000
    for i in range(5):
        store.buffer_snapshot(base_ts + i * 10_000, MockSnap())
    store.flush_snapshots()

    filtered = store.read_snapshots(from_ms=base_ts + 5_000, to_ms=base_ts + 25_000)
    check(len(filtered) == 2,
          f"2 snapshots in range (got {len(filtered)})")
    store.close()


def test_read_events_time_filter():
    print("test_read_events_time_filter")
    store = StrategyStore("EVTFILTER", data_dir=_tmpdir)

    base_ts = 1_700_000_000_000
    store.write_event(base_ts, "CONNECT")
    store.write_event(base_ts + 60_000, "ARM")
    store.write_event(base_ts + 120_000, "DISARM")

    filtered = store.read_events(from_ms=base_ts + 30_000)
    check(len(filtered) == 2,
          f"2 events after 30s (got {len(filtered)})")
    store.close()


# ====================================================================
# F. Legacy / backward compatibility (E6)
# ====================================================================

def test_legacy_file_detection():
    print("test_legacy_file_detection")
    legacy_path = os.path.join(_tmpdir, "LEGACY_strategy.h5")
    with h5py.File(legacy_path, "w") as f:
        f.create_group("LEGACY")

    check(StrategyStore.is_legacy(legacy_path),
          "file without schema_version is legacy")

    v1_path = os.path.join(_tmpdir, "V1_strategy.h5")
    with h5py.File(v1_path, "w") as f:
        f.attrs["schema_version"] = 1
        f.create_group("V1")

    check(StrategyStore.is_legacy(v1_path),
          "file with schema_version=1 is legacy")

    store = StrategyStore("V2TEST", data_dir=_tmpdir)
    check(not StrategyStore.is_legacy(store.path),
          "new file with schema_version=2 is not legacy")
    store.close()


def test_legacy_file_readable():
    print("test_legacy_file_readable")
    legacy_path = os.path.join(_tmpdir, "LEGACY2_strategy.h5")
    with h5py.File(legacy_path, "w") as f:
        f.create_group("LEGACY2")

    result = StrategyStore.open_readonly(legacy_path, "LEGACY2")
    check(result is not None, "can open legacy file readonly")
    if result:
        signals = result.read_signals()
        check(signals == [], "empty signals from legacy file without datasets")
        result.close()


def test_nonexistent_file():
    print("test_nonexistent_file")
    result = StrategyStore.open_readonly("/nonexistent/path.h5", "SYM")
    check(result is None, "returns None for nonexistent file")


def test_missing_symbol_readonly():
    print("test_missing_symbol_readonly")
    path = os.path.join(_tmpdir, "NOSYM_strategy.h5")
    with h5py.File(path, "w") as f:
        f.attrs["schema_version"] = 2
        f.create_group("OTHERSYM")

    result = StrategyStore.open_readonly(path, "MISSING")
    check(result is None, "returns None for missing symbol")


# ====================================================================
# G. Lifecycle: flush and close
# ====================================================================

def test_close_flushes_buffers():
    print("test_close_flushes_buffers")
    store = StrategyStore("CLOSETEST", data_dir=_tmpdir)

    class MockSnap:
        class tide:
            bias = "NEUTRAL"
        class wave:
            regime = "NEUTRAL"
        class ripple:
            liquidity_state = "STABLE"
            microprice = 50000.0
            imbalance = 0.0
        class trade:
            state = "IDLE"
            unrealized_pnl = 0.0
            cumulative_pnl = 0.0
        class risk:
            consumed_es = 0.0
            es_budget = 100.0

    for i in range(3):
        store.buffer_snapshot(1_700_000_000_000 + i * 1000, MockSnap())

    check(store.snapshot_count() == 0, "not flushed before close")
    store.close()

    with h5py.File(os.path.join(_tmpdir, "CLOSETEST_strategy.h5"), "r") as f:
        check(f["CLOSETEST"]["strategy_snapshots"].shape[0] == 3,
              "3 snapshots flushed on close")


def test_reopen_preserves_data():
    print("test_reopen_preserves_data")
    store = StrategyStore("REOPEN", data_dir=_tmpdir)
    store.write_event(1_700_000_000_000, "CONNECT", {"test": True})
    store.close()

    store2 = StrategyStore("REOPEN", data_dir=_tmpdir)
    check(store2.event_count() == 1, f"event preserved after reopen (got {store2.event_count()})")
    check(store2.schema_version == SCHEMA_VERSION,
          f"schema version preserved (got {store2.schema_version})")
    store2.close()


# ====================================================================
# H. Empty field handling
# ====================================================================

def test_signal_with_defaults():
    print("test_signal_with_defaults")
    store = StrategyStore("DEFAULTS", data_dir=_tmpdir)

    entry = SignalEntry(
        timestamp=1_700_000_000_000,
        signal_type="RIPPLE_ENTRY",
        source="ripple",
        category=SignalCategory.RIPPLE_ENTRY,
    )
    store.write_signal(entry)

    signals = store.read_signals()
    check(len(signals) == 1, "1 signal with defaults")
    s = signals[0]
    check(s["side"] == "", f"side defaults empty (got '{s['side']}')")
    check(s["archetype"] == "", f"archetype defaults empty (got '{s['archetype']}')")
    check(s["wave_regime"] == "", f"wave_regime defaults empty (got '{s['wave_regime']}')")
    check(s["risk_pct"] == 0.0, f"risk_pct defaults 0 (got {s['risk_pct']})")
    store.close()


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_schema_version_written,
        test_signal_write_read_roundtrip,
        test_multiple_signals,
        test_snapshot_buffered_write,
        test_snapshot_auto_flush,
        test_session_event_persistence,
        test_read_signals_time_filter,
        test_read_snapshots_time_filter,
        test_read_events_time_filter,
        test_legacy_file_detection,
        test_legacy_file_readable,
        test_nonexistent_file,
        test_missing_symbol_readonly,
        test_close_flushes_buffers,
        test_reopen_preserves_data,
        test_signal_with_defaults,
    ]
    setup()
    try:
        for t in tests:
            t()
    finally:
        teardown()
    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"Strategy store tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
