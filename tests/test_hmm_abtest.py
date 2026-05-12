"""Phase 7V — Offline tests for the HMM A/B validation harness.

Covers:

* ``hmm.abtest.derive_state_map`` — majority vote, edge cases, errors.
* ``hmm.abtest.compare_metrics`` + ``summarize_winner`` — direction
  semantics, num_trades-as-informational, NaN handling.
* ``hmm.abtest.format_comparison_report`` /
  ``format_comparison_json`` — schema + content invariants.
* ``tools.hmm_abtest.run_abtest`` — full orchestrator wired against a
  stub backtest runner that returns canned evidence + metrics. No C++
  engine, no HDF5 store, no Qt; exercises the train/save/re-run path
  and the on-disk report shape.
* ``tools.hmm_abtest._parse_date_arg`` — ISO-day vs. epoch-ms parsing.
* ``strategies.orderflow._build_config`` — ``hmm_enabled`` and
  ``hmm_model_path`` round-trip through the ``_RIPPLE_MAP`` (Phase 7V
  added these two keys; the existing 13Y drift-detection test pins
  the rest).
"""
from __future__ import annotations

import json
import math
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from hmm.abtest import (
    AbtestSummary,
    METRIC_HIGHER_IS_BETTER,
    METRIC_KEYS,
    compare_metrics,
    derive_state_map,
    format_comparison_json,
    format_comparison_report,
    summarize_winner,
)


# ---------------------------------------------------------------------------
# derive_state_map
# ---------------------------------------------------------------------------

class TestDeriveStateMap(unittest.TestCase):
    def test_perfect_alignment(self):
        # Every observation under hidden-state k is labelled state k+1.
        K = 3
        hmm = [0, 0, 1, 1, 2, 2]
        gt = [1, 1, 2, 2, 3, 3]
        sm = derive_state_map(hmm, gt, K)
        self.assertEqual(sm, [1, 2, 3])

    def test_majority_with_noise(self):
        # Hidden 0 mostly labelled 5, hidden 1 mostly labelled 7.
        K = 2
        hmm = [0, 0, 0, 0, 1, 1, 1, 1]
        gt = [5, 5, 5, 6, 7, 7, 7, 7]
        sm = derive_state_map(hmm, gt, K)
        self.assertEqual(sm, [5, 7])

    def test_unused_hidden_state_uses_fallback(self):
        K = 4
        hmm = [0, 0, 1, 1]
        gt = [2, 2, 3, 3]
        sm = derive_state_map(hmm, gt, K, fallback_state=9)
        self.assertEqual(sm[0], 2)
        self.assertEqual(sm[1], 3)
        self.assertEqual(sm[2], 9)
        self.assertEqual(sm[3], 9)

    def test_tie_break_lowest_state_int(self):
        # Hidden 0 sees state 4 twice, state 7 twice → tie → pick 4.
        K = 1
        hmm = [0, 0, 0, 0]
        gt = [7, 4, 7, 4]
        sm = derive_state_map(hmm, gt, K)
        self.assertEqual(sm, [4])

    def test_length_mismatch_raises(self):
        with self.assertRaises(ValueError):
            derive_state_map([0, 1], [1, 2, 3], K=2)

    def test_zero_K_raises(self):
        with self.assertRaises(ValueError):
            derive_state_map([], [], K=0)

    def test_out_of_range_hidden_state_silently_skipped(self):
        # K=2 but the assignment list contains 5 — should be skipped,
        # not crash, not corrupt counts for valid states.
        K = 2
        hmm = [0, 5, 1, 1]
        gt = [1, 1, 2, 2]
        sm = derive_state_map(hmm, gt, K, fallback_state=0)
        self.assertEqual(sm, [1, 2])


# ---------------------------------------------------------------------------
# compare_metrics + summarize_winner
# ---------------------------------------------------------------------------

