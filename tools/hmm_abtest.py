"""Phase 7V — HMM vs. rule-based backtest comparison harness.

End-to-end script that:

1. Runs an ``orderflow`` backtest against a stored ``TickStore`` with the
   default rule-based ``ScoreBasedInference`` backend, capturing every
   ripple-decision evidence vector + assigned ``RippleState`` along the
   way.
2. Trains an HMM via :class:`hmm.HMMTrainer` on the captured evidence
   sequence, choosing ``K`` by BIC across ``{3, 4, 5, 6}``, and derives
   the ``state_map`` by majority vote against the rule-based labels.
3. Saves the trained model to ``models/`` as JSON.
4. Re-runs the same backtest with ``hmm_enabled=True`` +
   ``hmm_model_path=<saved file>``, capturing the same metrics.
5. Writes a Markdown + JSON A/B report to ``reports/``.

Closes the explicit validation gap from
``implementation_plan.md`` Phase 7 ("Actual HMM vs. rule-based backtest
comparison ... requires labeled V1 backtest data").

Usage::

    cd /Users/clintsellen/Documents/Trading/app/backtest
    python -m tools.hmm_abtest \\
        --symbol BTCUSDT --exchange binance \\
        --from-time 2024-01-01 --to-time 2024-12-31 \\
        --label phase7v_smoke

The harness is deterministic given the same tick store, parameters,
and HMM training seed. All defaults match the post-13Y baseline
(see ``reports/orderflow_BTCUSDT_post13Y_baseline.txt``).
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
    derive_state_map,
    format_comparison_json,
    format_comparison_report,
    now_iso,
)
from hmm.hmm_trainer import HMMTrainer  # noqa: E402
from hmm.hmm_model import HMMModel  # noqa: E402

logger = logging.getLogger(__name__)


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

    np.random.seed(seed)
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
        description="Phase 7V — HMM vs. rule-based backtest A/B harness.",
    )
    p.add_argument("--symbol", default="BTCUSDT",
                   help="symbol (default: BTCUSDT)")
    p.add_argument("--exchange", default="binance",
                   help="exchange / TickStore prefix (default: binance, "
                        "reads data/<exchange>_ticks.h5)")
    p.add_argument("--from-time", required=True,
                   help="start date (YYYY-MM-DD) or epoch ms")
    p.add_argument("--to-time", required=True,
                   help="end date (YYYY-MM-DD) or epoch ms")
    p.add_argument(
        "--label", default="phase7v",
        help="label suffix for output filenames (default: phase7v)")
    p.add_argument(
        "--k-range", default="3,4,5,6",
        help="comma-separated K candidates for HMM model selection "
             "(default: 3,4,5,6)")
    p.add_argument(
        "--seed", type=int, default=0,
        help="numpy seed for HMM training reproducibility (default: 0)")
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    log_level = logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s :: %(message)s",
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
