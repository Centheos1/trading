# Trading App — Backtesting & Optimisation Platform

A multi-layer crypto trading strategy research platform implementing the
**Tide / Wave / Ripple** hierarchy with event-time backtesting, multi-objective
parameter optimisation (NSGA-II), and a C++ order flow engine.

---

## V1 Status — ✅ GA AS OF 2026-05-11

**TL;DR.** All five Phase 14 sub-phases (14A–14E) are done. The live
execute path is now V1-compliant: Ripple-driven (event-time), Tide /
Wave / RV snapshots pushed on 60 s / 5 s / 1 s cadences, every intent
gated by `intent_risk_block_reason`, cross-venue boost factors are
`WaveConfig` parameters (no magic constants), and `num_trades` is a
first-class NSGA-II Pareto axis. See `implementation_plan.md` §7.1 for
the full Phase 14 closure record.

| Mode | V1 Status | Safe to use? |
|---|---|---|
| `python main.py backtest` (orderflow / wave / tide) | ✅ V1-complete | Yes |
| `python main.py optimise` (NSGA-II) | ✅ V1-complete (three-axis Pareto post-14E: cagr, sharpe, num_trades) | Yes |
| `python main.py ui` (live view, paper fills via `paper_fills` flag) | ✅ V1-complete | Yes (paper only) |
| `python main.py execute` (live Binance USD-M futures trading) | ✅ **V1-complete** — Phase 14A (Ripple-driven, event-time cooldown), 14B (Tide budget, Wave snapshot, RV pushed live), 14C (V1 §22.2 #12 contract pinned by 18-test compliance suite — broker provably sees zero orders under ES exhausted / Wave DISABLED / Tide CRISIS / max_position exceeded / two-trades-concurrent / cooldown active), 14D (cross-venue boost factors are `WaveConfig` parameters), 14E (`num_trades` is a Pareto axis) | **TESTNET soak recommended before flipping `BINANCE_TESTNET=false`** — all V1 contract violations and quality gaps are closed; ordinary pre-prod hygiene (broker key rotation, account isolation, position limits) still applies. |
| `tools/replay_harness.py` (deterministic replay verifier) | ✅ V1-complete | Yes |
| `tools/hmm_abtest.py` (Phase 7V HMM A/B harness) | ✅ V2 research tooling | Yes (research only) |

**V1 closure work — all done:**

| Sub-phase | Title | Severity | Status | Est. effort |
|---|---|---|---|---|
| 14A | Live execution driven by Ripple decisions (incl. event-time cooldown) | 🔴 Blocker | ✅ **DONE 2026-05-09** | 2–3 days (actual: ~1 day) |
| 14B | Tide / Wave / Vol snapshot push to live engine | 🔴 Blocker | ✅ **DONE 2026-05-11** | 1–2 days (actual: ~1 day) |
| 14C | Live broker risk-rejection acceptance test | 🔴 Blocker | ✅ **DONE 2026-05-12** | 1 day (actual: ~0.5 day) |
| 14D | Cross-venue boost factors as `WaveConfig` parameters | 🟠 Quality | ✅ **DONE 2026-05-11** | 0.5 day (actual: ~0.25 day) |
| 14E | Optimiser `num_trades` as a Pareto objective | 🟠 Quality | ✅ **DONE 2026-05-11** | 0.5 day (actual: ~0.25 day) |
| 14F | V1 closure tail — deprecated `on_signal` removed, STRAT_PARAMS audit, `_push_layered_strategy` test coverage, V1.1 risk-gate diagnostics + wiring indicator, docs drift | 🟢 Polish | ✅ **DONE 2026-05-12** | 1 day (actual: ~0.75 day) |

V2 / V3 scope (HMM Wave classifier, Hierarchical ES, Multi-symbol,
Multi-factor PCA, Adaptive Kelly sizing, Live model retraining, LIMIT/OCO
broker support) is documented in `implementation_plan.md` §7.2 and is
explicitly out-of-scope for V1.

---

## Architecture Overview

```
main.py                          CLI entry point (data / tide / wave / backtest / optimise / ui / execute)

── Tide Layer (macro bias, risk budgeting) ──────────────────────────────────
tide/
  tide_engine.py                 Deterministic Tide regime classifier + risk multiplier publisher
  tide_features.py               L1-only feature computation (realized vol, η, LSI proxy)
  tide_backtest.py               Event-time Tide-overlay backtester + SizingMode
  tide_metrics.py                Full quantitative performance + risk metrics suite
  tide_accuracy.py               Forward-looking signal accuracy analysis (hit rate, IC, PnL proxy)
  tide_report.py                 Markdown + text report writer, equity/drawdown/signal plots
  tide_optimiser.py              NSGA-II over Tide params (Sharpe, Calmar, MaxDD objectives)
  tide_cli.py                    CLI: backtest / optimise subcommands

── Wave Layer (meso-scale regime classification) ────────────────────────────
wave/
  wave_engine.py                 Deterministic WaveRegime classifier + PermissionSet publisher
  wave_features.py               L1-only Wave feature computation (η, VWAP, structure, D proxy, AR proxy)
  wave_backtest.py               Wave-overlay backtester — isolated and stacked modes
  wave_metrics.py                Wave performance + regime accuracy metrics suite
  wave_accuracy.py               Regime accuracy analysis (hit rate, IC, transition matrix, stability)
  wave_report.py                 Markdown + text report writer, regime timeline + accuracy plots
  wave_optimiser.py              NSGA-II over Wave params including timeframe
  wave_cli.py                    CLI: backtest / optimise subcommands

── Shared ───────────────────────────────────────────────────────────────────
schemas.py                       Canonical data contracts (TideBias, VolRegime, WaveRegime, PermissionSet, …)
METRICS_GLOSSARY.md              Full mathematical + plain-language definitions of every metric

── Legacy Order Flow Engine ─────────────────────────────────────────────────
data_service.py                  Tick data collection (Python WebSocket → C++ engine → HDF5)
backtester.py                    Strategy dispatcher (obv, sma, orderflow, …)
optimiser.py                     Legacy NSGA-II (for legacy strategies)
strategies/orderflow.py          Python wrapper for C++ order flow backtest
ui/                              PySide6 multi-view trading dashboard (heatmap + candle chart + strategy diagnostics)
  ui/main_window.py              Tab wrapper, timer pipeline, MarketState orchestration
  ui/heatmap_widget.py           Order-flow heatmap (depth + bubbles + CVD) — thin View
  ui/orderflow_viewmodel.py      Heatmap viewmodel: depth image, bubbles, percentile normalisation, alpha-fade
  ui/candle_chart_view.py        QPainter candlestick chart with overlay registration
  ui/chart_overlays.py           SMA / EMA / VWAP / structural levels / volume-profile bar
  ui/strategy_dashboard_view.py  Diagnostics + signal log + account + history mini-chart + ripple state table
  ui/market_state.py             Shared model: candles, snapshots, snapshot_history, signals
backtestingCpp/orderflow/        C++ order flow engine (L2 order book, signal generation, TickStore, …)
```

---

## Strategy Architecture — Tide / Wave / Ripple

```
┌──────────────────────────────────────────────────────────────────────┐
│  TIDE  (macro bias, risk budgeting, capital allocation)              │
│  L1 OHLCV only — does NOT trigger trades                             │
│  Output: TideBias (LONG / SHORT / NEUTRAL), risk_multiplier          │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ TideBias, risk_multiplier
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  WAVE  (meso-scale regime classification, structural context)        │
│  L1 OHLCV only — does NOT trigger trades                             │
│  Output: WaveRegime (BREAKOUT / MEAN_REVERSION / BREAKDOWN / NEUTRAL)│
│          PermissionSet gating Ripple archetypes                      │
└───────────────────────────┬──────────────────────────────────────────┘
                            │ WaveRegime, PermissionSet
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  RIPPLE  (microstructure execution — L2 data)                        │
│  Triggers actual trades based on permissions from Tide + Wave        │
└──────────────────────────────────────────────────────────────────────┘
```

Each layer is backtested independently (isolated) and in combination (stacked) to measure incremental contribution.

---

## Deployment & Workflow

The app ships as a **Docker Compose project**. `docker compose up` is the
single entry point for every environment — local development, CI, EC2 data
collection, and (future) GPU trading UI.

### Quick-start: collect tick data on EC2 (≤10 minutes)

```bash
# 1. Launch an EC2 t3.small — Ubuntu 24.04, IAM role with s3:PutObject

# 2. SSH in and clone the repo
git clone https://github.com/Centheos1/trading.git /app && cd /app

# 3. Bootstrap: installs Docker, builds the image, enables auto-start on reboot
bash scripts/setup_ec2.sh

# 4. Set your S3 bucket — the only required config
nano .env          # S3_BUCKET=your-bucket-name

# 5. Start collecting
docker compose up -d

# 6. Watch it
docker compose logs -f collector
```

Data flows to S3 hourly via cron. Download to your dev machine before
running the HMM campaign:

```bash
export S3_BUCKET=your-bucket-name
bash scripts/download_ticks.sh
```

### Compose services

| Command | What starts | Use case |
|---|---|---|
| `docker compose up -d` | `collector` (BTCUSDT) | EC2 data collection |
| `docker compose --profile multi up -d` | + `collector-eth` | Add ETHUSDT |
| `docker compose --profile ui up app` | `app` (trading UI) | Local UI (needs XQuartz) |
| `docker compose --profile test run --rm test` | `test` + exits | CI / regression sweep |

### Local development

```bash
# Option A — Dev container in Cursor (recommended)
# Open project → "Reopen in Container" (detected from .devcontainer/)
# Same Ubuntu 24.04 image as EC2; C++ engine compiled for linux/amd64

# Option B — Native macOS (fastest for UI iteration)
source .venv/bin/activate
python main.py   # choose 'ui' mode — uses Metal GPU directly

# Run tests (either environment)
docker compose --profile test run --rm test          # Docker (Linux parity)
python -m unittest discover -s tests -v              # Native macOS
```

### Update and redeploy (EC2)

```bash
git pull
docker compose build
docker compose up -d
```

### Add a second symbol

```bash
docker compose --profile multi up -d collector-eth   # ETHUSDT via Compose
# OR as a separate systemd-managed container instance:
bash scripts/add_symbol.sh SOLUSDT
```

See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the full step-by-step
guide including IAM policy, S3 setup, S3 sync cron, and troubleshooting.

---

## Prerequisites

### Docker (recommended — matches EC2 exactly)

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) ≥ 4.x
- Copy `.env.template` → `.env` and fill in `S3_BUCKET`