class TestCompareMetrics(unittest.TestCase):
    def test_metric_keys_all_present(self):
        # Sanity-pin the metric set we report on.
        self.assertEqual(
            tuple(METRIC_KEYS),
            ("pnl", "max_drawdown", "num_trades", "sharpe_ratio", "cagr"),
        )

    def test_higher_is_better_directions(self):
        self.assertTrue(METRIC_HIGHER_IS_BETTER["pnl"])
        self.assertTrue(METRIC_HIGHER_IS_BETTER["sharpe_ratio"])
        self.assertTrue(METRIC_HIGHER_IS_BETTER["cagr"])
        self.assertFalse(METRIC_HIGHER_IS_BETTER["max_drawdown"])
        self.assertIsNone(METRIC_HIGHER_IS_BETTER["num_trades"])

    def test_per_metric_winner_correct(self):
        score = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 10,
                 "sharpe_ratio": 0.5, "cagr": 1.0}
        hmm = {"pnl": 2.0, "max_drawdown": 0.10, "num_trades": 12,
               "sharpe_ratio": 0.4, "cagr": 1.0}
        rows = compare_metrics(score, hmm)
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["pnl"].winner, "hmm")
        self.assertEqual(by_name["max_drawdown"].winner, "score")
        self.assertEqual(by_name["num_trades"].winner, "n/a")
        self.assertEqual(by_name["sharpe_ratio"].winner, "score")
        self.assertEqual(by_name["cagr"].winner, "tie")

    def test_relative_pct_when_score_is_zero_is_none(self):
        rows = compare_metrics(
            {"pnl": 0.0, "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
            {"pnl": 5.0, "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
        )
        by_name = {r.name: r for r in rows}
        self.assertIsNone(by_name["pnl"].relative_pct)

    def test_relative_pct_handles_negative_score(self):
        # If score=-2 and hmm=1, delta=+3, relative_pct should be
        # +150% (improvement of 3 vs |2|), not -150%.
        rows = compare_metrics(
            {"pnl": -2.0, "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
            {"pnl": 1.0, "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
        )
        by_name = {r.name: r for r in rows}
        self.assertAlmostEqual(by_name["pnl"].relative_pct, 150.0, places=6)

    def test_nan_marks_winner_na(self):
        rows = compare_metrics(
            {"pnl": float("nan"), "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
            {"pnl": 1.0, "max_drawdown": 0.0, "num_trades": 0,
             "sharpe_ratio": 0.0, "cagr": 0.0},
        )
        by_name = {r.name: r for r in rows}
        self.assertEqual(by_name["pnl"].winner, "n/a")

    def test_summarize_hmm_pure_win(self):
        rows = compare_metrics(
            {"pnl": 1.0, "max_drawdown": 0.10, "num_trades": 5,
             "sharpe_ratio": 0.5, "cagr": 1.0},
            {"pnl": 2.0, "max_drawdown": 0.10, "num_trades": 6,
             "sharpe_ratio": 0.5, "cagr": 1.0},
        )
        verdict = summarize_winner(rows)
        self.assertIn("HMM improves", verdict)

    def test_summarize_score_pure_win(self):
        rows = compare_metrics(
            {"pnl": 5.0, "max_drawdown": 0.05, "num_trades": 5,
             "sharpe_ratio": 1.0, "cagr": 5.0},
            {"pnl": 1.0, "max_drawdown": 0.10, "num_trades": 5,
             "sharpe_ratio": 0.5, "cagr": 2.0},
        )
        verdict = summarize_winner(rows)
        self.assertIn("Rule-based wins", verdict)

    def test_summarize_full_tie(self):
        same = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                "sharpe_ratio": 0.5, "cagr": 1.0}
        rows = compare_metrics(same, dict(same))
        self.assertIn("tied", summarize_winner(rows))

    def test_summarize_mixed(self):
        rows = compare_metrics(
            {"pnl": 5.0, "max_drawdown": 0.10, "num_trades": 5,
             "sharpe_ratio": 0.5, "cagr": 1.0},
            {"pnl": 6.0, "max_drawdown": 0.15, "num_trades": 5,
             "sharpe_ratio": 0.5, "cagr": 1.0},
        )
        self.assertIn("Mixed", summarize_winner(rows))


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _make_summary(**overrides) -> AbtestSummary:
    base = dict(
        symbol="BTCUSDT",
        from_time_ms=1_000,
        to_time_ms=2_000,
        captured_at_iso="2026-05-09T00:00:00Z",
        n_observations=42,
        K_chosen=4,
        K_candidates=[3, 4, 5, 6],
        bic=123.4567,
        log_likelihood=-50.5,
        score_metrics={"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                       "sharpe_ratio": 0.5, "cagr": 1.0},
        hmm_metrics={"pnl": 2.0, "max_drawdown": 0.05, "num_trades": 6,
                     "sharpe_ratio": 0.6, "cagr": 1.5},
        score_decisions=10,
        hmm_decisions=12,
        state_map=[1, 2, 3, 4],
        score_state_histogram={1: 4, 2: 3, 3: 3},
        hmm_state_histogram={1: 5, 2: 4, 3: 3},
        model_json_path="/tmp/hmm.json",
    )
    base.update(overrides)
    return AbtestSummary(**base)


class TestFormatComparisonReport(unittest.TestCase):
    def test_markdown_contains_required_sections(self):
        s = _make_summary()
        md = format_comparison_report(s)
        for header in (
            "# HMM vs. Rule-Based A/B — BTCUSDT",
            "## Run metadata",
            "## State map",
            "## Metric comparison",
            "## Decision counts",
            "## Verdict",
        ):
            self.assertIn(header, md, f"missing header: {header}")

    def test_markdown_includes_every_metric_row(self):
        s = _make_summary()
        md = format_comparison_report(s)
        for k in METRIC_KEYS:
            self.assertIn(f"`{k}`", md)

    def test_num_trades_renders_as_int(self):
        s = _make_summary(
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                         "num_trades": 7, "sharpe_ratio": 0.5,
                         "cagr": 1.0},
        )
        md = format_comparison_report(s)
        # No fractional num_trades values like "5.000000" should leak in.
        self.assertIn("| 5 |", md)
        self.assertIn("| 7 |", md)
        self.assertIn("+2", md)

    def test_notes_section_present_when_set(self):
        s = _make_summary()
        s.notes.append("Loosen entry thresholds.")
        md = format_comparison_report(s)
        self.assertIn("## Notes", md)
        self.assertIn("Loosen entry thresholds.", md)

    def test_notes_section_omitted_when_empty(self):
        s = _make_summary()
        md = format_comparison_report(s)
        self.assertNotIn("## Notes", md)

    def test_empty_state_histogram_rendered_gracefully(self):
        s = _make_summary(score_state_histogram={}, hmm_state_histogram={})
        md = format_comparison_report(s)
        self.assertIn("_(empty", md)


class TestFormatComparisonJson(unittest.TestCase):
    def test_json_is_serializable(self):
        s = _make_summary()
        d = format_comparison_json(s)
        # Must be json.dumps-serializable in one shot.
        encoded = json.dumps(d)
        self.assertIsInstance(encoded, str)
        # Re-parse to make sure no NaN / Infinity leaked through.
        decoded = json.loads(encoded)
        self.assertEqual(decoded["symbol"], "BTCUSDT")
        self.assertEqual(decoded["schema"], 1)
        self.assertEqual(decoded["phase"], "7V")

    def test_json_metric_rows_match_compare_metrics(self):
        s = _make_summary()
        d = format_comparison_json(s)
        rows = compare_metrics(s.score_metrics, s.hmm_metrics)
        self.assertEqual(len(d["metric_rows"]), len(rows))
        for emitted, expected in zip(d["metric_rows"], rows):
            self.assertEqual(emitted["name"], expected.name)
            self.assertEqual(emitted["winner"], expected.winner)
            self.assertAlmostEqual(emitted["delta"], expected.delta)

    def test_state_map_and_histograms_int_typed(self):
        s = _make_summary()
        d = format_comparison_json(s)
        self.assertTrue(all(isinstance(x, int) for x in d["state_map"]))
        self.assertTrue(all(
            isinstance(k, int) and isinstance(v, int)
            for k, v in d["score_state_histogram"].items()
        ))


# ---------------------------------------------------------------------------
# AbtestSummary
# ---------------------------------------------------------------------------

class TestAbtestSummaryVerdict(unittest.TestCase):
    def test_winner_verdict_proxies_summarize_winner(self):
        s = _make_summary()
        self.assertEqual(
            s.winner_verdict(),
            summarize_winner(compare_metrics(s.score_metrics,
                                             s.hmm_metrics)),
        )


# ---------------------------------------------------------------------------
# tools.hmm_abtest._parse_date_arg
# ---------------------------------------------------------------------------

class TestParseDateArg(unittest.TestCase):
    def test_iso_date_start_of_day(self):
        from tools.hmm_abtest import _parse_date_arg
        ms = _parse_date_arg("2024-01-01")
        # Round-trip via datetime to verify.
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        self.assertEqual(dt.year, 2024)
        self.assertEqual(dt.month, 1)
        self.assertEqual(dt.day, 1)
        self.assertEqual(dt.hour, 0)
        self.assertEqual(dt.minute, 0)

    def test_iso_date_end_of_day_rounds_up(self):
        from tools.hmm_abtest import _parse_date_arg
        ms = _parse_date_arg("2024-01-01", end_of_day=True)
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
        self.assertEqual(dt.hour, 23)
        self.assertEqual(dt.minute, 59)
        self.assertEqual(dt.second, 59)

    def test_epoch_ms_passthrough(self):
        from tools.hmm_abtest import _parse_date_arg
        self.assertEqual(_parse_date_arg("1700000000000"), 1700000000000)

    def test_empty_raises(self):
        from tools.hmm_abtest import _parse_date_arg
        with self.assertRaises(ValueError):
            _parse_date_arg("")


# ---------------------------------------------------------------------------
# Full harness flow with a stub runner (no C++ engine required)
# ---------------------------------------------------------------------------

class _StubDecision:
    def __init__(self, ts: int, state: int):
        self.timestamp = ts
        self.triggering_state = state
        self.intent = ""
        self.reference_side = ""
        self.reference_price = 0.0
        self.invalidation_price = 0.0
        self.confidence = 0.0
        self.wall_id = 0
        self.reason = ""


def _make_stub_runner(
    *,
    score_evidence: List[List[float]],
    score_state_assignments: List[int],
    score_metrics: Dict[str, float],
    hmm_metrics: Dict[str, float],
):
    """Build a backtest_runner double that mimics _run_single_backtest."""
    from tools.hmm_abtest import _RunResult

    call_log: List[Dict[str, Any]] = []

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
        call_log.append({
            "exchange": exchange,
            "symbol": symbol,
            "from_time_ms": from_time_ms,
            "to_time_ms": to_time_ms,
            "capture_evidence": capture_evidence,
            "params_snapshot": dict(params),
        })
        result = _RunResult()
        if capture_evidence:
            result.evidence = list(score_evidence)
            result.state_assignments = list(score_state_assignments)
            result.decisions = len(score_evidence)
            for s in score_state_assignments:
                result.state_histogram[s] = (
                    result.state_histogram.get(s, 0) + 1)
            metrics = score_metrics
        else:
            result.decisions = len(score_evidence)
            for s in score_state_assignments:
                result.state_histogram[s] = (
                    result.state_histogram.get(s, 0) + 1)
            metrics = hmm_metrics
        result.pnl = float(metrics["pnl"])
        result.max_drawdown = float(metrics["max_drawdown"])
        result.num_trades = int(metrics["num_trades"])
        result.sharpe_ratio = float(metrics["sharpe_ratio"])
        result.cagr = float(metrics["cagr"])
        return result

    return runner, call_log


class TestRunAbtestEndToEnd(unittest.TestCase):
    def setUp(self):
        # Synthetic 6-D evidence with two clearly-separated clusters →
        # HMMTrainer should converge reliably for K∈{3,4,5,6}.
        rng = np.random.default_rng(seed=42)
        n_per_cluster = 30
        cluster_a = rng.normal(loc=[0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
                               scale=0.05, size=(n_per_cluster, 6))
        cluster_b = rng.normal(loc=[0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
                               scale=0.05, size=(n_per_cluster, 6))
        self.evidence = np.vstack([cluster_a, cluster_b]).tolist()
        self.state_assignments = (
            [2] * n_per_cluster + [3] * n_per_cluster
        )

    def test_writes_md_and_json_reports(self):
        import tempfile
        from tools.hmm_abtest import run_abtest

        runner, log = _make_stub_runner(
            score_evidence=self.evidence,
            score_state_assignments=self.state_assignments,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 2.0, "max_drawdown": 0.05,
                         "num_trades": 6, "sharpe_ratio": 0.6,
                         "cagr": 1.5},
        )
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "reports"
            models_dir = Path(td) / "models"
            summary, md_path, json_path = run_abtest(
                symbol="BTCUSDT",
                exchange="binance",
                from_time_ms=1_000,
                to_time_ms=2_000,
                label="utest",
                k_range=[3, 4],
                seed=123,
                output_dir=output_dir,
                models_dir=models_dir,
                params={"tick_size": 0.01},
                timeout_s=10.0,
                ofe_module=object(),  # stub runner ignores it
                backtest_runner=runner,
            )

            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            md = md_path.read_text(encoding="utf-8")
            self.assertIn("HMM vs. Rule-Based A/B", md)
            self.assertIn("BTCUSDT", md)

            jd = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(jd["phase"], "7V")
            self.assertEqual(jd["symbol"], "BTCUSDT")

            # Model JSON was written.
            self.assertTrue(Path(summary.model_json_path).exists())

    def test_runner_called_twice_first_with_capture_then_with_hmm(self):
        import tempfile
        from tools.hmm_abtest import run_abtest

        runner, log = _make_stub_runner(
            score_evidence=self.evidence,
            score_state_assignments=self.state_assignments,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 2.0, "max_drawdown": 0.05,
                         "num_trades": 6, "sharpe_ratio": 0.6,
                         "cagr": 1.5},
        )
        with tempfile.TemporaryDirectory() as td:
            run_abtest(
                symbol="BTCUSDT",
                exchange="binance",
                from_time_ms=1_000,
                to_time_ms=2_000,
                label="utest",
                k_range=[3],
                seed=0,
                output_dir=Path(td) / "reports",
                models_dir=Path(td) / "models",
                params={"tick_size": 0.01},
                timeout_s=10.0,
                ofe_module=object(),
                backtest_runner=runner,
            )

            self.assertEqual(len(log), 2,
                             "harness must run exactly two backtests")
            self.assertTrue(log[0]["capture_evidence"])
            self.assertFalse(log[1]["capture_evidence"])
            # Run 2 must enable HMM and reference the saved model file.
            self.assertTrue(log[1]["params_snapshot"].get("hmm_enabled"))
            path = log[1]["params_snapshot"].get("hmm_model_path")
            self.assertTrue(path)
            # Assert before tempdir cleanup wipes the file.
            self.assertTrue(Path(path).exists())
            # Run 1 must NOT have HMM enabled.
            self.assertFalse(
                log[0]["params_snapshot"].get("hmm_enabled", False))

    def test_state_map_derived_from_capture_labels(self):
        """State map should map each HMM hidden state to one of the
        RippleState ints actually present in the captured labels."""
        import tempfile
        from tools.hmm_abtest import run_abtest

        runner, _ = _make_stub_runner(
            score_evidence=self.evidence,
            score_state_assignments=self.state_assignments,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                         "num_trades": 5, "sharpe_ratio": 0.5,
                         "cagr": 1.0},
        )
        with tempfile.TemporaryDirectory() as td:
            summary, _, _ = run_abtest(
                symbol="BTCUSDT", exchange="binance",
                from_time_ms=1_000, to_time_ms=2_000,
                label="utest", k_range=[3], seed=0,
                output_dir=Path(td) / "reports",
                models_dir=Path(td) / "models",
                params={"tick_size": 0.01}, timeout_s=10.0,
                ofe_module=object(), backtest_runner=runner,
            )

        labelled_states = set(self.state_assignments)
        # fallback (0) is allowed for unused hidden states; everything
        # else must come from the labelled set.
        for st in summary.state_map:
            self.assertIn(st, labelled_states | {0})
        # At least one state must be a real label (otherwise the harness
        # silently degraded all hidden states to fallback).
        self.assertTrue(
            any(st in labelled_states for st in summary.state_map),
            f"state_map={summary.state_map} contains no real labels",
        )

    def test_zero_evidence_raises(self):
        """If V1 produces no decisions, the trainer can't run — the
        harness must surface a clear error rather than silently emit a
        meaningless report."""
        import tempfile
        from tools.hmm_abtest import run_abtest

        runner, _ = _make_stub_runner(
            score_evidence=[],
            score_state_assignments=[],
            score_metrics={"pnl": 0.0, "max_drawdown": 0.0,
                           "num_trades": 0, "sharpe_ratio": 0.0,
                           "cagr": 0.0},
            hmm_metrics={"pnl": 0.0, "max_drawdown": 0.0,
                         "num_trades": 0, "sharpe_ratio": 0.0,
                         "cagr": 0.0},
        )
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError) as ctx:
                run_abtest(
                    symbol="BTCUSDT", exchange="binance",
                    from_time_ms=1_000, to_time_ms=2_000,
                    label="utest", k_range=[3], seed=0,
                    output_dir=Path(td) / "reports",
                    models_dir=Path(td) / "models",
                    params={"tick_size": 0.01}, timeout_s=10.0,
                    ofe_module=object(), backtest_runner=runner,
                )
            self.assertIn("evidence", str(ctx.exception).lower())


# ---------------------------------------------------------------------------
# strategies.orderflow._build_config — Phase 7V wires hmm_enabled / path
# ---------------------------------------------------------------------------

class TestBuildConfigHmmFields(unittest.TestCase):
    """Pin that ``hmm_enabled`` and ``hmm_model_path`` from a params
    dict round-trip onto the C++ ``RippleConfig``. The 13Y drift test
    already pins the reverse direction (every key in ``_RIPPLE_MAP``
    is a real C++ attr); this checks the Phase 7V additions."""

    @classmethod
    def setUpClass(cls):
        try:
            sys.path.insert(0, os.path.join(
                os.path.dirname(__file__), "..", "backtestingCpp",
                "orderflow", "build"))
            import orderflow_engine  # noqa: F401, PLC0415
            cls.has_module = True
        except ImportError:
            cls.has_module = False

    def setUp(self):
        if not self.has_module:
            self.skipTest("orderflow_engine C++ module not built")

    def test_hmm_enabled_in_ripple_map(self):
        from strategies.orderflow import _RIPPLE_MAP
        self.assertIn("hmm_enabled", _RIPPLE_MAP)
        self.assertIn("hmm_model_path", _RIPPLE_MAP)

    def test_hmm_enabled_round_trip(self):
        from strategies.orderflow import _build_config
        cfg = _build_config({"hmm_enabled": True,
                             "hmm_model_path": "/tmp/some.json"})
        self.assertTrue(cfg.ripple.hmm_enabled)
        self.assertEqual(cfg.ripple.hmm_model_path, "/tmp/some.json")

    def test_default_keeps_hmm_disabled(self):
        from strategies.orderflow import _build_config
        cfg = _build_config({})
        self.assertFalse(cfg.ripple.hmm_enabled)
        self.assertEqual(cfg.ripple.hmm_model_path, "")


# ===========================================================================
# Phase 16 — HMM A/B Campaign at Scale
# ===========================================================================
#
# These tests cover:
#   * AbtestSummary.populate_phase16_fields and the new derived fields.
#   * VerdictThreshold + CampaignVerdict shape.
#   * aggregate_verdict promote logic (boundary at win_ratio=0.60).
#   * run_campaign cartesian iteration, per-pair file emission,
#     metadata stamping, and seed propagation.
#   * Determinism: identical seed + identical stub data → identical
#     AbtestSummary fields across two independent run_campaign calls.
#   * Config snapshot: write_config_snapshot's hash matches
#     compute_config_snapshot_hash; both match the spec formula
#     hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).
#   * parse_window_arg shorthand + ISO + invalid forms.
#   * Default fallback (hmm_enabled=False): the campaign harness does
#     not flip the rule-based config keys.
#   * --seed CLI flag propagates through to the trainer call.
#   * The CLI dry-run smoke (no real backtester needed) produces all
#     expected report files and a valid CampaignVerdict.

import hashlib

from hmm.abtest import (
    CampaignVerdict,
    DEFAULT_CAMPAIGN_SEED,
    DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN,
    DEFAULT_VERDICT_WIN_RATIO_MIN,
    SHORTHAND_WINDOWS_DAYS,
    VerdictThreshold,
    aggregate_verdict,
    campaign_verdict_to_dict,
    compute_config_snapshot_hash,
    format_campaign_pair_report,
    format_campaign_summary_report,
    parse_window_arg,
    run_campaign,
    write_config_snapshot,
)


def _make_pair_runner_double(
    score_metrics: Dict[str, float],
    hmm_metrics: Dict[str, float],
):
    """Build a stand-in for ``tools.hmm_abtest.run_abtest`` that skips
    the trainer + C++ engine entirely. Returns a fully-populated
    ``AbtestSummary`` so ``run_campaign`` can stamp Phase 16 metadata
    on top.

    The returned tuple matches ``run_abtest``'s signature
    ``(summary, md_path, json_path)`` so the harness's per-pair file
    handling exercises the real code path.
    """
    call_log: List[Dict[str, Any]] = []

    def runner(
        *,
        symbol: str,
        from_time_ms: int,
        to_time_ms: int,
        label: str,
        seed: int,
        output_dir: Path,
        models_dir: Path,
        exchange: str,
        params: Dict[str, Any],
        k_range: List[int],
        timeout_s: float,
        ofe_module: Any = None,
        backtest_runner: Any = None,
    ) -> tuple:
        call_log.append({
            "symbol": symbol,
            "from_time_ms": from_time_ms,
            "to_time_ms": to_time_ms,
            "label": label,
            "seed": seed,
            "params": dict(params),
            "k_range": list(k_range),
        })
        s = AbtestSummary(
            symbol=symbol,
            from_time_ms=from_time_ms,
            to_time_ms=to_time_ms,
            captured_at_iso="2026-05-12T00:00:00Z",
            n_observations=10,
            K_chosen=3,
            K_candidates=list(k_range),
            bic=-100.0,
            log_likelihood=50.0,
            score_metrics=dict(score_metrics),
            hmm_metrics=dict(hmm_metrics),
            score_decisions=10,
            hmm_decisions=10,
            state_map=[1, 2, 3],
            score_state_histogram={1: 5, 2: 5},
            hmm_state_histogram={1: 6, 2: 4},
            model_json_path=str(
                models_dir / f"hmm_{symbol}_{label}.json"),
        )
        # Touch the output dir + write a stub timestamped report so we
        # can verify the harness re-emits the canonical per-pair file
        # alongside it.
        output_dir.mkdir(parents=True, exist_ok=True)
        models_dir.mkdir(parents=True, exist_ok=True)
        md_path = output_dir / f"hmm_abtest_{symbol}_{label}.md"
        md_path.write_text("(stub timestamped report)", encoding="utf-8")
        json_path = output_dir / f"hmm_abtest_{symbol}_{label}.json"
        json_path.write_text("{}", encoding="utf-8")
        return s, md_path, json_path

    return runner, call_log


# ---------------------------------------------------------------------------
# AbtestSummary Phase 16 derived fields
# ---------------------------------------------------------------------------

class TestAbtestSummaryPhase16Fields(unittest.TestCase):
    def test_populate_phase16_fields_hmm_strict_winner(self):
        s = AbtestSummary(
            symbol="BTCUSDT", from_time_ms=0, to_time_ms=1,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.10,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 2.0, "max_drawdown": 0.10,
                         "num_trades": 6, "sharpe_ratio": 0.7,
                         "cagr": 2.0},
        )
        s.populate_phase16_fields()
        # Decisive metrics: pnl, max_drawdown, sharpe, cagr (4).
        # HMM wins pnl, sharpe, cagr; ties max_drawdown.
        self.assertEqual(s.winner, "hmm")
        self.assertAlmostEqual(s.win_ratio, 0.75)  # 3/4
        self.assertAlmostEqual(s.median_sharpe_delta, 0.2)
        self.assertAlmostEqual(s.median_cagr_delta, 1.0)
        self.assertAlmostEqual(s.max_drawdown, 0.10)
        self.assertEqual(s.trade_count, 6)

    def test_populate_phase16_fields_score_winner(self):
        s = AbtestSummary(
            symbol="BTCUSDT", from_time_ms=0, to_time_ms=1,
            score_metrics={"pnl": 5.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 1.0,
                           "cagr": 5.0},
            hmm_metrics={"pnl": 1.0, "max_drawdown": 0.10,
                         "num_trades": 5, "sharpe_ratio": 0.5,
                         "cagr": 2.0},
        )
        s.populate_phase16_fields()
        self.assertEqual(s.winner, "score")
        self.assertEqual(s.win_ratio, 0.0)

    def test_populate_phase16_fields_mixed(self):
        s = AbtestSummary(
            symbol="BTCUSDT", from_time_ms=0, to_time_ms=1,
            score_metrics={"pnl": 5.0, "max_drawdown": 0.10,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 6.0, "max_drawdown": 0.15,
                         "num_trades": 5, "sharpe_ratio": 0.5,
                         "cagr": 1.0},
        )
        s.populate_phase16_fields()
        # HMM wins pnl, score wins max_drawdown, sharpe + cagr tie.
        self.assertEqual(s.winner, "mixed")

    def test_populate_phase16_fields_tie(self):
        same = {"pnl": 1.0, "max_drawdown": 0.05,
                "num_trades": 5, "sharpe_ratio": 0.5, "cagr": 1.0}
        s = AbtestSummary(
            symbol="BTCUSDT", from_time_ms=0, to_time_ms=1,
            score_metrics=dict(same), hmm_metrics=dict(same))
        s.populate_phase16_fields()
        self.assertEqual(s.winner, "tie")
        self.assertEqual(s.win_ratio, 0.0)
        self.assertEqual(s.median_sharpe_delta, 0.0)

    def test_phase16_fields_default_to_neutral(self):
        # Default-constructed AbtestSummary must be backwards-compatible
        # with Phase 7V callers that never populate the new fields.
        s = AbtestSummary(symbol="BTCUSDT", from_time_ms=0, to_time_ms=1)
        self.assertEqual(s.window_label, "")
        self.assertEqual(s.seed, 0)
        self.assertEqual(s.config_snapshot_hash, "")
        self.assertEqual(s.winner, "tie")
        self.assertEqual(s.win_ratio, 0.0)
        self.assertEqual(s.trade_count, 0)


# ---------------------------------------------------------------------------
# VerdictThreshold + aggregate_verdict promote logic
# ---------------------------------------------------------------------------

class TestVerdictThreshold(unittest.TestCase):
    def test_defaults_match_spec(self):
        # Pin the Phase 16 spec defaults: win_ratio >= 0.60 AND
        # median_sharpe_delta >= 0.10. If either default changes, the
        # implementation_plan.md §7.2 spec must change first.
        th = VerdictThreshold()
        self.assertEqual(th.win_ratio_min,
                         DEFAULT_VERDICT_WIN_RATIO_MIN)
        self.assertEqual(th.median_sharpe_delta_min,
                         DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN)
        self.assertEqual(th.win_ratio_min, 0.60)
        self.assertEqual(th.median_sharpe_delta_min, 0.10)


def _summary_with_winner(
    *,
    winner: str,
    sharpe_delta: float = 0.20,
    cagr_delta: float = 0.50,
    dd_delta: float = 0.0,
    trade_delta: int = 1,
    symbol: str = "BTCUSDT",
    window_label: str = "30d",
) -> AbtestSummary:
    """Synthesize an AbtestSummary whose populate_phase16_fields()
    will yield the requested ``winner``."""
    score = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
             "sharpe_ratio": 0.5, "cagr": 1.0}
    if winner == "hmm":
        hmm = {
            "pnl": score["pnl"] + 1.0,
            "max_drawdown": score["max_drawdown"] - 0.01,
            "num_trades": score["num_trades"] + trade_delta,
            "sharpe_ratio": score["sharpe_ratio"] + sharpe_delta,
            "cagr": score["cagr"] + cagr_delta,
        }
    elif winner == "score":
        hmm = {
            "pnl": score["pnl"] - 1.0,
            "max_drawdown": score["max_drawdown"] + 0.01,
            "num_trades": score["num_trades"] + trade_delta,
            "sharpe_ratio": score["sharpe_ratio"] - 0.10,
            "cagr": score["cagr"] - 0.10,
        }
    elif winner == "tie":
        hmm = dict(score)
    else:
        raise ValueError(winner)
    hmm["max_drawdown"] = score["max_drawdown"] + dd_delta
    s = AbtestSummary(
        symbol=symbol, from_time_ms=0, to_time_ms=1,
        score_metrics=dict(score), hmm_metrics=dict(hmm),
        window_label=window_label, seed=42,
        config_snapshot_hash="dead" * 16,
    )
    s.populate_phase16_fields()
    return s


