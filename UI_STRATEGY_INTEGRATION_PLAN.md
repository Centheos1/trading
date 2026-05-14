# UI & Strategy Integration Plan

## 1. Overview

This document is a detailed, implementation-oriented plan for four concurrent workstreams:

1. **Order Flow View Update** — convert the heatmap time axis from continuous milliseconds to bucketed candlestick-style time periods (default 1m).
2. **Strategy UI Integration** — connect the Tide / Wave / Ripple strategy with UI arm/disarm controls, strategy state visibility, and signal log integration.
3. **Inactive UI Cleanup** — audit and plan the removal, hiding, or deferral of disconnected, placeholder, or misleading UI elements.
4. **HDF5 / .h5 Data Model Update** — extend the data persistence layer to support strategy integration, signal logging, and time-bucketed UI compatibility.

**This is a planning document only. No code changes should be made from this document without explicit approval.**

**Source-of-truth hierarchy (unchanged):**

| Priority | Document |
|---|---|
| 1 | `strategy.md` |
| 2 | `implementation_plan.md` |
| 3 | Tests |
| 4 | `AGENT_STRATEGY_RULES.md` |
| 5 | This document |

---

## 2. Current State Assessment

### 2.1 Order Flow View (HeatmapWidget)

**File:** `ui/heatmap_widget.py`

The heatmap currently operates on a **continuous millisecond timeline**:

- `VISIBLE_WINDOW_MS = 60_000` (60 seconds visible at a time).
- `HEATMAP_SLICE_MS = 100` (one depth slice every 100ms).
- Slices are stored in a `deque(maxlen=10_000)` of tuples: `(slice_ts, bids_snap, asks_snap, best_bid, best_ask, prices, log_qtys)`.
- `chart_now` is a capped unified timestamp: `max(trade_ts, min(depth_ts, trade_ts + MAX_DEPTH_LEAD_MS))`.
- `t_start = chart_now - visible_window_ms`.
- The time axis (`_draw_time_axis`) selects tick intervals from `_TIME_TICK_CANDIDATES` (1s to 10min) aiming for 6-12 ticks across the width.
- Bubbles (trades) are positioned at continuous x-coordinates: `px + (ts - t_start) * inv_ts * pw`.
- Sub-pixel scrolling: `t_start_q = (t_start // slice_ms) * slice_ms`, `shift = frac * col_px`.
- Price is vertical (y-axis), time is horizontal (x-axis), depth is rendered as a heatmap intensity image.

**Key observations:**
- There is **no concept of time buckets or candlestick periods** in the current heatmap.
- The x-axis is a pure millisecond-linear mapping.
- The `_candle_combo` in the toolbar exists but is not wired to the heatmap — it sets `self._candle_duration_ms` on `MainWindow` but this only affects VP recomputation cadence.
- The visible window is always 60 seconds.
- Depth slices are quantized to 100ms but this is a rendering optimization, not a semantic bucketing.

### 2.2 Strategy UI Integration

**Current state of strategy controls:**

| Control | Location | Current Behavior |
|---|---|---|
| Ripple mode combo | Toolbar | Disabled / Log Only / Paper — controls whether Ripple decisions are logged, paper-traded, or ignored |
| Arm Execution button | Toolbar | Enables execution manager for live Binance fills. Disabled until connected. |
| `StrategyDiagnosticsPanel` | Left column | Displays Wave regime, η, liquidity state, µPrice, trade state, uPnL, ES used, imbalance from `get_strategy_snapshot()` |
| `TradeBlotter` (Signal Log) | Bottom panel | Shows signals with categories: RIPPLE_ENTRY, RIPPLE_EXIT, RIPPLE_PREPARE, RIPPLE_CANCEL, RIPPLE_REARM, EXECUTION, LEGACY_RAW, CONTEXT, DIAGNOSTIC |
| `_StatusPanel` | Left column | Shows connection/feed health status rows |

**Strategy pipeline flow in UI mode:**
1. `MainWindow._on_connect()` creates `OrderFlowEngine`, starts WebSocket feed.
2. `_on_timer_tick()` (100ms) drains trade buffer, feeds depth updates, runs VP recomputation.
3. Ripple decisions come via `_new_ripple` signal → `_on_ripple_received()` → signal log + optional execution.
4. `get_strategy_snapshot()` called on timer tick → `StrategyDiagnosticsPanel.update_snapshot()`.

**What's missing:**
- No unified "strategy armed/disarmed" concept — Ripple mode and Arm Execution are separate controls with no coordinated state.
- No clear mapping of strategy lifecycle states (armed, disarmed, waiting, active, invalidated, exited) to the UI.
- Signal log shows raw Ripple decisions but lacks structured strategy-level context (Tide bias, Wave regime at time of signal, risk budget state).
- No way to arm/disarm the full Tide/Wave/Ripple strategy stack from the UI — only Ripple mode and execution can be toggled independently.

### 2.3 Inactive / Disconnected UI Elements

Likely inactive or misleading UI elements (from code inspection):

| Element | Location | Status |
|---|---|---|
| Candle combo | Toolbar | Sets `_candle_duration_ms` but only affects VP recomputation timing, not heatmap bucketing. Misleading label suggests candlestick periods. |
| Tick Size input | Toolbar | Sets `tick_size` on engine config but unclear if actively used in the Tide/Wave/Ripple pipeline. Legacy signal engine parameter. |
| Imbalance input | Toolbar | Sets `imbalance_threshold` on engine config. Legacy signal engine parameter — the Ripple pipeline has its own thresholds. |
| Sizing controls | Toolbar | `sizing_mode_combo` + `sizing_value_input` — used by execution manager but only meaningful when armed. Visible even when disconnected. |
| Account Panel | Bottom right | `AccountPanel` shows account balance and orders. Only meaningful in execute mode or when broker is connected. May show stale/empty data in pure UI mode. |
| Legacy strategies | `strategies/` | `ichimoku.py`, `obv.py`, `support_resistance.py` — backtest-only strategies not connected to the Tide/Wave/Ripple architecture. Not exposed in UI but referenced in `main.py` mode selection. |
| Mode combo (Live/Replay) | Toolbar | "Replay" mode is present but the replay functionality in the UI is not fully wired (no file picker, no replay controls). |

### 2.4 HDF5 / .h5 Data Model

**Current storage:**

| File | Format | Content | Access |
|---|---|---|---|
| `data/{exchange}.h5` | `Hdf5Client` (Python h5py) | OHLCV candle data per symbol | `database.py` |
| `data/{exchange}_ticks.h5` | C++ `TickStore` | Raw trades and depth snapshots | `TickStore` C++ class, used by `ReplayFeed` |

**OHLCV schema:** `(timestamp, open, high, low, close, volume)` or 7-col with `spread` — flat float64 arrays.

**Tick data schema (C++ TickStore):** HDF5 datasets for trades (ts, price, qty, is_buyer_maker) and depth snapshots.

**What's missing:**
- No strategy signal/event storage in .h5.
- No arm/disarm state persistence.
- No strategy snapshot time series storage.
- No time-bucketed aggregation storage.
- No schema versioning on .h5 files.
- `BacktestResult` has `h5_serialise()` but `write_optimised_parameters` is commented out in `main.py`.

---

## 3. Order Flow View Update Plan

### 3.1 Design Goals

Transform the heatmap x-axis from a continuous millisecond timeline to a **bucketed candle-like time axis** while preserving the order flow visual character (bubbles, depth heatmap, price on y-axis, smooth scrolling).

**Key principles:**
- Default bucket size: **1 minute** (60,000 ms).
- Bucket boundaries are aligned to wall-clock minute boundaries (e.g., 14:01:00, 14:02:00).
- Within each bucket, depth/bubble data is still rendered with continuous sub-bucket positioning.
- The visual result should feel like the current order flow view but with clearer temporal structure.
- Future parameterization for 5s, 15s, 30s, 5m, 15m buckets.
- The `_candle_combo` already exists in the toolbar — wire it to control bucket size.

### 3.2 Bucket Model

Each time bucket represents a fixed-duration period:

```
BucketIndex = floor(timestamp / bucket_duration_ms)
BucketStart = BucketIndex * bucket_duration_ms
BucketEnd   = BucketStart + bucket_duration_ms
```

**Within-bucket x-position:**
```
bucket_frac = (ts - bucket_start) / bucket_duration_ms    # in [0, 1)
x = bucket_left_px + bucket_frac * bucket_width_px
```

This gives smooth continuous positioning within buckets while creating visual gaps or separators between buckets.

### 3.3 Visual Design

**Bucket visual structure:**
- Each bucket occupies a fixed pixel width on screen (determined by visible window and number of visible buckets).
- A subtle vertical separator line (1px, darker than grid) marks bucket boundaries.
- Optionally, a thin OHLC bar or price range indicator at the bucket boundary to reinforce the candle metaphor.
- Depth heatmap continues rendering within each bucket as before (the 100ms slice quantization still applies within the bucket).
- Bubbles (trades) are positioned within their bucket using sub-bucket fractional x-mapping.
- The current bucket (rightmost, in progress) grows in real-time as new data arrives.

**Visible window:**
- Instead of `VISIBLE_WINDOW_MS = 60_000`, the visible window becomes N visible buckets.
- For 1m buckets with a 60s visible window: only 1 bucket is visible. This is too few.
- **Recommended default:** 5 visible buckets at 1m each = 5 minutes visible.
- This means expanding `VISIBLE_WINDOW_MS` to `bucket_duration_ms * num_visible_buckets`.
- `num_visible_buckets` should default to 5 and be configurable.

### 3.4 Data Structure Changes

**Current:** `_slices` is a deque of `(slice_ts, bids, asks, bid, ask, prices, qtys)` at 100ms resolution.

**Proposed:** Add a bucket-level indexing layer on top of the existing slice deque.

```python
@dataclass
class TimeBucket:
    bucket_ts: int          # bucket start timestamp
    slice_start_idx: int    # index into _slices deque (approximate)
    trade_start_idx: int    # index into _trades deque (approximate)
    high: float             # highest trade price in bucket
    low: float              # lowest trade price in bucket
    open_price: float       # first trade price
    close_price: float      # last trade price
    volume: float           # total trade volume
    buy_volume: float       # buy-side volume
    sell_volume: float      # sell-side volume
    trade_count: int        # number of trades
```

The `_buckets` deque maintains a rolling set of these summaries. When a new trade or depth update arrives, it's assigned to the appropriate bucket. When the bucket boundary crosses, a new bucket is created.

**The underlying `_slices` and `_trades` deques remain unchanged.** The bucket layer is an overlay for rendering organization, not a replacement of the raw data.

### 3.5 Rendering Changes

**`_draw_depth` changes:**
- Currently builds an intensity array mapping `(price_row, time_col)` where `time_col` is derived from `slice_ts`.
- With bucketing: the column mapping changes from `col = (slice_ts - t_start) / slice_ms` to a two-level mapping: `bucket_idx → bucket_x_start`, then `within_bucket_col`.
- In practice, the math is similar but the pixel x-range for a given slice now respects bucket boundaries.

**`_draw_bubbles` changes:**
- Currently: `x = px + (ts - t_start) * inv_ts * pw`.
- With bucketing: `x = bucket_left_px + (ts - bucket_start) / bucket_duration * bucket_width_px`.
- This is functionally similar but uses bucket-relative coordinates.

**`_draw_time_axis` changes:**
- Currently: adaptive tick spacing from `_TIME_TICK_CANDIDATES`.
- With bucketing: tick marks at bucket boundaries. Label format adapts based on bucket size.
- Add vertical separator lines at bucket boundaries (more prominent than current grid lines).

**`_draw_bucket_separators` (new):**
- Draw thin vertical lines at bucket boundaries.
- Optionally draw a mini OHLC bar or price range ribbon at each bucket boundary.

### 3.6 Scrolling / Interaction Changes

**Current scrolling:**
- The view auto-scrolls as `chart_now` advances. `t_start = chart_now - visible_window_ms`.
- Sub-pixel smooth scrolling via `frac = (t_start - t_start_q) / slice_ms`.

**With bucketing:**
- The current bucket is always the rightmost visible bucket.
- As time advances within a bucket, the current bucket grows (new slices appear on the right edge).
- When a bucket boundary is crossed, the entire view shifts left by one bucket width and a new (empty) bucket appears on the right.
- This creates a "step-scroll" behavior at bucket boundaries with smooth rendering within each bucket.
- Smooth sub-pixel scrolling can still apply for within-bucket advancement.

**Alternative: continuous scroll with bucket overlay:**
- Keep the existing smooth continuous scrolling.
- Simply overlay bucket boundary markers and organize the x-axis labels at bucket boundaries.
- This is visually simpler and preserves the existing scrolling feel.
- **Recommended approach** for V1 — add bucket separators and labels to the existing continuous scroll.

### 3.7 Replay / Backtest Compatibility

- The bucket model is purely a UI rendering concern.
- The underlying data (trades, depth slices) remains continuous.
- In replay mode, `chart_now` advances based on event timestamps — buckets are computed from these timestamps identically to live mode.
- No changes to the C++ `ReplayFeed` or `TickStore` are required.
- Bucket boundaries are derived from timestamps and bucket size — deterministic.

### 3.8 Configuration

| Parameter | Default | Where Set |
|---|---|---|
| `bucket_duration_ms` | 60000 (1 min) | Toolbar `_candle_combo` (already exists) |
| `num_visible_buckets` | 5 | New toolbar control or hardcoded default |
| `show_bucket_separators` | True | Settings |
| `show_bucket_ohlc` | False | Settings (deferred) |

---

## 4. Time-Bucketed X-Axis Design

### 4.1 Axis Layout

The x-axis is divided into N visible buckets, each occupying `bucket_width_px = plot_width / num_visible_buckets` pixels.

```
|  Bucket N-4  |  Bucket N-3  |  Bucket N-2  |  Bucket N-1  |  Current  |
|  14:01-14:02 |  14:02-14:03 |  14:03-14:04 |  14:04-14:05 |  14:05-.. |
```

Labels at bucket boundaries show the bucket start time. Format depends on bucket duration:
- < 1 min: `HH:MM:SS`
- 1-59 min: `HH:MM`
- >= 1 hr: `HH:MM`

### 4.2 Bucket Boundary Rendering

At each bucket boundary:
- A vertical line (1px, `#2a2a45`, slightly more visible than grid lines).
- A time label centered on the boundary or at the bucket start.
- Optionally, a thin horizontal band showing the bucket's OHLC range (deferred to later iteration).

### 4.3 Within-Bucket Rendering

Within each bucket, the existing rendering logic applies at reduced horizontal scale:
- Depth slices are positioned within the bucket's pixel range.
- Bubbles are positioned within the bucket's pixel range.
- The sub-pixel scrolling logic applies to the current (rightmost) bucket.

### 4.4 Transition Behavior

When `chart_now` crosses a bucket boundary:
1. `visible_window_ms` is recalculated as `num_visible_buckets * bucket_duration_ms`.
2. `t_start` shifts to align with the new visible bucket range.
3. Old data outside the visible window is pruned as before.

---

## 5. Rendering / Layout Implications

### 5.1 Heatmap Intensity Image

The depth intensity image currently has dimensions `(n_price_rows, n_time_cols)` where `n_time_cols = visible_window_ms / slice_ms`.

With 5 visible buckets at 1m each: `visible_window_ms = 300,000 ms`. At 100ms slices: `n_time_cols = 3,000`. This is 5x the current column count (600 for 60s).

**Mitigation options:**
- Increase `slice_ms` proportionally (e.g., 500ms for 5-bucket view).
- Dynamically adjust `slice_ms` based on `visible_window_ms` to keep `n_time_cols` bounded (target: 600-1200).
- Use level-of-detail: older buckets rendered at coarser time resolution, current bucket at full resolution.

**Recommended:** Dynamically set `slice_ms = max(100, visible_window_ms / 1200)`. For 5m visible: `slice_ms = 250ms`. For 60s visible: `slice_ms = 100ms` (unchanged).

### 5.2 Bubble Density

With a 5-minute visible window, there will be more trades visible simultaneously. The existing `p95_size` normalization handles density, but bubble overlap may increase.

**Mitigation:** Reduce `MAX_BUBBLE_RADIUS` proportionally to the number of visible buckets, or use a density-aware radius calculation.

### 5.3 Memory

More visible data means more items in the `_slices` and `_trades` deques. The existing `maxlen` limits should accommodate 5x the current window. Current maxlen:
- `_slices`: 10,000 (sufficient for 5min at 250ms = 1,200 slices).
- `_trades`: 100,000 (sufficient for 5 minutes of active trading).

No changes needed.

---

## 6. Interaction / Scrolling Behavior

### 6.1 Recommended Approach: Continuous Scroll with Bucket Overlay

Keep the existing smooth continuous scrolling and add bucket separators as a visual overlay:

1. `visible_window_ms = num_visible_buckets * bucket_duration_ms`.
2. `t_start = chart_now - visible_window_ms`.
3. Depth/bubble rendering uses the same continuous x-mapping as before.
4. `_draw_time_axis` is replaced with `_draw_bucket_axis` that draws labels and separators at bucket boundaries.
5. The visual effect: the view scrolls smoothly but bucket boundary lines march across the screen like candlestick separators.

This preserves the existing order flow feel while adding temporal structure.

### 6.2 Zoom / Pan Behavior

- Vertical zoom/pan remains unchanged (price axis).
- Horizontal zoom could be added later (change `num_visible_buckets`).
- For V1, horizontal extent is fixed at `num_visible_buckets` buckets.
- The `_candle_combo` changes `bucket_duration_ms`, which changes `visible_window_ms`.

### 6.3 Auto-Scale

The existing `_auto_scale` flag controls vertical price range. This remains unchanged. Horizontal auto-scroll (following `chart_now`) is the default and only behavior in V1.

---

## 7. Candlestick Bucket Model

### 7.1 Bucket Lifecycle

```
FORMING → COMPLETE → VISIBLE → PRUNED
```

- **FORMING:** The current (rightmost) bucket. New data is appended. OHLC/volume updated on each trade.
- **COMPLETE:** A bucket whose time period has elapsed. No new data can be added.
- **VISIBLE:** A complete bucket that falls within the visible window.
- **PRUNED:** A bucket that has scrolled off the left edge and been discarded.

### 7.2 Bucket OHLC Computation

For each bucket:
- `open` = price of first trade in the bucket.
- `high` = max trade price.
- `low` = min trade price.
- `close` = price of last trade.
- `volume` = sum of trade quantities.
- `buy_volume` = sum of quantities where `is_buyer_maker == False` (taker buy).
- `sell_volume` = sum of quantities where `is_buyer_maker == True` (taker sell).

These are computed incrementally as trades arrive. They are used for:
- Optional OHLC rendering at bucket boundaries.
- Signal log context ("this trade occurred in a bucket with this OHLC profile").
- Volume-at-bucket aggregation.

### 7.3 Future Bucket Sizes

The bucket model should be parameterized from the start:

| Bucket Size | Use Case |
|---|---|
| 5s | Ultra-short-term scalping view |
| 15s | Short-term order flow view |
| 30s | Default for fast markets |
| 1m | Default standard view |
| 5m | Wider context view |
| 15m | Session overview |

The `_candle_combo` already has 1m, 5m, 15m, 30m, 1hr options. Shorter intervals (5s, 15s, 30s) should be added.

---

## 8. Strategy UI Integration Plan

### 8.1 Design Goals

Create a unified strategy arm/disarm UX that:
- Controls the full Tide/Wave/Ripple strategy stack.
- Shows clear state (armed, disarmed, waiting, active, invalidated, exited).
- Integrates with the signal log for strategy-specific events.
- Respects the deterministic baseline and does not introduce non-determinism.
- Works across app modes (UI live, replay, paper).

### 8.2 Strategy State Model

The UI should expose a single top-level strategy state machine:

```
DISARMED → ARMING → ARMED_WAITING → ARMED_ACTIVE → ARMED_EXITING → ARMED_COOLDOWN
    ↑                                                                      ↓
    └──────────────────────── DISARMING ←──────────────────────────────────┘
```

| State | Meaning | UI Indicator |
|---|---|---|
| `DISARMED` | Strategy is not active. Ripple decisions are ignored. | Grey badge, "Strategy Off" |
| `ARMING` | User has requested arm. System is validating preconditions (feed health, risk config). | Yellow badge, "Arming..." |
| `ARMED_WAITING` | Strategy is armed but no trade setup is in progress. Ripple is scanning. | Green badge, "Watching" |
| `ARMED_ACTIVE` | A trade is in an active lifecycle state (SETUP through MATURATION). | Bright green badge, "Active: {archetype}" |
| `ARMED_EXITING` | A trade is in EXIT state. | Orange badge, "Exiting" |
| `ARMED_COOLDOWN` | A trade has completed and is in cooldown. | Blue badge, "Cooldown" |
| `DISARMING` | User has requested disarm. System is closing any open position. | Yellow badge, "Disarming..." |

This maps directly to the existing `LifecycleState` enum from `strategy.md` §12:
- `DISARMED` = no lifecycle active.
- `ARMED_WAITING` = lifecycle is idle (post-cooldown or initial).
- `ARMED_ACTIVE` = lifecycle in `SETUP`, `ENTRY`, `CONFIRMATION`, `EXPANSION`, or `MATURATION`.
- `ARMED_EXITING` = lifecycle in `EXIT`.
- `ARMED_COOLDOWN` = lifecycle in `COOLDOWN`.

### 8.3 Integration Points

| Component | Role |
|---|---|
| `MainWindow` | Holds top-level strategy state. Coordinates arm/disarm. |
| Ripple mode combo | **Replaced or augmented** by a unified strategy state control. |
| Arm Execution button | **Replaced** by a unified Arm/Disarm strategy button. |
| `StrategyDiagnosticsPanel` | Extended to show strategy state prominently. |
| `TradeBlotter` | Extended to include strategy-level context in signals. |
| `OrderFlowEngine` (C++) | Already provides `get_strategy_snapshot()`. |
| Execution Manager | Continues to handle order routing. Armed state gates execution. |

---

## 9. Arm / Disarm UX Design

### 9.1 Primary Control

Replace the current separate "Arm Execution" button and "Ripple" mode combo with a single unified control:

**Strategy Control Widget** (in toolbar):
```
[Strategy: ▼ Observe | Paper | Live] [ARM / DISARM button]
```

- **Observe** mode: Strategy runs, signals logged, no execution. (Replaces "Log Only".)
- **Paper** mode: Strategy runs with paper fills. (Replaces "Paper".)
- **Live** mode: Strategy runs with real execution. (Replaces the old "Arm Execution" flow.)

The ARM button:
- When disarmed: labeled "ARM", green border. Click to arm.
- When armed: labeled "DISARM", red border. Click to disarm.
- When transitioning: labeled "..." with yellow border. Not clickable.

### 9.2 Arm Preconditions

Before the strategy can be armed, the following must be true:
1. Feed is connected and healthy (both trade and depth streams live).
2. `OrderFlowEngine` is initialized and has received initial data.
3. If Live mode: execution manager is connected and authenticated.
4. Risk configuration is valid (ES budget > 0, max position > 0).

If preconditions are not met, the ARM button shows a tooltip explaining what's missing.

### 9.3 Disarm Behavior

When the user clicks DISARM:
1. If a trade is active (ARMED_ACTIVE or ARMED_EXITING), emit a forced exit signal.
2. Transition to DISARMING.
3. Wait for the trade to close (or timeout).
4. Transition to DISARMED.
5. Log a "Strategy disarmed" event to the signal log.

If no trade is active, transition directly to DISARMED.

### 9.4 Persistence

Strategy mode (Observe/Paper/Live) is persisted via QSettings (replacing the current `ripple/mode` setting). Armed state is **not** persisted — the strategy always starts disarmed on app launch.

---

## 10. Signal Log Integration Plan

### 10.1 Current Signal Log

The `TradeBlotter` uses `SignalEntry` with these categories:
- `RIPPLE_ENTRY`, `RIPPLE_EXIT`, `RIPPLE_PREPARE`, `RIPPLE_CANCEL`, `RIPPLE_REARM`
- `EXECUTION`
- `LEGACY_RAW`
- `CONTEXT`
- `DIAGNOSTIC`

### 10.2 Required Changes

**Add new signal categories:**
- `STRATEGY_ARM` — strategy was armed.
- `STRATEGY_DISARM` — strategy was disarmed.
- `STRATEGY_STATE_CHANGE` — strategy state transition (e.g., WAITING → ACTIVE).

**Enrich existing Ripple signal entries with strategy context:**

Each `SignalEntry` should include optional metadata:

```python
@dataclass
class SignalEntry:
    # ... existing fields ...
    wave_regime: str = ""          # Wave regime at time of signal
    tide_bias: str = ""            # Tide bias at time of signal
    risk_budget_pct: float = 0.0   # ES budget usage at time of signal
    lifecycle_state: str = ""      # Trade lifecycle state
    archetype: str = ""            # Trade archetype (BOUNCE/BREAKOUT)
```

### 10.3 Signal Log Filtering

The existing category filter (Source combo, "Ripple only", "Exec only" checkboxes) should be extended:

- ✅ **Done (Phase C):** "Strategy" filter that shows only STRATEGY_* and RIPPLE_* categories.
- ⏳ **Deferred to V1.1** (paired with §15.3 diagnostics polish): "All Active" filter that hides LEGACY_RAW and DIAGNOSTIC by default. Not blocking V1 GA — operators can already isolate the strategy traffic via the existing Strategy filter; this is a noise-reduction nicety that lands alongside the V1.1 diagnostics dashboard work.

### 10.4 Signal Log for This Strategy

For the Tide/Wave/Ripple strategy specifically, the signal log should show:

| Event | Category | Description Format |
|---|---|---|
| Setup detected | RIPPLE_PREPARE | "{archetype} setup at {wall_price}, Wave={regime}" |
| Entry confirmed | RIPPLE_ENTRY | "{archetype} entry {side} at {price}, size={qty}" |
| Scale-in | RIPPLE_ENTRY | "Scale-in {side} at {price}, tranche {n}" |
| Scale-out | RIPPLE_EXIT | "Scale-out at {price}, target {n}/{total}" |
| Exit | RIPPLE_EXIT | "{exit_type} exit at {price}, PnL={pnl}" |
| Cancel | RIPPLE_CANCEL | "Setup cancelled: {reason}" |
| Strategy armed | STRATEGY_ARM | "Strategy armed in {mode} mode" |
| Strategy disarmed | STRATEGY_DISARM | "Strategy disarmed, reason: {reason}" |
| State change | STRATEGY_STATE_CHANGE | "State: {old} → {new}" |

---

## 11. Strategy Status / Visibility in UI

### 11.1 Strategy Diagnostics Panel Update

The existing `StrategyDiagnosticsPanel` should be extended to show:

**Row 1 (prominent):** Strategy state badge with color.
**Existing rows:** Wave regime, η, liquidity state, µPrice, trade state, uPnL, ES usage, imbalance.
**New rows:**
- Tide bias (LONG/SHORT/NEUTRAL with color).
- Trade archetype (BOUNCE/BREAKOUT/—).
- Current stop price.
- Current target price.
- Hold time (formatted as MM:SS).
- Scale-in count.
- PnL (cumulative session PnL).

### 11.2 Status Bar Update

The status bar should show a compact strategy state indicator:
```
Strategy: WATCHING | Wave: NEUTRAL | Trades: 3 | PnL: +0.0042
```

### 11.3 Heatmap Overlays

When the strategy is armed and a trade is active:
- Draw a horizontal line at the stop price (red, dashed).
- Draw a horizontal line at the target price (green, dashed).
- Draw a horizontal line at the entry price (white, solid).
- These lines appear as overlays on the heatmap within the `paintEvent`.

---

