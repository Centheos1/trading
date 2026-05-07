# Wave Backtest Report
**Symbol:** BTCUSDT | **Timeframe:** 5m | **Mode:** STACKED | **Exchange:** binance  
**Period:** 2022-12-31 05:40:00 → 2024-08-13 21:40:00 | **Bars:** 170,401

## Equity Curve

![Equity Curve vs Buy & Hold](wave_optimise_BTCUSDT_best03_equity.png)

## Returns & Growth
| Metric | Formula | Value |
|---|---|---|
| Total Return | $\frac{E_N - E_0}{E_0}$ | 231.80% |
| CAGR | $(E_N/E_0)^{P/N} - 1$ | 109.57% |
| Initial Capital | $E_0$ | $10,000.00 |
| Final Equity | $E_N$ | $33,179.77 |

## Risk
| Metric | Formula | Value |
|---|---|---|
| Ann. Volatility | $\sigma(r) \times \sqrt{P}$ | 23.51% |
| Ann. Downside Vol | $\sigma(\min(r,0)) \times \sqrt{P}$ | 15.45% |
| Max Drawdown | $\min_t (E_t - \text{peak}_t)/\text{peak}_t$ | -3.65% |
| Max DD Duration (bars) | 2,746 |
| VaR 95% | $-Q_{0.05}(r)$ | 0.01% |
| CVaR 95% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{95}]$ | 0.14% |
| VaR 99% | $-Q_{0.01}(r)$ | 0.20% |
| CVaR 99% (ES) | $-\mathbb{E}[r \mid r < \text{VaR}_{99}]$ | 0.39% |
| Ulcer Index | $\sqrt{N^{-1}\sum D_t^2}$ | 0.0095 |
| Skewness | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^3$ | 1.4684 |
| Excess Kurtosis | $\frac{1}{N}\sum((r_t-\bar r)/\sigma)^4 - 3$ | 131.3147 |

## Risk-Adjusted
| Metric | Formula | Value |
|---|---|---|
| Sharpe Ratio | $(\bar{r} \cdot P - R_f)\,/\,(\sigma\sqrt{P})$ | 6.0823 |
| Sortino Ratio | $(\text{CAGR} - \text{MAR})\,/\,(\sigma_d\sqrt{P})$ | 9.2549 |
| Calmar Ratio | $\text{CAGR}\,/\,\lvert\text{MDD}\rvert$ | 29.9955 |

## Activity & Trades
| Metric | Value |
|---|---|
| Num Trades | 634 |
| Exposure % | 11.5% |
| Win Rate | 80.10% |
| Profit Factor | 13.1392 |
| Avg Win | $170.99 |
| Avg Loss | $-52.40 |
| Expectancy / Bar | 0.0014% |

## Per-Regime Breakdown
| Regime | Time % | PnL Contribution | Sharpe | N Bars |
|---|---|---|---|---|
| BREAKOUT | 0.1% | $-347.33 | -25.56 | 103 |
| MEAN_REVERSION | 99.3% | $22,606.90 | 6.10 | 169,274 |
| BREAKDOWN | 0.1% | $-3.41 | -24.79 | 171 |
| NEUTRAL | 0.5% | $923.60 | 20.97 | 853 |

## Monthly Returns
Best month: 17.44%  |  Worst month: 0.67%  |  Positive months: 100.0%

| Month | Return |
|---|---|
| 2023-01 | 6.32% |
| 2023-02 | 2.35% |
| 2023-03 | 6.31% |
| 2023-04 | 6.56% |
| 2023-05 | 5.47% |
| 2023-06 | 4.96% |
| 2023-07 | 0.67% |
| 2023-08 | 2.26% |
| 2023-09 | 1.36% |
| 2023-10 | 8.34% |
| 2023-11 | 2.79% |
| 2023-12 | 11.33% |
| 2024-01 | 7.67% |
| 2024-02 | 17.44% |
| 2024-03 | 9.54% |
| 2024-04 | 3.49% |
| 2024-05 | 8.95% |
| 2024-06 | 2.05% |
| 2024-07 | 12.22% |
| 2024-08 | 5.09% |

## Regime Timeline

![Wave Regime Timeline](wave_optimise_BTCUSDT_best03_wave_timeline.png)

## Wave Regime Accuracy

Accuracy measures whether the Wave regime at time $t$ predicts the direction of price over the next $h$ bars.  For BREAKOUT and BREAKDOWN bars (where $g(\rho_t) \neq 0$):

$$\text{Hit Rate} = \frac{|\{t \in \mathcal{D} : g(\rho_t) \cdot f_t^{(h)} > 0\}|}{|\mathcal{D}|} \times 100\qquad \text{IC} = \text{corr}\!\left(g(\rho_t),\; f_t^{(h)}\right)$$

where $f_t^{(h)} = \log(c_{t+h}/c_t)$ and $\mathcal{D} = \{t : g(\rho_t) \neq 0\}$ (BREAKOUT + BREAKDOWN bars).

![Regime Hit Rate vs Horizon](wave_optimise_BTCUSDT_best03_wave_hit_rate.png)