The Docker image (`Dockerfile.dev`) is Ubuntu 24.04 and installs all
C++ and Python dependencies automatically. No Homebrew or manual
`pip install` required.

### Native macOS (local UI / fast iteration)

- **macOS** (tested on Apple Silicon M3 Pro)
- **Python 3.12+** with a virtual environment at `.venv/`
- **Homebrew** packages: `boost`, `hdf5`, `openssl`, `cmake`, `nlohmann-json`, `pybind11`
- **pip** packages: see `requirements.txt`

---

## Setup

### Docker setup (one command)

```bash
cp .env.template .env   # fill in S3_BUCKET if deploying to EC2
docker compose build    # builds Ubuntu 24.04 image + C++ engine (~5 min first run)
```

### Native macOS setup

### 1. Install system dependencies

```bash
brew install cmake boost hdf5 openssl nlohmann-json pybind11
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Build the C++ order flow engine (optional — for legacy strategies)

```bash
cd backtestingCpp/orderflow
chmod +x build.sh
./build.sh
```

### 4. Create required directories

```bash
mkdir -p logs data reports
```

---

## Running the Tide Layer

### Interactive mode

```bash
python main.py
# Mode: tide
# Subcommand: backtest
```

### CLI — single backtest

```bash
python -m tide.tide_cli backtest \
  --symbol BTCUSDT --exchange binance \
  --timeframe 1h \
  --sizing-mode base --max-position-base 1.0 \
  --from-time 2023-01-01
```

### CLI — multi-objective optimisation

```bash
python -m tide.tide_cli optimise \
  --symbol BTCUSDT --exchange binance \
  --timeframes 1h 4h 12h 1d \
  --pop-size 30 --generations 20 \
  --sizing-mode base --max-position-base 1.0
```

### Key output files

| File | Description |
|---|---|
| `reports/tide_BTCUSDT_<ts>.md` | Full backtest report (returns, risk, accuracy, regime breakdown) |
| `reports/tide_BTCUSDT_<ts>_equity.png` | Equity curve vs Buy & Hold |
| `reports/tide_BTCUSDT_<ts>_signal_timeline.png` | LONG/SHORT/NEUTRAL regime overlay on price |
| `reports/tide_BTCUSDT_<ts>_hit_rate.png` | Signal hit rate vs forward horizon |
| `reports/tide_optimise_BTCUSDT_best01.md` | Pareto-optimal parameter set #1 report |

---

## Running the Wave Layer

### Isolated mode (Wave alone, TideBias pinned to NEUTRAL)

```bash
python -m wave.wave_cli backtest \
  --symbol BTCUSDT --exchange binance \
  --timeframe 1h --mode isolated \
  --sizing-mode base --max-position-base 1.0
```

### Stacked mode (Wave ingests Tide signals)

```bash
python -m wave.wave_cli backtest \
  --symbol BTCUSDT --exchange binance \
  --timeframe 1h --mode stacked \
  --sizing-mode base --max-position-base 1.0
```

In stacked mode the backtester automatically runs a Tide backtest inline to
produce `TideBias` and `risk_multiplier` for every bar, then feeds them into
the Wave engine. This mirrors how the layers will operate in production.

### CLI — Wave optimisation (all parameters including timeframe)

```bash
python -m wave.wave_cli optimise \
  --symbol BTCUSDT --exchange binance \
  --mode stacked \
  --timeframes 1h 4h 12h 1d \
  --pop-size 30 --generations 20
