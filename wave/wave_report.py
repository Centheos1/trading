"""
Wave backtest report writer.

Produces three deliverables for any WavePerformanceReport:
    1. Plain-text summary       — for stdout / log
    2. Markdown report          — for reports/<run>.md
    3. PNG plots                — equity curve, drawdown, regime timeline,
                                  accuracy plots
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from wave.wave_backtest import WaveBacktestResult
from wave.wave_metrics import WavePerformanceReport, compute_wave_report
from wave.wave_accuracy import plot_regime_accuracy

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Formatters (shared with tide_report; duplicated to keep wave independent)
# ────────────────────────────────────────────────────────────────────────

def _pct(x: float, dp: int = 2) -> str:
    return "n/a" if not np.isfinite(x) else f"{x * 100:.{dp}f}%"


def _num(x: float, dp: int = 4) -> str:
    if x == float("inf"):
        return "+inf"
    if x == float("-inf"):
        return "-inf"
    return "n/a" if not np.isfinite(x) else f"{x:.{dp}f}"


def _money(x: float, dp: int = 2) -> str:
    return "n/a" if not np.isfinite(x) else f"${x:,.{dp}f}"


def _int(x: int) -> str:
    return f"{int(x):,}"


# ────────────────────────────────────────────────────────────────────────
# Text rendering
# ────────────────────────────────────────────────────────────────────────

def render_text(report: WavePerformanceReport) -> str:
    r = report
    lines = [
        "=" * 65,
        f"  WAVE BACKTEST REPORT  ({r.symbol} | {r.timeframe} | {r.mode.upper()})",
        "=" * 65,
        f"  Period   : {r.start} → {r.end}",
        f"  Bars     : {_int(r.n_bars)}",
        "",
        "  ── RETURNS ──────────────────────────────────────────────",
        f"  Total return        : {_pct(r.total_return)}",
        f"  CAGR                : {_pct(r.cagr)}",
        f"  Initial capital     : {_money(r.initial_capital)}",
        f"  Final equity        : {_money(r.final_equity)}",
        "",
        "  ── RISK ─────────────────────────────────────────────────",
        f"  Ann. volatility     : {_pct(r.ann_vol)}",
        f"  Ann. downside vol   : {_pct(r.ann_downside_vol)}",
        f"  Max drawdown        : {_pct(r.max_drawdown)}",
        f"  Max DD duration     : {_int(r.max_drawdown_duration)} bars",
        f"  VaR 95%             : {_pct(r.var_95)}",
        f"  CVaR 95%            : {_pct(r.es_95)}",
        f"  Ulcer index         : {_num(r.ulcer_index)}",
        f"  Skewness            : {_num(r.skewness)}",
        f"  Kurtosis (excess)   : {_num(r.kurtosis)}",
        "",
        "  ── RISK-ADJUSTED ────────────────────────────────────────",
        f"  Sharpe ratio        : {_num(r.sharpe)}",
        f"  Sortino ratio       : {_num(r.sortino)}",
        f"  Calmar ratio        : {_num(r.calmar)}",
        "",
        "  ── TRADES ───────────────────────────────────────────────",
        f"  Num trades          : {_int(r.num_trades)}",
        f"  Exposure %          : {_num(r.exposure_pct, 1)}%",
        f"  Win rate            : {_pct(r.win_rate)}",
        f"  Profit factor       : {_num(r.profit_factor)}",
        f"  Avg win             : {_money(r.avg_win)}",
        f"  Avg loss            : {_money(r.avg_loss)}",
        f"  Expectancy/bar      : {_pct(r.expectancy_per_bar, 4)}",
    ]
    if r.per_regime:
        lines += ["", "  ── REGIME BREAKDOWN ─────────────────────────────────────"]
        lines.append(f"  {'Regime':<18} {'Time%':>6}  {'PnL':>10}  {'Sharpe':>7}")
        lines.append("  " + "-" * 52)
        for row in r.per_regime:
            lines.append(
                f"  {row['regime']:<18} {row['time_pct']:>5.1f}%  "
                f"{_money(row['contribution_pnl']):>10}  {_num(row['sharpe'], 2):>7}"
            )
    if r.regime_accuracy is not None:
        lines += ["", r.regime_accuracy.summary_text()]
    lines.append("=" * 65)
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Markdown rendering
# ────────────────────────────────────────────────────────────────────────

_METRIC_GLOSSARY_MD = """
---

## Metric Glossary

