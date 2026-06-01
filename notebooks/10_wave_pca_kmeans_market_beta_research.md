# Wave PCA / K-means Market-Beta Research

**Notebook:** `notebooks/10_wave_pca_kmeans_market_beta_research.ipynb`  
**Helper module:** `notebooks/wave_factor_research.py`  
**Strategy reference:** §8 (Wave), §18 (Permissions Matrix), §17 (Data)

---

## Research question

Can a pruned universe of Binance and Oanda assets provide useful Wave-layer
context for BTC and ETH?

Specifically: do contemporaneous correlations, rolling correlation stability,
and lead/lag correlations across a broad asset universe reveal market-structure
information — regime shifts, directional pressure, cohesion and fragmentation —
that the single-symbol Wave features (η, dispersion, AR) cannot capture on
their own?

The hypothesis is that:

1. A small, carefully selected cross-asset universe carries the majority of the
   market-structure signal.
2. The first principal component of that universe tracks the broad market
   factor and its variance-explained fraction is a reliable cohesion proxy.
3. BTC/ETH residuals relative to the factor model identify idiosyncratic
   price pressure that can precede or accompany directional moves.

---

## Universe pruning

### Scoring pipeline

Every non-BTC/ETH asset is scored on four dimensions, implemented in
`wave_factor_research.score_universe`:

| Dimension | Metric | Weight |
|---|---|---|
| Contemporaneous relevance | `max(abs_corr_btc, abs_corr_eth)` | 0.30 |
| Correlation stability | `mean(|rolling_corr|) / std(rolling_corr)` | 0.30 |
| Predictive lead/lag | `max |corr(asset[t], BTC[t+h])|` across h ∈ {1,5,15,30} bars | 0.30 |
| Data quality | `n_valid / n_total` | 0.10 |

All component scores are min-max normalised to [0, 1] before combining.

### Selection priority

The final universe of `target_n_assets` (default 30) is built in order:

1. **Anchors** — BTC and ETH always included.
2. **Core crypto bucket** — BNB, SOL, XRP, DOGE, ADA, LINK, AVAX if available.
3. **Core macro/Oanda bucket** — NAS100, SPX500, XAU/USD, EUR/USD, USD/JPY,
   GBP/USD, AUD/USD, WTI/crude if available (with name-variant fallback).
4. **Score fill** — remaining slots from the top of the `combined_score`
   ranking.

Each selected asset is labelled with its `selection_reason` so the provenance
of the universe is transparent.

---

## How PCA is applied

Rolling PCA is applied to the standardised return panel of the selected
universe.  For each timestep `t`, a window of `W` preceding bars is
standardised and decomposed into `n_components` orthogonal factors.

Key outputs per window:

- **Explained variance ratio** for each PC — how much of total universe
  variance each factor captures.
- **PC1 loadings** — each asset's contribution to the market factor.
- **PC1 score** — the instantaneous return of the market factor.
- **BTC / ETH loadings on PC1** — whether BTC/ETH are leading or lagging the
  market factor.

Two look-back windows are run in parallel (default 60 and 240 bars) to
distinguish short-term and medium-term structure.

**Market cohesion proxy:** `wave.pc1_variance_explained` — the fraction of
total universe return variance captured by PC1.  A high value (e.g. > 0.45)
indicates the universe is moving together, amplifying directional signals.  A
low value indicates fragmentation.

---

## How K-means is applied

K-means clusters the universe assets using their latest PC loadings as
features (one row per asset, one column per PC).

Steps:

1. Extract the latest snapshot of PC loadings from the rolling PCA output.
2. Sweep `k` from 2 to 8, recording inertia and silhouette score.
3. Choose `k_final` (default 4) from the elbow / silhouette plots.
4. Fit the final model; assign each asset a cluster label.
5. Compute per-cluster statistics: mean return, volatility, Sharpe, correlation
   to BTC/ETH, lead/lag relationship to BTC/ETH, and the cluster leader (asset
   with highest abs correlation to the cluster mean).

Clusters typically resolve into groups such as: large-cap crypto, macro/FX,
high-beta altcoins, and a decorrelated or inverse group.  The cluster leader is
the asset whose return most reliably proxies the cluster's direction.

---

## Metrics used to evaluate the hypothesis

| Metric | What it tests |
|---|---|
| `pearson_r` of signal vs future return | Linear predictive relationship |
| Directional hit rate | Fraction of bars where sign(signal) == sign(future return) |
| 95% bootstrap CI on hit rate | Whether hit rate is statistically above chance |
| Quantile mean return curves | Monotonic response to signal buckets |
| t-stat (positive vs negative signal bins) | Whether the mean future return differs between signal states |

Tests are run for signals `btc_residual_z`, `eth_residual_z`,
`market_beta_pressure`, and `pc1_score` at horizons 1, 5, 15, and 30 bars.

A **useful** result is one where:
- Pearson r > 0.05 at a short horizon, or
- Directional hit rate > 52% with a CI that does not include 50%, or
- Quantile curves show a clear monotonic pattern.

---

## Outputs that could become Wave features

The full feature catalogue is in the notebook's §8 and in
`wave_factor_research.wave_feature_table()`.  Priority candidates:

| Feature | Computation | Intended use |
|---|---|---|
| `wave.pc1_variance_explained` | `PCA.explained_variance_ratio_[0]` from rolling fit | Cohesion gate — high value amplifies directional permissions |
| `wave.market_beta_pressure` | `tanh((btc_residual_z + eth_residual_z) / 4)` | Directional composite [-1, +1] for permission weighting |
| `wave.btc_residual_z` | `(btc_actual - btc_expected) / rolling_std(btc_residual)` | Idiosyncratic BTC pressure; large positive → amplify long permission |
| `wave.eth_residual_z` | Same for ETH | Same for ETH permissions |
| `wave.cluster_leader_return` | Return of leader asset in dominant cluster | Early directional signal |
| `wave.market_cohesion` | Mean off-diagonal pairwise correlation | Cohesion vs fragmentation label |
| `wave.risk_on_score` | Weighted: equities up, gold down, high-beta crypto leading | Enable long breakout/momentum |
| `wave.risk_off_score` | Weighted: equities down, gold up, USD strong | Enable short/breakdown |

---

## Scope boundary

This notebook is **research only**.

- No production `WaveEngine` code is modified.
- No connection to the live trading path.
- No live exchange API calls (uses collected Parquet/HDF5 data only).
- Features require out-of-sample validation, production cadence design, and
  a full code review before being wired into `WaveFeatureParams` or
  `WaveEngine`.
