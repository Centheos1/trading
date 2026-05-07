# Wave Backtest Report
**Symbol:** BTCUSDT | **Timeframe:** 5m | **Mode:** STACKED | **Exchange:** binance  
**Period:** 2022-12-31 05:40:00 → 2024-08-13 21:40:00 | **Bars:** 170,401

## Equity Curve

![Equity Curve vs Buy & Hold](wave_optimise_BTCUSDT_best05_equity.png)

## Returns & Growth
| Metric | Formula | Value |
|---|---|---|
| Total Return | $\frac{E_N - E_0}{E_0}$ | 570.99% |
| CAGR | $(E_N/E_0)^{P/N} - 1$ | 223.60% |
| Initial Capital | $E_0$ | $10,000.00 |
| Final Equity | $E_N$ | $67,099.49 |

## Risk
| Metric | Formula | Value |
|---|---|---|
| Ann. Volatility | $\sigma(r) \times \sqrt{P}$ | 56.74% |
| Ann. Downside Vol | $\sigma(\min(r,0)) \times \sqrt{P}$ | 37.09% |
| Max Drawdown | $\min_t (E_t - \text{peak}_t)/\text{peak}_t$ | -5.44% |
| Max DD Duration (bars) | 992 |
| VaR 95% | $-Q_{0.05}(r)$ | 0.03% |
| CVaR 95% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{95}]$ | 0.33% |
| VaR 99% | $-Q_{0.01}(r)$ | 0.49% |
| CVaR 99% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{99}]$ | 0.93% |
| Ulcer Index | $\sqrt{N^{-1}\sum D_t^2}$ | 0.0159 |
| Skewness | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^3$ | 1.6743 |
| Excess Kurtosis | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^4 - 3$ | 130.6723 |

## Risk-Adjusted
| Metric | Formula | Value |
|---|---|---|
| Sharpe Ratio | $(\bar{r} \cdot P - R_f)\,/\,(\sigma\sqrt{P})$ | 6.2076 |
| Sortino Ratio | $(\text{CAGR} - \text{MAR})\,/\,(\sigma_d\sqrt{P})$ | 9.4966 |
| Calmar Ratio | $\text{CAGR}\,/\,\lvert\text{MDD}\rvert$ | 41.0767 |

## Activity & Trades
| Metric | Value |
|---|---|
| Num Trades | 636 |
| Exposure % | 11.5% |
| Win Rate | 80.63% |
| Profit Factor | 13.8790 |
| Avg Win | $416.43 |
| Avg Loss | $-124.88 |
| Expectancy / Bar | 0.0034% |

## Per-Regime Breakdown
| Regime | Time % | PnL Contribution | Sharpe | N Bars |
|---|---|---|---|---|
| BREAKOUT | 0.2% | $1,092.84 | 24.16 | 304 |
| MEAN_REVERSION | 91.4% | $47,148.76 | 6.11 | 155,698 |
| BREAKDOWN | 0.1% | $-8.28 | -24.79 | 171 |
| NEUTRAL | 8.3% | $8,866.17 | 7.17 | 14,228 |

## Monthly Returns
Best month: 26.30%  |  Worst month: 1.17%  |  Positive months: 100.0%

| Month | Return |
|---|---|
| 2023-01 | 17.36% |
| 2023-02 | 5.47% |
| 2023-03 | 13.49% |
| 2023-04 | 13.15% |
| 2023-05 | 10.31% |
| 2023-06 | 8.95% |
| 2023-07 | 1.17% |
| 2023-08 | 4.26% |
| 2023-09 | 2.31% |
| 2023-10 | 14.03% |
| 2023-11 | 4.45% |
| 2023-12 | 17.79% |
| 2024-01 | 11.39% |
| 2024-02 | 26.30% |
| 2024-03 | 12.72% |
| 2024-04 | 4.52% |
| 2024-05 | 11.48% |
| 2024-06 | 2.57% |
| 2024-07 | 15.26% |
| 2024-08 | 6.19% |

## Regime Timeline

![Wave Regime Timeline](wave_optimise_BTCUSDT_best05_wave_timeline.png)

## Wave Regime Accuracy

Accuracy measures whether the Wave regime at time $t$ predicts the direction of price over the next $h$ bars.  For BREAKOUT and BREAKDOWN bars (where $g(\rho_t) \neq 0$):

$$\text{Hit Rate} = \frac{|\{t \in \mathcal{D} : g(\rho_t) \cdot f_t^{(h)} > 0\}|}{|\mathcal{D}|} \times 100\qquad \text{IC} = \text{corr}\!\left(g(\rho_t),\; f_t^{(h)}\right)$$

where $f_t^{(h)} = \log(c_{t+h}/c_t)$ and $\mathcal{D} = \{t : g(\rho_t) \neq 0\}$ (BREAKOUT + BREAKDOWN bars).