```

### Key output files

| File | Description |
|---|---|
| `reports/wave_BTCUSDT_<ts>.md` | Full Wave backtest report |
| `reports/wave_BTCUSDT_<ts>_equity.png` | Equity curve vs Buy & Hold |
| `reports/wave_BTCUSDT_<ts>_wave_timeline.png` | Regime timeline overlay on price |
| `reports/wave_BTCUSDT_<ts>_wave_hit_rate.png` | Regime hit rate vs forward horizon |
| `reports/wave_BTCUSDT_<ts>_wave_fwd_return.png` | Per-regime mean forward return vs horizon |
| `reports/wave_BTCUSDT_<ts>_wave_transition.png` | Regime transition matrix heatmap |
| `reports/wave_BTCUSDT_<ts>_wave_confusion.png` | Regime × Tide bias confusion heatmap |

---

## Interpreting Results

### Tide Layer

The Tide backtest implements a **regime-following overlay strategy**: Tide's
output (`TideBias`, `risk_multiplier`) drives a notional position in the
underlying. A LONG bias at full risk multiplier holds `max_position_base` BTC;
NEUTRAL flattens to zero.

This measures the economic value of Tide's directional signal and risk
management — not Ripple execution quality.

**Signal accuracy tables** in the report answer: "Is the bias at time $t$ correct
for $t+1, t+2, \ldots, t+T$?" using hit rate, IC, and PnL proxy at each
forward horizon.

### Wave Layer — Isolated Mode

In isolated mode TideBias is permanently NEUTRAL, so Wave's position-sizing
proxy is driven by regime alone:

| Regime | Size fraction | Direction logic |
|---|---|---|
| BREAKOUT | 1.0 (full) | Long above VWAP, short below VWAP |
| MEAN_REVERSION | `reduced_size_fraction` (default 0.5) | Fade: short above VWAP, long below |
| BREAKDOWN | 0.0 (flat) | No position — instability, all archetypes off |
| NEUTRAL | `reduced_size_fraction` | Conservative; same direction logic as BREAKOUT |

Use isolated mode to validate the regime classifier independently of Tide.

### Wave Layer — Stacked Mode

In stacked mode Tide provides direction (via `TideBias`) and a `risk_multiplier`
scaling factor. Wave applies an additional `wave_size_fraction` gating:

```
target_units = tide_bias_sign × tide_risk_mult × wave_size_fraction × max_position
```

This measures the incremental value of Wave's regime filter on top of Tide
signals. A Sharpe improvement over the isolated Tide backtest indicates Wave
adds genuine structural context.

### Regime Accuracy

The **regime accuracy report** answers: "Does the Wave regime classification
predict forward returns over the next $h$ bars?"

- **Hit Rate %**: fraction of BREAKOUT/BREAKDOWN bars where the forward return
  aligns with the regime's expected direction (BREAKOUT → positive, BREAKDOWN → negative).
- **IC**: Pearson correlation of regime signal with forward log-returns.
- **Transition Matrix**: probability of transitioning from one regime to another bar-by-bar.
- **Regime Stability**: mean consecutive-bar run length per regime.
- **Regime × Bias Confusion**: in stacked mode, shows mean forward return for
  every `(WaveRegime, TideBias)` combination at $h=1$.

---

## Running Tests

```bash
# Full regression sweep — Docker (Linux/amd64, matches CI and EC2)
docker compose --profile test run --rm test

# Full regression sweep — native macOS
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v

# Targeted suites (either environment)
python -m unittest tests/test_wave_backtest.py -v      # Wave layer (46 tests)
python -m unittest tests/test_tide_accuracy.py -v      # Tide layer
python -m unittest tests/test_collect_ticks.py -v      # Collector CLI (22 tests)

# C++ unit tests (native or inside container)
cd backtestingCpp/orderflow/build
./test_ripple && ./test_schemas && ./test_trade_lifecycle
```

---

## Legacy Order Flow Tests

### Collect tick data

For continuous headless collection (EC2), use `docker compose up -d`
(see the [Deployment & Workflow](#deployment--workflow) section above).

For a quick local test run:

```bash
# Docker (30-second smoke run)
docker compose run --rm collector \
    python collect_ticks.py --symbol BTCUSDT --duration 30

# Native macOS
python collect_ticks.py --symbol BTCUSDT --duration 30

