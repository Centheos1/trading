# Trading App - Backtesting & Order Flow Engine

A multi-strategy backtesting and optimisation platform with a C++ order flow engine and Bookmap-style UI.

---

## Prerequisites

- **macOS** (tested on Apple Silicon)
- **Python 3.9+** with a virtual environment at `.venv/`
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

### 3. Build the C++ order flow engine

```bash
cd backtestingCpp/orderflow
chmod +x build.sh
./build.sh
```

Verify the build:

```bash
cd ../..
python -c "
import sys; sys.path.insert(0, 'backtestingCpp/orderflow/build')
import orderflow_engine as ofe
print('Module loaded:', [x for x in dir(ofe) if not x.startswith('_')])
engine = ofe.OrderFlowEngine()
print('Engine OK, running:', engine.is_running())
"
```

You should see the full class list and `Engine OK, running: False`.

### 4. Create required directories

```bash
mkdir -p logs data
```

---

## Testing Guide

### Test 1: Collect Tick Data (WebSocket)

This streams live Binance Futures trade and depth data via WebSocket and stores it in HDF5.

```bash
python main.py
```

Then enter:
- Mode: `data`
- Exchange: `binance`
- Symbol: `BTCUSDT`
- Pull type: `ticks`
- Duration: `30` (seconds, use `0` for indefinite with Ctrl+C to stop)

**Expected output:**
```
Starting tick data collection for BTCUSDT
Loaded depth snapshot: 1000 bids, 1000 asks
Collected X trades, Y depth updates
Tick data collection complete for BTCUSDT: X trades, Y depth updates
```

**Verify the stored data:**

```bash
python -c "
import sys, time; sys.path.insert(0, 'backtestingCpp/orderflow/build')
import orderflow_engine as ofe
store = ofe.TickStore('data/binance_ticks.h5')
print('Symbols:', store.list_symbols())
trades = store.load_trades('BTCUSDT', 0, int(time.time()*1000))
print(f'Trades: {len(trades)}')
if trades:
    t = trades[0]
    print(f'  First: ts={t.timestamp} price={t.price:.2f} qty={t.quantity}')
depths = store.load_depth_updates('BTCUSDT', 0, int(time.time()*1000))
print(f'Depth updates: {len(depths)}')
snaps = store.load_depth_snapshots('BTCUSDT', 0, int(time.time()*1000))
print(f'Depth snapshots: {len(snaps)}')
store.close()
"
```

**Collect more data** for better backtesting results. Run for several minutes or longer. The more tick data you have, the more meaningful the backtests and optimisation will be.

---

### Test 2: Backtest the Order Flow Strategy

Requires tick data from Test 1.

```bash
python main.py
```

Then enter:
- Mode: `backtest`
- Exchange: `binance`
- Symbol: `BTCUSDT`
- Strategy: `orderflow`
- Timeframe: `1m` (or any supported)
- From: press Enter (uses all data)
- To: press Enter (uses all data)

Then enter strategy parameters when prompted:
- Imbalance Threshold: `3.0`
- Stacked Imbalance Levels: `3`
- Absorption Volume Ratio: `5.0`
- CVD Divergence Lookback: `100`
- Exhaustion Lookback Bars: `5`
- Min Signal Strength: `0.3`
- Tick Size: `0.01`

**Expected output:** A tuple of `(pnl, max_drawdown, num_trades, sharpe_ratio, cagr)`.

If you get `FileNotFoundError`, you need to collect tick data first (Test 1).

---

### Test 3: Optimise Order Flow Parameters (NSGA-II)

Requires tick data from Test 1.

```bash
python main.py
```

Then enter:
- Mode: `optimise`
- Exchange: `binance`
- Symbol: `BTCUSDT`
- Strategy: `orderflow`
- Timeframe: `1m`
- From / To: press Enter for both
- Save results: `f`
- Population size: `10` (small for testing, use 50+ for real runs)
- Generations: `3` (small for testing, use 20+ for real runs)

**Expected output:** Progress percentage and a table of Pareto-optimal parameter sets ranked by PnL, max drawdown, and Sharpe ratio.

---

### Test 4: Launch the UI

```bash
python main.py
```

Then enter: `ui`

**Expected:** A PySide6 window opens with:
- **Toolbar**: Symbol input, Mode selector (Live/Replay), Connect/Disconnect buttons, Tick Size, Imbalance threshold
- **Heatmap widget** (top right): Bookmap-style depth heatmap
- **Volume Profile widget** (top left): Horizontal volume bars with POC and value area
- **CVD chart** (bottom left): Cumulative Volume Delta line chart
- **Trade Blotter** (bottom right): Signal log table

#### Known issue: Live mode in the UI

The UI's "Live" Connect button currently uses the C++ `BinanceWsFeed` for WebSocket streaming, which has a known crash with Boost.Beast 1.90 on macOS (`mutex lock failed` after ~100 messages). This needs to be ported to the same Python WebSocket approach used by the tick data collector.

**To test the UI visually**, you can launch it and verify:
1. The window renders correctly with all 4 panels
2. The dark theme applies
3. The toolbar controls are interactive
4. The status bar shows at the bottom

**To make the UI work with live data**, the `_on_connect` method in `ui/main_window.py` needs to be updated to use Python WebSocket streaming (like `TickDataCollector`) instead of `ofe.BinanceWsFeed`. This is the remaining work item.

---

### Test 5: Verify C++ Engine Processing Directly

Quick standalone test of the engine's data processing pipeline:

