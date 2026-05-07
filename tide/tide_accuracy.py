"""
Signal-accuracy analysis for any strategy layer.

Answers: "Is bias_t correct for t+h where h ∈ {1, 2, …, T}?"

Exported entry points
─────────────────────
  compute_accuracy_report(bars, horizons, sizing_base)
      → AccuracyReport

  plot_accuracy(result, AccuracyReport, outdir, name)
      → dict[str, Path]   (name → file path)

Design
──────
One-sided bias: +1 (LONG), 0 (NEUTRAL), −1 (SHORT).

For each horizon h:
  • forward_return_h[t]  = log(close_{t+h} / close_t)
  • prediction_t         = bias_sign_t   ∈ {-1, 0, +1}
  • signed_forward[t]    = prediction_t × forward_return_h[t]

Metrics (excluding NEUTRAL bars from directional accuracy):
  hit_rate_h      % correct direction when bias ≠ NEUTRAL
  ic_h            Pearson correlation of (bias_sign × forward_ret) with 0
                  across ALL bars (IC of the binary signal)
  directional_ic  Pearson corr restricted to LONG/SHORT bars
  forward_pnl_h   Sum of signed_forward_h × sizing_base × close_t
                  (position-unit PnL proxy = edge in BTC-or-$ terms)
  avg_win_h       Mean gain per hit (LONG/SHORT bars only)
  avg_loss_h      Mean loss per miss
  expectancy_h    avg(signed_forward_h) over active bars
  vol_of_fwd_h    std of forward_return over all bars (regime effect)
  calmar_proxy_h  E[signed_fwd_h] / std(signed_fwd_h) (bar Sharpe)

Additionally:
  confusion matrix  per (vol_regime, bias) → mean forward return h=1
  accuracy vs horizon curve (hit_rate[h] for h in horizons)
  signal-timeline bar-series for plotting

Layer agnosticism
─────────────────
This module only needs a DataFrame with columns:
  close, bias (str), vol_regime (str), [optional: risk_mult]
These are produced by TideBacktester.run().bars and by any future
Wave / Ripple backtester, so the same code re-runs unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_BIAS_SIGN: Dict[str, int] = {"LONG": 1, "SHORT": -1, "NEUTRAL": 0}


# ─────────────────────────────────────────────────────────────────────────────
# Single-horizon metrics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HorizonMetrics:
    """Accuracy metrics at a single forward horizon h (in bars)."""
    h: int                       # forward horizon in bars
    n_active: int                # LONG + SHORT bars (NEUTRAL excluded)
    n_neutral: int               # NEUTRAL bars
    hit_rate: float              # % correct direction (active bars only)
    avg_win: float               # avg forward return when correct
    avg_loss: float              # avg forward return when wrong (negative)
    expectancy: float            # avg(signed_forward) over active bars
    ic: float                    # Pearson IC (all bars, including neutral)
    directional_ic: float        # Pearson IC restricted to active bars
    avg_long_fwd: float          # avg forward return when bias==LONG
    avg_short_fwd: float         # avg forward return when bias==SHORT
    avg_neutral_fwd: float       # avg |forward return| when bias==NEUTRAL
    calmar_proxy: float          # E[signed_fwd] / std(signed_fwd)
    pnl_proxy: float             # Σ(signed_fwd × sizing_base × close)
    long_fwd_returns: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    short_fwd_returns: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    neutral_fwd_returns: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation, returns 0 on degenerate inputs."""
    if a.size < 3:
        return 0.0
    a_std, b_std = float(np.std(a)), float(np.std(b))
    if a_std == 0 or b_std == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def compute_horizon_metrics(
    bars: pd.DataFrame,
    h: int,
    sizing_base: float = 1.0,
    scale_by_price: bool = False,
) -> Optional[HorizonMetrics]:
    """Compute signal accuracy metrics for a single forward horizon.

    Parameters
    ----------
    bars : pd.DataFrame
        Must have columns: close, bias (str).  Optional: risk_mult.
    h : int
        Forward horizon in bars.
    sizing_base : float
        Notional exposure per signal bar.
        BASE mode  → pass ``max_position_base`` (e.g. 1.0 for 1 BTC).
        USD  mode  → pass ``max_position_usd``  (e.g. 10_000).
    scale_by_price : bool
        True  → BASE mode: units are fixed (e.g. 1 BTC); multiply by close
                to express PnL in dollar terms.
                PnL = signed_fwd × sizing_base × close_t
        False → USD mode: notional is already in dollars; position units
                embed 1/close, which cancels the price factor.
                PnL = signed_fwd × sizing_base
        Default False (USD).

    Returns None if there are insufficient bars.
    """
    n = len(bars)
    if n < h + 2:
        return None

    close = bars["close"].to_numpy(dtype=np.float64)
    bias_str = bars["bias"].astype(str).to_numpy()
    bias_sign = np.array([_BIAS_SIGN.get(b, 0) for b in bias_str], dtype=np.float64)

    # Forward log returns: fwd_ret[t] = log(close[t+h] / close[t])
    fwd_log = np.log(close[h:] / close[:n - h])

    # Align: bias_sign[t] predicts fwd_log[t]
    pred = bias_sign[:n - h]          # predictions (with NEUTRAL=0)
    signed_fwd = pred * fwd_log       # negative = loss

    # Active mask (LONG/SHORT only)
    active = pred != 0
    n_active = int(active.sum())
    n_neutral = int((pred == 0).sum())

    # PnL proxy
    # BASE mode: hold `sizing_base` units; dollar PnL ≈ units × close × fwd_log
    # USD  mode: hold `sizing_base` notional; dollar PnL ≈ notional × fwd_log
    #            (the 1/close in unit count cancels with the close in price move)
    if scale_by_price:
        pnl_proxy_arr = signed_fwd[active] * sizing_base * close[:n - h][active]
    else:
        pnl_proxy_arr = signed_fwd[active] * sizing_base
    pnl_proxy = float(pnl_proxy_arr.sum())

    if n_active == 0:
        return HorizonMetrics(
            h=h, n_active=0, n_neutral=n_neutral,
            hit_rate=0.0, avg_win=0.0, avg_loss=0.0, expectancy=0.0,
            ic=0.0, directional_ic=0.0,
            avg_long_fwd=0.0, avg_short_fwd=0.0, avg_neutral_fwd=0.0,
            calmar_proxy=0.0, pnl_proxy=0.0,
        )

    signed_active = signed_fwd[active]
    hit_mask = signed_active > 0
    hit_rate = float(hit_mask.mean()) * 100.0
    avg_win = float(signed_active[hit_mask].mean()) if hit_mask.any() else 0.0
    avg_loss = float(signed_active[~hit_mask].mean()) if (~hit_mask).any() else 0.0
    expectancy = float(signed_active.mean())
    sd = float(np.std(signed_active, ddof=1)) if len(signed_active) > 1 else 0.0
    calmar_proxy = expectancy / sd if sd > 0 else 0.0

    ic = _safe_corr(pred, fwd_log)
    directional_ic = _safe_corr(pred[active], fwd_log[active])

    long_mask = pred == 1
    short_mask = pred == -1
    neutral_mask = pred == 0
    avg_long_fwd = float(fwd_log[long_mask].mean()) if long_mask.any() else float("nan")
    avg_short_fwd = float((-fwd_log[short_mask]).mean()) if short_mask.any() else float("nan")
    avg_neutral_fwd = float(np.abs(fwd_log[neutral_mask]).mean()) if neutral_mask.any() else float("nan")

    return HorizonMetrics(
        h=h, n_active=n_active, n_neutral=n_neutral,
        hit_rate=hit_rate, avg_win=avg_win, avg_loss=avg_loss,
        expectancy=expectancy, ic=ic, directional_ic=directional_ic,
        avg_long_fwd=avg_long_fwd,
        avg_short_fwd=avg_short_fwd,
        avg_neutral_fwd=avg_neutral_fwd,
        calmar_proxy=calmar_proxy, pnl_proxy=pnl_proxy,
        long_fwd_returns=fwd_log[long_mask],
        short_fwd_returns=fwd_log[short_mask],
        neutral_fwd_returns=fwd_log[neutral_mask],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Regime × bias confusion analysis
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConfusionMatrix:
    """Per-(vol_regime, bias) forward return summary at h=1."""
    pivot_mean: pd.DataFrame    # rows=vol_regime, cols=bias, values=mean fwd return
    pivot_hit:  pd.DataFrame    # same but hit rate %
    pivot_count: pd.DataFrame   # number of bars per cell


def compute_confusion(bars: pd.DataFrame) -> Optional[ConfusionMatrix]:
    """Compute vol_regime × bias confusion at the h=1 horizon."""
    n = len(bars)
    if n < 3 or "vol_regime" not in bars.columns:
        return None

    close = bars["close"].to_numpy(dtype=np.float64)
    bias_str = bars["bias"].astype(str).to_numpy()
    bias_sign = np.array([_BIAS_SIGN.get(b, 0) for b in bias_str], dtype=np.float64)
    regime_str = bars["vol_regime"].astype(str).to_numpy()

    fwd = np.log(close[1:] / close[:-1])
    signed_fwd = bias_sign[:n - 1] * fwd

    df = pd.DataFrame({
        "vol_regime": regime_str[:n - 1],
        "bias": bias_str[:n - 1],
        "fwd_return": fwd,
        "signed_fwd": signed_fwd,
    })

    pivot_mean = df.pivot_table(
        index="vol_regime", columns="bias", values="signed_fwd",
        aggfunc="mean", observed=True
    ).fillna(0.0)

    pivot_hit = df.pivot_table(
        index="vol_regime", columns="bias",
        values="signed_fwd",
        aggfunc=lambda x: float((x > 0).mean()) * 100.0,
        observed=True,
    ).fillna(0.0)

    pivot_count = df.pivot_table(
        index="vol_regime", columns="bias", values="fwd_return",
        aggfunc="count", observed=True,
    ).fillna(0)

    return ConfusionMatrix(
        pivot_mean=pivot_mean, pivot_hit=pivot_hit, pivot_count=pivot_count
    )


# ─────────────────────────────────────────────────────────────────────────────
# Top-level accuracy report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AccuracyReport:
    """Signal-accuracy report across multiple forward horizons."""
    horizons: List[HorizonMetrics]
    confusion: Optional[ConfusionMatrix]
    summary_df: pd.DataFrame            # horizons × metrics table
    layer: str = "tide"                 # "tide", "wave", "ripple" in future


def compute_accuracy_report(
    bars: pd.DataFrame,
    horizons: List[int] | None = None,
    sizing_base: float = 1.0,
    scale_by_price: bool = False,
    layer: str = "tide",
) -> AccuracyReport:
    """Compute the full accuracy report for a backtest result's bar frame.

    Parameters
    ----------
    bars : pd.DataFrame
        ``result.bars`` from TideBacktestResult.
    horizons : list of int
        Forward horizons in bars.  Default: [1, 2, 4, 8, 12, 24].
    sizing_base : float
        Position size for PnL proxy.
        BASE mode → ``max_position_base`` (e.g. 1.0 BTC).
        USD  mode → ``max_position_usd``  (e.g. 10_000).
    scale_by_price : bool
        True  (BASE): PnL = signed_fwd × sizing_base × close_t  ($ terms).
        False (USD):  PnL = signed_fwd × sizing_base             (already $).
    layer : str
        Used for labelling in reports ("tide" / "wave" / "ripple").
    """
    if horizons is None:
        horizons = [1, 2, 4, 8, 12, 24]

    hm_list: List[HorizonMetrics] = []
    for h in horizons:
        hm = compute_horizon_metrics(
            bars, h=h, sizing_base=sizing_base, scale_by_price=scale_by_price
        )
        if hm is not None:
            hm_list.append(hm)

    confusion = compute_confusion(bars)

    rows = []
    for hm in hm_list:
        rows.append({
            "horizon": hm.h,
            "n_active": hm.n_active,
            "n_neutral": hm.n_neutral,
            "hit_rate%": round(hm.hit_rate, 2),
            "ic": round(hm.ic, 4),
            "dir_ic": round(hm.directional_ic, 4),
            "expectancy": round(hm.expectancy, 6),
            "calmar_proxy": round(hm.calmar_proxy, 3),
            "pnl_proxy": round(hm.pnl_proxy, 4),
            "avg_win": round(hm.avg_win, 6),
            "avg_loss": round(hm.avg_loss, 6),
            "avg_long_fwd%": round(hm.avg_long_fwd * 100, 4)
                if np.isfinite(hm.avg_long_fwd) else float("nan"),
            "avg_short_fwd%": round(hm.avg_short_fwd * 100, 4)
                if np.isfinite(hm.avg_short_fwd) else float("nan"),
        })
    summary_df = pd.DataFrame(rows)

    return AccuracyReport(
        horizons=hm_list, confusion=confusion,
        summary_df=summary_df, layer=layer,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Text + Markdown renderers
# ─────────────────────────────────────────────────────────────────────────────

def render_accuracy_text(rep: AccuracyReport, *, bar_label: str = "bar") -> str:
    lines: List[str] = [
        f"\n{rep.layer.upper()} Signal Accuracy — forward horizons",
        "=" * 78,
    ]
    df = rep.summary_df
    if df.empty:
        lines.append("  (no data)")
        return "\n".join(lines)
    lines.append(df.to_string(index=False))
    if rep.confusion is not None:
        lines.append(f"\nVol-regime × Bias confusion (mean signed fwd log-return, h=1):")
        lines.append(rep.confusion.pivot_mean.to_string(float_format=lambda v: f"{v:.5f}"))
        lines.append(f"\nHit rate % per (regime, bias) cell:")
        lines.append(rep.confusion.pivot_hit.to_string(float_format=lambda v: f"{v:.1f}%"))
    return "\n".join(lines)


def render_accuracy_markdown(rep: AccuracyReport) -> str:
    lines: List[str] = [
        f"## {rep.layer.capitalize()} signal accuracy",
        "",
        "Forward-horizon analysis: is the bias at time *t* predictive for time *t+h*?",
        "",
    ]
    df = rep.summary_df
    if df.empty:
        lines.append("*No data.*")
        return "\n".join(lines)

    lines.append("| h | Active | Neutral | Hit % | IC | Dir IC | Expectancy | Calmar | PnL proxy |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for _, r in df.iterrows():
        pnl = r.get("pnl_proxy", float("nan"))
        pnl_str = f"{float(pnl):,.4f}" if np.isfinite(pnl) else "n/a"
        lines.append(
            f"| {int(r['horizon'])} "
            f"| {int(r['n_active']):,} "
            f"| {int(r['n_neutral']):,} "
            f"| {r['hit_rate%']:.2f}% "
            f"| {r['ic']:.4f} "
            f"| {r['dir_ic']:.4f} "
            f"| {r['expectancy']:.6f} "
            f"| {r['calmar_proxy']:.3f} "
            f"| {pnl_str} |"
        )
    lines.append("")

    if rep.confusion is not None:
        lines.append("### Vol-regime × Bias — mean signed forward log-return (h=1)")
        lines.append("")
        m = rep.confusion.pivot_mean
        lines.append("| Regime / Bias | " + " | ".join(str(c) for c in m.columns) + " |")
        lines.append("|---" + "|---:" * len(m.columns) + "|")
        for idx, row in m.iterrows():
            lines.append(
                f"| **{idx}** | " + " | ".join(f"{v:.5f}" for v in row.values) + " |"
            )
        lines.append("")

        lines.append("### Hit rate % per cell (h=1)")
        lines.append("")
        h = rep.confusion.pivot_hit
        lines.append("| Regime / Bias | " + " | ".join(str(c) for c in h.columns) + " |")
        lines.append("|---" + "|---:" * len(h.columns) + "|")
        for idx, row in h.iterrows():
            lines.append(
                f"| **{idx}** | " + " | ".join(f"{v:.1f}%" for v in row.values) + " |"
            )
        lines.append("")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Plotting
# ─────────────────────────────────────────────────────────────────────────────

def _agg():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def plot_hit_rate_vs_horizon(rep: AccuracyReport, outpath) -> "Path":
    """Bar chart: hit rate % for each forward horizon."""
    from pathlib import Path
    plt = _agg()

    df = rep.summary_df
    if df.empty:
        return Path(outpath)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4))

    hs = df["horizon"].to_numpy()
    hrs = df["hit_rate%"].to_numpy()
    ics = df["ic"].to_numpy()
    dir_ics = df["dir_ic"].to_numpy()

    ax1 = axes[0]
    colors = ["#27ae60" if h >= 50 else "#c0392b" for h in hrs]
    ax1.bar([str(h) for h in hs], hrs, color=colors, alpha=0.8)
    ax1.axhline(50.0, color="grey", linestyle="--", linewidth=0.8, label="Chance (50%)")
    ax1.set_title(f"{rep.layer.capitalize()} — Hit rate vs horizon")
    ax1.set_xlabel("Forward horizon (bars)")
    ax1.set_ylabel("Hit rate %")
    ax1.set_ylim(0, 100)
    ax1.legend(frameon=False)
    ax1.grid(axis="y", alpha=0.25)

    ax2 = axes[1]
    ax2.plot([str(h) for h in hs], ics, "o-", color="#2980b9", label="IC (all bars)")
    ax2.plot([str(h) for h in hs], dir_ics, "s--", color="#8e44ad", label="Dir IC (active)")
    ax2.axhline(0.0, color="grey", linestyle="--", linewidth=0.8)
    ax2.set_title(f"{rep.layer.capitalize()} — Information Coefficient vs horizon")
    ax2.set_xlabel("Forward horizon (bars)")
    ax2.set_ylabel("Pearson IC")
    ax2.legend(frameon=False)
    ax2.grid(axis="y", alpha=0.25)

    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_forward_return_by_bias(
    rep: AccuracyReport, outpath, *, h_idx: int = 0
) -> "Path":
    """Box/violin of forward returns split by LONG / SHORT / NEUTRAL."""
    from pathlib import Path
    plt = _agg()

    if not rep.horizons:
        return Path(outpath)

    hm = rep.horizons[min(h_idx, len(rep.horizons) - 1)]
    data = {
        "LONG": hm.long_fwd_returns * 100.0,
        "NEUTRAL": hm.neutral_fwd_returns * 100.0,
        "SHORT": -hm.short_fwd_returns * 100.0,   # sign-adjusted (short = negative-fwd good)
    }
    data = {k: v for k, v in data.items() if v.size > 0}
    if not data:
        return Path(outpath)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = list(data.keys())
    vals = [data[k] for k in labels]
    parts = ax.violinplot(vals, showmedians=True, showextrema=True)
    for i, (pc, lbl) in enumerate(zip(parts["bodies"], labels)):
        color = "#27ae60" if lbl == "LONG" else ("#c0392b" if lbl == "SHORT" else "#95a5a6")
        pc.set_facecolor(color)
        pc.set_alpha(0.6)
    ax.set_xticks(range(1, len(labels) + 1))
    ax.set_xticklabels(labels)
    ax.axhline(0, color="grey", linewidth=0.8, linestyle="--")
    ax.set_ylabel("Forward log-return % (sign-adjusted)")
    ax.set_title(
        f"{rep.layer.capitalize()} — Forward return by bias at h={hm.h} bars"
    )
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_confusion_heatmap(rep: AccuracyReport, outpath) -> "Path":
    """Heatmap of vol_regime × bias → hit rate %."""
    from pathlib import Path
    plt = _agg()

    if rep.confusion is None or rep.confusion.pivot_hit.empty:
        return Path(outpath)

    import matplotlib.colors as mcolors
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 4))

    def _heat(ax, df: pd.DataFrame, title: str, fmt: str, cmap: str):
        vals = df.to_numpy(dtype=np.float64)
        im = ax.imshow(vals, cmap=cmap, aspect="auto",
                       vmin=np.nanmin(vals), vmax=np.nanmax(vals))
        ax.set_xticks(range(df.shape[1]))
        ax.set_xticklabels(df.columns, rotation=30, ha="right")
        ax.set_yticks(range(df.shape[0]))
        ax.set_yticklabels(df.index)
        for i in range(vals.shape[0]):
            for j in range(vals.shape[1]):
                ax.text(j, i, fmt.format(vals[i, j]),
                        ha="center", va="center", fontsize=9,
                        color="white" if abs(vals[i, j]) > (np.nanmax(vals) * 0.5) else "black")
        ax.set_title(title)
        fig.colorbar(im, ax=ax, shrink=0.8)

    _heat(ax1, rep.confusion.pivot_hit,
          "Hit rate % (regime × bias, h=1)", "{:.1f}%", "RdYlGn")
    _heat(ax2, rep.confusion.pivot_mean * 100.0,
          "Mean signed fwd% (regime × bias, h=1)", "{:.3f}", "RdYlGn")

    fig.suptitle(f"{rep.layer.capitalize()} regime × bias confusion", y=1.01)
    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_signal_timeline(
    bars: pd.DataFrame, rep: AccuracyReport, outpath,
    *, n_bars: int = 500
) -> "Path":
    """Tide signal timeline showing entries, exits, and NEUTRAL hand-off periods.

    Layout (4 panels, shared x-axis):
      Panel 1 – Price with background shading:
                   green  = LONG period (Tide directing long exposure)
                   red    = SHORT period (Tide directing short exposure)
                   grey   = NEUTRAL period (Tide is silent; Wave/Ripple take over)
                 Entry arrows (▲ LONG, ▼ SHORT) at the bar where the bias
                 *changes* to LONG/SHORT.
                 Exit  arrows (◆) at the bar where the bias *returns* to NEUTRAL.
      Panel 2 – Risk multiplier (0–1).  Filled area shows conviction.
                 Colour tracks the current bias (green/red/grey).
      Panel 3 – Volatility regime colour strip (LOW/NORMAL/HIGH/CRISIS).
      Panel 4 – Cumulative PnL (net, including fees) for context.
    """
    from pathlib import Path
    import matplotlib.patches as mpatches
    plt = _agg()

    if bars.empty:
        return Path(outpath)

    tail = bars.iloc[-min(n_bars, len(bars)):]
    idx = tail.index
    n = len(tail)

    bias_str = tail["bias"].astype(str).to_numpy()
    close     = tail["close"].to_numpy(dtype=np.float64)
    rm        = (
        tail["risk_mult"].to_numpy(dtype=np.float64)
        if "risk_mult" in tail.columns
        else np.ones(n, dtype=np.float64)
    )
    net_pnl   = (
        tail["net_pnl"].to_numpy(dtype=np.float64)
        if "net_pnl" in tail.columns
        else np.zeros(n, dtype=np.float64)
    )

    # ── Palette ────────────────────────────────────────────────────────
    COL_LONG    = "#27ae60"   # green
    COL_SHORT   = "#c0392b"   # red
    COL_NEUTRAL = "#bdc3c7"   # grey  (Wave/Ripple zone)
    COL_PRICE   = "#1c2833"
    ALPHA_BG    = 0.18        # background shading alpha
    ALPHA_RM    = 0.55        # risk-mult fill alpha

    def _bias_color(b: str) -> str:
        return COL_LONG if b == "LONG" else (COL_SHORT if b == "SHORT" else COL_NEUTRAL)

    # ── Detect transitions: entry and exit events ──────────────────────
    # entry_long[i]  = True on the first bar of a LONG run
    # entry_short[i] = True on the first bar of a SHORT run
    # exit[i]        = True on the first NEUTRAL bar after a LONG or SHORT run
    prev_bias = np.concatenate([["NEUTRAL"], bias_str[:-1]])
    entry_long  = (bias_str == "LONG")  & (prev_bias != "LONG")
    entry_short = (bias_str == "SHORT") & (prev_bias != "SHORT")
    exit_signal = (bias_str == "NEUTRAL") & (prev_bias != "NEUTRAL")

    # ── Background spans ───────────────────────────────────────────────
    # Build contiguous segments so we can axvspan each run efficiently.
    def _segments(bias_arr):
        segs = []
        i = 0
        while i < len(bias_arr):
            j = i + 1
            while j < len(bias_arr) and bias_arr[j] == bias_arr[i]:
                j += 1
            segs.append((i, j - 1, bias_arr[i]))
            i = j
        return segs

    # ── Figure ─────────────────────────────────────────────────────────
    fig, axes = plt.subplots(
        4, 1, figsize=(14, 10), sharex=True,
        gridspec_kw={"height_ratios": [4, 1.2, 0.6, 1.2]},
    )
    ax_price, ax_rm, ax_reg, ax_pnl = axes

    # ── Panel 1: Price + shading + entry/exit markers ──────────────────
    for i0, i1, bias in _segments(bias_str):
        t0 = idx[i0]
        t1 = idx[min(i1 + 1, n - 1)]  # extend span to next bar boundary
        ax_price.axvspan(t0, t1, color=_bias_color(bias), alpha=ALPHA_BG, linewidth=0)

    ax_price.plot(idx, close, color=COL_PRICE, linewidth=0.9, zorder=4)

    # Entry/exit markers — drawn only at transition bars
    # Use a price range-relative offset so arrows scale with the chart.
    price_range = close.max() - close.min() if close.max() != close.min() else close.mean() * 0.05
    offset_frac = price_range * 0.025  # 2.5% of visible range

    if entry_long.any():
        el_idx = idx[entry_long]
        el_px  = close[entry_long]
        # Up-arrow below price: tip at price - 0.5×offset, tail at price - 2.5×offset
        for xi, yi in zip(el_idx, el_px):
            ax_price.annotate(
                "", xy=(xi, yi - offset_frac * 0.5),
                xytext=(xi, yi - offset_frac * 2.5),
                arrowprops=dict(arrowstyle="-|>", color=COL_LONG, lw=1.8,
                                mutation_scale=10),
                zorder=7,
            )
        ax_price.scatter([], [], marker="^", color=COL_LONG, s=60,
                         label=f"LONG entry ({entry_long.sum()})")

    if entry_short.any():
        es_idx = idx[entry_short]
        es_px  = close[entry_short]
        # Down-arrow above price: tip at price + 0.5×offset, tail at price + 2.5×offset
        for xi, yi in zip(es_idx, es_px):
            ax_price.annotate(
                "", xy=(xi, yi + offset_frac * 0.5),
                xytext=(xi, yi + offset_frac * 2.5),
                arrowprops=dict(arrowstyle="-|>", color=COL_SHORT, lw=1.8,
                                mutation_scale=10),
                zorder=7,
            )
        ax_price.scatter([], [], marker="v", color=COL_SHORT, s=60,
                         label=f"SHORT entry ({entry_short.sum()})")

    if exit_signal.any():
        ex_idx = idx[exit_signal]
        ex_px  = close[exit_signal]
        # Diamond at price level for exits (→ NEUTRAL hand-off)
        ax_price.scatter(ex_idx, ex_px, marker="D", color="#8e44ad",
                         s=35, zorder=6, linewidths=0.5, edgecolors="white",
                         label=f"→ NEUTRAL ({exit_signal.sum()})")

    # Legend patches for shading
    patch_long    = mpatches.Patch(color=COL_LONG,    alpha=0.45, label="LONG period")
    patch_short   = mpatches.Patch(color=COL_SHORT,   alpha=0.45, label="SHORT period")
    patch_neutral = mpatches.Patch(color=COL_NEUTRAL, alpha=0.55,
                                   label="NEUTRAL (Wave/Ripple zone)")
    ax_price.legend(
        handles=[patch_long, patch_short, patch_neutral] +
                ax_price.get_legend_handles_labels()[0],
        loc="upper left", frameon=True, framealpha=0.85,
        fontsize=7, ncol=3,
    )
    ax_price.set_ylabel("Price")
    ax_price.set_title(
        f"{rep.layer.capitalize()} — Tide signal timeline  "
        f"(last {n} bars)",
        fontsize=10,
    )
    ax_price.grid(True, alpha=0.15)

    # ── Panel 2: Risk multiplier (conviction) ──────────────────────────
    # Fill colour tracks current bias
    for i0, i1, bias in _segments(bias_str):
        seg_idx = idx[i0 : i1 + 1]
        seg_rm  = rm[i0 : i1 + 1]
        color   = _bias_color(bias)
        ax_rm.fill_between(seg_idx, seg_rm, 0,
                           color=color, alpha=ALPHA_RM, linewidth=0)
        ax_rm.plot(seg_idx, seg_rm, color=color, linewidth=0.7)

    ax_rm.axhline(0, color="grey", linewidth=0.4)
    ax_rm.set_ylim(-0.05, 1.1)
    ax_rm.set_ylabel("Risk mult", fontsize=8)
    ax_rm.set_yticks([0, 0.5, 1.0])
    ax_rm.grid(True, alpha=0.15)
    ax_rm.annotate("conviction →", xy=(0.01, 0.85), xycoords="axes fraction",
                   fontsize=7, color="grey")

    # ── Panel 3: Vol regime strip ──────────────────────────────────────
    if "vol_regime" in tail.columns:
        from matplotlib.colors import ListedColormap
        regime_map = {"LOW": 0, "NORMAL": 1, "HIGH": 2, "CRISIS": 3}
        reg = tail["vol_regime"].astype(str).map(regime_map).fillna(1).to_numpy()
        reg_cmap = ListedColormap(["#a3e4d7", "#fef9e7", "#fad7a0", "#f1948a"])
        # Draw as horizontal bands
        for i0, i1, bias in _segments(tail["vol_regime"].astype(str).to_numpy()):
            rval = regime_map.get(bias, 1)
            color = reg_cmap(rval / 3.0)
            ax_reg.axvspan(idx[i0], idx[min(i1 + 1, n - 1)],
                           color=color, alpha=0.85, linewidth=0)
        ax_reg.set_yticks([])
        ax_reg.set_ylabel("Regime", fontsize=8)
        # Inline text labels for each regime block
        for i0, i1, bias in _segments(tail["vol_regime"].astype(str).to_numpy()):
            mid = idx[i0 + (i1 - i0) // 2]
            if i1 - i0 >= max(3, n // 80):   # only label wide-enough blocks
                ax_reg.text(mid, 0.5, bias[:3], ha="center", va="center",
                            fontsize=6, color="#2c3e50", transform=ax_reg.get_xaxis_transform())

    # ── Panel 4: Cumulative net PnL ────────────────────────────────────
    cum_pnl = np.cumsum(net_pnl)
    ax_pnl.plot(idx, cum_pnl, color="#2980b9", linewidth=0.9)
    ax_pnl.fill_between(idx, cum_pnl, 0,
                        where=cum_pnl >= 0, color="#2980b9", alpha=0.25, interpolate=True)
    ax_pnl.fill_between(idx, cum_pnl, 0,
                        where=cum_pnl < 0, color=COL_SHORT, alpha=0.25, interpolate=True)
    ax_pnl.axhline(0, color="grey", linewidth=0.5)
    ax_pnl.set_ylabel("Cum PnL", fontsize=8)
    ax_pnl.grid(True, alpha=0.15)

    fig.autofmt_xdate()
    fig.tight_layout(h_pad=0.3)
    out = Path(outpath)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_pnl_proxy_vs_horizon(rep: AccuracyReport, outpath) -> "Path":
    """PnL proxy per unit sizing, cumulated forward at each horizon."""
    from pathlib import Path
    plt = _agg()

    if not rep.horizons:
        return Path(outpath)

    hs = [hm.h for hm in rep.horizons]
    pnls = [hm.pnl_proxy for hm in rep.horizons]

    fig, ax = plt.subplots(figsize=(8, 4))
    colors = ["#27ae60" if p >= 0 else "#c0392b" for p in pnls]
    ax.bar([str(h) for h in hs], pnls, color=colors, alpha=0.8)
    ax.axhline(0, color="grey", linewidth=0.8)
    ax.set_xlabel("Forward horizon (bars)")
    ax.set_ylabel("Cumulative PnL proxy (sizing units)")
    ax.set_title(f"{rep.layer.capitalize()} — PnL proxy by forward horizon")
    ax.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    out = Path(outpath)
    fig.savefig(out, dpi=140)
    plt.close(fig)
    return out


def plot_all(
    bars: pd.DataFrame,
    rep: AccuracyReport,
    outdir,
    name: str,
) -> Dict[str, "Path"]:
    """Generate all accuracy plots and return {label: path}."""
    from pathlib import Path
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out: Dict[str, Path] = {}
    try:
        out["Signal timeline"] = plot_signal_timeline(
            bars, rep, outdir / f"{name}_accuracy_timeline.png"
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("timeline plot: %s", e)
    try:
        out["Hit rate vs horizon"] = plot_hit_rate_vs_horizon(
            rep, outdir / f"{name}_accuracy_hitrate.png"
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("hitrate plot: %s", e)
    try:
        out["Forward return by bias"] = plot_forward_return_by_bias(
            rep, outdir / f"{name}_accuracy_fwdret.png", h_idx=0
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("fwdret plot: %s", e)
    try:
        out["Regime-bias confusion"] = plot_confusion_heatmap(
            rep, outdir / f"{name}_accuracy_confusion.png"
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("confusion plot: %s", e)
    try:
        out["PnL proxy vs horizon"] = plot_pnl_proxy_vs_horizon(
            rep, outdir / f"{name}_accuracy_pnl.png"
        )
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("pnl plot: %s", e)
    return out
