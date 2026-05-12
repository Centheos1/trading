"""Phase 7V / Phase 16 — HMM vs. rule-based A/B validation helpers.

Pure-Python utilities used by ``tools.hmm_abtest`` to:

1. Derive the HMM ``state_map`` from labelled V1 evidence/state pairs
   (each HMM hidden state maps to whichever ``RippleState`` integer it
   most often co-occurred with under the rule-based backend).
2. Compute per-metric deltas between two backtest result tuples and
   declare a winner per metric.
3. Format the comparison as both Markdown (human-readable, gets
   committed to ``reports/``) and a JSON-safe dict (machine-readable
   for downstream tooling).
4. (Phase 16) Run the harness across a campaign of (symbol, window)
   pairs, aggregate the per-pair :class:`AbtestSummary` into a
   :class:`CampaignVerdict`, and emit per-pair + aggregate Markdown
   reports along with a frozen ``config_snapshot.json``.

These helpers are deliberately decoupled from the C++ engine so they
can be unit-tested without ``orderflow_engine`` being built.

The ``state_map`` derivation is required because Baum-Welch is
unsupervised: it learns ``K`` hidden states, but those states have no
intrinsic correspondence to the 8 ``RippleState`` enum values
(IDLE / WALL_FORMING / ABSORBING / EXHAUSTING / WITHDRAWING / BREAKING
/ REFILLING / STABILIZING). Mapping by majority vote against the
score-based backend's labels lets the HMM produce comparable
``RippleInferenceResult`` outputs.

Phase 16 adds a campaign harness on top of the per-(symbol, window)
:func:`run_abtest` orchestrator that lives in ``tools.hmm_abtest``.
:func:`run_campaign` iterates over the cartesian product of
``symbols × windows``, calls ``tools.hmm_abtest.run_abtest`` (or any
caller-supplied stand-in) once per pair, populates the Phase 16
summary fields on each :class:`AbtestSummary`, and writes the per-pair
Markdown reports. :func:`aggregate_verdict` then collapses the list
of summaries into a single :class:`CampaignVerdict`, applying the
``win_ratio ≥ 0.60 AND median_sharpe_delta ≥ 0.10`` promotion rule
(thresholds are configurable via :class:`VerdictThreshold` so that
no magic constants live in the verdict path).
"""
from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

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
    """Top-level structured result of one A/B run.

    Phase 16 additions (``window_label`` / ``seed`` / ``config_snapshot_hash``
    / ``win_ratio`` / ``median_sharpe_delta`` / ``median_cagr_delta`` /
    ``max_drawdown`` / ``trade_count`` / ``winner``) all default to neutral
    values so existing Phase 7V callers (which only populate the legacy
    score/HMM metric dicts) continue to construct ``AbtestSummary``
    objects unchanged. Call :meth:`populate_phase16_fields` after
    setting ``score_metrics`` / ``hmm_metrics`` to derive the new
    fields, or pass them in explicitly.
    """
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
    # ---- Phase 16 fields (campaign harness) ---------------------------
    window_label: str = ""
    seed: int = 0
    config_snapshot_hash: str = ""
    win_ratio: float = 0.0
    median_sharpe_delta: float = 0.0
    median_cagr_delta: float = 0.0
    max_drawdown: float = 0.0   # HMM run's max_drawdown (this window)
    trade_count: int = 0        # HMM run's trade count (this window)
    winner: str = "tie"         # "hmm" / "score" / "tie" / "mixed"

    def winner_verdict(self) -> str:
        return summarize_winner(compare_metrics(
            self.score_metrics, self.hmm_metrics))

    def populate_phase16_fields(self) -> None:
        """Recompute the Phase 16 derived fields from the per-metric
        dicts. Idempotent. No-op when both metric dicts are empty.

        - ``win_ratio``: fraction of *decisive* metrics (per
          :data:`METRIC_HIGHER_IS_BETTER`) where HMM strictly beats
          rule-based. ``num_trades`` is informational and excluded
          (mirrors :func:`summarize_winner`'s definition of decisive).
        - ``median_sharpe_delta`` / ``median_cagr_delta``: HMM minus
          rule-based for this window. The "median" prefix is kept so
          per-window summaries and the campaign aggregate share the
          same field name; for one window the "median" of one value
          is that value.
        - ``max_drawdown`` / ``trade_count``: the HMM run's values
          for this window (not the deltas — those are reported in the
          comparison table).
        - ``winner``: one of ``"hmm"`` / ``"score"`` / ``"tie"`` /
          ``"mixed"``.
        """
        rows = compare_metrics(self.score_metrics, self.hmm_metrics)
        decisive = [r for r in rows if r.higher_is_better is not None]
        hmm_wins = sum(1 for r in decisive if r.winner == "hmm")
        score_wins = sum(1 for r in decisive if r.winner == "score")
        if decisive:
            self.win_ratio = hmm_wins / len(decisive)
        else:
            self.win_ratio = 0.0

        sharpe_score = float(self.score_metrics.get("sharpe_ratio", 0.0))
        sharpe_hmm = float(self.hmm_metrics.get("sharpe_ratio", 0.0))
        self.median_sharpe_delta = sharpe_hmm - sharpe_score

        cagr_score = float(self.score_metrics.get("cagr", 0.0))
        cagr_hmm = float(self.hmm_metrics.get("cagr", 0.0))
        self.median_cagr_delta = cagr_hmm - cagr_score

        self.max_drawdown = float(self.hmm_metrics.get("max_drawdown", 0.0))
        self.trade_count = int(self.hmm_metrics.get("num_trades", 0))

        if hmm_wins > 0 and score_wins == 0:
            self.winner = "hmm"
        elif score_wins > 0 and hmm_wins == 0:
            self.winner = "score"
        elif hmm_wins == 0 and score_wins == 0:
            self.winner = "tie"
        else:
            self.winner = "mixed"


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


