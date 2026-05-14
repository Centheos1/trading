"""Phase 5: End-to-end integration test across all four UI workstreams.

Validates that bucketing (Phase 1), strategy controls (Phase 2),
UI cleanup (Phase 3), and HDF5 persistence (Phase 4) work together
correctly in a single simulated session lifecycle:

  connect → arm → feed trades → emit signals → snapshot → disarm → disconnect
           ↓ bucket model updates   ↓ signal log populates
           ↓ strategy overlay set   ↓ HDF5 persists signals + snapshots + events
           ↓ UI cleanup rules hold  ↓ read-back matches write

This is a cross-cutting integration test, not a unit test for any
single phase.
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
_app = QApplication.instance() or QApplication(sys.argv[:1])

from execution.models import (
    SignalCategory, SignalEntry, StrategyUIState,
)
from strategy_store import StrategyStore
from ui.heatmap_widget import HeatmapWidget
from ui.trade_blotter import TradeBlotter
from ui.strategy_panel import StrategyDiagnosticsPanel
from ui.account_panel import AccountPanel

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
    """Minimal stand-in for a C++ StrategySnapshot."""
    class _Wave:
        regime = "MEAN_REVERSION"
        trend_efficiency = 0.72
    class _Tide:
        bias = "LONG"
    class _Ripple:
        liquidity_state = "BALANCED"
        microprice = 100.12
        imbalance = 0.015
    class _Trade:
        state = "CONFIRMATION"
        archetype = "BOUNCE"
        unrealized_pnl = 0.05
        stop_price = 99.5
        target_price = 101.0
        hold_time_ms = 15000
        cumulative_pnl = 0.12
    class _Risk:
        consumed_es = 0.3
        es_budget = 1.0
    wave = _Wave()
    tide = _Tide()
    ripple = _Ripple()
    trade = _Trade()
    risk = _Risk()


def _make_signal(ts, cat=SignalCategory.RIPPLE_ENTRY, desc="test signal"):
    return SignalEntry(
        timestamp=ts,
        signal_type=cat.value,
        source="ripple",
        category=cat,
        side="BUY",
        price=100.0,
        strength=0.8,
        description=desc,
        lifecycle_state="armed_active",
        wave_regime="MEAN_REVERSION",
        tide_bias="LONG",
    )


# ====================================================================
# 1. Full session lifecycle: bucket + strategy + HDF5
# ====================================================================

def test_session_lifecycle():
    """Simulate connect → arm → trades → signals → snapshots → disarm → disconnect."""
    print("test_session_lifecycle")

    tmpdir = tempfile.mkdtemp()
    symbol = "BTCUSDT"
    now = int(time.time() * 1000)

    # --- connect: open strategy store, write CONNECT event
    store = StrategyStore(symbol, data_dir=tmpdir)
    store.write_event(now, "CONNECT", {"symbol": symbol, "mode": "paper"})

    # --- heatmap: feed trades into bucket model
    hm = HeatmapWidget()
    hm.resize(800, 500)

    trade_prices = [100.0, 100.1, 99.9, 100.2, 100.05]
    trade_qtys = [0.5, 0.3, 0.7, 0.2, 0.4]
    for i, (p, q) in enumerate(zip(trade_prices, trade_qtys)):
        ts = now + i * 200
        is_buy = i % 2 == 0
        hm.add_trade(ts, p, q, is_buy)

    check(len(hm._trades) == 5, f"5 trades added (got {len(hm._trades)})")
    check(len(hm._buckets) >= 1, f"at least 1 bucket (got {len(hm._buckets)})")

    b = hm._buckets[-1]
    check(b.trade_count == 5, f"bucket trade_count=5 (got {b.trade_count})")
    check(b.high == 100.2, f"bucket high=100.2 (got {b.high})")
    check(b.low == 99.9, f"bucket low=99.9 (got {b.low})")

    # --- arm: write ARM event
    store.write_event(now + 1000, "ARM", {"mode": "paper"})

    # --- strategy overlay
    hm.set_strategy_overlay(entry_price=100.0, stop_price=99.5, target_price=101.0)
    check(hm._overlay_entry_price == 100.0, "overlay entry set")
    check(hm._overlay_stop_price == 99.5, "overlay stop set")
    check(hm._overlay_target_price == 101.0, "overlay target set")

    # --- signal log: write signals to blotter and store
    blotter = TradeBlotter()
    signals = []
    for i in range(5):
        sig = _make_signal(now + 2000 + i * 100)
        blotter.add_entry(sig)
        store.write_signal(sig)
        signals.append(sig)

    check(blotter._model.rowCount() == 5, f"blotter has 5 entries (got {blotter._model.rowCount()})")

    # --- snapshots: buffer and flush
    snap = _FakeSnap()
    for i in range(3):
        store.buffer_snapshot(now + 3000 + i * 500, snap)
    store.flush_snapshots()

    # --- disarm: write DISARM event, clear overlay
    hm.clear_strategy_overlay()
    check(hm._overlay_entry_price == 0.0, "overlay cleared after disarm")
    store.write_event(now + 5000, "DISARM", {"was_active": True})

    # --- disconnect: write DISCONNECT, close store
    store.write_event(now + 6000, "DISCONNECT")
    store.close()

    # --- read back: verify all persisted data
    ro = StrategyStore.open_readonly(store.path, symbol)
    check(ro is not None, "readonly store opened")

    sigs = ro.read_signals()
    check(len(sigs) == 5, f"5 signals read back (got {len(sigs)})")
    check(sigs[0]["signal_type"] == SignalCategory.RIPPLE_ENTRY.value,
          f"signal type roundtrip (got {sigs[0]['signal_type']})")
    check(sigs[0]["wave_regime"] == "MEAN_REVERSION",
          f"wave_regime roundtrip (got {sigs[0]['wave_regime']})")

    snaps = ro.read_snapshots()
    check(len(snaps) == 3, f"3 snapshots read back (got {len(snaps)})")
    check(abs(snaps[0]["microprice"] - 100.12) < 0.001,
          f"microprice roundtrip (got {snaps[0]['microprice']})")

    evts = ro.read_events()
    check(len(evts) == 4, f"4 events read back (got {len(evts)})")
    types = [e["event_type"] for e in evts]
    check(types == ["CONNECT", "ARM", "DISARM", "DISCONNECT"],
          f"event sequence correct (got {types})")

    ro.close()


# ====================================================================
# 2. Strategy state + UI cleanup interactions
# ====================================================================

def test_strategy_state_gates_ui():
    """Verify that strategy state changes correctly gate UI elements."""
    print("test_strategy_state_gates_ui")

    from ui.main_window import MainWindow
    w = MainWindow()

    # Initial state: DISARMED, sizing disabled.  Phase 9D removed the
    # ``_tick_size_input`` / ``_imbalance_input`` widgets entirely; the
    # legacy "hidden but present" assertions are now covered by
    # ``test_ui_cleanup.py::test_tick_size_removed`` /
    # ``test_imbalance_removed``.
    check(w._strategy_ui_state == StrategyUIState.DISARMED, "initial DISARMED")
    check(not w._sizing_mode_combo.isEnabled(), "sizing disabled when disarmed")
    check(not hasattr(w, "_tick_size_input"),
          "Phase 9D: _tick_size_input fully removed")
    check(not hasattr(w, "_imbalance_input"),
          "Phase 9D: _imbalance_input fully removed")

    # Arm: sizing enabled
    w._set_strategy_state(StrategyUIState.ARMED_WAITING)
    check(w._sizing_mode_combo.isEnabled(), "sizing enabled when armed")
    check(w._sizing_value_input.isEnabled(), "sizing value enabled when armed")

    # State transitions
    w._set_strategy_state(StrategyUIState.ARMED_ACTIVE)
    check(w._strategy_ui_state == StrategyUIState.ARMED_ACTIVE,
          "state follows transition to ACTIVE")

    # Disarm: sizing disabled again
    w._set_strategy_state(StrategyUIState.DISARMED)
    check(not w._sizing_mode_combo.isEnabled(), "sizing disabled after disarm")


# ====================================================================
# 3. Blotter filtering interop with strategy signals
# ====================================================================

def test_blotter_strategy_filter_with_mixed_signals():
    """Ensure blotter's strategy filter works alongside legacy signals."""
    print("test_blotter_strategy_filter_with_mixed_signals")

    blotter = TradeBlotter()
    now = int(time.time() * 1000)

    # Add mixed signals
    blotter.add_entry(SignalEntry(
        timestamp=now, signal_type="LEGACY_SIG", source="legacy",
        category=SignalCategory.LEGACY_RAW, description="old signal"))
    blotter.add_entry(SignalEntry(
        timestamp=now + 100, signal_type="STRATEGY_ARM", source="strategy",
        category=SignalCategory.STRATEGY_ARM, description="armed"))
    blotter.add_entry(SignalEntry(
        timestamp=now + 200, signal_type="RIPPLE_ENTER_BOUNCE_LONG",
        source="ripple", category=SignalCategory.RIPPLE_ENTRY,
        side="BUY", price=100.0, description="entry"))
    blotter.add_entry(SignalEntry(
        timestamp=now + 300, signal_type="EXEC_FILL", source="execution",
        category=SignalCategory.EXECUTION, description="filled"))

    check(blotter._model.rowCount() == 4, "4 entries in model")

    # Strategy filter: shows ARM + RIPPLE + EXECUTION but not LEGACY_RAW
    from ui.trade_blotter import _FILTER_STRATEGY
    blotter._proxy.set_category_filter(_FILTER_STRATEGY)
    visible = blotter._proxy.rowCount()
    check(visible == 3, f"strategy filter shows 3 rows (got {visible})")

    # Clear filter
    blotter._proxy.set_category_filter(None)
    visible = blotter._proxy.rowCount()
    check(visible == 4, f"all rows visible after clear (got {visible})")