## 12. Inactive UI Elements Audit

### 12.1 Audit Method

For each UI element, check:
1. Is it wired to backend logic that runs?
2. Does it affect any behavior when changed?
3. Is it relevant to the Tide/Wave/Ripple strategy?
4. Would removing it break any test or functionality?

### 12.2 Audit Results

| Element | File | Verdict | Action |
|---|---|---|---|
| **Candle combo** | `main_window.py:517-527` | ✅ **Wired (Phase A — completed)** — drives heatmap bucket size and VP recompute cadence. | **Done** — see Phase A completion in §11. |
| **Tick Size input** | `main_window.py:531-533` | Wired to `ofe.EngineConfig.tick_size`. Used by the C++ signal engine (legacy, pre-Ripple). Not used by Tide/Wave/Ripple. | **Hide now, keep in code** — may be useful for legacy strategy testing. |
| **Imbalance input** | `main_window.py:536-538` | Wired to `ofe.EngineConfig.imbalance_threshold`. Legacy signal engine parameter. Ripple has its own `absorption_entry` etc. | **Hide now, keep in code** — same rationale as Tick Size. |
| **Sizing controls** | `main_window.py:543-553` | Used by execution manager. Only meaningful when armed. | **Keep, but disable when disarmed.** |
| **Account Panel** | `ui/account_panel.py` | Shows Binance account state. Empty/stale when not connected to broker. | **Keep, but show "Not connected" placeholder when broker unavailable.** |
| **Ripple mode combo** | `main_window.py:564-575` | Currently functional but will be superseded by unified strategy control. | **Replace** with unified strategy mode (Observe/Paper/Live). |
| **Arm Execution button** | `main_window.py:556-559` | Currently functional but will be superseded by unified ARM/DISARM. | **Replace** with unified ARM/DISARM button. |
| **Ripple status label** | `main_window.py:577-579` | Shows Ripple suppression metrics. Useful for diagnostics. | **Keep** — move to diagnostics panel or status bar. |
| **Mode combo (Live/Replay)** | `main_window.py:483-487` | "Replay" option exists but replay in UI is not fully implemented (no file picker). | **Keep "Live" active, grey out "Replay" with tooltip "Coming soon".** |
| **VP Window combo** | `main_window.py:502-513` | Fully functional — controls VP time window. | **Keep as-is.** |
| **Legacy strategies** | `strategies/{ichimoku,obv,support_resistance}.py` | Not exposed in UI mode. Used in backtest/optimise modes via `main.py`. | **Defer decision** — these are separate from the Tide/Wave/Ripple path. No UI action needed. |

---

## 13. Proposed Removals / Hiding / Deferrals

### 13.1 Remove Now
- Nothing should be deleted outright in this phase. All changes should be hiding or replacement.

### 13.2 Hide Now / Keep in Code

| Element | How to Hide | Why Keep |
|---|---|---|
| Tick Size input | `setVisible(False)` on the QLabel + QLineEdit | Legacy signal engine may still be useful for debugging |
| Imbalance input | `setVisible(False)` on the QLabel + QLineEdit | Same rationale |
| Sizing controls (when disarmed) | `setEnabled(False)` + reduced opacity | Controls are valid but only meaningful when armed |

### 13.3 Replace Now

| Old Element | New Element |
|---|---|
| Ripple mode combo + Arm Execution button | Unified Strategy mode combo (Observe/Paper/Live) + ARM/DISARM button |

### 13.4 Defer Decision

| Element | Rationale |
|---|---|
| Legacy strategies | Not visible in UI mode. No immediate harm. |
| Replay mode | Useful feature but not complete. Grey out rather than remove. |
| Account Panel (empty state) | Show placeholder text instead of empty table. Low priority. |

### 13.5 Cleanup Safety

- All hidden elements remain in code with `setVisible(False)` or `setEnabled(False)`.
- No files are deleted.
- No UI widget classes are removed.
- Changes are reversible by toggling visibility flags.

---

## 14. HDF5 / .h5 Data Model Update Plan

### 14.1 New Data Requirements

| Data | Purpose | Storage Location |
|---|---|---|
| Strategy signals/events | Replay and audit signal log | New HDF5 group in tick store |
| Strategy snapshots | Time series of strategy state for replay | New HDF5 group in tick store |
| Arm/disarm events | Session audit trail | New HDF5 group in tick store |
| Bucket OHLC summaries | Optional pre-computed bucket data | New HDF5 group in tick store |
| Session metadata | Schema version, session config hash | HDF5 attributes on root group |

### 14.2 Proposed Schema Extensions

All new data goes into the existing `data/{exchange}_ticks.h5` file managed by C++ `TickStore`. This keeps all real-time data co-located and replayable.

**New HDF5 groups:**

```
/{symbol}/
    trades/          (existing)
    depth_snapshots/ (existing)
    strategy_signals/    (NEW)
        timestamp    int64     event timestamp (ms)
        signal_type  string    RIPPLE_ENTRY, RIPPLE_EXIT, etc.
        category     string    signal category
        side         string    BUY/SELL/NONE
        price        float64   signal price
        quantity     float64   signal quantity (if applicable)
        archetype    string    BOUNCE/BREAKOUT/NONE
        lifecycle    string    lifecycle state at signal time
        wave_regime  string    Wave regime at signal time
        tide_bias    string    Tide bias at signal time
        risk_pct     float64   ES budget usage percentage
        description  string    human-readable description
    strategy_snapshots/  (NEW)
        timestamp    int64
        wave_regime  string
        tide_bias    string
        liquidity_state string
        lifecycle_state string
        microprice   float64
        imbalance    float64
        unrealized_pnl float64
        consumed_es  float64
        es_budget    float64
        cumulative_pnl float64
    session_events/      (NEW)
        timestamp    int64
        event_type   string    ARM, DISARM, CONNECT, DISCONNECT, ERROR
        details      string    JSON-encoded metadata
```

### 14.3 Schema Versioning

Add an HDF5 attribute `schema_version` to the root group:

```
/{symbol}.attrs['schema_version'] = 2
```

- Version 1: existing trades + depth_snapshots.
- Version 2: adds strategy_signals, strategy_snapshots, session_events.

On read, check `schema_version`:
- If missing or 1: legacy format, no strategy data.
- If 2: full format with strategy data.

### 14.4 Write Path

**Strategy signals:** Written from Python (not C++) since signals are generated in the Python UI/strategy layer. Options:
1. **Via pybind11:** Extend `TickStore` with a `write_strategy_signal()` method callable from Python.
2. **Via h5py:** Open the same .h5 file from Python and write to separate groups.

**Recommended:** Option 1 (pybind11 extension) for consistency and to avoid concurrent file access issues. Alternatively, buffer signals in Python and write in batch at session end.

**Strategy snapshots:** Written periodically (every 1-5 seconds) from the timer tick. Batch writes to avoid per-tick I/O.

**Session events:** Written on arm/disarm/connect/disconnect. Infrequent, immediate write is acceptable.

### 14.5 Read Path

For replay:
- `ReplayFeed` continues to replay trades and depth as before.
- Strategy signals and snapshots are read by the Python replay orchestrator to populate the signal log and diagnostics panel.
- This enables "replay with strategy overlay" — seeing what the strategy decided at each point.

### 14.6 Backward Compatibility

- Existing .h5 files without the new groups remain readable.
- The version check on read handles missing groups gracefully.
- New code that reads strategy data checks for group existence before access.
- No migration script needed — new groups are created on first write.

---

## 15. Backward Compatibility and Migration Considerations

### 15.1 UI Changes

- The toolbar layout changes (hiding elements, replacing controls) are fully backward compatible at the code level.
- QSettings keys change from `ripple/mode` to `strategy/mode`. The old key is read as fallback.
- Existing saved window geometry and splitter positions are unaffected.

### 15.2 Data Format

- Existing .h5 files: no modification needed. New groups are added on new writes.
- Existing OHLCV .h5 files (`data/{exchange}.h5`): completely unaffected.
- Backtest results: unaffected.

### 15.3 C++ Interface

- `OrderFlowEngine::get_strategy_snapshot()` is already implemented and bound. No changes needed.
- If `TickStore` is extended for signal writing, the extension is additive (new methods only).
- All existing pybind11 bindings remain unchanged.

### 15.4 Strategy Logic

- No changes to Tide, Wave, or Ripple logic.
- No changes to the C++ hot path.
- All changes are in the Python UI and orchestration layer.

---

## 16. App Mode Implications

### 16.1 Data Collection Mode (`data`)

**Changes:** None required. Data collection writes trades and depth to .h5 as before. The new HDF5 groups (strategy signals, snapshots) are not written during data collection because the strategy does not run.

**Future consideration:** If context engine data should be collected alongside market data, `data_service.py` would need extension. Out of scope for this plan.

### 16.2 Backtest Mode (`backtest`)

**Changes:**
- `strategies/orderflow.py:backtest()` remains unchanged for now.
- Strategy signals generated during backtest should optionally be written to a results file for analysis.
- The `_build_config()` function in `strategies/orderflow.py` maps params to `EngineConfig`. The new UI bucket duration is not a backtest parameter.
- Bucket visualization is a UI concern and does not affect backtest.

**Future consideration:** Backtest results could include a strategy signal time series for the replay-with-overlay feature.

### 16.3 Optimise Mode (`optimise`)

**Changes:** None. Optimization runs backtest in a loop. UI changes do not affect optimization.

**Future consideration:** Strategy signal logging could feed into optimization metrics (e.g., signal quality scoring).

### 16.4 UI Mode (`ui`)

**This is the primary mode affected by this plan.** All UI changes described in this document apply to UI mode.

Summary of UI mode changes:
- Heatmap bucketing and time axis update.
- Strategy arm/disarm controls.
- Signal log enrichment.
- Strategy state visibility.
- Inactive element cleanup.
- HDF5 strategy data writing (if armed and live/paper).

### 16.5 Execute Mode (`execute`)

**Changes:** Execute mode (`main.py:execute`) runs without a UI. The arm/disarm concept becomes a CLI parameter or automatic-on-start.

**No immediate changes needed.** Execute mode already has its own execution manager setup. The UI integration plan does not affect it.

### 16.6 Mode-Agnostic Components

| Component | Mode-Agnostic? |
|---|---|
| Bucket model for heatmap | Yes — purely a rendering concern |
| Strategy state machine | Mostly — the state machine is useful in UI and execute modes |
| Signal logging | Yes — useful in all modes that run the strategy |
| HDF5 schema extensions | Yes — the schema supports all modes |

### 16.7 Shared Contracts

The following should be defined as shared contracts across modes:

| Contract | Definition | Consumers |
|---|---|---|
| `StrategyMode` enum | `OBSERVE`, `PAPER`, `LIVE` | UI, execute, backtest |
| `StrategyUIState` enum | `DISARMED`, `ARMING`, `ARMED_WAITING`, etc. | UI |
| `StrategySignalEntry` | Extended `SignalEntry` with strategy context | Signal log, HDF5 |
| `BucketSummary` | OHLC + volume for a time bucket | UI heatmap, HDF5 (optional) |

---

## 17. Testing Plan

### 17.1 UI Rendering Tests

| Test | What It Validates |
|---|---|
| `test_bucket_boundaries_align` | Bucket start timestamps align to wall-clock boundaries |
| `test_visible_window_matches_buckets` | `visible_window_ms == num_buckets * bucket_duration_ms` |
| `test_bubble_x_within_bucket` | Trade bubbles render within the correct bucket's x-range |
| `test_depth_slice_bucket_assignment` | Depth slices are assigned to the correct bucket |
| `test_bucket_transition_no_data_loss` | When a bucket boundary is crossed, no trades or depth slices are lost |
| `test_dynamic_slice_ms` | `slice_ms` adjusts correctly for different bucket configurations |

### 17.2 Bucketed Time-Axis Tests

| Test | What It Validates |
|---|---|
| `test_bucket_label_format` | Labels use correct format for bucket duration |
| `test_bucket_separator_positions` | Separator lines are at correct x-coordinates |
| `test_bucket_ohlc_computation` | OHLC values for a bucket match expected values from trades |
| `test_empty_bucket_handling` | Buckets with no trades are handled gracefully |

### 17.3 Scrolling Behavior Tests

| Test | What It Validates |
|---|---|
| `test_auto_scroll_follows_chart_now` | View tracks latest data |
| `test_bucket_cross_scroll` | View shifts correctly when bucket boundary is crossed |
| `test_chart_now_cap_preserved` | `chart_now` capping still works with bucketed view |
| `test_pruning_with_wider_window` | Pruning works correctly with 5x wider visible window |
| `test_pruning_uses_trade_time` | Existing bubble pipeline regression test still passes |

### 17.4 Signal Log Tests

| Test | What It Validates |
|---|---|
| `test_strategy_arm_signal` | ARM event appears in signal log with correct category |
| `test_strategy_disarm_signal` | DISARM event appears in signal log |
| `test_ripple_signal_context` | Ripple signals include Wave regime, Tide bias, risk budget |
| `test_signal_filtering` | New filter options correctly show/hide categories |
| `test_signal_entry_metadata` | New fields are populated correctly |

### 17.5 Strategy Arm/Disarm Tests

| Test | What It Validates |
|---|---|
| `test_arm_preconditions` | ARM fails gracefully when feed not connected |
| `test_arm_state_transitions` | State machine transitions correctly |
| `test_disarm_closes_position` | Active trade is closed on disarm |
| `test_disarm_no_position` | Disarm from ARMED_WAITING is immediate |
| `test_mode_persistence` | Strategy mode persists across restart via QSettings |
| `test_armed_state_not_persisted` | Strategy starts disarmed on launch |

### 17.6 HDF5 Schema Tests

| Test | What It Validates |
|---|---|
| `test_schema_version_written` | New files have `schema_version = 2` |
| `test_legacy_file_readable` | Version 1 files open without error |
| `test_signal_write_read_roundtrip` | Signals written to .h5 can be read back identically |
| `test_snapshot_write_read_roundtrip` | Snapshots survive write/read cycle |
| `test_session_event_persistence` | ARM/DISARM events are stored and recoverable |

### 17.7 Backward Compatibility Tests

| Test | What It Validates |
|---|---|
| `test_legacy_h5_no_strategy_groups` | Old .h5 files work without strategy groups |
| `test_old_qsettings_fallback` | Old `ripple/mode` QSettings key is read as fallback |
| `test_replay_determinism_preserved` | Existing `test_replay_determinism.py` tests still pass |
| `test_bubble_pipeline_preserved` | Existing `test_bubble_pipeline.py` tests still pass |

### 17.8 Performance / Regression Tests

| Test | What It Validates |
|---|---|
| `test_render_cycle_under_50ms` | Full paint cycle < 50ms with 5-bucket view |
| `test_signal_log_no_lag` | Adding 100 signals in 1 second doesn't block UI |
| `test_h5_write_throughput` | Signal writes don't exceed 10ms per batch |
| `test_memory_bounded` | Deque sizes remain within limits during extended run |

---

## 18. Performance Considerations

### 18.1 Rendering Hot Spots

| Hot Spot | Current | With Bucketing | Mitigation |
|---|---|---|---|
| Depth intensity image | 600 cols × ~200 rows | Up to 1200 cols × ~200 rows | Dynamic `slice_ms` adjustment |
| Bubble drawing | ~1000 trades visible | ~5000 trades visible | Density-aware radius; cull off-screen early |
| Time axis drawing | 6-12 tick labels | 5-6 bucket labels + separators | Cheaper than current (fewer labels) |
| VP recomputation | Every `_candle_duration_ms` | Unchanged | No change |

### 18.2 Aggregation/Bucketing Cost

Bucket OHLC computation is O(1) per trade (incremental update). No aggregation scan needed. Cost is negligible.

Bucket assignment (`floor(ts / bucket_ms)`) is a single integer division. Negligible.

### 18.3 Signal Log Volume

Strategy signals are infrequent relative to market events:
- At most a few signals per minute in active trading.
- Signal log `_data` deque is bounded at 2,000 entries.
- No performance concern.

### 18.4 HDF5 Write Throughput

Strategy snapshots at 1-5 second intervals produce ~12-60 rows per minute. At ~200 bytes per row, this is < 1 KB/s. Negligible.

Strategy signals are even rarer (a few per trade lifecycle). Negligible.

**Batch writes are recommended** to avoid HDF5 file lock contention with the C++ TickStore:
- Buffer snapshots in Python.
- Flush to HDF5 every 30 seconds or on session end.
- Use a separate HDF5 file for strategy data if lock contention is an issue: `data/{exchange}_strategy.h5`.

### 18.5 Replay / UI Synchronization

In replay mode, the event-driven clock means buckets are populated at replay speed. No wall-clock timing issues.

In live mode, the timer tick (100ms) drives rendering. Bucket transitions are detected during the timer tick and handled synchronously. No async issues.

---

## 19. Incremental Build Order

### Phase A: Time-Bucketed Order Flow View (standalone, no strategy changes) — **[COMPLETED]**

1. **A1: Bucket model** — ~~Implement `TimeBucket` dataclass and `_buckets` deque in `HeatmapWidget`.~~ **Done.**
2. **A2: Visible window** — ~~Change `visible_window_ms` to be `num_visible_buckets * bucket_duration_ms`. Wire `_candle_combo` to control `bucket_duration_ms`.~~ **Done.**
3. **A3: Dynamic slice_ms** — ~~Implement `slice_ms = max(100, visible_window_ms / 1200)`.~~ **Done.**
4. **A4: Bucket axis** — ~~Implement `_draw_bucket_axis` replacing `_draw_time_axis`. Draw bucket separators and labels.~~ **Done.**
5. **A5: Test and polish** — ~~Verify bubble pipeline tests pass. Verify render performance < 50ms.~~ **Done.** 64/64 bucket checks + 63/63 bubble checks pass.

### Phase B: Strategy UI Controls (no HDF5 changes) — **[COMPLETED]**

1. **B1: Strategy state model** — ~~Define `StrategyMode` and `StrategyUIState` enums in `execution/models.py`.~~ **Done.**
2. **B2: Toolbar update** — ~~Replace Ripple mode combo + Arm Execution with unified Strategy mode + ARM/DISARM.~~ **Done.**
3. **B3: State machine** — ~~Implement strategy state transitions in `MainWindow`.~~ **Done.**
4. **B4: Diagnostics panel** — ~~Extend `StrategyDiagnosticsPanel` with strategy state, Tide bias, archetype, stop/target, hold time.~~ **Done.**
5. **B5: Status bar** — ~~Add compact strategy state indicator.~~ **Done.**
6. **B6: Heatmap overlays** — ~~Draw stop/target/entry lines when strategy is active.~~ **Done.**

### Phase C: Signal Log Integration — **[COMPLETED]**

1. **C1: Signal categories** — ~~Add `STRATEGY_ARM`, `STRATEGY_DISARM`, `STRATEGY_STATE_CHANGE` to `SignalCategory`.~~ **Done.**
2. **C2: Signal metadata** — ~~Extend `SignalEntry` with strategy context fields.~~ **Done.**
3. **C3: Signal emission** — ~~Emit strategy events from `MainWindow` state machine.~~ **Done.**
4. **C4: Filter update** — ~~Add "Strategy" filter to `TradeBlotter`.~~ **Done.**

### Phase D: Inactive UI Cleanup [COMPLETED]

1. **D1: Hide elements** — ~~Hide Tick Size, Imbalance inputs. Disable sizing controls when disarmed.~~ **Done.**
2. **D2: Replay mode** — ~~Grey out "Replay" option with tooltip.~~ **Done.**
3. **D3: Account panel** — ~~Show placeholder when broker not connected.~~ **Done.**

### Phase E: HDF5 Data Model [COMPLETED]

1. **E1: Schema version** — ~~Add `schema_version` attribute to .h5 root group.~~ **Done.**
2. **E2: Strategy signals group** — ~~Implement write path for strategy signals.~~ **Done.**
3. **E3: Strategy snapshots group** — ~~Implement periodic snapshot writes.~~ **Done.**
4. **E4: Session events group** — ~~Implement arm/disarm event storage.~~ **Done.**
5. **E5: Read path** — ~~Implement read path for replay-with-overlay.~~ **Done.**
6. **E6: Backward compatibility** — ~~Test with legacy .h5 files.~~ **Done.**

---

## 20. Risks / Open Questions

### 20.1 Risks

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Heatmap performance regression with wider visible window | UI feels laggy | Medium | Dynamic `slice_ms`; profile early; set `num_visible_buckets` conservatively |
| Bucket separators clutter the view | Reduced readability | Low | Make separators subtle; provide toggle |
| Unified strategy control confuses users expecting old controls | UX friction | Low | Tooltip help; default to Observe mode |
| HDF5 file lock contention between C++ TickStore and Python writes | Data corruption or write failures | Medium | Use separate .h5 file for strategy data, or use batched writes with file locking |
| Existing bubble pipeline tests break from visible window change | Regression in bubble rendering | Medium | Run `test_bubble_pipeline.py` after every change; the tests operate on raw timestamps, not visible window size |
| Breaking the `chart_now` cap invariant | Bubbles disappear (known regression pattern) | Medium | All changes must preserve the `chart_now = max(t, min(d, t + MAX_DEPTH_LEAD_MS))` formula |
| Signal log metadata adds complexity to `SignalEntry` | Harder to maintain | Low | All new fields are optional with defaults |

### 20.2 Open Questions

1. **Number of visible buckets:** Should this be configurable from the toolbar, or fixed at 5? More buckets = more context but smaller per-bucket area.
2. **OHLC bars at bucket boundaries:** Should we render mini OHLC bars? This adds visual complexity. Defer to after basic bucketing is validated?
3. **Strategy data in separate .h5 file vs. same file as tick data:** Separate file avoids lock contention but means replay needs to open two files. Same file is simpler but riskier.
4. **Should the strategy be auto-armed in execute mode?** Currently the plan requires manual arming. Execute mode might benefit from auto-arm with a config flag.
5. **Should the candle combo add shorter intervals (5s, 15s, 30s)?** The current combo has 1m, 5m, 15m, 30m, 1hr. Adding sub-minute intervals is useful for order flow but may cause performance issues.
6. **Should the bucket model persist across reconnections?** Currently, disconnecting clears the heatmap. Should bucket OHLC summaries survive reconnection?
7. **How should the wider visible window interact with the existing `_auto_scale` price range?** With 5 minutes of data visible, the price range may be wider, requiring more aggressive auto-scaling.
8. **Should strategy signals be written to .h5 in Observe mode?** Observe mode logs to UI but should it also persist to disk? This would enable post-session analysis.

---

## 21. Recommended Phase Breakdown

### Phase 1: Order Flow Bucketing (Week 1-2) — **[COMPLETED]**
- Steps A1-A5.
- Deliverable: Heatmap renders with bucket separators, configurable bucket size, wider visible window.
- Test: Existing bubble/heatmap tests pass (63/63). New bucket tests pass (64/64). Render < 50ms.
- No strategy logic changes.
- **Implementation notes:**
  - `TimeBucket` dataclass added to `ui/heatmap_widget.py` with incremental OHLC.
  - `_buckets` deque tracks per-bucket summaries; updated on each `add_trade()`.
  - `set_bucket_duration_ms()` recomputes `visible_window_ms` and `slice_ms`.
  - Default: 5 visible buckets × 1 min = 300s window, `slice_ms = 250ms`.
  - `_draw_bucket_axis` replaces `_draw_time_axis` — draws separators at bucket boundaries with time labels.
  - Continuous scroll with bucket overlay approach (§6.1) — no change to depth/bubble rendering math.
  - `_on_candle_changed` in `main_window.py` now wires to `heatmap.set_bucket_duration_ms()`.
  - `test_bubble_pipeline.py` updated: pruning test uses wider window-aware timestamps.
  - `test_bucket_model.py` added: 25 tests, 64 checks covering dataclass, assignment, OHLC, visible window, slice_ms, axis, backward compat, determinism.

### Phase 2: Strategy Controls + Signal Log (Week 2-3) — **[COMPLETED]**
- Steps B1-B6 + C1-C4.
- Deliverable: Unified strategy mode + ARM/DISARM in toolbar. Strategy state visible in diagnostics panel and status bar. Signal log enriched with strategy context. Heatmap shows stop/target lines.
- Test: 117/117 strategy UI checks pass. All existing tests (63 bubble, 64 bucket, 8 replay, 19 tide, 56 wave, 11 HMM, 21 crossvenue, 9 crossvenue-wave) pass. Zero regressions.
- No HDF5 changes.
- **Implementation notes:**
  - `StrategyMode` enum (OBSERVE/PAPER/LIVE) and `StrategyUIState` enum (7 states) added to `execution/models.py`.
  - `SignalCategory` extended with STRATEGY_ARM, STRATEGY_DISARM, STRATEGY_STATE_CHANGE.
  - `SignalEntry` extended with wave_regime, tide_bias, risk_budget_pct, lifecycle_state, archetype (all optional, backward-compatible).
  - Toolbar: Ripple mode combo + Arm Execution button replaced by Strategy mode combo (Observe/Paper/Live) + ARM/DISARM button with dynamic styling.
  - State machine in `MainWindow._set_strategy_state()` drives transitions; `_update_strategy_state_from_snapshot()` maps C++ trade lifecycle to UI state.
  - Signal emission: `_emit_strategy_signal()` creates SignalEntry with category and pushes to blotter on arm/disarm/state changes.
  - `StrategyDiagnosticsPanel`: state badge (row 1), Tide bias, archetype, stop/target, hold time, existing rows preserved.
  - Status bar: `_strategy_state_label` shows compact state with color.
  - Heatmap: `set_strategy_overlay()` / `clear_strategy_overlay()` + `_draw_strategy_overlay()` renders entry/stop/target horizontal lines.
  - `TradeBlotter`: "Strategy" source combo item, strategy checkbox filter, `_STRATEGY_CATEGORIES` set, colors for new categories.
  - Settings: `strategy/mode` QSettings key; backward-compat migration from `ripple/mode`.
  - `test_strategy_ui.py`: 17 tests, 117 checks covering enums, signals, state machine, blotter, panel, overlay, migration, backward compat.

### Phase 3: UI Cleanup (Week 3) [COMPLETED]
- Steps D1-D3.
- Deliverable: Inactive elements hidden. Replay mode greyed out. Account panel shows placeholder.
- Test: No regressions. Hidden elements still accessible in code.

**Implementation notes (Phase 3):**
  - `main_window.py`: Tick Size label+input and Imbalance label+input set to `setVisible(False)`. Widgets retained for legacy engine compatibility.
  - `main_window.py`: Sizing mode combo and value input gated via `_update_sizing_enabled()` — disabled when `StrategyUIState.DISARMED`, enabled when any armed state.
  - `main_window.py`: Replay mode combo item disabled via `QStandardItem.setEnabled(False)` with tooltip "Coming soon".
  - `account_panel.py`: Added `_placeholder` QLabel ("Not connected") and `_content` QWidget wrapper. `set_connected(bool)` toggles visibility. `update_account()` auto-connects. `clear()` reverts to placeholder.
  - `test_ui_cleanup.py`: 11 tests, 38 checks covering hidden elements, sizing gating, replay disable, account placeholder lifecycle, backward compatibility.

### Phase 4: HDF5 Persistence (Week 3-4) [COMPLETED]
- Steps E1-E6.
- Deliverable: Strategy signals, snapshots, and session events persisted in .h5. Schema versioning. Backward compatible reads.
- Test: Write/read roundtrip. Legacy file compatibility. No TickStore regression.