> Full derivations and plain-language definitions are in `METRICS_GLOSSARY.md`
> at the repository root.  The compact reference below covers every number in
> this report.

### Returns & Growth

| Metric | Formula | Plain language |
|---|---|---|
| **Total Return** | $\\frac{E_N - E_0}{E_0}$ | Simple % gain/loss over the full period |
| **CAGR** | $\\left(\\frac{E_N}{E_0}\\right)^{P/N} - 1$ | Constant annual growth rate; normalised for run length |

### Risk

| Metric | Formula | Plain language |
|---|---|---|
| **Ann. Volatility** | $\\sigma(r) \\times \\sqrt{P}$ | Annualised return std; penalises up and down moves equally |
| **Ann. Downside Vol** | $\\sigma(\\min(r,0)) \\times \\sqrt{P}$ | Volatility of losses only |
| **Max Drawdown** | $\\min_t \\frac{E_t - \\max_{s\\leq t} E_s}{\\max_{s\\leq t} E_s}$ | Worst peak-to-trough decline as a fraction of the prior peak |
| **VaR 95 / 99** | $-Q_{0.05/0.01}(r)$ | Loss exceeded on 5%/1% of bars |
| **CVaR / ES** | $-\\mathbb{E}[r \\mid r < \\text{VaR}]$ | Average loss in the worst 5%/1% tail |
| **Ulcer Index** | $\\sqrt{\\frac{1}{N}\\sum D_t^2}$ | RMS drawdown depth; penalises deep + prolonged drawdowns |

### Risk-Adjusted

| Metric | Formula | Plain language |
|---|---|---|
| **Sharpe** | $\\frac{\\bar{r} \\cdot P - R_f}{\\sigma \\cdot \\sqrt{P}}$ | Return per unit of total volatility, annualised |
| **Sortino** | $\\frac{\\text{CAGR} - \\text{MAR}}{\\sigma_d \\cdot \\sqrt{P}}$ | Like Sharpe but only penalises downside volatility |
| **Calmar** | $\\frac{\\text{CAGR}}{\\lvert\\text{MDD}\\rvert}$ | Return per unit of worst drawdown |

### Wave Regime Accuracy

| Metric | Formula | Plain language |
|---|---|---|
| **Regime Hit Rate** | $\\frac{\\lvert\\{t \\in \\mathcal{D} : g(\\rho_t) \\cdot f_t^{(h)} > 0\\}\\rvert}{\\lvert\\mathcal{D}\\rvert} \\times 100$ | % of BREAKOUT/BREAKDOWN bars where forward return aligned with regime direction |
| **Regime IC** | $\\text{corr}(g(\\rho_t),\\; f_t^{(h)})$ | Pearson correlation of regime signal with forward log-returns |
| **Transition Prob** | $P(\\rho_{t+1}=j \\mid \\rho_t=i)$ | Row-normalised probability of moving from regime $i$ to regime $j$ |
| **Regime Stability** | $\\mathbb{E}[\\text{consecutive bars in regime } R]$ | Mean run length per regime; higher = more persistent (less whipsaw) |

Where $g(\\rho_t) \\in \\{+1, 0, -1\\}$: BREAKOUT $\\to +1$, BREAKDOWN $\\to -1$, MR/NEUTRAL $\\to 0$.

### Wave Sizing

$$\\text{target\\_units} = \\underbrace{b_t}_{\\text{Tide sign}} \\times \\underbrace{m_t}_{\\text{Tide risk mult}} \\times \\underbrace{\\phi(\\rho_t)}_{\\text{Wave size frac}} \\times q_{\\max}$$