# ====================================================================
# 4. Bucket + overlay coexistence
# ====================================================================

def test_bucket_and_overlay_coexist():
    """Verify bucketed heatmap and strategy overlay can coexist
    without corrupting each other's state."""
    print("test_bucket_and_overlay_coexist")

    hm = HeatmapWidget()
    hm.resize(800, 500)
    now = int(time.time() * 1000)

    # Set overlay
    hm.set_strategy_overlay(entry_price=50000.0, stop_price=49500.0,
                            target_price=50500.0)

    # Feed depth data to initialize heatmap
    bids = [(49990.0, 1.0), (49980.0, 2.0)]
    asks = [(50010.0, 1.0), (50020.0, 2.0)]
    hm.add_depth_column(now, bids, asks, 49990.0, 50010.0, sample_ts=now)

    # Feed trades
    for i in range(20):
        ts = now + i * 50
        p = 50000.0 + (i % 5 - 2) * 10
        hm.add_trade(ts, p, 0.1, i % 2 == 0)

    check(len(hm._buckets) >= 1, "buckets populated")
    check(hm._overlay_entry_price == 50000.0, "overlay survived trade feeding")
    check(hm._overlay_stop_price == 49500.0, "stop overlay intact")
    check(hm._overlay_target_price == 50500.0, "target overlay intact")

    # Verify bucket OHLC is independent of overlay
    b = hm._buckets[-1]
    check(b.trade_count == 20, f"bucket has 20 trades (got {b.trade_count})")
    check(b.open_price == 50000.0 + (0 % 5 - 2) * 10,
          f"bucket open is first trade price")

    # Clear overlay — buckets unaffected
    hm.clear_strategy_overlay()
    check(hm._overlay_entry_price == 0.0, "overlay cleared")
    check(b.trade_count == 20, "bucket unaffected by overlay clear")