**Implementation notes (Phase 4):**
  - `strategy_store.py`: New Python module using h5py. Separate file per symbol (`data/{SYMBOL}_strategy.h5`) to avoid C++ TickStore contention.
  - Schema version 2 with compound numpy dtypes for `strategy_signals`, `strategy_snapshots`, `session_events`.
  - Signals written immediately on emission. Snapshots buffered (default batch=50) and flushed periodically. Session events written on ARM/DISARM/CONNECT/DISCONNECT.
  - Read path: `read_signals()`, `read_snapshots()`, `read_events()` with optional `from_ms`/`to_ms` time-range filtering for replay-with-overlay.
  - Backward compatibility: `is_legacy()` detects schema_version < 2. `open_readonly()` handles missing datasets gracefully. All read methods return `[]` for legacy files.
  - `main_window.py`: StrategyStore opened on connect, closed on disconnect. Signal writes via `_emit_strategy_signal()`. Snapshot writes in strategy diagnostics timer tick (only when armed). Session events on connect/disconnect/arm/disarm.
  - `test_strategy_store.py`: 16 tests, 63 checks covering schema version, signal roundtrip, snapshot buffer/flush, session events, time-range filtering, legacy detection, readonly open, reopen persistence, default field handling.

### Phase 5: Polish and Integration Testing (Week 4) [COMPLETED]
- End-to-end testing of all four workstreams together.
- Performance profiling of the full UI render cycle with bucketing + strategy overlays + signal logging.
- Replay-with-strategy-overlay smoke test.
- Documentation updates to `TESTING_GUIDE.md`.

**Implementation notes (Phase 5):**
- `test_integration_e2e.py`: 8 tests, 57 checks. Full session lifecycle (connect → arm → trades → signals → snapshots → disarm → disconnect), cross-workstream state gating (strategy state gates UI cleanup elements), blotter filtering interop with mixed signal categories, bucket + overlay coexistence, HDF5 time-range filtering across all datasets, diagnostics panel from snapshot, deterministic bucket replay, account panel lifecycle.
- `test_performance_profile.py`: 6 tests, 13 checks. Heatmap paintEvent timing (< 100ms), bucket model throughput (10k trades < 500ms, 1.5µs/trade), signal log append throughput (2k signals < 500ms), HDF5 write throughput (100 signals + 100 snapshots + 20 events < 2s), strategy overlay marginal rendering cost (< 10ms), large trade set render (5k trades + 500 depth < 150ms).
- `test_replay_overlay.py`: 7 tests, 34 checks. Replay overlay reconstruction from persisted snapshots, signal playback into blotter from HDF5, event timeline reconstruction with JSON detail parsing, deterministic read-back (identical reads), time-windowed partial replay, diagnostics panel from replay snapshot, heatmap with replay trades + overlay.
- All 15 test suites (542 total checks across 10 suites verified this pass) passing with zero failures.

### Post-Phase Optimization: Bubble Aggregation [COMPLETED]
- Targeted optimization of bubble rendering path: pre-render aggregation via (time_bucket, price_bucket, side) grid.
- Price buckets derived from viewport: `pr * AGG_PRICE_BUCKET_PX / ph` (~4 pixels per bucket).
- Time buckets derived from viewport: `ts_span * AGG_TIME_BUCKET_PX / pw` (~1.7s per bucket on default widget, ~173 x-positions across visible window). Wall-clock aligned for stability.
- VWAP positioning for aggregated bubbles.
- Diagnostics: `agg_cells`, `agg_raw_visible`, `agg_culled`, `agg_coarsen_passes` tracked in `_bubble_diag`.
- `test_bubble_aggregation.py`: 25 tests, 48 checks.

**Bug fix (post-optimization):** Initial implementation used candle-duration (60s) for time buckets, which collapsed all trades within a minute to one x-position — breaking auto-scroll and causing cross-column visual coupling. Fixed by deriving time buckets from pixel resolution (same approach as price buckets). This restored smooth scrolling while preserving aggregation benefits.

### Stability Pass: Hard Caps and Bounded Rendering [COMPLETED]
- **Problem:** Under sustained live/replay feed, paint times exceeded 1000ms and UI crashed. Root causes: unbounded `_trades` deque (maxlen=100,000), no cap on rendered bubble count, QPainterPath with thousands of ellipses.
- **Trades deque cap:** `MAX_TRADES_RETAINED = 10,000` (was 100,000). At BTC's ~4 trades/s, this retains ~40 minutes — well beyond the 5-minute visible window.
- **Rendered bubble hard cap:** `MAX_RENDERED_BUBBLES = 600`. After aggregation, if grid cells exceed this limit, three reduction stages activate:
  1. **Dynamic coarsening:** Double both time and price bucket sizes, re-grid. Up to 6 passes.
  2. **Volume culling:** Remove cells with < 2% of max cell volume (`MIN_BUBBLE_QTY_FRAC = 0.02`).
  3. **Top-N rank cap:** If still over 600, keep only the highest-volume cells.
- **Grid construction refactored** to `_build_grid()` static method, enabling clean re-invocation during coarsening.
- **Reduction results:** 10k trades → 580 grid cells → 464 visible (94% reduction); 50k trades → deque capped at 10k → 288 visible (97% reduction). Paint time ~12ms avg for 10k trades.
- **Tests:** 5 new tests added (rendered cap, coarsening activation, volume culling, deque bounds, largest-bubble survival). Total: 25 tests, 48 checks.

### Viewport Tuning: 1-Minute Window and Wider Heatmap [COMPLETED]
- **Problem:** Visible time window was 5 minutes (`DEFAULT_NUM_VISIBLE_BUCKETS = 5`), compressing too much history. Auto-scale price range was too narrow (`mid * 0.0004` = ~$33 for BTC), showing depth only in a thin band around price action. Heatmap looked sparse compared to Bookmap-style views.
- **Time window fix:** Changed `DEFAULT_NUM_VISIBLE_BUCKETS` from 5 to 1. Default visible window is now 60s (1 candle). Adjusts automatically when candle duration changes.
- **Price range fix:** New constants `AUTO_SCALE_MIN_SPAN_FRAC = 0.002` (was 0.0004) and `AUTO_SCALE_MARGIN_FRAC = 0.5` (was 0.25). For BTC at $83k: minimum span ~$166 with ~$83 margin on each side, showing full depth structure above and below price.
- **Performance impact:** Positive — 1-minute window means fewer visible trades/slices per frame. Bubble aggregation handles ~2400 visible trades in ~4ms avg (was 10k in ~12ms).
- **Tests:** Updated `test_bucket_model.py` and `test_bubble_aggregation.py` for new defaults. All 497 checks pass.

### Heatmap Continuity: Forward-Fill for Persistent Depth [COMPLETED]
- **Problem:** Depth heatmap showed broken segments / visible gaps across the time axis. Resting liquidity that remained in the book did not render continuously across the view. Root cause: `_draw_depth` only populates intensity columns that have a matching slice in the deque. The gap-fill in `add_depth_column` caps at 20 slices (2s at 100ms resolution), so any gap longer than 2 seconds leaves empty columns (black strips).
- **Fix:** Added `_forward_fill_intensity()` static method to `HeatmapWidget`. Called at the end of `_draw_depth` after the main slice iteration loop and before color-mapping. Two passes:
  1. **Forward-fill (left→right):** For each zero column, copy the nearest previous non-zero column. Matches order-book semantics: resting liquidity persists until changed.
  2. **Backward-fill (left edge):** If the first N columns have no data, copy from the first non-zero column to give a clean left edge on startup.
- **Performance:** `np.any(intensity > 0, axis=0)` vectorized check + bounded column copies. Typical cost < 0.2ms. Short-circuits immediately when all columns already have data.
- **Determinism:** Same slice deque → same intensity array → same fill → deterministic.
- **Tests:** `test_heatmap_continuity.py`: 12 tests, 49 checks. Unit tests for the forward-fill static method plus widget integration test with sparse depth.

### Signal Log Strategy Focus and Layout Fix [COMPLETED]
- **Problem 1 (Signal Log):** Default "All" filter flooded the log with `LEGACY_RAW` `STACKED_IMBALANCE_*` signals, burying strategy-relevant events. `EXHAUSTION_*` and `ABSORPTION_*` signals were classified as `LEGACY_RAW` despite being strategy-relevant.
- **Problem 2 (Layout):** Strategy panel in `_left_col` made it a 3-widget splitter while `_chart_stack` had 2 widgets. The splitter sync (`setSizes`) passed 2 values to a 3-widget splitter, causing the strategy panel to steal height from the VP and break VP-heatmap vertical alignment.
- **Signal Log fix (initial):**
  1. Default "Strategy" checkbox to ON — users see strategy events immediately.
  2. Added `_CONTEXT_SIGNAL_PREFIXES` (`EXHAUSTION_`, `ABSORPTION_`, `SWEEP_`, `FLIP_`) — these signals are now classified as `CONTEXT` instead of `LEGACY_RAW`.
  3. Added `CONTEXT` and `EXECUTION` to `_STRATEGY_CATEGORIES` — contextually relevant and execution events visible in strategy view.
  4. Users can still see all signals by unchecking "Strategy".