class TestAggregateVerdict(unittest.TestCase):
    def test_empty_results_returns_promote_false(self):
        v = aggregate_verdict([], VerdictThreshold())
        self.assertIsInstance(v, CampaignVerdict)
        self.assertFalse(v.promote)
        self.assertEqual(v.win_ratio, 0.0)
        self.assertEqual(v.n_pairs, 0)
        self.assertEqual(v.symbols, [])
        self.assertEqual(v.windows, [])

    def test_promote_true_when_both_thresholds_met(self):
        results = [
            _summary_with_winner(winner="hmm", sharpe_delta=0.20),
            _summary_with_winner(winner="hmm", sharpe_delta=0.20),
            _summary_with_winner(winner="hmm", sharpe_delta=0.20),
        ]
        v = aggregate_verdict(results, VerdictThreshold())
        self.assertTrue(v.promote)
        self.assertAlmostEqual(v.win_ratio, 1.0)
        self.assertAlmostEqual(v.median_sharpe_delta, 0.20)

    def test_promote_false_when_win_ratio_too_low(self):
        # 1 of 3 HMM wins → win_ratio = 0.333 < 0.60 → promote False
        # even though the median sharpe delta clears 0.10.
        results = [
            _summary_with_winner(winner="hmm", sharpe_delta=0.20),
            _summary_with_winner(winner="score"),
            _summary_with_winner(winner="score"),
        ]
        v = aggregate_verdict(results, VerdictThreshold())
        self.assertFalse(v.promote)
        self.assertAlmostEqual(v.win_ratio, 1.0 / 3.0)

    def test_promote_false_when_sharpe_delta_too_low(self):
        # All HMM wins (win_ratio=1.0) but median sharpe delta < 0.10.
        results = [
            _summary_with_winner(winner="hmm", sharpe_delta=0.05),
            _summary_with_winner(winner="hmm", sharpe_delta=0.05),
            _summary_with_winner(winner="hmm", sharpe_delta=0.05),
        ]
        v = aggregate_verdict(results, VerdictThreshold())
        self.assertFalse(v.promote)

    def test_promote_at_exact_win_ratio_boundary(self):
        # 0.60 boundary: 3 HMM wins out of 5 → win_ratio = 0.60.
        # The spec requires `>=` so promote is True if sharpe also clears.
        results = (
            [_summary_with_winner(winner="hmm", sharpe_delta=0.20)] * 3
            + [_summary_with_winner(winner="score")] * 2
        )
        v = aggregate_verdict(results, VerdictThreshold())
        self.assertAlmostEqual(v.win_ratio, 0.60)
        self.assertTrue(v.promote)

    def test_threshold_overrides_apply(self):
        # Custom threshold: win_ratio_min = 0.99 → 0.60 is no longer enough.
        results = (
            [_summary_with_winner(winner="hmm", sharpe_delta=0.20)] * 3
            + [_summary_with_winner(winner="score")] * 2
        )
        v = aggregate_verdict(
            results,
            VerdictThreshold(win_ratio_min=0.99, median_sharpe_delta_min=0.0),
        )
        self.assertFalse(v.promote)

    def test_campaign_verdict_field_completeness(self):
        # Implementation_plan.md §7.2 "CampaignVerdict recorded here"
        # block lists the exact fields the CampaignVerdict must carry.
        # If any field disappears, this test catches the regression.
        v = aggregate_verdict(
            [_summary_with_winner(winner="hmm")],
            VerdictThreshold(),
            config_snapshot_hash="abc" * 16,
            recorded_by="unit-test",
            recorded_at="2026-05-12T00:00:00Z",
        )
        for fld in ("promote", "win_ratio", "median_sharpe_delta",
                    "median_cagr_delta", "max_drawdown_delta",
                    "trade_count_delta", "symbols", "windows",
                    "config_snapshot_hash", "recorded_by", "recorded_at"):
            self.assertTrue(hasattr(v, fld), f"missing field: {fld}")
        self.assertEqual(v.config_snapshot_hash, "abc" * 16)
        self.assertEqual(v.recorded_by, "unit-test")
        self.assertEqual(v.recorded_at, "2026-05-12T00:00:00Z")

    def test_aggregated_symbols_and_windows_dedupe_preserve_order(self):
        results = [
            _summary_with_winner(winner="hmm", symbol="BTCUSDT",
                                 window_label="30d"),
            _summary_with_winner(winner="hmm", symbol="BTCUSDT",
                                 window_label="60d"),
            _summary_with_winner(winner="score", symbol="ETHUSDT",
                                 window_label="30d"),
            _summary_with_winner(winner="hmm", symbol="ETHUSDT",
                                 window_label="60d"),
        ]
        v = aggregate_verdict(results, VerdictThreshold())
        self.assertEqual(v.symbols, ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(v.windows, ["30d", "60d"])
        self.assertEqual(v.n_pairs, 4)

    def test_campaign_verdict_to_dict_is_json_serializable(self):
        v = aggregate_verdict(
            [_summary_with_winner(winner="hmm")],
            VerdictThreshold(),
            config_snapshot_hash="hex",
        )
        d = campaign_verdict_to_dict(v)
        encoded = json.dumps(d, sort_keys=True)
        self.assertIsInstance(encoded, str)
        decoded = json.loads(encoded)
        self.assertEqual(decoded["schema"], 1)
        self.assertEqual(decoded["config_snapshot_hash"], "hex")
        self.assertIn("threshold", decoded)


# ---------------------------------------------------------------------------
# parse_window_arg
# ---------------------------------------------------------------------------

class TestParseWindowArg(unittest.TestCase):
    NOW_MS = 1_700_000_000_000  # fixed UTC anchor for deterministic tests

    def test_shorthand_30d(self):
        f, t, lbl = parse_window_arg("30d", now_ms=self.NOW_MS)
        self.assertEqual(t, self.NOW_MS)
        self.assertEqual(f, self.NOW_MS - 30 * 86_400_000)
        self.assertEqual(lbl, "30d")

    def test_shorthand_60d_and_90d(self):
        for spec, days in (("60d", 60), ("90d", 90)):
            f, t, lbl = parse_window_arg(spec, now_ms=self.NOW_MS)
            self.assertEqual(t - f, days * 86_400_000)
            self.assertEqual(lbl, spec)

    def test_iso_range(self):
        f, t, lbl = parse_window_arg(
            "2024-01-01:2024-01-31", now_ms=self.NOW_MS)
        self.assertLess(f, t)
        self.assertEqual(lbl, "2024-01-01_2024-01-31")
        # Round-trip via datetime.
        from datetime import datetime, timezone
        f_dt = datetime.fromtimestamp(f / 1000.0, tz=timezone.utc)
        t_dt = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc)
        self.assertEqual((f_dt.year, f_dt.month, f_dt.day), (2024, 1, 1))
        self.assertEqual((t_dt.year, t_dt.month, t_dt.day), (2024, 1, 31))
        self.assertEqual(t_dt.hour, 23)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_window_arg("", now_ms=self.NOW_MS)

    def test_unknown_shorthand_raises(self):
        with self.assertRaises(ValueError):
            parse_window_arg("17d", now_ms=self.NOW_MS)

    def test_inverted_range_raises(self):
        with self.assertRaises(ValueError):
            parse_window_arg(
                "2024-01-31:2024-01-01", now_ms=self.NOW_MS)

    def test_shorthand_dictionary_pinned(self):
        # If anyone changes the shorthand vocabulary, the spec ref in
        # implementation_plan.md §7.2 must update too.
        self.assertEqual(SHORTHAND_WINDOWS_DAYS,
                         {"30d": 30, "60d": 60, "90d": 90})


