"""
Tide backtest report writer.

Produces three deliverables for any :class:`TidePerformanceReport`:

    1. Plain-text summary       — for stdout / log.
    2. Markdown report          — for ``reports/<run>.md``.
    3. PNG plots                — equity curve, drawdown, regime overlay.

The markdown layout is meant to be the kind of one-pager a portfolio
manager would skim:  headline KPIs at the top, full risk + activity
table, then per-regime / per-bias breakdown, then monthly returns and
parameters echoed for traceability.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from tide.tide_backtest import TideBacktestResult
from tide.tide_metrics import TidePerformanceReport, compute_report

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Formatters
# ────────────────────────────────────────────────────────────────────────

def _fmt_pct(x: float, dp: int = 2) -> str:
    if not np.isfinite(x):
        return "n/a"
    return f"{x * 100:.{dp}f}%"


def _fmt_pct_already(x: float, dp: int = 2) -> str:
    """For values already expressed as percent (e.g. exposure 73.5)."""
    if not np.isfinite(x):
        return "n/a"
    return f"{x:.{dp}f}%"


def _fmt_num(x: float, dp: int = 4) -> str:
    if x == float("inf"):
        return "+inf"
    if x == float("-inf"):
        return "-inf"
    if not np.isfinite(x):
        return "n/a"
    return f"{x:.{dp}f}"


def _fmt_int(x: int) -> str:
    return f"{int(x):,}"


def _fmt_money(x: float, dp: int = 2) -> str:
    if not np.isfinite(x):
        return "n/a"
    return f"${x:,.{dp}f}"


def _fmt_ts(ts: Optional[pd.Timestamp]) -> str:
    if ts is None:
        return "n/a"
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ────────────────────────────────────────────────────────────────────────
# Text + markdown rendering
# ────────────────────────────────────────────────────────────────────────

_HEADLINE = """\
{symbol} Tide-Overlay Backtest
{ruler}
Period      : {start}  →  {end}  ({bars} bars @ {tf})
Capital     : {init} → {final}   ({total_ret})
CAGR        : {cagr}
Sharpe      : {sharpe} (Sortino {sortino}, Calmar {calmar}, Omega {omega})
Max DD      : {mdd}  (duration {mdd_dur} bars, trough {mdd_ts})
Ann Vol     : {avol}    Downside Vol: {dvol}
VaR 95 / 99 : {var95} / {var99}
ES  95 / 99 : {es95} / {es99}
Ulcer       : {ulcer}    Skew: {skew}    Kurt(excess): {kurt}
"""


def render_text(report: TidePerformanceReport) -> str:
    """One-page plain-text summary (returned, not printed)."""
    head = _HEADLINE.format(
        symbol=report.symbol or "?",
        ruler="=" * 78,
        start=_fmt_ts(report.start),
        end=_fmt_ts(report.end),
        bars=_fmt_int(report.num_bars),
        tf=report.timeframe or "?",
        init=_fmt_money(report.initial_capital),
        final=_fmt_money(report.final_equity),
        total_ret=_fmt_pct(report.total_return),
        cagr=_fmt_pct(report.cagr),
        sharpe=_fmt_num(report.sharpe, 3),
        sortino=_fmt_num(report.sortino, 3),
        calmar=_fmt_num(report.calmar, 3),
        omega=_fmt_num(report.omega, 3),
        mdd=_fmt_pct(report.max_drawdown),
        mdd_dur=_fmt_int(report.max_drawdown_duration_bars),
        mdd_ts=_fmt_ts(report.max_drawdown_trough),
        avol=_fmt_pct(report.ann_volatility),
        dvol=_fmt_pct(report.ann_downside_volatility),
        var95=_fmt_pct(report.var_95, 3),
        var99=_fmt_pct(report.var_99, 3),
        es95=_fmt_pct(report.es_95, 3),
        es99=_fmt_pct(report.es_99, 3),
        ulcer=_fmt_pct(report.ulcer_index, 3),
        skew=_fmt_num(report.return_skewness, 3),
        kurt=_fmt_num(report.return_kurtosis_excess, 3),
    )

    activity = (
        f"\nActivity / Trades\n{'-' * 78}\n"
        f"  Rebalances    : {_fmt_int(report.num_rebalances):>10}    "
        f"Round-trips   : {_fmt_int(report.num_round_trips):>10}\n"
        f"  Turnover/yr   : {_fmt_num(report.turnover_per_year, 1):>10}    "
        f"Exposure      : {_fmt_pct_already(report.exposure_pct):>10}\n"
        f"  Win rate      : {_fmt_pct_already(report.win_rate):>10}    "
        f"Profit factor : {_fmt_num(report.profit_factor, 2):>10}\n"
        f"  Avg win       : {_fmt_money(report.avg_win):>12}  "
        f"Avg loss      : {_fmt_money(report.avg_loss):>12}\n"
        f"  Largest win   : {_fmt_money(report.largest_win):>12}  "
        f"Largest loss  : {_fmt_money(report.largest_loss):>12}\n"
        f"  Expectancy    : {_fmt_money(report.expectancy_per_trip):>12}  "
        f"Fees paid     : {_fmt_money(report.fees_paid):>12}\n"
    )

    parts = [head, activity]

    if not report.regime_breakdown.empty:
        parts.append("\nRegime breakdown\n" + "-" * 78)
        parts.append(_format_breakdown_table(report.regime_breakdown))
    if not report.bias_breakdown.empty:
        parts.append("\nBias breakdown\n" + "-" * 78)
        parts.append(_format_breakdown_table(report.bias_breakdown))

    if not report.monthly_returns.empty:
        parts.append("\nMonthly returns\n" + "-" * 78)
        parts.append(
            f"  Best month     : {_fmt_pct(report.best_month)}\n"
            f"  Worst month    : {_fmt_pct(report.worst_month)}\n"
            f"  % positive     : {_fmt_pct_already(report.pct_positive_months)}\n"
            f"  Months sampled : {_fmt_int(len(report.monthly_returns))}"
        )

    return "".join(parts)


def _format_breakdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "  (no data)"
    cols = ["name", "bars", "time_pct", "net_pnl", "fees", "hit_rate_pct", "sharpe"]
    df = df[cols].copy()
    df.columns = ["category", "bars", "time%", "net_pnl", "fees", "hit%", "sharpe"]
    return "\n" + df.to_string(index=False, float_format=lambda v: f"{v:,.2f}")


_METRIC_GLOSSARY_MD = """
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
"""


def render_markdown(report: TidePerformanceReport, plots: dict | None = None) -> str:
    """Markdown report.  ``plots`` is an optional ``{name: path}`` dict."""
    plots = plots or {}
    lines: list[str] = []

    lines.append(f"# Tide Overlay Backtest — {report.symbol}")
    lines.append("")
    lines.append(f"- **Exchange / TF:** {report.exchange} / `{report.timeframe}`")
    lines.append(
        f"- **Period:** {_fmt_ts(report.start)} → {_fmt_ts(report.end)}"
        f"  ({_fmt_int(report.num_bars)} bars)"
    )
    lines.append(
        f"- **Capital:** {_fmt_money(report.initial_capital)}"
        f" → {_fmt_money(report.final_equity)}"
        f"  (**{_fmt_pct(report.total_return)}**)"
    )
    lines.append("")

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value | Metric | Value |")
    lines.append("|---|---:|---|---:|")
    lines.append(
        f"| CAGR        | {_fmt_pct(report.cagr)}  "
        f"| Max Drawdown      | {_fmt_pct(report.max_drawdown)} |"
    )
    lines.append(
        f"| Sharpe      | {_fmt_num(report.sharpe, 3)}  "
        f"| MDD duration      | {_fmt_int(report.max_drawdown_duration_bars)} bars |"
    )
    lines.append(
        f"| Sortino     | {_fmt_num(report.sortino, 3)}  "
        f"| MDD trough        | {_fmt_ts(report.max_drawdown_trough)} |"
    )
    lines.append(
        f"| Calmar      | {_fmt_num(report.calmar, 3)}  "
        f"| Ann Volatility    | {_fmt_pct(report.ann_volatility)} |"
    )
    lines.append(
        f"| Omega (>0)  | {_fmt_num(report.omega, 3)}  "
        f"| Ann Downside Vol  | {_fmt_pct(report.ann_downside_volatility)} |"
    )
    lines.append(
        f"| VaR 95 / 99 | {_fmt_pct(report.var_95, 3)} / "
        f"{_fmt_pct(report.var_99, 3)}  "
        f"| ES 95 / 99        | {_fmt_pct(report.es_95, 3)} / "
        f"{_fmt_pct(report.es_99, 3)} |"
    )
    lines.append(
        f"| Ulcer Index | {_fmt_pct(report.ulcer_index, 3)}  "
        f"| Avg bar return    | {_fmt_pct(report.avg_bar_return, 4)} |"
    )
    lines.append(
        f"| Skewness    | {_fmt_num(report.return_skewness, 3)}  "
        f"| Kurtosis (excess) | {_fmt_num(report.return_kurtosis_excess, 3)} |"
    )
    lines.append("")

    lines.append("## Activity")
    lines.append("")
    lines.append("| Metric | Value | Metric | Value |")
    lines.append("|---|---:|---|---:|")
    lines.append(
        f"| Rebalances     | {_fmt_int(report.num_rebalances)} "
        f"| Round-trips      | {_fmt_int(report.num_round_trips)} |"
    )
    lines.append(
        f"| Turnover / year| {_fmt_num(report.turnover_per_year, 1)} "
        f"| Exposure         | {_fmt_pct_already(report.exposure_pct)} |"
    )
    lines.append(
        f"| Win rate       | {_fmt_pct_already(report.win_rate)} "
        f"| Profit factor    | {_fmt_num(report.profit_factor, 2)} |"
    )
    lines.append(
        f"| Avg win        | {_fmt_money(report.avg_win)} "
        f"| Avg loss         | {_fmt_money(report.avg_loss)} |"
    )
    lines.append(
        f"| Largest win    | {_fmt_money(report.largest_win)} "
        f"| Largest loss     | {_fmt_money(report.largest_loss)} |"
    )
    lines.append(
        f"| Expectancy/trip| {_fmt_money(report.expectancy_per_trip)} "
        f"| Fees paid        | {_fmt_money(report.fees_paid)} |"
    )
    lines.append(
        f"| Avg |position| | {_fmt_num(report.avg_position_abs, 4)} |  |  |"
    )
    lines.append("")

    if not report.regime_breakdown.empty:
        lines.append("## Volatility-regime breakdown")
        lines.append("")
        lines.append(_md_breakdown_table(report.regime_breakdown))
        lines.append("")
    if not report.bias_breakdown.empty:
        lines.append("## Bias breakdown")
        lines.append("")
        lines.append(_md_breakdown_table(report.bias_breakdown))
        lines.append("")

    if not report.monthly_returns.empty:
        lines.append("## Monthly returns")
        lines.append("")
        lines.append(
            f"- **Best month:** {_fmt_pct(report.best_month)}  "
            f"  **Worst month:** {_fmt_pct(report.worst_month)}  "
            f"  **% positive:** {_fmt_pct_already(report.pct_positive_months)}"
        )
        lines.append("")
        lines.append("| Month | Return |")
        lines.append("|---|---:|")
        for ts, ret in report.monthly_returns.items():
            lines.append(
                f"| {pd.Timestamp(ts).strftime('%Y-%m')} | {_fmt_pct(ret)} |"
            )
        lines.append("")

    if report.accuracy is not None:
        try:
            from tide.tide_accuracy import render_accuracy_markdown
            lines.append(render_accuracy_markdown(report.accuracy))
        except Exception:
            pass

    if plots:
        lines.append("## Charts")
        lines.append("")
        for name, path in plots.items():
            lines.append(f"### {name}")
            lines.append("")
            lines.append(f"![{name}]({path})")
            lines.append("")

    lines.append("## Parameters")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("|---|---:|")
    for k, v in report.params.items():
        if isinstance(v, (list, tuple)):
            v_str = "[" + ", ".join(f"{float(x):g}" for x in v) + "]"
        elif isinstance(v, float):
            v_str = f"{v:g}"
        else:
            v_str = str(v)
        lines.append(f"| `{k}` | {v_str} |")
    lines.append("")

    lines += _METRIC_GLOSSARY_MD.splitlines()
    lines.append("")

    return "\n".join(lines)


def _md_breakdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return ""
    out = ["| Category | Bars | Time % | Net PnL | Fees | Hit % | Sharpe |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for _, r in df.iterrows():
        out.append(
            f"| {r['name']} | {int(r['bars']):,} | {r['time_pct']:.2f}% "
            f"| {_fmt_money(r['net_pnl'])} | {_fmt_money(r['fees'])} "
            f"| {r['hit_rate_pct']:.2f}% | {r['sharpe']:.3f} |"
        )
    return "\n".join(out)


# ────────────────────────────────────────────────────────────────────────
# Plots
# ────────────────────────────────────────────────────────────────────────

def _ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def plot_equity_curve(
    result: TideBacktestResult, outpath: str | Path
) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    bars = result.bars
    eq   = result.equity_curve
    capital = result.initial_capital

    # ── Buy-and-hold baseline ─────────────────────────────────────────
    # Hold `initial_capital / open_price` units of the asset from bar 0.
    # Mark-to-market on close each bar.  Uses close prices from the bars
    # frame so the comparison is on exactly the same data as the strategy.
    close = bars["close"].to_numpy(dtype=np.float64)
    if close.size > 0 and close[0] > 0:
        bh_units  = capital / close[0]
        bh_equity = pd.Series(bh_units * close, index=bars.index)
    else:
        bh_equity = pd.Series(np.full(len(bars), capital), index=bars.index)

    bh_total_return = (bh_equity.iloc[-1] / capital - 1.0) * 100.0
    strat_total_return = (eq.iloc[-1] / capital - 1.0) * 100.0

    # ── Figure: two panels (equity + relative performance) ───────────
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1.2]},
    )

    # Panel 1 — absolute equity
    ax1.plot(eq.index, eq.values, color="#0a7d4f", linewidth=1.4,
             label=f"Tide overlay  ({strat_total_return:+.1f}%)")
    ax1.plot(bh_equity.index, bh_equity.values, color="#2980b9",
             linewidth=1.1, linestyle="--",
             label=f"Buy & hold    ({bh_total_return:+.1f}%)")
    ax1.axhline(capital, color="#95a5a6", linestyle=":", linewidth=0.8,
                label="Initial capital")
    ax1.set_ylabel("Equity (USD)")
    ax1.set_title(f"{result.params.symbol} — Tide overlay vs Buy & Hold")
    ax1.legend(loc="upper left", frameon=True, framealpha=0.85, fontsize=9)
    ax1.grid(True, alpha=0.2)

    # Panel 2 — strategy equity minus buy-and-hold (alpha in $)
    alpha = eq.values - bh_equity.reindex(eq.index).ffill().values
    ax2.fill_between(eq.index, alpha, 0,
                     where=alpha >= 0, color="#0a7d4f", alpha=0.45,
                     interpolate=True, label="Outperforming B&H")
    ax2.fill_between(eq.index, alpha, 0,
                     where=alpha < 0, color="#c0392b", alpha=0.45,
                     interpolate=True, label="Underperforming B&H")
    ax2.axhline(0, color="#95a5a6", linewidth=0.7)
    ax2.set_ylabel("Alpha vs B&H ($)")
    ax2.legend(loc="upper left", frameon=False, fontsize=8)
    ax2.grid(True, alpha=0.2)

    fig.autofmt_xdate()
    fig.tight_layout(h_pad=0.3)
    out = Path(outpath)
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_drawdown(result: TideBacktestResult, outpath: str | Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 3.0))
    dd = result.drawdown * 100.0
    ax.fill_between(dd.index, dd.values, 0.0, color="#c0392b", alpha=0.55)
    ax.plot(dd.index, dd.values, color="#c0392b", linewidth=0.8)
    ax.set_title("Drawdown (%)")
    ax.set_ylabel("Drawdown %")
    ax.grid(True, alpha=0.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_regime_overlay(
    result: TideBacktestResult, outpath: str | Path
) -> Path:
    """Equity curve with vol-regime shading and bias overlay."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bars = result.bars
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 6.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    ax1.plot(bars.index, bars["close"].values, color="#1c2833",
             linewidth=0.9, label="Close")
    ax1.set_ylabel("Price")

    palette = {
        "LOW": "#a3e4d7", "NORMAL": "#fef9e7",
        "HIGH": "#fad7a0", "CRISIS": "#f1948a",
    }
    regimes = bars["vol_regime"].astype(str).fillna("NORMAL").to_numpy()
    if regimes.size:
        change_idx = np.flatnonzero(regimes[1:] != regimes[:-1]) + 1
        boundaries = np.concatenate(([0], change_idx, [regimes.size]))
        for k in range(boundaries.size - 1):
            i0, i1 = boundaries[k], boundaries[k + 1]
            color = palette.get(regimes[i0], "#ffffff")
            ax1.axvspan(bars.index[i0], bars.index[min(i1, bars.shape[0]) - 1],
                        color=color, alpha=0.35, linewidth=0)

    ax1.set_title(f"{result.params.symbol} — Price + vol regime shading")
    handles = [
        plt.Rectangle((0, 0), 1, 1, color=col, alpha=0.5, label=name)
        for name, col in palette.items()
    ]
    ax1.legend(handles=handles, loc="upper left", frameon=False, ncol=4)

    ax1.grid(True, alpha=0.25)

    bias_map = {"LONG": 1, "NEUTRAL": 0, "SHORT": -1}
    bias_num = bars["bias"].map(lambda b: bias_map.get(str(b), 0)).astype(float)
    rm = bars["risk_mult"].astype(float)
    signal = bias_num * rm
    ax2.fill_between(bars.index, signal.values, 0.0,
                     where=signal.values >= 0, color="#27ae60", alpha=0.6,
                     interpolate=True, label="Long exposure")
    ax2.fill_between(bars.index, signal.values, 0.0,
                     where=signal.values < 0, color="#c0392b", alpha=0.6,
                     interpolate=True, label="Short exposure")
    ax2.axhline(0.0, color="grey", linewidth=0.6)
    ax2.set_ylabel("Bias × risk_mult")
    ax2.set_ylim(-1.1, 1.1)
    ax2.grid(True, alpha=0.25)
    ax2.legend(loc="upper left", frameon=False)

    fig.autofmt_xdate()
    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