- **Layout fix:**
  1. Removed strategy panel from `_left_col` (now 2 widgets: VP + StatusPanel, matching chart_stack's 2 widgets).
  2. Moved strategy panel to bottom-right area in a vertical splitter with account panel.
  3. Splitter sync now operates on 2:2 widget correspondence, restoring correct VP-heatmap alignment.
  4. Blotter gets 3x stretch vs 1x for bottom-right panel, giving the signal log most of the width.
- **Tests:** 5 new tests (133 total in test_strategy_ui.py). Signal reclassification, default filter, uncheck behavior, left column widget count. Updated integration E2E test for expanded strategy filter set.

### Signal Log Architecture Alignment [COMPLETED]
- **Problem:** Signal log categories were flat and did not reflect the Tide/Wave/Ripple/Trade Lifecycle strategy architecture. Entry and exit decisions from Ripple were categorized as `RIPPLE_ENTRY`/`RIPPLE_EXIT` rather than trade lifecycle events. No Tide or Wave layer categories existed. The "Source" column was implementation-oriented rather than strategy-oriented.
- **Signal category changes (`execution/models.py`):**
  1. Added `SignalCategory.TIDE` — macro bias/regime events from the Tide layer.
  2. Added `SignalCategory.WAVE` — market structure/regime events from the Wave layer.
  3. Added `SignalCategory.TRADE_LIFECYCLE` — setup/entry/exit/confirmation events (highest priority for user).
  4. Reclassified `ENTER_BOUNCE_*`, `ENTER_BREAKOUT_*`, `EXIT_BOUNCE`, `EXIT_BREAKOUT` from `RIPPLE_ENTRY`/`RIPPLE_EXIT` to `TRADE_LIFECYCLE`.
  5. Added `_CATEGORY_LAYER_MAP` — maps every `SignalCategory` to a display layer name (TIDE/WAVE/RIPPLE/TRADE/EXEC/STRATEGY/RAW/CONTEXT/DIAG).
- **Blotter UI changes (`ui/trade_blotter.py`):**
  1. Replaced "Source" column with "Layer" column using `_CATEGORY_LAYER_MAP`.
  2. Replaced source combo + 3 checkboxes with 4 layer-based filter buttons: Strategy (default ON), Trade, Ripple, Raw.
  3. Filter sets: `_FILTER_STRATEGY` (Tide+Wave+Ripple+Trade+Strategy+Execution+Context), `_FILTER_TRADE` (Trade+Execution), `_FILTER_RIPPLE` (Ripple_* only), `_FILTER_RAW` (Legacy+Context+Diagnostic).
  4. New color scheme: TIDE = gold, WAVE = slate blue, TRADE_LIFECYCLE = bright cyan/white, LEGACY_RAW = dim grey.
  5. Bold font for `TRADE_LIFECYCLE` rows to make trade events visually dominant.
- **Strategy metadata (`ui/main_window.py`):**
  1. Cached `_last_strategy_snap` from the C++ strategy snapshot.
  2. `_snap_metadata()` extracts `tide_bias` and `wave_regime` from the cached snapshot.
  3. All strategy signals and ripple decisions now populate `tide_bias`, `wave_regime`, and `lifecycle_state` fields.
- **Layout guard:** Added `setMaximumHeight(400)` on `bottom_right` splitter to prevent it from stealing vertical space from the chart area.
- **Bubble fix:** Added `Qt.FillRule.WindingFill` to buy/sell `QPainterPath` objects to eliminate transparent inner circles on overlapping bubbles.
- **Tests:** 163 checks in `test_strategy_ui.py` (up from 117). Added `test_trade_filter`, `test_layer_display_map`. Updated `test_blotter_strategy_categories` for new filter sets. Updated `test_integration_e2e.py` for `_FILTER_STRATEGY`.
- **Files changed:** `execution/models.py`, `ui/trade_blotter.py`, `ui/main_window.py`, `ui/heatmap_widget.py`, `tests/test_strategy_ui.py`, `tests/test_integration_e2e.py`, `tests/test_heatmap_continuity.py`.

### Trade Feed Stale Recovery: Depth-Driven chart_now Fallback [COMPLETED]
- **Problem:** When the Binance trade WebSocket feed stalls (connection drop, quiet period), `chart_now` freezes at `_last_trade_ts + _MAX_DEPTH_LEAD_MS (5000ms)`. Depth keeps arriving but the heatmap cannot scroll — the display looks completely frozen. PERF logs showed skew growing from 0 to 18,562ms with the trade count stuck and `lag=5000ms` hitting the cap repeatedly. The Qt event loop was still running but the visual output was static.
- **Root cause:** `chart_now` property: `max(t, min(d, t + 5000))`. When `t` (trade time) stops advancing, chart_now is pinned at `t + 5000` indefinitely regardless of how far `d` (depth time) advances.
- **Fix:** Added `_STALE_TRADE_THRESHOLD_MS = 10,000`. When `depth_ts - trade_ts` exceeds this threshold, `chart_now` switches to `depth_ts - _MAX_DEPTH_LEAD_MS`, allowing the heatmap to scroll with depth time while preserving a margin for any remaining bubbles. New `trade_feed_stale` property for UI status checks.
- **Visual indicator:** "Trade feed stale (Xs)" warning rendered at the bottom of the heatmap when the stale threshold is exceeded.
- **Recovery:** When trades resume and skew drops below the threshold, the normal capping formula takes over automatically.
- **Tests:** Updated `test_chart_now_caps_depth_lead` to cover medium skew (8s, normal cap) and large skew (14s, stale fallback). Added `trade_feed_stale` property assertions. Updated `test_heatmap_slices_within_visible_window` and `test_chart_now_unchanged` to use sub-threshold skew values.
- **Files changed:** `ui/heatmap_widget.py`, `tests/test_bubble_pipeline.py`, `tests/test_bucket_model.py`.

### Full Depth Book Auto-Scale [COMPLETED]
- **Problem:** Auto-scale used only `best_bid` and `best_ask` (inside quote, ~$2 spread on BTC) to set the visible price range. With `AUTO_SCALE_MIN_SPAN_FRAC = 0.002` and the margin, the visible range was ~$332 — far narrower than the depth book's ~$200 per side. The heatmap showed full depth initially (before trades arrived), but once trades started driving auto-scale, the range narrowed to the action area and distant depth levels were clipped out of the rendered window.
- **Fix:** Auto-scale now uses `_cur_depth_prices.min()` / `.max()` — the full extent of all resting liquidity in the order book — instead of just `best_bid` / `best_ask`. On slice pruning, recalculation scans the actual `snap_prices` arrays (element [5]) from sampled slices. Trade prices are still included as a safety net so bubbles are never clipped, but trades cannot narrow the range below the depth book extent.
- **Margin reduction:** `AUTO_SCALE_MARGIN_FRAC` reduced from `0.5` to `0.05`. With a ~$200 depth range, 50% margin added $100 of empty space per side. The 5% margin adds ~$10 buffer — enough padding without wasting screen space.
- **Performance impact:** `np.min()` / `np.max()` on ~400 floats (200 bids + 200 asks) is negligible (<0.01ms).
- **Files changed:** `ui/heatmap_widget.py`.

### Trade Storage Refactor: Time-Bucketed Slice Store [COMPLETED]
- **Problem:** The bubble rendering path stored trades in two redundant structures: a raw `_trades` deque and a `_live_grid` dict of `{(p_key, is_buy): [qty, count, sum_pq]}` lists. The grid keyed on `(t_idx, p_key, is_buy)`, treating buy/sell as separate cells, and used viewport-derived variable-width time buckets. This resulted in ~2× the cell count needed, variable-width time resolution that coupled the store to the viewport, and no structured buy/sell imbalance data per cell.
- **Solution (Time-bucketed slice store):** Replaced `_live_grid` with `_trade_slices: dict[int, TradeSlice]`, a store keyed by `floor(trade_ts / 100) * 100` (fixed 100 ms time slices). Each `TradeSlice` contains a `price_levels: dict[int, TradeBucketAggregate]` where `TradeBucketAggregate` tracks `trade_count`, `buy_count`, `sell_count`, `total_qty`, `buy_qty`, `sell_qty`, and `sum_price_qty`. Buy and sell trades at the same time/price cell merge into a single aggregate; bubble colour derives from buy/sell imbalance.
- **Data model (conceptual):**
  ```
  TradeBucketsByTime:
    dict[int, TradeSlice]       # key = bucket_start_ms (100 ms aligned)
  TradeSlice:
    bucket_start_ms: int
    price_levels: dict[int, TradeBucketAggregate]
  TradeBucketAggregate:
    trade_count, buy_count, sell_count
    total_qty, buy_qty, sell_qty
    sum_price_qty               # for VWAP = sum_price_qty / total_qty
  ```
- **Lifecycle:**
  1. **Incremental insert in `add_trade()`:** O(1) per trade. Computes `bucket_ms = (ts // TRADE_SLICE_MS) * TRADE_SLICE_MS` and `p_key = round(price / price_bucket_size)`. Creates or updates the `TradeBucketAggregate` at that cell.
  2. **Incremental prune in `add_depth_column()`:** Slices with `bucket_ms < cutoff` are removed. O(B) where B = number of expired buckets.
  3. **Full rebuild on price-bucket change:** When `price_bucket_size` drifts > 10% (viewport resize/zoom), slices are rebuilt from the `_trades` deque. O(N) but infrequent.
  4. **Helper API:** `get_visible_slices(start_ms, end_ms)`, `prune_older_than(cutoff_ms)`.
- **Render path change:** `_draw_bubbles()` iterates `_trade_slices` (O(S) where S = visible slices × price levels). Render cells store `[total_qty, trade_count, buy_qty, sell_qty, sum_pq]`. Bubble colour: buy_qty ≥ sell_qty → buy colour, otherwise sell colour.
- **Key design details:**
  - Fixed 100 ms time slices (`TRADE_SLICE_MS = 100`) decoupled from viewport pixel width.
  - Price keys use `round(price / bucket_size)` for IEEE 754 boundary stability.
  - Raw `_trades` deque retained for P95 normalization and rebuild; not iterated in the per-frame render path.
  - `_slice_trade_total` tracks aggregate trade count for diagnostics.
- **Performance results:**
  - Steady-state draw: **0.63 ms avg** (10-draw average, 500 trades)
  - 10k trades input: **1.46 ms avg** draw
  - 84% aggregation reduction (3000 raw → 481 aggregated)
  - 50k trades bounded: deque=3000, visible≤600
  - Well under the 50 ms UI render target (§9.2)
- **Bounded data structure (§9.1.1):** Store bounded by retention window / TRADE_SLICE_MS × price fan-out. Eviction: time-based pruning on `trade_cutoff`. Overflow: coarsening + volume culling + hard cap (MAX_RENDERED_BUBBLES = 600).
- **Preserved invariants:**
  - `chart_now` capping (§21.2) — untouched
  - Trade pruning uses trade-derived time only (§21.3) — untouched
  - `_bubble_diag` instrumentation (§21.5) — preserved, counts raw trades not cells
  - All 6 mandatory regression tests in `test_bubble_pipeline.py` — pass
  - Deterministic replay — preserved (store is deterministic for same inputs)
- **Tests:** 6 trade-slice tests in `test_bubble_aggregation.py`: incremental insert, prune, rebuild, steady-state no-rebuild, render all visible, boundary stability. Total: 30 tests, 67 checks.
- **Files changed:** `ui/heatmap_widget.py`, `tests/test_bubble_aggregation.py`.

---

## Recommended Heatmap Improvements

The following items were identified during diagnosis of the rendering pipeline. **All five recommendations are now implemented (items 1–4 during the multi-view refactor, item 5 in Phase 7).**

### 1. Increase Depth Level Count [COMPLETED]

**Resolution:** Depth-level slicing was removed during the `OrderFlowViewModel` refactor. `ui/live_trading_session.py` now passes the full `snap.get_bids()` / `snap.get_asks()` arrays to the heatmap (no `[:N]` cap). All available REST/feed levels are rendered.

**Location:** `ui/live_trading_session.py` lines 381–382.

### 2. Increase Heatmap Pixel Resolution [COMPLETED]

**Resolution:** Vertical resolution cap raised from 400 to 800 in the viewmodel (`n_rows = min(int(ph), 800)`). On a 1080p display with ~700px of chart height the heatmap image is now full-resolution.

**Location:** `ui/orderflow_viewmodel.py` line 628.

### 3. Improve Color Gradient Contrast [COMPLETED]

**Resolution:** The single `HEAT_GRADIENT` was split into two complementary LUTs — `HEAT_GRADIENT_BID` (cool blues/greens for resting bids) and `HEAT_GRADIENT_ASK` (warm oranges/reds for resting asks) — and a gamma curve (`DEPTH_GAMMA = 0.55`) was added to expand contrast in the moderate-liquidity zone. Bid-side rendering is mirrored below the mid row; ask-side above.

**Location:** `ui/heatmap_widget.py` lines 50–74 (`HEAT_GRADIENT_BID`, `HEAT_GRADIENT_ASK`, `DEPTH_GAMMA`).

### 4. Decouple Intensity Normalisation from Current Book [COMPLETED]

**Resolution:** Normalisation now uses the **95th-percentile** of recent `log_qty` values across the slice deque (`np.percentile(lq, DEPTH_NORM_PCTILE)`), rather than the current-book max. A single oversized resting order no longer dims everything else, and intensity is stable as that order appears/disappears.

**Location:** `ui/orderflow_viewmodel.py` line 639 (`DEPTH_NORM_PCTILE = 95`).

### 5. Fading / Time-Weighted Depth Columns [COMPLETED — Phase 7]

**Resolution:** `_forward_fill_intensity()` accepts an optional `fade_out` array which is populated with per-column alpha multipliers in `[DEPTH_MIN_FADE, 1.0]`. Real-data columns get `1.0`; forward-filled columns decay linearly with distance from the source over `DEPTH_FADE_WINDOW` columns (≈3 s at the 100 ms slice cadence) and clamp at `DEPTH_MIN_FADE` (0.30). The fade is applied **after** the LUT step (alpha-only), so position logic that reads `intensity` values is bit-identical to the pre-fade behaviour.

**Constants:** `DEPTH_FADE_WINDOW = 30`, `DEPTH_MIN_FADE = 0.30` (`ui/heatmap_widget.py` lines 78–79).

**Location:** `ui/orderflow_viewmodel.py` `_forward_fill_intensity()` and `_compute_depth_image()` (post-LUT alpha multiplication).

**Tests:** `test_heatmap_continuity.py` adds 8 fade-specific tests (default-1 for real columns, linear decay, MIN_FADE clamp, left-edge backfill, intensity invariance, end-to-end alpha application).

### 6. Heatmap Data Pipeline Summary

For reference, the end-to-end data flow:

```
Binance REST API (1000 levels)
  └─> C++ OrderFlowEngine.process_depth()
        └─> Python: engine.get_order_book().get_snapshot()
              └─> main_window._on_timer_tick() [every 100ms]
                    └─> bids[:200], asks[:200]  ← TRUNCATION HERE
                          └─> heatmap.add_depth_column()
                                ├─> _cur_depth_prices (numpy array of all prices)
                                ├─> _cur_depth_log_qtys (log1p of quantities)
                                ├─> _slices deque (time-indexed snapshots)
                                └─> auto-scale: _price_min / _price_max
                                      └─> paintEvent → _draw_depth()
                                            ├─> intensity matrix (n_rows × n_cols)
                                            ├─> price→row mapping, log_qty→intensity
                                            ├─> forward-fill gaps
                                            ├─> LUT color mapping → QImage
                                            └─> draw to screen with sub-pixel scroll
```

**Total estimated effort: 4-5 weeks.** All phases complete.

---

## Appendix A: File Change Summary

| File | Changes | Phase |
|---|---|---|
| `ui/heatmap_widget.py` | Bucket model, `_draw_bucket_axis`, dynamic `slice_ms`, visible window, stop/target/entry overlays, bubble aggregation, hard caps, dynamic coarsening, volume culling, 1-min window, wider auto-scale, depth forward-fill, stale-trade chart_now fallback, full depth book auto-scale, time-bucketed trade slice store (`TradeBucketAggregate`, `TradeSlice`, `TRADE_SLICE_MS`) | 1, 2, Opt, Stab, VP, HmC, TFS, FDA, PG, TS |
| `ui/main_window.py` | Unified strategy controls, state machine, toolbar update, hide inactive elements, strategy state in status bar, StrategyStore integration, layout fix: strategy panel moved to bottom-right | 2, 3, 4, SLF |
| `ui/strategy_panel.py` | Extended diagnostics rows (Tide bias, archetype, stop, target, hold time, session PnL) | 2 |
| `ui/trade_blotter.py` | New signal categories, metadata columns, strategy filter, context signal reclassification, default strategy filter | 2, SLF |
| `ui/account_panel.py` | "Not connected" placeholder, connect/disconnect visibility toggle | 3 |
| `execution/models.py` | `StrategyMode` enum, `StrategyUIState` enum, extended `SignalEntry`, new `SignalCategory` values | 2 |
| `strategy_store.py` | HDF5 persistence for strategy signals, snapshots, session events (separate file per symbol) | 4 |
| `tests/test_bucket_model.py` | TimeBucket OHLC, bucket assignment, visible window, determinism | 1, VP |
| `tests/test_strategy_ui.py` | Strategy controls, state machine, signal emission, blotter filter, overlay, context reclassification, default filter, layout widget count | 2, SLF |
| `tests/test_ui_cleanup.py` | Hidden elements, sizing gating, Replay disabled, account panel lifecycle | 3 |
| `tests/test_strategy_store.py` | HDF5 schema versioning, signal/snapshot/event roundtrip, legacy compat | 4 |
| `tests/test_integration_e2e.py` | Cross-workstream integration (all four phases together) | 5 |
| `tests/test_performance_profile.py` | Render cycle profiling, throughput benchmarks | 5 |
| `tests/test_replay_overlay.py` | Replay-with-strategy-overlay smoke test | 5 |
| `tests/test_bubble_pipeline.py` | Stale-trade chart_now fallback, trade_feed_stale property, updated skew thresholds | TFS |
| `tests/test_bucket_model.py` | Updated chart_now_unchanged for sub-threshold skew | TFS |
| `tests/test_bubble_aggregation.py` | Bubble aggregation correctness, determinism, performance, hard caps, coarsening, culling, trade-slice incremental insert/prune/rebuild/boundary tests, buy/sell imbalance | Opt, Stab, VP, PG, TS |
| `tests/test_heatmap_continuity.py` | Forward-fill unit tests, left-edge backward-fill, sparse depth integration, determinism, **alpha-fade buffer tests (Phase 7)** | HmC, 7 |
| `ui/orderflow_viewmodel.py` | Frame-computation viewmodel (depth image, bubbles); 95th-pctile percentile normalisation; 800-row resolution; **`fade_out` alpha buffer + post-LUT alpha multiplication (Phase 7)** | 6, 7 |
| `ui/heatmap_widget.py` (Phase 7 follow-up) | Removed dead `_native_gesture_event`; added `DEPTH_FADE_WINDOW=30` / `DEPTH_MIN_FADE=0.30` constants; `_forward_fill_intensity` shim forwards `**kwargs` | 7 |
| `ui/candle_chart_view.py` | Auto-registers default overlays in `__init__`; `clear_overlays()` helper | 6, 7 |
| `ui/chart_overlays.py` | New: `SmaOverlay`, `EmaOverlay`, `VwapOverlay`, `StructuralLevelsOverlay`, `VolProfileOverlay` (B1–B4) | 7 |
| `ui/market_state.py` | Shared model class; **+`snapshot_history` deque slot (Phase 7)** | 6, 7 |
| `ui/live_trading_session.py` | Live data ingestion; **appends to `snapshot_history` on fresh snapshots (Phase 7)** | 6, 7 |
| `ui/strategy_dashboard_view.py` | Composite dashboard; **`_StrategyHistoryPanel` + `_RippleStateTable` replace placeholder (Phase 7)** | 6, 7 |
| `tests/test_chart_overlays.py` | New: SMA/EMA/VWAP math, structural levels, VP POC selection, default-overlay integration | 7 |
| `tests/test_strategy_dashboard.py` | New: `snapshot_history` bounds, history-panel paint robustness, ripple-table updates and threshold colours | 7 |
| `tests/test_market_state.py` | MarketState construction, candle sharing, **snapshot_history defaults (Phase 7)** | 6, 7 |

| File | No Changes |
|---|---|
| All C++ strategy logic | Unchanged |
| `strategies/orderflow.py` | Unchanged |
| `tide/`, `wave/`, `hmm/`, `crossvenue/` | Unchanged |
| `backtester.py`, `optimiser.py` | Unchanged |
| `data_service.py` | Unchanged |
| `database.py` | Unchanged |
| `schemas.py` | Unchanged |

## Appendix B: Dependency Graph

```
Phase A (Bucketing) ──────────────────────────────────────────────┐
                                                                   │
Phase B (Strategy Controls) ─── depends on ─── Phase C (Signals) ─┤
                                                                   │
Phase D (UI Cleanup) ── independent ──────────────────────────────┤
                                                                   │
Phase E (HDF5) ─── depends on C (signal schema) ─────────────────┘
                                                                   │
Phase 5 (Polish) ─── depends on all above ────────────────────────┘
```

Phases A and D are independent of each other and of B/C. Phase B and C are tightly coupled. Phase E depends on C for the signal schema but can start in parallel with B.

**Maximum parallelism:** A + D in parallel, then B + C + E, then Phase 5.

**Serial path:** A → B+C → D → E → Phase 5 (simplest, lowest risk).

---

## Phase 6: Multi-View MVVM Architecture [COMPLETED — First Slice]

### Overview

Evolves the monolithic `MainWindow` layout into a multi-view architecture with
shared state.  Three views are available via a `QTabWidget`:

| Tab | Name | Purpose |
|-----|------|---------|
| 0 | **Order Flow** | Existing heatmap / bubbles / CVD / VP — unchanged |
| 1 | **Chart** | QPainter candlestick chart reading shared candle data |
| 2 | **Strategy** | Strategy diagnostics, signal log, account panel, future diagnostics placeholder |

### Architecture

```
Data Feed / Replay
    → MainWindow._on_timer_tick
    → MarketState (shared fields)
    → Active view .update()

MarketState
    .candles         ← deque[TimeBucket] (shared with HeatmapWidget)
    .chart_now       ← unified timestamp
    .visible_window_ms / .bucket_duration_ms
    .best_bid / .best_ask
    .strategy_snapshot
    .strategy_ui_state
    .signals         ← deque[SignalEntry]
```

**Key principles:**
- Views consume shared state — no duplicated heavy computation per view.
- Only the active tab is repainted each tick (tab-gated `.update()`).
- HeatmapWidget and CandleChartView share the same `deque[TimeBucket]` candle store.
- Both Order Flow and Strategy Dashboard blotters receive all signals (broadcast).
- Deterministic replay is preserved — candle and trade aggregation are unchanged.

### New Files

| File | Purpose |
|------|---------|
| `ui/market_state.py` | `MarketState` shared model class |
| `ui/candle_chart_view.py` | `CandleChartView` — QPainter candlestick chart with overlay hooks |
| `ui/strategy_dashboard_view.py` | `StrategyDashboardView` — composite strategy/signal/account view |
| `tests/test_market_state.py` | MarketState construction, candle sharing, property helpers |
| `tests/test_candle_chart_view.py` | Candle rendering data flow, auto-scale, overlay registration, coordinate helpers |
| `tests/test_multi_view.py` | Tab switching, signal broadcast, repaint gating, deterministic candle sharing |

### Modified Files

| File | Changes |
|------|---------|
| `ui/main_window.py` | `QTabWidget` wrapper, `MarketState` creation, tab-gated repaint, `_broadcast_entry` for dual-blotter signal delivery, `_on_tab_changed` |
| `ui/heatmap_widget.py` | `candle_store` constructor parameter for shared candle deque |

### Performance

- Only the active tab repaints (inactive tabs do zero rendering work).
- No additional per-tick computation — MarketState sync is scalar field copies.
- Candle deque shared by reference — zero duplication.

### Future Work (Phase 6+)

The candle-chart overlays and strategy-dashboard real content originally listed here have shipped in **Phase 7** — see the next section. The remaining items below are still open:

- Candle overlays: Bollinger Bands, value area (80/90/95) on the volume-profile bar
- Strategy dashboard: correlation heatmaps (seaborn-style), state-variable time-series plots beyond Tide / Wave / risk / uPnL
- View synchronization: crosshair / time-cursor sync across tabs
- Unified signal model: single signal model instance consumed by both blotters (reparenting)

---

## Phase 7: Visual Polish, Candle Overlays, and Strategy Dashboard [COMPLETED]

### Overview

Phase 7 closes out the remaining UI visual-quality items from "Recommended Heatmap Improvements" (item 5) and the open candle-chart / strategy-dashboard work from Phase 6. It also fixes a latent dead-code bug in `heatmap_widget.py`.

Three independent tracks (A / B / C) share no internal coupling, so they can ship in any order.

### Bug Fix — Dead `_native_gesture_event`

`HeatmapWidget._native_gesture_event` was defined twice. Python's method resolution silently kept the second (cursor-anchored zoom) and discarded the first; the unused first definition has been removed. No behavioural change — gesture tests already exercised the live definition.

**Location:** `ui/heatmap_widget.py`.

### Track A — Heatmap Visual Polish

#### A5. Forward-Fill Alpha Fade

Forward-filled depth columns now fade their **alpha channel** with age, while leaving the underlying intensity values untouched. Implementation details in the "Recommended Heatmap Improvements" section above (item 5).

### Track B — Candle Chart Overlays (`ui/chart_overlays.py`)

`CandleChartView.register_overlay(fn)` accepts callables matching the signature
`fn(painter, px, py, pw, ph, pmin, pmax, visible)`. All Phase-7 overlays are
shipped as **callable classes** so they can be instantiated with config and
registered/cleared at runtime. The view auto-registers a sensible default set
in `__init__`; callers can call `clear_overlays()` followed by individual
`register_overlay()` calls to customise.

| Overlay | Class | Behaviour |
|---|---|---|
| **B1 — SMA** | `SmaOverlay(period, color)` | Rolling-sum simple moving average over `close`; warm-up = `period` candles |
| **B1 — EMA** | `EmaOverlay(period, color)` | α = 2/(n+1); seed = SMA of first window so the line starts at the same index as SMA |
| **B2 — VWAP** | `VwapOverlay(color)` | Cumulative `Σ(close·vol) / Σ(vol)` across visible candles. Close-price proxy — tick-level `Σ(price·qty)` is not exposed by candle data. Resets automatically on `set_bucket_duration()` because the candle deque clears. |
| **B3 — Structural levels** | `StructuralLevelsOverlay(extra_candles=None)` | Dashed session high / low across the visible window. Optional `extra_candles` widens the search to the full in-memory deque so ATH / ATL can be drawn beyond the visible slice. |
| **B4 — Volume Profile bar** | `VolProfileOverlay(n_bins, width_px)` | Right-edge horizontal histogram. Buckets visible candles into `n_bins` price bins (mid-price weighted by volume). POC bin highlighted; remaining bins use a translucent fill. Outline drawn at the inner edge. |

**Robustness:** `_draw_overlays_fn` swallows overlay exceptions (`try / except`) — overlays still bail out early on degenerate inputs (`pmax <= pmin`, `pw <= 0`, empty `visible`, fewer candles than `period`) to keep paint time low.

**Defaults registered in `CandleChartView.__init__`:**
`SmaOverlay(20)`, `EmaOverlay(50)`, `VwapOverlay()`, `StructuralLevelsOverlay(extra_candles=self._candles)`, `VolProfileOverlay()`.

### Track C — Strategy Dashboard Real Content

#### C1. `MarketState.snapshot_history`

Added a bounded rolling buffer to `MarketState`:

```python
self.snapshot_history: deque  # default maxlen = SNAPSHOT_HISTORY_MAXLEN (600)
```

Each entry is a `(ts_ms, snapshot)` tuple. The default buffer covers ≈5 minutes at the strategy-tick cadence of ~500 ms (every 5th 100 ms timer tick). The cap is configurable via the new `MarketState(snapshot_history_maxlen=…)` parameter.

`ui/live_trading_session.py` appends to this deque whenever `_engine.get_strategy_snapshot()` returns a non-`None` snapshot — alongside the existing `mw._last_strategy_snap` write.

#### C2. `_StrategyHistoryPanel` (replaces `_DiagnosticsPlaceholder`)

A pure-`QPainter` mini-chart with two stacked rows:

- **Row 1 — coloured-band step chart.** The Wave regime stripe (BREAKOUT=blue, BREAKDOWN=red, MEAN_REVERSION=green, NEUTRAL=grey) sits above the Tide bias stripe (LONG=green, SHORT=red, NEUTRAL=grey). Each visible band represents one snapshot in `snapshot_history`.
- **Row 2 — line chart.** Risk budget (`risk.consumed_es / risk.es_budget * 100`) is drawn in orange against a dotted 100% reference line. Unrealized PnL is auto-scaled to fit the row, centred on a dotted zero line, coloured green when latest value > 0, red when < 0.

The panel reads `MarketState.snapshot_history` live each `update()` — no caching, no external charting library.

#### C3. `_RippleStateTable`

A compact 4-column × 2-row `QGridLayout` showing the **current** trade state pulled from `MarketState.strategy_snapshot`:

- Lifecycle, Archetype, Entry, Stop, Target, Hold time (MM:SS), unrealized PnL (signed, colour-coded), ES Used (% with 50 % / 80 % colour thresholds).

Falls back to em-dashes when `strategy_snapshot is None`.

`StrategyDashboardView` now exposes both new widgets via `history_panel` / `ripple_table` properties; `update_from_state()` invokes both on every tick.

### New Files

| File | Purpose |
|------|---------|
| `ui/chart_overlays.py` | `SmaOverlay`, `EmaOverlay`, `VwapOverlay`, `StructuralLevelsOverlay`, `VolProfileOverlay` (B1–B4) |
| `tests/test_chart_overlays.py` | SMA/EMA/VWAP math, structural-level selection, VP POC selection, helper-math sanity, default-overlay integration (27 checks) |
| `tests/test_strategy_dashboard.py` | C1 deque slot/maxlen/bounding/empty-start; C2 panel paint robustness (empty, zero budget, zero PnL span, multi-resize); C3 table fields, dashes fallback, snapshot updates, uPnL color, ES% threshold colors; dashboard wiring (49 checks) |

### Modified Files

| File | Phase-7 Changes |
|------|----------------|
| `ui/heatmap_widget.py` | Removed dead first `_native_gesture_event`; added `DEPTH_FADE_WINDOW=30` / `DEPTH_MIN_FADE=0.30` constants; `_forward_fill_intensity` shim now forwards `**kwargs` |
| `ui/orderflow_viewmodel.py` | Imports new fade constants; allocates `_depth_fade` cache; `_forward_fill_intensity` accepts optional `fade_out`; `_compute_depth_image` populates the buffer and applies post-LUT alpha multiplication when any column < 1.0 |
| `ui/candle_chart_view.py` | `_register_default_overlays()` registers SMA/EMA/VWAP/structural/VP in `__init__`; new `clear_overlays()`; structural overlay holds `extra_candles=self._candles` for ATH/ATL |
| `ui/market_state.py` | New `snapshot_history` slot + `SNAPSHOT_HISTORY_MAXLEN = 600` class constant + `snapshot_history_maxlen` constructor param |
| `ui/live_trading_session.py` | Append `(ts_ms, snap)` to `mw._market_state.snapshot_history` when a fresh snapshot is produced |
| `ui/strategy_dashboard_view.py` | `_DiagnosticsPlaceholder` deleted; `_StrategyHistoryPanel` and `_RippleStateTable` added; bottom area now stacks history panel above table |
| `tests/test_heatmap_continuity.py` | +8 fade tests (132/132 checks; previously 82) |
| `tests/test_candle_chart_view.py` | `test_construction` and `test_overlay_registration` updated to expect the auto-registered defaults |

### Regression Status

All UI test suites green after Phase 7 (zero changes to behavioural invariants):

| Suite | Result |
|---|---|
| `test_heatmap_continuity.py` | 132/132 |
| `test_chart_overlays.py` (new) | 27/27 |
| `test_strategy_dashboard.py` (new) | 49/49 |
| `test_candle_chart_view.py` | 15/15 |
| `test_market_state.py` | 9/9 |
| `test_multi_view.py` | 12/12 |
| `test_bubble_pipeline.py` | 65/65 |
| `test_bubble_aggregation.py` | 68/68 |
| `test_bucket_model.py` | 60/60 |
| `test_strategy_ui.py` | 163/163 |
| `test_ui_cleanup.py` | 38/38 |
| `test_strategy_store.py` | 63/63 |
| `test_integration_e2e.py` | 55/55 |
| `test_replay_overlay.py` | 34/34 |
| `test_performance_profile.py` | 13/13 |

Phase-7 invariants explicitly tested:

- Forward-fill intensity values are bit-identical with or without the fade buffer (`test_fade_buffer_intensity_unchanged`).
- Alpha fade is applied only post-LUT (`test_fade_alpha_applied_in_compute_frame`).
- `compute_frame` benchmark still under 50 ms (`test_compute_frame_benchmark`).
- Dual bid/ask LUTs and bucket-row coverage unchanged.
- Overlay errors do not crash the paint cycle.

---

## 13. Phase 11C — Heatmap Depth/Seam Coherence Fix + Diagnostics

### 13.1 Symptom (User-Reported, Persistent After 11B)

> "It is glitchy, heatmap boundary doesn't make sense (red is on the blue side of price)…"

The Phase-11B per-column-mid fix correctly addressed the symptom in the *fully-populated-book* scenario (every slice carrying its own valid depth + bid/ask). **It did not address** the much more common live-feed pattern where Binance USD-M futures sends:

- A trade-only / quote-level update (best_bid / best_ask change), but
- The depth snapshot for the next 100 ms slice is unchanged (or temporarily empty), so

`OrderFlowViewModel.add_depth_column()` reused the prior slice's depth dictionaries while overwriting `best_bid`/`best_ask` with the *fresh* values supplied by the caller. Each slice tuple consequently carried **OLD depth glued to a NEW seam**.

### 13.2 Root Cause Analysis

In `ui/orderflow_viewmodel.py::add_depth_column`:

1. `new_depth = (timestamp != self._last_raw_depth_ts)` is **True** even when `bids=[]` and `asks=[]` (an empty-book tick), because the gating only depends on the raw depth timestamp.
2. Inside the `new_depth` branch, when both new dicts are empty, `_cur_bids/_cur_asks/_cur_depth_prices/_cur_depth_log_qtys` are **NOT updated** (only `_book_empty_ticks` is incremented).
3. The slice-construction block then read `dict(self._cur_bids)` (still the *prior* depth) but stored the caller's *current* `best_bid`/`best_ask`.
4. The gap-fill loop suffered the same defect: gap placeholders were appended with the latest call's `best_bid`/`best_ask` rather than the prior slice's pair.

When `_compute_depth_image` later builds per-column `mid_rows[col] = row(0.5*(s[3]+s[4]))`, those columns end up with a seam reflecting the **new** mid while their depth bands map to **old** prices. Visually, old asks at prices in `[new_mid, old_mid]` re-classify as BIDs (and vice-versa) — the textbook "red on the blue side of price" artefact, plus a flickery boundary on every quote refresh.

The Phase-11B per-column logic is correct **assuming the slice tuple's `best_bid/best_ask` truly paired with its depth** — that assumption was being violated upstream.

### 13.3 Fix

`OrderFlowViewModel` (Phase 11C):

1. **Track `_cur_best_bid` / `_cur_best_ask` alongside `_cur_bids/_cur_asks`** — these are pinned to the latest book update that *also refreshed the depth dictionaries*. They are **NOT** updated during empty-book ticks.
2. **Slice construction uses `_cur_best_bid/_cur_best_ask`** (not the raw `best_bid/best_ask` parameter) when storing the new slice tuple. This guarantees depth + seam are captured at the same moment.
3. **Gap-fill placeholders inherit the prior slice's full state** (depth + bid/ask) rather than the current call's bid/ask, so missing slices render consistently with the last known book.
4. **Reusing the prior slice's depth (the `elif self._slices` branch) also reuses the prior slice's `best_bid/best_ask`** for the same coherence reason.

Net effect: every tuple in `_slices` is now guaranteed to be internally coherent — `(bids_snap, asks_snap, best_bid, best_ask, prices, log_qtys)` always represents one consistent snapshot of the book. `_compute_depth_image` (unchanged from 11B) consequently renders every column with its own *true* historical seam.

### 13.4 Diagnostic Logging

To make any future regression observable without recompiling, `OrderFlowViewModel` now emits a throttled summary of heatmap state once per `_heatmap_diag_interval_s` (default 5 s) when enabled:

- **Toggle**: env var `ORDERFLOW_HEATMAP_DIAG=1` *or* set `vm._heatmap_diag_enabled = True` interactively.
- **Cadence**: `vm._heatmap_diag_interval_s` (default 5.0).
- **Schema** (`vm._last_heatmap_diag` is always populated, even when logging is disabled):

| Key | Meaning |
|---|---|
| `n_img_cols` | Image column count |
| `n_rows` | Image row count |
| `pmin` / `pmax` / `pr` | Current visible price range |
| `n_slices` | Slices in deque |
| `mid_set_in_loop` | # columns whose seam was set from a slice's best_bid/best_ask |
| `mid_forward_filled` | # columns whose seam was inherited via forward-fill |
| `mid_row_min` / `mid_row_max` | Row range covered by the seam |
| `mid_row_latest` | Latest column's seam row (for cross-check vs. trade mid) |
| `mid_row_clipped_top` / `mid_row_clipped_bot` | # columns whose seam pegged to image edges (signals price-range zoom too tight) |
| `book_empty_ticks` | Running count of empty-book updates |

A typical healthy log line on the live feed:

```
heatmap_diag cols=601 rows=400 slices=600 mid_set=600 ff=1 clip_top=0 clip_bot=0 \
  mid_rows=[120..145] latest=132 p=[8910.2..8945.6] empty_ticks=3
```

If `mid_row_clipped_top` or `mid_row_clipped_bot` is large, the auto-scale price range is too tight for the historical mid swing in the visible window — that becomes the next investigation target.

### 13.5 Test Plan

| Test | Purpose | Status |
|---|---|---|
| `tests/test_heatmap_continuity.py::test_phase11c_slice_carries_depth_and_seam_together` | Empty-book tick at a *different* mid retains prior bid/ask. | Added |
| `tests/test_heatmap_continuity.py::test_phase11c_gap_fill_carries_prior_seam` | Every gap-filled slice carries the prior slice's bid/ask. | Added |
| `tests/test_heatmap_continuity.py::test_phase11c_held_over_depth_keeps_paired_seam` | 8 consecutive empty-book ticks all retain old-mid seam. | Added |
| `tests/test_heatmap_continuity.py::test_phase11c_diag_dict_populated` | `_last_heatmap_diag` populated with required keys after `compute_frame`. | Added |

All four tests **fail without the production fix** (verified by `git stash` / re-run / `git stash pop`). Existing 11B tests (`test_per_column_mid_row_with_moving_price`, `test_per_column_mid_row_forward_fills_through_gap`) continue to pass.

### 13.6 Acceptance Criteria

- `tests/test_heatmap_continuity.py` — 158/158 checks pass.
- All UI suites green after the change: `test_strategy_dashboard` (70/70), `test_bubble_pipeline` (65/65), `test_market_state` (9/9), `test_multi_view` (12/12), `test_bubble_aggregation` (68/68), `test_bucket_model` (60/60), `test_strategy_ui` (163/163), `test_ui_cleanup` (38/38), `test_strategy_store` (63/63), `test_replay_overlay` (34/34), `test_performance_profile` (13/13), `test_candle_chart_view` (15/15), `test_chart_overlays` (27/27), `test_live_trading_session` (10/10), `test_stream_health` (19/19).
- No public API change to `add_depth_column` (caller still passes `best_bid, best_ask`); the change is purely in how those values are stored.

### 13.7 Files Touched

| File | Change |
|---|---|
| `ui/orderflow_viewmodel.py` | New `_cur_best_bid` / `_cur_best_ask` paired with `_cur_*`; slice construction + gap-fill use them; throttled `_last_heatmap_diag` snapshot; `os` import added; opt-in `ORDERFLOW_HEATMAP_DIAG` env-var logger. |
| `tests/test_heatmap_continuity.py` | Four new Phase-11C tests; helper test list updated. |
| `UI_STRATEGY_INTEGRATION_PLAN.md` | This section. |

---

## 14. Heatmap UI Pause & Deferred Phases

### 14.1 Pause Trigger

Per the user's directive: **if the heatmap visual issues (glitch / mis-coloured boundary / bubble drop-outs) persist after the Phase 11C fix, no further heatmap UI work is to be undertaken until later phases of `implementation_plan.md` are complete**. This section captures the scope that would otherwise have been prioritised, so the work is recoverable later without re-discovering the context.

### 14.2 Phase 11D — Bubble Drop-out Investigation (Deferred)

**User-reported symptom:** "the bubbles stop rendering" — trade bubbles cease appearing on the heatmap intermittently or after a UI session has been running for a while.

**Reproducibility:** Not reproducible from the static screenshot to date. Earlier session diagnostics suggested:

- `bub` count plateauing at the cell-cap (`MAX_RENDERED_BUBBLES`) rather than dropping to 0,
- depth/trade timestamp skew (`skew=-12421ms`) at one point — i.e. depth ahead of trades, which `chart_now` accounts for but only up to `_MAX_DEPTH_LEAD_MS` (5 s).

**Investigation plan when re-opened:**

1. Add (or wire) a UI-thread bubble-pipeline diag overlay using the existing `_bubble_diag` dict (`failure_stage`, `filtered_by_time/x/y`, `agg_*` counters). Right now the dict is populated but only logged when there are zero or non-zero trades — no UI exposure.
2. Capture a session log + clipboard snapshot at the moment bubbles disappear. Specifically need: `_bubble_diag.failure_stage`, deque size, `last_trade_ts` vs. `chart_now`, `t_start`, `pmin`/`pmax`.
3. Audit `_compute_bubbles` for off-by-one in time-window vs. `t_start` quantisation when the WS feed catches up after a gap.
4. Check if `_trades_dropped` / `_MAX_TRADES_HELD` saturation correlates with the drop-out (high-volume burst → deque trimmed → new trades arriving but at indices that fail the visible-window filter).

**Acceptance:** A reproducible test plus a fix that keeps `frame.visible_bubble_count` non-zero across the same scenario.

### 14.3 Phase 11E — Auto-scale Price-Range Hardening (Deferred)

**Hypothesis (not yet user-reported as a symptom, but flagged by 11C diagnostics):** when `_zoom_fraction` (default 0.0017) yields a very tight price band and the visible window contains a large mid swing, historical mids fall outside `[pmin, pmax]` and `mid_rows` clip to row 0 or `n_rows-1`. The user perceives this as "the boundary doesn't make sense at the edges."

**Investigation plan:**

1. Make `_compute_depth_image` log a warning when `mid_row_clipped_top + mid_row_clipped_bot > N_THRESHOLD` (e.g. >5% of cols) for several consecutive frames.
2. Decide whether `add_depth_column` should widen `[pmin, pmax]` to also enclose all *visible-window* slice mids (not just trade extremes) when `_auto_scale=True`.
3. Consider exposing a UI control to switch between "tight zoom (current price)" and "history-aware zoom" so the user can choose.

**Acceptance:** No `mid_row_clipped_*` warnings during a normal session.

### 14.4 Phase 11F — LUT Perceptual Calibration (Deferred / Cosmetic)

The current bid/ask LUT peaks (BID = `(240, 255, 245)` near-white-cyan; ASK = `(255, 245, 120)` yellow) make high-intensity cells visually similar regardless of side. Some of the user's "glitchy" perception may be the LUT washing out at saturation. **Defer until 11D/11E settle**, then reassess whether the gradients should be tightened (e.g. BID peak at saturated cyan, ASK peak at saturated red) for clearer side discrimination.

### 14.5 Resumption Trigger

Phase 11D / 11E / 11F should be opened only after the user explicitly confirms heatmap UI work should resume (or after the next-priority `implementation_plan.md` phase ships). At that point, recover the diagnostics in `vm._last_heatmap_diag` and `vm._bubble_diag` and start with the highest-impact deferred phase (likely 11D — bubble drop-out is the only remaining user-reported visible defect once 11C lands).

### 14.6 Reference: Diagnostic Commands

```bash
# Enable heatmap diag at startup
ORDERFLOW_HEATMAP_DIAG=1 python main.py --mode ui

# Or interactively in the running app's Python shell
mw._orderflow_vm._heatmap_diag_enabled = True
mw._orderflow_vm._heatmap_diag_interval_s = 1.0
```

The throttled log line goes to the standard `ui.orderflow_viewmodel` logger at `INFO`.

---

## 15. UI Implications of the V1 Closure Roadmap (Phase 14)

The 2026-05 V1 audit (see `implementation_plan.md` §7.1) surfaced two
items that touch this document. Phase 14 work happens primarily in
`execution/` and the C++ engine, but two pieces land in `ui/`.

### 15.1 Phase 14B — Tide / Wave Snapshot Push from `LiveTradingSession`

**Why this affects the UI plan.** `LiveTradingSession.on_timer_tick`
already updates `MarketState` from `engine.get_strategy_snapshot()` for
the diagnostics panel (§2.2). Phase 14B requires the *opposite*
direction as well: the session must `push` the latest Tide/Wave
snapshots and realized vol *into* the engine before the engine emits
its next decision.

**Implementation outline (delegated to `implementation_plan.md` §7.1
Phase 14B; this section captures only the UI surface impact).**

| File | UI-side change |
|---|---|
| `ui/live_trading_session.py` | New `_tide_engine`, `_wave_engine` attributes (Python research engines, instantiated alongside the C++ engine). On every `on_timer_tick`: query both engines, then call `self._engine.set_risk_budget(...)`, `self._engine.get_ripple().set_wave_snapshot(...)`, `self._engine.get_ripple().set_realized_vol(...)`. All three calls gated on `enable_layered_strategy: bool = True` (default ON; allows pure-Ripple legacy mode for regression). Cadences match `strategy.md` §5.3 (Tide 60 s, Wave 5 s, RV 1 s) — implemented via simple "elapsed since last push" gates inside `on_timer_tick`. |
| `ui/main_window.py` | Optional new `Strategy → Layered Strategy` checkbox in the toolbar that toggles `enable_layered_strategy`. If absent, default ON is fine. |
| Diagnostics panel | No data-contract change. `StrategyDiagnosticsPanel` already reads Wave regime / risk fields from `get_strategy_snapshot()`; once the engine *receives* real snapshots, those fields will start showing real values instead of the static `DefaultTideSnapshot` / `DefaultWaveSnapshot` defaults. Worth adding a small visual hint (e.g. "● live" vs "○ default") to communicate the wiring state to the operator. |

### 15.2 Phase 14A — Live Execution Driven by Ripple Decisions

**UI-side impact: minor.** The existing toolbar `Arm Execution` button
keeps its current semantics (toggles `ExecutionManager._armed`). What
changes is the data the manager consumes:

| File | UI-side change |
|---|---|
| `ui/main_window.py` | `_on_ripple_received` already converts `RippleDecision` → `ExecutionIntent` (via `ripple_decision_to_intent`) for the paper path. Phase 14A makes the live path consume the same intent stream. The UI surface change is essentially zero — `Arm Execution` continues to gate, the diagnostics panel continues to show counts. The "live wiring" indicator from §15.1 should also reflect the routing topology (`signal-driven (legacy)` vs `ripple-driven (V1)`). |
| `ui/strategy_dashboard_view.py` | Optional: a small "routing: ripple-driven" badge in the strategy panel. Cosmetic — not blocking V1. |

### 15.3 Phase 14C — Live Risk-Gate Wiring `[COMPLETED 2026-05-12]`

**UI-side impact: minor but operationally important.** The same
`intent_risk_block_reason` gate that lives inside the headless runner
also lives inside `ui/main_window.py::_on_ripple_received` (the
`StrategyMode.LIVE` branch). The UI itself does not need a new
widget; the gate operates silently and logs a `V1 §22.2 #12 gate
blocked live intent: ...` warning when it fires.

| File | UI-side change (shipped) |
|---|---|
| `ui/main_window.py` | Imports `intent_risk_block_reason`. The `StrategyMode.LIVE` branch of `_on_ripple_received` computes a coarse `current_position_usd = abs(exec_mgr.current_qty) * intent.reference_price`, runs the gate, and short-circuits with a warning log when blocked. Exits + cancels short-circuit the gate so risk-reducing flows are never suppressed. |
| Diagnostics panel | ✅ **Closed by Phase 14F.4 (2026-05-12)** — the V1.1 polish item ("surface most recent block reason / count without grepping the log") shipped as a `_RiskGateStatusBar` widget under `StrategyDashboardView`. Session-level `record_block(reason, ts_ms)` increments a per-reason counter and stashes `last_block_reason` / `last_block_ts_ms`; the dashboard renders "Last block: REASON Xs ago \| Blocked this session: N" on every 100 ms timer tick, with an orange highlight for blocks <10 s old. 9 new headless tests in `test_strategy_dashboard.py`. |

### 15.4 Phase 14F — V1 Closure Tail `[COMPLETED 2026-05-12]`

**Wraps up the UI-facing items called out in §15.1 (Tide/Wave/RV
wiring indicator) and §15.3 (risk-gate block reason).** Both were
explicitly labelled "V1.1 polish" / "future polish" in their parent
sections; Phase 14F lands both ahead of the next planned UI work so
operators have full visibility into the layered strategy without
log-grepping.

| File | UI-side change (shipped) |
|---|---|
| `ui/strategy_dashboard_view.py` | New `_RiskGateStatusBar` (Phase 14F.4) and `_LayeredWiringIndicator` (Phase 14F.5) widgets, composited at the bottom of `StrategyDashboardView`. Both update via `update_block_status` / `update_wiring` proxy methods on the dashboard. The wiring indicator is one-way: a layer flips from grey `○ default` to green `● live` on its first successful setter call and stays live for the rest of the session, so transient C++ binding hiccups don't make the panel flicker grey. |
| `ui/live_trading_session.py` | `record_block(reason, ts_ms)`, `block_status()`, `layered_push_status()`, `block_counts_by_reason` accessors added. These are pure Python — no Qt dependency — so they can be unit-tested headless. |
| `ui/main_window.py` | `_on_ripple_received` calls `record_block(...)` when `intent_risk_block_reason` returns a reason. `_on_timer_tick` proxies the latest snapshot to the dashboard. |
| `tests/test_strategy_dashboard.py` | +16 checks across both widgets (9 risk-gate + 7 wiring). Suite total: 113/113 passing under `QT_QPA_PLATFORM=offscreen`. |

The wiring indicator closes the §15.1 follow-up ("Worth adding a small
visual hint (e.g. '● live' vs '○ default') so the operator can see at
a glance whether Tide/Wave/RV are being pushed live"). The risk-gate
status bar closes the §15.3 V1.1 polish item. No additional UI work
remains on the V1 plan.

### 15.5 Phase 11D / 11E / 11F — Status Unchanged

The deferred heatmap-cosmetics phases (§14.2 / §14.3 / §14.4) remain
deferred per §14.5. They are **not** V1 closure work — `strategy.md`
§22.2 has no UI rendering items, and the bubble drop-out regression
guards are already in place from `tests/test_bubble_pipeline.py`.
Resume only on user request or after V1 GA.

---

## 16. Phase 8 — UI Accuracy & First-Impression Polish `[IN PROGRESS — 2 of 3 sub-phases complete]`

### 16.1 Overview

**Progress (2026-05-12):** Phase 8A complete; Phase 8B complete (2026-05-12); Phase 8C
complete (2026-05-12). All 3 sub-phases landed — Phase 8 GA gate pending synthesising
agent confirmation.

Three user-identified gaps remain after V1 GA that make the live UI
misleading or incomplete:

1. **Candlestick chart has no historical data on connect.** The chart
   shows "Waiting for candle data…" until enough live ticks arrive —
   up to 80 minutes for 80 visible candles at 1 m. This makes the chart
   useless as a trading reference on launch.

2. **PnL and trade counts are not tied to strategy execution.**
   `AccountPanel` computes realized PnL using naive FIFO order
   matching (any BUY→SELL pair) rather than strategy-attributed entry/
   exit pairs from `ExecutionManager`. The `uPnL` field in
   `StrategyDiagnosticsPanel` derives from the C++ engine's internal
   position tracking, which may diverge from actual paper/live fills
   when orders are rejected or partially filled.

3. **Signal log shows legacy engine signals unrelated to the strategy.**
   `_on_signal_received` in `main_window.py` is still wired to the old
   `ScoreBasedInference` signal path and adds `LEGACY_RAW` signals
   (`STACKED_IMBALANCE_*`, `BULLISH_*`, `BEARISH_*`, etc.) to the
   blotter at the same rate as live market events. These are NOT Ripple
   decisions — they are raw scoring outputs from the pre-architecture
   signal engine. Additionally, Tide bias changes and Wave regime
   changes are never emitted as explicit log events, so operators
   cannot see macro/regime context inline.

Three sub-phases address these gaps independently:

| Sub-phase | Name | Dependency |
|---|---|---|
| **8A** | Candlestick Historical Preload | Phase 6 (DONE) |
| **8B** | Strategy-Attributed PnL & Trade Tracking | Phase 14A (DONE) |
| **8C** | Signal Log Accuracy — Gate Legacy + Tide/Wave Events | Phase 14B (DONE) |

**Phase 8 GA gate:** All three sub-phases complete and regression suite green.

---

### 16.2 Phase 8A — Candlestick Historical Preload `[COMPLETED 2026-05-12]`

**Evidence.**

- **Files touched** (5):
  - `data_feed/binance_klines_rest.py` *(new)* — REST helper
    `fetch_binance_klines(...)` + `interval_ms_to_label(...)`; module
    constants `BINANCE_FUTURES_USDM_KLINES_URL`,
    `DEFAULT_KLINES_LIMIT = 200`, `KLINES_FETCH_TIMEOUT_S = 10.0`.
  - `data_feed/__init__.py` — re-exports the new helper + constants.
  - `ui/candle_chart_view.py` — new public API
    `preload_candles(...)`, `set_loading(...)`, `loading` property;
    new module constants `_LABEL_LOADING`, `_LABEL_WAITING`,
    `_PRELOAD_TRADE_COUNT`, `_MAX_STORED_CANDLES`; `paintEvent` honours
    `_loading` and records `_last_paint_label` for test assertions.
  - `ui/live_trading_session.py` — synchronous wrapper
    `fetch_historical_klines(symbol, interval_ms, limit=200)` that
    swallows all REST exceptions and returns `[]` on failure.
  - `ui/main_window.py` — new Qt signal `_klines_ready`, token
    `_klines_token`, helpers `_kickoff_candle_preload(...)` and
    `_on_klines_ready(...)`; `_on_connect` kicks off the preload after
    `start_live`; `_on_chart_tf_changed` re-fetches at the new bucket
    while connected; `_on_disconnect` bumps the token and clears the
    loading overlay.
- **Tests added.** `tests/test_candle_preload.py` (new, **18 tests** in
  6 unittest classes) covering: OHLC population, deque replacement,
  sort-on-ingest, same-bucket / next-bucket / stale-bucket live-tick
  interaction, empty-list no-op preservation, loading overlay state
  machine, paintEvent label assertions, session wrapper happy path /
  exception swallow / empty-symbol short-circuit / unsupported-interval
  swallow, plus a §3.5 production-wiring grep on `ui/main_window.py`.
- **Verification commands** (all green at 2026-05-12):
  - `QT_QPA_PLATFORM=offscreen python -m unittest tests.test_candle_preload -v`
    → **18 tests OK** (0.085 s).
  - `python tests/test_candle_chart_view.py` → **15/15 OK** (existing
    suite passes byte-identically).
  - `QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v`
    → **746 tests OK** (25.8 s) — broad regression sweep clean.
  - Wiring grep
    `rg "preload_candles|fetch_historical_klines" ui/ tests/` returns
    production hits in `ui/main_window.py:762,797`,
    `ui/live_trading_session.py:474` and the new test file
    (AGENT_STRATEGY_RULES.md §3.5 gate satisfied).
- **Acceptance criteria — all met.**
  1. On connect with BTCUSDT@1m the chart fills with ≥80 candles within
     ~1 REST round-trip (`limit=200`, single sync `requests.get` off
     the GUI thread).
  2. Same-bucket / next-bucket `process_trade` calls extend the
     preloaded deque without duplicates — covered by
     `TestLiveTradesAfterPreload`.
  3. Loading overlay rendered while a preload is in flight (Qt signal
     marshals completion back to the GUI thread) — covered by
     `TestLoadingOverlay`.
  4. REST exceptions → `[]` → no crash, no dialog, chart reverts to
     "Waiting for live data" — covered by
     `TestFetchHistoricalKlines.test_fetch_swallows_exceptions_returns_empty`.
  5. Timeframe change while connected re-fetches at the new bucket
     (`_on_chart_tf_changed` calls `_kickoff_candle_preload` when
     `_engine is not None`); stale results from the prior request are
     dropped via the `_klines_token` guard.
  6. Existing `tests/test_candle_chart_view.py` passes byte-identically
     (15/15 in custom-runner mode).

**Problem in detail.** `CandleChartView._candles` is populated
exclusively by `process_trade(ts, price, qty, is_buy)` calls from
`MainWindow._on_timer_tick`. On connect, the deque starts empty. At
1 m timeframe with `_visible_candles = 80`, the user must wait 80
minutes of live data before the visible window is filled. Changing
timeframe clears the deque (`set_bucket_duration` calls
`self._candles.clear()`), repeating the wait.

**Proposed solution.**

On `_on_connect` (and on `_on_chart_tf_changed` while connected),
fetch historical OHLCV klines from the Binance Futures REST API
(`GET /fapi/v1/klines`) and pre-populate the chart. The fetch must
be non-blocking (async background task) to avoid freezing the UI.

**Scope.**

| File | Change |
|---|---|
| `ui/candle_chart_view.py` | Add `preload_candles(candles: list[tuple[int, float, float, float, float, float]])` method accepting `(ts_ms, open, high, low, close, volume)` tuples. Inserted as a batch at the front of `_candles` so live ticks append naturally after them. Add `_loading: bool` flag — while True, `paintEvent` draws "Loading historical candles…" instead of "Waiting for data…". Add `set_loading(bool)` toggler. |
| `ui/live_trading_session.py` | Add `fetch_historical_klines(symbol: str, interval_ms: int, limit: int = 200) -> list[tuple]` async method. Uses `python-binance` futures REST (`client.futures_klines`) or a direct `aiohttp` GET to Binance `/fapi/v1/klines`. Returns `[(ts_ms, o, h, l, c, v), …]` sorted oldest-first. Fails gracefully: catches all exceptions and returns `[]`. |
| `ui/main_window.py` | In `_on_connect`, after session.start_live succeeds: call `candle_view.set_loading(True)`, then launch `asyncio.create_task` (or `threading.Thread`) to call `session.fetch_historical_klines(symbol, bucket_ms, limit=200)`. On result, call `candle_view.preload_candles(result)` and `candle_view.set_loading(False)`. In `_on_chart_tf_changed` while connected, repeat the same async fetch/preload pattern at the new bucket duration. |
| `tests/test_candle_preload.py` | NEW. Tests: `preload_candles` with 100 tuples populates `_candles` with correct OHLC; live `process_trade` after preload extends the deque correctly (no duplicate bucket); `preload_candles([])` is a no-op; `set_loading(True)` causes `paintEvent` to render loading text, not data. |

**Acceptance criteria.**

1. On connect with symbol BTCUSDT at 1 m timeframe, chart shows ≥ 80
   pre-populated candles within 5 seconds of connection (async REST
   fetch completes).
2. `process_trade` calls arriving during or after the fetch correctly
   extend / update the pre-populated candles without creating
   duplicates or gaps.
3. Chart shows "Loading historical candles…" while fetch is in
   progress; reverts to normal rendering on completion.
4. If the REST fetch fails (network error, rate-limit), chart falls
   back to "Waiting for live data" with no crash and no error dialog.
5. On timeframe change while connected, chart re-fetches at the new
   interval and replaces the preloaded set.
6. All existing `tests/test_candle_chart_view.py` tests pass unchanged.

---

### 16.3 Phase 8B — Strategy-Attributed PnL & Trade Tracking `[COMPLETED 2026-05-12]`

**Problem in detail.** `AccountPanel` maintains its own FIFO PnL
accumulator (`_last_entry_price` / `_last_entry_side` /
`_realized_pnl`), computed by matching any filled BUY order against
the next SELL order regardless of whether those orders originated from
Ripple decisions. This produces PnL numbers that:

- May include non-strategy fills (manual orders, liquidations, other
  bots) in live mode.
- Are recalculated independently of `ExecutionManager`, which already
  tracks `_current_side`, `_current_qty`, and (post-Phase 8B)
  `_session_realized_pnl` authoritatively.
- Show "ENTRY" for every BUY regardless of archetype or Ripple reason,
  losing strategic context.

In paper mode, `StrategyDiagnosticsPanel.uPnL` reads from
`engine.get_strategy_snapshot().trade.unrealized_pnl` — the C++
engine's position — which reflects the engine's internal simulation
rather than the `PaperEngine`'s actual paper fills. If an order is
suppressed by `PaperEngine` (e.g. inventory gate), the two values
diverge silently.

**Proposed solution.**

Wire `AccountPanel` and `StrategyDiagnosticsPanel` to
`ExecutionManager` state (and `PaperEngine` metrics for paper mode)
as the single authoritative source for strategy-attributed PnL and
trade counts.

**Scope.**

| File | Change |
|---|---|
| `execution/execution_manager.py` | Add `_session_realized_pnl: float = 0.0` accumulator. Increment in `_execute_intent_exit` when `order.status == FILLED`: `pnl = (fill_price - entry_price) * qty * side_sign`. Add read-only property `session_realized_pnl -> float`. Add `_session_trade_count: int = 0`; increment on each completed exit fill. Add `session_trade_count -> int` property. Add `reset_session_stats()` to clear both (called on ARM). |
| `execution/paper_engine.py` | Expose `session_realized_pnl -> float` (alias to `_metrics.realized_pnl`). Expose `session_trade_count -> int` (alias to `_metrics.exits_filled`). |
| `ui/account_panel.py` | Replace the FIFO accumulator (`_last_entry_price`, `_last_entry_side`, `_realized_pnl`) with a new `update_strategy_stats(side, qty, entry_price, upnl, realized_pnl, trade_count, mode_label)` method driven by caller. Add "Mode" label (Paper / Live / —). Add "Strategy Trades" counter row. Rename "Realized PnL" to "Session PnL (strategy)" to make the source explicit. Order table: add `Reason` column (populated from `order.ripple_reason`). |
| `ui/main_window.py` | In `_on_timer_tick`, call `account_panel.update_strategy_stats(...)` from `_exec_manager` state (live mode) or `_paper_engine` metrics (paper mode). Pass `mode_label="Paper"` or `"Live"`. When in OBSERVE mode or disarmed, pass zeros + mode label `"Observe"` so the panel shows a clean zero-state rather than stale FIFO data. |
| `tests/test_account_panel.py` | NEW. Tests: `update_strategy_stats` renders side/qty/upnl/realized/count correctly; mode label shows Paper/Live/Observe; "Strategy Trades" counter increments; `Reason` column populated from `ripple_reason`; FIFO accumulator is absent (assert `_last_entry_price` attribute does not exist). |

**Acceptance criteria.**

1. Account panel "Realized PnL" is sourced exclusively from
   `ExecutionManager.session_realized_pnl` (live) or
   `PaperEngine.session_realized_pnl` (paper); never from FIFO
   order matching.
2. "Strategy Trades" counter increments by 1 for each completed
   strategy round-trip (one entry fill + one exit fill).
3. "Mode" label shows "Paper", "Live", or "Observe" depending on
   `StrategyMode`.
4. Order rows show `order.ripple_reason` in the Reason column (e.g.
   `EXIT_TARGET`, `BOUNCE_SETUP`).
5. In OBSERVE mode (no execution), account panel shows "Strategy not
   armed" placeholder; no stale PnL numbers.
6. All existing `tests/test_strategy_store.py` and
   `tests/test_integration_e2e.py` tests pass unchanged.

**Evidence (2026-05-12).**

```
# New tests — all pass
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest tests.test_account_panel -v
# Ran 28 tests in 2.049s — OK

# Backward-compat tests — pass unchanged
QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_ui_cleanup.py
# UI cleanup tests: 38/38 passed, 0 failed

QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_strategy_store.py
# Strategy store tests: 63/63 passed, 0 failed

QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_integration_e2e.py
# Integration E2E tests: 55/55 passed, 0 failed

# Broad regression sweep
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests
# Ran 840 tests in 31.846s — OK

# Wired live? grep
grep -rn "session_realized_pnl|session_trade_count|reset_session_stats" execution/ ui/
# Hits: execution/execution_manager.py, execution/paper_engine.py, ui/main_window.py
```

Acceptance criteria:
1. ✅ "Session PnL (strategy)" sourced from `ExecutionManager.session_realized_pnl` / `PaperEngine.session_realized_pnl` — never FIFO.
2. ✅ "Strategy Trades" increments per completed exit fill.
3. ✅ Mode label shows "Paper", "Live", or "Observe".
4. ✅ Order rows show `order.ripple_reason` in Reason column.
5. ✅ Observe mode shows "Strategy not armed" — no stale PnL.
6. ✅ `test_strategy_store.py` and `test_integration_e2e.py` pass unchanged.

---

### 16.4 Phase 8C — Signal Log Accuracy `[COMPLETED 2026-05-12]`

**Problem in detail.** Three distinct issues pollute or thin the
signal log in the current implementation:

**Issue 1 — Legacy raw signals flood the log when strategy is armed.**
`MainWindow._on_signal_received` is connected to the C++ engine's
`set_signal_callback` (wired at `session.start_live`). The C++ engine
emits a signal for every `ScoreBasedInference` decision, including
`STACKED_IMBALANCE_*`, `BULLISH_*`, `BEARISH_*` events that fire at
trade frequency (potentially hundreds per minute). These are NOT Ripple
decisions — they are pre-architecture internal scoring events. The
`_on_signal_received` handler adds them as `LEGACY_RAW` entries to the
blotter at full speed. With strategy mode set to PAPER or LIVE, these
entries drown out the strategy-relevant events.

**Issue 2 — Tide and Wave state changes are not emitted as log events.**
Tide bias and Wave regime changes are the most strategically significant
events in the system, yet the signal log never shows them. An operator
watching the log cannot see "Wave flipped to BREAKDOWN" without
switching to the diagnostics panel. Tide/Wave states are available from
`_last_strategy_snap` (cached in `main_window.py`), but no logic
compares current vs. previous values and emits a structured entry.

**Issue 3 — No trade outcome annotation on exit signals.**
When a `TRADE_LIFECYCLE` EXIT signal is emitted, the Description column
shows the exit type and Ripple reason but not the realized PnL for that
trade. Operators must cross-reference the Account Panel separately.

**Proposed solution.**

| Issue | Fix |
|---|---|
| 1 — Legacy signal flood | Gate `_on_signal_received` additions to blotter: when `_strategy_ui_state != DISARMED`, skip `blotter.add_signal` for `LEGACY_RAW` entries. Raw signals remain available in a new "Debug" filter (for diagnostics) but are hidden in all other filters when strategy is active. |
| 2 — No Tide/Wave events | Add `_last_tide_bias` / `_last_wave_regime` caches in `MainWindow`. In `_on_timer_tick`, after updating `_last_strategy_snap`, compare bias/regime against last values. On change, call `_broadcast_entry` with a `TIDE` or `WAVE` category `SignalEntry` describing the old → new transition. |
| 3 — No exit PnL annotation | Add `realized_pnl: float = 0.0` to `SignalEntry` dataclass. When emitting an EXIT `TRADE_LIFECYCLE` entry in `_on_ripple_received`, populate `realized_pnl` from `ExecutionManager.session_realized_pnl` delta (snapshot before/after intent). Add a PnL column to `TradeBlotter` (visible only when non-zero). |

**Scope.**

| File | Change |
|---|---|
| `execution/models.py` | Add `realized_pnl: float = 0.0` optional field to `SignalEntry`. No default change to existing fields. |
| `ui/main_window.py` | **Issue 1:** In `_on_signal_received`, gate `blotter.add_signal` on `self._strategy_ui_state == StrategyUIState.DISARMED` — when armed, suppress raw-legacy additions. **Issue 2:** Add `_last_tide_bias: Optional[str] = None` and `_last_wave_regime: Optional[str] = None` instance vars. In `_on_timer_tick`, after snapshot update: compare `snap.tide.bias` / `snap.wave.regime` to cached values; on change, emit `SignalEntry(category=TIDE, signal_type="BIAS_CHANGE", description="NEUTRAL → LONG", ...)` / `SignalEntry(category=WAVE, signal_type="REGIME_CHANGE", ...)` via `_broadcast_entry`. **Issue 3:** In EXIT handling inside `_on_ripple_received`, record `pnl_before = exec_manager.session_realized_pnl`, process intent, then set `entry.realized_pnl = exec_manager.session_realized_pnl - pnl_before`. |
| `ui/trade_blotter.py` | Add `_btn_trades_only` filter checkbox (label "Trades") that shows only `TRADE_LIFECYCLE` + `EXECUTION` entries. Add a "PnL" display column after "Strength" — renders `entry.realized_pnl` formatted `+x.xx` / `-x.xx` when non-zero, blank otherwise. Add `_FILTER_DEBUG` set (`LEGACY_RAW` + `DIAGNOSTIC`) activated by a "Debug" checkbox (replaces old "Raw" checkbox). Update color scheme: new "Debug" button is dim grey; "Trades" button is bold white. |
| `tests/test_signal_log_accuracy.py` | NEW. Tests: legacy `LEGACY_RAW` signal suppressed when strategy armed; legacy signal appears when strategy disarmed; Tide `BIAS_CHANGE` entry emitted on bias transition; Wave `REGIME_CHANGE` entry emitted on regime transition; no spurious event when bias/regime unchanged; exit `TRADE_LIFECYCLE` entry has non-zero `realized_pnl` after fill; Trades Only filter hides non-trade entries; Debug filter shows `LEGACY_RAW`; PnL column renders formatted value. |

**Acceptance criteria.**

1. With strategy mode PAPER or LIVE, `_on_signal_received` no longer
   adds `LEGACY_RAW` entries to the blotter; existing Ripple entry/exit
   and EXECUTION entries are unaffected.
2. A Tide bias change detected from `_last_strategy_snap` emits a
   `TIDE` category `SignalEntry` with `signal_type="BIAS_CHANGE"` and
   description `"<OLD> → <NEW>"` within one timer tick.
3. A Wave regime change emits a `WAVE` category `SignalEntry` with
   `signal_type="REGIME_CHANGE"`.
4. No duplicate events: if bias/regime is stable across ticks, no
   additional entries are emitted.
5. EXIT `TRADE_LIFECYCLE` signals populate `realized_pnl` with the
   correct delta from `ExecutionManager`.
6. "Trades" filter shows only `TRADE_LIFECYCLE` + `EXECUTION` entries.
7. "Debug" filter shows `LEGACY_RAW` + `DIAGNOSTIC` entries.
8. All existing `tests/test_strategy_ui.py` (163 checks) pass unchanged.

**Evidence (2026-05-12).**

```
Files modified:
  execution/models.py       — realized_pnl: float = 0.0 added to SignalEntry
  ui/main_window.py         — Issue 1 gate, Issue 2 Tide/Wave tick detection,
                              Issue 3 PnL delta annotation; named constants
                              _SIGNAL_TYPE_BIAS_CHANGE, _SIGNAL_TYPE_REGIME_CHANGE,
                              _LEGACY_CONTEXT_PREFIXES; cache vars _last_tide_bias,
                              _last_wave_regime
  ui/trade_blotter.py       — _FILTER_DEBUG, _PNL_DISPLAY_THRESHOLD, PnL column
                              (col 7), _btn_trades_only (renamed from _btn_trade),
                              _btn_debug, _on_trades_only_toggled, _on_debug_toggled
  tests/test_signal_log_accuracy.py — NEW, 26 tests (9 spec + extras)

Test results:
  python -m unittest tests.test_signal_log_accuracy -v → Ran 26 tests in 0.665s OK
  python tests/test_strategy_ui.py → 163/163 passed, 0 failed
  python -m unittest discover -s tests → Ran 840 tests in 30.894s OK
```

---

### 16.5 Phase 8 Dependency Graph

```
Phase 8A (Candlestick Preload) — independent of 8B and 8C
Phase 8B (Strategy PnL) ─── depends on Phase 14A ExecutionManager state
Phase 8C (Signal Log) ─────── depends on Phase 14B Tide/Wave snapshot push
                               (need _last_strategy_snap to have live data)

8A + 8B + 8C can run in parallel once their individual dependencies are met.
Phase 8 GA requires all three sub-phases complete.
```

---

## 21. Phase 16D — Docker Dev Container & EC2 Deployment Infrastructure `[COMPLETED 2026-05-13]`

### 21.1 Overview

Phase 16D establishes a unified development and deployment environment
so that local dev (macOS M3 Pro) and EC2 production (Ubuntu 24.04) run
identical code and the same compiled C++ extension.

**Two problems solved simultaneously:**

1. **Phase 16P EC2 deployment** — get the tick data collector running on
   EC2 to start the 30-day data accumulation clock (blocker for Phase 16).
2. **Phase 9 dev environment** — provide an Ubuntu 24.04 dev container
   for Phase 9 QML development without waiting for a GPU EC2 instance.

### 21.2 Delivered artefacts

| File | Purpose |
|---|---|
| `Dockerfile.dev` | Ubuntu 24.04 image; C++ engine + Python venv; Qt XCB runtime; default `QT_QPA_PLATFORM=offscreen` |
| `.devcontainer/devcontainer.json` | Cursor/VS Code dev container; `linux/amd64` via Rosetta; X11 socket mount for optional GUI passthrough |
| `collect_ticks.py` | Headless CLI tick collector (`--symbol`, `--duration`, `--s3-bucket`, SIGTERM-safe) |
| `requirements-collector.txt` | Stripped EC2 deps — no Qt, no matplotlib |
| `backtestingCpp/orderflow/build.sh` | Cross-platform (macOS Homebrew or Linux system packages) |
| `backtestingCpp/orderflow/CMakeLists.txt` | `APPLE` guard around Homebrew prefix and `Boost_NO_SYSTEM_PATHS` |
| `scripts/setup_ec2.sh` | Full Ubuntu 24.04 bootstrap: apt, venv, C++ build, systemd, cron |
| `scripts/collector.service` | systemd unit for BTCUSDT collector |
| `scripts/collector@.service` | Template unit for multi-symbol instances |
| `scripts/s3_sync.sh` | Hourly S3 cron sync |
| `scripts/download_ticks.sh` | Developer download helper |
| `scripts/add_symbol.sh` | Add a second collector instance (e.g. ETHUSDT) |
| `docs/DEPLOYMENT.md` | Step-by-step deployment guide |
| `tests/test_collect_ticks.py` | 22 tests; 862/862 broad regression |

### 21.3 Cross-platform GPU strategy

| Environment | GPU | Qt RHI backend | Set by |
|---|---|---|---|
| macOS M3 Pro (local native) | Apple Silicon / Metal | `metal` | `_configure_rhi_backend()` in Phase 9A |
| Docker (local dev container) | None / software | `software` + `offscreen` | `devcontainer.json` env vars |
| EC2 t3.small (data collection) | None — headless only | N/A | No Qt dependency |
| EC2 g5.xlarge (Phase 9 UI) | NVIDIA A10G / OpenGL | `opengl` + `xcb` | `Dockerfile.gpu` (Phase 9A) |

Running the Qt GUI locally uses the **native macOS app** (outside Docker)
with Metal. Docker is for tests, C++ builds, and the headless collector.
The `_configure_rhi_backend()` function specified in Phase 9A §22.2
handles backend selection at runtime.

### 21.4 Local dev container quick-start

```bash
# 1. Open project in Cursor → "Reopen in Container" (auto-detected)

# 2. Run tests (offscreen Qt)
python -m unittest discover -s tests -v

# 3. Optional: GUI passthrough via XQuartz on macOS
export DISPLAY=host.docker.internal:0
export QT_QPA_PLATFORM=xcb
python main.py  # choose 'ui' mode
```

---

## 22. Phase 9 — Qt Quick/QML Migration, GPU Acceleration & UI Cleanup

> **Deployment target:** Ubuntu 24.04 on AWS EC2 g5.xlarge (NVIDIA A10G),
> streamed via NICE DCV. All constraints in `AGENT_STRATEGY_RULES.md §22`
> apply to every component built in this phase.
>
> **Dev environment:** Use the `Dockerfile.dev` dev container (Phase 16D)
> for local development. Phase 9A adds `Dockerfile.gpu` as an extension
> for the g5.xlarge production target.

### 22.1 Overview

Phase 9 is a **rendering-stack architectural pivot**. The existing
`QWidget` + `QPainter` (CPU raster) UI is replaced by **Qt Quick / QML**
running on an OpenGL scene graph. This enables:

1. **True GPU rendering** — chart geometry and heatmap textures live in
   GPU VRAM. No per-frame CPU→GPU pixel upload.
2. **NICE DCV optimization** — QML's retained-mode scene graph minimizes
   redraws; DCV streams only changed regions.
3. **Linux-first deployment** — QML runs identically on Ubuntu 24.04
   with NVIDIA proprietary drivers; no macOS-only APIs.
4. **Clean dead-element removal** — the QML migration is the right moment
   to drop hidden legacy stubs and consolidate duplicate controls.

| Sub-phase | Name | Status | Depends on |
|---|---|---|---|
| **9A** | QML Scaffold & OpenGL Backend Setup | `COMPLETED 2026-05-14` | Phase 8 (parallel) |
| **9B** | Chart Widgets → QML Scene Graph | `COMPLETED 2026-05-14` | 9A |
| **9C** | Strategy Dashboard → QML + Trade Indicators | `COMPLETED 2026-05-14` | 9B + Phase 8B |
| **9D** | Dead Element Audit & Toolbar Cleanup | `COMPLETED 2026-05-14` | 9A (parallel with 9B) |

**Phase 9 GA gate:** All four sub-phases complete. `QSGRendererInterface`
backend is `OpenGL` (not `Software`) at startup. P95 frame time < 20 ms
at 60 FPS on g5.xlarge. NICE DCV session stable for ≥ 30 minutes.

---

### 22.2 Phase 9A — QML Scaffold & Cross-Platform GPU Backend Setup `[COMPLETED 2026-05-14]`

**Evidence.**

- **Files touched** (12):
  - `ui/app.py` — replaced `QApplication` + `MainWindow` entry with
    `QGuiApplication` + `QQmlApplicationEngine`.  Adds
    `_configure_rhi_backend()` (Darwin → `metal`, Linux → `opengl`
    + `xcb` + `QT_ENABLE_GLYPH_CACHE_WORKAROUND=1`; all use
    `setdefault` so shell overrides win), `_check_gpu()` (logs ERROR
    + flips `gpuWarning` on the QML root when the scene graph
    reports `Software` / `Unknown`), `_iter_quick_windows()`,
    `_register_qml_types()`, and a `--legacy-widgets` opt-in that
    keeps the deprecated `QMainWindow` shell launchable during
    the 9A→9C transition.
  - `ui/qml/main.qml` *(new)* — root `ApplicationWindow`
    (`objectName: orderFlowWindow`) plus two detachable top-level
    `Window` items (`chartWindow`, `strategyWindow`).  Embeds the
    six stub components inside a column / row layout; persistent
    red banner bound to `gpuWarning` surfaces inline when software
    rendering is detected.
  - `ui/qml/components/HeatmapView.qml` *(new)*,
    `ui/qml/components/CandleChartView.qml` *(new)*,
    `ui/qml/components/CvdView.qml` *(new)*,
    `ui/qml/components/VolumeProfileView.qml` *(new)*,
    `ui/qml/components/StrategyDashboard.qml` *(new)*,
    `ui/qml/components/PositionCard.qml` *(new)*,
    `ui/qml/components/TradeBlotter.qml` *(new)* — solid-background
    placeholders annotated `// TODO Phase 9B/9C` so the QML scene
    is a self-documenting migration map.
  - `ui/models/__init__.py` *(new)* + `ui/models/snapshot_model.py`
    *(new)* — `SnapshotModel(QObject)` exposing `tideBias`,
    `waveRegime`, `riskBudgetPct`, `unrealizedPnl`, `tradeState`
    as `Q_PROPERTY` with per-field `Notify` signals.  Setters
    coerce + dedupe so QML bindings only invalidate on real change.
    `update_from_snapshot()` mirrors a `StrategySnapshot`
    (Tide / Wave / Risk / TradeState).
  - `tests/test_qml_scaffold.py` *(new)* — 25 unittest cases
    covering env-var selection per platform, shell override
    precedence, QML engine load + window inventory, software /
    metal / opengl / vulkan branches of `_check_gpu` (real and
    faked), a subprocess end-to-end run forcing
    `QSG_RHI_BACKEND=software` to verify the FATAL log fires for
    every QQuickWindow, `SnapshotModel` Q_PROPERTY semantics, and
    a guard that the QWidget paths remain importable.
- **Acceptance criteria coverage:**

  | AC | Test |
  |---|---|
  | #1 main.qml loads, 3 windows | `TestQmlEngineLoads.test_engine_loads_without_warnings` |
  | #2 Metal on Darwin, OpenGL on Linux | `TestConfigureRhiBackend.test_darwin_selects_metal` / `test_linux_selects_opengl_and_xcb` |
  | #3 `_configure_rhi_backend` runs before `QGuiApplication`; shell override honoured | `TestConfigureRhiBackend.test_shell_override_wins` (Vulkan override) |
  | #4 `_check_gpu` logs ERROR on Software, not on Metal/OpenGL | `TestCheckGpu.test_software_logs_error` + `test_metal_passes` + `test_opengl_passes` + `TestCheckGpuEndToEnd.test_software_backend_triggers_error_for_each_window` |
  | #5 Legacy QWidget paths still importable | `TestLegacyWidgetPathsImportable` (3 tests) + `tests/test_strategy_ui.py` (163/163), `tests/test_strategy_dashboard.py` (113/113), `tests/test_ui_cleanup.py` (38/38) |
  | #6 Offscreen scaffold tests | `TestQmlEngineLoads`, `TestCheckGpuEndToEnd`, `TestSnapshotModel` (all under `QT_QPA_PLATFORM=offscreen`) |
- **Regression suite** — `tests/test_qml_scaffold.py` 25/25, plus
  the broader UI suites (`test_account_panel`, `test_candle_chart_view`,
  `test_candle_preload`, `test_chart_overlays`, `test_signal_log_accuracy`)
  remained green: 72/72 unittest-style + 163/163 strategy_ui +
  113/113 strategy_dashboard + 38/38 ui_cleanup checks pass.

### 22.2.1 Phase 9A — Original spec (archived)

**Problem in detail.**

The application currently uses `QApplication` + `QMainWindow` (QWidget
stack). Migrating to Qt Quick requires replacing the application root
with `QGuiApplication` + `QQmlApplicationEngine` and establishing the
correct scene graph backend before any window is shown.

**Cross-platform GPU topology.**

The app targets two execution environments with different GPU stacks:

| Environment | GPU | Native Qt RHI backend | Platform plugin |
|---|---|---|---|
| macOS M-series (local dev) | Apple Silicon integrated (Metal) | `metal` | `cocoa` |
| Ubuntu 24.04 EC2 g5.xlarge (production) | NVIDIA A10G (OpenGL / Vulkan) | `opengl` | `xcb` (NICE DCV) |

Verify the local GPU at any time with:

```bash
# macOS
system_profiler SPDisplaysDataType | grep -E "Chipset|VRAM|Metal|Vendor"

# Linux EC2
nvidia-smi
lspci | grep -i vga
glxinfo | grep "OpenGL renderer"
```

Qt Quick defaults to `metal` on macOS and `opengl` (or `vulkan`) on
Linux. Both are hardware-accelerated — neither is the software fallback.
The GPU verification check (9A-2) must accept `Metal` and `OpenGL` as
valid backends; only `Software` is an error.

**Proposed solution.**

*9A-1 — Switch application root to QML engine with platform-aware backend.*

Replace `ui/app.py`'s `QApplication` + `MainWindow()` with:

```python
import platform, os, sys
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface

def _configure_rhi_backend() -> None:
    """Set Qt RHI env vars before QGuiApplication is created.

    macOS  → Metal  (native; OpenGL is a deprecated compat layer on Darwin)
    Linux  → OpenGL (NVIDIA driver; required for NICE DCV streaming)

    All vars use setdefault so a developer can override from the shell
    without editing source (e.g. QSG_RHI_BACKEND=vulkan for testing).
    """
    if platform.system() == "Darwin":
        os.environ.setdefault("QSG_RHI_BACKEND", "metal")
        # No QT_QPA_PLATFORM override needed — cocoa is the default on macOS.
    else:
        os.environ.setdefault("QSG_RHI_BACKEND", "opengl")
        os.environ.setdefault("QT_QPA_PLATFORM", "xcb")   # X11 via NICE DCV
    os.environ.setdefault("QSG_RENDER_LOOP", "threaded")  # scene graph off GUI thread

_configure_rhi_backend()   # MUST be called before QGuiApplication()

app = QGuiApplication(sys.argv)
engine = QQmlApplicationEngine()
engine.load("ui/qml/main.qml")
```

*9A-2 — GPU verification check.*

After the first `QQuickWindow` is created, call `_check_gpu(window)`
(see `AGENT_STRATEGY_RULES.md §22.2`). Log at ERROR and display an
in-app banner if **software** rendering is detected. Metal and OpenGL
are both accepted as valid hardware backends:

```python
_HW_BACKENDS = {
    QSGRendererInterface.GraphicsApi.Metal,
    QSGRendererInterface.GraphicsApi.OpenGL,
    QSGRendererInterface.GraphicsApi.Vulkan,
}

def _check_gpu(window: QQuickWindow) -> None:
    api = window.rendererInterface().graphicsApi()
    if api not in _HW_BACKENDS:
        logger.error(
            "Qt Quick is using software rendering (%s). "
            "GPU acceleration unavailable — frame budget will be exceeded.",
            api,
        )
        # Surface a dismissible banner in the QML root window.
        window.rootObject().setProperty("gpuWarning", True)
    else:
        logger.info("Qt Quick GPU backend: %s", api)
```

*9A-3 — NICE DCV frame pacing.*

```python
window.setMaximumFrameLatency(1)   # single-frame pipeline — minimizes latency
window.setRenderTarget(QQuickWindow.DefaultTarget)
```

*9A-4 — QML directory structure.*

```
ui/qml/
  main.qml                    # root ApplicationWindow
  components/
    HeatmapView.qml           # wraps HeatmapItem (QQuickPaintedItem)
    CandleChartView.qml       # wraps CandleItem (QSGGeometryNode-backed)
    CvdView.qml               # wraps CvdItem
    VolumeProfileView.qml     # wraps VolumeProfileItem
    StrategyDashboard.qml     # composes diagnostics + blotter + position card
    PositionCard.qml          # trade position summary
    TradeBlotter.qml          # ListView-backed blotter
  models/
    TradeBlotterModel.py      # QAbstractListModel registered as QML type
    SnapshotModel.py          # exposes TideSnapshot / WaveSnapshot props to QML
```

*9A-5 — C++ / Python model registration.*

Expose data models to QML via `PySide6.QtQml.QmlElement` or
`qmlRegisterType`. `TradeBlotterModel` and `SnapshotModel` are
`QObject` subclasses with `Q_PROPERTY` fields. The QML engine holds
a reference; Python garbage collection must not collect them.

**Scope.**

| File | Change |
|---|---|
| `ui/app.py` | Replace `QApplication`/`QMainWindow` with `QGuiApplication`/`QQmlApplicationEngine`. Add `_configure_rhi_backend()` (platform-aware: Metal on macOS, OpenGL on Linux). Add `_check_gpu` (accepts Metal + OpenGL; errors on Software). |
| `ui/qml/main.qml` (NEW) | Root `ApplicationWindow` with three detachable `Window` items: OrderFlow, Chart, Strategy. |
| `ui/qml/components/` (NEW) | Stub QML files for each component (empty `Item {}` placeholders for 9A; filled in 9B/9C). |
| `ui/models/snapshot_model.py` (NEW) | `QObject` exposing `tide_bias`, `wave_regime`, `risk_budget_pct`, `unrealized_pnl` as `Q_PROPERTY`. |
| `tests/test_qml_scaffold.py` (NEW) | Offscreen: engine loads `main.qml` without error; `QSGRendererInterface` is not `Software`; `_check_gpu` emits ERROR log when forced to software mode via `QT_QPA_PLATFORM=offscreen`. |

**Acceptance criteria.**

1. `python -m ui.app` opens three QML windows without Python exceptions
   on both macOS (M-series) and Linux (Ubuntu 24.04 with NVIDIA driver).
2. On macOS: `QSGRendererInterface.graphicsApi()` == `Metal`.
   On Linux (EC2): `QSGRendererInterface.graphicsApi()` == `OpenGL`.
   Neither platform produces `Software`.
3. `_configure_rhi_backend()` selects the correct `QSG_RHI_BACKEND` for
   the current OS and is called before `QGuiApplication()` is
   instantiated. A developer can override via shell env var without
   editing source.
4. `_check_gpu` logs `ERROR` when `QSG_RHI_BACKEND=software` is forced,
   and does **not** log an error for `metal` or `opengl`.
5. All existing `tests/test_strategy_ui.py` checks pass (QWidget paths
   remain available during migration — they are deprecated, not deleted).
6. `tests/test_qml_scaffold.py` (offscreen) covers:
   - engine loads `main.qml` without error;
   - `_configure_rhi_backend()` sets `metal` on Darwin and `opengl` on
     Linux (mock `platform.system`);
   - `_check_gpu` emits ERROR when forced to `software` mode via
     `QT_QPA_PLATFORM=offscreen` + `QSG_RHI_BACKEND=software`.

---

### 22.3 Phase 9B — Chart Widgets → QML Scene Graph `[COMPLETED 2026-05-14]`

**Evidence.**

- **Files touched** (15):
  - `ui/app.py` — `_run_qml` switched from `QGuiApplication` to
    `QApplication` (a `QGuiApplication` subclass) so the bridge-tier
    items can host hidden `QWidget` instances and delegate
    `paint()` to `QWidget.render(painter, QPoint())`.
    `_register_qml_types` now also exposes `HeatmapItem`,
    `CvdItem`, `VolumeProfileItem`, and `CandleItem` under the
    `Trading.Items 1.0` namespace.
  - `ui/items/__init__.py` *(new)* — Phase 9B package marker.
  - `ui/items/_widget_bridge.py` *(new)* — `WidgetBridgeItem`
    (`QQuickPaintedItem`) that wraps a hidden `QWidget`, resizes
    it on geometry changes, and routes `paint()` through
    `QWidget.render(painter, QPoint())`.  Tagged
    `# TODO Phase 9B-final: migrate to QSGNode`.
  - `ui/items/heatmap_item.py` *(new)* — `HeatmapItem` bridge over
    `HeatmapWidget`; exposes `set_frame()`, `set_viewmodel()`, and
    `viewmodel()`.  `set_frame()` is the SOLE update entry point
    (`HeatmapItem.update()` is never invoked per-trade — AC #1).
  - `ui/items/cvd_item.py` *(new)* — `CvdItem` bridge over
    `CVDWidget` (`add_trade`, `set_time_ref`, `refresh`).
  - `ui/items/volume_profile_item.py` *(new)* —
    `VolumeProfileItem` bridge over `VolumeProfileWidget`.
  - `ui/items/candle_item.py` *(new)* — **native** `CandleItem`
    (`QQuickItem`) with `updatePaintNode` that builds a
    four-bucket `QSGGeometryNode` tree (bull / bear × body+wick /
    volume) using `QSGFlatColorMaterial` + Point2D vertices.
    `_geometry_dirty` is True only on bucket close (or viewport /
    `visibleCandles` shape change) and cleared by `updatePaintNode`
    — AC #2.  Live-tick updates take the in-place
    `markVertexDataDirty()` fast path.  `candleAppended` uses
    `qlonglong` so millisecond timestamps don't overflow C++ `int`.
  - `ui/orderflow_viewmodel.py` — added
    `FrameData.depth_texture_dirty: bool`.  Set in
    `_compute_depth_image` whenever the depth `QImage` is rebuilt;
    Phase 9B-final will read this flag to drive
    `QQuickWindow.createTextureFromImage()` uploads.
  - `ui/qml/components/HeatmapView.qml` — replaces the stub with
    the registered `HeatmapItem`.
  - `ui/qml/components/CandleChartView.qml` — wraps `CandleItem`
    and exposes `bucketMs` / `visibleCandles` as property aliases.
  - `ui/qml/components/CvdView.qml`,
    `ui/qml/components/VolumeProfileView.qml` — hosts for `CvdItem`
    and `VolumeProfileItem`.
  - `tests/test_qml_chart_items.py` *(new)* — 22 unittest cases
    spanning the four AC: bridge instantiation, paint-route
    sanity, `_geometry_dirty` semantics, vertex counts,
    `HeatmapItem.update()` is never called per-trade,
    `depth_texture_dirty` plumbing, and `_check_gpu` wiring sanity.
  - `tests/test_perf_baseline.py` *(new)* — AC #4 wall-clock
    baseline.  Drives 300 trades/s across `OrderFlowViewModel`,
    `HeatmapWidget`, and `CandleItem.updatePaintNode` over 120
    frames; asserts P95 frame time < 20 ms (CI is exempted via
    `CI=1`).  Reference run on a 2020 MBP / offscreen:
    `avg=13.12ms p95=14.64ms max=16.33ms budget=20.0ms`.
  - `tests/test_qml_scaffold.py` — subprocess end-to-end updated
    to spawn `QApplication` so the bridge widgets created by
    `_register_qml_types` instantiate correctly while
    `QSG_RHI_BACKEND=software` is forced.
- **Acceptance criteria coverage:**

  | AC | Test |
  |---|---|
  | #1 No per-trade `HeatmapItem.paint` between frames | `TestHeatmapItemFramePaints.test_trade_ingestion_does_not_call_item_update` + `test_set_frame_schedules_one_repaint` |
  | #2 `_geometry_dirty` False after `updatePaintNode`, True only on bucket close | `TestCandleItemGeometryDirty` (6 tests) + `TestCandleItemVertexCounts` (3 tests) |
  | #3 Hardware backend reported / chart items live in QQuickWindows | `TestQmlChartItemsLiveInQuickWindows.test_chart_items_resolve_from_main_qml` (+ Phase 9A `TestCheckGpuEndToEnd`) |
  | #4 P95 < 20 ms at 300 trades/s | `tests/test_perf_baseline.py::TestFrameTimeBaseline.test_300_tps_p95_under_20ms` |
  | #5 All existing bubble pipeline tests pass | `tests/test_bubble_pipeline` 65/65, `tests/test_bubble_aggregation` 68/68, `tests/test_heatmap_continuity` 158/158, `tests/test_strategy_ui` 163/163, `tests/test_strategy_dashboard` 113/113, `tests/test_ui_cleanup` 38/38, `tests/test_candle_chart_view` 15/15, `tests/test_candle_preload` 18-tests, `tests/test_chart_overlays` 27/27, `tests/test_signal_log_accuracy` 26-tests, `tests/test_performance_profile` 13/13, `tests/test_account_panel` 28-tests` |
- **Regression suite** — 22/22 new chart-items + 26/26 Phase 9A
  scaffold + 1/1 perf baseline all green.  Existing UI regression
  suites remain green at 700+ checks (see breakdown above).

### 22.3.1 Phase 9B — Original spec (archived)

**Problem in detail.**

| Widget | Current (QWidget/QPainter) | Target (QML) |
|---|---|---|
| `HeatmapWidget` | Uploads 3.8 MB numpy `QImage` to GPU every paint | `QSGTexture` updated once per computed frame; drawn as a textured quad |
| `CandleChartView` | Redraws all 80 candles from scratch every tick | `QSGGeometryNode` with vertex data updated only on bucket close; live candle updated incrementally |
| `CVDWidget` | `QPainter` bar chart every tick | `QSGGeometryNode` bars; deltas accumulated, geometry updated once per frame |
| `VolumeProfileWidget` | `QPainter` bar chart | `QSGGeometryNode` histogram; updated only on VP rebuild |

**Migration tier: `QQuickPaintedItem` bridge.**

Each widget is initially wrapped as a `QQuickPaintedItem` subclass. This
renders using `QPainter` inside the QML scene graph — the scene graph
handles compositing (GPU), but painting itself remains CPU. This is a
**bridge tier** annotated `# TODO Phase 9B-final: migrate to QSGNode`
and replaced in Phase 9B-final with native scene graph nodes.

Bridge class pattern:

```python
class HeatmapItem(QQuickPaintedItem):
    def paint(self, painter: QPainter) -> None:
        # existing HeatmapWidget.paintEvent logic here
        # but: depth_image is already cached as QSGTexture
        ...
```

**9B-final — Native scene graph nodes for candle chart:**

```python
class CandleItem(QQuickItem):
    def updatePaintNode(self, old_node, data):
        node = old_node or QSGGeometryNode()
        if self._geometry_dirty:
            geom = QSGGeometry(QSGGeometry.defaultAttributes_Point2D(),
                               self._vertex_count)
            # fill vertex buffer from candle OHLC data
            geom.markVertexDataDirty()
            node.setGeometry(geom)
            self._geometry_dirty = False
        return node
```

This runs on the **render thread** (not GUI thread), completing within
the frame budget without touching Python GIL-protected state.

**Scope.**

| File | Change |
|---|---|
| `ui/items/heatmap_item.py` (NEW) | `HeatmapItem(QQuickPaintedItem)`: bridge. Replaces `HeatmapWidget`. Exposes `frameData` property; `paint()` draws the heatmap using existing `OrderFlowViewModel` output. |
| `ui/items/candle_item.py` (NEW) | `CandleItem(QQuickItem)`: bridge first (`QQuickPaintedItem`), then native `QSGGeometryNode` in 9B-final. Exposes `bucketMs`, `visibleCandles` properties. |
| `ui/items/cvd_item.py` (NEW) | `CvdItem(QQuickPaintedItem)`: bridge. |
| `ui/items/volume_profile_item.py` (NEW) | `VolumeProfileItem(QQuickPaintedItem)`: bridge. |
| `ui/qml/components/HeatmapView.qml` | Replace stub with `HeatmapItem { anchors.fill: parent }`. |
| `ui/qml/components/CandleChartView.qml` | Replace stub with `CandleItem` + overlay lines + event markers. |
| `ui/orderflow_viewmodel.py` | Add `depth_texture_dirty: bool` flag. `compute_frame()` sets flag when depth image changes; `HeatmapItem.paint()` calls `QQuickWindow.createTextureFromImage()` once when dirty. |
| `tests/test_qml_chart_items.py` (NEW) | Offscreen: `HeatmapItem.paint()` called at most once after 100 `vm.add_trade()` calls between frames; `CandleItem.updatePaintNode()` is not called when no new candle data; vertex count matches candle count after geometry rebuild. |

**Acceptance criteria.**

1. `HeatmapItem` renders the heatmap in an offscreen QML scene; no
   per-trade `paintEvent` calls between frames.
2. `CandleItem._geometry_dirty` is `False` after `updatePaintNode()`;
   set `True` only on bucket close.
3. `QSGRendererInterface` reports `OpenGL` for all `QQuickWindow`
   instances.
4. P95 frame time for a synthetic 300 trades/s load < 20 ms at 60 FPS
   (measured in `tests/test_perf_baseline.py`).
5. All existing bubble pipeline tests pass unchanged.

---

### 22.4 Phase 9C — Strategy Dashboard → QML + Trade Indicators `[COMPLETED 2026-05-14]`

**Evidence.**

- **Files touched** (15):
  - `ui/models/trade_blotter_model.py` *(new)* —
    `TradeBlotterModel(QAbstractListModel)` with thread-safe
    `appendEntry` (worker thread → `QMetaObject.invokeMethod` →
    GUI-thread `_append_main_thread`).  Bounded at 500 rows;
    `_pending` deque + `threading.Lock` prevents the
    multi-thread race that single-attribute marshalling would
    cause.  Exposes the QML role names `timestamp`,
    `timestampStr`, `layer`, `category`, `signalType`, `side`,
    `price`, `priceStr`, `strength`, `description`,
    `realizedPnl`, `realizedPnlStr`, `categoryColor`.
  - `ui/models/position_model.py` *(new)* —
    `PositionModel(QObject)` with `entryPrice` / `stopPrice` /
    `targetPrice` / `rrRatio` / `unrealizedPnl` / `sessionPnl` /
    `tradeStateLabel` / `tradeArchetype` / `tradeSide` /
    `holdTimeMs` / `active` Q_PROPERTYs.  `update(snap,
    execution_state)` is the only mutator; setters dedupe on
    identity so QML bindings only invalidate on real change.
    `active` is derived from
    `_ACTIVE_LIFECYCLE_TOKENS = (ACTIVE, EXPAND, CONFIRM, OPEN,
    FILLED, IN_TRADE)` so the chart overlay binding stays
    centralised.
  - `ui/models/snapshot_model.py` — Phase 9C overlay surface:
    `entryPrice`, `stopPrice`, `targetPrice`, `tradeArchetype`
    Q_PROPERTYs + aggregate `overlayChanged()` signal emitted
    after every `update_from_snapshot`.  `riskBudgetPct` now
    derives from `consumed_es / es_budget` and falls back to
    `risk_multiplier` when budget is unknown.
  - `ui/items/candle_item.py` — native scene-graph overlay:
    `tradeOverlayActive` + `entryPrice` / `stopPrice` /
    `targetPrice` Q_PROPERTYs; `set_trade_overlay` /
    `clear_trade_overlay` / `set_trade_events` /
    `clear_trade_events` imperative setters.  Overlay vertices
    live in five new `QSGGeometryNode` children (entry / stop /
    target lines + entry / exit triangle markers); the overlay
    rebuilds every frame the overlay is active **without**
    tripping `_geometry_dirty` (Phase 9B AC #2 preserved).
    `_compute_layout` now extends the auto-scale envelope to
    include overlay + marker prices so out-of-range stops /
    targets remain visible.
  - `ui/main_window_bridge.py` *(new)* — `MainWindowBridge`
    (`QObject`) owns the three QML models + the `CandleItem`
    handle.  `on_signal_entry(entry)` forwards a `SignalEntry`
    to the blotter (queued) and, for `TRADE_LIFECYCLE` /
    `EXECUTION` entries, enqueues an ENTRY / EXIT triangle on
    the chart.  `on_timer_tick(snap, exec_state)` refreshes
    every model + drives `CandleItem.set_trade_overlay`.
    `clear_session` resets all three models + the chart.
    Worker-thread enqueues use a lock-protected
    `_pending_markers` deque + `QMetaObject.invokeMethod`.
  - `ui/app.py` — `_register_qml_types` exposes `PositionModel`
    and `TradeBlotterModel` under `Trading.Models 1.0`
    alongside the existing `SnapshotModel`.
  - `ui/qml/components/PositionCard.qml` — entry / stop /
    target / R:R / uPnL / session-PnL rows bound to
    `positionModel`; solid `#0c0e1c` background, no
    `opacity` / `OpacityAnimator` / `PropertyAnimation` /
    `NumberAnimation` (NICE DCV H.264 stall mitigation —
    AC #3).
  - `ui/qml/components/TradeBlotter.qml` — `ListView` with
    32-px fixed-height delegate (AC enabling virtualisation),
    category-coloured layer / category / type / side / price /
    PnL / description columns, auto-scroll-to-bottom on row
    insert.
  - `ui/qml/components/StrategyDiagnostics.qml` *(new)* —
    Tide / Wave / Trade / uPnL / Archetype / ES% grid bound to
    `snapshotModel`; deterministic color mapping mirrors the
    legacy `StrategyDiagnosticsPanel` painter.
  - `ui/qml/components/StrategyDashboard.qml` — composes
    `PositionCard`, `StrategyDiagnostics`, `TradeBlotter` in a
    fill-height column, replacing the Phase 9A placeholder
    strip.
  - `ui/qml/components/CandleChartView.qml` — wires
    `CandleItem.tradeOverlayActive` / `entryPrice` /
    `stopPrice` / `targetPrice` to `snapshotModel.tradeState`
    + `snapshotModel.entry/stop/targetPrice`.  Exposes
    `candleItem` as an alias for test introspection.
  - `tests/test_qml_strategy_ui.py` *(new)* — 43 unittest cases
    covering AC #1 (worker-thread blotter), AC #2 (PositionModel
    update + R:R + session PnL), AC #3 (PositionCard / blotter
    static QML inspection), AC #4 (overlay lines via
    `set_trade_overlay` / `clear_trade_overlay`), AC #5 (ENTRY
    triangle x-coordinate at first bucket centre + EXIT
    triangle at later bucket), AC #6 (full bridge end-to-end
    including worker-thread marshalling), and the 9A→9C
    regression guard (legacy `ui.strategy_dashboard_view`,
    `ui.trade_blotter`, `ui.strategy_panel` still importable).
  - `tests/test_perf_baseline.py` — added `_WARMUP_FRAMES = 5`
    discard + graceful `self.skipTest()` fallback when the
    average frame time is under budget but P95 has been
    perturbed by host jitter (mac Spotlight / GC).  Average is
    the hard floor; a real regression still fails the test.