# ====================================================================
# 5. HDF5 time-range filtering across signals + snapshots + events
# ====================================================================

def test_hdf5_time_range_filtering_cross_dataset():
    """Verify time-range filters work consistently across all three datasets."""
    print("test_hdf5_time_range_filtering_cross_dataset")

    tmpdir = tempfile.mkdtemp()
    store = StrategyStore("ETHUSDT", data_dir=tmpdir)

    base = 1000000
    for i in range(10):
        ts = base + i * 1000
        store.write_signal(_make_signal(ts, desc=f"sig_{i}"))
        store.buffer_snapshot(ts, _FakeSnap())
        store.write_event(ts, "TICK", {"i": i})
    store.flush_snapshots()

    # Full read
    check(store.signal_count() == 10, "10 signals total")
    check(store.snapshot_count() == 10, "10 snapshots total")
    check(store.event_count() == 10, "10 events total")

    # Time range: ts[3] to ts[6] inclusive
    from_ms = base + 3000
    to_ms = base + 6000
    sigs = store.read_signals(from_ms=from_ms, to_ms=to_ms)
    snaps = store.read_snapshots(from_ms=from_ms, to_ms=to_ms)
    evts = store.read_events(from_ms=from_ms, to_ms=to_ms)

    check(len(sigs) == 4, f"4 signals in range (got {len(sigs)})")
    check(len(snaps) == 4, f"4 snapshots in range (got {len(snaps)})")
    check(len(evts) == 4, f"4 events in range (got {len(evts)})")

    store.close()


