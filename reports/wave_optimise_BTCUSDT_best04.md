# Wave Backtest Report
**Symbol:** BTCUSDT | **Timeframe:** 5m | **Mode:** STACKED | **Exchange:** binance  
**Period:** 2022-12-31 05:40:00 → 2024-08-13 21:40:00 | **Bars:** 170,401

## Equity Curve

![Equity Curve vs Buy & Hold](wave_optimise_BTCUSDT_best04_equity.png)

## Returns & Growth
| Metric | Formula | Value |
|---|---|---|
| Total Return | $\frac{E_N - E_0}{E_0}$ | 569.96% |
| CAGR | $(E_N/E_0)^{P/N} - 1$ | 223.29% |
| Initial Capital | $E_0$ | $10,000.00 |
| Final Equity | $E_N$ | $66,995.81 |

## Risk
| Metric | Formula | Value |
|---|---|---|
| Ann. Volatility | $\sigma(r) \times \sqrt{P}$ | 56.67% |
| Ann. Downside Vol | $\sigma(\min(r,0)) \times \sqrt{P}$ | 37.04% |
| Max Drawdown | $\min_t (E_t - \text{peak}_t)/\text{peak}_t$ | -5.45% |
| Max DD Duration (bars) | 992 |
| VaR 95% | $-Q_{0.05}(r)$ | 0.03% |
| CVaR 95% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{95}]$ | 0.33% |
| VaR 99% | $-Q_{0.01}(r)$ | 0.49% |
| CVaR 99% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{99}]$ | 0.93% |
| Ulcer Index | $\sqrt{N^{-1}\sum D_t^2}$ | 0.0160 |
| Skewness | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^3$ | 1.6790 |
| Excess Kurtosis | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^4 - 3$ | 130.9599 |

## Risk-Adjusted
| Metric | Formula | Value |
|---|---|---|
| Sharpe Ratio | $(\bar{r} \cdot P - R_f)\,/\,(\sigma\sqrt{P})$ | 6.2042 |
| Sortino Ratio | $(\text{CAGR} - \text{MAR})\,/\,(\sigma_d\sqrt{P})$ | 9.4917 |
| Calmar Ratio | $\text{CAGR}\,/\,\lvert\text{MDD}\rvert$ | 40.9460 |

## Activity & Trades
| Metric | Value |
|---|---|
| Num Trades | 633 |
| Exposure % | 11.5% |
| Win Rate | 80.63% |
| Profit Factor | 13.8797 |
| Avg Win | $415.70 |
| Avg Loss | $-124.66 |
| Expectancy / Bar | 0.0033% |

## Per-Regime Breakdown
| Regime | Time % | PnL Contribution | Sharpe | N Bars |
|---|---|---|---|---|
| BREAKOUT | 0.0% | $27.94 | 34.89 | 14 |
| MEAN_REVERSION | 99.6% | $56,102.37 | 6.16 | 169,776 |
| BREAKDOWN | 0.1% | $-8.28 | -24.79 | 171 |
| NEUTRAL | 0.3% | $873.78 | 16.82 | 440 |

## Monthly Returns
Best month: 26.21%  |  Worst month: 1.17%  |  Positive months: 100.0%

| Month | Return |
|---|---|
| 2023-01 | 17.44% |
| 2023-02 | 5.18% |
| 2023-03 | 13.52% |
| 2023-04 | 13.17% |
| 2023-05 | 10.33% |
| 2023-06 | 8.96% |
| 2023-07 | 1.17% |
| 2023-08 | 4.10% |
| 2023-09 | 2.32% |
| 2023-10 | 14.07% |
| 2023-11 | 4.46% |
| 2023-12 | 17.83% |
| 2024-01 | 11.41% |
| 2024-02 | 26.21% |
| 2024-03 | 12.76% |
| 2024-04 | 4.54% |
| 2024-05 | 11.51% |
| 2024-06 | 2.57% |
| 2024-07 | 15.28% |
| 2024-08 | 6.20% |

## Regime Timeline

![Wave Regime Timeline](wave_optimise_BTCUSDT_best04_wave_timeline.png)

## Wave Regime Accuracy

Accuracy measures whether the Wave regime at time $t$ predicts the direction of price over the next $h$ bars.  For BREAKOUT and BREAKDOWN bars (where $g(\rho_t) \neq 0$):

$$\text{Hit Rate} = \frac{|\{t \in \mathcal{D} : g(\rho_t) \cdot f_t^{(h)} > 0\}|}{|\mathcal{D}|} \times 100\qquad \text{IC} = \text{corr}\!\left(g(\rho_t),\; f_t^{(h)}\right)$$

where $f_t^{(h)} = \log(c_{t+h}/c_t)$ and $\mathcal{D} = \{t : g(\rho_t) \neq 0\}$ (BREAKOUT + BREAKDOWN bars).

![Regime Hit Rate vs Horizon](wave_optimise_BTCUSDT_best04_wave_hit_rate.png)