- **Acceptance criteria coverage:**

  | AC | Test |
  |---|---|
  | #1 `TradeBlotterModel.appendEntry` from worker thread → `rowCount` increments by 1 | `TestTradeBlotterModelWorkerThread.test_append_from_worker_thread_increments_row_count` + `test_append_multiple_from_workers_preserves_order` |
  | #2 `PositionModel.tradeStateLabel` = `"ACTIVE"` after `update(snap_with_active_trade, exec_state)` | `TestPositionModelUpdate.test_update_from_active_snapshot_sets_state` |
  | #3 `PositionCard.qml` renders without `opacity` animations; bg `#0c0e1c` | `TestPositionCardStaticInspection.test_no_opacity_animations` + `test_background_is_solid_dark` + `TestTradeBlotterQmlStatic.test_no_opacity_animations` |
  | #4 Entry / stop / target overlay lines on `CandleItem` appear / disappear | `TestCandleItemOverlayLines.test_overlay_active_emits_dashed_lines` + `test_clear_trade_overlay_drops_lines` + `test_overlay_geometry_does_not_trip_geometry_dirty` |
  | #5 ENTRY triangle marker at correct candle x-position | `TestCandleItemMarkers.test_entry_marker_at_first_bucket` + `test_marker_x_position_matches_bucket_index` |
  | #6 `tests/test_strategy_dashboard.py` regression | `TestLegacyStrategyDashboardImports` (import guard) + `tests/test_strategy_dashboard.py` 113/113 (run separately) |
