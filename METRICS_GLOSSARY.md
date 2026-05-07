# Strategy Metric Glossary — Tide & Wave Layers

All metrics are produced by `tide/tide_metrics.py`, `tide/tide_accuracy.py`,
`wave/wave_metrics.py`, and `wave/wave_accuracy.py`.
Notation conventions are given first, then every metric is defined with both a
mathematical formula and a plain-language interpretation.

---

## Notation

| Symbol | Meaning |
|---|---|
| $r_t$ | Bar-level net return: `net_pnl[t] / equity[t-1]` |
| $E_t$ | Equity at end of bar $t$ |
| $E_0$ | Initial capital |
| $N$ | Total number of bars |
| $P$ | Periods per year: `(365 × 24 × 3600) / bar_seconds` (continuous crypto year) |
| $\bar{r}$ | Mean bar return: $\frac{1}{N}\sum_t r_t$ |
| $\sigma$ | Standard deviation of bar returns |
| $\sigma_d$ | Downside deviation: std of `min(r_t, 0)` values |
| $R_f$ | Annual risk-free rate (default 0 in a crypto context) |
| MDD | Maximum drawdown (negative fraction) |
| MAR | Minimum acceptable return (default 0) |

---

## Returns & Growth

### Total Return
**Formula:** $\displaystyle \text{Total Return} = \frac{E_N - E_0}{E_0}$

**Plain language:** The simple percentage gain or loss over the entire backtest
period, unadjusted for time.  A value of `+0.10` means the strategy grew the
portfolio by 10%.

---

### CAGR (Compound Annual Growth Rate)
**Formula:** $\displaystyle \text{CAGR} = \left(\frac{E_N}{E_0}\right)^{P/N} - 1$

**Plain language:** The constant annual rate at which the portfolio would have
needed to compound to reach the same final value.  Normalises performance for
comparison across runs of different lengths.  A CAGR of `+12%` means the
strategy grew at the same pace as an investment doubling roughly every 6 years.

---

### Avg Bar Return
**Formula:** $\bar{r} = \frac{1}{N}\sum_{t=1}^{N} r_t$

**Plain language:** The arithmetic average return earned per bar.  Useful for
sanity-checking whether the strategy is generating any edge at the granular
bar level before compounding.

---

## Risk Metrics

### Annualised Volatility
**Formula:** $\displaystyle \sigma_{\text{ann}} = \sigma(r) \times \sqrt{P}$

**Plain language:** How much the strategy's returns fluctuate, scaled to an
annual figure.  High volatility means large swings in portfolio value.
Analogous to the standard deviation of annual returns if you held a portfolio
that rebalances with every bar.

---

### Annualised Downside Volatility
**Formula:** $\displaystyle \sigma_{d,\text{ann}} = \sigma\!\left(\min(r_t, 0)\right) \times \sqrt{P}$

**Plain language:** Like annualised volatility but only counts negative returns.
Preferred by investors who do not penalise upside variance.  A strategy with
low downside vol and high total vol has many small losses but large occasional
gains.

---

### Maximum Drawdown (MDD)
**Formula:**
$$
\text{MDD} = \min_{t} \frac{E_t - \max_{s \leq t} E_s}{\max_{s \leq t} E_s}
$$

**Plain language:** The worst peak-to-trough decline experienced during the
backtest, expressed as a fraction of the prior peak.  A MDD of `-0.30` means
the portfolio fell 30% from its high before recovering.  The single most
intuitive risk number for a portfolio manager.

---

### MDD Duration
**Formula:** Number of bars from the equity peak that preceded the trough to
the bar where equity fully recovered (or end of series if never recovered).

**Plain language:** How long you were "underwater" at the worst drawdown.
A short duration means the strategy recovered quickly; a long duration
signals a prolonged adverse period, even if the drawdown percentage was small.

---

### Value at Risk (VaR 95 / VaR 99)
**Formula:** The $\alpha$-quantile of the empirical bar-return distribution:
$$
\text{VaR}_\alpha = -Q_\alpha(r_t)
$$
where $Q_\alpha$ is the $(1-\alpha)$ quantile (i.e. the 5th percentile for
VaR 95).

