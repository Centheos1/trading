# Tide Overlay Backtest — BTCUSDT

- **Exchange / TF:** binance / `4h`
- **Period:** 2022-12-31 04:00:00 → 2024-08-13 20:00:00  (3,551 bars)
- **Capital:** $10,000.00 → $22,590.43  (**125.90%**)

## Headline metrics

| Metric | Value | Metric | Value |
|---|---:|---|---:|
| CAGR        | 65.32%  | Max Drawdown      | -25.73% |
| Sharpe      | 1.535  | MDD duration      | 511 bars |
| Sortino     | 2.576  | MDD trough        | 2024-08-06 12:00:00 |
| Calmar      | 2.539  | Ann Volatility    | 37.20% |
| Omega (>0)  | 1.310  | Ann Downside Vol  | 22.16% |
| VaR 95 / 99 | 0.432% / 2.004%  | ES 95 / 99        | 1.565% / 3.703% |
| Ulcer Index | 12.951%  | Avg bar return    | 0.0261% |
| Skewness    | 3.030  | Kurtosis (excess) | 70.430 |

## Activity

| Metric | Value | Metric | Value |
|---|---:|---|---:|
| Rebalances     | 138 | Round-trips      | 42 |
| Turnover / year| 85.1 | Exposure         | 15.38% |
| Win rate       | 35.71% | Profit factor    | 2.18 |
| Avg win        | $1,620.02 | Avg loss         | $-412.85 |
| Largest win    | $5,244.23 | Largest loss     | $-3,267.46 |
| Expectancy/trip| $313.18 | Fees paid        | $1,323.60 |
| Avg |position| | 0.1265 |  |  |

## Volatility-regime breakdown

| Category | Bars | Time % | Net PnL | Fees | Hit % | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| LOW | 1,546 | 43.54% | $11,639.39 | $550.15 | 5.30% | 2.963 |
| NORMAL | 1,873 | 52.75% | $2,119.83 | $674.21 | 9.88% | 0.738 |
| HIGH | 132 | 3.72% | $-1,168.80 | $99.25 | 12.12% | -2.502 |

## Bias breakdown

| Category | Bars | Time % | Net PnL | Fees | Hit % | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| LONG | 436 | 12.28% | $20,182.07 | $551.66 | 53.90% | 6.389 |
| NEUTRAL | 3,004 | 84.60% | $-558.39 | $558.39 | 0.00% | -5.352 |
| SHORT | 111 | 3.13% | $-7,033.25 | $213.56 | 43.24% | -8.206 |

## Monthly returns

- **Best month:** 49.38%    **Worst month:** -14.72%    **% positive:** 45.00%

| Month | Return |
|---|---:|
| 2023-01 | 49.38% |
| 2023-02 | -4.08% |
| 2023-03 | -9.22% |
| 2023-04 | -6.95% |
| 2023-05 | 0.00% |
| 2023-06 | 6.78% |
| 2023-07 | -0.34% |
| 2023-08 | 1.51% |
| 2023-09 | -0.28% |
| 2023-10 | 27.71% |
| 2023-11 | 0.68% |
| 2023-12 | 5.55% |
| 2024-01 | 0.00% |
| 2024-02 | 37.71% |
| 2024-03 | 13.65% |
| 2024-04 | 0.00% |
| 2024-05 | 3.93% |
| 2024-06 | -3.57% |
| 2024-07 | -4.56% |
| 2024-08 | -14.72% |

## Tide signal accuracy

Forward-horizon analysis: is the bias at time *t* predictive for time *t+h*?

| h | Active | Neutral | Hit % | IC | Dir IC | Expectancy | Calmar | PnL proxy |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 547 | 3,003 | 52.10% | 0.0285 | 0.0066 | 0.000921 | 0.075 | 16,183.9161 |
| 2 | 547 | 3,002 | 51.19% | 0.0354 | -0.0160 | 0.001677 | 0.099 | 29,244.4464 |
| 4 | 547 | 3,000 | 54.84% | 0.0446 | -0.0210 | 0.003107 | 0.132 | 52,454.7167 |
| 8 | 547 | 2,996 | 56.49% | 0.0434 | -0.0799 | 0.004838 | 0.135 | 88,289.3044 |
| 24 | 547 | 2,980 | 60.33% | 0.0233 | -0.2541 | 0.008186 | 0.119 | 188,300.2429 |

### Vol-regime × Bias — mean signed forward log-return (h=1)

| Regime / Bias | LONG | NEUTRAL | SHORT |
|---|---:|---:|---:|
| **HIGH** | -0.00106 | 0.00000 | -0.02119 |
| **LOW** | 0.00416 | 0.00000 | -0.00044 |
| **NORMAL** | 0.00092 | 0.00000 | -0.00189 |

### Hit rate % per cell (h=1)

| Regime / Bias | LONG | NEUTRAL | SHORT |
|---|---:|---:|---:|
| **HIGH** | 58.6% | 0.0% | 0.0% |
| **LOW** | 59.1% | 0.0% | 46.7% |
| **NORMAL** | 52.2% | 0.0% | 42.0% |

## Parameters

| Parameter | Value |
|---|---:|
| `vol_window` | 60 |
| `eta_window` | 60 |
| `lsi_window` | 240 |
| `timeframe` | 4h |
| `bar_seconds` | 14400 |
| `vol_regime_thresholds` | [0.4, 0.8, 1.5] |
| `eta_threshold` | 0.3 |
| `bias_deadband` | 0 |
| `lsi_weights` | [0.333333, 0.333333, 0.333333] |
| `risk_mult_by_regime` | [1, 0.8, 0.5, 0] |
| `lsi_reduce_threshold` | 1.5 |
| `lsi_reduce_slope` | 0.2 |
| `es_budget_global` | 1000 |
| `sizing_mode` | base |
| `max_position_usd` | 10000 |
| `max_position_base` | 1 |
| `leverage` | 1 |
| `taker_fee_bps` | 4 |
| `maker_fee_bps` | 2 |
| `rebalance_threshold` | 0 |


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