# Legacy interactive prompt (still works)
python main.py
# Mode: data | Exchange: binance | Symbol: BTCUSDT | Pull type: ticks | Duration: 30
```

### Backtest the order flow strategy

```bash
python main.py
# Mode: backtest | Exchange: binance | Symbol: BTCUSDT | Strategy: orderflow | Timeframe: 1m
```

### Optimise (NSGA-II)

```bash
python main.py
# Mode: optimise | Strategy: orderflow | Population: 10 | Generations: 3
```

### Launch the UI

```bash
python main.py
# Mode: ui
```

The UI is a multi-view PySide6 dashboard with three tabs that share a single
`MarketState` model (so candle data, snapshots, and signals are never duplicated):

| Tab | What it shows |
|---|---|
| **Order Flow** | Depth heatmap with dual bid/ask LUTs, gamma-curved intensity, 95th-percentile normalisation, forward-fill alpha fade for stale columns; trade bubbles with volume-cap and dynamic coarsening; CVD overlay |
| **Chart** | QPainter candlestick chart with auto-registered overlays: SMA-20, EMA-50, session VWAP, session H/L + ATH/ATL structural levels, right-edge volume-profile bar with POC highlight |
| **Strategy** | Strategy diagnostics panel, trade blotter (signal log), account panel, rolling history mini-chart (Wave regime + Tide bias band, ES% + uPnL line chart over the last ~5 minutes), and a current Ripple state table (lifecycle, archetype, entry/stop/target, hold time, uPnL, ES used) |

Only the active tab repaints each tick. See [`UI_STRATEGY_INTEGRATION_PLAN.md`](UI_STRATEGY_INTEGRATION_PLAN.md)
for the full architectural breakdown (Phases A–E + 6 + 7).

---

## Remaining Work

### Live Execute Path (Phase 12 + Phase 14A — Ripple-driven)

The outbound order-routing layer mirrors the Phase 10 inbound-feed
hardening. Phase 12 added the unit-test scaffolding the
`PaperEngine` / `ExecutionManager` / `BinanceBroker` layer was missing.
**Phase 14A** (2026-05-09) rewired the live execution topology so that
both paper and live paths consume `RippleDecision` intents (which
already pass through the lifecycle FSM, scaling logic, exit taxonomy,
and `RiskEngine`).

> **Phase 14B (Tide / Wave / Vol wiring) shipped 2026-05-11.** Both
> live entry points (`ui` and `execute`) now push Tide budget on a 60 s
> cadence, Wave snapshot on a 5 s cadence, and realized vol on a 1 s
> cadence into the C++ `RippleEngine` via the three setters
> `set_risk_budget` / `set_wave_snapshot` / `set_realized_vol`. The
> local `RiskEngine` is therefore now armed with real budgets on the
> live path and Wave permissions reflect real regime classification.
>
> **Phase 14C (V1 §22.2 #12 contract acceptance) shipped 2026-05-12.**
> The Python live path now invokes a single-source-of-truth
> `intent_risk_block_reason(intent, engine, current_position_usd=...,
> ofe_module=ofe)` gate inside `_ripple_cb` (headless runner) and
> `_on_ripple_received` (UI) BEFORE forwarding to
> `ExecutionManager.on_intent`. The gate mirrors the C++ engine's
> Wave/Risk gate so the broker provably sees zero orders when
> (i) ES is exhausted, (ii) Wave is DISABLED, (iii) Tide is CRISIS,
> (iv) `max_position_usd` is exceeded, (v) two trades are concurrent,
> or (vi) cooldown is active. Pinned by 18 tests in
> `tests/test_live_execution_v1_compliance.py` (8 end-to-end acceptance,
> 7 unit, 2 wiring), including explicit negative-controls that prove
> exits are NEVER blocked and the happy-path entry does fire.

```
RippleEngine (C++)
  → RippleDecision
    ├─ ripple_decision_to_intent (execution/models.py)
    │    ├─ paper:  PaperEngine.on_intent          ← unchanged from Phase 12
    │    │            → _handle_entry / _handle_exit
    │    │            → order_callback → TradeBlotter / AccountPanel
    │    └─ live:   intent_risk_block_reason       ← Phase 14C (V1 §22.2 #12 gate)
    │                 (Wave permissions, ES budget, Tide risk_multiplier,
    │                  max_position_usd — exits + cancels always pass)
    │                 → ExecutionManager.on_intent ← Phase 14A
    │                     (event-time cooldown via intent.timestamp)
    │                     → asyncio worker loop
    │                       → BinanceBroker.place_order (REST, MARKET)
    │                         → _poll_order_fill until FILLED
    └─ recorder.record_ripple_decision (sidecar — Phase 13)

SignalEngine (C++)
  → Signal
    └─ recorder.record_signal (sidecar — Phase 13, observation only)
       NB: signals NO LONGER drive ExecutionManager post-14A.
       The deprecated ExecutionManager.on_signal entry point logs a
       one-time WARNING if any caller still routes signals through it.
```

Phase 12 added ≈ 83 new offline tests across four suites (no real
network, no real `binance.AsyncClient`, no Qt):

- [`tests/test_paper_engine.py`](tests/test_paper_engine.py) — 26
  checks for `PaperEngine`: construction defaults, every `on_intent`
  branch (entry / exit / cancel / rearm / unknown), suppression rules
  (`INVENTORY` for missing side / missing price / zero-rounded
  quantity / same-side re-entry; `NO_POSITION` for stray exits),
  realized-PnL signing on long and short flips, all three
  `SizingMode`s, `unrealized_pnl()`, `update_sizing()` mid-flight,
  `reset()`, and order-callback safety.
- [`tests/test_execution_manager.py`](tests/test_execution_manager.py)
  — 25 checks for `ExecutionManager` driven through a
  `StubBroker(BrokerInterface)`: lifecycle (start/stop/idempotent),
  arm/disarm with optional close, cooldown + disarmed + same-side
  gates on `on_signal`, `_execute_signal` happy-path / reverse /
  close-not-filled, `_compute_quantity` for every `SizingMode`
  including `MIN_NOTIONAL` floor, `step_size` rounding, and
  `max_position` clamp, and `_periodic_refresh` resilience to
  consecutive `get_account_info` failures.
- [`tests/test_binance_broker.py`](tests/test_binance_broker.py) —
  19 checks for `BinanceBroker` with `binance.AsyncClient.create`
  patched and `load_dotenv` neutered: connect (testnet vs. live vs.
  failure), `disconnect`, `get_account_info` parsing + filtering,
  `place_order` rounding-zero rejection / happy path / `SUBMITTED`
  + 0-fill polling / exchange-exception rejection, `cancel_order`,
  `get_open_orders`, `get_position`, `close_position` flip,
  `_round_quantity` precision, and `_map_status` over all six Binance
  statuses.
- [`tests/test_execution_models.py`](tests/test_execution_models.py)
  — 13 checks pinning `_parse_intent_name`, every
  `_RIPPLE_INTENT_MAP` entry's round-trip through
  `ripple_decision_to_entry` / `_to_intent`, `NO_ACTION` and
  unknown-intent short-circuits, the explicit-`intent_name` argument
  override, and the dataclass defaults the rest of the execution layer
  depends on.

Combined runtime of the four new suites is under 1 second, well below
the 5-second budget. No production-code patches were required — the
audit pass during test writing did not surface any defect.

**V1 closure work (Phase 14 — see `implementation_plan.md` §7.1):**
- ✅ **14A — DONE 2026-05-09.** Replaced `set_signal_callback(on_signal)`
  with `set_ripple_callback(...)` → `ripple_decision_to_intent` →
  `ExecutionManager.on_intent`. Wall-clock cooldown moved to event-time
  (`intent.timestamp` → `_last_intent_ts_ms`). 23 new tests in
  `tests/test_execution_manager.py`, 13 new tests in
  `tests/test_live_runner.py`. Both grep gates pass.
- ✅ **14B — DONE 2026-05-11.** UI live session and headless
  `run_live_execute` now push `TideSnapshot` (60 s), `WaveSnapshot` (5 s),
  and realized vol (1 s) into the C++ engine via the three
  `RippleEngine` setters. WS trade callback feeds `WaveEngine.on_price`
  and a rolling RV buffer. 24 new tests in
  `tests/test_layered_live_wiring.py`. All three grep gates pass with
  multiple production hits each.
- ✅ **14C — DONE 2026-05-12.** End-to-end V1 §22.2 #12 contract pinned
  by `tests/test_live_execution_v1_compliance.py` (18 tests).
  `intent_risk_block_reason` re-applies the C++ Wave/Risk gate on the
  Python side from inside `execution/live_runner.py:_ripple_cb` and
  `ui/main_window.py:_on_ripple_received` so a real broker provably
  receives zero orders under all six failure modes (ES exhausted,
  Wave DISABLED, Tide CRISIS, max_position exceeded, two-trades
  concurrent, cooldown active). Exits + happy-path negative-controls
  pin no over-suppression.
- ✅ **14D — DONE 2026-05-11.** Cross-venue divergence + correlation
  boost factors are now `WaveConfig.crossvenue_divergence_boost` /
  `crossvenue_correlation_boost` parameters (previously hardcoded
  `2.0` / `0.5`). Two new tests in `test_crossvenue_wave.py`.
- ✅ **14E — DONE 2026-05-11.** `num_trades` is a third Pareto axis in
  NSGA-II (`crowding_distance` iterates it; `non_dominated_sorting`
  treats it as "higher is better" alongside `cagr` and `sharpe`).
  Closes `strategy.md` §22.2 #15. Both `# TODO add num_trades` markers
  removed from `optimiser.py`; 4 new tests in `test_optimiser.py`.
- ✅ **14F — DONE 2026-05-12.** V1 closure tail:
  - Deleted the deprecated `ExecutionManager.on_signal` /
    `_execute_signal` surface and the `import time` it required.
    Wall-clock reads on the decision path are now impossible by
    construction (the `time` module is no longer in
    `execution/execution_manager.py`'s namespace).
  - Added `tests/test_strat_params_audit.py` (7 tests) to pin the
    Wave/Tide `STRAT_PARAMS` ↔ dataclass-field contract (Phase 13Y
    parity follow-up).
  - Added 12 new tests for `LiveTradingSession._push_layered_strategy`
    pinning RV/Wave/Tide cadences, snapshot translation, counter
    stall on `get_ripple()` failure, and counter parity with the
    headless `execution.live_runner._layered_push_step` (closes the
    Phase 10 "untested `on_timer_tick`" known limitation).
  - V1.1 diagnostics: the strategy dashboard now surfaces the latest
    risk-gate block reason + age + session count (`_RiskGateStatusBar`)
    and three `Tide ● / Wave ● / RV ●` indicators that flip to
    `● live` on first successful push (`_LayeredWiringIndicator`).
    16 new dashboard tests.
  - Documentation drift sweep across `implementation_plan.md` /
    `UI_STRATEGY_INTEGRATION_PLAN.md`.

**Deferred to V2:**
- LIMIT / OCO order support and partial-fill bookkeeping
  enhancements.
- Multi-symbol routing.
- Server-side risk policy (Binance risk limits) — Phase 14C only
  enforces the *local* `RiskEngine`.

### Wave Parameter Tuning & Calibration (Phase 9 — Backtest Hardening)
Wave thresholds (`eta_bo_threshold`, `dispersion_critical`, etc.) are calibrated
against the **bar-count distribution** of the chosen timeframe. A parameter set
tuned for 1h bars sees windows ~12× narrower (in wall-clock terms) when applied
at 5m, which shifts every feature distribution and breaks the calibration.

Phase 9 ships three CLI flags to address this on the backtester:

- `--rescale-windows` — opt into automatic scaling of `eta_window`,
  `vwap_window`, `structure_window`, `disp_window`, `ar_window` whenever
  `--timeframe` is changed. Recommended whenever a parameter set tuned at one
  timeframe is applied at another.
- `--liquidation-equity-frac F` — equity floor as a fraction of
  `initial_capital`; once equity drops to or below the floor, position is
  forced to 0 and trading halts (mirrors a margin-call event). Defaults to
  `0.0` (disabled) so existing parameter sets reproduce V1 behaviour.
- `--slippage-bps B` (and `--slippage-per-unit-bps`) — linear plus quadratic
  slippage charged alongside fees. Use `~1-2 bps` for liquid majors as a
  realistic starting point. Defaults to `0.0`.

A validated 5m run with `--liquidation-equity-frac 0.5 --slippage-bps 2.0` is
saved at [`reports/wave_BTCUSDT_5m_hardened.md`](reports/wave_BTCUSDT_5m_hardened.md)
(Sharpe ≈ 5.87, MaxDD −4.4%, monthly returns bounded in [+0.6%, +22.4%], no
liquidation triggered).

The compute path also fixes the monthly-return math: when a prior month's
equity drops below 10% of `initial_capital`, returns are normalised by initial
capital instead of `pct_change` (which previously produced `+13717%` months on
runs that crossed zero). The report flags `monthly_returns_normalised` whenever
the fallback was used.

New per-regime trade aggregates (`trades`, `fees_paid`, `turnover_usd`), a
`flips_per_bar` chop diagnostic, and a regime run-length histogram are surfaced
in the markdown report so you can spot churn at a glance.

### Multi-Asset Dispersion & Absorption Ratio (V2/V3)
V1 uses single-symbol proxies for dispersion (rolling return std) and absorption
ratio (vol-of-vol ratio). True cross-sectional metrics require a panel of
correlated assets — planned for V2/V3 per `strategy.md` roadmap.

### UI Live Path (Phase 10 — Hardened)
The UI live path is fully Python-based and now has regression coverage:

```
main_window._on_connect()
  → LiveTradingSession.start_live()
    → threading.Thread(_run_ws_feed)
      → run_binance_usdm_futures_ws_feed (asyncio)
        → _run_trade_stream / _run_depth_stream / _watchdog
    → fetch_and_build_depth_snapshot (REST)
  → LiveTradingSession.on_timer_tick → _resync_depth_snapshot
    after _BOOK_EMPTY_RESYNC_THRESHOLD empty ticks
```

Phase 10 added 40 new tests across three suites:
- [`tests/test_stream_health.py`](tests/test_stream_health.py) — 19 tests for the
  `FeedStreamHealth` state machine (`DISCONNECTED → CONNECTING → LIVE → STALE
  → RECONNECTING → FAILED` transitions, `consecutive_failures` threshold,
  `short_status` rendering).
- [`tests/test_binance_futures_ws.py`](tests/test_binance_futures_ws.py) — 11
  mocked-WS integration tests for `run_binance_usdm_futures_ws_feed`
  covering trade/depth parse + dispatch, dedupe, parse-error tolerance,
  `ConnectionClosed` reconnect, `FEED_MAX_CONSECUTIVE_FAILURES` promotion,
  watchdog stale detection, exponential backoff cap, and `stop_event` shutdown
  within 3 seconds.
- [`tests/test_live_trading_session.py`](tests/test_live_trading_session.py) —
  10 orchestration tests for `LiveTradingSession` (engine construction, WS
  thread lifecycle, buffer drain, depth resync error handling).

### Phase 10B — `run_live` CLI Migration & C++ Feed Removal `[COMPLETED]`

`strategies/orderflow.py::run_live()` now uses the same
`run_binance_usdm_futures_ws_feed` runner as the UI and returns a
`LiveSession` handle (`engine`, `stop()`, per-stream
`FeedStreamHealth`, context-manager support). The legacy C++
`BinanceWsFeed` class has been removed end-to-end — header,
implementation, pybind11 binding, and CMake source-list entry — so
the only remaining C++ feed is `ReplayFeed` (deterministic backtest
path). 14 new checks in
[`tests/test_run_live.py`](tests/test_run_live.py) cover the new
function: missing-`ofe` / missing-`websockets` errors, lifecycle,
trade/depth dispatch, signal-callback wiring, REST-snapshot success /
failure / disabled, tick-store registration, and dispatch-exception
isolation.

### Phase 10C — `main.py:execute` Mode Migration `[COMPLETED]`

The `execute` CLI mode (live broker submission via
`ExecutionManager` + `BinanceBroker`) was the third Binance USD-M
consumer in the codebase still rolling its own ad-hoc asyncio WS
plumbing — no `FeedStreamHealth`, no watchdog, no exponential
reconnect, no trade-ID dedupe. Phase 10C extracts the live-trading
loop into a testable
[`execution/live_runner.py`](execution/live_runner.py) module
(`run_live_execute(...)`) routed through the shared
`run_binance_usdm_futures_ws_feed` runner, and trims the
`main.py:execute` block from ~170 lines to ~70 (just user-input
prompts + broker / `ExecutionManager` construction + delegation to
`run_live_execute`). 9 new checks in
[`tests/test_live_runner.py`](tests/test_live_runner.py) cover engine
configuration, signal-callback wiring, WS trade/depth dispatch, REST
snapshot success / failure, status-loop cadence + exception
isolation, the shutdown disarm contract, and the default status
printer. The shutdown sequence
(`disarm(close_position=True)` → grace → `exec_mgr.stop()` →
`engine.stop()`) is preserved bit-for-bit; the live status log line
now reports `trade` and `depth` `FeedStreamHealth` short-status
alongside the existing position / order count.

### Deterministic Replay Harness (Phase 13 — Hardened)

Every captured live session can now be replayed through a fresh
`OrderFlowEngine` + `PaperEngine` and verified to produce
**byte-identical** Tide / Wave / Ripple decisions and paper-engine
fills. The C++ engine and `PaperEngine` are deterministic state
machines (no wall-clock, no randomness) — Phase 13 adds the missing
**capture sidecar** + **replay verifier** around them.

```
LIVE   ──► run_live / run_live_execute (recorder=…)
            ├─ TickStore.h5  (trades + depth — already there)
            └─ session.jsonl (signals + ripples + paper orders + EngineConfig)
                                                │
                                                ▼
REPLAY ──► python main.py replay
            ├─ load EngineConfig + SizingConfig from sidecar
            ├─ fresh OrderFlowEngine
            ├─ fresh PaperEngine (RippleDecision → ExecutionIntent → fill)
            ├─ replay events from TickStore (trade + depth, sorted)
            └─ diff captured stream vs. sidecar → ReplayReport
                                                  ↳ PASS  (exit 0)
                                                  ↳ FAIL  (exit 2 + first divergences)
```

`tools/session_recorder.py` writes a schema-versioned JSONL trace
(header / signal / ripple / order / footer). `NO_ACTION` ripple
decisions are filtered by default to keep traces small; flip
`record_no_action=True` for full-fidelity captures. The
`SessionRecorder` chains onto a paper-engine `order_callback` via
`wrap_paper_callback(...)` and attaches signal + ripple callbacks via
`attach_to_engine(engine)`. `apply_engine_config` and
`apply_sizing_config` rebuild the configs at replay time, including
the full `RippleConfig` + `LifecycleConfig` parameter set.

`tools/replay_harness.py::replay_session(...)` returns a
`ReplayReport` with expected/actual counts and a list of `Divergence`
records pinpointing the first mismatched event. The harness
intentionally **bypasses** `ExecutionManager` (its cooldown gate uses
`time.time()`, breaking determinism) and routes
`RippleDecision → ExecutionIntent → PaperEngine.on_intent` directly
through `execution.models.ripple_decision_to_intent`.

Notes uncovered while building Phase 13:
- The harness loads events directly via
  `TickStore.load_trades` / `load_depth_snapshots` /
  `load_depth_updates` rather than driving `ReplayFeed.run_sync()`.
  The existing path in `strategies/orderflow.py:backtest()` calls
  `engine.start()` (which spawns a background `ReplayFeed`
  thread) **and** `replay.run_sync()` (which runs the same loop on
  the main thread), producing 2× event output. Documented as a
  known limitation; the harness sidesteps it.
- pybind11's default vector binding for `DepthUpdate.bids` /
  `DepthUpdate.asks` returns a copy on read, so per-element
  `.append()` is a no-op. End-to-end tests use whole-list assignment
  (`d.bids = [...]`).
- Phase 13 also fixes a one-arg `engine.set_tick_store(store)` bug
  in Phase 10B's `run_live` — the C++ binding requires
  `(store, symbol)`. The Phase 10B test stub had a matching one-arg
  method, masking the bug.

`tests/test_replay_harness.py` exercises 34 checks across schema,
serialization round-trips, sidecar load error paths, divergence
detection (length / field / extra-event), and end-to-end
capture-then-replay with the real C++ engine + HDF5 `TickStore` +
`PaperEngine` — including a corrupted-sidecar regression that
verifies divergence is reported. Combined runtime ≈ 0.09 s.

Deferred to a separate phase:
- `LiveTradingSession` orchestration replay (UI timer-tick
  scheduling + candle aggregation). Phase 13B candidate.
- `BinanceBroker` round-trip — paper-only is the verification target
  here; real-broker reconciliation belongs in Phase 14.

### Phase 13X — Backtest Double-Fire Fix `[COMPLETED]`

**⚠ Behaviour change:** All backtest reports generated before this
fix processed every trade and depth update **twice** (a Phase 13X
probe measured an exact 2× volume profile / CVD count). Absolute
values from prior `reports/wave_optimise_*` and
`reports/tide_optimise_*` outputs cannot be directly compared to
post-fix runs. Re-run optimisations to get correct absolute numbers.

`strategies/orderflow.py:backtest()` was calling
`engine.start(symbol)` (which spawned a `ReplayFeed` worker thread)
**and** `replay.run_sync()` (which ran the same loop synchronously
on the main thread). Phase 13 surfaced the bug while building the
deterministic-replay harness; the harness already side-stepped it by
loading events directly from `TickStore`. Phase 13X applies the
proper fix to the production `backtest()` path.

The corrected pattern is:

```python
ofe.connect_feed(engine, replay)
engine.start(symbol)              # spawns the replay worker thread
while not replay.is_complete():    # main thread waits, no double-fire
    time.sleep(0.05)
```

A 10-minute safety timeout prevents an indefinite hang if the worker
thread wedges.

Phase 13X also fixed
[`tests/test_replay_determinism.py`](tests/test_replay_determinism.py)`::_make_depth`
which used `d.bids.append(...)` / `d.asks.append(...)`. Under
pybind11's default vector binding `DepthUpdate.bids` returns a copy
on read, so per-element `.append()` modifies a temporary list — the
helper was producing empty depth updates and the suite was silently
asserting "two empty engines produce identical empty snapshots."
Production paths (`data_feed/binance_futures_ws.py`,
`data_feed/binance_depth_rest.py`, `data_service.py`) all use
whole-list assignment (`u.bids = bids_list`) and were never
affected.

New regression coverage:
- [`tests/test_orderflow_backtest.py`](tests/test_orderflow_backtest.py)
  — 4 checks: volume profile total matches expected, CVD history
  count matches trade count, repeated runs are independent, and a
  full `strategies.orderflow.backtest()` smoke test against a
  chdir-staged synthetic tick store.
- [`tests/test_replay_determinism.py`](tests/test_replay_determinism.py)
  — added `test_depth_helper_actually_populates_levels` (pins
  populated-list shape) and `test_engine_actually_observes_depth`
  (drives 50 events and asserts the OrderBook ends with non-zero
  `best_bid` / `best_ask` and a positive spread).

Validation:
- Direct probe: 10 trades × 1.0 qty → VolumeProfile total = **10.0**
  (was 20.0 pre-fix); CVD history len = **10** (was 20).
- Targeted regression sweep across Phase 7 / 9 / 10 / 10B / 10C /
  11 / 11B / 11C / 12 / 13 suites: **377/378 OK**. The single
  remaining failure (`test_crossvenue_wave::test_low_correlation_boosts_breakdown`)
  is a pre-existing `WaveRegime.BREAKDOWN` issue confirmed identical
  on a clean checkout.

Out of scope (deferred):
- Re-running historical optimisation reports under the corrected
  pipeline.
- A proper pybind11 fix for the `DepthUpdate.bids` / `.asks`
  by-copy footgun (would require `PYBIND11_MAKE_OPAQUE` +
  `py::bind_vector`; documented in the codebase as a known trap).
- Pre-existing `WaveRegime.BREAKDOWN` regime cleanup
  (`test_wave_engine` and `test_crossvenue_wave` together: ~7
  failures).

### Phase 13Y — Orderflow Binding Drift Fix `[COMPLETED]`

`python main.py backtest` (or `optimise`) with strategy `orderflow`
crashed mid-config with
`AttributeError: 'orderflow_engine.RippleConfig' object has no
attribute 'idle_exit_threshold'`. The parameter was advertised as
tunable in `STRAT_PARAMS["orderflow"]` (`utils.py:57`) and mapped via
`_RIPPLE_MAP` in `strategies/orderflow.py`, but the underlying C++
field (`RippleConfig.h:130`, used by `ScoreBasedInference.cpp:54` to
gate leaving the IDLE state) was never exposed in
`backtestingCpp/orderflow/bindings.cpp`.

Fix:
1. Added the missing `def_readwrite("idle_exit_threshold",
   &RippleConfig::idle_exit_threshold)` in `bindings.cpp` and rebuilt
   via `bash backtestingCpp/orderflow/build.sh`.
2. Promoted `_RIPPLE_MAP` and `_LIFECYCLE_KEYS` from local-vars-inside-
   `_build_config` to module-level constants so the contract is
   testable.
3. Wrapped both setter loops in `hasattr(...)` guards: a missing C++
   attribute now logs `WARNING` and skips, rather than crashing the
   entire backtest.

New regression coverage in
[`tests/test_orderflow_backtest.py`](tests/test_orderflow_backtest.py)
(`TestBuildConfigBindingDrift`, 5 checks):
- `test_every_ripple_map_key_exists_on_RippleConfig` — pins every
  value in `_RIPPLE_MAP` to a real `RippleConfig` attribute.
- `test_every_lifecycle_key_exists_on_LifecycleConfig` — same for
  the lifecycle keys.
- `test_strat_params_orderflow_keys_round_trip_through_build` —
  feeds every `STRAT_PARAMS["orderflow"]` tunable through
  `_build_config` to confirm none raise.
- `test_idle_exit_threshold_round_trips` — explicitly verifies the
  previously-broken param is applied end-to-end.
- `test_unknown_param_does_not_crash` — phantom `_RIPPLE_MAP` entry
  triggers the defensive guard without raising.

Validation:
- `RippleConfig().idle_exit_threshold` reads back the C++ default
  (`0.30`); set + get round-trips correctly.
- The user's exact failing CLI prompt sequence (`backtest → binance
  → BTCUSDT → orderflow → 1h → ` + all 22 param values) now returns
  `(pnl=3.236, max_dd=0.00679, num_trades=17, sharpe=0.269,
  cagr=0.0565)` instead of `AttributeError`.
- `tests/test_orderflow_backtest.py`: 9/9 OK (4 Phase 13X + 5 new
  Phase 13Y).

### Phase 13Z — `DepthUpdate.bids`/`.asks` Opaque-Vector Decision `[DOCUMENTED]`

The Phase 13X "known limitation" about pybind11's by-copy semantics
for `DepthUpdate.bids` / `.asks` was reviewed and **deliberately
deferred**. Rationale captured in
[`backtestingCpp/orderflow/bindings.cpp`](backtestingCpp/orderflow/bindings.cpp)
above the `DepthUpdate` class binding:

1. The "fix" — `PYBIND11_MAKE_OPAQUE(std::vector<DepthLevel>)` plus
   `py::bind_vector` — removes pybind11's implicit conversion from
   Python `list` to `std::vector<DepthLevel>`. Every existing
   assignment of the form `update.bids = python_list` would break
   without a hand-written `py::implicitly_convertible` shim, and the
   wrapped vector type leaks into Python repr / error messages.
2. Every production caller (`data_feed/binance_futures_ws.py`,
   `data_feed/binance_depth_rest.py`, `data_service.py`) and every
   test already uses the safe whole-list-assignment pattern.
3. Two regression test suites pin the safe pattern:
   `test_replay_determinism.py::test_*_actually_*` (Phase 13X) and
   `test_orderflow_backtest.py::TestBuildConfigBindingDrift`
   (Phase 13Y).
4. Reverting the decision later is mechanical (one C++ change + one
   Python migration sweep) — no schema change.

The comment block in `bindings.cpp` warns future contributors before
they reach for `.append()`.

### Phase 13W — Wave BREAKDOWN Regime Test Cleanup `[COMPLETED]`

Resolved the 7 long-standing `WaveRegime.BREAKDOWN` test failures
explicitly punted from Phase 13X. Root cause: the tests pre-date a
deliberate hardening of `WaveEngine._classify_regime`
([`wave/wave_engine.py:407`](wave/wave_engine.py)) that requires
**multi-factor stress** (`extreme_stress = ar > ar_critical AND
d > dispersion_threshold`) to trigger BREAKDOWN. The tests set only
AR and left dispersion at 0.0, so the predicate evaluated to
`True AND False = False` and the state machine never left NEUTRAL.

The hardening is correct as-shipped — its docstring explicitly
states the design intent: *"BOTH AR and dispersion must be elevated
so AR alone (which can sit ~0.9 in normal markets) does not trigger
BREAKDOWN during an orderly bull run."* The tests were out-of-sync.

Fix:
- Added `set_dispersion(...)` calls alongside `set_absorption_ratio(...)`
  in 6 tests in [`tests/test_wave_engine.py`](tests/test_wave_engine.py)
  (`test_neutral_to_breakdown_via_ar`, `test_breakdown_recovery`,
  `test_breakdown_stays_if_ar_still_high`, `test_breakout_to_breakdown`,
  `test_mean_reversion_to_breakdown`, `test_ar_just_above_critical`)
  and 1 test in [`tests/test_crossvenue_wave.py`](tests/test_crossvenue_wave.py)
  (`test_low_correlation_boosts_breakdown`).
- Inline comments reference `wave_engine.py:_classify_regime` so the
  same drift cannot recur silently.
- **No engine code change** — `wave/wave_engine.py` is untouched.

Validation:
- `tests/test_wave_engine.py`: 65/65 OK (was 60/65).
- `tests/test_crossvenue_wave.py`: 16/16 OK (was 15/16).
- Aggregate non-Qt regression sweep: 546/546 OK.

### Phase 13B — `LiveTradingSession` Orchestration Replay (MVP) `[COMPLETED]`

Extended the Phase 13 deterministic-replay harness to cover
`LiveTradingSession.on_timer_tick` orchestration state — drained-trade
counts, book-empty / book-crossed flags, depth-resync triggers,
trade-buffer backlog. Until this phase, the sidecar trace captured
what the C++ engine did but not what the Python UI orchestration
loop did with it.

New schema event:

```jsonc
{"event":"session_tick", "tick_n":42, "drained":17,
 "best_bid":42000.5, "best_ask":42001.0,
 "bid_count":20, "ask_count":20,
 "book_empty":false, "book_crossed":false,
 "book_empty_ticks":0, "book_resync_pending":false,
 "book_resync_count":0, "trade_buf_remaining":3}
```

Recording API (in [`tools/session_recorder.py`](tools/session_recorder.py)):

```python
recorder = SessionRecorder("session.jsonl")
recorder.write_header(symbol="BTCUSDT", engine_config=cfg)
recorder.record_session_tick(tick_n=42, drained=17,
                             best_bid=42000.5, best_ask=42001.0)
```

`LiveTradingSession.attach_recorder(recorder)` wires it onto the live
UI (call after `start_live(...)`, detach with `attach_recorder(None)`
before `stop_live(...)`). The recorder hook is wrapped in
`try/except` so a faulty recorder cannot break the live tick loop.

Verification API (in [`tools/replay_harness.py`](tools/replay_harness.py)):

```python
from tools.session_recorder import load_sidecar
from tools.replay_harness import verify_session_ticks
trace_a = load_sidecar("captured.jsonl")
trace_b = load_sidecar("replayed.jsonl")
report = verify_session_ticks(trace_a.session_ticks,
                              trace_b.session_ticks)
print(report.summary())
```

`SessionTickReport` reuses the same `Divergence` structure as
`replay_session`, so length, field, and book-state mismatches all
surface with the same diff semantics.

Test coverage in
[`tests/test_session_tick_replay.py`](tests/test_session_tick_replay.py)
— 18 offline tests across 4 classes (no Qt, no real C++ engine):
- `TestRecordSessionTickSchema` (5) — emitted-line shape, defaults,
  pre-header drop, counter increment, footer round-trip.
- `TestLoadSidecarSessionTicks` (1) — `SidecarTrace.session_ticks`
  populates correctly and preserves field types.
- `TestVerifySessionTicks` (6) — identical / length-mismatch /
  field-mismatch / book-state-flip / max-divergences cap /
  float-tolerance pass-through.
- `TestLiveTradingSessionRecorderHook` (6) — no-recorder, every-tick
  call, book state propagation, book-empty flag, recorder-exception
  isolation, attach-then-detach.

Deferred to Phase 13C:
- A full Qt-mocked replay-against-recorded-tick driver. The MVP
  captures the deterministic scalars sufficient to detect timer-cadence
  / drain / book-state regressions at the per-tick boundary — drift
  *inside* a single `on_timer_tick` invocation (e.g. reordering of
  heatmap vs. CVD updates) is out of scope.

### Phase 7V — HMM vs. Rule-Based Backtest A/B Validation `[COMPLETED]`

Closes the explicit validation gap left open at the end of Phase 7 —
the comparison infrastructure (`set_hmm_backend` / `set_score_backend`
on `RippleEngine`) shipped in mid-2025 but the actual A/B validation
run was never executed. Phase 7V delivers the harness, runs it, and
commits the first real baseline.

The harness:

1. Runs an `orderflow` backtest against a `TickStore` with the default
   rule-based `ScoreBasedInference` backend, capturing every
   `RippleDecision` along with the corresponding 6-D evidence vector
   (via `ripple.last_evidence()`) and the `triggering_state`
   `RippleState` integer.
2. Trains a Gaussian-emission HMM on the captured evidence sequence
   via `hmm.HMMTrainer.select_model(k_range=[3, 4, 5, 6])`, picks K by
   BIC argmin, and derives the `state_map` (HMM hidden state →
   `RippleState` int) by majority vote against the rule-based labels.
3. Saves the trained model JSON to `models/hmm_<symbol>_<label>_<ts>.json`.
4. Re-runs the same backtest with `params["hmm_enabled"]=True` +
   `params["hmm_model_path"]=<saved file>`, capturing the same metrics.
5. Writes a Markdown + JSON A/B report to `reports/`.

CLI:

```bash
cd /Users/clintsellen/Documents/Trading/app/backtest
python -m tools.hmm_abtest \
  --symbol BTCUSDT --exchange binance \
  --from-time 2026-03-07 --to-time 2026-03-09 \
  --label phase7v_smoke --k-range 3,4,5 --seed 0
```

Files:

- `hmm/abtest.py` — pure-Python helpers: `derive_state_map`,
  `compare_metrics`, `summarize_winner`, `format_comparison_report`,
  `format_comparison_json`, `AbtestSummary`.
- `tools/hmm_abtest.py` — CLI orchestrator.
- `tests/test_hmm_abtest.py` — 38 new offline tests; full coverage of
  the helpers + a stub-runner integration that exercises
  `run_abtest()` end-to-end without the C++ engine.
- `reports/hmm_abtest_BTCUSDT_phase7v_baseline.md` /
  `.json` — the canonical first real run captured against
  `data/binance_ticks.h5` (BTCUSDT, 2026-03-07 → 2026-03-09).
- `models/hmm_BTCUSDT_phase7v_smoke_*.json` — first trained HMM model.

First-run result (BTCUSDT, 2-day window, default post-13Y params):

| Metric | Rule-based | HMM (K=3) | Winner |
|---|---:|---:|---|
| `pnl` | 0.000000 | 3.336452 | **hmm** |
| `max_drawdown` | 0.000000 | 0.000151 | **score** |
| `num_trades` | 1 | 16 | informational |
| `sharpe_ratio` | 0.289721 | 0.289721 | **tie** |
| `cagr` | 5337.121322 | 5337.121322 | **tie** |

**Verdict:** *Mixed: HMM wins 1, rule-based wins 1, ties 2.* Sharpe and
CAGR tie because both runs share the same `SignalEngine`; only the
Ripple inference backend differs. The CAGR figure is a mechanical
annualization of a tiny return over 2 days — not a meaningful
multi-year projection. For a credible promotion decision, re-run
against ≥30-day windows on multiple symbols.

Validation:

- `python -m unittest tests.test_hmm_abtest -v` — 38/38 pass in 0.40 s.
- `python -m tools.hmm_abtest --symbol BTCUSDT ...` end-to-end run
  against `data/binance_ticks.h5` — exits 0, emits both report
  artifacts, deterministic given fixed `--seed`.

---

## Metric Documentation

See [`METRICS_GLOSSARY.md`](METRICS_GLOSSARY.md) for full mathematical and
plain-language definitions of every metric in the reports, covering:

- Returns & Growth (Total Return, CAGR)
- Risk (Volatility, Max Drawdown, VaR, CVaR, Ulcer Index, Skewness, Kurtosis)
- Risk-Adjusted (Sharpe, Sortino, Calmar, Omega)
- Activity & Trades (Win Rate, Profit Factor, Expectancy, Exposure %)
- Tide Signal Accuracy (Hit Rate, IC, Directional IC, Expectancy, PnL Proxy, Calmar Proxy)
- Wave Regime Accuracy (Regime Hit Rate, Regime IC, Transition Matrix, Regime Stability, Regime × Bias Confusion)
- Regime & Bias Breakdown tables
- Parameters (Tide and Wave)
