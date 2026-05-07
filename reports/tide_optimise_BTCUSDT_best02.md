# Tide Overlay Backtest — BTCUSDT

- **Exchange / TF:** binance / `4h`
- **Period:** 2022-12-31 04:00:00 → 2024-08-13 20:00:00  (3,551 bars)
- **Capital:** $10,000.00 → $15,003.72  (**50.04%**)

## Headline metrics

| Metric | Value | Metric | Value |
|---|---:|---|---:|
| CAGR        | 28.44%  | Max Drawdown      | -2.61% |
| Sharpe      | 2.736  | MDD duration      | 1,190 bars |
| Sortino     | 6.524  | MDD trough        | 2024-02-28 00:00:00 |
| Calmar      | 10.906  | Ann Volatility    | 9.30% |
| Omega (>0)  | 1.957  | Ann Downside Vol  | 3.90% |
| VaR 95 / 99 | -0.000% / 0.411%  | ES 95 / 99        | 0.013% / 0.683% |
| Ulcer Index | 1.550%  | Avg bar return    | 0.0116% |
| Skewness    | 8.689  | Kurtosis (excess) | 137.312 |

## Activity

| Metric | Value | Metric | Value |
|---|---:|---|---:|
| Rebalances     | 48 | Round-trips      | 16 |
| Turnover / year| 29.6 | Exposure         | 8.70% |
| Win rate       | 56.25% | Profit factor    | 13.61 |
| Avg win        | $605.94 | Avg loss         | $-57.24 |
| Largest win    | $2,091.01 | Largest loss     | $-152.48 |
| Expectancy/trip| $315.80 | Fees paid        | $101.21 |
| Avg |position| | 0.0243 |  |  |

## Volatility-regime breakdown

| Category | Bars | Time % | Net PnL | Fees | Hit % | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| LOW | 722 | 20.33% | $2,691.04 | $35.86 | 11.63% | 5.054 |
| NORMAL | 929 | 26.16% | $2,318.87 | $59.17 | 9.58% | 3.384 |
| HIGH | 1,900 | 53.51% | $-6.18 | $6.18 | 0.00% | -1.514 |

## Bias breakdown

| Category | Bars | Time % | Net PnL | Fees | Hit % | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| LONG | 433 | 12.19% | $4,306.48 | $36.53 | 30.72% | 7.390 |
| SHORT | 144 | 4.06% | $740.06 | $21.86 | 27.78% | 5.158 |
| NEUTRAL | 2,974 | 83.75% | $-42.82 | $42.82 | 0.00% | -3.194 |

## Monthly returns

- **Best month:** 19.56%    **Worst month:** -1.03%    **% positive:** 40.00%

| Month | Return |
|---|---:|
| 2023-01 | 19.56% |
| 2023-02 | 0.71% |
| 2023-03 | 0.00% |
| 2023-04 | 0.00% |
| 2023-05 | 0.00% |
| 2023-06 | 3.15% |
| 2023-07 | 0.00% |
| 2023-08 | 5.82% |
| 2023-09 | -0.26% |
| 2023-10 | 11.28% |
| 2023-11 | 0.00% |
| 2023-12 | 1.18% |
| 2024-01 | 0.00% |
| 2024-02 | 1.92% |
| 2024-03 | 0.00% |
| 2024-04 | 0.00% |
| 2024-05 | 0.00% |
| 2024-06 | 0.77% |
| 2024-07 | -1.03% |
| 2024-08 | 0.00% |

## Tide signal accuracy

Forward-horizon analysis: is the bias at time *t* predictive for time *t+h*?

| h | Active | Neutral | Hit % | IC | Dir IC | Expectancy | Calmar | PnL proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 577 | 2,973 | 53.38% | 0.0502 | 0.0831 | 0.001400 | 0.115 | 8,075.1650 |
| 2 | 577 | 2,972 | 53.03% | 0.0714 | 0.1217 | 0.002823 | 0.163 | 16,288.5015 |
| 4 | 577 | 2,970 | 53.90% | 0.0915 | 0.1381 | 0.005241 | 0.208 | 30,240.9690 |
| 8 | 577 | 2,966 | 56.33% | 0.0933 | 0.1147 | 0.008018 | 0.235 | 46,266.2761 |
| 12 | 577 | 2,962 | 58.23% | 0.0920 | 0.0751 | 0.010155 | 0.240 | 58,593.7701 |
| 24 | 577 | 2,950 | 59.10% | 0.0528 | -0.0851 | 0.011048 | 0.169 | 63,749.6446 |

