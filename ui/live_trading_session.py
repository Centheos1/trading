"""Live market session: C++ orderflow engine, WebSockets, trade buffer, tick pipeline.

Owns all non-UI market ingestion and engine interaction. ``MainWindow`` wires
widgets and strategy/execution; this class is the ViewModel/session layer for
live Binance USD-M feeds.
"""
from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional

from data_feed import (
    BINANCE_FUTURES_USDM_DEPTH_URL,
    BINANCE_FUTURES_USDM_KLINES_URL,
    DEFAULT_KLINES_LIMIT,
    depth_book_to_engine_update,
    fetch_and_build_depth_snapshot,
    fetch_binance_depth_book,
    fetch_binance_klines,
)
from data_feed.binance_futures_ws import run_binance_usdm_futures_ws_feed
from data_feed.stream_health import FeedState, FeedStreamHealth
from execution.models import (
    compute_realized_vol_from_prices,
    wave_snapshot_to_ofe,
)
from tide.tide_engine import TideEngine
from wave.wave_engine import WaveEngine

if TYPE_CHECKING:
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)

_OFE_BUILD = os.path.join(
    os.path.dirname(__file__), "..", "backtestingCpp", "orderflow", "build"
)
if _OFE_BUILD not in sys.path:
    sys.path.insert(0, os.path.normpath(_OFE_BUILD))

try:
    import orderflow_engine as ofe
except ImportError:
    ofe = None


@dataclass
class _HealthSnapshot:
    """Lightweight per-tick health telemetry."""

    tick_count: int = 0
    trades_drained_total: int = 0
    last_trade_buffer_depth: int = 0
    last_signal_log_size: int = 0
    last_ripple_state: str = ""
    last_ripple_state_age_ms: int = 0
    last_emitted_decision_ts: int = 0
    last_candidate_decision_ts: int = 0
    tick_duration_ms_avg: float = 0.0
    tick_duration_ms_max: float = 0.0
    vp_recomputes: int = 0
    status_updates: int = 0
    badge_updates: int = 0
    phase_trade_ms: float = 0.0
    phase_book_ms: float = 0.0
    phase_vp_cvd_ms: float = 0.0
    _recent_durations: deque = field(default_factory=lambda: deque(maxlen=100))

    def record_tick(self, duration_ms: float):
        self.tick_count += 1
        self._recent_durations.append(duration_ms)
        n = len(self._recent_durations)
        if n > 0:
            self.tick_duration_ms_avg = sum(self._recent_durations) / n
            self.tick_duration_ms_max = max(self._recent_durations)