# ---------------------------------------------------------------------------
# config snapshot hash
# ---------------------------------------------------------------------------

class TestConfigSnapshot(unittest.TestCase):
    def test_hash_matches_spec_formula(self):
        cfg = {"b": 1, "a": [1, 2, {"z": True}]}
        expected = hashlib.sha256(
            json.dumps(cfg, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.assertEqual(compute_config_snapshot_hash(cfg), expected)

    def test_hash_invariant_to_input_key_order(self):
        cfg_a = {"a": 1, "b": 2, "nested": {"x": 1, "y": 2}}
        cfg_b = {"b": 2, "a": 1, "nested": {"y": 2, "x": 1}}
        self.assertEqual(compute_config_snapshot_hash(cfg_a),
                         compute_config_snapshot_hash(cfg_b))

    def test_write_config_snapshot_round_trip(self):
        import tempfile
        cfg = {"foo": [1, 2, 3], "bar": {"baz": True}}
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "snap.json"
            written_hash = write_config_snapshot(cfg, path)
            self.assertTrue(path.exists())
            # Re-hash the on-disk content the same way and confirm
            # bit-equality.
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(compute_config_snapshot_hash(on_disk),
                             written_hash)
            self.assertEqual(written_hash,
                             compute_config_snapshot_hash(cfg))

    def test_real_config_json_hashes(self):
        # The real ``config.json`` must hash without error and the
        # hash must be 64 hex chars (sha256).
        cfg = json.load(open("config.json", encoding="utf-8"))
        h = compute_config_snapshot_hash(cfg)
        self.assertEqual(len(h), 64)
        int(h, 16)  # must be parseable hex


# ---------------------------------------------------------------------------
# run_campaign + per-pair report writing
# ---------------------------------------------------------------------------

class TestRunCampaign(unittest.TestCase):
    def setUp(self):
        self.score = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                      "sharpe_ratio": 0.50, "cagr": 1.0}
        self.hmm = {"pnl": 2.0, "max_drawdown": 0.05, "num_trades": 6,
                    "sharpe_ratio": 0.75, "cagr": 1.5}

    def _kwargs(self, output_dir, models_dir, runner):
        return dict(
            symbols=["BTCUSDT", "ETHUSDT"],
            windows=[
                (1_000, 1_001, "w1"),
                (2_000, 2_001, "w2"),
            ],
            config={"x": 1},
            seed=42,
            output_dir=output_dir,
            models_dir=models_dir,
            config_snapshot_hash="abc" * 16,
            pair_runner=runner,
            pair_runner_kwargs={
                "exchange": "binance",
                "params": {"tick_size": 0.01},
                "k_range": [3],
                "timeout_s": 10.0,
            },
        )

    def test_returns_one_summary_per_pair_with_phase16_fields(self):
        import tempfile
        runner, log = _make_pair_runner_double(self.score, self.hmm)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            results = run_campaign(**self._kwargs(out, mod, runner))

        self.assertEqual(len(results), 4)
        # Cartesian order: BTCUSDT/w1, BTCUSDT/w2, ETHUSDT/w1, ETHUSDT/w2.
        self.assertEqual(
            [(r.symbol, r.window_label) for r in results],
            [("BTCUSDT", "w1"), ("BTCUSDT", "w2"),
             ("ETHUSDT", "w1"), ("ETHUSDT", "w2")],
        )
        for r in results:
            self.assertEqual(r.seed, 42)
            self.assertEqual(r.config_snapshot_hash, "abc" * 16)
            self.assertEqual(r.winner, "hmm")
            self.assertGreater(r.win_ratio, 0.0)
            self.assertEqual(r.trade_count, 6)
        self.assertEqual(len(log), 4)
        for entry in log:
            self.assertEqual(entry["seed"], 42)

    def test_canonical_per_pair_reports_written(self):
        import tempfile
        runner, _ = _make_pair_runner_double(self.score, self.hmm)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            run_campaign(**self._kwargs(out, mod, runner))
            for sym in ("BTCUSDT", "ETHUSDT"):
                for w in ("w1", "w2"):
                    p = out / f"hmm_campaign_{sym}_{w}_42.md"
                    self.assertTrue(p.exists(), f"missing {p}")
                    md = p.read_text(encoding="utf-8")
                    self.assertIn(f"# HMM Campaign Pair — {sym} / {w}", md)
                    self.assertIn("abc" * 16, md)  # config hash
                    self.assertIn("**Seed:** `42`", md)

    def test_seed_propagates_to_pair_runner(self):
        import tempfile
        runner, log = _make_pair_runner_double(self.score, self.hmm)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            kwargs = self._kwargs(out, mod, runner)
            kwargs["seed"] = 1234
            results = run_campaign(**kwargs)
        for r in results:
            self.assertEqual(r.seed, 1234)
        for entry in log:
            self.assertEqual(entry["seed"], 1234)

    def test_empty_symbols_or_windows_raises(self):
        with self.assertRaises(ValueError):
            run_campaign(
                symbols=[], windows=[(1, 2, "w")],
                config={}, seed=42,
                output_dir=Path("/tmp"), models_dir=Path("/tmp"),
                pair_runner=lambda **kw: (None, None, None),
            )
        with self.assertRaises(ValueError):
            run_campaign(
                symbols=["X"], windows=[],
                config={}, seed=42,
                output_dir=Path("/tmp"), models_dir=Path("/tmp"),
                pair_runner=lambda **kw: (None, None, None),
            )

    def test_run_campaign_does_not_set_hmm_enabled_in_pair_kwargs(self):
        # Phase 16 acceptance #6 + V2 default-fallback rule: the
        # campaign harness must not flip ``hmm_enabled`` in the
        # caller-supplied params dict (the per-pair runner does that
        # internally for its own second invocation of the C++ engine).
        # If the campaign harness ever leaks ``hmm_enabled=True`` into
        # the rule-based-only param dict, the V1 byte-identical
        # contract is broken.
        import tempfile
        runner, log = _make_pair_runner_double(self.score, self.hmm)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            run_campaign(**self._kwargs(out, mod, runner))
        for entry in log:
            self.assertNotIn("hmm_enabled", entry["params"])
            self.assertNotIn("hmm_model_path", entry["params"])


# ---------------------------------------------------------------------------
# Determinism — same seed + same data → identical results
# ---------------------------------------------------------------------------

class TestCampaignDeterminism(unittest.TestCase):
    def test_two_runs_same_seed_produce_identical_summaries(self):
        import tempfile
        score = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                 "sharpe_ratio": 0.50, "cagr": 1.0}
        hmm = {"pnl": 2.0, "max_drawdown": 0.05, "num_trades": 6,
               "sharpe_ratio": 0.75, "cagr": 1.5}

        def go():
            runner, _ = _make_pair_runner_double(score, hmm)
            with tempfile.TemporaryDirectory() as td:
                results = run_campaign(
                    symbols=["BTCUSDT", "ETHUSDT"],
                    windows=[(1_000, 2_000, "30d"),
                             (3_000, 4_000, "60d")],
                    config={"x": 1, "y": 2},
                    seed=DEFAULT_CAMPAIGN_SEED,
                    output_dir=Path(td) / "reports",
                    models_dir=Path(td) / "models",
                    config_snapshot_hash="dead" * 16,
                    pair_runner=runner,
                    pair_runner_kwargs={
                        "exchange": "binance",
                        "params": {"tick_size": 0.01},
                        "k_range": [3],
                        "timeout_s": 10.0,
                    },
                )
            return [
                (r.symbol, r.window_label, r.winner, r.win_ratio,
                 r.median_sharpe_delta, r.median_cagr_delta,
                 r.max_drawdown, r.trade_count, r.seed,
                 r.config_snapshot_hash)
                for r in results
            ]

        a = go()
        b = go()
        self.assertEqual(a, b)

    def test_real_pair_runner_seed_changes_trainer_call_seed(self):
        """Even though HMMTrainer is currently deterministic-by-init,
        Phase 16 spec requires the seed to be threaded through to the
        trainer call. Capture the seed argument received by the
        ``_train_hmm_from_capture`` helper."""
        from tools.hmm_abtest import run_abtest
        import tempfile

        score_evidence = [
            [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
            [0.1, 0.8, 0.1, 0.1, 0.1, 0.1],
        ]
        score_state_assignments = [2, 2, 3, 3, 3]
        runner, _ = _make_stub_runner(
            score_evidence=score_evidence,
            score_state_assignments=score_state_assignments,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 2.0, "max_drawdown": 0.05,
                         "num_trades": 6, "sharpe_ratio": 0.6,
                         "cagr": 1.5},
        )
        captured: List[int] = []
        import tools.hmm_abtest as mod
        original_train = mod._train_hmm_from_capture

        def capturing_train(evidence, sa, *, k_range, seed):
            captured.append(seed)
            return original_train(evidence, sa, k_range=k_range, seed=seed)

        with mock.patch.object(mod, "_train_hmm_from_capture",
                               capturing_train):
            with tempfile.TemporaryDirectory() as td:
                run_abtest(
                    symbol="BTCUSDT", exchange="binance",
                    from_time_ms=1, to_time_ms=2,
                    label="seed-test", k_range=[3], seed=999,
                    output_dir=Path(td) / "reports",
                    models_dir=Path(td) / "models",
                    params={"tick_size": 0.01}, timeout_s=10.0,
                    ofe_module=object(), backtest_runner=runner,
                )
        self.assertEqual(captured, [999])

    def test_no_global_numpy_seed_pollution(self):
        """Trainer-helper must not call ``np.random.seed()`` (that would
        clobber global numpy state). We assert by snapshotting global
        RNG state before and after and checking it is unchanged."""
        from tools.hmm_abtest import _train_hmm_from_capture
        evidence = [[0.5] * 6 for _ in range(8)]
        sa = [1] * 8
        before = np.random.get_state()
        _train_hmm_from_capture(evidence, sa, k_range=[3], seed=12345)
        after = np.random.get_state()
        # Global state must be byte-identical (no np.random.seed call).
        self.assertEqual(before[0], after[0])
        np.testing.assert_array_equal(before[1], after[1])
        self.assertEqual(before[2], after[2])
        self.assertEqual(before[3], after[3])
        self.assertEqual(before[4], after[4])