**Plain language:** On any given bar there is a $(1-\alpha)$ probability of
losing *at least* this much.  VaR 95 = `−0.005` means there is a 5% chance of
losing more than 0.5% in a single bar.  It is a threshold, not an average.

---

### Expected Shortfall / CVaR (ES 95 / ES 99)
**Formula:**
$$
\text{ES}_\alpha = -\mathbb{E}\!\left[r_t \mid r_t < Q_{1-\alpha}(r_t)\right]
$$

**Plain language:** The average loss *given* that you are in the worst
$(1-\alpha)\%$ of outcomes.  Always ≥ VaR.  ES 95 answers "when things go
badly wrong, how bad does it get on average?" — a more informative tail-risk
measure than VaR alone.

---

### Ulcer Index
**Formula:**
$$
\text{Ulcer} = \sqrt{\frac{1}{N}\sum_{t=1}^{N} D_t^2}, \quad
D_t = \frac{E_t - \max_{s \leq t} E_s}{\max_{s \leq t} E_s}
$$

**Plain language:** Measures both the depth and duration of drawdowns
simultaneously.  Named after the stomach distress caused by prolonged
losses.  A strategy that is 10% underwater for a long time scores much
higher than one that dips 10% briefly.  Lower is better.

---

### Return Skewness
**Formula:** Third standardised moment of $r_t$:
$$
\text{Skew} = \frac{1}{N}\sum_t \left(\frac{r_t - \bar{r}}{\sigma}\right)^3
$$

**Plain language:** Measures asymmetry of the return distribution.
- **Positive skew:** more small losses, occasional large gains (desirable).
- **Negative skew:** more small gains, occasional large losses (risk of ruin).
- Trend-following strategies typically have positive skew; mean-reversion
  strategies typically have negative skew.

---

### Excess Kurtosis
**Formula:** Fourth standardised moment minus 3 (the normal distribution
baseline):
$$
\text{Kurt}_{xs} = \frac{1}{N}\sum_t \left(\frac{r_t - \bar{r}}{\sigma}\right)^4 - 3
$$

**Plain language:** Measures how fat the tails of the return distribution are.
Positive excess kurtosis ("leptokurtic") means extreme events occur more
frequently than a normal distribution would predict.  Crypto markets
typically exhibit very high positive kurtosis.  A value near 0 indicates
Gaussian-like tails.

---

## Risk-Adjusted Metrics

### Sharpe Ratio
**Formula:**
$$
\text{Sharpe} = \frac{\bar{r} \cdot P - R_f}{\sigma \cdot \sqrt{P}}
= \frac{\text{CAGR} - R_f}{\sigma_{\text{ann}}}
$$

**Plain language:** Return earned per unit of total volatility, annualised.
The industry-standard risk-adjusted performance metric.  A Sharpe above 1.0
is generally considered good; above 2.0 is excellent.  It penalises both
upside and downside volatility equally, which can be misleading for
skewed strategies.

---

### Sortino Ratio
**Formula:**
$$
\text{Sortino} = \frac{\text{CAGR} - \text{MAR}}{\sigma_{d,\text{ann}}}
$$

**Plain language:** Like Sharpe but only penalises *downside* volatility.
Preferred for strategies with positive skew where upside variance should not
be penalised.  A Sortino meaningfully higher than the Sharpe signals a
right-skewed return distribution.

---

### Calmar Ratio
**Formula:**
$$
\text{Calmar} = \frac{\text{CAGR}}{|\text{MDD}|}
$$

**Plain language:** Return per unit of worst drawdown.  Particularly useful
for evaluating strategies that must respect hard drawdown limits.
A Calmar of 1.0 means the strategy earns its worst-ever drawdown back
in a year.  Higher is better; a Calmar below 0.5 is a warning sign.

---