![Regime Hit Rate vs Horizon](wave_optimise_BTCUSDT_best05_wave_hit_rate.png)

### Overall Hit Rate & IC
| Horizon $h$ | Bars | Hit Rate | IC |
|---|---|---|---|
| 5min | 1 | 49.26% | -0.0110 |
| 15min | 3 | 47.58% | -0.0093 |
| 30min | 6 | 48.21% | -0.0046 |
| 1h | 12 | 48.63% | -0.0014 |
| 2h | 24 | 56.84% | 0.0080 |
| 4h | 48 | 50.74% | 0.0089 |

![Per-Regime Mean Forward Return vs Horizon](wave_optimise_BTCUSDT_best05_wave_fwd_return.png)


### Per-Regime Forward Return

Mean forward log-return $\mathbb{E}[f_t^{(h)} \mid \rho_t = R]$ and regime hit rate per horizon:

**Horizon $h=1$ bars (5min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155697 | 0.001% | 0.000% | 0.00% |
| BREAKOUT | 304 | 0.003% | 0.008% | 51.64% |
| BREAKDOWN | 171 | 0.094% | 0.002% | 45.03% |
| NEUTRAL | 14228 | 0.002% | 0.001% | 0.00% |

**Horizon $h=3$ bars (15min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155695 | 0.002% | 0.001% | 0.00% |
| BREAKOUT | 304 | 0.017% | 0.024% | 52.96% |
| BREAKDOWN | 171 | 0.155% | 0.024% | 38.01% |
| NEUTRAL | 14228 | 0.007% | 0.001% | 0.00% |

**Horizon $h=6$ bars (30min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155692 | 0.003% | 0.002% | 0.00% |
| BREAKOUT | 304 | 0.071% | 0.061% | 56.91% |
| BREAKDOWN | 171 | 0.210% | 0.043% | 32.75% |
| NEUTRAL | 14228 | 0.015% | 0.002% | 0.00% |

**Horizon $h=12$ bars (1h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155686 | 0.007% | 0.004% | 0.00% |
| BREAKOUT | 304 | 0.183% | 0.081% | 58.55% |
| BREAKDOWN | 171 | 0.357% | 0.063% | 30.99% |
| NEUTRAL | 14228 | 0.021% | 0.003% | 0.00% |

**Horizon $h=24$ bars (2h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155674 | 0.015% | 0.010% | 0.00% |
| BREAKOUT | 304 | 0.370% | 0.337% | 70.72% |
| BREAKDOWN | 171 | 0.346% | 0.075% | 32.16% |
| NEUTRAL | 14228 | 0.040% | 0.012% | 0.00% |

**Horizon $h=48$ bars (4h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 155665 | 0.032% | 0.016% | 0.00% |
| BREAKOUT | 304 | 0.492% | 0.335% | 64.14% |
| BREAKDOWN | 171 | 0.377% | 0.134% | 26.90% |
| NEUTRAL | 14213 | 0.074% | 0.022% | 0.00% |

![Regime Transition Matrix](wave_optimise_BTCUSDT_best05_wave_transition.png)

### Regime Transition Matrix

Row-normalised probabilities $T_{ij} = P(\rho_{t+1}=j \mid \rho_t=i)$:

| From \ To | MEAN_REVERSION | BREAKOUT | BREAKDOWN | NEUTRAL |
|---|---|---|---|---|
| MEAN_REVERSION | 0.995 | 0.000 | 0.000 | 0.005 |
| BREAKOUT | 0.000 | 0.974 | 0.000 | 0.026 |
| BREAKDOWN | 0.000 | 0.000 | 0.971 | 0.029 |
| NEUTRAL | 0.054 | 0.000 | 0.000 | 0.945 |

### Regime Stability

Mean consecutive-bar run length per regime ($\approx 1/(1-T_{ii})$ from transition matrix diagonal):

| Regime | Mean Run (bars) |
|---|---|
| MEAN_REVERSION | 201.4 |
| BREAKOUT | 38.0 |
| BREAKDOWN | 34.2 |
| NEUTRAL | 18.2 |

![Regime × Tide Bias Confusion](wave_optimise_BTCUSDT_best05_wave_confusion.png)

### Regime × Tide Bias Confusion

Mean forward log-return at $h=1$ for each $(\rho_t, b_t)$ combination (stacked mode only):

| Wave Regime \ Tide Bias | LONG | NEUTRAL | SHORT |
|---|---|---|---|
| BREAKDOWN | 0.000% | 0.050% | 0.392% |
| BREAKOUT | 0.005% | -0.002% | 0.000% |
| MEAN_REVERSION | 0.012% | 0.000% | -0.015% |
| NEUTRAL | 0.025% | -0.001% | -0.020% |

## Parameters
| Parameter | Value |
|---|---|
| symbol | BTCUSDT |
| exchange | binance |
| timeframe | 5m |
| mode | stacked |
| sizing_mode | base |
| max_position_base | 1.0 |
| max_position_usd | 10000.0 |
| initial_capital | 10000.0 |
| leverage | 1.0 |
| taker_fee_bps | 4.0 |
| eta_window | 29 |
| vwap_window | 8 |
| disp_window | 77 |
| ar_window | 34 |
| disp_scale | 0.056 |
| eta_mr_threshold | 0.21 |
| eta_bo_threshold | 0.61 |
| eta_neutral_threshold | 0.3 |
| dispersion_threshold | 1.028 |
| dispersion_critical | 1.263 |
| ar_critical | 0.62 |
| ar_recover | 0.48 |
| reduced_size_fraction | 0.9 |
| update_interval_ms | 0 |
| accuracy_horizons | [1, 3, 6, 12, 24, 48] |
| bar_seconds | 300.0 |

---

## Metric Glossary

> Full derivations and plain-language definitions are in `METRICS_GLOSSARY.md`
> at the repository root.  The compact reference below covers every number in
> this report.

### Returns & Growth

| Metric | Formula | Plain language |
|---|---|---|
| **Total Return** | $\frac{E_N - E_0}{E_0}$ | Simple % gain/loss over the full period |
| **CAGR** | $\left(\frac{E_N}{E_0}\right)^{P/N} - 1$ | Constant annual growth rate; normalised for run length |

### Risk

| Metric | Formula | Plain language |
|---|---|---|
| **Ann. Volatility** | $\sigma(r) \times \sqrt{P}$ | Annualised return std; penalises up and down moves equally |
| **Ann. Downside Vol** | $\sigma(\min(r,0)) \times \sqrt{P}$ | Volatility of losses only |
| **Max Drawdown** | $\min_t \frac{E_t - \max_{s\leq t} E_s}{\max_{s\leq t} E_s}$ | Worst peak-to-trough decline as a fraction of the prior peak |
| **VaR 95 / 99** | $-Q_{0.05/0.01}(r)$ | Loss exceeded on 5%/1% of bars |
| **CVaR / ES** | $-\mathbb{E}[r \mid r < \text{VaR}]$ | Average loss in the worst 5%/1% tail |
| **Ulcer Index** | $\sqrt{\frac{1}{N}\sum D_t^2}$ | RMS drawdown depth; penalises deep + prolonged drawdowns |

### Risk-Adjusted

| Metric | Formula | Plain language |
|---|---|---|
| **Sharpe** | $\frac{\bar{r} \cdot P - R_f}{\sigma \cdot \sqrt{P}}$ | Return per unit of total volatility, annualised |
| **Sortino** | $\frac{\text{CAGR} - \text{MAR}}{\sigma_d \cdot \sqrt{P}}$ | Like Sharpe but only penalises downside volatility |
| **Calmar** | $\frac{\text{CAGR}}{\lvert\text{MDD}\rvert}$ | Return per unit of worst drawdown |

### Wave Regime Accuracy

| Metric | Formula | Plain language |
|---|---|---|
| **Regime Hit Rate** | $\frac{\lvert\{t \in \mathcal{D} : g(\rho_t) \cdot f_t^{(h)} > 0\}\rvert}{\lvert\mathcal{D}\rvert} \times 100$ | % of BREAKOUT/BREAKDOWN bars where forward return aligned with regime direction |
| **Regime IC** | $\text{corr}(g(\rho_t),\; f_t^{(h)})$ | Pearson correlation of regime signal with forward log-returns |
| **Transition Prob** | $P(\rho_{t+1}=j \mid \rho_t=i)$ | Row-normalised probability of moving from regime $i$ to regime $j$ |
| **Regime Stability** | $\mathbb{E}[\text{consecutive bars in regime } R]$ | Mean run length per regime; higher = more persistent (less whipsaw) |

Where $g(\rho_t) \in \{+1, 0, -1\}$: BREAKOUT $\to +1$, BREAKDOWN $\to -1$, MR/NEUTRAL $\to 0$.

### Wave Sizing

$$\text{target\_units} = \underbrace{b_t}_{\text{Tide sign}} \times \underbrace{m_t}_{\text{Tide risk mult}} \times \underbrace{\phi(\rho_t)}_{\text{Wave size frac}} \times q_{\max}$$

| $\rho_t$ | $\phi(\rho_t)$ |
|---|---|
| BREAKOUT | $1.0$ |
| MEAN\_REVERSION | `reduced_size_fraction` |
| NEUTRAL | `reduced_size_fraction` |
| BREAKDOWN | $0.0$ |
