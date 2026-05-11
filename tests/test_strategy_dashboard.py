"""Tests for the Phase-7 strategy dashboard additions:

- C1: MarketState.snapshot_history deque (slot, default maxlen, custom maxlen)
- C2: _StrategyHistoryPanel (paint, snapshot ingestion, robustness)
- C3: _RippleStateTable (per-field updates, missing-snapshot fallback)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPainter, QPixmap, QColor

_app = QApplication.instance() or QApplication(sys.argv[:1])

from collections import deque

from ui.market_state import MarketState
from ui.strategy_dashboard_view import (
    StrategyDashboardView,
    _StrategyHistoryPanel,
    _RippleStateTable,
    _RiskGateStatusBar,
    _LayeredWiringIndicator,
)
from schemas import (
    StrategySnapshot, TideSnapshot, WaveSnapshot,
    RippleStateSnapshot, TradeStateSnapshot, RiskBudgetSnapshot,
    TideBias, WaveRegime, LifecycleState, TradeArchetype, TradeSide,
    PermissionSet,
)


PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


def _make_snapshot(*,
                   bias=TideBias.NEUTRAL,
                   regime=WaveRegime.NEUTRAL,
                   trade_state=LifecycleState.SETUP,
                   archetype=TradeArchetype.BOUNCE,
                   entry=0.0, stop=0.0, target=0.0,
                   upnl=0.0, hold_ms=0,
                   consumed_es=0.0, es_budget=0.0) -> StrategySnapshot:
    return StrategySnapshot(
        timestamp=0,
        tide=TideSnapshot(bias=bias, risk_multiplier=1.0,
                          max_position_usd=10_000.0, es_budget=es_budget),
        wave=WaveSnapshot(regime=regime, trend_efficiency=0.5,
                          permissions=PermissionSet()),
        ripple=RippleStateSnapshot(),
        trade=TradeStateSnapshot(state=trade_state, archetype=archetype,
                                 side=TradeSide.LONG,
                                 entry_price=entry, stop_price=stop,
                                 target_price=target,
                                 unrealized_pnl=upnl, hold_time_ms=hold_ms),
        risk=RiskBudgetSnapshot(consumed_es=consumed_es,
                                es_budget=es_budget),
    )


# ────────────────────────────────────────────── C1: MarketState history

def test_snapshot_history_slot_present():
    ms = MarketState()
    check(hasattr(ms, "snapshot_history"),
          "MarketState.snapshot_history exists")
    check(isinstance(ms.snapshot_history, deque),
          "snapshot_history is a deque")


def test_snapshot_history_default_maxlen():
    ms = MarketState()
    check(ms.snapshot_history.maxlen == MarketState.SNAPSHOT_HISTORY_MAXLEN,
          f"default maxlen is {MarketState.SNAPSHOT_HISTORY_MAXLEN}")


def test_snapshot_history_custom_maxlen():
    ms = MarketState(snapshot_history_maxlen=42)
    check(ms.snapshot_history.maxlen == 42,
          f"custom maxlen 42 (got {ms.snapshot_history.maxlen})")


def test_snapshot_history_bounded():
    ms = MarketState(snapshot_history_maxlen=10)
    snap = _make_snapshot()
    for i in range(50):
        ms.snapshot_history.append((i, snap))
    check(len(ms.snapshot_history) == 10,
          f"deque bounded to maxlen (got {len(ms.snapshot_history)})")
    # Newest entries retained
    last_ts = ms.snapshot_history[-1][0]
    check(last_ts == 49, f"newest entry kept (last_ts {last_ts})")


def test_snapshot_history_starts_empty():
    ms = MarketState()
    check(len(ms.snapshot_history) == 0,
          "snapshot_history empty on construction")


# ────────────────────────────────────────── C2: _StrategyHistoryPanel

def test_history_panel_construct():
    ms = MarketState()
    panel = _StrategyHistoryPanel(ms)
    check(panel._ms is ms, "history panel holds MarketState reference")
    check(panel.minimumHeight() >= 60,
          f"panel has min height (got {panel.minimumHeight()})")


def test_history_panel_paints_with_no_data():
    """Empty history must not raise — shows the 'waiting' message."""
    ms = MarketState()
    panel = _StrategyHistoryPanel(ms)
    panel.resize(400, 120)
    # Render to a pixmap to drive paintEvent
    pm = QPixmap(400, 120)
    panel.render(pm)
    check(True, "paint with empty history did not raise")


def test_history_panel_paints_with_history():
    ms = MarketState()
    snap_long_breakout = _make_snapshot(
        bias=TideBias.LONG, regime=WaveRegime.BREAKOUT,
        consumed_es=30.0, es_budget=100.0, upnl=5.0)
    snap_short_breakdown = _make_snapshot(
        bias=TideBias.SHORT, regime=WaveRegime.BREAKDOWN,
        consumed_es=80.0, es_budget=100.0, upnl=-3.0)
    ms.snapshot_history.append((1_000, snap_long_breakout))
    ms.snapshot_history.append((1_500, snap_short_breakdown))
    ms.snapshot_history.append((2_000, snap_long_breakout))
    panel = _StrategyHistoryPanel(ms)
    panel.resize(400, 120)
    pm = QPixmap(400, 120)
    panel.render(pm)
    check(True, "paint with 3 snapshots did not raise")


def test_history_panel_handles_zero_budget():
    """Risk pct path must handle es_budget==0 (division-safe)."""
    ms = MarketState()
    snap = _make_snapshot(consumed_es=0.0, es_budget=0.0)
    for i in range(5):
        ms.snapshot_history.append((i * 100, snap))
    panel = _StrategyHistoryPanel(ms)
    panel.resize(400, 120)
    pm = QPixmap(400, 120)
    panel.render(pm)
    check(True, "paint with zero risk budget did not raise")


def test_history_panel_handles_zero_pnl_span():
    """All-zero PnL series must not raise (avoid div-by-zero)."""
    ms = MarketState()
    for i in range(5):
        ms.snapshot_history.append(
            (i * 100, _make_snapshot(upnl=0.0,
                                     consumed_es=10.0, es_budget=100.0)))
    panel = _StrategyHistoryPanel(ms)
    panel.resize(400, 120)
    pm = QPixmap(400, 120)
    panel.render(pm)
    check(True, "paint with zero-span PnL did not raise")


def test_history_panel_resizes():
    ms = MarketState()
    snap = _make_snapshot(bias=TideBias.LONG, regime=WaveRegime.BREAKOUT,
                          consumed_es=20.0, es_budget=100.0, upnl=1.0)
    for i in range(20):
        ms.snapshot_history.append((i * 100, snap))
    panel = _StrategyHistoryPanel(ms)
    for w, h in [(200, 100), (400, 120), (800, 220), (60, 80)]:
        panel.resize(w, h)
        pm = QPixmap(max(w, 1), max(h, 1))
        panel.render(pm)
    check(True, "paint at multiple sizes did not raise")


# ──────────────────────────────────────────── C3: _RippleStateTable

def test_ripple_table_construct():
    ms = MarketState()
    table = _RippleStateTable(ms)
    # 8 fields specified in _LABELS
    check(len(table._value_labels) == 8,
          f"8 value labels (got {len(table._value_labels)})")
    for label_name in ("Lifecycle", "Archetype", "Entry",
                       "Stop", "Target", "Hold", "uPnL", "ES Used"):
        check(label_name in table._value_labels,
              f"field '{label_name}' present")


def test_ripple_table_no_snapshot_shows_dashes():
    ms = MarketState()
    ms.strategy_snapshot = None
    table = _RippleStateTable(ms)
    table.update_from_state()
    for k, v in table._value_labels.items():
        check(v.text() == "\u2014",
              f"{k} shows em-dash with no snapshot (got '{v.text()}')")


def test_ripple_table_updates_from_snapshot():
    ms = MarketState()
    ms.strategy_snapshot = _make_snapshot(
        trade_state=LifecycleState.EXPANSION,
        archetype=TradeArchetype.BREAKOUT,
        entry=83000.5, stop=82500.0, target=84000.0,
        upnl=12.34, hold_ms=125_000,
        consumed_es=45.0, es_budget=100.0,
    )
    table = _RippleStateTable(ms)
    table.update_from_state()
    check(table._value_labels["Lifecycle"].text() == "EXPANSION",
          f"Lifecycle = EXPANSION (got {table._value_labels['Lifecycle'].text()})")
    check(table._value_labels["Archetype"].text() == "BREAKOUT",
          "Archetype = BREAKOUT")
    check("83000" in table._value_labels["Entry"].text(),
          f"Entry shows price (got {table._value_labels['Entry'].text()})")
    check("82500" in table._value_labels["Stop"].text(),
          "Stop shows price")
    check("84000" in table._value_labels["Target"].text(),
          "Target shows price")
    check(table._value_labels["Hold"].text() == "02:05",
          f"Hold formats MM:SS (got {table._value_labels['Hold'].text()})")
    check("12" in table._value_labels["uPnL"].text(),
          "uPnL shows positive value")
    check(table._value_labels["ES Used"].text() == "45%",
          f"ES Used shows pct (got {table._value_labels['ES Used'].text()})")


def test_ripple_table_negative_upnl_color():
    ms = MarketState()
    ms.strategy_snapshot = _make_snapshot(upnl=-5.0,
                                          consumed_es=10.0, es_budget=100.0)
    table = _RippleStateTable(ms)
    table.update_from_state()
    style = table._value_labels["uPnL"].styleSheet()
    check("e65050" in style,
          f"negative uPnL gets red color (got '{style}')")


def test_ripple_table_es_pct_thresholds():
    ms = MarketState()
    table = _RippleStateTable(ms)
    # Below 50%: green
    ms.strategy_snapshot = _make_snapshot(consumed_es=20.0, es_budget=100.0)
    table.update_from_state()
    style = table._value_labels["ES Used"].styleSheet()
    check("28dc82" in style, f"<50% ES is green (got '{style}')")
    # 50-80%: yellow/orange
    ms.strategy_snapshot = _make_snapshot(consumed_es=60.0, es_budget=100.0)
    table.update_from_state()
    style = table._value_labels["ES Used"].styleSheet()
    check("ffa53c" in style, f"50-80% ES is orange (got '{style}')")
    # >80%: red
    ms.strategy_snapshot = _make_snapshot(consumed_es=90.0, es_budget=100.0)
    table.update_from_state()
    style = table._value_labels["ES Used"].styleSheet()
    check("e65050" in style, f">80% ES is red (got '{style}')")


# ─────────────────────────────────────── StrategyDashboardView wiring

def test_dashboard_has_history_and_table():
    ms = MarketState()
    view = StrategyDashboardView(ms)
    check(view.history_panel is not None, "dashboard exposes history_panel")
    check(view.ripple_table is not None, "dashboard exposes ripple_table")
    check(isinstance(view.history_panel, _StrategyHistoryPanel),
          "history_panel is correct type")
    check(isinstance(view.ripple_table, _RippleStateTable),
          "ripple_table is correct type")


def test_dashboard_update_from_state_runs():
    ms = MarketState()
    ms.strategy_snapshot = _make_snapshot(
        bias=TideBias.LONG, regime=WaveRegime.BREAKOUT,
        upnl=2.5, consumed_es=20.0, es_budget=100.0)
    ms.snapshot_history.append((1_000, ms.strategy_snapshot))
    view = StrategyDashboardView(ms)
    view.update_from_state()
    # Confirm ripple table updated
    check(view.ripple_table._value_labels["Lifecycle"].text() != "\u2014",
          "ripple table updated via dashboard")


def test_dashboard_update_with_no_snapshot():
    """update_from_state must be safe with strategy_snapshot=None."""
    ms = MarketState()
    ms.strategy_snapshot = None
    view = StrategyDashboardView(ms)
    view.update_from_state()
    # Should not crash; ripple table should show dashes
    check(view.ripple_table._value_labels["Lifecycle"].text() == "\u2014",
          "no-snapshot path leaves dashes")


# ─────────────────────── Phase 11: history-panel cache regression tests
#
# Phase 7 introduced an O(N) cost per paint of `_StrategyHistoryPanel`
# because every paintEvent walked the full `snapshot_history` (up to 600
# entries) through reflective `_attr()` lookups. With the live timer
# triggering a paint every 100 ms and history filling to 600, the live
# tick avg grew from ~11 ms to ~145 ms over the first minute. Phase 11
# adds a `(len, last_ts)`-keyed cache so repeated paints with unchanged
# history are O(1).


def test_history_panel_cache_starts_empty():
    ms = MarketState()
    panel = _StrategyHistoryPanel(ms)
    check(panel._cache_key is None,
          "cache key starts as None (no paint yet)")


def test_history_panel_cache_populates_on_first_paint():
    ms = MarketState()
    snap = _make_snapshot(bias=TideBias.LONG, regime=WaveRegime.BREAKOUT,
                          upnl=2.5, consumed_es=20.0, es_budget=100.0)
    ms.snapshot_history.append((1_000, snap))
    ms.snapshot_history.append((2_000, snap))
    panel = _StrategyHistoryPanel(ms)
    pix = QPixmap(400, 200)
    pix.fill(QColor("black"))
    p = QPainter(pix)
    panel._paint(p)
    p.end()
    check(panel._cache_key == (2, 2_000),
          f"cache key = (n=2, last_ts=2000) after paint "
          f"(got {panel._cache_key})")
    check(len(panel._cache_wave_colors) == 2,
          "wave_colors cached for both samples")
    check(len(panel._cache_risk_pcts) == 2,
          "risk_pcts cached for both samples")
    check(panel._cache_risk_pcts[0] == 20.0,
          f"risk_pct cached correctly (got {panel._cache_risk_pcts[0]})")


def test_history_panel_cache_reused_when_unchanged():
    """Two paints with the same history must NOT rebuild the cache."""
    ms = MarketState()
    snap = _make_snapshot(consumed_es=20.0, es_budget=100.0, upnl=1.0)
    ms.snapshot_history.append((1_000, snap))
    ms.snapshot_history.append((2_000, snap))
    panel = _StrategyHistoryPanel(ms)

    rebuild_count = {"n": 0}
    original = panel._refresh_cache

    def counting_refresh(history):
        rebuild_count["n"] += 1
        original(history)

    panel._refresh_cache = counting_refresh

    pix = QPixmap(400, 200)
    pix.fill(QColor("black"))
    p = QPainter(pix)
    panel._paint(p); p.end()
    first = rebuild_count["n"]

    # Five more paints, same history
    for _ in range(5):
        p = QPainter(pix)
        panel._paint(p); p.end()
    after = rebuild_count["n"]

    check(first == 1, f"first paint refreshes cache (got {first})")
    check(after == first,
          f"5 subsequent paints with unchanged history must NOT rebuild "
          f"cache (rebuilds: first={first}, after={after})")


def test_history_panel_cache_invalidates_on_append():
    ms = MarketState()
    snap = _make_snapshot(consumed_es=20.0, es_budget=100.0)
    ms.snapshot_history.append((1_000, snap))
    ms.snapshot_history.append((2_000, snap))
    panel = _StrategyHistoryPanel(ms)
    pix = QPixmap(400, 200); pix.fill(QColor("black"))
    p = QPainter(pix); panel._paint(p); p.end()
    key_before = panel._cache_key

    # Append a new snapshot with a new ts
    ms.snapshot_history.append((3_000, snap))
    p = QPainter(pix); panel._paint(p); p.end()
    key_after = panel._cache_key

    check(key_before == (2, 2_000),
          f"cache key before = (2, 2000) (got {key_before})")
    check(key_after == (3, 3_000),
          f"cache key after append = (3, 3000) (got {key_after})")
    check(len(panel._cache_wave_colors) == 3,
          "cache rebuilt with 3 entries after append")


def test_history_panel_cache_handles_empty_history():
    ms = MarketState()
    panel = _StrategyHistoryPanel(ms)
    pix = QPixmap(400, 200); pix.fill(QColor("black"))
    p = QPainter(pix); panel._paint(p); p.end()
    check(panel._cache_n == 0, "cache_n is 0 for empty history")
    # Must not crash on a second paint either
    p = QPainter(pix); panel._paint(p); p.end()
    check(True, "second paint of empty history does not crash")


def test_history_panel_invalidate_cache_helper():
    ms = MarketState()
    snap = _make_snapshot(consumed_es=20.0, es_budget=100.0)
    ms.snapshot_history.append((1_000, snap))
    ms.snapshot_history.append((2_000, snap))
    panel = _StrategyHistoryPanel(ms)
    pix = QPixmap(400, 200); pix.fill(QColor("black"))
    p = QPainter(pix); panel._paint(p); p.end()
    check(panel._cache_key is not None, "cache populated after paint")
    panel.invalidate_cache()
    check(panel._cache_key is None,
          "invalidate_cache() drops the cache key")


# ─────────────────────── Phase 14F.4: risk-gate diagnostics


def test_risk_gate_bar_idle_text():
    bar = _RiskGateStatusBar()
    check(bar.text == _RiskGateStatusBar._IDLE_TEXT,
          f"idle bar shows placeholder (got '{bar.text}')")
    check(bar.last_count == 0, "idle bar has count=0")
    check(bar.last_reason == "", "idle bar has empty reason")


def test_risk_gate_bar_renders_reason_and_count():
    bar = _RiskGateStatusBar()
    bar.update_block_status(
        "ES_EXHAUSTED", ts_ms=1_000_000, count=3, now_ms=1_005_000)
    txt = bar.text
    check("ES_EXHAUSTED" in txt,
          f"bar text contains reason (got '{txt}')")
    check("5s ago" in txt, f"bar text shows age in seconds (got '{txt}')")
    check("3" in txt, f"bar text shows count (got '{txt}')")
    check(bar.last_reason == "ES_EXHAUSTED",
          f"last_reason recorded (got '{bar.last_reason}')")
    check(bar.last_count == 3,
          f"last_count recorded (got {bar.last_count})")


def test_risk_gate_bar_age_formats_minutes():
    bar = _RiskGateStatusBar()
    bar.update_block_status(
        "WAVE_DISABLED", ts_ms=1_000_000, count=1,
        now_ms=1_000_000 + 125_000)  # 125 s = 2m 5s
    txt = bar.text
    check("2m 5s ago" in txt,
          f"bar formats minutes + seconds (got '{txt}')")


def test_risk_gate_bar_recent_block_is_highlighted():
    bar = _RiskGateStatusBar()
    bar.update_block_status(
        "TIDE_CRISIS", ts_ms=1_000_000, count=1, now_ms=1_002_000)
    style = bar._label.styleSheet()
    check("ffa53c" in style.lower(),
          f"recent (<10s) block highlighted in orange (got '{style}')")


def test_risk_gate_bar_old_block_is_muted():
    bar = _RiskGateStatusBar()
    bar.update_block_status(
        "MAX_POSITION", ts_ms=1_000_000, count=1, now_ms=1_030_000)
    style = bar._label.styleSheet()
    check("c8c8d8" in style.lower(),
          f"old (>10s) block uses muted color (got '{style}')")


def test_risk_gate_bar_resets_to_idle_when_count_zero():
    bar = _RiskGateStatusBar()
    bar.update_block_status("X", 1_000, 1, now_ms=2_000)
    check("X" in bar.text, "primed with a block")
    bar.update_block_status("", 0, 0)
    check(bar.text == _RiskGateStatusBar._IDLE_TEXT,
          f"reset to idle when count=0 (got '{bar.text}')")


def test_dashboard_proxies_block_status_to_bar():
    ms = MarketState()
    view = StrategyDashboardView(ms)
    view.update_block_status("ES_EXHAUSTED", 100_000, 7)
    check(view.risk_gate_bar.last_reason == "ES_EXHAUSTED",
          "dashboard proxied reason to risk-gate bar")
    check(view.risk_gate_bar.last_count == 7,
          f"dashboard proxied count (got {view.risk_gate_bar.last_count})")


def test_session_record_block_increments_counters():
    """The session-level record_block tracks per-reason and total."""
    from ui.live_trading_session import LiveTradingSession

    class _FakeMW:
        _websockets_module = None
    session = LiveTradingSession(_FakeMW())
    check(session.block_count == 0, "session starts with 0 blocks")
    session.record_block("ES_EXHAUSTED", 100_000)
    session.record_block("ES_EXHAUSTED", 101_000)
    session.record_block("WAVE_DISABLED", 102_000)
    reason, ts_ms, count = session.block_status()
    check(reason == "WAVE_DISABLED",
          f"last_reason latches most recent (got '{reason}')")
    check(ts_ms == 102_000,
          f"last_ts_ms matches most recent (got {ts_ms})")
    check(count == 3, f"total count = 3 (got {count})")
    by_reason = session.block_counts_by_reason
    check(by_reason.get("ES_EXHAUSTED") == 2,
          f"per-reason counter for ES_EXHAUSTED=2 "
          f"(got {by_reason.get('ES_EXHAUSTED')})")
    check(by_reason.get("WAVE_DISABLED") == 1,
          f"per-reason counter for WAVE_DISABLED=1 "
          f"(got {by_reason.get('WAVE_DISABLED')})")


def test_session_record_block_ignores_empty_reason():
    """Defensive: a None/empty reason is a no-op."""
    from ui.live_trading_session import LiveTradingSession

    class _FakeMW:
        _websockets_module = None
    session = LiveTradingSession(_FakeMW())
    session.record_block("", 100_000)
    session.record_block(None, 101_000)  # type: ignore[arg-type]
    check(session.block_count == 0,
          f"empty/None reasons are no-ops (got {session.block_count})")


# ─────────────────────── Phase 14F.5: layered-wiring indicator


def test_wiring_indicator_starts_in_default_state():
    ind = _LayeredWiringIndicator()
    for layer in ("tide", "wave", "rv"):
        check(not ind.is_live(layer),
              f"{layer} indicator starts as default")
        txt = ind.labels[layer].text()
        check("\u25cb" in txt,
              f"{layer} label contains \u25cb glyph (got '{txt}')")


def test_wiring_indicator_flips_to_live_once():
    ind = _LayeredWiringIndicator()
    ind.update_wiring({"tide": 1, "wave": 0, "rv": 0})
    check(ind.is_live("tide"), "tide flipped to live")
    check(not ind.is_live("wave"), "wave still default")
    check(not ind.is_live("rv"), "rv still default")
    txt = ind.labels["tide"].text()
    check("\u25cf" in txt,
          f"tide label uses filled \u25cf after flip (got '{txt}')")
    style = ind.labels["tide"].styleSheet()
    check("28dc82" in style.lower(),
          f"tide label coloured green after flip (got '{style}')")


def test_wiring_indicator_flips_all_three_layers():
    ind = _LayeredWiringIndicator()
    ind.update_wiring({"tide": 2, "wave": 5, "rv": 12})
    for layer in ("tide", "wave", "rv"):
        check(ind.is_live(layer), f"{layer} flipped to live")


def test_wiring_indicator_does_not_revert_on_zero_after_live():
    """A transient failure that stops incrementing the count must NOT
    revert the indicator to default. Once live, always live (until
    session restart)."""
    ind = _LayeredWiringIndicator()
    ind.update_wiring({"tide": 1, "wave": 1, "rv": 1})
    # Now an update where counts unexpectedly drop (e.g. a stale read).
    ind.update_wiring({"tide": 0, "wave": 0, "rv": 0})
    for layer in ("tide", "wave", "rv"):
        check(ind.is_live(layer),
              f"{layer} stays live after subsequent zero count")


def test_dashboard_proxies_wiring_to_indicator():
    ms = MarketState()
    view = StrategyDashboardView(ms)
    view.update_wiring({"tide": 0, "wave": 1, "rv": 0})
    check(view.wiring_indicator.is_live("wave"),
          "dashboard proxied wave wiring to indicator")
    check(not view.wiring_indicator.is_live("tide"),
          "tide stays default")


def test_session_layered_push_status_returns_zero_at_start():
    from ui.live_trading_session import LiveTradingSession

    class _FakeMW:
        _websockets_module = None
    session = LiveTradingSession(_FakeMW())
    status = session.layered_push_status()
    check(status == {"tide": 0, "wave": 0, "rv": 0},
          f"layered_push_status starts at zero (got {status})")


def test_session_layered_push_status_reflects_counters():
    from ui.live_trading_session import LiveTradingSession

    class _FakeMW:
        _websockets_module = None
    session = LiveTradingSession(_FakeMW())
    session._layered_pushes_tide = 3
    session._layered_pushes_wave = 7
    session._layered_pushes_rv = 11
    status = session.layered_push_status()
    check(status == {"tide": 3, "wave": 7, "rv": 11},
          f"layered_push_status mirrors counters (got {status})")


# ─────────────────────── Phase 11: dashboard repaint-gate regression tests


def test_should_repaint_returns_false_when_window_hidden():
    from ui.live_trading_session import LiveTradingSession
    fn = LiveTradingSession._should_repaint_strategy_dashboard
    check(fn(False, False, True) is False,
          "hidden window: no repaint even on refresh")
    check(fn(False, True, True) is False,
          "hidden window: no repaint even on refresh + prev_visible")
    check(fn(False, False, False) is False,
          "hidden window: no repaint when nothing changed")


def test_should_repaint_only_on_strat_refresh():
    from ui.live_trading_session import LiveTradingSession
    fn = LiveTradingSession._should_repaint_strategy_dashboard
    # Window visible, no refresh, was already visible → skip (the perf win)
    check(fn(True, True, False) is False,
          "visible+steady+no-refresh: SKIP (this is the gate that saves "
          "us from the 5x over-repaint)")
    # Window visible, refresh fired → repaint
    check(fn(True, True, True) is True,
          "visible+steady+refresh: repaint")


def test_should_repaint_on_visibility_rising_edge():
    from ui.live_trading_session import LiveTradingSession
    fn = LiveTradingSession._should_repaint_strategy_dashboard
    # Just opened: visible now, was hidden last tick → force one repaint
    # so the user sees current state immediately.
    check(fn(True, False, False) is True,
          "rising-edge of visibility: repaint once even without refresh")
    check(fn(True, False, True) is True,
          "rising-edge + refresh: repaint")


# ────────────────────────────────────────────────────────────── MAIN

if __name__ == '__main__':
    tests = [
        # C1
        test_snapshot_history_slot_present,
        test_snapshot_history_default_maxlen,
        test_snapshot_history_custom_maxlen,
        test_snapshot_history_bounded,
        test_snapshot_history_starts_empty,
        # C2
        test_history_panel_construct,
        test_history_panel_paints_with_no_data,
        test_history_panel_paints_with_history,
        test_history_panel_handles_zero_budget,
        test_history_panel_handles_zero_pnl_span,
        test_history_panel_resizes,
        # C3
        test_ripple_table_construct,
        test_ripple_table_no_snapshot_shows_dashes,
        test_ripple_table_updates_from_snapshot,
        test_ripple_table_negative_upnl_color,
        test_ripple_table_es_pct_thresholds,
        # Dashboard
        test_dashboard_has_history_and_table,
        test_dashboard_update_from_state_runs,
        test_dashboard_update_with_no_snapshot,
        # Phase 11 — history-panel cache
        test_history_panel_cache_starts_empty,
        test_history_panel_cache_populates_on_first_paint,
        test_history_panel_cache_reused_when_unchanged,
        test_history_panel_cache_invalidates_on_append,
        test_history_panel_cache_handles_empty_history,
        test_history_panel_invalidate_cache_helper,
        # Phase 11 — dashboard repaint gate
        test_should_repaint_returns_false_when_window_hidden,
        test_should_repaint_only_on_strat_refresh,
        test_should_repaint_on_visibility_rising_edge,
        # Phase 14F.4 — risk-gate diagnostics
        test_risk_gate_bar_idle_text,
        test_risk_gate_bar_renders_reason_and_count,
        test_risk_gate_bar_age_formats_minutes,
        test_risk_gate_bar_recent_block_is_highlighted,
        test_risk_gate_bar_old_block_is_muted,
        test_risk_gate_bar_resets_to_idle_when_count_zero,
        test_dashboard_proxies_block_status_to_bar,
        test_session_record_block_increments_counters,
        test_session_record_block_ignores_empty_reason,
        # Phase 14F.5 — layered-wiring indicator
        test_wiring_indicator_starts_in_default_state,
        test_wiring_indicator_flips_to_live_once,
        test_wiring_indicator_flips_all_three_layers,
        test_wiring_indicator_does_not_revert_on_zero_after_live,
        test_dashboard_proxies_wiring_to_indicator,
        test_session_layered_push_status_returns_zero_at_start,
        test_session_layered_push_status_reflects_counters,
    ]

    for t in tests:
        print(f"  {t.__name__} ...", end=" ")
        try:
            t()
            print("OK")
        except Exception as e:
            FAIL += 1
            print(f"EXCEPTION: {e}")
            import traceback
            traceback.print_exc()

    total = PASS + FAIL
    print(f"\n{'=' * 50}")
    print(f"Strategy dashboard tests: {PASS}/{total} checks passed")
    if FAIL:
        print(f"  {FAIL} FAILURES")
        sys.exit(1)
    else:
        print("  All passed.")
        sys.exit(0)