### Omega Ratio (threshold = 0)
**Formula:**
$$
\Omega = \frac{\mathbb{E}[\max(r_t, 0)]}{\mathbb{E}[\max(-r_t, 0)]}
= \frac{\text{mean of all gains}}{\text{mean of all losses (absolute)}}
$$

**Plain language:** The ratio of average gain to average loss per bar,
regardless of frequency.  An Omega above 1.0 means the average gain
exceeds the average loss; above 1.5 is strong.  Unlike Sharpe it makes no
distributional assumption and captures all moments of the return distribution.

---

## Activity & Trade Metrics

### Rebalances
**Plain language:** Total number of bars on which the strategy changed its
position (buys or sells any amount).  High rebalance counts increase fee drag.

---

### Round Trips
**Plain language:** A complete open-then-close cycle: entering a directional
position and fully exiting (or reversing) it.  Equivalent to a "trade" in
traditional reporting.  One round trip can span many bars.

---

### Turnover per Year
**Formula:** $\displaystyle \text{Turnover/yr} = \frac{\text{Rebalances}}{N} \times P$

**Plain language:** How many times the portfolio is fully turned over
(repositioned) in a year on average.  Higher turnover incurs more fees.
A turnover of 10 means the strategy repositions the equivalent of the full
portfolio 10 times per year.

---

### Exposure %
**Formula:** Fraction of bars where `|position| > 0`.

**Plain language:** The percentage of time the strategy actually holds a
position (long or short).  A Tide-only overlay typically has low exposure
(often <10%) because NEUTRAL periods dominate.  Low exposure also means low
fee drag and reduced market risk.

---

