"""Phase 7V / Phase 16 — HMM vs. rule-based backtest comparison harness.

End-to-end script that runs the HMM vs. rule-based A/B harness in one
of two modes:

- **Single-pair mode** (legacy, Phase 7V): ``--symbol`` + ``--from-time``
  + ``--to-time``. Runs the rule-based backtest, trains an HMM on the
  captured evidence, re-runs with HMM, writes one Markdown + JSON
  report to ``reports/``.

- **Campaign mode** (Phase 16): ``--symbols`` + ``--windows`` (each
  window is either ``YYYY-MM-DD:YYYY-MM-DD`` or shorthand ``30d`` /
  ``60d`` / ``90d``). Iterates over the cartesian product, writes one
  per-pair report (``hmm_campaign_{SYMBOL}_{WINDOW}_{seed}.md``) plus
  one aggregate (``hmm_campaign_summary_{timestamp}.md``) and a frozen
  ``config_snapshot.json``. Used for the multi-symbol / multi-window
  research evidence that gates Phase 17.

Both modes share the per-pair runner :func:`run_abtest` so the same
training + comparison code path is exercised end-to-end.

For testing without a built C++ engine, the campaign mode supports
``--dry-run`` which uses a synthetic stub backtester (no real data
needed). The unit tests reuse the same stub via direct import.

Closes the explicit validation gap from ``implementation_plan.md``
Phase 7 ("Actual HMM vs. rule-based backtest comparison ... requires
labeled V1 backtest data") and delivers the Phase 16 campaign harness
that records the :class:`CampaignVerdict` consumed by Phase 17's hard
dependency gate.

Usage::

    cd /Users/clintsellen/Documents/Trading/app/backtest

    # Single-pair (legacy Phase 7V):
    python -m tools.hmm_abtest \\
        --symbol BTCUSDT --exchange binance \\
        --from-time 2024-01-01 --to-time 2024-12-31 \\
        --label phase7v_smoke

    # Campaign (Phase 16):
    python -m tools.hmm_abtest \\
        --symbols BTCUSDT,ETHUSDT --windows 30d,60d --seed 42

    # Smoke / CI without a built engine + without market data:
    python -m tools.hmm_abtest \\
        --symbols BTCUSDT --windows 30d --seed 42 --dry-run

The harness is deterministic given the same tick store, parameters,
``--seed``, and (for shorthand windows) ``--now``. All defaults match
the post-13Y baseline (see
``reports/orderflow_BTCUSDT_post13Y_baseline.txt``).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Ensure project root is on sys.path when invoked via ``python tools/hmm_abtest.py``.
_PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJ_ROOT))

from hmm.abtest import (  # noqa: E402
    AbtestSummary,
    CampaignVerdict,
    DEFAULT_CAMPAIGN_SEED,
    DEFAULT_CAMPAIGN_SYMBOLS,
    DEFAULT_CAMPAIGN_WINDOWS,
    DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN,
    DEFAULT_VERDICT_WIN_RATIO_MIN,
    SHORTHAND_WINDOWS_DAYS,
    VerdictThreshold,
    aggregate_report_path,
    aggregate_verdict,
    campaign_verdict_to_dict,
    compute_config_snapshot_hash,
    derive_state_map,
    format_campaign_summary_report,
    format_comparison_json,
    format_comparison_report,
    now_iso,
    parse_window_arg,
    run_campaign,
    write_config_snapshot,
)
from hmm.hmm_trainer import HMMTrainer  # noqa: E402
from hmm.hmm_model import HMMModel  # noqa: E402

logger = logging.getLogger(__name__)

# Default config.json path used by the campaign mode for the
# config-snapshot SHA-256. Documented as the user-overridable
# ``--config-path`` flag.
DEFAULT_CONFIG_PATH: Path = _PROJ_ROOT / "config.json"


# Default parameter set — mirrors reports/orderflow_BTCUSDT_post13Y_baseline.txt.
DEFAULT_PARAMS: Dict[str, float] = {
    "imbalance_threshold": 3.0,
    "stacked_imbalance_levels": 3,
    "absorption_volume_ratio": 5.0,
    "cvd_divergence_lookback": 100,
    "exhaustion_lookback_bars": 5,
    "signal_strength_min": 0.3,
    "tick_size": 0.01,
    "wall_min_relative_size": 2.0,
    "absorption_entry": 0.4,
    "exhaustion_entry": 0.4,
    "breakout_entry": 0.5,
    "idle_exit_threshold": 0.3,
    "bounce_max_break_risk": 0.4,
    "feature_window_ms": 5000,
    "confirmation_window_ms": 5000,
    "max_hold_time_ms": 300000,
    "trailing_stop_sigma": 1.5,
    "target_distance_sigma": 2.5,
}


@dataclass
class _RunResult:
    pnl: float = 0.0
    max_drawdown: float = 0.0
    num_trades: int = 0
    sharpe_ratio: float = 0.0
    cagr: float = 0.0
    decisions: int = 0
    state_histogram: Dict[int, int] = field(default_factory=dict)
    evidence: List[List[float]] = field(default_factory=list)
    state_assignments: List[int] = field(default_factory=list)

    def as_metric_dict(self) -> Dict[str, float]:
        return {
            "pnl": float(self.pnl),
            "max_drawdown": float(self.max_drawdown),
            "num_trades": float(self.num_trades),
            "sharpe_ratio": float(self.sharpe_ratio),
            "cagr": float(self.cagr),
        }


def _import_ofe() -> Any:
    """Import the C++ orderflow_engine module."""
    sys.path.insert(0, str(_PROJ_ROOT / "backtestingCpp" / "orderflow" /
                           "build"))
    import orderflow_engine as ofe  # noqa: PLC0415
    return ofe


def _build_engine_config(ofe: Any, params: Dict[str, Any]) -> Any:
    """Build an EngineConfig from the params dict (delegates to the
    canonical strategies.orderflow._build_config so any future changes
    there flow through automatically)."""
    from strategies.orderflow import _build_config  # noqa: PLC0415
    cfg = _build_config(params)
    rcfg = cfg.ripple
    rcfg.paper_fills = True
    cfg.ripple = rcfg
    return cfg


def _run_single_backtest(
    ofe: Any,
    *,
    exchange: str,
    symbol: str,
    from_time_ms: int,
    to_time_ms: int,
    params: Dict[str, Any],
    capture_evidence: bool,
    timeout_s: float = 600.0,
) -> _RunResult:
    """Run one backtest end-to-end and return the metrics + (optionally)
    the per-decision evidence sequence.

    Mirrors the body of :func:`strategies.orderflow.backtest` but adds
    a ripple-decision callback that captures the 6-D evidence vector
    via ``ripple.last_evidence()`` after each decision fires. We can't
    use the existing ``backtest()`` directly because it doesn't expose
    a callback hook; replicating the engine wiring inline keeps the
    capture path obvious and avoids polluting the production path.
    """
    config = _build_engine_config(ofe, params)
    engine = ofe.OrderFlowEngine(config)

    result = _RunResult()

    if capture_evidence:
        ripple = engine.get_ripple()

        def on_decision(decision: Any) -> None:
            try:
                ev = ripple.last_evidence()
                result.evidence.append([
                    float(ev.absorption),
                    float(ev.exhaustion),
                    float(ev.withdrawal),
                    float(ev.breakout),
                    float(ev.refill),
                    float(ev.stabilization),
                ])
                # ``triggering_state`` is the RippleState the engine was
                # in when the decision fired — this is what the HMM will
                # learn to predict.
                state_int = int(decision.triggering_state)
                result.state_assignments.append(state_int)
            except Exception:
                logger.exception("evidence capture failed")
            result.state_histogram[
                int(decision.triggering_state)
            ] = result.state_histogram.get(
                int(decision.triggering_state), 0) + 1
            result.decisions += 1

        engine.set_ripple_callback(on_decision)
    else:
        # Still count decisions on the HMM run so we can compare
        # decision volume across backends.
        def on_decision_count(decision: Any) -> None:
            state_int = int(decision.triggering_state)
            result.state_histogram[state_int] = (
                result.state_histogram.get(state_int, 0) + 1)
            result.decisions += 1
        engine.set_ripple_callback(on_decision_count)

    tick_store_path = str(_PROJ_ROOT / "data" / f"{exchange}_ticks.h5")
    if not os.path.exists(tick_store_path):
        raise FileNotFoundError(
            f"Tick data file not found: {tick_store_path}. "
            "Collect tick data first using the data collection mode.")

    store = ofe.TickStore(tick_store_path)
    replay = ofe.ReplayFeed(store, 0.0)
    replay.set_time_range(from_time_ms, to_time_ms)

    ofe.connect_feed(engine, replay)
    engine.start(symbol)

    deadline = time.monotonic() + timeout_s
    while not replay.is_complete():
        if time.monotonic() > deadline:
            logger.warning(
                "backtest replay did not complete within %.1f s; "
                "forcing engine.stop()", timeout_s)
            break
        time.sleep(0.05)

    ripple = engine.get_ripple()
    ripple_trades = ripple.completed_trades()
    ripple_pnl = ripple.cumulative_pnl()
    ripple_dd = ripple.lifecycle_max_drawdown()

    sig_engine = engine.get_signal_engine()
    sig_pnl = sig_engine.get_pnl()
    sig_dd = sig_engine.get_max_drawdown()
    sig_trades = sig_engine.get_num_trades()

    if ripple_trades > 0:
        result.pnl = float(ripple_pnl)
        result.max_drawdown = float(ripple_dd)
        result.num_trades = int(ripple_trades)
    else:
        result.pnl = float(sig_pnl)
        result.max_drawdown = float(sig_dd)
        result.num_trades = int(sig_trades)

    returns = sig_engine.get_returns()
    result.sharpe_ratio = float(_compute_sharpe(returns))
    result.cagr = float(_compute_cagr(returns, from_time_ms, to_time_ms))

    engine.stop()
    store.close()
    return result


def _compute_sharpe(returns: List[float], risk_free: float = 0.0) -> float:
    if len(returns) < 2:
        return 0.0
    r = np.diff(returns)
    if np.std(r) == 0:
        return 0.0
    return float((np.mean(r) - risk_free) / np.std(r))


def _compute_cagr(returns: List[float], from_time: int, to_time: int,
                  initial: float = 10000.0) -> float:
    if not returns or from_time >= to_time:
        return 0.0
    final_value = initial * (1.0 + returns[-1] / 100.0)
    years = (to_time - from_time) / (365.25 * 24 * 3600 * 1000)
    if years <= 0 or final_value <= 0:
        return 0.0
    return float((final_value / initial) ** (1.0 / years) - 1.0) * 100.0


def _train_hmm_from_capture(
    evidence: List[List[float]],
    state_assignments: List[int],
    *,
    k_range: List[int],
    seed: int,
) -> Tuple[HMMModel, List[int], List[int]]:
    """Train HMM on captured evidence + derive state_map.

    Returns ``(best_model, state_map, K_candidates)``.
    """
    if len(evidence) < 4:
        raise ValueError(
            f"need at least 4 evidence observations to train HMM, "
            f"got {len(evidence)}; widen the date range or relax "
            f"signal thresholds")

    # Phase 16 determinism rule (AGENT_STRATEGY_RULES.md §7.1 +
    # implementation_plan.md §7.2 acceptance #4): ``HMMTrainer`` is
    # deterministic-by-construction (quantile-based init, no PRNG
    # calls), so the seed does not currently feed any random state.
    # We allocate a local ``default_rng(seed)`` anyway so future
    # trainer changes that introduce randomized restarts can plumb
    # the seed through without touching every caller. Crucially we
    # do **NOT** call ``np.random.seed(seed)`` here — that mutates
    # global numpy state and would break the determinism contract
    # for any concurrent caller.
    _rng = np.random.default_rng(seed)  # noqa: F841 — reserved for forward-compat
    obs = np.array(evidence, dtype=np.float64)

    trainer = HMMTrainer()
    best_model, _ = trainer.select_model(obs, k_range=k_range)
    K = best_model.K

    # Derive the state_map from the rule-based labels.
    # First we need each observation's MAP HMM state, which means
    # running a forward pass with the trained model. The trainer
    # doesn't return per-observation assignments, so we recompute the
    # log-emission for each observation against each state and
    # argmax over them. (For long sequences a proper Viterbi pass is
    # better, but argmax-of-emission is sufficient to align hidden
    # states with rule-based labels for state_map derivation.)
    means = best_model.means
    variances = best_model.variances
    inv_var = 1.0 / variances
    log_norm = -0.5 * np.sum(
        np.log(2.0 * np.pi * variances), axis=1)  # (K,)

    log_emit = np.zeros((obs.shape[0], K), dtype=np.float64)
    for k in range(K):
        diff = obs - means[k]
        log_emit[:, k] = log_norm[k] - 0.5 * np.sum(
            diff * diff * inv_var[k], axis=1)
    hmm_assignments = np.argmax(log_emit, axis=1).tolist()

    state_map = derive_state_map(
        hmm_assignments, state_assignments, K=K, fallback_state=0)
    best_model.state_map = state_map
    return best_model, state_map, k_range


def _parse_date_arg(s: str, *, end_of_day: bool = False) -> int:
    """Parse a YYYY-MM-DD string or epoch-ms int into epoch ms.

    ``end_of_day=True`` rounds the day up to ``23:59:59.999`` UTC so
    ``--from-time YYYY-MM-DD --to-time YYYY-MM-DD`` covers the full day.
    """
    s = (s or "").strip()
    if not s:
        raise ValueError("date arg is empty")
    if s.lstrip("-").isdigit():
        return int(s)
    fmt = "%Y-%m-%d"
    dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999_000)
    return int(dt.timestamp() * 1000)


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tools.hmm_abtest",
        description=(
            "Phase 7V (single-pair) / Phase 16 (campaign) — HMM vs. "
            "rule-based backtest A/B harness."
        ),
    )
    # ---- Single-pair mode (Phase 7V, legacy) -------------------------
    p.add_argument("--symbol", default="BTCUSDT",
                   help="single-pair mode: symbol (default: BTCUSDT). "
                        "Ignored when --symbols is set.")
    p.add_argument("--exchange", default="binance",
                   help="exchange / TickStore prefix (default: binance, "
                        "reads data/<exchange>_ticks.h5)")
    p.add_argument("--from-time", default=None,
                   help="single-pair mode: start date (YYYY-MM-DD) or "
                        "epoch ms. Required when --symbols/--windows "
                        "are not given.")
    p.add_argument("--to-time", default=None,
                   help="single-pair mode: end date (YYYY-MM-DD) or "
                        "epoch ms. Required when --symbols/--windows "
                        "are not given.")
    # ---- Phase 16 campaign mode --------------------------------------
    p.add_argument(
        "--symbols", default=None,
        help="Phase 16 campaign mode: comma-separated symbols, e.g. "
             "'BTCUSDT,ETHUSDT'. Triggers campaign mode when set. "
             f"Default if --windows is set: {','.join(DEFAULT_CAMPAIGN_SYMBOLS)}.",
    )
    p.add_argument(
        "--windows", default=None,
        help="Phase 16 campaign mode: comma-separated window list. Each "
             "token is either an explicit ISO range "
             "'YYYY-MM-DD:YYYY-MM-DD' or shorthand from "
             f"{sorted(SHORTHAND_WINDOWS_DAYS)} (resolved relative to "
             "--now). Triggers campaign mode when set. Default if "
             f"--symbols is set: {','.join(DEFAULT_CAMPAIGN_WINDOWS)}.",
    )
    p.add_argument(
        "--now", type=int, default=None,
        help="Phase 16 campaign mode: UTC epoch ms used as 'now' for "
             "shorthand window resolution. Default: actual UTC now. "
             "Pass a fixed value for deterministic replay of a campaign.",
    )
    p.add_argument(
        "--verdict-threshold", default=None,
        help="Phase 16 campaign mode: '<win_ratio_min>,<sharpe_delta_min>' "
             f"(default: '{DEFAULT_VERDICT_WIN_RATIO_MIN:.2f},"
             f"{DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN:.2f}'). "
             "Promotion requires win_ratio >= win_ratio_min AND "
             "median_sharpe_delta >= sharpe_delta_min (both).",
    )
    p.add_argument(
        "--config-path", default=str(DEFAULT_CONFIG_PATH),
        help="Phase 16 campaign mode: path to canonical config.json. The "
             "SHA-256 of this file (computed with sort_keys=True) is "
             "embedded in every report and written next to them as "
             f"config_snapshot.json. Default: {DEFAULT_CONFIG_PATH}.",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Phase 16 campaign mode: use a synthetic stub backtester "
             "(no C++ engine, no market data). Writes the per-pair + "
             "aggregate reports + config snapshot for smoke testing. "
             "Single-pair mode ignores this flag.",
    )
    # ---- Shared knobs -----------------------------------------------
    p.add_argument(
        "--label", default="phase7v",
        help="label suffix for output filenames (default: phase7v; "
             "single-pair mode only — campaign mode uses a fixed "
             "'campaign' prefix).")
    p.add_argument(
        "--k-range", default="3,4,5,6",
        help="comma-separated K candidates for HMM model selection "
             "(default: 3,4,5,6)")
    p.add_argument(
        "--seed", type=int, default=DEFAULT_CAMPAIGN_SEED,
        help=f"seed for HMM training reproducibility "
             f"(default: {DEFAULT_CAMPAIGN_SEED}). Same seed + same "
             "data + same config → identical per-window results.")
    p.add_argument(
        "--output-dir", default=None,
        help="report output dir (default: reports/)")
    p.add_argument(
        "--models-dir", default=None,
        help="trained-model output dir (default: models/)")
    p.add_argument(
        "--params-json", default=None,
        help="optional JSON file with EngineConfig params overrides "
             "(merged on top of DEFAULT_PARAMS)")
    p.add_argument(
        "--timeout-s", type=float, default=600.0,
        help="per-run replay timeout in seconds (default: 600)")
    p.add_argument(
        "--quiet", action="store_true",
        help="suppress progress logging")
    return p


def run_abtest(
    *,
    symbol: str,
    exchange: str,
    from_time_ms: int,
    to_time_ms: int,
    label: str,
    k_range: List[int],
    seed: int,
    output_dir: Path,
    models_dir: Path,
    params: Dict[str, Any],
    timeout_s: float,
    ofe_module: Any = None,
    backtest_runner: Any = None,
) -> Tuple[AbtestSummary, Path, Path]:
    """Run the full A/B harness end-to-end.

    Args:
        backtest_runner: test-only injection. When provided, must be a
            callable matching ``_run_single_backtest``'s signature.
            Defaults to the real C++-backed implementation.
        ofe_module: test-only injection of the imported ``orderflow_engine``
            module. ``None`` triggers a real import.

    Returns:
        ``(summary, md_path, json_path)`` where ``md_path`` and
        ``json_path`` point at the freshly-written reports.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    models_dir.mkdir(parents=True, exist_ok=True)

    if ofe_module is None and backtest_runner is None:
        ofe_module = _import_ofe()

    runner = backtest_runner or _run_single_backtest

    logger.info("Run 1 (rule-based) starting on %s [%s, %s)",
                symbol, from_time_ms, to_time_ms)
    score_run = runner(
        ofe_module,
        exchange=exchange, symbol=symbol,
        from_time_ms=from_time_ms, to_time_ms=to_time_ms,
        params=params, capture_evidence=True, timeout_s=timeout_s,
    )
    logger.info(
        "Run 1 done: pnl=%.6f trades=%d decisions=%d evidence=%d",
        score_run.pnl, score_run.num_trades, score_run.decisions,
        len(score_run.evidence),
    )

    logger.info(
        "Training HMM on %d evidence vectors (K candidates=%s, seed=%d)",
        len(score_run.evidence), k_range, seed,
    )
    model, state_map, k_used = _train_hmm_from_capture(
        score_run.evidence, score_run.state_assignments,
        k_range=k_range, seed=seed,
    )

    ts_tag = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    model_path = models_dir / f"hmm_{symbol}_{label}_{ts_tag}.json"
    model.save(str(model_path))
    logger.info("Saved trained HMM model to %s "
                "(K=%d, BIC=%.4f, ll=%.4f)",
                model_path, model.K, model.bic, model.log_likelihood)

    hmm_params = dict(params)
    hmm_params["hmm_enabled"] = True
    hmm_params["hmm_model_path"] = str(model_path)

    logger.info("Run 2 (HMM-backed) starting")
    hmm_run = runner(
        ofe_module,
        exchange=exchange, symbol=symbol,
        from_time_ms=from_time_ms, to_time_ms=to_time_ms,
        params=hmm_params, capture_evidence=False, timeout_s=timeout_s,
    )
    logger.info(
        "Run 2 done: pnl=%.6f trades=%d decisions=%d",
        hmm_run.pnl, hmm_run.num_trades, hmm_run.decisions,
    )

    summary = AbtestSummary(
        symbol=symbol,
        from_time_ms=from_time_ms,
        to_time_ms=to_time_ms,
        captured_at_iso=now_iso(),
        n_observations=len(score_run.evidence),
        K_chosen=model.K,
        K_candidates=list(k_used),
        bic=float(model.bic),
        log_likelihood=float(model.log_likelihood),
        score_metrics=score_run.as_metric_dict(),
        hmm_metrics=hmm_run.as_metric_dict(),
        score_decisions=score_run.decisions,
        hmm_decisions=hmm_run.decisions,
        state_map=list(state_map),
        score_state_histogram=dict(score_run.state_histogram),
        hmm_state_histogram=dict(hmm_run.state_histogram),
        model_json_path=str(model_path),
    )

    if score_run.decisions == 0:
        summary.notes.append(
            "Rule-based run captured ZERO ripple decisions — HMM had no "
            "training labels and the state_map fell back to RippleState 0 "
            "(IDLE). Loosen `signal_strength_min` / `*_entry` thresholds "
            "or widen the date range, then re-run."
        )
    if score_run.num_trades == 0 and hmm_run.num_trades == 0:
        summary.notes.append(
            "Both runs completed ZERO lifecycle trades — comparison is "
            "purely structural. Re-tune entries before drawing conclusions."
        )

    md_path = output_dir / f"hmm_abtest_{symbol}_{label}_{ts_tag}.md"
    json_path = output_dir / f"hmm_abtest_{symbol}_{label}_{ts_tag}.json"
    md_path.write_text(format_comparison_report(summary), encoding="utf-8")
    json_path.write_text(
        json.dumps(format_comparison_json(summary), indent=2),
        encoding="utf-8",
    )
    logger.info("Wrote report: %s", md_path)
    logger.info("Wrote JSON:   %s", json_path)

    return summary, md_path, json_path


