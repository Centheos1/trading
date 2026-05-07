"""
Wave regime accuracy analysis — strategy.md §8.

Answers: "How accurate is the Wave regime classification over the forward
horizon h ∈ {1, 2, …, T}?"

This module is complementary to tide_accuracy.py:
  - tide_accuracy.py measures directional bias signal accuracy
  - wave_accuracy.py measures regime classification quality

Metrics computed
────────────────
Per-regime forward-return analysis
    For each regime label (MEAN_REVERSION, BREAKOUT, BREAKDOWN, NEUTRAL):
    • mean / median forward return at each horizon h
    • hit_rate: fraction of bars where the forward return aligns with the
      regime's expected direction
          BREAKOUT → positive return
          MEAN_REVERSION → (not applicable — no direction; use |fwd| < threshold)
          BREAKDOWN / NEUTRAL → regime is "correct" if volatility is elevated

Regime hit rate (directional)
    For BREAKOUT bars: hit = forward_return_h > 0
    For BREAKDOWN bars: hit = |forward_return_h| > cross-sectional median
    (i.e. regime anticipated increased volatility)

Regime IC
    Pearson correlation of (regime_expected_return_proxy × forward_return_h)
    over all bars.  BREAKOUT → +1, BREAKDOWN → -1, rest → 0.

Regime transition matrix
    count[i, j] = number of bars where regime transitions from i to j.
    Expressed as row-normalised probabilities.

Regime stability
    Mean number of consecutive bars in the same regime.

Regime × bias confusion table
    For stacked mode: per-(wave_regime, tide_bias) cell → mean forward return.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

REGIMES = ["MEAN_REVERSION", "BREAKOUT", "BREAKDOWN", "NEUTRAL"]

# Expected direction proxy per regime (+1 = trending up, -1 = volatile/down, 0 = none)
_REGIME_SIGNAL: Dict[str, int] = {
    "BREAKOUT": 1,     # regime anticipates continued trend → positive return proxy
    "BREAKDOWN": -1,   # regime anticipates instability → tend to be negative
    "MEAN_REVERSION": 0,
    "NEUTRAL": 0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeHorizonStats:
    """Per-regime accuracy statistics at a single forward horizon h."""
    regime: str
    h: int
    n: int                    # bars in this regime
    mean_fwd_return: float    # mean forward log-return for bars in this regime
    median_fwd_return: float
    hit_rate: float           # fraction of bars where fwd return aligns with regime signal
    ic: float                 # Pearson IC of regime signal × forward return


@dataclass
class TransitionMatrix:
    """Regime transition probabilities (row-normalised)."""
    regimes: List[str]
    counts: np.ndarray        # shape (n_regimes, n_regimes); counts[i, j] = i→j
    probs: np.ndarray         # row-normalised counts

    @classmethod
    def compute(cls, regime_series: pd.Series) -> "TransitionMatrix":
        labels = REGIMES
        idx = {r: i for i, r in enumerate(labels)}
        n = len(labels)
        counts = np.zeros((n, n), dtype=int)
        prev = None
        for r in regime_series:
            if prev is not None and prev in idx and r in idx:
                counts[idx[prev], idx[r]] += 1
            prev = r
        row_sum = counts.sum(axis=1, keepdims=True)
        row_sum = np.where(row_sum == 0, 1, row_sum)
        probs = counts / row_sum
        return cls(regimes=labels, counts=counts, probs=probs)


def _horizon_label(h: int, bar_seconds: float) -> str:
    """Human-readable time label for h bars of duration bar_seconds."""
    mins = h * bar_seconds / 60.0
    if mins < 60:
        return f"{int(round(mins))}min"
    hrs = mins / 60.0
    return f"{hrs:.0f}h" if hrs == int(hrs) else f"{hrs:.1f}h"


@dataclass
class RegimeAccuracyReport:
    """Complete Wave regime accuracy output."""
    layer: str = "WAVE"
    bar_seconds: float = 300.0      # bar duration; used for human-readable horizon labels
    horizons: List[int] = field(default_factory=list)
    regime_horizon_stats: List[RegimeHorizonStats] = field(default_factory=list)
    transition_matrix: Optional[TransitionMatrix] = None

    # Per-regime regime stability (mean run length in bars)
    regime_stability: Dict[str, float] = field(default_factory=dict)

    # Confusion: (wave_regime, tide_bias) → mean forward return at h=1
    regime_bias_mean_fwd: Optional[pd.DataFrame] = None

    # Overall regime hit rate (all regimes, all horizons): list indexed by horizon
    overall_hit_rate: List[float] = field(default_factory=list)
    overall_ic: List[float] = field(default_factory=list)

    def summary_text(self) -> str:
        lines = [f"=== Wave Regime Accuracy ({self.layer}) ===\n"]
        for h, hr, ic in zip(self.horizons, self.overall_hit_rate, self.overall_ic):
            label = _horizon_label(h, self.bar_seconds)
            lines.append(f"  h={h:>3} ({label:>6})  hit_rate={hr:.1%}  IC={ic:+.4f}")
        if self.transition_matrix is not None:
            lines.append("\n  Transition matrix (row → from, col → to):")
            tm = self.transition_matrix
            header = "         " + "  ".join(f"{r[:4]:>4}" for r in tm.regimes)
            lines.append(header)
            for i, r in enumerate(tm.regimes):
                row = "  ".join(f"{tm.probs[i, j]:.2f}" for j in range(len(tm.regimes)))
                lines.append(f"  {r[:4]:>4}  {row}")
        if self.regime_stability:
            lines.append("\n  Regime stability (mean run length, bars):")
            for r, v in self.regime_stability.items():
                lines.append(f"    {r:<18} {v:.1f}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Core computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_regime_accuracy_report(
    bars: pd.DataFrame,
    horizons: Sequence[int] = (1, 3, 6, 12, 24, 48),
    bar_seconds: float = 300.0,
) -> RegimeAccuracyReport:
    """Compute a full RegimeAccuracyReport from a WaveBacktestResult.bars DataFrame.

    Parameters
    ----------
    bars : pd.DataFrame
        Must contain columns: close, wave_regime.
        Optional: tide_bias (for confusion matrix).
    horizons : sequence of int
        Forward horizons in *bar counts*.  Defaults match the 5m timeframe
        (strategy.md §27.2 minimum update_interval_ms = 5000).
    bar_seconds : float
        Duration of each bar in seconds; used for human-readable labels.
    """
    horizons = list(horizons)
    close = bars["close"].astype(float)
    regime_s = bars["wave_regime"].astype(str)
    n = len(bars)

    # Pre-compute all forward log-returns
    log_ret = np.log(close / close.shift(1)).fillna(0.0).to_numpy(dtype=np.float64)

    def fwd_return(h: int) -> np.ndarray:
        # forward log-return from bar t to bar t+h
        arr = np.zeros(n, dtype=np.float64)
        for t in range(n - h):
            arr[t] = log_ret[t + 1: t + h + 1].sum()
        return arr

    regime_arr = regime_s.to_numpy()
    regime_signal_arr = np.array(
        [_REGIME_SIGNAL.get(r, 0) for r in regime_arr], dtype=np.float64
    )

    regime_horizon_stats: list[RegimeHorizonStats] = []
    overall_hit_rate: list[float] = []
    overall_ic: list[float] = []

    for h in horizons:
        fwd = fwd_return(h)
        valid = n - h   # last h bars cannot have forward returns

        # Overall hit rate: bars with non-zero signal only
        active_mask = (regime_signal_arr[:valid] != 0)
        if active_mask.sum() > 0:
            signed_fwd = regime_signal_arr[:valid] * fwd[:valid]
            hr = float((signed_fwd[active_mask] > 0).mean())
            sig_std = float(np.std(regime_signal_arr[:valid]))
            fwd_std = float(np.std(fwd[:valid]))
            if sig_std > 0 and fwd_std > 0:
                ic = float(np.corrcoef(regime_signal_arr[:valid], fwd[:valid])[0, 1])
                ic = 0.0 if np.isnan(ic) else ic
            else:
                ic = 0.0
        else:
            hr = 0.0
            ic = 0.0
        overall_hit_rate.append(hr)
        overall_ic.append(ic)

        # Per-regime stats
        for reg in REGIMES:
            mask = (regime_arr[:valid] == reg)
            cnt = int(mask.sum())
            if cnt == 0:
                regime_horizon_stats.append(RegimeHorizonStats(
                    regime=reg, h=h, n=0,
                    mean_fwd_return=0.0, median_fwd_return=0.0,
                    hit_rate=0.0, ic=0.0,
                ))
                continue
            fwd_reg = fwd[:valid][mask]
            mean_fwd = float(fwd_reg.mean())
            med_fwd = float(np.median(fwd_reg))
            sig = _REGIME_SIGNAL[reg]
            if sig != 0:
                hit_rate = float(((sig * fwd_reg) > 0).mean())
                # Within a single regime the signal is a constant, so its
                # stddev is 0 and Pearson IC is undefined.  Report 0.0 here;
                # the meaningful cross-regime IC is in overall_ic.
                ic_val = 0.0
            else:
                hit_rate = 0.0
                ic_val = 0.0
            regime_horizon_stats.append(RegimeHorizonStats(
                regime=reg, h=h, n=cnt,
                mean_fwd_return=mean_fwd,
                median_fwd_return=med_fwd,
                hit_rate=hit_rate,
                ic=ic_val,
            ))

    # Transition matrix
    transition = TransitionMatrix.compute(regime_s)

    # Regime stability: mean run length
    stability: dict[str, float] = {}
    for reg in REGIMES:
        runs = []
        run = 0
        for r in regime_arr:
            if r == reg:
                run += 1
            else:
                if run > 0:
                    runs.append(run)
                run = 0
        if run > 0:
            runs.append(run)
        stability[reg] = float(np.mean(runs)) if runs else 0.0

    # Regime × bias confusion (mean forward return h=1)
    regime_bias_df: Optional[pd.DataFrame] = None
    if "tide_bias" in bars.columns:
        fwd1 = fwd_return(1)
        tmp = pd.DataFrame({
            "wave_regime": regime_arr[:n - 1],
            "tide_bias": bars["tide_bias"].iloc[:n - 1].to_numpy(),
            "fwd1": fwd1[:n - 1],
        })
        if not tmp.empty:
            regime_bias_df = (
                tmp.groupby(["wave_regime", "tide_bias"])["fwd1"]
                .mean()
                .unstack(fill_value=0.0)
            )

    return RegimeAccuracyReport(
        layer="WAVE",
        bar_seconds=bar_seconds,
        horizons=horizons,
        regime_horizon_stats=regime_horizon_stats,
        transition_matrix=transition,
        regime_stability=stability,
        regime_bias_mean_fwd=regime_bias_df,
        overall_hit_rate=overall_hit_rate,
        overall_ic=overall_ic,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Plotting
# ─────────────────────────────────────────────────────────────────────────────

def plot_regime_accuracy(
    bars: pd.DataFrame,
    report: RegimeAccuracyReport,
    out_dir,
    name: str = "wave",
) -> dict:
    """Generate Wave regime accuracy plots.

    Returns dict mapping plot name → Path.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches
    from pathlib import Path

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict = {}

    _REGIME_COLORS = {
        "BREAKOUT": "#2196F3",       # blue
        "MEAN_REVERSION": "#4CAF50", # green
        "BREAKDOWN": "#F44336",      # red
        "NEUTRAL": "#9E9E9E",        # grey
    }

    # ── 1. Regime timeline ──────────────────────────────────────────────────
    fig, axes = plt.subplots(3, 1, figsize=(14, 9), sharex=True,
                             gridspec_kw={"height_ratios": [3, 1, 1]})
    ax_price, ax_regime, ax_eq = axes
    close = bars["close"]
    regime_s = bars["wave_regime"].astype(str)

    ax_price.plot(close.index, close.values, color="#212121", linewidth=0.8, label="Close")
    ax_price.set_ylabel("Price")
    ax_price.set_title(f"Wave Regime Timeline — {name}")
    ax_price.grid(True, alpha=0.3)

    # Shade price area by regime
    def _shade_axis(ax, regime_series, alpha=0.15):
        for i in range(len(regime_series)):
            if i + 1 < len(regime_series):
                start = regime_series.index[i]
                end = regime_series.index[i + 1]
                r = regime_series.iloc[i]
                color = _REGIME_COLORS.get(r, "#9E9E9E")
                ax.axvspan(start, end, alpha=alpha, color=color, linewidth=0)

    _shade_axis(ax_price, regime_s)

    # Regime numeric encoding for plotting
    _R_NUM = {"BREAKOUT": 3, "MEAN_REVERSION": 2, "NEUTRAL": 1, "BREAKDOWN": 0}
    r_num = regime_s.map(_R_NUM).fillna(1)
    colors_list = [_REGIME_COLORS.get(r, "#9E9E9E") for r in regime_s]
    ax_regime.scatter(regime_s.index, r_num.values, c=colors_list, s=8, linewidths=0)
    ax_regime.set_yticks([0, 1, 2, 3])
    ax_regime.set_yticklabels(["BKDN", "NEUT", "MR", "BKOT"], fontsize=7)
    ax_regime.set_ylabel("Regime", fontsize=8)
    ax_regime.grid(True, alpha=0.2)

    if "equity" in bars.columns:
        ax_eq.plot(bars.index, bars["equity"].values, color="#7B1FA2", linewidth=0.8)
        ax_eq.set_ylabel("Equity", fontsize=8)
        ax_eq.grid(True, alpha=0.2)

    patches = [mpatches.Patch(color=c, label=r) for r, c in _REGIME_COLORS.items()]
    ax_price.legend(handles=patches, loc="upper left", fontsize=7, ncol=2)

    fig.tight_layout()
    p = out_dir / f"{name}_wave_timeline.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    paths["timeline"] = p

    # ── 2. Hit rate vs horizon ──────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(report.horizons, [r * 100 for r in report.overall_hit_rate],
            marker="o", color="#1565C0", linewidth=1.5, label="Overall (BKOT+BKDN)")
    ax.axhline(50, color="#B0BEC5", linestyle="--", linewidth=1, label="50% (random)")
    ax.set_xlabel("Forward horizon (bars)")
    ax.set_ylabel("Hit rate (%)")
    ax.set_title(f"Regime Hit Rate vs Horizon — {name}")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = out_dir / f"{name}_wave_hit_rate.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    paths["hit_rate"] = p

    # ── 3. Per-regime mean forward return vs horizon ─────────────────────────
    fig, ax = plt.subplots(figsize=(9, 5))
    for reg in REGIMES:
        stats = [s for s in report.regime_horizon_stats if s.regime == reg]
        if not stats:
            continue
        hs = [s.h for s in stats]
        mfwds = [s.mean_fwd_return * 100 for s in stats]
        color = _REGIME_COLORS.get(reg, "#9E9E9E")
        ax.plot(hs, mfwds, marker="o", color=color, linewidth=1.5,
                label=reg, markersize=4)
    ax.axhline(0, color="#607D8B", linestyle="--", linewidth=0.8)
    ax.set_xlabel("Forward horizon (bars)")
    ax.set_ylabel("Mean forward log-return (%)")
    ax.set_title(f"Per-Regime Mean Forward Return vs Horizon — {name}")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = out_dir / f"{name}_wave_fwd_return.png"
    fig.savefig(p, dpi=150, bbox_inches="tight")
    plt.close(fig)
    paths["fwd_return"] = p

    # ── 4. Transition matrix heatmap ────────────────────────────────────────
    if report.transition_matrix is not None:
        import matplotlib.cm as cm
        tm = report.transition_matrix
        fig, ax = plt.subplots(figsize=(6, 5))
        im = ax.imshow(tm.probs, cmap="Blues", vmin=0, vmax=1)
        plt.colorbar(im, ax=ax, label="Transition probability")
        labels = [r[:4] for r in tm.regimes]
        ax.set_xticks(range(len(labels)))
        ax.set_yticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_yticklabels(labels, fontsize=8)
        ax.set_xlabel("To regime")
        ax.set_ylabel("From regime")
        ax.set_title(f"Regime Transition Matrix — {name}")
        for i in range(len(tm.regimes)):
            for j in range(len(tm.regimes)):
                ax.text(j, i, f"{tm.probs[i, j]:.2f}",
                        ha="center", va="center", fontsize=8,
                        color="white" if tm.probs[i, j] > 0.6 else "black")
        fig.tight_layout()
        p = out_dir / f"{name}_wave_transition.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths["transition"] = p

    # ── 5. Regime × bias confusion (stacked mode) ───────────────────────────
    if report.regime_bias_mean_fwd is not None:
        df_conf = report.regime_bias_mean_fwd * 100
        fig, ax = plt.subplots(figsize=(7, 4))
        im = ax.imshow(df_conf.values, cmap="RdYlGn",
                       vmin=-df_conf.abs().max().max(),
                       vmax=df_conf.abs().max().max())
        plt.colorbar(im, ax=ax, label="Mean forward log-return h=1 (%)")
        ax.set_xticks(range(len(df_conf.columns)))
        ax.set_yticks(range(len(df_conf.index)))
        ax.set_xticklabels(df_conf.columns, fontsize=8)
        ax.set_yticklabels(df_conf.index, fontsize=8)
        ax.set_xlabel("Tide Bias")
        ax.set_ylabel("Wave Regime")
        ax.set_title(f"Regime × Bias Mean Forward Return — {name}")
        for i in range(len(df_conf.index)):
            for j in range(len(df_conf.columns)):
                ax.text(j, i, f"{df_conf.values[i, j]:.3f}%",
                        ha="center", va="center", fontsize=7)
        fig.tight_layout()
        p = out_dir / f"{name}_wave_confusion.png"
        fig.savefig(p, dpi=150, bbox_inches="tight")
        plt.close(fig)
        paths["confusion"] = p

    return paths