### Overall Hit Rate & IC
| Horizon $h$ | Bars | Hit Rate | IC |
|---|---|---|---|
| 5min | 1 | 44.86% | -0.0190 |
| 15min | 3 | 38.38% | -0.0209 |
| 30min | 6 | 33.51% | -0.0197 |
| 1h | 12 | 30.27% | -0.0228 |
| 2h | 24 | 34.05% | -0.0137 |
| 4h | 48 | 30.81% | -0.0087 |

![Per-Regime Mean Forward Return vs Horizon](wave_optimise_BTCUSDT_best04_wave_fwd_return.png)


### Per-Regime Forward Return

Mean forward log-return $\mathbb{E}[f_t^{(h)} \mid \rho_t = R]$ and regime hit rate per horizon:

**Horizon $h=1$ bars (5min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169775 | 0.001% | 0.000% | 0.00% |
| BREAKOUT | 14 | -0.034% | -0.080% | 42.86% |
| BREAKDOWN | 171 | 0.094% | 0.002% | 45.03% |
| NEUTRAL | 440 | 0.007% | 0.001% | 0.00% |

**Horizon $h=3$ bars (15min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169773 | 0.002% | 0.001% | 0.00% |
| BREAKOUT | 14 | -0.294% | -0.154% | 42.86% |
| BREAKDOWN | 171 | 0.155% | 0.024% | 38.01% |
| NEUTRAL | 440 | 0.038% | 0.011% | 0.00% |

**Horizon $h=6$ bars (30min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169770 | 0.004% | 0.002% | 0.00% |
| BREAKOUT | 14 | -0.342% | -0.319% | 42.86% |
| BREAKDOWN | 171 | 0.210% | 0.043% | 32.75% |
| NEUTRAL | 440 | 0.091% | 0.043% | 0.00% |

**Horizon $h=12$ bars (1h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169764 | 0.008% | 0.004% | 0.00% |
| BREAKOUT | 14 | -0.381% | -0.146% | 21.43% |
| BREAKDOWN | 171 | 0.357% | 0.063% | 30.99% |
| NEUTRAL | 440 | 0.179% | 0.077% | 0.00% |

**Horizon $h=24$ bars (2h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169752 | 0.017% | 0.010% | 0.00% |
| BREAKOUT | 14 | 0.124% | 0.074% | 57.14% |
| BREAKDOWN | 171 | 0.346% | 0.075% | 32.16% |
| NEUTRAL | 440 | 0.320% | 0.286% | 0.00% |

**Horizon $h=48$ bars (4h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169728 | 0.035% | 0.016% | 0.00% |
| BREAKOUT | 14 | 0.678% | 0.713% | 78.57% |
| BREAKDOWN | 171 | 0.377% | 0.134% | 26.90% |
| NEUTRAL | 440 | 0.506% | 0.378% | 0.00% |

![Regime Transition Matrix](wave_optimise_BTCUSDT_best04_wave_transition.png)

### Regime Transition Matrix

Row-normalised probabilities $T_{ij} = P(\rho_{t+1}=j \mid \rho_t=i)$:

| From \ To | MEAN_REVERSION | BREAKOUT | BREAKDOWN | NEUTRAL |
|---|---|---|---|---|
| MEAN_REVERSION | 1.000 | 0.000 | 0.000 | 0.000 |
| BREAKOUT | 0.000 | 0.214 | 0.000 | 0.786 |
| BREAKDOWN | 0.000 | 0.000 | 0.971 | 0.029 |
| NEUTRAL | 0.030 | 0.007 | 0.000 | 0.964 |

### Regime Stability

Mean consecutive-bar run length per regime ($\approx 1/(1-T_{ii})$ from transition matrix diagonal):

| Regime | Mean Run (bars) |
|---|---|
| MEAN_REVERSION | 12126.9 |
| BREAKOUT | 1.3 |
| BREAKDOWN | 34.2 |
| NEUTRAL | 27.5 |

![Regime × Tide Bias Confusion](wave_optimise_BTCUSDT_best04_wave_confusion.png)

### Regime × Tide Bias Confusion

Mean forward log-return at $h=1$ for each $(\rho_t, b_t)$ combination (stacked mode only):

| Wave Regime \ Tide Bias | LONG | NEUTRAL | SHORT |
|---|---|---|---|
| BREAKDOWN | 0.000% | 0.050% | 0.392% |
| BREAKOUT | -0.011% | -0.065% | 0.000% |
| MEAN_REVERSION | 0.014% | 0.000% | -0.016% |
| NEUTRAL | 0.006% | 0.002% | 0.141% |

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
| disp_window | 39 |
| ar_window | 34 |
| disp_scale | 0.089 |
| eta_mr_threshold | 0.21 |
| eta_bo_threshold | 0.62 |
| eta_neutral_threshold | 0.68 |
| dispersion_threshold | 1.028 |
| dispersion_critical | 1.263 |
| ar_critical | 0.6 |
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