### Win Rate
**Formula:** $\displaystyle \text{Win Rate} = \frac{\text{\# round trips with PnL} > 0} {\text{total round trips}}$

**Plain language:** The fraction of closed trades that made money.  A
strategy can be profitable with a win rate below 50% if wins are large enough
relative to losses (see Profit Factor and Expectancy).

---

### Profit Factor
**Formula:** $\displaystyle \text{PF} = \frac{\sum \text{gross wins}}{\sum |\text{gross losses}|}$

**Plain language:** Total money made on winning trades divided by total money
lost on losing trades.  A Profit Factor above 1.0 means the strategy makes
more than it loses in aggregate.  Above 1.5 is considered robust; above 2.0
is excellent.

---

### Expectancy per Round Trip
**Formula:** $\text{Expectancy} = \bar{P}_{\text{win}} \times W - |\bar{P}_{\text{loss}}| \times (1-W)$

where $W$ is win rate, $\bar{P}_{\text{win}}$ and $\bar{P}_{\text{loss}}$
are average win and loss PnL.

**Plain language:** The average amount the strategy expects to make (or lose)
per trade in dollar terms.  A positive expectancy is necessary for long-run
profitability.  E.g. Expectancy = +$50 means on average each closed trade
adds $50 to the account.

---

### Avg Win / Avg Loss
**Plain language:** Average dollar PnL of winning trades and losing trades
respectively.  The ratio `Avg Win / |Avg Loss|` (the "win-to-loss ratio")
combined with Win Rate determines Expectancy.

---

### Fees Paid
**Plain language:** Total fees charged over the backtest in dollars.  Includes
only taker or maker fees depending on the `use_taker_fees` flag.
Fees are charged on the notional size of the position change: `|Δunits| × price × fee_rate`.

---

## Breakdown Tables (Regime & Bias)

Each row in the regime or bias breakdown table shows the following for bars
where the strategy was in that regime/bias state:

| Column | Definition |
|---|---|
| **Bars** | Count of bars in this category |
| **Time %** | `bars / total_bars × 100` |
| **Net PnL** | Sum of `(gross_pnl - fee)` for all bars in this group |
| **Fees** | Sum of fees charged in this group |
| **Hit %** | % of bars with `bar_return > 0` |
| **Sharpe** | `mean(bar_return) / std(bar_return) × √P` within the group |

**Plain language:** These tables answer "where does the strategy make or lose
money?" — useful for diagnosing whether edge is regime-specific (e.g. only
works in LOW vol) or bias-specific (e.g. SHORT is unprofitable).

---

## Signal Accuracy Metrics

These metrics assess whether the Tide bias signal at time $t$ predicts
the direction of the market over the following $h$ bars.

For each horizon $h$, let:
- $c_t$ = close price at bar $t$
- $f_t^{(h)} = \log(c_{t+h} / c_t)$ = forward log-return over $h$ bars
- $s_t \in \{-1, 0, +1\}$ = bias sign at bar $t$ (SHORT, NEUTRAL, LONG)
- $\mathcal{A} = \{t : s_t \neq 0\}$ = active bars (LONG or SHORT only)

---

### Hit Rate %
**Formula:**
$$
\text{Hit Rate} = \frac{|\{t \in \mathcal{A} : s_t \cdot f_t^{(h)} > 0\}|}{|\mathcal{A}|} \times 100
$$

**Plain language:** The percentage of LONG/SHORT calls that correctly predicted
the direction of price movement over the next $h$ bars.  50% is chance; a
consistent reading above 55% is economically meaningful.  NEUTRAL bars are
excluded because they carry no directional claim.

---

### IC (Information Coefficient)
**Formula:** Pearson correlation of the bias signal against forward returns,
computed over **all** bars (including NEUTRAL = 0):
$$
\text{IC} = \text{corr}(s_t, f_t^{(h)})
$$

**Plain language:** How linearly related the signal is to future returns.
A value of 0 means no relationship; +0.05 is considered good in quantitative
finance (signals rarely exceed 0.1 IC in practice).  Even a small positive IC
is exploitable if it is persistent.

---

### Dir IC (Directional IC)
**Formula:** Same Pearson correlation, restricted to $t \in \mathcal{A}$:
$$
\text{Dir IC} = \text{corr}_{t \in \mathcal{A}}(s_t, f_t^{(h)})
$$

**Plain language:** IC computed only for bars where the strategy is taking a
directional stance.  More relevant than the full IC for sparse signals
(where many NEUTRAL bars would dilute the correlation).

---

### Expectancy
**Formula:**
$$
\text{Expectancy} = \frac{1}{|\mathcal{A}|}\sum_{t \in \mathcal{A}} s_t \cdot f_t^{(h)}
$$

**Plain language:** Average signed forward log-return per active bar.  Positive
means the signal earns positive log-returns on average when it takes a
position.  This is the raw per-bar edge before position sizing and fees.

---

### Calmar Proxy (bar-level Sharpe)
**Formula:**
$$
\text{Calmar proxy} = \frac{\text{Expectancy}}{\sigma(s_t \cdot f_t^{(h)})_{\mathcal{A}}}
$$

**Plain language:** The signal's Sharpe ratio at the bar level (expectancy
divided by its standard deviation).  Named "Calmar proxy" here because at the
bar level there is no equity curve to compute a proper Calmar.  A value above
0.05 indicates a statistically stable per-bar edge.

---

### PnL Proxy
**Formula:**
$$
\text{PnL proxy} = \sum_{t \in \mathcal{A}} s_t \cdot f_t^{(h)} \times q \times c_t
$$

where $q$ = `sizing_base` (1.0 in 1-BTC mode, or `max_position_usd` in USD
mode).

**Plain language:** The cumulative dollar (or BTC-move) value the signal would
have captured if you held exactly `sizing_base` units whenever the bias was
active.  In 1-BTC mode this reads directly as "the total price appreciation
in BTC terms captured by the signal" — a practical proxy for economic edge.
Note: fees are not deducted here; this is gross signal value, not net PnL.

---

### Confusion Matrix (Vol-regime × Bias)
**Formula:** For each `(regime, bias)` cell at $h=1$:
- **Mean signed forward return**: $\mathbb{E}[s_t \cdot f_t^{(1)}]$ for bars
  in that cell.
- **Hit rate %**: $\mathbb{E}[\mathbf{1}_{s_t \cdot f_t^{(1)} > 0}] \times 100$.