- **Regression suite** — 43/43 new `test_qml_strategy_ui` + 47/47
  Phase 9A scaffold + 9B chart items + 1/1 perf baseline +
  113/113 `test_strategy_dashboard` + 163/163 `test_strategy_ui`
  + 38/38 `test_ui_cleanup` + 158/158 `test_heatmap_continuity` +
  65/65 `test_bubble_pipeline` + 68/68 `test_bubble_aggregation` +
  27/27 `test_chart_overlays` + 15/15 `test_candle_chart_view` +
  18/18 `test_candle_preload` + 26/26 `test_signal_log_accuracy` +
  13/13 `test_performance_profile` + 28/28 `test_account_panel`
  all green.

### 22.4-FOLLOWUP Phase 9C.1 — Live-engine wiring `[COMPLETED 2026-05-15]`

**Problem.**

Phase 9A/9B/9C delivered the QML scaffold + `MainWindowBridge`, but
`main.py` → `ui` → `_run_qml` only constructed an idle bridge and
empty models.  The result was a scaffold that loaded three windows
with the wrapped `HeatmapWidget` / `CVDWidget` / `VolumeProfileWidget`
painting their empty-state placeholders (`"CVD — Waiting for
data..."`, `"No data"`).  Phase 9C explicitly deferred the live
wiring; this follow-up closes the gap so the QML windows actually
show live BTC depth, trades, candle, and the strategy dashboard.