### Overall Hit Rate & IC
| Horizon $h$ | Bars | Hit Rate | IC |
|---|---|---|---|
| 5min | 1 | 44.89% | -0.0190 |
| 15min | 3 | 41.97% | -0.0208 |
| 30min | 6 | 38.69% | -0.0163 |
| 1h | 12 | 40.51% | -0.0136 |
| 2h | 24 | 48.18% | -0.0007 |
| 4h | 48 | 47.81% | 0.0076 |

![Per-Regime Mean Forward Return vs Horizon](wave_optimise_BTCUSDT_best03_wave_fwd_return.png)


### Per-Regime Forward Return

Mean forward log-return $\mathbb{E}[f_t^{(h)} \mid \rho_t = R]$ and regime hit rate per horizon:

**Horizon $h=1$ bars (5min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169273 | 0.001% | 0.000% | 0.00% |
| BREAKOUT | 103 | -0.039% | -0.020% | 44.66% |
| BREAKDOWN | 171 | 0.094% | 0.002% | 45.03% |
| NEUTRAL | 853 | 0.014% | 0.011% | 0.00% |

**Horizon $h=3$ bars (15min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169271 | 0.002% | 0.001% | 0.00% |
| BREAKOUT | 103 | -0.101% | -0.022% | 48.54% |
| BREAKDOWN | 171 | 0.155% | 0.024% | 38.01% |
| NEUTRAL | 853 | 0.038% | 0.023% | 0.00% |

**Horizon $h=6$ bars (30min)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169268 | 0.004% | 0.002% | 0.00% |
| BREAKOUT | 103 | -0.046% | -0.019% | 48.54% |
| BREAKDOWN | 171 | 0.210% | 0.043% | 32.75% |
| NEUTRAL | 853 | 0.048% | 0.030% | 0.00% |

**Horizon $h=12$ bars (1h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169262 | 0.008% | 0.004% | 0.00% |
| BREAKOUT | 103 | 0.131% | 0.079% | 56.31% |
| BREAKDOWN | 171 | 0.357% | 0.063% | 30.99% |
| NEUTRAL | 853 | 0.096% | 0.023% | 0.00% |

**Horizon $h=24$ bars (2h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169250 | 0.017% | 0.010% | 0.00% |
| BREAKOUT | 103 | 0.530% | 0.542% | 74.76% |
| BREAKDOWN | 171 | 0.346% | 0.075% | 32.16% |
| NEUTRAL | 853 | 0.242% | 0.111% | 0.00% |

**Horizon $h=48$ bars (4h)**
| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 169226 | 0.034% | 0.016% | 0.00% |
| BREAKOUT | 103 | 1.108% | 1.711% | 82.52% |
| BREAKDOWN | 171 | 0.377% | 0.134% | 26.90% |
| NEUTRAL | 853 | 0.283% | 0.185% | 0.00% |

![Regime Transition Matrix](wave_optimise_BTCUSDT_best03_wave_transition.png)

### Regime Transition Matrix

Row-normalised probabilities $T_{ij} = P(\rho_{t+1}=j \mid \rho_t=i)$:

| From \ To | MEAN_REVERSION | BREAKOUT | BREAKDOWN | NEUTRAL |
|---|---|---|---|---|
| MEAN_REVERSION | 1.000 | 0.000 | 0.000 | 0.000 |
| BREAKOUT | 0.000 | 0.922 | 0.000 | 0.078 |
| BREAKDOWN | 0.000 | 0.000 | 0.971 | 0.029 |
| NEUTRAL | 0.075 | 0.008 | 0.001 | 0.916 |

### Regime Stability

Mean consecutive-bar run length per regime ($\approx 1/(1-T_{ii})$ from transition matrix diagonal):

| Regime | Mean Run (bars) |
|---|---|
| MEAN_REVERSION | 2604.2 |
| BREAKOUT | 12.9 |
| BREAKDOWN | 34.2 |
| NEUTRAL | 11.8 |

![Regime × Tide Bias Confusion](wave_optimise_BTCUSDT_best03_wave_confusion.png)

### Regime × Tide Bias Confusion

Mean forward log-return at $h=1$ for each $(\rho_t, b_t)$ combination (stacked mode only):

| Wave Regime \ Tide Bias | LONG | NEUTRAL | SHORT |
|---|---|---|---|
| BREAKDOWN | 0.000% | 0.050% | 0.392% |
| BREAKOUT | -0.044% | -0.025% | 0.000% |
| MEAN_REVERSION | 0.014% | 0.000% | -0.016% |
| NEUTRAL | 0.037% | 0.005% | -0.022% |

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
| disp_window | 96 |
| ar_window | 34 |
| disp_scale | 0.141 |
| eta_mr_threshold | 0.38 |
| eta_bo_threshold | 0.62 |
| eta_neutral_threshold | 0.49 |
| dispersion_threshold | 1.288 |
| dispersion_critical | 1.263 |
| ar_critical | 0.6 |
| ar_recover | 0.48 |
| reduced_size_fraction | 0.37 |
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