| $\\rho_t$ | $\\phi(\\rho_t)$ |
|---|---|
| BREAKOUT | $1.0$ |
| MEAN\\_REVERSION | `reduced_size_fraction` |
| NEUTRAL | `reduced_size_fraction` |
| BREAKDOWN | $0.0$ |
"""


def render_markdown(
    report: WavePerformanceReport,
    result: Optional[WaveBacktestResult] = None,
    name: str = "",
) -> str:
    r = report

    def _row(label: str, value: str) -> str:
        return f"| {label} | {value} |"

    # Helper: embed a plot if the file exists alongside the report
    def _img(suffix: str, caption: str) -> str:
        fname = f"{name}{suffix}" if name else ""
        return f"![{caption}]({fname})" if fname else ""

    # Human-readable horizon labels derived from bar_seconds
    def _hlabel(h: int) -> str:
        bar_sec = result.params.bar_seconds if result is not None else 3600.0
        mins = h * bar_sec / 60.0
        if mins < 60:
            return f"{int(round(mins))}min"
        hrs = mins / 60.0
        return f"{hrs:.0f}h" if hrs == int(hrs) else f"{hrs:.1f}h"

    lines = [
        f"# Wave Backtest Report",
        f"**Symbol:** {r.symbol} | **Timeframe:** {r.timeframe} | "
        f"**Mode:** {r.mode.upper()} | **Exchange:** {r.exchange}  ",
        f"**Period:** {r.start} → {r.end} | **Bars:** {_int(r.n_bars)}",
    ]

    # ── Equity curve visualisation ────────────────────────────────────────
    eq_img = _img("_equity.png", "Equity Curve vs Buy & Hold")
    if eq_img:
        lines += ["", "## Equity Curve", "", eq_img, ""]

    lines += [
        "## Returns & Growth",
        "| Metric | Formula | Value |",
        "|---|---|---|",
        f"| Total Return | $\\frac{{E_N - E_0}}{{E_0}}$ | {_pct(r.total_return)} |",
        f"| CAGR | $(E_N/E_0)^{{P/N}} - 1$ | {_pct(r.cagr)} |",
        f"| Initial Capital | $E_0$ | {_money(r.initial_capital)} |",
        f"| Final Equity | $E_N$ | {_money(r.final_equity)} |",
        "",
        "## Risk",
        "| Metric | Formula | Value |",
        "|---|---|---|",
        f"| Ann. Volatility | $\\sigma(r) \\times \\sqrt{{P}}$ | {_pct(r.ann_vol)} |",
        f"| Ann. Downside Vol | $\\sigma(\\min(r,0)) \\times \\sqrt{{P}}$ | {_pct(r.ann_downside_vol)} |",
        f"| Max Drawdown | $\\min_t (E_t - \\text{{peak}}_t)/\\text{{peak}}_t$ | {_pct(r.max_drawdown)} |",
        _row("Max DD Duration (bars)", _int(r.max_drawdown_duration)),
        f"| VaR 95% | $-Q_{{0.05}}(r)$ | {_pct(r.var_95)} |",
        f"| CVaR 95% (ES) | $-\\mathbb{{E}}[r \\mid r < \\text{{VaR}}_{{95}}]$ | {_pct(r.es_95)} |",
        f"| VaR 99% | $-Q_{{0.01}}(r)$ | {_pct(r.var_99)} |",
        f"| CVaR 99% (ES) | $-\\mathbb{{E}}[r \\mid r < \\text{{VaR}}_{{99}}]$ | {_pct(r.es_99)} |",
        f"| Ulcer Index | $\\sqrt{{N^{{-1}}\\sum D_t^2}}$ | {_num(r.ulcer_index)} |",
        f"| Skewness | $\\frac{{1}}{{N}}\\sum((r_t-\\bar r)/\\sigma)^3$ | {_num(r.skewness)} |",
        f"| Excess Kurtosis | $\\frac{{1}}{{N}}\\sum((r_t-\\bar r)/\\sigma)^4 - 3$ | {_num(r.kurtosis)} |",
        "",
        "## Risk-Adjusted",
        "| Metric | Formula | Value |",
        "|---|---|---|",
        f"| Sharpe Ratio | $(\\bar{{r}} \\cdot P - R_f)\\,/\\,(\\sigma\\sqrt{{P}})$ | {_num(r.sharpe)} |",
        f"| Sortino Ratio | $(\\text{{CAGR}} - \\text{{MAR}})\\,/\\,(\\sigma_d\\sqrt{{P}})$ | {_num(r.sortino)} |",
        f"| Calmar Ratio | $\\text{{CAGR}}\\,/\\,\\lvert\\text{{MDD}}\\rvert$ | {_num(r.calmar)} |",
        "",
        "## Activity & Trades",
        "| Metric | Value |",
        "|---|---|",
        _row("Num Trades", _int(r.num_trades)),
        _row("Exposure %", f"{_num(r.exposure_pct, 1)}%"),
        _row("Win Rate", _pct(r.win_rate)),
        _row("Profit Factor", _num(r.profit_factor)),
        _row("Avg Win", _money(r.avg_win)),
        _row("Avg Loss", _money(r.avg_loss)),
        _row("Expectancy / Bar", _pct(r.expectancy_per_bar, 4)),
    ]

    if r.per_regime:
        lines += [
            "",
            "## Per-Regime Breakdown",
            "| Regime | Time % | PnL Contribution | Sharpe | N Bars |",
            "|---|---|---|---|---|",
        ]
        for row in r.per_regime:
            lines.append(
                f"| {row['regime']} | {row['time_pct']:.1f}% | "
                f"{_money(row['contribution_pnl'])} | {_num(row['sharpe'], 2)} | "
                f"{_int(row['n_bars'])} |"
            )

    if r.monthly_returns is not None and not r.monthly_returns.empty:
        lines += [
            "",
            "## Monthly Returns",
            f"Best month: {_pct(r.best_month)}  |  "
            f"Worst month: {_pct(r.worst_month)}  |  "
            f"Positive months: {_num(r.pct_positive_months, 1)}%",
            "",
            "| Month | Return |",
            "|---|---|",
        ]
        for ts, val in r.monthly_returns.items():
            lines.append(f"| {pd.Timestamp(ts).strftime('%Y-%m')} | {_pct(val)} |")

    if r.regime_accuracy is not None:
        acc = r.regime_accuracy

        # ── Regime timeline visualisation ─────────────────────────────────
        tl_img = _img("_wave_timeline.png", "Wave Regime Timeline")
        if tl_img:
            lines += ["", "## Regime Timeline", "", tl_img, ""]

        lines += [
            "## Wave Regime Accuracy",
            "",
            "Accuracy measures whether the Wave regime at time $t$ predicts the "
            "direction of price over the next $h$ bars.  For BREAKOUT and BREAKDOWN "
            "bars (where $g(\\rho_t) \\neq 0$):",
            "",
            "$$\\text{Hit Rate} = "
            "\\frac{|\\{t \\in \\mathcal{D} : g(\\rho_t) \\cdot f_t^{(h)} > 0\\}|}"
            "{|\\mathcal{D}|} \\times 100"
            "\\qquad "
            "\\text{IC} = \\text{corr}\\!\\left(g(\\rho_t),\\; f_t^{(h)}\\right)$$",
            "",
            "where $f_t^{(h)} = \\log(c_{t+h}/c_t)$ and "
            "$\\mathcal{D} = \\{t : g(\\rho_t) \\neq 0\\}$ (BREAKOUT + BREAKDOWN bars).",
            "",
        ]

        # Hit rate plot
        hr_img = _img("_wave_hit_rate.png", "Regime Hit Rate vs Horizon")
        if hr_img:
            lines += [hr_img, ""]

        lines += [
            "### Overall Hit Rate & IC",
            "| Horizon $h$ | Bars | Hit Rate | IC |",
            "|---|---|---|---|",
        ]
        for h, hr, ic in zip(acc.horizons, acc.overall_hit_rate, acc.overall_ic):
            lines.append(f"| {_hlabel(h)} | {h} | {_pct(hr)} | {_num(ic, 4)} |")

        # Forward return plot
        fwd_img = _img("_wave_fwd_return.png", "Per-Regime Mean Forward Return vs Horizon")
        if fwd_img:
            lines += ["", fwd_img, ""]

        lines += ["", "### Per-Regime Forward Return",
                  "",
                  "Mean forward log-return $\\mathbb{E}[f_t^{(h)} \\mid \\rho_t = R]$ "
                  "and regime hit rate per horizon:"]
        for h in acc.horizons:
            stats = [s for s in acc.regime_horizon_stats if s.h == h]
            if not stats:
                continue
            lines += [
                "",
                f"**Horizon $h={h}$ bars ({_hlabel(h)})**",
                "| Regime | $N$ | Mean $f^{(h)}$ | Median $f^{(h)}$ | Hit Rate |",
                "|---|---|---|---|---|",
            ]
            for s in stats:
                lines.append(
                    f"| {s.regime} | {s.n} | {_pct(s.mean_fwd_return, 3)} | "
                    f"{_pct(s.median_fwd_return, 3)} | {_pct(s.hit_rate)} |"
                )

        if acc.transition_matrix is not None:
            tm = acc.transition_matrix

            # Transition matrix plot
            tr_img = _img("_wave_transition.png", "Regime Transition Matrix")
            if tr_img:
                lines += ["", tr_img, ""]

            lines += [
                "### Regime Transition Matrix",
                "",
                "Row-normalised probabilities $T_{ij} = P(\\rho_{t+1}=j \\mid \\rho_t=i)$:",
                "",
            ]
            header = "| From \\ To | " + " | ".join(tm.regimes) + " |"
            lines.append(header)
            lines.append("|" + "---|" * (len(tm.regimes) + 1))
            for i, from_r in enumerate(tm.regimes):
                row_vals = " | ".join(f"{tm.probs[i, j]:.3f}" for j in range(len(tm.regimes)))
                lines.append(f"| {from_r} | {row_vals} |")

        if acc.regime_stability:
            lines += [
                "",
                "### Regime Stability",
                "",
                "Mean consecutive-bar run length per regime "
                "($\\approx 1/(1-T_{ii})$ from transition matrix diagonal):",
                "",
                "| Regime | Mean Run (bars) |",
                "|---|---|",
            ]
            for reg, v in acc.regime_stability.items():
                lines.append(f"| {reg} | {v:.1f} |")

        if acc.regime_bias_mean_fwd is not None:
            conf_img = _img("_wave_confusion.png", "Regime × Tide Bias Confusion")
            if conf_img:
                lines += ["", conf_img, ""]
            lines += [
                "### Regime × Tide Bias Confusion",
                "",
                "Mean forward log-return at $h=1$ for each $(\\rho_t, b_t)$ combination "
                "(stacked mode only):",
                "",
            ]
            df_conf = acc.regime_bias_mean_fwd * 100
            col_headers = "| Wave Regime \\ Tide Bias | " + " | ".join(df_conf.columns) + " |"
            lines.append(col_headers)
            lines.append("|" + "---|" * (len(df_conf.columns) + 1))
            for idx_r in df_conf.index:
                row_vals = " | ".join(f"{df_conf.loc[idx_r, c]:.3f}%" for c in df_conf.columns)
                lines.append(f"| {idx_r} | {row_vals} |")

    if r.params_echo:
        lines += ["", "## Parameters", "| Parameter | Value |", "|---|---|"]
        for k, v in r.params_echo.items():
            lines.append(f"| {k} | {v} |")

    lines.append(_METRIC_GLOSSARY_MD)
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────
# Equity curve / drawdown plot
# ────────────────────────────────────────────────────────────────────────

def plot_equity_curve(
    result: WaveBacktestResult,
    out_path: Path,
) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    bars = result.bars
    equity = bars["equity"]
    close = bars["close"]

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                              gridspec_kw={"height_ratios": [3, 1]})
    ax_eq, ax_dd = axes

    # Strategy equity
    ax_eq.plot(equity.index, equity.values, color="#7B1FA2", linewidth=1.2,
               label="Wave Strategy")

    # Buy & hold baseline
    if not close.empty and float(close.iloc[0]) > 0:
        bh = result.initial_capital * close / float(close.iloc[0])
        ax_eq.plot(bh.index, bh.values, color="#B0BEC5", linewidth=0.8,
                   linestyle="--", label="Buy & Hold")

    ax_eq.set_ylabel("Equity ($)")
    ax_eq.set_title(
        f"Wave Equity Curve — {result.params.symbol} {result.params.timeframe} "
        f"[{result.mode.upper()}]"
    )
    ax_eq.legend(fontsize=8)
    ax_eq.grid(True, alpha=0.3)

    dd = bars["drawdown"] * 100
    ax_dd.fill_between(dd.index, dd.values, 0, alpha=0.4, color="#EF5350")
    ax_dd.plot(dd.index, dd.values, color="#C62828", linewidth=0.6)
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.grid(True, alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


# ────────────────────────────────────────────────────────────────────────
# Write report (markdown + all plots)
# ────────────────────────────────────────────────────────────────────────

def write_report(
    result: WaveBacktestResult,
    report: WavePerformanceReport,
    out_dir: Path | str,
    name: str,
) -> Path:
    """Write the full Wave report: markdown + plots.

    Plots are generated first so the markdown can embed relative image links.
    Returns the path to the markdown file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Generate plots first ──────────────────────────────────────────────
    try:
        plot_equity_curve(result, out_dir / f"{name}_equity.png")
    except Exception as exc:
        logger.warning("Wave equity plot failed: %s", exc)

    if report.regime_accuracy is not None:
        try:
            plot_regime_accuracy(result.bars, report.regime_accuracy, out_dir, name)
        except Exception as exc:
            logger.warning("Wave accuracy plots failed: %s", exc)

    # ── Write markdown with image references ──────────────────────────────
    md_path = out_dir / f"{name}.md"
    md_content = render_markdown(report, result, name=name)
    md_path.write_text(md_content, encoding="utf-8")
    logger.info("Wave markdown report → %s", md_path)

    return md_path
