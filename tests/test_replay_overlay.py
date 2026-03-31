"""Phase 5: Replay-with-strategy-overlay smoke test.

Simulates the replay-overlay workflow:
1. A "live" session writes strategy signals, snapshots, and events to HDF5.
2. The store is closed (simulating disconnect).
3. A read-only store is opened (simulating replay load).
4. Signals and snapshots are read back and used to reconstruct:
   - Strategy overlay lines on the heatmap
   - Snapshot values for the diagnostics panel
   - Event timeline for the blotter
5. Verifies determinism: re-reading produces identical results.
6. Verifies time-range queries work for replay window slicing.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import SignalCategory, SignalEntry
from strategy_store import StrategyStore
from ui.heatmap_widget import HeatmapWidget
from ui.strategy_panel import StrategyDiagnosticsPanel
from ui.trade_blotter import TradeBlotter

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

class _FakeSnap:
    """Stand-in for C++ StrategySnapshot with settable values."""
    class _Wave:
        def __init__(self):
            self.regime = "MEAN_REVERSION"
            self.trend_efficiency = 0.72
    class _Tide:
        def __init__(self):
            self.bias = "LONG"
    class _Ripple:
        def __init__(self):
            self.liquidity_state = "BALANCED"
            self.microprice = 50100.5
            self.imbalance = 0.025
    class _Trade:
        def __init__(self):
            self.state = "EXPANSION"
            self.archetype = "BOUNCE"
            self.unrealized_pnl = 0.15
            self.stop_price = 49800.0
            self.target_price = 50500.0
            self.hold_time_ms = 30000
            self.cumulative_pnl = 0.45
    class _Risk:
        def __init__(self):
            self.consumed_es = 0.6
            self.es_budget = 1.0
    def __init__(self):
        self.wave = self._Wave()
        self.tide = self._Tide()
        self.ripple = self._Ripple()
        self.trade = self._Trade()
        self.risk = self._Risk()


def _write_session(tmpdir, symbol="BTCUSDT"):
    """Simulate a full live session writing to HDF5."""
    store = StrategyStore(symbol, data_dir=tmpdir)
    base = 1700000000000

    store.write_event(base, "CONNECT", {"symbol": symbol})
    store.write_event(base + 1000, "ARM", {"mode": "paper"})

    # Write signals at known timestamps with known prices
    signal_prices = [50000.0, 50100.0, 50050.0, 49950.0, 50200.0]
    for i, price in enumerate(signal_prices):
        ts = base + 10000 + i * 5000
        entry = SignalEntry(
            timestamp=ts,
            signal_type="RIPPLE_ENTER_BOUNCE_LONG" if i % 2 == 0 else "RIPPLE_EXIT_BOUNCE",
            source="ripple",
            category=SignalCategory.RIPPLE_ENTRY if i % 2 == 0 else SignalCategory.RIPPLE_EXIT,
            side="BUY" if i % 2 == 0 else "",
            price=price,
            strength=0.7 + i * 0.05,
            description=f"replay signal {i}",
            lifecycle_state="armed_active",
            wave_regime="MEAN_REVERSION",
            tide_bias="LONG",
            archetype="BOUNCE",
        )
        store.write_signal(entry)

    # Write snapshots with varying stop/target prices
    snap = _FakeSnap()
    for i in range(8):
        ts = base + 10000 + i * 3000
        snap.trade.stop_price = 49800.0 + i * 10
        snap.trade.target_price = 50500.0 + i * 10
        snap.ripple.microprice = 50100.5 + i * 5
        store.buffer_snapshot(ts, snap)
    store.flush_snapshots()

    store.write_event(base + 50000, "DISARM", {"was_active": True})
    store.write_event(base + 60000, "DISCONNECT")
    store.close()

    return store.path, base


# ====================================================================
# 1. Read-back and overlay reconstruction
# ====================================================================

def test_replay_overlay_reconstruction():
    """Read persisted data and reconstruct heatmap overlay from the last snapshot."""
    print("test_replay_overlay_reconstruction")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")
    check(ro is not None, "readonly store opened")

    snaps = ro.read_snapshots()
    check(len(snaps) == 8, f"8 snapshots read (got {len(snaps)})")

    # Use last snapshot to reconstruct overlay
    last = snaps[-1]
    hm = HeatmapWidget()
    hm.resize(800, 500)

    stop = float(last.get("microprice", 0)) - 300  # derived stop
    target = float(last.get("microprice", 0)) + 400  # derived target
    entry = float(last.get("microprice", 0))

    hm.set_strategy_overlay(entry_price=entry, stop_price=stop, target_price=target)
    check(hm._overlay_entry_price == entry, f"entry overlay set to {entry}")
    check(hm._overlay_stop_price == stop, f"stop overlay set to {stop}")
    check(hm._overlay_target_price == target, f"target overlay set to {target}")

    # Verify snapshot values are plausible
    check(last["wave_regime"] == "MEAN_REVERSION",
          f"wave_regime from snapshot (got {last['wave_regime']})")
    check(last["tide_bias"] == "LONG",
          f"tide_bias from snapshot (got {last['tide_bias']})")
    check(last["microprice"] > 50000,
          f"microprice plausible (got {last['microprice']})")

    ro.close()


# ====================================================================
# 2. Signal playback into blotter
# ====================================================================

def test_replay_signal_playback():
    """Read persisted signals and replay them into the trade blotter."""
    print("test_replay_signal_playback")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")
    sigs = ro.read_signals()
    check(len(sigs) == 5, f"5 signals read (got {len(sigs)})")

    blotter = TradeBlotter()
    for s in sigs:
        cat_str = s.get("category", "LEGACY_RAW")
        try:
            cat = SignalCategory(cat_str)
        except ValueError:
            cat = SignalCategory.LEGACY_RAW
        entry = SignalEntry(
            timestamp=s["timestamp"],
            signal_type=s["signal_type"],
            source="replay",
            category=cat,
            side=s.get("side", ""),
            price=s.get("price", 0.0),
            strength=0.0,
            description=s.get("description", ""),
            lifecycle_state=s.get("lifecycle", ""),
            wave_regime=s.get("wave_regime", ""),
            tide_bias=s.get("tide_bias", ""),
            archetype=s.get("archetype", ""),
        )
        blotter.add_entry(entry)

    check(blotter._model.rowCount() == 5, f"5 signals in blotter (got {blotter._model.rowCount()})")

    # Verify first signal attributes survived the roundtrip
    first = blotter._model._data[0]
    check(first.signal_type == "RIPPLE_ENTER_BOUNCE_LONG",
          f"first signal type (got {first.signal_type})")
    check(first.price == 50000.0, f"first price roundtrip (got {first.price})")
    check(first.source == "replay", f"source is replay (got {first.source})")

    ro.close()


# ====================================================================
# 3. Event timeline reconstruction
# ====================================================================

def test_replay_event_timeline():
    """Read persisted events and verify the session timeline is intact."""
    print("test_replay_event_timeline")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")
    evts = ro.read_events()
    check(len(evts) == 4, f"4 events (got {len(evts)})")

    expected_types = ["CONNECT", "ARM", "DISARM", "DISCONNECT"]
    actual_types = [e["event_type"] for e in evts]
    check(actual_types == expected_types,
          f"event sequence (got {actual_types})")

    # CONNECT details
    connect_details = json.loads(evts[0]["details"])
    check(connect_details.get("symbol") == "BTCUSDT",
          f"connect symbol (got {connect_details})")

    # ARM details
    arm_details = json.loads(evts[1]["details"])
    check(arm_details.get("mode") == "paper",
          f"arm mode (got {arm_details})")

    # Timestamps are monotonically increasing
    timestamps = [e["timestamp"] for e in evts]
    check(all(timestamps[i] <= timestamps[i + 1] for i in range(len(timestamps) - 1)),
          "event timestamps monotonically increasing")

    ro.close()


# ====================================================================
# 4. Deterministic read-back (identical reads)
# ====================================================================

def test_replay_deterministic_readback():
    """Two consecutive reads of the same store produce identical results."""
    print("test_replay_deterministic_readback")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro1 = StrategyStore.open_readonly(path, "BTCUSDT")
    sigs1 = ro1.read_signals()
    snaps1 = ro1.read_snapshots()
    evts1 = ro1.read_events()
    ro1.close()

    ro2 = StrategyStore.open_readonly(path, "BTCUSDT")
    sigs2 = ro2.read_signals()
    snaps2 = ro2.read_snapshots()
    evts2 = ro2.read_events()
    ro2.close()

    check(sigs1 == sigs2, "signals identical across reads")
    check(snaps1 == snaps2, "snapshots identical across reads")
    check(evts1 == evts2, "events identical across reads")


# ====================================================================
# 5. Time-windowed replay (partial replay)
# ====================================================================

def test_replay_time_window():
    """Read only a portion of the session for partial replay."""
    print("test_replay_time_window")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")

    # Signals: first 3 (base+10000, base+15000, base+20000)
    sigs = ro.read_signals(from_ms=base + 10000, to_ms=base + 20000)
    check(len(sigs) == 3, f"3 signals in window (got {len(sigs)})")

    # Snapshots: ts range base+10000 to base+19000 → 4 snapshots
    # (at 10000, 13000, 16000, 19000)
    snaps = ro.read_snapshots(from_ms=base + 10000, to_ms=base + 19000)
    check(len(snaps) == 4, f"4 snapshots in window (got {len(snaps)})")

    # Events: only ARM (base+1000) via narrow window
    evts = ro.read_events(from_ms=base + 500, to_ms=base + 1500)
    check(len(evts) == 1, f"1 event in window (got {len(evts)})")
    check(evts[0]["event_type"] == "ARM", f"event is ARM (got {evts[0]['event_type']})")

    ro.close()


# ====================================================================
# 6. Diagnostics panel from replay snapshot
# ====================================================================

def test_replay_diagnostics_panel():
    """Replay snapshot data populates the diagnostics panel correctly."""
    print("test_replay_diagnostics_panel")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")
    snaps = ro.read_snapshots()
    ro.close()

    panel = StrategyDiagnosticsPanel()
    panel.resize(300, 350)

    # Convert a snapshot dict into a simple namespace for the panel
    snap_dict = snaps[-1]

    class ReplaySnap:
        class wave:
            regime = snap_dict["wave_regime"]
            trend_efficiency = 0.0
        class tide:
            bias = snap_dict["tide_bias"]
        class ripple:
            liquidity_state = snap_dict["liquidity_state"]
            microprice = snap_dict["microprice"]
            imbalance = snap_dict["imbalance"]
        class trade:
            state = snap_dict["lifecycle_state"]
            archetype = ""
            unrealized_pnl = snap_dict["unrealized_pnl"]
            stop_price = 0.0
            target_price = 0.0
            hold_time_ms = 0
            cumulative_pnl = snap_dict["cumulative_pnl"]
        class risk:
            consumed_es = snap_dict["consumed_es"]
            es_budget = snap_dict["es_budget"]

    panel.update_snapshot(ReplaySnap())
    check(len(panel._rows) > 0, "panel rows populated from replay snapshot")

    tide_row = next((r for r in panel._rows if r[0] == "Tide"), None)
    check(tide_row is not None, "Tide row exists")
    check(tide_row[1] == "LONG", f"Tide from replay = LONG (got {tide_row[1]})")

    wave_row = next((r for r in panel._rows if r[0] == "Wave"), None)
    check(wave_row is not None, "Wave row exists")
    check(wave_row[1] == "MEAN_REVERSION",
          f"Wave from replay = MEAN_REVERSION (got {wave_row[1]})")


# ====================================================================
# 7. Heatmap with replay trades + overlay from persisted data
# ====================================================================

def test_replay_heatmap_with_overlay():
    """Simulate loading trades + overlay from persisted session."""
    print("test_replay_heatmap_with_overlay")

    tmpdir = tempfile.mkdtemp()
    path, base = _write_session(tmpdir)

    ro = StrategyStore.open_readonly(path, "BTCUSDT")
    sigs = ro.read_signals()
    snaps = ro.read_snapshots()
    ro.close()

    hm = HeatmapWidget()
    hm.resize(800, 500)

    # Feed synthetic trades at signal prices
    for s in sigs:
        ts = s["timestamp"]
        price = s["price"]
        if price > 0:
            hm.add_trade(ts, price, 0.1, s.get("side") == "BUY")

    check(len(hm._trades) == len([s for s in sigs if s["price"] > 0]),
          "trades added from signals")
    check(len(hm._buckets) >= 1, "buckets populated from replay trades")

    # Set overlay from last snapshot
    last_snap = snaps[-1]
    mp = last_snap["microprice"]
    hm.set_strategy_overlay(entry_price=mp, stop_price=mp - 200,
                            target_price=mp + 400)
    check(hm._overlay_entry_price == mp, "replay overlay entry set")

    # Bucket OHLC reflects replay data
    b = hm._buckets[-1]
    check(b.trade_count > 0, "replay bucket has trades")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_replay_overlay_reconstruction,
        test_replay_signal_playback,
        test_replay_event_timeline,
        test_replay_deterministic_readback,
        test_replay_time_window,
        test_replay_diagnostics_panel,
        test_replay_heatmap_with_overlay,
    ]
    for t in tests:
        t()
    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"Replay overlay tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