# ====================================================================
# 6. Strategy diagnostics panel reflects snapshot
# ====================================================================

def test_diagnostics_panel_from_snapshot():
    """Verify StrategyDiagnosticsPanel renders all expected rows from a snapshot."""
    print("test_diagnostics_panel_from_snapshot")

    panel = StrategyDiagnosticsPanel()
    panel.resize(300, 350)

    snap = _FakeSnap()
    panel.update_snapshot(snap)

    expected_labels = {"Tide", "Wave", "\u03b7", "Liquidity", "\u00b5Price",
                       "Trade", "Archetype", "uPnL", "Stop", "Target",
                       "Hold", "ES Used", "Imbal"}
    actual_labels = {r[0] for r in panel._rows}
    check(expected_labels.issubset(actual_labels),
          f"all expected rows present (missing: {expected_labels - actual_labels})")

    # Specific values
    tide_row = next(r for r in panel._rows if r[0] == "Tide")
    check(tide_row[1] == "LONG", f"Tide=LONG (got {tide_row[1]})")

    wave_row = next(r for r in panel._rows if r[0] == "Wave")
    check(wave_row[1] == "MEAN_REVERSION", f"Wave=MEAN_REVERSION (got {wave_row[1]})")

    # State badge
    panel.set_strategy_state(StrategyUIState.ARMED_ACTIVE)
    check(panel._strategy_state == StrategyUIState.ARMED_ACTIVE, "badge state set")


# ====================================================================
# 7. Deterministic bucket state on identical replays
# ====================================================================

def test_deterministic_bucket_replay():
    """Two identical trade sequences must produce identical bucket state."""
    print("test_deterministic_bucket_replay")

    def run_replay():
        hm = HeatmapWidget()
        hm.resize(800, 500)
        base_ts = 1700000000000
        for i in range(100):
            ts = base_ts + i * 600
            price = 50000.0 + (i % 7 - 3) * 5.0
            qty = 0.01 * (1 + i % 3)
            is_buy = i % 2 == 0
            hm.add_trade(ts, price, qty, is_buy)
        return [(b.bucket_ts, b.open_price, b.close_price, b.high, b.low,
                 b.volume, b.trade_count) for b in hm._buckets]

    r1 = run_replay()
    r2 = run_replay()
    check(r1 == r2, "bucket state identical across replays")
    check(len(r1) >= 1, f"at least 1 bucket produced (got {len(r1)})")


# ====================================================================
# 8. Account panel + strategy lifecycle
# ====================================================================

def test_account_panel_lifecycle():
    """Account panel transitions through connect/disconnect cycle."""
    print("test_account_panel_lifecycle")

    panel = AccountPanel()
    check(not panel._connected, "initially disconnected")
    check(not panel._placeholder.isHidden(), "placeholder visible")

    panel.update_account(1000.0, 900.0, [])
    check(panel._connected, "connected after update")
    check(panel._placeholder.isHidden(), "placeholder hidden after connect")

    panel.clear()
    check(not panel._connected, "disconnected after clear")
    check(not panel._placeholder.isHidden(), "placeholder visible after clear")


# ====================================================================
# Run all
# ====================================================================

if __name__ == "__main__":
    tests = [
        test_session_lifecycle,
        test_strategy_state_gates_ui,
        test_blotter_strategy_filter_with_mixed_signals,
        test_bucket_and_overlay_coexist,
        test_hdf5_time_range_filtering_cross_dataset,
        test_diagnostics_panel_from_snapshot,
        test_deterministic_bucket_replay,
        test_account_panel_lifecycle,
    ]
    for t in tests:
        t()
    total = PASS + FAIL
    print(f"\n{'='*60}")
    print(f"Integration E2E tests: {PASS}/{total} passed, {FAIL} failed")
    if FAIL:
        print("SOME TESTS FAILED")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")