# ---------------------------------------------------------------------------
# Default fallback (hmm_enabled=False preserves V1)
# ---------------------------------------------------------------------------

class TestDefaultFallback(unittest.TestCase):
    def test_run_campaign_does_not_mutate_caller_params(self):
        # The caller hands in params with no HMM keys; the campaign
        # harness must not write hmm_enabled / hmm_model_path back
        # into that dict. (The per-pair run_abtest does mutate a
        # *copy* internally, but the harness's own dict is untouched.)
        import tempfile
        score = {"pnl": 1.0, "max_drawdown": 0.05, "num_trades": 5,
                 "sharpe_ratio": 0.5, "cagr": 1.0}
        hmm = {"pnl": 2.0, "max_drawdown": 0.05, "num_trades": 6,
               "sharpe_ratio": 0.7, "cagr": 1.5}
        runner, _ = _make_pair_runner_double(score, hmm)
        original_params = {"tick_size": 0.01, "feature_window_ms": 5000}
        with tempfile.TemporaryDirectory() as td:
            run_campaign(
                symbols=["BTCUSDT"],
                windows=[(1_000, 2_000, "30d")],
                config={}, seed=42,
                output_dir=Path(td) / "reports",
                models_dir=Path(td) / "models",
                pair_runner=runner,
                pair_runner_kwargs={
                    "exchange": "binance",
                    "params": original_params,
                    "k_range": [3],
                    "timeout_s": 10.0,
                },
            )
        # Caller's dict is unchanged.
        self.assertNotIn("hmm_enabled", original_params)
        self.assertNotIn("hmm_model_path", original_params)