# ────────────────────────────────────────────────────────────────────────
# All-in-one writer
# ────────────────────────────────────────────────────────────────────────

@dataclass
class WrittenReport:
    text_path: Path
    markdown_path: Path
    equity_path: Path
    drawdown_path: Path
    regime_path: Path
    summary_text: str


def write_report(
    result: TideBacktestResult,
    out_dir: str | Path = "reports",
    *,
    name: str | None = None,
    make_plots: bool = True,
) -> WrittenReport:
    """Compute the report from a backtest result and persist all artefacts.

    Returns the paths of the written files plus the in-memory text summary.
    """
    out = _ensure_dir(out_dir)
    if name is None:
        ts_stamp = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%S")
        sym = result.params.symbol or "TIDE"
        name = f"tide_{sym}_{ts_stamp}"
    base = out / name

    report = compute_report(result)

    text_path = Path(str(base) + ".txt")
    md_path = Path(str(base) + ".md")
    equity_path = Path(str(base) + "_equity.png")
    dd_path = Path(str(base) + "_drawdown.png")
    regime_path = Path(str(base) + "_regime.png")

    summary_text = render_text(report)
    text_path.write_text(summary_text + "\n")

    plots: dict[str, str] = {}
    if make_plots:
        try:
            plot_equity_curve(result, equity_path)
            plot_drawdown(result, dd_path)
            plot_regime_overlay(result, regime_path)
            plots = {
                "Equity curve": equity_path.name,
                "Drawdown": dd_path.name,
                "Regime overlay": regime_path.name,
            }
        except Exception as exc:  # plotting must never break a CLI run
            logger.warning("Plot generation failed: %s", exc)

        if report.accuracy is not None:
            try:
                from tide.tide_accuracy import plot_all as _plot_acc
                acc_plots = _plot_acc(
                    result.bars, report.accuracy, out, name
                )
                plots.update({k: Path(v).name for k, v in acc_plots.items()})
            except Exception as exc:
                logger.warning("Accuracy plots failed: %s", exc)

    md_path.write_text(render_markdown(report, plots=plots) + "\n")
    logger.info("Report written: %s", md_path)

    return WrittenReport(
        text_path=text_path,
        markdown_path=md_path,
        equity_path=equity_path,
        drawdown_path=dd_path,
        regime_path=regime_path,
        summary_text=summary_text,
    )