### Vol-regime × Bias — mean signed forward log-return (h=1)

| Regime / Bias | LONG | NEUTRAL | SHORT |
|---|---:|---:|---:|
| **HIGH** | 0.00080 | 0.00000 | 0.00006 |
| **LOW** | 0.00324 | 0.00000 | 0.00148 |
| **NORMAL** | 0.00184 | 0.00000 | 0.00087 |

### Hit rate % per cell (h=1)

| Regime / Bias | LONG | NEUTRAL | SHORT |
|---|---:|---:|---:|
| **HIGH** | 54.1% | 0.0% | 38.9% |
| **LOW** | 62.8% | 0.0% | 56.5% |
| **NORMAL** | 53.5% | 0.0% | 50.0% |

## Charts

### Equity curve

![Equity curve](tide_optimise_BTCUSDT_best02_equity.png)

### Drawdown

![Drawdown](tide_optimise_BTCUSDT_best02_drawdown.png)

### Regime overlay

![Regime overlay](tide_optimise_BTCUSDT_best02_regime.png)

### Signal timeline

![Signal timeline](tide_optimise_BTCUSDT_best02_accuracy_timeline.png)

### Hit rate vs horizon

![Hit rate vs horizon](tide_optimise_BTCUSDT_best02_accuracy_hitrate.png)

### Forward return by bias

![Forward return by bias](tide_optimise_BTCUSDT_best02_accuracy_fwdret.png)

### Regime-bias confusion

![Regime-bias confusion](tide_optimise_BTCUSDT_best02_accuracy_confusion.png)

### PnL proxy vs horizon

![PnL proxy vs horizon](tide_optimise_BTCUSDT_best02_accuracy_pnl.png)

## Parameters

| Parameter | Value |
|---|---:|
| `vol_window` | 240 |
| `eta_window` | 47 |
| `lsi_window` | 454 |
| `timeframe` | 4h |
| `bar_seconds` | 14400 |
| `vol_regime_thresholds` | [0.37, 0.42, 1.05] |
| `eta_threshold` | 0.33 |
| `bias_deadband` | 22.9 |
| `lsi_weights` | [0.333333, 0.333333, 0.333333] |
| `risk_mult_by_regime` | [0.79, 0.76, 0, 0] |
| `lsi_reduce_threshold` | 3.67 |
| `lsi_reduce_slope` | 0.022 |
| `es_budget_global` | 1000 |
| `sizing_mode` | usd |
| `max_position_usd` | 10000 |
| `max_position_base` | 1 |
| `leverage` | 1 |
| `taker_fee_bps` | 4 |
| `maker_fee_bps` | 2 |
| `rebalance_threshold` | 0.0223 |


---

## Metric glossary

> Full derivations and plain-language definitions are in `METRICS_GLOSSARY.md`
> at the repository root.  The compact reference below covers every number in
> this report.

### Returns & growth

| Metric | Formula | Plain language |
|---|---|---|
| **Total Return** | (E_N − E_0) / E_0 | Simple % gain/loss over the full period, unadjusted for time |
| **CAGR** | (E_N/E_0)^(P/N) − 1 | Constant annual rate that produces the same terminal value; normalised for run length |
| **Avg bar return** | mean(r_t) | Arithmetic average return per bar; sanity check for per-bar edge |

### Risk