# ---------------------------------------------------------------------------
# CLI dry-run smoke (uses _make_dry_run_backtest_runner internally)
# ---------------------------------------------------------------------------

class TestCliCampaignDryRun(unittest.TestCase):
    def test_cli_dry_run_writes_all_reports_and_snapshot(self):
        import tempfile
        from tools.hmm_abtest import main
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            rc = main([
                "--symbols", "BTCUSDT",
                "--windows", "30d",
                "--seed", "42",
                "--dry-run",
                "--output-dir", str(out),
                "--models-dir", str(mod),
                "--now", "1700000000000",
                "--quiet",
            ])
            self.assertEqual(rc, 0)
            # Per-pair canonical report.
            self.assertTrue(
                (out / "hmm_campaign_BTCUSDT_30d_42.md").exists())
            # Aggregate report (timestamped).
            agg = list(out.glob("hmm_campaign_summary_*.md"))
            self.assertEqual(len(agg), 1)
            agg_md = agg[0].read_text(encoding="utf-8")
            self.assertIn("CampaignVerdict", agg_md)
            self.assertIn("promote:", agg_md)
            # Config snapshot is alongside the reports.
            snap = out / "config_snapshot.json"
            self.assertTrue(snap.exists())
            # Config snapshot SHA-256 in the aggregate matches the
            # on-disk file's hash.
            cfg_obj = json.load(open(snap, encoding="utf-8"))
            expected_hash = compute_config_snapshot_hash(cfg_obj)
            self.assertIn(expected_hash, agg_md)
            # Aggregate JSON exists too.
            agg_json = list(out.glob("hmm_campaign_summary_*.json"))
            self.assertEqual(len(agg_json), 1)
            jd = json.loads(agg_json[0].read_text(encoding="utf-8"))
            self.assertIn("promote", jd)
            self.assertEqual(jd["config_snapshot_hash"], expected_hash)

    def test_cli_dry_run_full_acceptance_command(self):
        # Mirror the Phase 16 acceptance criterion #1 invocation:
        #   python tools/hmm_abtest.py --symbols BTCUSDT,ETHUSDT
        #     --windows 30d,60d --seed 42
        # (with --dry-run + --now to keep the test offline + deterministic).
        import tempfile
        from tools.hmm_abtest import main
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "reports"
            mod = Path(td) / "models"
            rc = main([
                "--symbols", "BTCUSDT,ETHUSDT",
                "--windows", "30d,60d",
                "--seed", "42",
                "--dry-run",
                "--output-dir", str(out),
                "--models-dir", str(mod),
                "--now", "1700000000000",
                "--quiet",
            ])
            self.assertEqual(rc, 0)
            for sym in ("BTCUSDT", "ETHUSDT"):
                for win in ("30d", "60d"):
                    p = out / f"hmm_campaign_{sym}_{win}_42.md"
                    self.assertTrue(p.exists(), f"missing {p}")

    def test_cli_invalid_verdict_threshold_errors(self):
        from tools.hmm_abtest import main
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                main([
                    "--symbols", "BTCUSDT",
                    "--windows", "30d",
                    "--verdict-threshold", "not-a-pair",
                    "--dry-run",
                    "--output-dir", str(Path(td) / "reports"),
                    "--models-dir", str(Path(td) / "models"),
                    "--now", "1700000000000",
                    "--quiet",
                ])