class LiveTradingSession:
    """Engine + live feeds + timer-tick pipeline (no QWidget)."""

    _MAX_TRADES_PER_TICK = 2000
    _VP_RECOMPUTE_EVERY = 5
    _STATUS_PANEL_EVERY = 10
    _FEED_BADGE_EVERY = 5
    _BOOK_EMPTY_RESYNC_THRESHOLD = 30
    _BOOK_HEALTH_LOG_INTERVAL = 50
    _BUBBLE_DEAD_THRESHOLD_TICKS = 50

    # Phase 14B — layered-strategy push cadences. UI timer fires every
    # 100 ms, so the per-tick multipliers below resolve to wall-clock
    # cadences that match `strategy.md` §5.3 + AGENT_STRATEGY_RULES.md
    # §7.5. The Wave/Tide engines remain in event-time internally; this
    # cadence is only the *push* throttle from Python → C++ engine.
    _RV_PUSH_EVERY = 10     # 100 ms × 10 = 1 s — realized vol → engine
    _WAVE_PUSH_EVERY = 50   # 100 ms × 50 = 5 s — Wave snapshot → engine
    _TIDE_PUSH_EVERY = 600  # 100 ms × 600 = 60 s — Tide budget → engine
    _RV_PRICE_BUF_LEN = 60  # rolling window for realized-vol estimate

    _FEED_BADGE_STYLES = {
        FeedState.LIVE: "color: #00cc66;",
        FeedState.STALE: "color: #ffaa00;",
        FeedState.RECONNECTING: "color: #ff6600;",
        FeedState.FAILED: "color: #ff3333;",
        FeedState.CONNECTING: "color: #6a6a8a;",
        FeedState.DISCONNECTED: "color: #6a6a8a;",
    }
    _FEED_STATE_LABEL = {
        FeedState.LIVE: "Live",
        FeedState.STALE: "Stale",
        FeedState.RECONNECTING: "Recon",
        FeedState.FAILED: "Failed",
        FeedState.CONNECTING: "...",
        FeedState.DISCONNECTED: "Off",
    }

    def __init__(self, main_window: MainWindow):
        self._mw = main_window
        self._engine: Any = None
        self._trade_buffer: deque = deque(maxlen=200_000)
        self._trades_received_ws = 0
        self._trades_dropped_in_drain = 0
        self._last_drained_trade_ts = 0
        self._latest_ws_trade_ts = 0
        self._trade_feed_health = FeedStreamHealth()
        self._depth_feed_health = FeedStreamHealth()
        self._book_empty_ticks = 0
        self._book_crossed_ticks = 0
        self._last_valid_bid = 0.0
        self._last_valid_ask = 0.0
        self._last_valid_bid_count = 0
        self._last_valid_ask_count = 0
        self._book_resync_count = 0
        self._book_resync_pending = False
        self._recent_trade_keys: deque = deque(maxlen=200)
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_stop_event: Optional[threading.Event] = None
        self._health = _HealthSnapshot()
        self._vp_cache_key: tuple | None = None
        self._bubble_dead_ticks = 0
        # Phase 13B — optional SessionRecorder for orchestration replay.
        # When set, ``on_timer_tick`` calls ``recorder.record_session_tick``
        # at end-of-tick with deterministic state scalars.
        self._recorder: Any = None

        # Phase 14B — layered-strategy state. The C++ engine pulls real
        # Tide budgets, Wave permissions, and realized vol via the three
        # `RippleEngine` setters; without these calls the engine runs
        # against `DefaultTideSnapshot` / `DefaultWaveSnapshot` for the
        # entire session (V1 §22.2 #8 / #9 contract violation).
        #
        # Defaults are fine for V1: `TideConfig` ships with NEUTRAL bias
        # and NORMAL vol_regime; `WaveConfig` ships with V1 thresholds
        # documented in `wave/wave_engine.py`. Dynamic bias / vol_regime
        # sourcing from macro data is V2 scope per strategy.md §23.
        self._tide_engine: TideEngine = TideEngine()
        self._wave_engine: WaveEngine = WaveEngine()
        self._enable_layered_strategy: bool = True
        self._rv_push_counter: int = 0
        self._wave_push_counter: int = 0
        self._tide_push_counter: int = 0
        self._rv_price_buf: deque = deque(maxlen=self._RV_PRICE_BUF_LEN)
        self._layered_pushes_tide: int = 0
        self._layered_pushes_wave: int = 0
        self._layered_pushes_rv: int = 0

        # Phase 14F.4 — V1.1 risk-gate diagnostics. ``ui/main_window.py``
        # calls :meth:`record_block` whenever ``intent_risk_block_reason``
        # rejects a live Ripple intent (AGENT_STRATEGY_RULES.md §7.6).
        # Operators previously had to grep the log to see suppressed
        # trades; the strategy dashboard now surfaces the latest reason
        # plus a session running total. Timestamps are event-time
        # (``intent.timestamp``) — the dashboard converts to "Xs ago"
        # only at render time, where wall-clock is acceptable.
        self._last_block_reason: str = ""
        self._last_block_ts_ms: int = 0
        self._block_count: int = 0
        self._block_counts_by_reason: dict[str, int] = {}

    def attach_recorder(self, recorder: Any) -> None:
        """Phase 13B — attach a ``SessionRecorder`` so each timer tick
        emits a ``session_tick`` event capturing the orchestration state
        (drained count, book state, resync flags). Pass ``None`` to
        detach."""
        self._recorder = recorder

    # ──────────────────────────────────────────────────────────────
    # Phase 14F.4 — Risk-gate diagnostics
    # ──────────────────────────────────────────────────────────────

    def record_block(self, reason: str, ts_ms: int) -> None:
        """Record a Phase 14C risk-gate block.

        Called by ``ui/main_window.py:_on_ripple_received`` whenever
        :func:`execution.models.intent_risk_block_reason` rejects a live
        intent. The strategy dashboard reads these fields via
        :meth:`block_status` on every UI tick.

        ``ts_ms`` is the event-time of the blocked intent
        (``intent.timestamp``); the dashboard converts to "Xs ago" at
        render time using wall-clock — wall-clock is acceptable on a
        purely-cosmetic age display per AGENT_STRATEGY_RULES.md §7.1.
        """
        if not reason:
            return
        self._last_block_reason = reason
        self._last_block_ts_ms = int(ts_ms or 0)
        self._block_count += 1
        self._block_counts_by_reason[reason] = (
            self._block_counts_by_reason.get(reason, 0) + 1)

    def block_status(self) -> tuple[str, int, int]:
        """Snapshot of (last_reason, last_ts_ms, total_count) for the
        strategy dashboard."""
        return (self._last_block_reason,
                self._last_block_ts_ms,
                self._block_count)

    @property
    def last_block_reason(self) -> str:
        return self._last_block_reason

    @property
    def last_block_ts_ms(self) -> int:
        return self._last_block_ts_ms

    @property
    def block_count(self) -> int:
        return self._block_count

    @property
    def block_counts_by_reason(self) -> dict[str, int]:
        return dict(self._block_counts_by_reason)

    # ──────────────────────────────────────────────────────────────
    # Phase 14F.5 — Layered-wiring indicator
    # ──────────────────────────────────────────────────────────────

    def layered_push_status(self) -> dict[str, int]:
        """Phase 14F.5 — return the per-layer push tallies so the
        strategy panel can show ``● live`` once a layer has received at
        least one successful push, or ``○ default`` while the layer is
        still running against ``DefaultTideSnapshot`` /
        ``DefaultWaveSnapshot`` / zero realized-vol.

        Keys are stable identifiers; the UI maps them to display
        labels.
        """
        return {
            "tide": int(self._layered_pushes_tide),
            "wave": int(self._layered_pushes_wave),
            "rv": int(self._layered_pushes_rv),
        }

    @property
    def engine(self) -> Any:
        return self._engine

    @property
    def health(self) -> _HealthSnapshot:
        return self._health

    @property
    def trade_feed_health(self) -> FeedStreamHealth:
        return self._trade_feed_health

    @property
    def depth_feed_health(self) -> FeedStreamHealth:
        return self._depth_feed_health

    @property
    def latest_ws_trade_ts(self) -> int:
        return self._latest_ws_trade_ts

    @property
    def last_drained_trade_ts(self) -> int:
        return self._last_drained_trade_ts

    @property
    def trades_received_ws(self) -> int:
        return self._trades_received_ws

    @property
    def trades_dropped_in_drain(self) -> int:
        return self._trades_dropped_in_drain

    def start_live(
        self,
        *,
        symbol: str,
        tick_size: float,
        imbalance: float,
        vp_window_ms: int,
        websockets_module: Any,
        signal_callback: Callable,
        ripple_callback: Callable,
    ) -> bool:
        """Create engine, start WS thread, load REST snapshot. Returns False if ofe missing."""
        if ofe is None:
            return False
        config = ofe.EngineConfig()
        config.tick_size = tick_size
        config.signal_params.imbalance_threshold = imbalance
        try:
            config.ripple.enable_diagnostics = True
            config.ripple.console_diagnostics = False
        except AttributeError:
            pass

        self._engine = ofe.OrderFlowEngine(config)
        self._engine.set_signal_callback(signal_callback)
        self._engine.set_ripple_callback(ripple_callback)
        self._engine.get_volume_profile().set_window(vp_window_ms)

        self._trade_feed_health = FeedStreamHealth(symbol=symbol)
        self._depth_feed_health = FeedStreamHealth(symbol=symbol)
        self._recent_trade_keys.clear()
        self._book_empty_ticks = 0
        self._book_crossed_ticks = 0
        self._book_resync_count = 0
        self._book_resync_pending = False

        self._ws_stop_event = threading.Event()
        self._ws_thread = threading.Thread(
            target=self._run_ws_feed,
            args=(symbol.lower(), self._ws_stop_event),
            daemon=True,
        )
        self._fetch_depth_snapshot(symbol)
        self._ws_thread.start()
        return True

    def stop(self) -> None:
        """Stop WS thread and drop engine reference (does not touch strategy UI)."""
        if self._ws_stop_event is not None:
            self._ws_stop_event.set()
            if self._ws_thread and self._ws_thread.is_alive():
                self._ws_thread.join(timeout=5)
            self._ws_stop_event = None
            self._ws_thread = None

        self._trade_feed_health = FeedStreamHealth()
        self._depth_feed_health = FeedStreamHealth()
        self._recent_trade_keys.clear()
        self._trades_received_ws = 0
        self._trades_dropped_in_drain = 0
        self._last_drained_trade_ts = 0
        self._latest_ws_trade_ts = 0
        self._book_empty_ticks = 0
        self._book_crossed_ticks = 0
        self._book_resync_count = 0
        self._book_resync_pending = False
        self._engine = None
        self._trade_buffer.clear()
        self._vp_cache_key = None

    @staticmethod
    def _should_repaint_strategy_dashboard(
        strat_visible: bool,
        prev_visible: bool,
        strat_refreshed: bool,
    ) -> bool:
        """Decide whether to repaint the strategy dashboard this tick.

        The dashboard's `_StrategyHistoryPanel.paintEvent` is O(N) in
        `MarketState.snapshot_history` length (capped at 600). Calling it
        every 100 ms timer tick — when the underlying snapshot only
        changes every 500 ms (the 5-tick strategy cadence) — drove the
        live tick avg from ~11 ms to ~145 ms (Phase 7 regression).
        Repaint only when:
        - the window is visible AND
        - either the snapshot just refreshed, OR the window just became
          visible (rising edge — the prior state is stale).
        """
        if not strat_visible:
            return False
        return strat_refreshed or not prev_visible

    def _compute_realized_vol(self) -> float:
        """Phase 14B — rolling realized vol from the last N trade prices
        in ``_rv_price_buf``. Returns 0.0 if fewer than 2 prices have
        arrived (cannot compute a stdev yet).

        Thin wrapper around :func:`execution.models.compute_realized_vol_from_prices`
        so the session and ``execution/live_runner.py`` share one
        implementation.
        """
        return compute_realized_vol_from_prices(self._rv_price_buf)

    def _push_layered_strategy(self) -> None:
        """Phase 14B — push Tide budget, Wave snapshot, and realized vol
        into the live C++ engine on the cadences defined by
        ``_RV_PUSH_EVERY`` / ``_WAVE_PUSH_EVERY`` / ``_TIDE_PUSH_EVERY``.

        Each setter is wrapped in its own try/except so a single
        binding-error (e.g. the live engine was rebuilt with stale
        bindings) cannot cascade and disable the others. Counts of
        successful pushes are tracked on ``_layered_pushes_*`` for
        diagnostics + tests.

        Called exclusively from ``on_timer_tick``; this method does NOT
        consult wall-clock time (the counter cadence is the only gate)
        so the determinism contract (AGENT_STRATEGY_RULES.md §7.1) is
        preserved.
        """
        if not self._enable_layered_strategy or not self._engine:
            return
        if ofe is None:
            return
        try:
            ripple = self._engine.get_ripple()
        except Exception as exc:
            logger.warning("get_ripple failed (layered push skipped): %s", exc)
            return

        self._rv_push_counter += 1
        self._wave_push_counter += 1
        self._tide_push_counter += 1

        last_ts = self._last_drained_trade_ts or self._latest_ws_trade_ts

        if self._rv_push_counter >= self._RV_PUSH_EVERY:
            self._rv_push_counter = 0
            try:
                rv = self._compute_realized_vol()
                self._tide_engine.set_realized_vol(rv)
                ripple.set_realized_vol(rv)
                self._layered_pushes_rv += 1
            except Exception as exc:
                logger.warning("set_realized_vol failed: %s", exc)

        if self._wave_push_counter >= self._WAVE_PUSH_EVERY:
            self._wave_push_counter = 0
            try:
                tide_snap = self._tide_engine.get_snapshot()
                wave_snap = self._wave_engine.get_snapshot(bias=tide_snap.bias)
                if last_ts > 0:
                    self._wave_engine.update(last_ts, bias=tide_snap.bias)
                ofe_ws = wave_snapshot_to_ofe(wave_snap, ofe)
                ripple.set_wave_snapshot(ofe_ws)
                self._layered_pushes_wave += 1
            except Exception as exc:
                logger.warning("set_wave_snapshot failed: %s", exc)

        if self._tide_push_counter >= self._TIDE_PUSH_EVERY:
            self._tide_push_counter = 0
            try:
                if last_ts > 0:
                    self._tide_engine.update(last_ts)
                snap = self._tide_engine.get_snapshot()
                ripple.set_risk_budget(
                    snap.es_budget,
                    snap.max_position_usd,
                    snap.risk_multiplier,
                )
                self._layered_pushes_tide += 1
            except Exception as exc:
                logger.warning("set_risk_budget failed: %s", exc)

    def set_volume_profile_window(self, window_ms: int) -> None:
        if self._engine:
            self._engine.get_volume_profile().set_window(window_ms)

    # ──────────────────────────────────────────────────────────────
    # Phase 8A — Historical candle preload
    # ──────────────────────────────────────────────────────────────

    def fetch_historical_klines(
        self,
        symbol: str,
        interval_ms: int,
        limit: int = DEFAULT_KLINES_LIMIT,
    ) -> list[tuple[int, float, float, float, float, float]]:
        """Phase 8A — fetch historical OHLCV klines from Binance.

        Synchronous wrapper around :func:`data_feed.fetch_binance_klines`
        that **always returns a list** — network failures, malformed
        responses, or invalid ``interval_ms`` values are logged and the
        method returns ``[]``.  ``MainWindow`` runs this off the GUI
        thread so the Qt event loop stays responsive (Acceptance
        Criterion #4 — "no crash, no error dialog").

        See ``UI_STRATEGY_INTEGRATION_PLAN.md`` §16.2.
        """
        sym = (symbol or "").strip()
        if not sym:
            return []
        try:
            return fetch_binance_klines(
                BINANCE_FUTURES_USDM_KLINES_URL,
                sym,
                int(interval_ms),
                limit=int(limit),
            )
        except Exception as exc:
            logger.warning(
                "fetch_historical_klines failed (symbol=%s interval_ms=%d): %s",
                symbol, int(interval_ms), exc,
            )
            return []

    def _fetch_depth_snapshot(self, symbol: str) -> None:
        if not self._engine or ofe is None:
            return
        try:
            snap = fetch_and_build_depth_snapshot(
                ofe,
                BINANCE_FUTURES_USDM_DEPTH_URL,
                symbol,
                limit=1000,
                timeout=10.0,
            )
            self._engine.process_depth(snap)
            logger.info(
                "Depth snapshot loaded: %d bids, %d asks",
                len(snap.bids),
                len(snap.asks),
            )
        except Exception as e:
            logger.error("Depth snapshot failed: %s", e)

    def _ws_apply_trade(self, t, is_buy_aggressor: bool) -> None:
        if not self._engine:
            return
        self._engine.process_trade(t)
        self._trades_received_ws += 1
        if t.timestamp > self._latest_ws_trade_ts:
            self._latest_ws_trade_ts = t.timestamp
        self._trade_buffer.append(
            (t.timestamp, t.price, t.quantity, is_buy_aggressor))

    def _ws_apply_depth(self, u) -> None:
        if self._engine:
            self._engine.process_depth(u)

    def _run_ws_feed(self, symbol_lower: str, stop_event: threading.Event) -> None:
        mw = self._mw
        ws_mod = getattr(mw, "_websockets_module", None)
        if ws_mod is None:
            try:
                import websockets as ws_mod  # noqa: PLC0415
            except ImportError:
                ws_mod = None
        if ofe is None or ws_mod is None:
            return
        run_binance_usdm_futures_ws_feed(
            symbol_lower=symbol_lower,
            stop_event=stop_event,
            ofe=ofe,
            trade_health=self._trade_feed_health,
            depth_health=self._depth_feed_health,
            recent_trade_ids=self._recent_trade_keys,
            on_ws_trade=self._ws_apply_trade,
            on_ws_depth=self._ws_apply_depth,
            websockets_module=ws_mod,
        )

    def _resync_depth_snapshot(self, symbol: str) -> None:
        try:
            if not self._engine or ofe is None:
                return
            book = fetch_binance_depth_book(
                BINANCE_FUTURES_USDM_DEPTH_URL,
                symbol,
                limit=1000,
                timeout=10.0,
            )
            u = depth_book_to_engine_update(
                book, ofe, timestamp_ms=int(time.time() * 1000)
            )
            self._engine.process_depth(u)
            logger.info(
                "Depth snapshot resync applied — %d bids, %d asks",
                len(book.bids),
                len(book.asks),
            )
        except Exception as e:
            logger.error("Depth snapshot resync failed: %s", e)
        finally:
            self._book_resync_pending = False

    def on_timer_tick(self) -> None:
        if not self._engine:
            return

        mw = self._mw
        t0 = time.monotonic()
        drained = 0

        ws_ts = self._latest_ws_trade_ts
        if ws_ts > 0:
            mw._heatmap.sync_trade_time(ws_ts)

        try:
            buf_depth = len(self._trade_buffer)
            self._health.last_trade_buffer_depth = buf_depth
            cap = self._MAX_TRADES_PER_TICK
            if buf_depth > 10_000:
                cap = min(buf_depth, cap * 4)
            elif buf_depth > 5000:
                cap = cap * 2
            drain_errors = 0
            while self._trade_buffer and drained < cap:
                try:
                    ts, price, qty, is_buy = self._trade_buffer.popleft()
                except IndexError:
                    break
                try:
                    mw._heatmap.add_trade(ts, price, qty, is_buy)
                except Exception as exc:
                    drain_errors += 1
                    if drain_errors <= 3:
                        logger.warning("heatmap.add_trade error: %s", exc)
                try:
                    mw._cvd.add_trade(ts, price, qty, is_buy)
                except Exception as exc:
                    drain_errors += 1
                    if drain_errors <= 3:
                        logger.warning("cvd.add_trade error: %s", exc)
                try:
                    mw._candle_view.process_trade(ts, price, qty, is_buy)
                except Exception:
                    pass
                if self._enable_layered_strategy:
                    try:
                        self._wave_engine.on_price(price, ts)
                        self._rv_price_buf.append(price)
                    except Exception as exc:
                        drain_errors += 1
                        if drain_errors <= 3:
                            logger.warning("wave_engine.on_price error: %s", exc)
                self._last_drained_trade_ts = ts
                drained += 1
            if drain_errors > 0:
                self._trades_dropped_in_drain += drain_errors
                if (self._trades_dropped_in_drain <= 10
                        or self._health.tick_count % 50 == 0):
                    logger.warning(
                        "Trade drain errors: %d this tick, %d total",
                        drain_errors, self._trades_dropped_in_drain)
            remaining = len(self._trade_buffer)
            if remaining > 5000:
                logger.warning(
                    "Trade buffer backlog: %d remaining after draining %d "
                    "(started with %d)", remaining, drained, buf_depth)
        except Exception as e:
            logger.error("Trade drain error: %s", e)

        t1 = time.monotonic()

        book_ok = False
        try:
            ob = self._engine.get_order_book()
            snap = ob.get_snapshot()

            bids = snap.get_bids()
            asks = snap.get_asks()
            best_bid = snap.best_bid
            best_ask = snap.best_ask
            snap_ts = snap.timestamp

            bid_count = len(bids)
            ask_count = len(asks)
            book_empty = (bid_count == 0 and ask_count == 0)
            book_crossed = (best_bid > 0 and best_ask > 0
                            and best_bid >= best_ask)

            if book_empty:
                self._book_empty_ticks += 1
                if (self._book_empty_ticks == 1
                        or self._book_empty_ticks % self._BOOK_HEALTH_LOG_INTERVAL == 0):
                    logger.warning(
                        "Order book EMPTY for %d ticks — last valid "
                        "bid=%.2f ask=%.2f (%d/%d levels)",
                        self._book_empty_ticks,
                        self._last_valid_bid, self._last_valid_ask,
                        self._last_valid_bid_count,
                        self._last_valid_ask_count)
            else:
                if self._book_empty_ticks > 0:
                    logger.info("Order book recovered after %d empty ticks",
                                self._book_empty_ticks)
                self._book_empty_ticks = 0
                self._last_valid_bid = best_bid
                self._last_valid_ask = best_ask
                self._last_valid_bid_count = bid_count
                self._last_valid_ask_count = ask_count

            if book_crossed:
                self._book_crossed_ticks += 1
            else:
                self._book_crossed_ticks = 0

            if (not mw._is_replay_mode
                    and self._book_empty_ticks >= self._BOOK_EMPTY_RESYNC_THRESHOLD
                    and not self._book_resync_pending):
                self._book_resync_pending = True
                self._book_resync_count += 1
                symbol = mw._symbol_input.text().strip().upper()
                logger.warning("Triggering depth snapshot resync #%d for %s",
                               self._book_resync_count, symbol)
                threading.Thread(
                    target=self._resync_depth_snapshot,
                    args=(symbol,),
                    daemon=True,
                ).start()

            if not book_empty:
                chart_now_ts = mw._heatmap.chart_now
                sample_ts = chart_now_ts if chart_now_ts > 0 else snap_ts
                mw._heatmap.add_depth_column(
                    snap_ts, bids, asks, best_bid, best_ask,
                    sample_ts=sample_ts,
                )
                book_ok = True
            else:
                chart_now_ts = mw._heatmap.chart_now
                if chart_now_ts > 0:
                    mw._heatmap.add_depth_column(
                        snap_ts, [], [], 0, 0,
                        sample_ts=chart_now_ts,
                    )

        except Exception as e:
            logger.error("Order book snapshot error: %s", e)

        t2 = time.monotonic()

        try:
            self._vp_tick_counter = getattr(self, "_vp_tick_counter", 0) + 1
            if self._vp_tick_counter >= self._VP_RECOMPUTE_EVERY:
                self._vp_tick_counter = 0
                self._recompute_volume_profile()

            chart_now = mw._heatmap.chart_now
            if chart_now > 0:
                mw._cvd.set_time_ref(
                    chart_now,
                    mw._heatmap._visible_window_ms,
                    mw._heatmap._chart_right_inset,
                )
            mw._cvd.refresh()
        except Exception as e:
            logger.error("VP/CVD update error: %s", e)

        t3 = time.monotonic()

        try:
            if self._engine:
                tf = self._engine.get_trade_flow()
                mw._trades_label.setText(
                    f"Trades: {int(tf.get_total_volume())}")

                sig = self._engine.get_signal_engine()
                mw._signals_label.setText(
                    f"Signals: {mw._signal_count} | "
                    f"PnL: {sig.get_pnl():.2f}% | "
                    f"Pos: {sig.get_position()}")
        except Exception as e:
            logger.error("Status label error: %s", e)

        try:
            self._badge_tick_counter = getattr(self, "_badge_tick_counter", 0) + 1
            self._status_tick_counter = getattr(self, "_status_tick_counter", 0) + 1

            if self._badge_tick_counter >= self._FEED_BADGE_EVERY:
                self._badge_tick_counter = 0
                self._update_feed_health_badge()

            if self._status_tick_counter >= self._STATUS_PANEL_EVERY:
                self._status_tick_counter = 0
                self._update_pipeline_diagnostics(book_ok)
        except Exception as e:
            logger.error("Diagnostics error: %s", e)

        strat_refreshed = False
        try:
            self._strat_tick_counter = getattr(self, "_strat_tick_counter", 0) + 1
            if self._strat_tick_counter >= 5:
                self._strat_tick_counter = 0
                if self._engine and hasattr(self._engine, "get_strategy_snapshot"):
                    snap = self._engine.get_strategy_snapshot()
                    mw._last_strategy_snap = snap
                    mw._update_strategy_state_from_snapshot(snap)
                    mw._update_heatmap_overlay_from_snapshot(snap)
                    # Append to MarketState rolling history for dashboard
                    # mini-charts. We store (ts_ms, snap) tuples so the panel
                    # can plot a true time series independent of tick cadence.
                    if snap is not None:
                        mw._market_state.snapshot_history.append(
                            (int(time.time() * 1000), snap))
                    if (mw._strategy_store
                            and mw._strategy_ui_state.value.startswith("armed")):
                        mw._strategy_store.buffer_snapshot(
                            int(time.time() * 1000), snap)
                    strat_refreshed = True
        except Exception as e:
            logger.error("Strategy panel error: %s", e)

        try:
            if mw._exec_manager:
                acct = mw._exec_manager.account
                mw._strategy_dashboard.account_panel.update_account(
                    acct.balance, acct.available_balance, acct.positions
                )
                if mw._exec_manager.armed:
                    side = mw._exec_manager.current_side
                    qty = mw._exec_manager.current_qty
                    pos_str = f"{side.value} {qty:.6f}" if side else "Flat"
                    mw._exec_label.setText(
                        f"ARMED | Bal: {acct.balance:.2f} | Pos: {pos_str}")
        except Exception as e:
            logger.error("Exec manager update error: %s", e)

        vm = mw._orderflow_vm
        pw, ph, ml, mt, cri = mw._heatmap.get_viewport_params()
        frame = vm.compute_frame(pw, ph, ml, mt, cri)
        mw._heatmap.set_frame(frame)

        ms = mw._market_state
        ms.chart_now = vm.chart_now
        ms.visible_window_ms = vm.visible_window_ms
        ms.bucket_duration_ms = vm.bucket_duration_ms
        ms.best_bid = self._last_valid_bid
        ms.best_ask = self._last_valid_ask
        ms.strategy_snapshot = mw._last_strategy_snap
        ms.strategy_ui_state = mw._strategy_ui_state

        mw._heatmap.update()
        if mw._candle_window.isVisible():
            mw._candle_view.update()

        # Strategy dashboard: refresh+repaint only when the underlying
        # snapshot actually changed (the 5-tick strategy cadence) or on the
        # rising edge of window visibility. Repainting at the 100 ms timer
        # cadence is wasted work because `_StrategyHistoryPanel.paintEvent`
        # is O(N) in `snapshot_history` length (Phase 7 regression — see
        # `implementation_plan.md` Phase 11).
        strat_visible = mw._strategy_window.isVisible()
        prev_visible = getattr(self, "_strat_was_visible", False)
        if self._should_repaint_strategy_dashboard(
                strat_visible, prev_visible, strat_refreshed):
            mw._strategy_dashboard.update_from_state()
            mw._strategy_dashboard.update()
        self._strat_was_visible = strat_visible

        # Phase 14B — push the latest Tide/Wave/RV snapshots into the C++
        # engine on the cadences in `strategy.md` §5.3 + AGENT_STRATEGY_RULES.md
        # §7.5. Wrapped in try/except so a layered-strategy failure cannot
        # break the live UI loop.
        try:
            self._push_layered_strategy()
        except Exception:
            logger.exception("layered-strategy push failed")

        elapsed = (time.monotonic() - t0) * 1000.0
        self._health.record_tick(elapsed)
        self._health.trades_drained_total += drained
        self._health.phase_trade_ms = (t1 - t0) * 1000.0
        self._health.phase_book_ms = (t2 - t1) * 1000.0
        self._health.phase_vp_cvd_ms = (t3 - t2) * 1000.0

        # Phase 13B — emit a deterministic per-tick orchestration snapshot
        # for replay verification. Called last so all post-tick scalars
        # (book state, drained count, resync flags) are settled. Wrapped
        # in try/except so a faulty recorder cannot break the live UI.
        if self._recorder is not None:
            try:
                self._recorder.record_session_tick(
                    self._health.tick_count,
                    drained=drained,
                    best_bid=self._last_valid_bid,
                    best_ask=self._last_valid_ask,
                    bid_count=self._last_valid_bid_count,
                    ask_count=self._last_valid_ask_count,
                    book_empty=(self._book_empty_ticks > 0),
                    book_crossed=(self._book_crossed_ticks > 0),
                    book_empty_ticks=self._book_empty_ticks,
                    book_resync_pending=self._book_resync_pending,
                    book_resync_count=self._book_resync_count,
                    trade_buf_remaining=len(self._trade_buffer),
                )
            except Exception:
                logger.exception("session_recorder.record_session_tick failed")

        if self._health.tick_count % 50 == 0:
            hm = mw._heatmap
            trade_lag = 0
            if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
                trade_lag = hm.chart_now - self._last_drained_trade_ts
            buf_len = len(self._trade_buffer)
            overflow = max(
                0,
                self._trades_received_ws
                - self._health.trades_drained_total
                - buf_len)
            bd = (hm.get_bubble_diagnostics()
                  if hasattr(hm, "get_bubble_diagnostics") else {})
            dt_skew = hm._last_depth_ts - hm._last_trade_ts
            logger.info(
                "PERF tick=%d avg=%.1fms max=%.1fms | "
                "drain=%.1fms book=%.1fms vp=%.1fms | "
                "q=%d bub=%d slc=%d trd=%d paint=%.1fms | "
                "lag=%dms ovfl=%d drErr=%d skew=%dms | "
                "fT=%d fX=%d fY=%d p95=%.4g prn=%d stage=%s",
                self._health.tick_count,
                self._health.tick_duration_ms_avg,
                self._health.tick_duration_ms_max,
                self._health.phase_trade_ms,
                self._health.phase_book_ms,
                self._health.phase_vp_cvd_ms,
                buf_len,
                getattr(hm, "_last_visible_bubble_count", 0),
                len(hm._slices),
                len(hm._trades),
                getattr(hm, "_last_paint_ms", 0),
                trade_lag,
                overflow,
                self._trades_dropped_in_drain,
                dt_skew,
                bd.get("filtered_by_time", -1),
                bd.get("filtered_by_x", -1),
                bd.get("filtered_by_y", -1),
                bd.get("p95_size", -1),
                bd.get("trades_pruned_this_frame", -1),
                bd.get("failure_stage", "?"),
            )

    def _update_feed_health_badge(self) -> None:
        mw = self._mw
        if mw._is_replay_mode:
            txt = "Replay"
            style = (
                "font-family: Menlo; font-size: 10px; "
                "padding: 0 6px; color: #6a6a8a;"
            )
        else:
            th = self._trade_feed_health
            dh = self._depth_feed_health

            if self._book_empty_ticks > 0:
                color = "#ff3333"
            elif th.state in (FeedState.STALE, FeedState.FAILED):
                color = "#ffaa00"
            elif th.state == FeedState.LIVE and dh.state == FeedState.LIVE:
                color = "#00cc66"
            else:
                color = "#6a6a8a"

            style = (
                f"font-family: Menlo; font-size: 10px; "
                f"padding: 0 6px; color: {color};"
            )
            txt = (
                f"T:{self._FEED_STATE_LABEL.get(th.state, '?')} "
                f"D:{self._FEED_STATE_LABEL.get(dh.state, '?')}"
            )

        prev = getattr(mw, "_feed_badge_cache", (None, None))
        if (txt, style) != prev:
            mw._feed_badge_cache = (txt, style)
            mw._trade_feed_label.setStyleSheet(style)
            mw._trade_feed_label.setText(txt)
            self._health.badge_updates += 1

    def _update_pipeline_diagnostics(self, book_ok: bool) -> None:
        mw = self._mw
        th = self._trade_feed_health
        dh = self._depth_feed_health
        hm = mw._heatmap

        if mw._is_replay_mode:
            mode_val = "Replay"
        elif not self._engine:
            mode_val = "Off"
        else:
            mode_val = "Live"

        t_label = self._FEED_STATE_LABEL.get(th.state, "?")
        d_label = self._FEED_STATE_LABEL.get(dh.state, "?")

        if self._book_empty_ticks > 0:
            book_val = "Empty"
        elif self._book_crossed_ticks > 0:
            book_val = "Cross"
        elif book_ok:
            book_val = "Synced"
        else:
            book_val = "?"

        hm_ok = hm._heatmap_samples > 0 and hm._last_log_max > 0
        hm_val = "Live" if hm_ok else ("Stalled" if hm._heatmap_samples > 0 else "?")

        ripple_val = (
            f"{mw._strategy_mode.value.title()}"
            f"/{mw._strategy_ui_state.value.split('_')[-1].title()}"
        )

        bubble_ct = getattr(hm, "_last_visible_bubble_count", 0)
        trade_ct = len(hm._trades)
        buf_depth = self._health.last_trade_buffer_depth
        trades_arriving = (th.state == FeedState.LIVE or trade_ct > 0)
        bd = hm.get_bubble_diagnostics() if hasattr(hm, "get_bubble_diagnostics") else {}
        failure_stage = bd.get("failure_stage", "NONE")

        if bubble_ct > 0:
            bub_val = "Live"
            self._bubble_dead_ticks = 0
        elif trades_arriving and trade_ct > 0:
            self._bubble_dead_ticks += self._STATUS_PANEL_EVERY
            if self._bubble_dead_ticks >= self._BUBBLE_DEAD_THRESHOLD_TICKS:
                bub_val = f"DEAD:{failure_stage}"
                if self._bubble_dead_ticks == self._BUBBLE_DEAD_THRESHOLD_TICKS:
                    self._emit_bubbles_dead_warning(hm, trade_ct, buf_depth)
            else:
                bub_val = "Live"
        elif trades_arriving and trade_ct == 0:
            bub_val = f"DEAD:{failure_stage}"
            self._bubble_dead_ticks += self._STATUS_PANEL_EVERY
            if self._bubble_dead_ticks == self._STATUS_PANEL_EVERY:
                self._emit_bubbles_dead_warning(hm, trade_ct, buf_depth)
        else:
            bub_val = "?" if not self._engine else "Off"
            self._bubble_dead_ticks = 0

        tl = 0
        if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
            tl = hm.chart_now - self._last_drained_trade_ts
        buf_val = f"{self._health.last_trade_buffer_depth}"
        if tl > 2000:
            buf_val += f" lag={tl // 1000}s"
        if self._trades_dropped_in_drain > 0:
            buf_val += f" err={self._trades_dropped_in_drain}"

        rows = [
            ("Mode", mode_val),
            ("Trade", t_label),
            ("Depth", d_label),
            ("Book", book_val),
            ("Heatmap", hm_val),
            ("Bubbles", bub_val),
            ("TrdBuf", buf_val),
            ("Strategy", ripple_val),
        ]

        mw._status_panel.set_health(rows, title="Pipeline")
        self._health.status_updates += 1

        m = mw._ripple_metrics
        h = self._health

        ripple_txt = f"Ripple: {m.emitted} emit"
        total_suppr = m.total_suppressed
        if total_suppr:
            ripple_txt += f" | {total_suppr} suppr"
        if mw._paper_engine:
            pm = mw._paper_engine.metrics
            if pm.entries_filled or pm.exits_filled:
                ripple_txt += (
                    f" | Paper: {pm.entries_filled}E "
                    f"{pm.exits_filled}X "
                    f"PnL:{pm.realized_pnl:+.2f}")

        ripple_txt += (
            f" | bub:{bubble_ct}/{trade_ct}"
            f" buf:{buf_depth}"
            f" slc:{hm._heatmap_samples}")

        mw._ripple_label.setText(ripple_txt)

    def _emit_bubbles_dead_warning(self, hm, trade_ct, buf_depth) -> None:
        mw = self._mw
        diag = hm.get_bubble_diagnostics() if hasattr(hm, "get_bubble_diagnostics") else {}
        stage = diag.get("failure_stage", "UNKNOWN")

        sample_price = hm._trades[-1][1] if hm._trades else 0
        sample_ts = hm._trades[-1][0] if hm._trades else 0
        trade_lag = 0
        if hm.chart_now > 0 and self._last_drained_trade_ts > 0:
            trade_lag = hm.chart_now - self._last_drained_trade_ts
        buf_overflow = max(
            0,
            self._trades_received_ws
            - self._health.trades_drained_total
            - len(self._trade_buffer))

        depth_trade_skew = hm._last_depth_ts - hm._last_trade_ts

        logger.warning(
            "BUBBLES_DEAD_WHILE_TRADES_LIVE stage=%s | "
            "trades=%d buf=%d deadTicks=%d | "
            "priceRange=[%.2f,%.2f] samplePrice=%.2f sampleTs=%d | "
            "chartNow=%d depthTs=%d tradeTs=%d depthTradeSkew=%dms | "
            "tradeLag=%dms bufOverflow=%d drainErrors=%d | "
            "filtTime=%d filtX=%d filtY=%d vis=%d | "
            "rMin=%.1f rMax=%.1f rAvg=%.1f | "
            "newestX=%.0f oldestX=%.0f pw=%d | "
            "p95=%.6g prunedFrame=%d prunedTotal=%d zeroFrames=%d | "
            "addedTotal=%d rejectedPrice=%d maxDeque=%d "
            "activeMinTs=%d activeMaxTs=%d",
            stage,
            trade_ct, buf_depth, self._bubble_dead_ticks,
            hm._price_min, hm._price_max, sample_price, sample_ts,
            hm.chart_now, hm._last_depth_ts, hm._last_trade_ts,
            depth_trade_skew,
            trade_lag, buf_overflow, self._trades_dropped_in_drain,
            diag.get("filtered_by_time", -1),
            diag.get("filtered_by_x", -1),
            diag.get("filtered_by_y", -1),
            diag.get("visible", -1),
            diag.get("min_radius", -1),
            diag.get("max_radius", -1),
            diag.get("avg_radius", -1),
            diag.get("newest_x", -1),
            diag.get("oldest_x", -1),
            diag.get("pw", -1),
            diag.get("p95_size", -1),
            diag.get("trades_pruned_this_frame", -1),
            diag.get("trades_pruned_total", -1),
            diag.get("consecutive_zero_frames", -1),
            diag.get("trades_added_total", -1),
            diag.get("trades_rejected_price", -1),
            diag.get("max_deque_depth", -1),
            diag.get("active_min_ts", -1),
            diag.get("active_max_ts", -1),
        )

    def _recompute_volume_profile(self) -> None:
        mw = self._mw
        hm = mw._heatmap
        hm_min = hm._price_min
        hm_max = hm._price_max

        if (hm_max <= hm_min or not math.isfinite(hm_min)
                or not math.isfinite(hm_max)):
            if hm._trades:
                last_trade_price = hm._trades[-1][1]
                margin = last_trade_price * 0.002
                hm_min = last_trade_price - margin
                hm_max = last_trade_price + margin
            else:
                return

        if not hm._slices and not hm._trades:
            return

        now_ts = hm.chart_now
        if now_ts <= 0:
            return
        t_start = now_ts - hm._visible_window_ms
        trade_count = len(hm._trades)

        vp_key = (round(hm_min, 4), round(hm_max, 4),
                  t_start // 500, trade_count // 50)
        if vp_key == self._vp_cache_key:
            return
        self._vp_cache_key = vp_key
        self._health.vp_recomputes += 1

        pr = hm_max - hm_min
        if pr <= 0:
            return
        n_bins = 200
        bin_size = pr / n_bins
        inv_bin = 1.0 / bin_size

        bins_buy = [0.0] * n_bins
        bins_sell = [0.0] * n_bins

        for i in range(trade_count - 1, -1, -1):
            ts, price, qty, is_buy = hm._trades[i]
            if ts < t_start:
                break
            row = int((price - hm_min) * inv_bin)
            if 0 <= row < n_bins:
                if is_buy:
                    bins_buy[row] += qty
                else:
                    bins_sell[row] += qty

        profile_data = []
        poc_price = 0.0
        poc_vol = 0.0
        for i in range(n_bins):
            bv = bins_buy[i]
            sv = bins_sell[i]
            total = bv + sv
            if total <= 0:
                continue
            p = hm_min + (i + 0.5) * bin_size
            profile_data.append((p, total, bv, sv))
            if total > poc_vol:
                poc_vol = total
                poc_price = p

        mw._volume_profile.set_profile(
            profile_data, poc_price, hm_min, hm_max
        )