| Metric | Formula | Plain language |
|---|---|---|
| **Ann Volatility** | std(r) × √P | Annualised return fluctuation; penalises both up and down moves |
| **Ann Downside Vol** | std(min(r,0)) × √P | Volatility of losses only; preferred for skewed strategies |
| **Max Drawdown** | min( (E_t − peak_t) / peak_t ) | Worst peak-to-trough decline as a fraction of the prior peak |
| **MDD Duration** | bars from peak → recovery (or end) | How long the portfolio was underwater at its worst drawdown |
| **VaR 95 / 99** | −Q_(0.05/0.01)(r) | Worst loss exceeded on 5%/1% of bars; a threshold, not an average |
| **ES 95 / 99** | −E[r | r < VaR] | Average loss in the worst 5%/1% of bars; more informative tail measure than VaR |
| **Ulcer Index** | √( mean(D_t²) ) | RMS drawdown depth; captures both depth and duration of underwater periods |
| **Skewness** | 3rd standardised moment | +ve = rare large gains; −ve = rare large losses (left-tail risk) |
| **Kurtosis (excess)** | 4th moment − 3 | Fat-tailedness; +ve means extreme events are more frequent than a normal distribution predicts |

### Risk-adjusted

| Metric | Formula | Plain language |
|---|---|---|
| **Sharpe** | CAGR / σ_ann | Return per unit of total volatility; > 1 good, > 2 excellent |
| **Sortino** | CAGR / σ_d,ann | Return per unit of downside volatility; better for right-skewed strategies |
| **Calmar** | CAGR / |MDD| | Return per unit of worst drawdown; useful for hard drawdown-limit mandates |
| **Omega (>0)** | E[max(r,0)] / E[max(−r,0)] | Mean gain / mean loss per bar; no distributional assumption; > 1 means gains exceed losses |

### Activity & trades

| Metric | Formula | Plain language |
|---|---|---|
| **Rebalances** | count of position changes | Each change incurs fee drag |
| **Round trips** | count of open→close trade cycles | Equivalent to "number of trades" |
| **Turnover / yr** | rebalances × P / N | Times the full portfolio is repositioned per year |
| **Exposure %** | bars with position ≠ 0, as % | Time actually in the market; Tide overlays are typically sparse (< 15%) |
| **Win rate** | % round trips with PnL > 0 | A strategy can profit with < 50% win rate if wins are large |
| **Profit factor** | gross wins / gross losses | > 1 makes money overall; > 1.5 considered robust |
| **Expectancy / trip** | mean(PnL per round trip) | Average $ earned per trade; must be positive for long-run profitability |
| **Avg win / loss** | mean of winning / losing trip PnL | Combined with win rate determines expectancy |
| **Fees paid** | Σ(|Δunits| × price × fee_rate) | Total cost of trading over the backtest |

### Signal accuracy (forward-horizon analysis)

Each row corresponds to a forward horizon h (bars).
NEUTRAL bars are always excluded from directional accuracy metrics.

| Metric | Formula | Plain language |
|---|---|---|
| **Active** | count(LONG or SHORT bars) | Bars where the strategy took a directional stance |
| **Neutral** | count(NEUTRAL bars) | Bars where Tide withheld a call |
| **Hit %** | % active bars where sign(bias) = sign(fwd_return_h) | Directional accuracy; 50% = chance level |
| **IC** | Pearson(bias_sign, fwd_log_return_h) over all bars | Linear signal-to-return correlation; > 0.05 is considered good |
| **Dir IC** | Same Pearson, restricted to active bars | IC for the signal when it is actually active |
| **Expectancy** | mean(bias_sign × fwd_log_return_h) over active bars | Raw per-bar edge in log-return units before sizing and fees |
| **Calmar proxy** | Expectancy / std(signed_fwd) | Bar-level Sharpe of the raw signal |
| **PnL proxy** | Σ(bias_sign × fwd_return_h × sizing_base × close) | Gross cumulative value captured by the signal at 1× sizing; fees not deducted |

#### Confusion matrix

Shows mean signed forward return and hit rate for every (vol_regime × bias)
combination at h = 1.  Identifies which regime-bias combinations drive edge
and which are noise — useful for regime-filtering rules.

### Regime / bias breakdown columns

| Column | Meaning |
|---|---|
| **Bars** | Bar count in this group |
| **Time %** | Fraction of total backtest time in this group |
| **Net PnL** | Σ(gross_pnl − fee) for all bars in this group |
| **Fees** | Total fees charged in this group |
| **Hit %** | % of bars with bar_return > 0 within the group |
| **Sharpe** | mean(r) / std(r) × √P within the group |

