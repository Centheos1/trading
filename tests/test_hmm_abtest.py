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


if __name__ == "__main__":
    unittest.main()