# ===========================================================================
# Phase 16 — HMM A/B Campaign at Scale
# ===========================================================================
#
# All defaults are exported as module-level named constants so the CLI
# layer (``tools/hmm_abtest.py``) and unit tests both pin the same
# values. Per AGENT_STRATEGY_RULES.md §20, no magic constants in
# decision logic.

# Default verdict thresholds — Phase 16 spec, "promote only when
# win_ratio ≥ 0.60 AND median_sharpe_delta ≥ 0.10".
DEFAULT_VERDICT_WIN_RATIO_MIN: float = 0.60
DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN: float = 0.10

# Default seed for HMM trainer reproducibility (Phase 16 spec).
DEFAULT_CAMPAIGN_SEED: int = 42

# Default campaign coverage (matches the Phase 16 acceptance command:
# ``--symbols BTCUSDT,ETHUSDT --windows 30d,60d``).
DEFAULT_CAMPAIGN_SYMBOLS: Tuple[str, ...] = ("BTCUSDT", "ETHUSDT")
DEFAULT_CAMPAIGN_WINDOWS: Tuple[str, ...] = ("30d", "60d")

# Shorthand window vocabulary. Resolved relative to a caller-supplied
# ``now_ms`` so the harness is deterministic given a ``--now`` override.
SHORTHAND_WINDOWS_DAYS: Dict[str, int] = {
    "30d": 30,
    "60d": 60,
    "90d": 90,
}

# Phase 16 schema version for the JSON form of CampaignVerdict /
# AbtestSummary in reports. Bumped on incompatible field changes.
PHASE16_REPORT_SCHEMA: int = 1


@dataclass
class VerdictThreshold:
    """Promotion threshold for :func:`aggregate_verdict`.

    Defaults match Phase 16 spec: ``win_ratio ≥ 0.60`` AND
    ``median_sharpe_delta ≥ 0.10``. Both conditions must hold to
    promote (the AND is intentional — a campaign that wins on
    Sharpe but only in 40% of pairs is too noisy to risk Phase 17).
    """
    win_ratio_min: float = DEFAULT_VERDICT_WIN_RATIO_MIN
    median_sharpe_delta_min: float = DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN


@dataclass
class CampaignVerdict:
    """Aggregate Phase 16 verdict over a campaign of (symbol, window)
    pairs. The shape matches the "CampaignVerdict recorded here" block
    in ``implementation_plan.md`` §7.2 byte-for-byte (plus a few extra
    fields for downstream tooling: ``n_pairs``, ``threshold``,
    ``schema``).

    ``promote`` is the **only** field Phase 17's hard gate consults.
    """
    promote: bool = False
    win_ratio: float = 0.0
    median_sharpe_delta: float = 0.0
    median_cagr_delta: float = 0.0
    max_drawdown_delta: float = 0.0
    trade_count_delta: int = 0
    symbols: List[str] = field(default_factory=list)
    windows: List[str] = field(default_factory=list)
    config_snapshot_hash: str = ""
    recorded_by: str = ""
    recorded_at: str = ""
    n_pairs: int = 0
    threshold: VerdictThreshold = field(default_factory=VerdictThreshold)
    schema: int = PHASE16_REPORT_SCHEMA


def compute_config_snapshot_hash(cfg: Any) -> str:
    """Return the SHA-256 hex digest of ``cfg`` serialized with
    ``sort_keys=True``.

    Per Phase 16 spec, the canonical hash formula is
    ``hashlib.sha256(json.dumps(cfg, sort_keys=True).encode("utf-8"))
    .hexdigest()``. Use this single helper so the hash committed to
    the report header and the hash of the on-disk
    ``config_snapshot.json`` file always agree.
    """
    payload = json.dumps(cfg, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_config_snapshot(cfg: Any, path: Path) -> str:
    """Write ``cfg`` to ``path`` (sorted-keys JSON) and return the
    SHA-256 hash of the on-disk content. The hash matches
    :func:`compute_config_snapshot_hash` because both use
    ``sort_keys=True``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(cfg, sort_keys=True, indent=2)
    path.write_text(payload, encoding="utf-8")
    return compute_config_snapshot_hash(cfg)


def parse_window_arg(
    spec: str,
    *,
    now_ms: int,
) -> Tuple[int, int, str]:
    """Parse one ``--windows`` token into ``(from_ms, to_ms, label)``.

    Supports two forms:

    1. Explicit ISO range: ``YYYY-MM-DD:YYYY-MM-DD``. ``from`` resolves
       to ``00:00:00.000 UTC``; ``to`` resolves to ``23:59:59.999 UTC``.
    2. Shorthand: ``30d`` / ``60d`` / ``90d``. The window ends at
       ``now_ms`` and starts ``N * 86_400_000`` ms before. ``now_ms``
       must be supplied by the caller (the CLI uses ``--now`` for
       deterministic resolution; tests pass a fixed value).

    The third element of the tuple is a filename-safe label suitable
    for use in report filenames (``2024-01-01_2024-01-31`` for the ISO
    form, ``30d`` for the shorthand form).
    """
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("window spec is empty")

    # Shorthand form first.
    if spec in SHORTHAND_WINDOWS_DAYS:
        days = SHORTHAND_WINDOWS_DAYS[spec]
        to_ms = int(now_ms)
        from_ms = to_ms - days * 86_400_000
        return from_ms, to_ms, spec

    # Explicit ISO range.
    if ":" not in spec:
        raise ValueError(
            f"window spec {spec!r} is neither shorthand "
            f"({sorted(SHORTHAND_WINDOWS_DAYS)}) nor "
            "an ISO range YYYY-MM-DD:YYYY-MM-DD"
        )

    from_str, to_str = spec.split(":", 1)
    from_str = from_str.strip()
    to_str = to_str.strip()
    fmt = "%Y-%m-%d"
    try:
        from_dt = datetime.strptime(from_str, fmt).replace(tzinfo=timezone.utc)
        to_dt = datetime.strptime(to_str, fmt).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"window spec {spec!r} has invalid ISO date(s): {exc}"
        ) from exc
    to_dt = to_dt.replace(hour=23, minute=59, second=59,
                          microsecond=999_000)
    from_ms = int(from_dt.timestamp() * 1000)
    to_ms = int(to_dt.timestamp() * 1000)
    if to_ms <= from_ms:
        raise ValueError(
            f"window spec {spec!r}: end must be strictly after start"
        )
    label = f"{from_str}_{to_str}"
    return from_ms, to_ms, label


def _safe_window_label(label: str) -> str:
    """Strip characters that aren't safe for filenames."""
    return label.replace(":", "_").replace("/", "_").replace(" ", "_")


def per_pair_report_path(
    output_dir: Path, symbol: str, window_label: str, seed: int,
) -> Path:
    """Return the canonical ``hmm_campaign_{SYMBOL}_{WINDOW}_{seed}.md``
    path under ``output_dir``."""
    safe = _safe_window_label(window_label)
    return output_dir / f"hmm_campaign_{symbol}_{safe}_{seed}.md"


def aggregate_report_path(output_dir: Path, ts_tag: str) -> Path:
    """Return the canonical ``hmm_campaign_summary_{timestamp}.md``
    path under ``output_dir``."""
    return output_dir / f"hmm_campaign_summary_{ts_tag}.md"


def aggregate_verdict(
    results: Sequence[AbtestSummary],
    threshold: Optional[VerdictThreshold] = None,
    *,
    config_snapshot_hash: str = "",
    recorded_by: str = "",
    recorded_at: str = "",
) -> CampaignVerdict:
    """Collapse a list of per-(symbol, window) :class:`AbtestSummary`
    objects into a single :class:`CampaignVerdict`.

    Verdict rule (Phase 16 spec): ``promote`` is True iff
    ``win_ratio ≥ threshold.win_ratio_min`` AND
    ``median_sharpe_delta ≥ threshold.median_sharpe_delta_min``. Both
    must hold; either alone is insufficient (the AND is intentional).

    ``win_ratio`` is the fraction of input ``results`` whose
    per-window ``winner`` field is ``"hmm"``. ``median_sharpe_delta``
    / ``median_cagr_delta`` / ``max_drawdown_delta`` are the medians
    of the per-window deltas (HMM minus rule-based). ``trade_count_delta``
    is the median over pairs (rounded to int) of
    ``hmm_metrics["num_trades"] - score_metrics["num_trades"]``.

    Empty input → ``promote=False`` and zeroed metrics. The caller is
    expected to assert at least one pair before promoting; this
    function does not raise on empty input so it can be safely called
    on partial results.
    """
    th = threshold or VerdictThreshold()
    if not results:
        return CampaignVerdict(
            promote=False,
            win_ratio=0.0,
            median_sharpe_delta=0.0,
            median_cagr_delta=0.0,
            max_drawdown_delta=0.0,
            trade_count_delta=0,
            symbols=[],
            windows=[],
            config_snapshot_hash=config_snapshot_hash,
            recorded_by=recorded_by,
            recorded_at=recorded_at,
            n_pairs=0,
            threshold=th,
        )

    # Make sure every summary has its Phase 16 fields populated. Idempotent.
    for r in results:
        # If win_ratio/winner are still at defaults but metrics dicts are
        # populated, derive them. Caller may have passed pre-populated
        # summaries (run_campaign does), in which case this is a no-op.
        if r.winner == "tie" and (r.score_metrics or r.hmm_metrics) \
                and r.win_ratio == 0.0 and r.median_sharpe_delta == 0.0:
            r.populate_phase16_fields()

    n = len(results)
    hmm_pair_wins = sum(1 for r in results if r.winner == "hmm")
    win_ratio = hmm_pair_wins / n

    sharpe_deltas = [r.median_sharpe_delta for r in results]
    cagr_deltas = [r.median_cagr_delta for r in results]
    dd_deltas = [
        float(r.hmm_metrics.get("max_drawdown", 0.0))
        - float(r.score_metrics.get("max_drawdown", 0.0))
        for r in results
    ]
    trade_deltas = [
        int(r.hmm_metrics.get("num_trades", 0))
        - int(r.score_metrics.get("num_trades", 0))
        for r in results
    ]

    median_sharpe_delta = float(statistics.median(sharpe_deltas))
    median_cagr_delta = float(statistics.median(cagr_deltas))
    max_drawdown_delta = float(statistics.median(dd_deltas))
    trade_count_delta = int(round(statistics.median(trade_deltas)))

    promote = (win_ratio >= th.win_ratio_min) and \
              (median_sharpe_delta >= th.median_sharpe_delta_min)

    # Preserve declaration order; dedupe while keeping the first occurrence.
    seen_syms: Dict[str, None] = {}
    for r in results:
        seen_syms.setdefault(r.symbol, None)
    seen_wins: Dict[str, None] = {}
    for r in results:
        if r.window_label:
            seen_wins.setdefault(r.window_label, None)

    return CampaignVerdict(
        promote=promote,
        win_ratio=win_ratio,
        median_sharpe_delta=median_sharpe_delta,
        median_cagr_delta=median_cagr_delta,
        max_drawdown_delta=max_drawdown_delta,
        trade_count_delta=trade_count_delta,
        symbols=list(seen_syms.keys()),
        windows=list(seen_wins.keys()),
        config_snapshot_hash=config_snapshot_hash,
        recorded_by=recorded_by,
        recorded_at=recorded_at,
        n_pairs=n,
        threshold=th,
    )


# ---------------------------------------------------------------------------
# Per-pair + aggregate report formatters
# ---------------------------------------------------------------------------

def format_campaign_pair_report(
    summary: AbtestSummary,
    *,
    config_snapshot_hash: str = "",
    timestamp_iso: str = "",
) -> str:
    """Format one (symbol, window) AbtestSummary as a Phase 16
    Markdown report.

    Header includes: symbol, window, seed, config_snapshot_hash,
    timestamp. Body reuses :func:`format_comparison_report`'s metric
    table (so the per-pair report shows the full HMM-vs-rule-based
    comparison) and adds an explicit Phase 16 verdict block with
    ``win_ratio``, ``median_sharpe_delta``, ``median_cagr_delta``,
    ``max_drawdown``, ``trade_count``, ``winner``.
    """
    summary.populate_phase16_fields()
    base = format_comparison_report(summary)

    cfg_hash = config_snapshot_hash or summary.config_snapshot_hash
    ts = timestamp_iso or summary.captured_at_iso or now_iso()
    window_label = summary.window_label or "<unspecified>"

    header_lines: List[str] = []
    header_lines.append(f"# HMM Campaign Pair — {summary.symbol} / "
                        f"{window_label}")
    header_lines.append("")
    header_lines.append("> Phase 16 — HMM A/B Campaign at Scale "
                        "(per-(symbol, window) row).")
    header_lines.append("")
    header_lines.append("## Campaign metadata")
    header_lines.append("")
    header_lines.append(f"- **Symbol:** `{summary.symbol}`")
    header_lines.append(f"- **Window:** `{window_label}`")
    header_lines.append(f"- **Seed:** `{summary.seed}`")
    header_lines.append(f"- **Config snapshot hash (SHA-256):** "
                        f"`{cfg_hash or '<unset>'}`")
    header_lines.append(f"- **Timestamp:** {ts}")
    header_lines.append("")
    header_lines.append("## Phase 16 verdict (this pair)")
    header_lines.append("")
    header_lines.append("| Field | Value |")
    header_lines.append("|---|---|")
    header_lines.append(f"| `winner` | **{summary.winner}** |")
    header_lines.append(f"| `win_ratio` | {summary.win_ratio:.4f} |")
    header_lines.append(
        f"| `median_sharpe_delta` | {summary.median_sharpe_delta:+.6f} |")
    header_lines.append(
        f"| `median_cagr_delta` | {summary.median_cagr_delta:+.6f} |")
    header_lines.append(
        f"| `max_drawdown` (HMM run) | {summary.max_drawdown:.6f} |")
    header_lines.append(
        f"| `trade_count` (HMM run) | {summary.trade_count} |")
    header_lines.append("")
    header_lines.append("---")
    header_lines.append("")
    return "\n".join(header_lines) + base


def format_campaign_summary_report(
    results: Sequence[AbtestSummary],
    verdict: CampaignVerdict,
    *,
    timestamp_iso: str = "",
    seed: int = DEFAULT_CAMPAIGN_SEED,
) -> str:
    """Format the aggregate Phase 16 campaign report.

    Body: per-pair table (one row per (symbol, window)), followed by
    the :class:`CampaignVerdict` block in the format specified by
    ``implementation_plan.md`` §7.2 ("CampaignVerdict recorded here").
    Copy-paste-friendly so the user/agent who later runs the campaign
    on real data can paste the verdict block back into the
    implementation plan.
    """
    ts = timestamp_iso or now_iso()
    lines: List[str] = []
    lines.append("# HMM A/B Campaign Summary — Phase 16")
    lines.append("")
    lines.append("> Aggregate of all (symbol, window) pairs run in this "
                 "campaign. The `CampaignVerdict` block at the bottom "
                 "is the **hard evidence gate** for Phase 17 — paste it "
                 "into `implementation_plan.md §7.2` to record the run.")
    lines.append("")

    lines.append("## Campaign metadata")
    lines.append("")
    lines.append(f"- **Timestamp:** {ts}")
    lines.append(f"- **Seed:** `{seed}`")
    lines.append(f"- **Config snapshot hash (SHA-256):** "
                 f"`{verdict.config_snapshot_hash or '<unset>'}`")
    lines.append(f"- **Pair count:** {verdict.n_pairs}")
    sym_str = ", ".join(f"`{s}`" for s in verdict.symbols) or "_(none)_"
    win_str = ", ".join(f"`{w}`" for w in verdict.windows) or "_(none)_"
    lines.append(f"- **Symbols:** {sym_str}")
    lines.append(f"- **Windows:** {win_str}")
    lines.append(f"- **Verdict thresholds:** "
                 f"`win_ratio ≥ {verdict.threshold.win_ratio_min:.2f}`"
                 f" AND `median_sharpe_delta ≥ "
                 f"{verdict.threshold.median_sharpe_delta_min:+.2f}`")
    lines.append("")

    lines.append("## Per-pair results")
    lines.append("")
    lines.append(
        "| Symbol | Window | `pnl` (HMM) | `max_drawdown` (HMM) | "
        "`sharpe` (HMM) | `cagr` (HMM) | `trade_count` (HMM) | "
        "`win_ratio` | `winner` |"
    )
    lines.append(
        "|---|---|---:|---:|---:|---:|---:|---:|---|"
    )
    for r in results:
        pnl = float(r.hmm_metrics.get("pnl", 0.0))
        sharpe = float(r.hmm_metrics.get("sharpe_ratio", 0.0))
        cagr = float(r.hmm_metrics.get("cagr", 0.0))
        lines.append(
            f"| `{r.symbol}` | `{r.window_label}` | "
            f"{pnl:+.6f} | {r.max_drawdown:.6f} | {sharpe:+.6f} | "
            f"{cagr:+.6f} | {r.trade_count} | {r.win_ratio:.4f} | "
            f"**{r.winner}** |"
        )
    if not results:
        lines.append("| _(no pairs)_ | | | | | | | | |")
    lines.append("")

    lines.append("## CampaignVerdict")
    lines.append("")
    lines.append("```")
    lines.append("Phase 16 CampaignVerdict (recorded):")
    lines.append(f"  promote:              {str(verdict.promote).lower()}")
    lines.append(f"  win_ratio:            {verdict.win_ratio:.6f}")
    lines.append(f"  median_sharpe_delta:  {verdict.median_sharpe_delta:+.6f}")
    lines.append(f"  median_cagr_delta:    {verdict.median_cagr_delta:+.6f}")
    lines.append(f"  max_drawdown_delta:   {verdict.max_drawdown_delta:+.6f}")
    lines.append(f"  trade_count_delta:    {verdict.trade_count_delta:+d}")
    lines.append(f"  symbols:              {list(verdict.symbols)!r}")
    lines.append(f"  windows:              {list(verdict.windows)!r}")
    lines.append(f"  config_snapshot_hash: "
                 f"{verdict.config_snapshot_hash or '<unset>'}")
    lines.append(f"  recorded_by:          "
                 f"{verdict.recorded_by or '<fill in>'}")
    lines.append(f"  recorded_at:          "
                 f"{verdict.recorded_at or ts}")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "**Phase 17 hard gate:** Phase 17 work must not begin until "
        "`promote=True` is recorded in `implementation_plan.md §7.2`. "
        "If `promote=False`, the project owner must explicitly override "
        "in writing before any Phase 17 code is written."
    )
    lines.append("")
    return "\n".join(lines)