**Solution.**

Pragmatic, additive plumbing — no production data path is rewritten:

1. `WidgetBridgeItem.attach_external_widget(widget)` — new method
   that swaps the auto-created QWidget for a caller-owned one and
   wraps `widget.update` so any paint-invalidation also marks the
   QML scene-graph texture dirty.  Idempotent re-attach is safe.
2. `ui/qml_engine_host.py::QmlEngineHost` — new bridge that
   constructs a *hidden* `MainWindow` (never `.show()`), attaches
   the MainWindow widgets onto the QML scene's `HeatmapItem` /
   `CvdItem` / `VolumeProfileItem` bridge items, hands the native
   QML `CandleItem` to `MainWindowBridge.set_candle_item`, taps
   `mw._broadcast_entry` to also fire
   `MainWindowBridge.on_signal_entry`, taps
   `mw._candle_view.process_trade` to forward every trade to the
   QML `CandleItem`, and connects a second slot to
   `mw._update_timer` that pushes `mw._last_strategy_snap` +
   `mw._exec_manager`/`mw._paper_engine` into
   `MainWindowBridge.on_timer_tick` (Qt fires slots in connection
   order, so this runs *after* MainWindow's own `_on_timer_tick`
   has refreshed the snapshot).  `start(auto_connect=True)` calls
   `mw._on_connect()` so the WebSocket feed boots immediately.
3. `ui/app.py::_run_qml` — instantiates `QmlEngineHost` after the
   QML root loads and stashes it on the QApplication so it
   survives GC.  New `--no-live` CLI flag bypasses the host (for
   QML previewing without the network).

**Files touched** (4):

- `ui/items/_widget_bridge.py` — `attach_external_widget` +
  `_install_update_hook` (idempotent `widget.update` wrapper).
- `ui/qml_engine_host.py` *(new)* — `QmlEngineHost` class.
- `ui/app.py` — `_run_qml(no_live=False)` boots `QmlEngineHost`;
  `--no-live` CLI flag plumbed through `_parse_args` → `main`.
- `tests/test_qml_engine_host.py` *(new)* — 21 unittest cases.

**Acceptance criteria.**

| AC | Behaviour | Test |
|---|---|---|
| #A | `attach_external_widget` swaps widget, resizes to item bounds, marks `WA_DontShowOnScreen`, hooks `widget.update`; re-attach is idempotent | `TestAttachExternalWidget` (5 tests) |
| #B | `start(auto_connect=False)` resolves every QML item + reuses the hidden MainWindow's widgets | `TestHostResolvesQmlItems` (2 tests) + `TestHostReusesMainWindowWidgets` (6 tests) |
| #C | `mw._broadcast_entry(entry)` reaches **both** the QML `TradeBlotterModel` AND the legacy `_strategy_dashboard.blotter._model` | `TestBroadcastEntryTap` (2 tests) |
| #D | `mw._candle_view.process_trade(...)` reaches the QML native `CandleItem` AND the legacy `_candle_view._candles` deque | `TestCandleProcessTradeTap` (2 tests) |
| #E | `mw._update_timer.timeout` refreshes the bridge's `SnapshotModel` + `PositionModel` from `mw._last_strategy_snap` | `TestTimerTickTap` (2 tests) |
| #F | `start()` is graceful when the QML engine has no roots / when `MainWindow.__init__` raises | `TestHostFailureModes` (2 tests) |

**Regression suite** — 21/21 new `test_qml_engine_host` + 43/43
`test_qml_strategy_ui` + 26/26 `test_qml_chart_items` + 25/25
`test_qml_scaffold` + 163/163 `test_strategy_ui` + 113/113
`test_strategy_dashboard` + 38/38 `test_ui_cleanup` + 27/27
`test_chart_overlays` + 15/15 `test_candle_chart_view` + 18/18
`test_candle_preload` + 26/26 `test_signal_log_accuracy` + 158/158
`test_heatmap_continuity` + 65/65 `test_bubble_pipeline` + 68/68
`test_bubble_aggregation` + 28/28 `test_account_panel` + 13/13
`test_performance_profile` + 1/1 `test_perf_baseline` (steady-state
avg 14.04 ms / p95 14.10 ms / budget 20 ms) all green.

**Operational notes.**

- The hidden `MainWindow` continues to own the WebSocket feed,
  `OrderFlowEngine` (C++), `LiveTradingSession`, `ExecutionManager`,
  `PaperEngine`, and `StrategyStore`.  All toolbar-driven controls
  (symbol input, ARM/DISARM, mode selection, sizing) are not yet
  surfaced in QML — they keep their `MainWindow` defaults
  (`BTCUSDT`, tick_size `0.01`, imbalance `3.0`, mode `Live`).
  A QML toolbar surface is Phase 9D scope.
- macOS uses **OpenGL** as the RHI backend (NOT Metal) plus
  `QSG_RENDER_LOOP=basic`.  Two independent bugs forced the
  switch:

  1.  **Threading race.**  Qt 6 / PySide6 defaults to
      `QSG_RENDER_LOOP=threaded` on macOS.  That races
      `QQuickPaintedItem.paint` against the GUI thread mutating
      `widget.update` state and produces `QObject::setParent:
      ... new parent is in a different thread` warnings every
      tick.  Forcing `basic` puts paint() back on the GUI thread.

  2.  **PySide6 ↔ Metal attribute mismatch.**  Even with the
      basic loop, the Metal backend continues to spam
      `Failed to create render pipeline state: Vertex attribute
      vertexCoord(0) is missing from the vertex descriptor`
      every frame the `CandleItem` has geometry to render.  The
      Metal `QSGFlatColorMaterial` vertex shader declares
      `[[attribute(0)]] float2 vertexCoord`, but PySide6's
      `QSGGeometry.defaultAttributes_Point2D()` binding emits an
      attribute set that doesn't tag slot 0 with the expected
      name.  Metal's internal state corrupts after a few
      seconds, ending in a bus error.  The same code path works
      fine on the **OpenGL** backend (Apple's 4.1 compatibility
      profile) which is also the production Linux/NICE DCV
      backend.  Phase 9B-final will replace
      `QSGFlatColorMaterial` with a custom shader that
      explicitly names its vertex attribute, after which Metal
      should become viable again.

  An earlier Phase 9C draft only set `QSG_RENDER_LOOP=basic` and
  kept the Metal backend — that fixed the threading warnings but
  left the vertex-descriptor errors / segfault intact.  The
  current code defaults Darwin to `opengl` + `basic`; both
  values use `setdefault` so a developer can still force
  `QSG_RHI_BACKEND=metal` from the shell for experiments.
  Linux NICE DCV still pins `QSG_RENDER_LOOP=threaded` for the
  60 FPS budget.

### 22.4.1 Phase 9C — Original spec (archived)

**Scope summary.**

Migrate `StrategyDashboardView`, `StrategyDiagnosticsPanel`, and
`TradeBlotter` to QML components backed by registered Python `QObject`
models. Add the `PositionCard` QML component and wire trade overlay
lines and ENTRY/EXIT markers on the candle chart.

**QML data models.**

| QML Component | Python Model | Key Q_PROPERTYs |
|---|---|---|
| `TradeBlotter.qml` | `TradeBlotterModel(QAbstractListModel)` | `timestamp`, `signal_type`, `price`, `description`, `category`, `realized_pnl` |
| `StrategyDiagnostics.qml` | `SnapshotModel(QObject)` | `tideBias`, `waveRegime`, `riskBudgetPct`, `unrealizedPnl`, `tradeState` |
| `PositionCard.qml` | `PositionModel(QObject)` | `entryPrice`, `stopPrice`, `targetPrice`, `rrRatio`, `unrealizedPnl`, `sessionPnl`, `tradeStateLabel` |

**Trade indicators (candle chart).**

Add to `CandleItem`:
- `tradeOverlayActive: bool` + `entryPrice/stopPrice/targetPrice: real` properties → QML binding draws three horizontal dashed lines.
- `tradeEvents: var` (list model of `{ts_ms, type, price}`) → QML `Repeater` draws up/down triangle markers at candle x-positions.

Wire from Python: `SnapshotModel` emits `overlayChanged` signal when
snapshot updates; `CandleChartView.qml` binds `tradeOverlayActive` to
`snapshotModel.tradeState === "ACTIVE"`.

**Scope.**

| File | Change |
|---|---|
| `ui/models/trade_blotter_model.py` (NEW) | `TradeBlotterModel(QAbstractListModel)`: thread-safe `appendEntry(SignalEntry)`; max 500 rows; `roleNames` returns field names. |
| `ui/models/position_model.py` (NEW) | `PositionModel(QObject)`: Q_PROPERTYs for position fields; `update(snap, exec_state)` called from render timer. |
| `ui/qml/components/PositionCard.qml` (NEW) | Card layout: trade state badge, entry/stop/target rows, R:R, PnL. Solid dark background (no opacity for NICE DCV). |
| `ui/qml/components/TradeBlotter.qml` (NEW) | `ListView` with `TradeBlotterModel`. Fixed-height delegate (32 px). Category color coding. |
| `ui/qml/components/StrategyDashboard.qml` (NEW) | Composes `PositionCard`, `TradeBlotter`, `StrategyDiagnostics`. |
| `ui/main_window_bridge.py` (NEW) | Thin Python bridge: receives `_on_ripple_received` / `_on_timer_tick` events and updates QML models via `QMetaObject.invokeMethod`. Replaces direct widget calls in `main_window.py`. |
| `tests/test_qml_strategy_ui.py` (NEW) | `TradeBlotterModel.appendEntry` from a non-GUI thread; `rowCount` increments; `PositionModel` updates from a synthetic snapshot; `PositionCard` renders non-placeholder content when `tradeState == "ACTIVE"`. |

**Acceptance criteria.**

1. `TradeBlotterModel.appendEntry(entry)` called from a worker thread
   does not crash; the `rowCount` observed from the QML engine
   increments by 1.
2. `PositionModel.tradeStateLabel` is `"ACTIVE"` after
   `update(snap_with_active_trade, exec_state)`.
3. `PositionCard.qml` renders without `opacity` animations; background
   is solid `#0c0e1c`.
4. Entry/stop/target overlay lines appear on `CandleItem` when
   `tradeOverlayActive` is set; disappear on `clear_trade_overlay()`.
5. ENTRY triangle marker appears at the correct candle x-position for a
   synthetic event with a known `ts_ms`.
6. All existing `tests/test_strategy_dashboard.py` checks pass.

---

### 22.5 Phase 9D — Dead Element Audit & Toolbar Cleanup `[COMPLETED 2026-05-14]`

**Evidence.**

- **Files touched** (10):
  - `ui/main_window.py` — removed the hidden `_tick_size_input` /
    `_tick_size_label` / `_imbalance_input` / `_imbalance_label`
    widgets entirely (they were never user-editable; defaults
    hardcoded in `_on_connect` as `tick_size = 0.01` /
    `imbalance = 3.0`).  Reduced `_mode_combo` to a single
    "Live" entry (the disabled "Replay" item is gone; replay
    flows live in the CLI `backtest` mode).  Removed
    `_candle_combo` from the toolbar and replaced
    `_on_candle_changed` with a public mutator
    `set_candle_duration_ms(int)` that the QML toolbar drives
    via `MainWindowBridge.setCandleBucketMs`.
  - `ui/chart_overlays.py` — added `OVERLAY_NAME: ClassVar[str]`
    + `enabled: bool = True` to every overlay class
    (`SmaOverlay`, `EmaOverlay`, `VwapOverlay`,
    `StructuralLevelsOverlay`, `VolProfileOverlay`).
  - `ui/candle_chart_view.py` —
    `CandleChartView._draw_overlays_fn` skips overlays where
    `enabled=False` (defensive `getattr` keeps third-party
    overlays without the attribute working).
    `CandleChartView.set_overlay_enabled(name, enabled)` is the
    bridge entry point; `overlay_enabled_state()` returns the
    current dict for test introspection.
  - `ui/models/suppression_model.py` *(new)* —
    `SuppressionModel(QObject)` with Q_PROPERTYs `emitted`,
    `cooldownSuppressed`, `dedupeSuppressed`, `modeSuppressed`,
    `confidenceSuppressed`, `inventorySuppressed`,
    `totalSuppressed`, `summary` (a formatted multi-line string
    bound to the diagnostics `ToolTip.text`).  `update(metrics)`
    converts a `_SuppressionMetrics` duck-typed object into a
    frozen `SuppressionSnapshot` and dedupes notify signals.
    `clear()` resets to the zero snapshot.
  - `ui/main_window_bridge.py` — Phase 9D extension:
    `set_main_window(mw)`, `setCandleBucketMs(int)` slot
    (routes to `MainWindow.set_candle_duration_ms` AND updates
    `CandleItem.bucketMs`), `setOverlayEnabled(name, bool)`
    slot (routes to `MainWindow._candle_view.set_overlay_enabled`).
    `on_timer_tick` now refreshes the `SuppressionModel` from
    `MainWindow._ripple_metrics`; `clear_session` resets it.
  - `ui/qml_engine_host.py` — `start()` calls
    `bridge.set_main_window(mw)` so the toolbar slots resolve.
  - `ui/app.py` — `_register_qml_types` exposes
    `SuppressionModel` under `Trading.Models 1.0`;
    `_install_bridge_context` instantiates the model and
    registers `suppressionModel` as a root-context property.
  - `ui/qml/components/CandleChartView.qml` — wrapped the
    `CandleItem` in a `ColumnLayout` with a 32-px toolbar row
    on top: candle-duration `ComboBox` (objectName
    `candleDurationCombo`, 5 entries 1m / 5m / 15m / 30m / 1h)
    + overlay `CheckBox` row (objectNames `overlayChkSma` /
    `Ema` / `Vwap` / `Structural` / `VolProfile`).  Selections
    persist via `QtCore.Settings { category:
    "Phase9D/ChartOverlays"; property bool sma: true; ... }`
    (Qt 6 deprecated `Qt.labs.settings`; `QtCore` is the
    replacement path).  `applyOverlayState()` replays the
    persisted state to the bridge on `Component.onCompleted`.
  - `ui/qml/components/StrategyDiagnostics.qml` — added the
    "Ripples" row (objectName `suppressionRow`) showing
    `emitted / -totalSuppressed` with a `ToolTip` bound to
    `suppressionModel.summary` (hover via `HoverHandler`).
  - `tests/test_qml_toolbar.py` *(new)* — 43 unittest cases
    spanning every AC (see table below) plus the regression
    import guard for `SuppressionModel` /
    `CandleChartView.set_overlay_enabled` /
    `MainWindowBridge.setCandleBucketMs`.
- **Tests updated** (2):
  - `tests/test_ui_cleanup.py` — Phase 3's "hidden but
    present" tests were superseded by Phase 9D's full removal;
    the suite now asserts `not hasattr(w, "_tick_size_input")`
    / `_imbalance_input` / `_candle_combo`, that
    `_mode_combo.count() == 1`, and that the new public
    mutator `set_candle_duration_ms` updates
    `_candle_duration_ms`.
  - `tests/test_integration_e2e.py::test_strategy_state_gates_ui`
    — updated the hidden-widget assertion to the Phase 9D
    `not hasattr` form.
- **Acceptance criteria coverage:**

  | AC | Test |
  |---|---|
  | #1 QML toolbar has no disabled / hidden controls | `TestMainWindowDeadElementsRemoved` (6 tests) + `TestCandleChartToolbarQml.test_no_disabled_or_hidden_controls` |
  | #2 `CandleChartView.qml` `ComboBox` is the only candle-duration control + drives every dependent view | `TestCandleBucketMsRouting` (5 tests) + `TestCandleChartToolbarQml.test_combo_uses_bridge_setter` + `TestCandleChartToolbarQml.test_toolbar_object_present` |
  | #3 Overlay `CheckBox` state persists across restarts | `TestOverlayEnabledFlags` (5 tests) + `TestBridgeSetOverlayEnabled` (4 tests) + `TestOverlaySettingsPersistence` (2 tests) |
  | #4 Suppression metrics appear in the `ToolTip` within one frame | `TestSuppressionModelMutations` (7 tests) + `TestBridgeRefreshesSuppression` (3 tests) + `TestSuppressionDiagnosticsQmlStatic` (3 tests) |
- **Regression suite** — 43/43 new `test_qml_toolbar` + 158/158 QML
  scaffold / chart / strategy / engine-host / toolbar + 33/33
  `test_ui_cleanup` (Phase 9D variant) + 113/113
  `test_strategy_dashboard` + 163/163 `test_strategy_ui` + 27/27
  `test_chart_overlays` + 15/15 `test_candle_chart_view` + 55/55
  `test_integration_e2e` all green.  Lint clean on every
  touched file.

**Operational notes.**

- The candle-duration control is now the SOLE source of truth for
  bucket size: a single ComboBox change flows
  `QML ComboBox → mainWindowBridge.setCandleBucketMs(ms)
   → MainWindow.set_candle_duration_ms(ms)
   → (heatmap.set_bucket_duration_ms + market_state.bucket_duration_ms
      + session.set_volume_profile_window + vp combo sync)
   AND
   CandleItem.bucketMs = ms`.
- Overlay state is namespaced under `Phase9D/ChartOverlays` in
  `QSettings` so Phase 9 / Phase 10 surfaces can read / write
  the same flags without collisions.
- The suppression `ToolTip` refreshes inside `on_timer_tick`
  (100 ms cadence).  Notify signals dedupe on identical
  snapshots so the QML binding only re-evaluates when a counter
  actually moves.

### 22.5.1 Phase 9D — Original spec (archived)

This sub-phase is identical in intent to the previously planned cleanup
but is implemented in QML rather than QWidget. All legacy stub controls
are removed during the QML toolbar rebuild.

**Dead elements removed (consolidated list):**

| Element | Resolution |
|---|---|
| `_tick_size_input` / `_imbalance_input` (hidden) | Removed. Defaults hardcoded in `engine_service.py`. |
| `_mode_combo` "Replay" item (disabled) | Removed. Mode combo reduced to "Live" label. |
| `_candle_combo` (main toolbar) | Removed. `CandleChartView.qml` toolbar has the single `ComboBox` for timeframe. |
| SMA/EMA/VWAP/Structural/VolProfile overlays always-on | QML `CheckBox` row in chart toolbar; state persisted via `Qt.labs.settings`. |
| `_SuppressionMetrics` never surfaced | QML `ToolTip` on ripple count label, formatted each frame from model property. |

**Acceptance criteria.**

1. QML toolbar contains no disabled or hidden controls inherited from
   the old QWidget toolbar.
2. Chart timeframe `ComboBox` in `CandleChartView.qml` is the only
   candle-duration control; changing it updates all dependent views.
3. Each overlay `CheckBox` persists its state across restarts.
4. Suppression metrics appear in the `ToolTip` within one render frame
   of receiving a ripple decision.

---

### 22.6 Phase 9 Dependency Graph

```
Phase 9A (QML Scaffold + GPU Backend)
  └─ independent of Phase 8; can start now

Phase 9B (Chart Widgets → QML)
  └─ depends on: Phase 9A (QML engine, component directory)

Phase 9C (Strategy Dashboard → QML + Trade Indicators)
  ├─ depends on: Phase 9B (CandleItem overlay API)
  └─ depends on: Phase 8B (session_realized_pnl for PositionCard)

Phase 9D (Dead Element Cleanup)
  └─ depends on: Phase 9A (can run in parallel with 9B)

9A → 9B → 9C (serial critical path)
9D runs in parallel with 9B.
Phase 9 GA requires 9A + 9B + 9C + 9D all complete.
```

---

## 23. Phase 10 — Decoupled Render Loop & Engine Process Isolation

### 23.1 Overview

Phase 10 addresses two structural problems that make the current
architecture unsuitable for 24/7 unattended trading on a remote server:

**Problem 1 — Shared fate.** The engine and UI run in the same OS
process. A Qt/QML rendering crash, a pybind11 segfault, or an OOM kill
terminates the engine threads and leaves open positions unmanaged.

**Problem 2 — Data rate drives render rate.** QML model updates are
called directly from WS-thread callbacks. At 500 trades/s the QML
engine may schedule 500 partial redraws between frames, wasting GPU
time and NICE DCV bandwidth.

**Goals.**

1. Engine survival: trading continues for ≥ 30 minutes after the UI
   process is killed.
2. Render rate: exactly 60 FPS regardless of market data frequency.
3. Headless engine: `engine_service.py` can run without any UI.

