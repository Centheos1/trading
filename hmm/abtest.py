"""Phase 7V — HMM vs. rule-based A/B validation helpers.

Pure-Python utilities used by ``tools.hmm_abtest`` to:

1. Derive the HMM ``state_map`` from labelled V1 evidence/state pairs
   (each HMM hidden state maps to whichever ``RippleState`` integer it
   most often co-occurred with under the rule-based backend).
2. Compute per-metric deltas between two backtest result tuples and
   declare a winner per metric.
3. Format the comparison as both Markdown (human-readable, gets
   committed to ``reports/``) and a JSON-safe dict (machine-readable
   for downstream tooling).

These helpers are deliberately decoupled from the C++ engine so they
can be unit-tested without ``orderflow_engine`` being built.

The ``state_map`` derivation is required because Baum-Welch is
unsupervised: it learns ``K`` hidden states, but those states have no
intrinsic correspondence to the 8 ``RippleState`` enum values
(IDLE / WALL_FORMING / ABSORBING / EXHAUSTING / WITHDRAWING / BREAKING
/ REFILLING / STABILIZING). Mapping by majority vote against the
score-based backend's labels lets the HMM produce comparable
``RippleInferenceResult`` outputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import math


# Order of the 5-tuple that ``strategies.orderflow.backtest()`` returns.
METRIC_KEYS: Tuple[str, ...] = (
    "pnl", "max_drawdown", "num_trades", "sharpe_ratio", "cagr",
)

# Per-metric semantics: True = higher is better, False = lower is better.
# Trade count is informational only (ties always).
METRIC_HIGHER_IS_BETTER: Dict[str, Optional[bool]] = {
    "pnl": True,
    "max_drawdown": False,
    "num_trades": None,
    "sharpe_ratio": True,
    "cagr": True,
}

DEFAULT_RIPPLE_STATE_MAX = 8  # IDLE..STABILIZING (see RippleState enum)


def derive_state_map(
    hmm_assignments: Sequence[int],
    ground_truth_states: Sequence[int],
    K: int,
    *,
    fallback_state: int = 0,
) -> List[int]:
    """Return a length-``K`` list mapping each HMM hidden state to a
    ``RippleState`` integer by majority vote.

    Args:
        hmm_assignments: per-observation argmax HMM state index in
            ``[0, K)``. Length T.
        ground_truth_states: per-observation rule-based ``RippleState``
            integer in ``[0, NUM_STATES)``. Length T.
        K: number of HMM hidden states.
        fallback_state: ``RippleState`` integer to use if a hidden state
            never co-occurs with any observation (e.g. unused state).

    Returns:
        A length-``K`` list of ``RippleState`` integers suitable for the
        ``state_map`` field of the JSON model file consumed by
        ``HMMBasedInference::load_model_from_string``.

    Raises:
        ValueError: if the input sequences have mismatched lengths or
            if ``K <= 0``.
    """
    if K <= 0:
        raise ValueError(f"K must be positive, got {K}")
    if len(hmm_assignments) != len(ground_truth_states):
        raise ValueError(
            f"length mismatch: hmm_assignments={len(hmm_assignments)} "
            f"ground_truth_states={len(ground_truth_states)}"
        )

    counts: List[Dict[int, int]] = [dict() for _ in range(K)]
    for hk, gs in zip(hmm_assignments, ground_truth_states):
        if not (0 <= hk < K):
            continue  # silently skip out-of-range hidden states
        c = counts[hk]
        c[int(gs)] = c.get(int(gs), 0) + 1

    state_map: List[int] = []
    for k in range(K):
        if not counts[k]:
            state_map.append(int(fallback_state))
            continue
        # Tie-break by lowest RippleState int (deterministic).
        best_state, best_count = None, -1
        for state_int, count in sorted(counts[k].items()):
            if count > best_count:
                best_state = state_int
                best_count = count
        state_map.append(int(best_state))
    return state_map


@dataclass
class MetricDelta:
    """One row of the comparison table."""
    name: str
    score_value: float
    hmm_value: float
    delta: float
    relative_pct: Optional[float]
    higher_is_better: Optional[bool]
    winner: str  # "score" / "hmm" / "tie" / "n/a"


def _safe_relative_pct(score: float, hmm: float) -> Optional[float]:
    """Return ((hmm-score) / |score|) * 100 or None if score is 0."""
    if not math.isfinite(score) or not math.isfinite(hmm):
        return None
    if abs(score) < 1e-12:
        return None
    return (hmm - score) / abs(score) * 100.0


def _decide_winner(score: float, hmm: float,
                   higher_is_better: Optional[bool]) -> str:
    if higher_is_better is None:
        return "n/a"
    if not (math.isfinite(score) and math.isfinite(hmm)):
        return "n/a"
    if abs(score - hmm) < 1e-12:
        return "tie"
    if higher_is_better:
        return "hmm" if hmm > score else "score"
    return "hmm" if hmm < score else "score"


def compare_metrics(
    score_metrics: Dict[str, float],
    hmm_metrics: Dict[str, float],
) -> List[MetricDelta]:
    """Compute per-metric deltas + winners for two backtest result dicts.

    Both inputs must contain at least the ``METRIC_KEYS`` keys with
    numeric values. Missing keys are coerced to 0.0.
    """
    rows: List[MetricDelta] = []
    for key in METRIC_KEYS:
        s = float(score_metrics.get(key, 0.0))
        h = float(hmm_metrics.get(key, 0.0))
        higher = METRIC_HIGHER_IS_BETTER.get(key)
        rows.append(MetricDelta(
            name=key,
            score_value=s,
            hmm_value=h,
            delta=h - s,
            relative_pct=_safe_relative_pct(s, h),
            higher_is_better=higher,
            winner=_decide_winner(s, h, higher),
        ))
    return rows


def summarize_winner(rows: Sequence[MetricDelta]) -> str:
    """Produce a one-line verdict.

    HMM "wins" only if it strictly improves at least one of the
    decisive metrics (pnl / sharpe / cagr / max_drawdown) AND does not
    regress any of them. ``num_trades`` is informational and excluded
    from the verdict.
    """
    decisive = [r for r in rows if r.higher_is_better is not None]
    score_wins = sum(1 for r in decisive if r.winner == "score")
    hmm_wins = sum(1 for r in decisive if r.winner == "hmm")
    ties = sum(1 for r in decisive if r.winner == "tie")
    if hmm_wins > 0 and score_wins == 0:
        verdict = "HMM improves at least one metric without regression."
    elif hmm_wins == 0 and score_wins > 0:
        verdict = "Rule-based wins on all decisive metrics."
    elif hmm_wins == 0 and score_wins == 0:
        verdict = "Backends tied across all decisive metrics."
    else:
        verdict = (
            f"Mixed: HMM wins {hmm_wins}, rule-based wins {score_wins}, "
            f"ties {ties}. Inspect per-metric deltas before promoting."
        )
    return verdict


@dataclass
class AbtestSummary:
    """Top-level structured result of one A/B run."""
    symbol: str
    from_time_ms: int
    to_time_ms: int
    captured_at_iso: str = ""
    n_observations: int = 0
    K_chosen: int = 0
    K_candidates: List[int] = field(default_factory=list)
    bic: float = 0.0
    log_likelihood: float = 0.0
    score_metrics: Dict[str, float] = field(default_factory=dict)
    hmm_metrics: Dict[str, float] = field(default_factory=dict)
    score_decisions: int = 0
    hmm_decisions: int = 0
    state_map: List[int] = field(default_factory=list)
    score_state_histogram: Dict[int, int] = field(default_factory=dict)
    hmm_state_histogram: Dict[int, int] = field(default_factory=dict)
    model_json_path: str = ""
    notes: List[str] = field(default_factory=list)

    def winner_verdict(self) -> str:
        return summarize_winner(compare_metrics(
            self.score_metrics, self.hmm_metrics))


def _fmt_float(v: float, precision: int = 6) -> str:
    if not math.isfinite(v):
        return repr(v)
    return f"{v:.{precision}f}"


def _fmt_pct(v: Optional[float]) -> str:
    if v is None or not math.isfinite(v):
        return "n/a"
    return f"{v:+.2f}%"


def _fmt_state_histogram(hist: Dict[int, int]) -> str:
    if not hist:
        return "_(empty — no decisions captured)_"
    items = sorted(hist.items())
    return ", ".join(f"{k}:{v}" for k, v in items)


def format_comparison_report(summary: AbtestSummary) -> str:
    """Format an ``AbtestSummary`` as a Markdown report."""
    rows = compare_metrics(summary.score_metrics, summary.hmm_metrics)
    verdict = summarize_winner(rows)

    lines: List[str] = []
    lines.append(f"# HMM vs. Rule-Based A/B — {summary.symbol}")
    lines.append("")
    lines.append("> Phase 7V validation harness (closes the validation gap "
                 "documented in `implementation_plan.md` Phase 7).")
    lines.append("")

    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- **Symbol:** `{summary.symbol}`")
    lines.append(
        f"- **Window:** {summary.from_time_ms} → {summary.to_time_ms} "
        f"(epoch ms)"
    )
    if summary.captured_at_iso:
        lines.append(f"- **Captured at:** {summary.captured_at_iso}")
    lines.append(f"- **Observations captured (V1 evidence):** "
                 f"{summary.n_observations}")
    lines.append(f"- **K candidates evaluated:** "
                 f"{summary.K_candidates or 'n/a'}")
    lines.append(f"- **K chosen (BIC argmin):** {summary.K_chosen}")
    lines.append(f"- **HMM BIC:** {_fmt_float(summary.bic, 4)}")
    lines.append(f"- **HMM log-likelihood:** "
                 f"{_fmt_float(summary.log_likelihood, 4)}")
    if summary.model_json_path:
        lines.append(f"- **Trained model JSON:** "
                     f"`{summary.model_json_path}`")
    lines.append("")

    lines.append("## State map (HMM hidden state → RippleState int)")
    lines.append("")
    if summary.state_map:
        for k, st in enumerate(summary.state_map):
            lines.append(f"- hidden state {k} → RippleState int {st}")
    else:
        lines.append("_(empty)_")
    lines.append("")

    lines.append("## Metric comparison")
    lines.append("")
    lines.append("| Metric | Rule-based | HMM | Δ (HMM-rule) | Δ % | "
                 "Direction | Winner |")
    lines.append("|---|---:|---:|---:|---:|---|---|")
    for r in rows:
        direction = (
            "higher better" if r.higher_is_better is True else
            "lower better" if r.higher_is_better is False else
            "informational"
        )
        # num_trades reads cleaner as int.
        if r.name == "num_trades":
            sv = f"{int(r.score_value)}"
            hv = f"{int(r.hmm_value)}"
            dv = f"{int(r.delta):+d}"
        else:
            sv = _fmt_float(r.score_value)
            hv = _fmt_float(r.hmm_value)
            dv = f"{r.delta:+.6f}"
        lines.append(
            f"| `{r.name}` | {sv} | {hv} | {dv} | "
            f"{_fmt_pct(r.relative_pct)} | {direction} | "
            f"**{r.winner}** |"
        )
    lines.append("")

    lines.append("## Decision counts and state distributions")
    lines.append("")
    lines.append(
        f"- Rule-based ripple decisions captured: "
        f"{summary.score_decisions}"
    )
    lines.append(
        f"- HMM ripple decisions captured: {summary.hmm_decisions}")
    lines.append("")
    lines.append("**Triggering-state histogram (rule-based run):** "
                 f"{_fmt_state_histogram(summary.score_state_histogram)}")
    lines.append("")
    lines.append("**Triggering-state histogram (HMM run):** "
                 f"{_fmt_state_histogram(summary.hmm_state_histogram)}")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    lines.append(f"**{verdict}**")
    lines.append("")

    if summary.notes:
        lines.append("## Notes")
        lines.append("")
        for n in summary.notes:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines)


def format_comparison_json(summary: AbtestSummary) -> Dict[str, Any]:
    """JSON-safe dict version of the report (for CI / downstream tools)."""
    rows = compare_metrics(summary.score_metrics, summary.hmm_metrics)
    return {
        "phase": "7V",
        "schema": 1,
        "symbol": summary.symbol,
        "from_time_ms": int(summary.from_time_ms),
        "to_time_ms": int(summary.to_time_ms),
        "captured_at_iso": summary.captured_at_iso,
        "n_observations": int(summary.n_observations),
        "K_chosen": int(summary.K_chosen),
        "K_candidates": [int(k) for k in summary.K_candidates],
        "bic": float(summary.bic),
        "log_likelihood": float(summary.log_likelihood),
        "model_json_path": summary.model_json_path,
        "state_map": [int(s) for s in summary.state_map],
        "score_metrics": {k: float(v) for k, v in
                          summary.score_metrics.items()},
        "hmm_metrics": {k: float(v) for k, v in summary.hmm_metrics.items()},
        "score_decisions": int(summary.score_decisions),
        "hmm_decisions": int(summary.hmm_decisions),
        "score_state_histogram": {int(k): int(v) for k, v in
                                  summary.score_state_histogram.items()},
        "hmm_state_histogram": {int(k): int(v) for k, v in
                                summary.hmm_state_histogram.items()},
        "metric_rows": [
            {
                "name": r.name,
                "score_value": float(r.score_value),
                "hmm_value": float(r.hmm_value),
                "delta": float(r.delta),
                "relative_pct": (None if r.relative_pct is None
                                 else float(r.relative_pct)),
                "higher_is_better": r.higher_is_better,
                "winner": r.winner,
            }
            for r in rows
        ],
        "verdict": summarize_winner(rows),
        "notes": list(summary.notes),
    }


def now_iso() -> str:
    """Wall-clock timestamp in ISO-8601 UTC, used for report headers.

    Wrapped here so tests can monkey-patch it deterministically.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