def _parse_verdict_threshold_arg(spec: Optional[str]) -> VerdictThreshold:
    """Parse the ``--verdict-threshold`` CLI value.

    Accepts ``None`` (use defaults) or a string of the form
    ``'<win_ratio_min>,<sharpe_delta_min>'``. Both fields are floats.
    """
    if spec is None:
        return VerdictThreshold()
    parts = [s.strip() for s in spec.split(",")]
    if len(parts) != 2:
        raise ValueError(
            f"--verdict-threshold must be 'win_ratio_min,sharpe_delta_min', "
            f"got {spec!r}"
        )
    try:
        wr = float(parts[0])
        sd = float(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"--verdict-threshold parse error: {exc}"
        ) from exc
    return VerdictThreshold(win_ratio_min=wr, median_sharpe_delta_min=sd)


def _make_dry_run_backtest_runner(seed: int):
    """Build a synthetic stub backtester for ``--dry-run`` mode.

    Mimics the signature of :func:`_run_single_backtest` but returns
    deterministic synthetic data derived from the seed. The first call
    (``capture_evidence=True``) yields two well-separated 6-D Gaussian
    clusters; the second (``capture_evidence=False``) yields HMM-side
    metrics with a small positive sharpe + cagr delta over the
    rule-based side. Reused by Phase 16 unit tests via direct import.
    """
    rng = np.random.default_rng(seed)
    n_per = 30
    cluster_a = rng.normal(loc=[0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
                           scale=0.05, size=(n_per, 6))
    cluster_b = rng.normal(loc=[0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
                           scale=0.05, size=(n_per, 6))
    evidence = np.vstack([cluster_a, cluster_b]).tolist()
    state_assignments = [2] * n_per + [3] * n_per
    # Pick HMM-side values that clearly clear the default Phase 16
    # promotion thresholds (win_ratio ≥ 0.60 AND median_sharpe_delta
    # ≥ 0.10) so the dry-run report exercises the promote=True path.
    # Sharpe delta = 0.25 (well above the 0.10 floor).
    score_metrics = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                     "sharpe_ratio": 0.50, "cagr": 1.0}
    hmm_metrics = {"pnl": 2.0, "max_drawdown": 0.05, "num_trades": 6,
                   "sharpe_ratio": 0.75, "cagr": 1.5}

    def runner(
        ofe: Any,
        *,
        exchange: str,
        symbol: str,
        from_time_ms: int,
        to_time_ms: int,
        params: Dict[str, Any],
        capture_evidence: bool,
        timeout_s: float,
    ) -> _RunResult:
        result = _RunResult()
        if capture_evidence:
            result.evidence = list(evidence)
            result.state_assignments = list(state_assignments)
            result.decisions = len(evidence)
            for s in state_assignments:
                result.state_histogram[s] = (
                    result.state_histogram.get(s, 0) + 1)
            metrics = score_metrics
        else:
            result.decisions = len(evidence)
            for s in state_assignments:
                result.state_histogram[s] = (
                    result.state_histogram.get(s, 0) + 1)
            metrics = hmm_metrics
        result.pnl = float(metrics["pnl"])
        result.max_drawdown = float(metrics["max_drawdown"])
        result.num_trades = int(metrics["num_trades"])
        result.sharpe_ratio = float(metrics["sharpe_ratio"])
        result.cagr = float(metrics["cagr"])
        return result

    return runner


def _resolve_now_ms(now_arg: Optional[int]) -> int:
    """Resolve the ``--now`` flag to a UTC epoch-ms integer.

    If ``now_arg`` is ``None``, returns ``datetime.now(UTC)`` in ms;
    otherwise returns the explicit value (used by tests + reproducible
    campaign replays).
    """
    if now_arg is not None:
        return int(now_arg)
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def _load_config_for_snapshot(
    config_path: Path, parser: argparse.ArgumentParser,
) -> Dict[str, Any]:
    """Load the canonical config.json. Errors degrade to ``parser.error``.

    Returns the parsed dict. The caller is responsible for hashing it
    via :func:`compute_config_snapshot_hash`.
    """
    if not config_path.exists():
        parser.error(
            f"--config-path {config_path} does not exist; cannot "
            f"compute config_snapshot_hash"
        )
    try:
        with open(config_path, encoding="utf-8") as fp:
            cfg = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(
            f"--config-path {config_path} could not be parsed as JSON: "
            f"{exc}"
        )
    if not isinstance(cfg, dict):
        parser.error(
            f"--config-path {config_path} must contain a JSON object"
        )
    return cfg


def _run_campaign_mode(args: argparse.Namespace,
                       parser: argparse.ArgumentParser) -> int:
    """Phase 16 campaign mode entry point. Returns the process exit
    code (0 on success). Always writes the config snapshot and the
    aggregate report; per-pair reports are written by
    :func:`run_campaign`.
    """
    symbols_str = args.symbols or ",".join(DEFAULT_CAMPAIGN_SYMBOLS)
    windows_str = args.windows or ",".join(DEFAULT_CAMPAIGN_WINDOWS)
    symbols = [s.strip() for s in symbols_str.split(",") if s.strip()]
    if not symbols:
        parser.error("--symbols must contain at least one non-empty token")

    now_ms = _resolve_now_ms(args.now)
    window_tokens = [w.strip() for w in windows_str.split(",") if w.strip()]
    if not window_tokens:
        parser.error("--windows must contain at least one non-empty token")
    windows: List[Tuple[int, int, str]] = []
    for w in window_tokens:
        try:
            windows.append(parse_window_arg(w, now_ms=now_ms))
        except ValueError as exc:
            parser.error(f"--windows: {exc}")

    try:
        threshold = _parse_verdict_threshold_arg(args.verdict_threshold)
    except ValueError as exc:
        parser.error(str(exc))

    k_range = [int(x) for x in args.k_range.split(",") if x.strip()]
    if not k_range or any(k < 2 for k in k_range):
        parser.error("--k-range must be a comma-separated list of "
                     "ints >= 2")

    output_dir = Path(args.output_dir) if args.output_dir else (
        _PROJ_ROOT / "reports")
    models_dir = Path(args.models_dir) if args.models_dir else (
        _PROJ_ROOT / "models")

    params = dict(DEFAULT_PARAMS)
    if args.params_json:
        with open(args.params_json) as fp:
            extra = json.load(fp)
        if not isinstance(extra, dict):
            parser.error("--params-json must contain a JSON object")
        params.update(extra)

    config_path = Path(args.config_path)
    cfg = _load_config_for_snapshot(config_path, parser)
    snapshot_hash = compute_config_snapshot_hash(cfg)

    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "config_snapshot.json"
    written_hash = write_config_snapshot(cfg, snapshot_path)
    assert written_hash == snapshot_hash, (
        "config snapshot hash mismatch — sort_keys serialization drift"
    )

    pair_runner_kwargs: Dict[str, Any] = {
        "exchange": args.exchange,
        "params": params,
        "k_range": k_range,
        "timeout_s": args.timeout_s,
    }
    if args.dry_run:
        # Inject the synthetic backtester into the per-pair runner so
        # we never touch the C++ engine or HDF5 store.
        pair_runner_kwargs["backtest_runner"] = (
            _make_dry_run_backtest_runner(args.seed))
        pair_runner_kwargs["ofe_module"] = object()

    logger.info(
        "Phase 16 campaign starting: %d symbols × %d windows = %d pairs",
        len(symbols), len(windows), len(symbols) * len(windows),
    )

    results = run_campaign(
        symbols=symbols,
        windows=windows,
        config=cfg,
        seed=args.seed,
        output_dir=output_dir,
        models_dir=models_dir,
        config_snapshot_hash=snapshot_hash,
        pair_runner_kwargs=pair_runner_kwargs,
        label="campaign",
    )

    verdict = aggregate_verdict(
        results, threshold,
        config_snapshot_hash=snapshot_hash,
        recorded_by="tools.hmm_abtest",
        recorded_at=now_iso(),
    )

    ts_tag = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S")
    summary_md = aggregate_report_path(output_dir, ts_tag)
    summary_md.write_text(
        format_campaign_summary_report(
            results, verdict, timestamp_iso=now_iso(), seed=args.seed),
        encoding="utf-8",
    )
    summary_json = summary_md.with_suffix(".json")
    summary_json.write_text(
        json.dumps(campaign_verdict_to_dict(verdict), indent=2,
                   sort_keys=True),
        encoding="utf-8",
    )

    if not getattr(args, "quiet", False):
        print()
        print(f"Campaign aggregate report: {summary_md}")
        print(f"Campaign aggregate JSON:   {summary_json}")
        print(f"Config snapshot:           {snapshot_path}")
        print(f"Config snapshot SHA-256:   {snapshot_hash}")
        print(f"Pairs written:             {len(results)}")
        print(f"Promote:                   {verdict.promote}")
        print(f"win_ratio:                 {verdict.win_ratio:.4f}")
        print(f"median_sharpe_delta:       {verdict.median_sharpe_delta:+.6f}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
    )

    # Phase 16 — campaign mode is selected by --symbols OR --windows.
    # If either is set, we ignore the legacy --symbol/--from-time/--to-time.
    if args.symbols is not None or args.windows is not None or args.dry_run:
        return _run_campaign_mode(args, parser)

    # Legacy single-pair mode (Phase 7V). --from-time and --to-time
    # become required here (they are only optional at the parser level
    # so campaign mode can omit them).
    if args.from_time is None or args.to_time is None:
        parser.error(
            "single-pair mode requires --from-time and --to-time. "
            "For campaign mode, pass --symbols and --windows instead."
        )

    from_time_ms = _parse_date_arg(args.from_time)
    to_time_ms = _parse_date_arg(args.to_time, end_of_day=True)
    if to_time_ms <= from_time_ms:
        parser.error("--to-time must be strictly after --from-time")

    k_range = [int(x) for x in args.k_range.split(",") if x.strip()]
    if not k_range or any(k < 2 for k in k_range):
        parser.error("--k-range must be a comma-separated list of "
                     "ints >= 2")

    output_dir = Path(args.output_dir) if args.output_dir else (
        _PROJ_ROOT / "reports")
    models_dir = Path(args.models_dir) if args.models_dir else (
        _PROJ_ROOT / "models")

    params = dict(DEFAULT_PARAMS)
    if args.params_json:
        with open(args.params_json) as fp:
            extra = json.load(fp)
        if not isinstance(extra, dict):
            parser.error("--params-json must contain a JSON object")
        params.update(extra)

    summary, md_path, json_path = run_abtest(
        symbol=args.symbol,
        exchange=args.exchange,
        from_time_ms=from_time_ms,
        to_time_ms=to_time_ms,
        label=args.label,
        k_range=k_range,
        seed=args.seed,
        output_dir=output_dir,
        models_dir=models_dir,
        params=params,
        timeout_s=args.timeout_s,
    )

    print()
    print(f"Report: {md_path}")
    print(f"JSON:   {json_path}")
    print(f"Verdict: {summary.winner_verdict()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