def campaign_verdict_to_dict(verdict: CampaignVerdict) -> Dict[str, Any]:
    """JSON-safe dict version of a :class:`CampaignVerdict`."""
    return {
        "schema": int(verdict.schema),
        "promote": bool(verdict.promote),
        "win_ratio": float(verdict.win_ratio),
        "median_sharpe_delta": float(verdict.median_sharpe_delta),
        "median_cagr_delta": float(verdict.median_cagr_delta),
        "max_drawdown_delta": float(verdict.max_drawdown_delta),
        "trade_count_delta": int(verdict.trade_count_delta),
        "symbols": list(verdict.symbols),
        "windows": list(verdict.windows),
        "config_snapshot_hash": str(verdict.config_snapshot_hash),
        "recorded_by": str(verdict.recorded_by),
        "recorded_at": str(verdict.recorded_at),
        "n_pairs": int(verdict.n_pairs),
        "threshold": {
            "win_ratio_min": float(verdict.threshold.win_ratio_min),
            "median_sharpe_delta_min":
                float(verdict.threshold.median_sharpe_delta_min),
        },
    }


# ---------------------------------------------------------------------------
# Campaign orchestrator
# ---------------------------------------------------------------------------

# Type alias for a single-pair runner. Matches ``tools.hmm_abtest.run_abtest``'s
# keyword-only signature; see ``run_campaign`` below for the call site.
CampaignPairRunner = Callable[..., Tuple[AbtestSummary, Path, Path]]