**Plain language:** A cross-tabulation answering "does the LONG signal work
equally well in LOW, NORMAL, and HIGH volatility regimes?"  Cells with high
hit rate and positive mean signed return indicate regime-bias combinations
where the Tide signal is genuinely informative.  This is essential for
deciding whether to filter signals by regime (e.g. "only take LONG calls in
LOW vol").

---

## Wave Regime Accuracy Metrics

These metrics assess the quality of the **Wave regime classification** over
forward horizons $h \in \{1, 2, \ldots, T\}$ bars.  They answer: "Does knowing
the Wave regime at time $t$ tell us anything useful about what price will do
over the next $h$ bars?"

For each bar $t$, let:
- $c_t$ = close price
- $f_t^{(h)} = \log(c_{t+h} / c_t)$ = forward log-return over $h$ bars
- $\rho_t \in \{\text{BREAKOUT}, \text{MEAN\_REVERSION}, \text{BREAKDOWN}, \text{NEUTRAL}\}$ = regime label
- $g(\rho_t) \in \{+1, 0, -1\}$ = regime signal proxy:
  - BREAKOUT → $+1$ (regime anticipates continued trend)
  - BREAKDOWN → $-1$ (regime anticipates instability / mean-reverting)
  - MEAN\_REVERSION, NEUTRAL → $0$ (no directional claim)
- $\mathcal{D} = \{t : g(\rho_t) \neq 0\}$ = directional bars (BREAKOUT + BREAKDOWN)

---

### Regime Hit Rate %
**Formula:**
$$
\text{Regime Hit Rate} = \frac{|\{t \in \mathcal{D} : g(\rho_t) \cdot f_t^{(h)} > 0\}|}{|\mathcal{D}|} \times 100
$$

**Plain language:** The percentage of BREAKOUT and BREAKDOWN bars where the
regime correctly predicted the direction of the forward price move.  50% is
random chance.  A BREAKOUT bar "wins" if the price moved up over the next $h$
bars; a BREAKDOWN bar "wins" if the price moved down (or was more volatile).
MEAN\_REVERSION and NEUTRAL bars make no directional claim and are excluded.

---

### Regime IC (Information Coefficient)
**Formula:** Pearson correlation of the regime signal against forward returns,
over all bars:
$$
\text{Regime IC} = \text{corr}(g(\rho_t),\; f_t^{(h)})
$$

**Plain language:** How linearly related the regime classification is to future
price returns.  A regime IC of zero means the regime labels carry no
information about forward price direction.  A small positive IC (+0.02 to +0.05)
is economically meaningful in practice.  Negative IC means the regime labels
are systematically wrong — which itself is information (flip the signal).

---

### Per-Regime Mean / Median Forward Return
**Formula:** For each regime $R \in \{\text{BREAKOUT, MR, BREAKDOWN, NEUTRAL}\}$:
$$
\mu_h^{(R)} = \mathbb{E}\!\left[f_t^{(h)} \;\middle|\; \rho_t = R\right]
$$

**Plain language:** The average log-return over the next $h$ bars, conditional
on the current regime being $R$.  Tells you the regime's *unconditional* forward
return bias:
- Persistent BREAKOUT with positive $\mu^{(\text{BREAKOUT})}$ validates the regime is a
  trend identifier.
- Persistent BREAKDOWN with negative $\mu^{(\text{BREAKDOWN})}$ validates it as a
  risk-off / reversal identifier.

---

### Regime Transition Matrix
**Formula:**
$$
T_{ij} = P(\rho_{t+1} = j \mid \rho_t = i) = \frac{\text{count}(\rho_t=i,\; \rho_{t+1}=j)}{\text{count}(\rho_t=i)}
$$

**Plain language:** A $4 \times 4$ row-normalised probability matrix showing
how likely each regime is to persist or transition to another on the next bar.
Key diagnostics:
- **High diagonal values** (e.g. $T_{\text{BREAKDOWN,BREAKDOWN}} = 0.97$) mean the
  regime is sticky — once entered, it stays for many bars.
- **Off-diagonal spikes** reveal which regimes naturally follow each other
  (e.g. BREAKOUT → NEUTRAL is common when a trend exhausts).
- Low-stability regimes (frequent transitions) may be too noisy to trade
  reliably.

---

### Regime Stability (Mean Run Length)
**Formula:**
$$
\text{Stability}(R) = \mathbb{E}[\text{consecutive bars in regime } R]
$$

**Plain language:** The average number of consecutive bars the strategy spends
in each regime before transitioning.  A stability of 20 bars means the regime
persists for roughly 20 bar-widths on average.  This is practically important:
- **High stability** → signals are persistent and tradeable (fewer whipsaws).
- **Low stability** → the regime is noisy; consider a wider smoothing window.

The transition matrix and stability are two views of the same thing: a diagonal
transition probability of 0.95 implies a mean run length of $1/(1-0.95) = 20$ bars.

---

### Regime × Tide Bias Confusion (stacked mode only)
**Formula:** In stacked mode, for each `(WaveRegime, TideBias)` cell at $h=1$:
$$
\mu^{(\rho, b)} = \mathbb{E}\!\left[f_t^{(1)} \;\middle|\; \rho_t = \rho,\; b_t = b\right]
$$

**Plain language:** When the Tide is saying LONG *and* the Wave says BREAKOUT,
what is the average next-bar return?  This two-dimensional breakdown reveals
regime-bias interactions:
- Strong positive cells (e.g. BREAKOUT + LONG) confirm the layers are
  reinforcing each other correctly.
- Weak or negative cells (e.g. BREAKOUT + SHORT) indicate conflicting signals
  — a useful risk filter.
- In isolated mode this table is omitted because TideBias is always NEUTRAL.

---

## Wave Layer — Position Sizing

The Wave backtester uses a **wave_size_fraction** to scale positions based on
regime, independent of Tide's risk multiplier:

| Wave Regime | Size Fraction | Rationale |
|---|---|---|
| BREAKOUT | 1.0 (full size) | Trend-following; high-conviction directional environment |
| MEAN\_REVERSION | `reduced_size_fraction` | Smaller; counter-trend trades carry more adverse-selection risk |
| NEUTRAL | `reduced_size_fraction` | Conservative; no strong regime signal |
| BREAKDOWN | 0.0 (flat) | All archetypes off; market instability, await recovery |

**Combined sizing (stacked mode):**
$$
\text{target\_units} = \text{tide\_bias\_sign} \times \text{tide\_risk\_mult} \times \text{wave\_size\_fraction} \times \text{max\_position}
$$

**Isolated mode direction proxy** (TideBias = NEUTRAL, so direction is derived from VWAP):

| Regime | Close vs VWAP | Direction |
|---|---|---|
| BREAKOUT | above VWAP | LONG (+1) |
| BREAKOUT | below VWAP | SHORT (−1) |
| MEAN\_REVERSION | above VWAP | SHORT (−1, fade) |
| MEAN\_REVERSION | below VWAP | LONG (+1, fade) |
| BREAKDOWN / NEUTRAL | — | Flat (0) |

---

## Parameters

### Tide Parameters

| Parameter | Meaning |
|---|---|
| `vol_window` | Rolling window (bars) for realized volatility computation |
| `eta_window` | Rolling window (bars) for directional efficiency ratio η |
| `lsi_window` | Rolling window (bars) for liquidity stress index z-scores |
| `timeframe` | Bar width: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `12h`, `1d` |
| `bar_seconds` | Bar width in seconds (auto-derived from `timeframe`) |
| `vol_regime_thresholds` | Three ascending annualised vol levels that separate LOW / NORMAL / HIGH / CRISIS |
| `eta_threshold` | Min directional efficiency ratio η required to assign LONG or SHORT bias (below → NEUTRAL) |
| `bias_deadband` | Min absolute net price move required to assign direction (prevents noise flips near zero) |
| `lsi_weights` | Weights summing to 1 for the three LSI proxy components: intrabar-range z-score, vol-of-vol z-score, volume z-score |
| `risk_mult_by_regime` | Risk multiplier (fraction of `max_position`) for [LOW, NORMAL, HIGH, CRISIS] regimes |
| `lsi_reduce_threshold` | LSI z-score above which the risk multiplier is further reduced |
| `lsi_reduce_slope` | Rate at which excess LSI reduces the risk multiplier (linear penalty per unit above threshold) |
| `es_budget_global` | Global ES budget in dollars published to downstream layers (not used in Tide-only backtest sizing) |
| `sizing_mode` | `usd`: position in dollars / price. `base`: fixed units of the base asset (e.g. 1 BTC) |
| `max_position_usd` | Maximum notional exposure in USD (used in `usd` sizing mode) |
| `max_position_base` | Maximum exposure in base-asset units (used in `base` sizing mode; 1.0 = 1 BTC) |
| `leverage` | Leverage multiplier applied to the target position |
| `taker_fee_bps` | Taker fee in basis points (1 bps = 0.01%) |
| `maker_fee_bps` | Maker fee in basis points |
| `rebalance_threshold` | Minimum fractional position change required to trigger a rebalance (avoids micro-trades) |
| `accuracy_horizons` | List of forward horizons (in bars) used in the signal accuracy analysis |

---

### Wave Parameters

#### Feature Pipeline

| Parameter | Meaning |
|---|---|
| `eta_window` | Rolling window (bars) for trend efficiency η = \|ΣΔP\| / Σ\|ΔP\|. Wider → smoother signal, slower to react. |
| `vwap_window` | Rolling window (bars) for VWAP. Acts as the primary structural reference level. |
| `structure_window` | Rolling window (bars) for session high/low (structural support/resistance proxy). |
| `disp_window` | Rolling window (bars) for dispersion proxy (rolling std of log-returns). |
| `ar_window` | Slow window (bars) for absorption ratio proxy. Should be 2–4× larger than `disp_window`. |
| `disp_scale` | Normalisation divisor for dispersion proxy (roughly one annualised vol unit, e.g. `0.05` = 5%). Adjust so proxy sits in [0, 2] on your data. |

#### Regime State Machine Thresholds

| Parameter | Meaning |
|---|---|
| `eta_mr_threshold` | η below this → eligible for MEAN\_REVERSION. Typical range: 0.1–0.5. |
| `eta_bo_threshold` | η above this → eligible for BREAKOUT. Typical range: 0.5–0.95. |
| `eta_neutral_threshold` | Hysteresis boundary for exiting MR/BREAKOUT back to NEUTRAL. Must satisfy `eta_mr < eta_neutral < eta_bo`. |
| `dispersion_threshold` | Dispersion proxy must be below this to enter MEAN\_REVERSION. |
| `dispersion_critical` | Dispersion proxy above this → forced BREAKDOWN (extreme spread = market dislocation). |
| `ar_critical` | AR proxy above this → forced BREAKDOWN (vol-of-vol spike = systemic instability). |
| `ar_recover` | AR proxy must fall below this (and dispersion below threshold) to exit BREAKDOWN → NEUTRAL. |
| `reduced_size_fraction` | Position size fraction used in MEAN\_REVERSION and NEUTRAL regimes (relative to full BREAKOUT size). Default 0.5. |
| `update_interval_ms` | Minimum ms between WaveEngine updates (cadence gate). Set 0 to update every bar. |

#### Portfolio / Sizing (identical semantics to Tide)

| Parameter | Meaning |
|---|---|
| `sizing_mode` | `base`: fixed units (e.g. 1 BTC). `usd`: notional dollars. |
| `max_position_base` | Max BTC-equivalent position when `sizing_mode=base`. |
| `max_position_usd` | Max dollar notional when `sizing_mode=usd`. |
| `initial_capital` | Starting portfolio value in dollars. |
| `leverage` | Multiplier applied to target position (default 1.0). |
| `taker_fee_bps` | Taker fee in basis points. |
| `maker_fee_bps` | Maker fee in basis points. |
| `rebalance_threshold` | Minimum fractional position change to trigger a trade. |

#### Run Metadata

| Parameter | Meaning |
|---|---|
| `symbol` | Trading symbol (e.g. `BTCUSDT`). |
| `exchange` | Exchange name matching the HDF5 dataset key (e.g. `binance`). |
| `timeframe` | Bar resolution: `1h`, `4h`, `12h`, `1d`. Optimisable. |
| `mode` | `isolated` (TideBias = NEUTRAL throughout) or `stacked` (Tide signals consumed). |
| `accuracy_horizons` | Forward horizons (bars) for regime accuracy analysis. |

---

## Wave Feature Definitions

### Trend Efficiency η (§8.5.4)
**Formula:**
$$
\eta_t = \frac{\left|\sum_{i=t-W+1}^{t} \Delta P_i\right|}{\sum_{i=t-W+1}^{t} |\Delta P_i|}
$$
where $\Delta P_i = P_i - P_{i-1}$ and $W$ = `eta_window`.

**Plain language:** Net price displacement divided by total path length over the
last $W$ bars.  $\eta = 1$ means price moved in a straight line (pure trend);
$\eta = 0$ means price reversed on every bar (pure oscillation).  This is the
primary regime discriminator driving BREAKOUT vs MEAN\_REVERSION classification.

---

### Dispersion Proxy D (§8.5.2, V1 single-symbol)
**Formula:**
$$
D_t = \frac{\sigma(\log r,\; W_D)}{\text{disp\_scale}}
$$
where $\sigma(\cdot, W_D)$ is rolling standard deviation of log-returns over
`disp_window` bars.

**Plain language:** In a multi-asset context, true dispersion measures the
spread of returns across $N$ assets simultaneously.  In V1 single-symbol mode
we approximate this as intra-asset log-return volatility.  High dispersion
indicates a dislocated or erratic market.  The `disp_scale` normalises the
proxy to sit in a [0, ~2] range comparable to the engine's thresholds.
Multi-asset true dispersion is planned for V2/V3.

---

### Absorption Ratio Proxy AR (§8.5.3, V1 single-symbol)
**Formula:**
$$
\text{AR}_t = \text{clip}\!\left(\frac{\sigma_{\text{fast}}(W/4)}{\sigma_{\text{slow}}(W)},\; 0,\; 1\right)
$$
where $\sigma$ is rolling log-return std with windows `ar_window/4` (fast) and `ar_window` (slow).

**Plain language:** The true absorption ratio (from Kritzman et al.) measures
the fraction of total portfolio variance explained by the dominant PCA eigenvector.
In V1 single-symbol mode we approximate it as recent vol / long-run vol: when
short-term volatility spikes above its long-run baseline the market is in an
unusual stressed state (analogous to high eigenvalue concentration).  Values
above `ar_critical` force BREAKDOWN.  True multi-factor PCA is planned for V3.

---

### Rolling VWAP (§8.5.5)
**Formula:**
$$
\text{VWAP}_t = \frac{\sum_{i=t-W+1}^{t} \frac{H_i + L_i + C_i}{3} \cdot V_i}{\sum_{i=t-W+1}^{t} V_i}
$$
where $H$, $L$, $C$, $V$ are high/low/close/volume and $W$ = `vwap_window`.

**Plain language:** Volume-weighted average price over the rolling window.
Acts as the primary structural reference level: price above VWAP signals
bullish control; below signals bearish.  In isolated Wave mode it drives the
directional signal for BREAKOUT (momentum) and MEAN\_REVERSION (fade) bars.

---

### Distance to VWAP δ (§8.5.5)
**Formula:**
$$
\delta_{\text{vwap},t} = \frac{P_t - \text{VWAP}_t}{P_t}
$$

**Plain language:** Fractional price distance from the rolling VWAP.  Positive
means price is stretched above its volume-weighted anchor (mean-reversion
target from above); negative means below.  Published in the Wave bars DataFrame
for downstream Ripple entry-level targeting.