```bash
python -c "
import sys, time; sys.path.insert(0, 'backtestingCpp/orderflow/build')
import orderflow_engine as ofe

engine = ofe.OrderFlowEngine()

# Process some synthetic trades
for i in range(100):
    t = ofe.Trade()
    t.timestamp = 1000 + i
    t.price = 50000.0 + (i % 20) * 0.5
    t.quantity = 0.01 + (i % 5) * 0.005
    t.is_buyer_maker = (i % 3 != 0)
    engine.process_trade(t)

# Process a depth snapshot
d = ofe.DepthUpdate()
d.timestamp = 2000
d.is_snapshot = True
d.first_update_id = 1
d.final_update_id = 1
bids, asks = [], []
for i in range(10):
    b = ofe.DepthLevel(); b.price = 49995.0 + i; b.quantity = 1.0 + i * 0.1
    a = ofe.DepthLevel(); a.price = 50005.0 + i; a.quantity = 1.0 + i * 0.1
    bids.append(b); asks.append(a)
d.bids = bids; d.asks = asks
engine.process_depth(d)

# Check results
tf = engine.get_trade_flow()
print(f'Delta:      {tf.get_delta():.4f}')
print(f'Buy vol:    {tf.get_buy_volume():.4f}')
print(f'Sell vol:   {tf.get_sell_volume():.4f}')
print(f'Total vol:  {tf.get_total_volume():.4f}')

cvd = engine.get_cvd()
print(f'CVD:        {cvd.get_cvd():.4f}')

ob = engine.get_order_book()
print(f'Best bid:   {ob.get_best_bid():.2f}')
print(f'Best ask:   {ob.get_best_ask():.2f}')
print(f'Spread:     {ob.get_spread():.2f}')

vp = engine.get_volume_profile()
print(f'POC:        {vp.get_poc_price():.2f}')
va = vp.compute_value_area(0.70)
print(f'Value Area: {va.val:.2f} - {va.vah:.2f}')

fp = engine.get_footprint()
print(f'Footprint bars: {fp.bar_count()}')

se = engine.get_signal_engine()
print(f'Signals:    {len(se.get_signals())}')
print(f'PnL:        {se.get_pnl():.2f}%')
print(f'Num trades: {se.get_num_trades()}')
print()
print('All engine components working!')
"
```

---

### Test 6: Replay Feed (Historical Backtest via C++)

If you have tick data stored from Test 1:

```bash
python -c "
import sys, time; sys.path.insert(0, 'backtestingCpp/orderflow/build')
import orderflow_engine as ofe

store = ofe.TickStore('data/binance_ticks.h5')
print('Symbols:', store.list_symbols())

config = ofe.EngineConfig()
config.tick_size = 0.01
engine = ofe.OrderFlowEngine(config)

replay = ofe.ReplayFeed(store, 0.0)
replay.set_time_range(0, int(time.time() * 1000))

ofe.connect_feed(engine, replay)
engine.start('BTCUSDT')
replay.run_sync()

se = engine.get_signal_engine()
print(f'Replay complete')
print(f'  Signals:    {len(se.get_signals())}')
print(f'  PnL:        {se.get_pnl():.4f}%')
print(f'  Max DD:     {se.get_max_drawdown():.4f}%')
print(f'  Num trades: {se.get_num_trades()}')

tf = engine.get_trade_flow()
print(f'  Total vol:  {tf.get_total_volume():.4f}')

engine.stop()
store.close()
"
```

---

## Remaining Work

### UI Live Mode (Priority)
The `ui/main_window.py` `_on_connect()` method uses the C++ `BinanceWsFeed` which crashes with Boost 1.90 on macOS. Port it to use Python `websockets` (async) feeding data into the engine via `process_trade()` / `process_depth()`, matching the pattern in `data_service.py` `TickDataCollector`.

### Other Items
- **HDF5 first-open diagnostic**: The HDF5 error trace when creating a new file is suppressed in `TickStore`, but may still appear if other code paths create stores
- **Longer data collection**: Collect hours/days of tick data for meaningful backtest and optimisation results
- **UI Replay mode**: Wire the Replay button in the UI to use `ReplayFeed` with a stored HDF5 file
- **Signal tuning**: Adjust `SignalParams` thresholds based on backtest results for better signal quality

---

## Architecture

```
main.py                     CLI entry point (data / backtest / optimise / ui)
data_service.py             Tick data collection (Python WebSocket -> C++ engine -> HDF5)
backtester.py               Strategy dispatcher (incl. orderflow)
optimiser.py                NSGA-II multi-objective optimiser
strategies/orderflow.py     Python wrapper for C++ order flow backtest

ui/
  app.py                    PySide6 application entry
  main_window.py            Main window with toolbar, panels, engine integration
  heatmap_widget.py         Bookmap-style depth heatmap
  volume_profile_widget.py  Volume at price histogram
  cvd_widget.py             Cumulative Volume Delta chart
  trade_blotter.py          Signal log table

backtestingCpp/orderflow/
  Types.h                   Core structs (Trade, DepthUpdate, Signal, etc.)
  IDataFeed.h               Abstract data feed interface
  OrderBook.h/cpp           L2 order book with absorption detection
  TradeFlow.h/cpp           Trade flow analysis, delta, large trade detection
  VolumeProfile.h/cpp       Volume at price, POC, value area
  CumulativeVolumeDelta.h/cpp  CVD tracking, divergence detection
  FootprintChart.h/cpp      Footprint bars with bid/ask per price
  SignalEngine.h/cpp         Auction market theory signal generation + PnL
  OrderFlowEngine.h/cpp     High-level facade wiring all components
  BinanceWsFeed.h/cpp       C++ WebSocket feed (has Boost 1.90 crash - use Python instead)
  TickStore.h/cpp           HDF5 storage for trades and depth data
  ReplayFeed.h/cpp          Replay historical data through IDataFeed
  bindings.cpp              pybind11 Python bindings
  CMakeLists.txt            Build configuration
  build.sh                  One-step build script
```