def run_campaign(
    *,
    symbols: Sequence[str],
    windows: Sequence[Tuple[int, int, str]],
    config: Dict[str, Any],
    seed: int = DEFAULT_CAMPAIGN_SEED,
    output_dir: Path,
    models_dir: Path,
    config_snapshot_hash: str = "",
    pair_runner: Optional[CampaignPairRunner] = None,
    pair_runner_kwargs: Optional[Dict[str, Any]] = None,
    label: str = "campaign",
    write_per_pair_report: bool = True,
) -> List[AbtestSummary]:
    """Run the HMM A/B harness across the cartesian product
    ``symbols × windows`` and return the per-pair :class:`AbtestSummary`
    list.

    The actual per-pair backtest is delegated to ``pair_runner`` (which
    must match the signature of ``tools.hmm_abtest.run_abtest``). When
    ``pair_runner`` is ``None``, this function imports and calls the
    real ``tools.hmm_abtest.run_abtest`` — but tests inject a stub that
    skips the C++ engine entirely.

    Each returned summary has its Phase 16 fields populated
    (``window_label``, ``seed``, ``config_snapshot_hash``,
    ``win_ratio``, etc.). Aggregate them with :func:`aggregate_verdict`
    to get the final :class:`CampaignVerdict`.

    Determinism: given identical ``config``, ``seed``, ``symbols``,
    ``windows`` (and identical ``pair_runner`` behaviour), the
    returned list of :class:`AbtestSummary` objects is byte-identical
    across runs. This is what acceptance criterion #4 ("Identical seed
    + identical data → identical per-window HMM training and test
    results") guarantees.

    Args:
        symbols: e.g. ``["BTCUSDT", "ETHUSDT"]``.
        windows: list of ``(from_ms, to_ms, label)`` triples, typically
            produced by :func:`parse_window_arg`.
        config: parsed ``config.json`` dict (passed through to the
            per-pair runner unchanged; also embedded in the snapshot
            file written by the CLI).
        seed: integer seed for HMM training (Phase 16: default 42).
        output_dir: directory to write per-pair Markdown reports under.
        models_dir: directory to write trained HMM JSON models under.
        config_snapshot_hash: SHA-256 hex digest of the canonical
            config JSON (see :func:`compute_config_snapshot_hash`).
            Embedded in every per-pair report header.
        pair_runner: test injection point. ``None`` triggers a real
            ``tools.hmm_abtest.run_abtest`` import + call.
        pair_runner_kwargs: extra kwargs forwarded to ``pair_runner``
            (e.g. ``ofe_module``, ``backtest_runner``, ``timeout_s``,
            ``params``, ``k_range``, ``exchange``).
        label: included in the per-pair runner's ``label`` arg; the
            per-pair report filename is overridden by
            :func:`per_pair_report_path` after the runner returns.
        write_per_pair_report: if True (default), copy the per-pair
            Markdown report into the canonical
            ``hmm_campaign_{SYMBOL}_{WINDOW}_{seed}.md`` filename.

    Raises:
        ValueError: if ``symbols`` or ``windows`` is empty.
    """
    if not symbols:
        raise ValueError("run_campaign requires at least one symbol")
    if not windows:
        raise ValueError("run_campaign requires at least one window")

    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    runner = pair_runner
    if runner is None:
        # Lazy-import to avoid a circular import at module load
        # (``tools.hmm_abtest`` imports from ``hmm.abtest``).
        from tools.hmm_abtest import run_abtest as _run_abtest  # noqa: PLC0415
        runner = _run_abtest

    extra_kwargs = dict(pair_runner_kwargs or {})

    results: List[AbtestSummary] = []
    for symbol in symbols:
        for from_ms, to_ms, window_label in windows:
            pair_label = f"{label}_{symbol}_{_safe_window_label(window_label)}"
            summary, md_path, _json_path = runner(
                symbol=symbol,
                from_time_ms=from_ms,
                to_time_ms=to_ms,
                label=pair_label,
                seed=seed,
                output_dir=output_dir,
                models_dir=models_dir,
                **extra_kwargs,
            )

            # Stamp Phase 16 metadata onto the returned summary.
            summary.window_label = window_label
            summary.seed = seed
            summary.config_snapshot_hash = config_snapshot_hash
            summary.populate_phase16_fields()

            # Re-emit the per-pair report under the canonical Phase 16
            # filename so the campaign output is grep-friendly. The
            # original timestamped report from ``run_abtest`` stays
            # untouched (it's part of the per-run audit trail).
            if write_per_pair_report:
                canonical_md = per_pair_report_path(
                    output_dir, symbol, window_label, seed)
                rendered = format_campaign_pair_report(
                    summary,
                    config_snapshot_hash=config_snapshot_hash,
                )
                canonical_md.write_text(rendered, encoding="utf-8")

            results.append(summary)

    return results