# ---------------------------------------------------------------------------
# Pair report formatter
# ---------------------------------------------------------------------------

class TestFormatCampaignPairReport(unittest.TestCase):
    def test_pair_report_header_includes_campaign_metadata(self):
        s = AbtestSummary(
            symbol="BTCUSDT", from_time_ms=0, to_time_ms=1,
            score_metrics={"pnl": 1.0, "max_drawdown": 0.05,
                           "num_trades": 5, "sharpe_ratio": 0.5,
                           "cagr": 1.0},
            hmm_metrics={"pnl": 2.0, "max_drawdown": 0.05,
                         "num_trades": 6, "sharpe_ratio": 0.7,
                         "cagr": 1.5},
            window_label="30d", seed=42,
            config_snapshot_hash="cafe" * 16,
            captured_at_iso="2026-05-12T00:00:00Z",
        )
        md = format_campaign_pair_report(s)
        self.assertIn("# HMM Campaign Pair — BTCUSDT / 30d", md)
        self.assertIn("**Symbol:** `BTCUSDT`", md)
        self.assertIn("**Window:** `30d`", md)
        self.assertIn("**Seed:** `42`", md)
        self.assertIn("cafe" * 16, md)
        # Embeds the standard comparison table from Phase 7V's
        # format_comparison_report() so per-pair reports are full
        # audit trails.
        self.assertIn("## Metric comparison", md)
        self.assertIn("## Phase 16 verdict (this pair)", md)


class TestFormatCampaignSummaryReport(unittest.TestCase):
    def test_summary_report_includes_verdict_block(self):
        results = [
            _summary_with_winner(winner="hmm", symbol="BTCUSDT",
                                 window_label="30d"),
            _summary_with_winner(winner="hmm", symbol="ETHUSDT",
                                 window_label="60d"),
        ]
        verdict = aggregate_verdict(
            results, VerdictThreshold(),
            config_snapshot_hash="abcd" * 16,
            recorded_by="unit-test",
            recorded_at="2026-05-12T00:00:00Z",
        )
        md = format_campaign_summary_report(
            results, verdict, timestamp_iso="2026-05-12T00:00:00Z")
        self.assertIn("# HMM A/B Campaign Summary — Phase 16", md)
        self.assertIn("CampaignVerdict", md)
        self.assertIn("promote:", md)
        self.assertIn("abcd" * 16, md)
        self.assertIn("BTCUSDT", md)
        self.assertIn("ETHUSDT", md)
        self.assertIn("Phase 17 hard gate", md)


if __name__ == "__main__":
    unittest.main()
