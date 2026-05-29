# Research Notebooks

A walk-through of the Tide / Wave / Ripple strategy aimed at quant
researchers. **This directory is for research, not deployment.** Every
notebook imports the live strategy modules (`schemas`, `tide`, `wave`,
`strategies.orderflow`, `hmm`) so changes to the implementation are
reflected immediately — there is no separate "research copy" of the math.

## Layout

| # | Notebook | strategy.md focus |
|---|---|---|
| 00 | [`00_data_infrastructure.ipynb`](00_data_infrastructure.ipynb) | §17 — OHLCV (Parquet/S3) and tick (HDF5) loaders |
| 01 | [`01_tide_layer.ipynb`](01_tide_layer.ipynb) | §7 — realised vol, vol regimes, LSI, risk multiplier, ES |
| 02 | [`02_wave_layer.ipynb`](02_wave_layer.ipynb) | §8 + §18 — η, dispersion, AR, regime state machine, permissions matrix |
| 03 | [`03_ripple_microstructure.ipynb`](03_ripple_microstructure.ipynb) | §9.5 — imbalance, microprice, OFI, CVD, λ, VPIN, LSI |
| 04 | [`04_wall_detection_liquidity_map.ipynb`](04_wall_detection_liquidity_map.ipynb) | §9.6 + §9.7 + §10 — walls, evidence → state, liquidity map |
| 05 | [`05_trade_archetypes.ipynb`](05_trade_archetypes.ipynb) | §11 + §9.6.3 — bounce / breakout, hold-vs-break logistic |
| 06 | [`06_trade_lifecycle_exits.ipynb`](06_trade_lifecycle_exits.ipynb) | §12 + §13 — state machine and exit taxonomy |
| 07 | [`07_risk_and_sizing.ipynb`](07_risk_and_sizing.ipynb) | §14.1–14.4 + §15 — 3-step sizing, scale-in/out, trailing stop |
| 08 | [`08_full_backtest_analysis.ipynb`](08_full_backtest_analysis.ipynb) | §20 + §22 — Tide/Wave/Ripple end-to-end on real data |
| 09 | [`09_hmm_extension.ipynb`](09_hmm_extension.ipynb) | §9.10 — HMM training, BIC selection, forward filtering |

## Setup

```bash
pip install -r requirements.txt -r notebooks/requirements.txt
jupyter lab
```

## Data sources

`notebooks/utils.py` provides one entry point for both data stores:

```python
from notebooks.utils import load_ohlcv, load_ticks

# Reads ./data/ohlcv/... by default; switch to S3 with
# DATA_STORE=s3 and S3_BUCKET=my-bucket.
df = load_ohlcv("binance", "BTCUSDT", "1m")

# Trades + per-side depth from data/binance_ticks.h5 (or any HDF5 written
# by the project's TickStore).
ticks = load_ticks("BTCUSDT")
```

The backend honours the project-wide `DATA_STORE` env var
(`local_parquet` or `s3`).