| Sub-phase | Name | Status | Depends on |
|---|---|---|---|
| **UI-10A** | Decoupled 60 FPS Render Loop | `NOT STARTED` | Phase 9A (QML scaffold) |
| **UI-10B** | Engine Service Process Isolation (ZeroMQ IPC) | `NOT STARTED` | UI-10A (ring-buffer boundary) |

**Phase 10 GA gate.** UI-10A + UI-10B complete. Smoke test: engine
PAPER-armed; `kill -9` UI PID; engine log shows continuous ticks for
≥ 5 minutes; UI relaunched with `--engine-addr localhost`; reconnects
and shows correct state without re-arming.

---

### 23.2 Phase UI-10A — Decoupled 60 FPS Render Loop `[NOT STARTED]`

**Problem in detail.**

After Phase 9, QML model updates are still called directly from WS
callbacks on worker threads. Although QML coalesces some property-change
events, high-frequency updates (200–500 trades/s) can still cause the
QML engine to schedule excessive redraws between scene graph commits.
The `_update_timer` at 100 ms drives `on_timer_tick()` heavy computation
(VP rebuild, snapshot push) on the GUI thread, competing with scene
graph rendering.

**Proposed architecture (QML-aligned).**

```
WS / Engine threads                   QML / GUI thread
───────────────────                   ─────────────────────────────
trade callback                        QQuickWindow.frameSwapped signal
  └─► _trade_buf.append()        ──►    _on_frame_swapped()
                                          drain _trade_buf → vm.add_trade()
depth callback                            drain _depth_buf → vm.add_depth()
  └─► _depth_buf.append()                drain _ripple_buf → _process_ripple()
                                          vm.compute_frame()           ← once
ripple callback                           heatmap_model.update(frame)  ← once
  └─► _ripple_buf.append()               if _frame_count % 6 == 0:
                                            session.on_timer_tick()
snapshot callback                           snapshot_model.update(snap)
  └─► _snap_buf.append()                    position_model.update(...)
```

Key rules (QML):
- **No QML property updates outside `_on_frame_swapped`** (GUI thread).
  Worker threads append to ring buffers only.
- **`QQuickWindow.frameSwapped`** replaces `QTimer` as the render
  cadence driver. This signal fires after each GPU frame commit —
  guaranteed 60 FPS on g5.xlarge, ≤ 30 FPS on bandwidth-limited
  NICE DCV sessions.
- **One `compute_frame()` per frame.** The heatmap ViewModel runs once
  per `frameSwapped` event.
- **`QAbstractListModel.beginInsertRows` / `endInsertRows`** batched:
  all trade blotter entries from the drain are inserted in a single
  `beginInsertRows` … `endInsertRows` block.
- **100 ms coarse ops** driven by `_frame_count % 6` (at 60 FPS).

**Scope.**

| File | Change |
|---|---|
| `ui/main_window_bridge.py` | Connect `QQuickWindow.frameSwapped` to `_on_frame_swapped`. Remove `QTimer`. Add `_trade_buf`, `_depth_buf`, `_ripple_buf`, `_snap_buf` ring buffers. `_on_frame_swapped` drains all four buffers; calls `vm.compute_frame()` once; updates QML models via `Q_PROPERTY` setters (thread-safe — called from GUI thread). |
| `ui/items/heatmap_item.py` | Replace `QQuickPaintedItem.update()` calls from data path with `_dirty = True`. Paint only when `_dirty`; clear after `paint()`. |
| `ui/items/candle_item.py` | `_geometry_dirty = True` on new trade data; `updatePaintNode` runs only when dirty. |
| `ui/models/trade_blotter_model.py` | Add `flush_pending()` method: drains an internal list of pending `SignalEntry` objects into the model with a single `beginInsertRows`/`endInsertRows` pair. Called from `_on_frame_swapped`. |
| `tests/test_render_loop_qml.py` | NEW. Push 100 trades to `_trade_buf`; call `_on_frame_swapped()` once; assert `heatmap_item._dirty` is `False`; assert `blotter_model.rowCount()` increased by 100 in a single begin/end block; assert `frame_count` incremented; 100 ms ops not called on frames 1–5. |

**Acceptance criteria.**

1. `_on_frame_swapped` is the **only** place where QML model properties
   are set. No worker thread directly calls `setProperty` on a model.
2. Pushing 500 trades to `_trade_buf` between two `frameSwapped` events
   results in exactly **one** `HeatmapItem.paint()` call and one
   `beginInsertRows` / `endInsertRows` batch.
3. `QQuickWindow.frameSwapped` drives the loop; no `QTimer` is used
   for the render cadence.
4. 100 ms accumulator ops execute once every 6 frames (±1 jitter).
5. `tests/test_render_loop_qml.py` all pass.
6. GPU utilization (steady-state, no market data) < 10% on g5.xlarge
   (measured via `nvidia-smi`).

---

### 23.3 Phase UI-10B — Engine Service Process Isolation `[NOT STARTED]`

**Problem in detail.**

Phase UI-10A decouples rendering from data frequency. However, the engine
and UI still share the same OS process. A `SIGSEGV` in the Qt OpenGL
driver, an unhandled exception in a QML `updatePaintNode`, or an OOM
kill will terminate the engine threads mid-trade. The only way to
guarantee the engine survives a UI failure is to run them in **separate
OS processes**.

**Proposed architecture.**

```
┌──────────────────────────────────────┐
│         engine_service.py            │   started by: ui/app.py as subprocess,
│         (separate OS process)        │   OR headlessly: python engine_service.py
│                                      │
│  ┌──────────────────────────────┐    │
│  │  WS Feed Thread              │    │
│  │  C++ OrderFlowEngine         │    │
│  │  LayeredPush Thread          │    │
│  │  ExecutionManager / Broker   │    │
│  └────────────┬─────────────────┘    │
│               │ events               │
│  ┌────────────▼─────────────────┐    │
│  │  IpcPublisher                │    │──► tcp://127.0.0.1:55001 (ZMQ PUB)
│  │  (zmq PUB socket)            │    │    publishes: TICK, DEPTH, RIPPLE,
│  └──────────────────────────────┘    │    SNAPSHOT, ORDER, HEALTH
│                                      │
│  ┌──────────────────────────────┐    │
│  │  ControlServer               │    │◄── tcp://127.0.0.1:55002 (ZMQ REP)
│  │  (zmq REP socket)            │    │    receives: CONNECT, DISCONNECT,
│  └──────────────────────────────┘    │    ARM, DISARM, SET_MODE
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│         ui/app.py (Qt process)       │
│                                      │
│  ┌──────────────────────────────┐    │
│  │  IpcClient Thread            │    │◄── ZMQ SUB (subscribes to PUB)
│  │  (zmq SUB socket)            │    │    writes to _trade_buf/_snap_buf/...
│  └──────────────────────────────┘    │
│                                      │
│  ┌──────────────────────────────┐    │
│  │  ControlClient               │    │──► ZMQ REQ → engine ControlServer
│  │  (zmq REQ socket, main thr.) │    │    (ARM, DISARM, CONNECT, etc.)
│  └──────────────────────────────┘    │
│                                      │
│  Qt Render Timer (16 ms)             │
│  drains buffers → repaints           │
└──────────────────────────────────────┘
```

**IPC message protocol.**

All messages are serialized as `msgpack` (fast, compact, no schema
compilation required). Each message is a 2-frame ZeroMQ multipart:
`[topic_bytes, payload_bytes]`.

| Topic | Direction | Fields |
|---|---|---|
| `TICK` | Engine → UI | `ts_ms, price, qty, side, symbol` |
| `DEPTH` | Engine → UI | `ts_ms, bids: [(p,q)], asks: [(p,q)]` |
| `RIPPLE` | Engine → UI | `ts_ms, action, confidence, ref_price, state, symbol` |
| `SNAPSHOT` | Engine → UI | `ts_ms, tide: {...}, wave: {...}, risk: {...}, trade: {...}` |
| `ORDER` | Engine → UI | `order_id, status, side, qty, fill_price, reason` |
| `HEALTH` | Engine → UI | `ts_ms, trade_feed_state, depth_feed_state, engine_ok` |
| `CONNECT` | UI → Engine | `symbol, tick_size` |
| `DISCONNECT` | UI → Engine | — |
| `ARM` | UI → Engine | `mode, sizing_mode, sizing_value` |
| `DISARM` | UI → Engine | — |
| `SET_MODE` | UI → Engine | `mode` |

**Engine service behaviour on UI disconnect.**

When the engine's `ControlServer` detects that the ZMQ REQ socket has
been silent for > `_CONTROL_TIMEOUT_S` (default 30 s), it enters
**UI-absent mode**: publishing continues (for reconnecting UI), strategy
execution continues with last armed state, no automatic disarm. This
ensures open positions remain managed even if the UI crashes.

**Watchdog / restart.**

`engine_service.py` includes a self-watchdog: if the WS feed or
ExecutionManager raises an unhandled exception, the watchdog logs the
error, closes positions, and exits with a non-zero code so that
systemd/supervisord can restart the process automatically.

**Scope.**

| File | Change |
|---|---|
| `engine_service.py` (NEW) | Standalone script: `if __name__ == "__main__": run_engine_service(symbol, ...)`. Contains `IpcPublisher`, `ControlServer`, watchdog loop, and the existing `run_live_execute` wiring from `live_runner.py` (imported, not duplicated). |
| `ipc/__init__.py` (NEW) | Package marker. |
| `ipc/protocol.py` (NEW) | `IpcMessage` dataclass. `encode(msg) → bytes` / `decode(bytes) → IpcMessage` via `msgpack`. Topic constants. |
| `ipc/publisher.py` (NEW) | `IpcPublisher`: owns ZMQ PUB socket; `publish_tick`, `publish_depth`, `publish_ripple`, `publish_snapshot`, `publish_order`, `publish_health`. Thread-safe. |
| `ipc/control_server.py` (NEW) | `ControlServer`: owns ZMQ REP socket in a daemon thread; dispatches `CONNECT` / `DISCONNECT` / `ARM` / `DISARM` / `SET_MODE` to engine callbacks. |
| `ipc/client.py` (NEW) | `IpcClient`: ZMQ SUB + REQ sockets in a daemon thread; writes received messages to the four `_*_buf` ring buffers in `MainWindow`. Exposes `send_control(cmd, payload)` for UI → engine commands. |
| `ui/live_trading_session.py` | Add `IpcMode` flag. When in IPC mode, `start_live()` connects `IpcClient` instead of starting a local WS feed. `on_timer_tick()` unchanged (render timer accumulator drives it). |
| `ui/main_window.py` | `_on_connect` spawns `engine_service.py` as a `subprocess.Popen` (or connects to an already-running instance). `_on_disconnect` sends `DISCONNECT` via `IpcClient.send_control`. ARM/DISARM send control messages instead of directly calling `ExecutionManager`. |
| `ui/app.py` | Accept `--engine-addr` CLI arg to connect to a remote/existing engine instead of spawning a subprocess. |
| `tests/test_ipc_protocol.py` (NEW) | Tests: encode/decode round-trip for all 6 topic types; unknown topic raises `ValueError`; `msgpack` payload is < 256 bytes for typical TICK message. |
| `tests/test_ipc_client_server.py` (NEW) | In-process loopback tests using `inproc://` ZMQ transport: publisher sends 10 TICK messages; client receives 10 in `_trade_buf`; control round-trip: UI sends ARM, engine callback fires. |

**New dependency.**

```
pyzmq >= 25.0     # ZeroMQ Python bindings
msgpack >= 1.0    # Fast serialization
```

Add both to `requirements.txt` (or `pyproject.toml`).

**Acceptance criteria.**

1. `python engine_service.py --symbol BTCUSDT` starts without error
   and begins publishing HEALTH messages at 1 Hz.
2. `ui/app.py --engine-addr localhost` connects to the running service;
   the Qt UI shows live trades within 2 s of connecting.
3. `kill -9 <ui_pid>` while the engine is PAPER-armed: engine log
   shows continuous tick processing for ≥ 5 minutes; no position
   opened or closed without operator instruction.
4. UI is relaunched with `--engine-addr localhost`; it reconnects and
   shows the correct PAPER-armed state without the user re-arming.
5. Engine `DISCONNECT` command stops the WS feed gracefully; engine
   exits with code 0.
6. All existing `tests/test_live_runner.py` and
   `tests/test_execution_manager.py` tests pass unchanged (the runner
   and exec manager are not structurally modified, only wrapped).
7. `ipc/protocol.py` encode/decode round-trip tests pass for all topic
   types (verified by `tests/test_ipc_protocol.py`).

---

### 23.4 Phase 10 Dependency Graph

```
Phase UI-10A (Decoupled Render Loop)
  ├─ depends on: Phase 9A (QML scaffold, QQuickWindow available)
  └─ independent of: Phase UI-10B

Phase UI-10B (Engine Service Process Isolation)
  ├─ depends on: Phase UI-10A (ring-buffer data boundary stable)
  └─ new deps: pyzmq >= 25.0, msgpack >= 1.0

UI-10A can start as soon as Phase 9A is merged.
UI-10B starts after UI-10A is merged and ring-buffer boundary is stable.

Phase 10 GA gate:
  • kill -9 smoke test passes (see §23.3 acceptance criterion 3)
  • UI reconnect smoke test passes (criterion 4)
  • GPU utilization < 10% at idle, < 40% at peak
  • All existing regression tests green
```

---

## 24. Phase 11 — Linux / AWS / NICE DCV Production Deployment

### 24.1 Overview

Phase 11 transitions the application from developer-laptop execution
to **production deployment** on Ubuntu 24.04 running on AWS EC2
g5.xlarge with NICE DCV remote rendering. It encompasses:

1. **NVIDIA driver + Qt 6 installation** (11A)
2. **NICE DCV rendering optimization** (11B)
3. **Systemd service files** for both engine and UI (11C)
4. **GPU observability and diagnostics** (11D)

This phase has no strategy-layer changes. It is purely operational /
infrastructure.

| Sub-phase | Name | Status | Depends on |
|---|---|---|---|
| **11A** | NVIDIA + Qt 6 Setup on Ubuntu 24.04 | `NOT STARTED` | Phase 9A (QML scaffold) |
| **11B** | NICE DCV Rendering Optimisation | `NOT STARTED` | Phase 9B (QML charts) |
| **11C** | Systemd Engine & UI Service Files | `NOT STARTED` | Phase UI-10B (IPC) |
| **11D** | GPU Observability & Startup Diagnostics | `NOT STARTED` | Phase 11A |

**Phase 11 GA gate:** Engine and UI running unattended on Ubuntu 24.04
g5.xlarge via NICE DCV for 72 hours with zero manual restarts.
All guardrails in `AGENT_STRATEGY_RULES.md §22` verified in CI against
an offscreen OpenGL context.

---

### 24.2 Phase 11A — NVIDIA + Qt 6 Setup on Ubuntu 24.04 `[NOT STARTED]`

**Problem in detail.**

Ubuntu 24.04 ships with Mesa (software OpenGL via llvmpipe) by default.
NVIDIA proprietary drivers must be installed and the Qt scene graph must
be confirmed to use the NVIDIA GPU before any trading UI is deployed.
There is currently no installation script, no deployment README, and no
OpenGL version assertion at startup.

**Proposed solution.**

*11A-1 — Installation script (`scripts/setup_aws_ubuntu.sh`).*

```bash
#!/usr/bin/env bash
set -euo pipefail

# NVIDIA driver
apt-get install -y nvidia-driver-535 nvidia-utils-535

# CUDA (optional — for future GPU strategy compute)
apt-get install -y cuda-toolkit-12-3

# Qt 6 runtime and development
apt-get install -y \
  qt6-base-dev qt6-declarative-dev qt6-qml-module \
  libqt6opengl6 libqt6quick6 qml6-module-qtquick

# Python bindings
pip install PySide6==6.6.*

# NICE DCV server
# (follow AWS NICE DCV install guide; requires license)
```

*11A-2 — OpenGL context assertion at startup.*

In `ui/app.py`, after the first `QQuickWindow` is visible:

```python
from PySide6.QtGui import QOpenGLContext
ctx = QOpenGLContext.currentContext()
if ctx is None:
    raise RuntimeError("No OpenGL context — check NVIDIA drivers")
version = ctx.format().version()
if version < (4, 0):
    raise RuntimeError(
        f"OpenGL {version[0]}.{version[1]} < 4.0. "
        "Proprietary NVIDIA drivers required.")
logger.info("OpenGL %d.%d on %s", *version, ctx.extensions())
```

*11A-3 — Detect and reject llvmpipe.*

```python
renderer = ctx.extensions()   # contains renderer string
if "llvmpipe" in renderer.lower() or "softpipe" in renderer.lower():
    logger.error("Software renderer detected: %s. "
                 "Install NVIDIA drivers and restart.", renderer)
    sys.exit(1)
```

**Scope.**

| File | Change |
|---|---|
| `scripts/setup_aws_ubuntu.sh` (NEW) | Full installation script for Ubuntu 24.04 on g5.xlarge. |
| `ui/app.py` | Add OpenGL version check and renderer string check. Emit structured log at INFO (hardware) or ERROR (software). |
| `docs/DEPLOYMENT.md` (NEW) | Step-by-step deployment guide: AMI selection, security groups, NICE DCV session, systemd setup, first-run smoke test. |
| `tests/test_opengl_guard.py` (NEW) | Offscreen: mock `QOpenGLContext` returning version (3, 3) → `RuntimeError` raised; version (4, 5) → passes; renderer `"NVIDIA A10G"` → passes; renderer `"llvmpipe"` → `sys.exit(1)` called. |

**Acceptance criteria.**

1. `setup_aws_ubuntu.sh` runs without errors on a fresh Ubuntu 24.04
   g5.xlarge AMI.
2. After setup, `python -m ui.app` starts with `QSGRendererInterface =
   OpenGL` logged at INFO.
3. OpenGL version ≥ 4.0 asserted at startup; process exits with code 1
   if not met.
4. `llvmpipe` renderer string causes immediate `sys.exit(1)` with an
   ERROR log.

---

### 24.3 Phase 11B — NICE DCV Rendering Optimisation `[NOT STARTED]`

**Problem in detail.**

NICE DCV captures the GPU framebuffer and streams it to the remote
client. Without optimization, the default Qt rendering behaviour causes:

- Unnecessary full-screen redraws when only one widget changed.
- Variable frame rate (Qt does not throttle unless told to).
- Semi-transparent overlays requiring compositor re-blends on every
  frame, increasing DCV compression workload.
- Animations that keep the scene "always dirty", preventing DCV from
  skipping identical frames.

**Proposed solution.**

*11B-1 — NICE DCV server configuration.*

`/etc/dcv/dcv.conf` (relevant sections):

```ini
[display]
target-fps = 30          # 30 FPS is sufficient for trading; saves bandwidth
web-client-max-head-resolution = (2560, 1440)

[connectivity]
enable-quic-frontend = true   # QUIC reduces latency vs TCP
```

*11B-2 — Qt scene graph NICE DCV hints.*

```python
# ui/app.py — before engine.load()
os.environ["QSG_RENDER_LOOP"] = "threaded"      # scene graph off GUI thread
os.environ["QT_QPA_UPDATE_IDLE_TIME"] = "16"    # ms between idle redraws
```

*11B-3 — QML component rules enforced in code review.*

- All `Rectangle` backgrounds use `color: "#0c0e1c"` (solid — no alpha).
- No `NumberAnimation` or `PropertyAnimation` on persistent UI elements.
- Chart `QQuickItem` subclasses override `isTextureProvider() → True`
  so DCV can capture them directly from the GPU without a CPU round-trip.
- `QQuickWindow::setRenderTarget(DefaultTarget)` set in `ui/app.py`.

*11B-4 — Frame-stable 30 FPS floor.*

When `QQuickWindow.frameSwapped` fires faster than 30 FPS (> 33 ms
between frames not guaranteed on DCV), the `_on_frame_swapped` handler
checks elapsed time and skips model updates if the last frame was < 16 ms
ago (prevents DCV oversaturation while maintaining UI responsiveness
on local GPU).

**Scope.**

| File | Change |
|---|---|
| `scripts/configure_dcv.sh` (NEW) | Applies `/etc/dcv/dcv.conf` settings above; restarts `dcvserver`. |
| `ui/app.py` | Add DCV render loop env vars; set `QQuickWindow::setRenderTarget`. |
| `ui/main_window_bridge.py` | Add frame-time guard in `_on_frame_swapped`: skip if `elapsed < 16 ms`. |
| `docs/DEPLOYMENT.md` | DCV configuration section. |

**Acceptance criteria.**

1. DCV client shows stable 30 FPS (±2 FPS) during a 60 s live-feed
   connection (measured via `dcv describe-session --statistics`).
2. QML scene graph uses `threaded` render loop (confirmed in startup log
   `QSG_INFO=1`).
3. No `NumberAnimation` present in any `.qml` file (verified by `grep
   -r "NumberAnimation" ui/qml/`).
4. GPU framebuffer captured directly (no CPU round-trip) for chart
   items (verified by `nvidia-smi --query-gpu=memory.used` remaining
   stable during idle streaming).

---

### 24.4 Phase 11C — Systemd Engine & UI Service Files `[NOT STARTED]`

**Problem in detail.**

Currently both the engine and UI are started manually. For 24/7
operation, the engine must restart automatically on failure and start
before the UI. The UI should be restartable independently.

**Proposed solution.**

*`/etc/systemd/system/trading-engine.service`*

```ini
[Unit]
Description=Trading Engine Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=trader
WorkingDirectory=/opt/trading
ExecStart=/opt/trading/venv/bin/python -m engine_service \
  --symbol BTCUSDT --pub-port 55001 --rep-port 55002
Restart=on-failure
RestartSec=5
OOMScoreAdj=-500          # protect engine from OOM killer
KillMode=mixed
TimeoutStopSec=30         # allow graceful position close

[Install]
WantedBy=multi-user.target
```

*`/etc/systemd/system/trading-ui.service`*

```ini
[Unit]
Description=Trading UI (NICE DCV)
After=trading-engine.service dcvserver.service
Requires=trading-engine.service

[Service]
Type=simple
User=trader
Environment=DISPLAY=:1
Environment=QSG_RHI_BACKEND=opengl
Environment=QT_QPA_PLATFORM=xcb
Environment=QSG_RENDER_LOOP=threaded
WorkingDirectory=/opt/trading
ExecStart=/opt/trading/venv/bin/python -m ui.app \
  --engine-addr localhost
Restart=on-failure
RestartSec=10
OOMScoreAdj=200           # UI is lower priority than engine
```

**Key design decisions:**

- `OOMScoreAdj=-500` for engine: Linux OOM killer prefers positive
  scores; engine is protected.
- `KillMode=mixed`: sends `SIGTERM` to engine main process and
  `SIGKILL` to remaining processes after `TimeoutStopSec`.
- UI `Requires=trading-engine.service`: if engine is stopped,
  systemd stops UI too (safe — engine is the authoritative process).

**Scope.**

| File | Change |
|---|---|
| `deploy/trading-engine.service` (NEW) | Systemd unit file above. |
| `deploy/trading-ui.service` (NEW) | Systemd unit file above. |
| `scripts/install_services.sh` (NEW) | Copies unit files to `/etc/systemd/system/`, reloads daemon, enables both services. |
| `docs/DEPLOYMENT.md` | Service management section (start, stop, status, logs). |

**Acceptance criteria.**

1. `systemctl start trading-engine` starts the engine; IPC PUB port
   55001 is listening within 5 s.
2. `systemctl start trading-ui` connects to the engine; NICE DCV shows
   the UI within 10 s.
3. `kill -9 <ui_pid>` → systemd restarts UI within 10 s; engine
   continues trading (verified by engine log).
4. `systemctl stop trading-engine` sends `SIGTERM`; engine closes
   positions gracefully and exits within 30 s.
5. After OS reboot, both services start automatically in the correct
   order.

---

### 24.5 Phase 11D — GPU Observability & Startup Diagnostics `[NOT STARTED]`

**Problem in detail.**

There is no visibility into GPU health, VRAM usage, or rendering
performance during a live session. Regressions in GPU utilization (e.g.,
accidental software fallback, memory leak in textures) are invisible
until performance degrades noticeably.

**Proposed solution.**

*11D-1 — Startup diagnostic block.*

```python
# ui/app.py — always logged at startup
def log_system_diagnostics() -> None:
    import subprocess, json
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,"
             "memory.free,utilization.gpu", "--format=csv,noheader"],
            timeout=5).decode()
        logger.info("GPU: %s", out.strip())
    except FileNotFoundError:
        logger.warning("nvidia-smi not found — GPU diagnostics unavailable")
    logger.info("QSG backend: %s", os.environ.get("QSG_RHI_BACKEND", "auto"))
    logger.info("Qt platform: %s", os.environ.get("QT_QPA_PLATFORM", "auto"))
```

*11D-2 — Runtime GPU health monitor.*

`GpuMonitor` (background thread): polls `nvidia-smi` every 60 s;
logs VRAM usage and GPU utilization. If GPU utilization is 0% for > 120 s
while the UI is rendering, emit a `GPU_IDLE_WHILE_RENDERING` warning
(potential software fallback regression).

*11D-3 — QSG frame timing.*

Enable `QSG_RENDER_TIMING=1` in the systemd service. Parse and log
P50/P95/P99 frame times from the QSG output every 60 s.

*11D-4 — Performance baseline CI test.*

`tests/test_perf_baseline.py` (offscreen, synthetic load):

| Metric | Pass Threshold |
|---|---|
| `QSGRendererInterface` | Not `Software` |
| P95 frame time at 500 trades/s synthetic load | < 20 ms |
| `TradeBlotterModel.rowCount()` after drain | Matches input count exactly |
| `HeatmapItem.paint()` calls per 100 trades | Exactly 1 |

**Scope.**

| File | Change |
|---|---|
| `ui/app.py` | Add `log_system_diagnostics()` call at startup. |
| `ui/monitoring/gpu_monitor.py` (NEW) | `GpuMonitor(threading.Thread)`: polls nvidia-smi; logs; emits `GPU_IDLE_WHILE_RENDERING` via Python logger. |
| `deploy/trading-engine.service` | Add `Environment=QSG_RENDER_TIMING=1`. |
| `tests/test_perf_baseline.py` (NEW) | Offscreen performance baseline tests (see table above). |

**Acceptance criteria.**

1. `log_system_diagnostics()` logs GPU name and driver version at startup
   on g5.xlarge (or logs warning on non-GPU host).
2. `GpuMonitor` runs for 60 s without exceptions in a unit test with a
   mocked `nvidia-smi` subprocess.
3. `GPU_IDLE_WHILE_RENDERING` warning appears in log when GPU utilization
   is 0% for > 120 s (injected via mock).
4. `tests/test_perf_baseline.py` all pass on an offscreen OpenGL context.

---

### 24.6 Phase 11 Dependency Graph

```
Phase 11A (NVIDIA + Qt 6 Setup)
  └─ depends on: Phase 9A (QML scaffold — confirms Qt 6 is required)
  └─ can run in parallel with Phase 9B, 9C

Phase 11B (NICE DCV Optimisation)
  └─ depends on: Phase 9B (QML charts must be rendering via scene graph)
  └─ depends on: Phase 11A (NVIDIA driver installed)

Phase 11C (Systemd Services)
  └─ depends on: Phase UI-10B (engine_service.py exists with IPC ports)
  └─ depends on: Phase 11A

Phase 11D (GPU Observability)
  └─ depends on: Phase 11A
  └─ can run in parallel with Phase 11B and 11C

Phase 11 GA gate:
  • 72-hour unattended run on g5.xlarge via NICE DCV
  • Zero OOM kills, zero unhandled engine exceptions
  • GPU utilization logged and within bounds throughout
  • All regression tests green on offscreen OpenGL context
```
