# Trading App — Backtesting & Optimisation Platform

A multi-layer crypto trading strategy research platform implementing the
**Tide / Wave / Ripple** hierarchy with event-time backtesting, multi-objective
parameter optimisation (NSGA-II), and a C++ order flow engine.

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
ui/                              PySide6 Bookmap-style trading UI
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

## Prerequisites

- **macOS** (tested on Apple Silicon)
- **Python 3.12+** with a virtual environment at `.venv/`
- **Homebrew** packages: `boost`, `hdf5`, `openssl`, `cmake`, `nlohmann-json`, `pybind11`
- **pip** packages: see `requirements.txt`

---

## Setup

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
# Wave layer tests (46 tests)
python -m unittest tests/test_wave_backtest.py -v

# Tide layer tests
python -m unittest tests/test_tide_accuracy.py -v
```

---

## Legacy Order Flow Tests

### Collect tick data

```bash
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

---

## Remaining Work

### Ripple Layer (Phase Next)
Microstructure execution layer using L2 order book data. Ingests `PermissionSet`
from Wave and `TideBias` + risk multiplier from Tide. Triggers actual trades via
bounce / breakout archetypes.

### Wave Parameter Tuning
Default thresholds (`eta_bo_threshold=0.7`, `dispersion_critical=1.2`, etc.) are
V1 defaults. Run the Wave optimiser on your dataset to calibrate them.

### Multi-Asset Dispersion & Absorption Ratio (V2/V3)
V1 uses single-symbol proxies for dispersion (rolling return std) and absorption
ratio (vol-of-vol ratio). True cross-sectional metrics require a panel of
correlated assets — planned for V2/V3 per `strategy.md` roadmap.

### UI Live Mode
The `ui/main_window.py` `_on_connect()` method uses the C++ `BinanceWsFeed`
which has a known crash with Boost 1.90 on macOS. Port to the Python WebSocket
approach used by `TickDataCollector`.

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
