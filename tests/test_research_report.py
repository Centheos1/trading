"""Tests for the shared notebook reporting helpers; no store is read."""

from __future__ import annotations

import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from research.evaluate import h20_development_fits
from research.manifest import DatasetObject
from research.registry import ExperimentRecord, load_record, save_record
from research.report import (
    calibration_summary,
    conditional_coefficients,
    confirmation_blockers,
    plain_english_result,
    plot_advantage_by_origin,
    plot_coefficients,
    plot_density_panels,
    plot_pit_qq,
    plot_score_levels,
    plot_shortfall,
    plot_var_hit_rates,
    quality_table,
    record_development_decision,
    residual_parameters,
    score_reading,
)


def _scores(advantage: float = 0.1, *, sealed: bool = False) -> pd.DataFrame:
    rows = []
    for horizon in (5, 15):
        for position, origin in enumerate(("20220101", "20220701")):
            for model, score in (("base", 1.0), ("candidate", 1.0 + advantage * (position + 1))):
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "origin": origin,
                        "model": model,
                        "mean_log_score": score,
                        "var_exceedance_rate": 0.03,
                        "pit_ks_pvalue": 0.0,
                        "expected_shortfall": -0.01,
                        "realised_shortfall": -0.012,
                        "sealed_access": sealed,
                    }
                )
    return pd.DataFrame(rows)


def _coverage(pvalue: float = 0.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"horizon_minutes": horizon, "model": model, "bonferroni_var_pvalue": pvalue}
            for horizon in (5, 15)
            for model in ("base", "candidate")
        ]
    )


def _record(experiment_id: str = "EXP-H09", **changes) -> ExperimentRecord:
    record = ExperimentRecord(
        experiment_id=experiment_id,
        hypothesis="h",
        null="n",
        parent_hypothesis=None,
        dataset_id="",
        dataset_digest="",
        code_version="",
        config_digest="",
        status="Proposed",
        result="",
        decision="",
        component="c",
        notebook="n.ipynb",
        horizons=(5, 15),
        sealed_access=False,
        run_id="",
    )
    return replace(record, **changes)


class TestReading(unittest.TestCase):
    def test_reading_reports_advantage_wins_and_density_change(self):
        reading, per_origin = score_reading(_scores(0.1), benchmark="base", candidates=("candidate",))
        row = reading.loc[reading["horizon_minutes"] == 5].iloc[0]
        self.assertAlmostEqual(row.mean_advantage, 0.15)
        self.assertEqual(row.origins_ahead, 2)
        self.assertAlmostEqual(row.density_change_pct, 100 * math.expm1(0.15))
        self.assertAlmostEqual(per_origin.loc["20220701", ("candidate", 15)], 0.2)

    def test_reading_refuses_a_missing_model_and_sealed_rows(self):
        with self.assertRaisesRegex(RuntimeError, "absent|no rows"):
            score_reading(_scores(), benchmark="base", candidates=("other",))
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            score_reading(_scores(sealed=True), benchmark="base", candidates=("candidate",))

    def test_calibration_summary_requires_every_pvalue(self):
        summary = calibration_summary(_scores(), _coverage(), alpha=0.05)
        self.assertTrue(summary["coverage_rejected"].all())
        with self.assertRaisesRegex(RuntimeError, "VaR p-value"):
            calibration_summary(_scores(), _coverage().iloc[:2], alpha=0.05)


class TestQualityTable(unittest.TestCase):
    def test_counts_every_exclusion(self):
        from research.manifest import QualityExclusions
        from research.quality import QualityReport

        report = QualityReport(10, 8, QualityExclusions(duplicate_timestamps=1, missing_minutes=4), 3)
        table = quality_table({"BTCUSDT": report})
        self.assertEqual(table.loc["BTCUSDT", "rows_kept"], 8)
        self.assertEqual(table.loc["BTCUSDT", "missing_minutes"], 4)
        with self.assertRaisesRegex(ValueError, "at least one"):
            quality_table({})


class TestPlainEnglish(unittest.TestCase):
    def _lines(self, decision: str, advantage: float = 0.1, failures: int = 0) -> str:
        reading, _ = score_reading(_scores(advantage), benchmark="base", candidates=("candidate",))
        calibration = calibration_summary(_scores(advantage), _coverage(), alpha=0.05)
        selected = "candidate" if decision == "reject" else "base"
        return "\n".join(
            plain_english_result(
                question="Does it help?",
                verdict={"null_decision": decision, "benchmark": selected, "reason": "because"},
                reading=reading,
                calibration=calibration,
                failures=failures,
                labels={"candidate": "the candidate"},
            )
        )

    def test_reject_states_working_forecast_and_not_retained(self):
        text = self._lines("reject")
        self.assertIn("Answer: yes", text)
        self.assertIn("the candidate", text)
        self.assertIn("not retained until the sealed year", text)
        self.assertIn("overstates tail risk", text)

    def test_accept_tells_the_reader_not_to_use_the_candidate(self):
        text = self._lines("accept", advantage=-0.1)
        self.assertIn("Answer: no", text)
        self.assertIn("do not add the candidate", text)

    def test_accept_names_several_candidates_as_alternatives(self):
        scores = _scores(-0.1)
        other = scores.loc[scores["model"] == "candidate"].assign(model="other")
        scores = pd.concat([scores, other], ignore_index=True)
        reading, _ = score_reading(scores, benchmark="base", candidates=("candidate", "other"))
        coverage = _coverage()
        coverage = pd.concat(
            [coverage, coverage.loc[coverage["model"] == "candidate"].assign(model="other")],
            ignore_index=True,
        )
        calibration = calibration_summary(scores, coverage, alpha=0.05)
        text = "\n".join(
            plain_english_result(
                question="Does it help?",
                verdict={"null_decision": "accept", "benchmark": "base", "reason": "because"},
                reading=reading,
                calibration=calibration,
                failures=0,
                labels={"candidate": "the candidate", "other": "the other"},
            )
        )
        self.assertIn("do not add the candidate or the other to Wave", text)

    def test_defer_and_failures_are_stated(self):
        text = self._lines("defer", failures=2)
        self.assertIn("deferred", text)
        self.assertIn("2 walk-forward window(s) failed", text)

    def test_unknown_decision_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self._lines("maybe")


class TestRegistration(unittest.TestCase):
    def test_accept_or_reject_writes_exploratory_with_blank_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H09.json"
            save_record(path, _record())
            with patch("research.report.code_version", return_value="abc+dirty"):
                updated = record_development_decision(
                    path,
                    {"null_decision": "reject", "benchmark": "candidate"},
                    experiment_id="EXP-H09",
                    dataset_id="data",
                    dataset_digest="a" * 64,
                    config={"x": 1},
                    root=directory,
                )
            self.assertEqual(updated.status, "Exploratory")
            self.assertEqual(updated.decision, "")
            self.assertFalse(updated.sealed_access)
            self.assertEqual(load_record(path).result, "candidate")

    def test_defer_writes_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H09.json"
            save_record(path, _record())
            with patch("research.report.code_version", return_value="abc"):
                updated = record_development_decision(
                    path,
                    {"null_decision": "defer", "benchmark": "base"},
                    experiment_id="EXP-H09",
                    dataset_id="data",
                    dataset_digest="a" * 64,
                    config={},
                    root=directory,
                )
            self.assertEqual((updated.status, updated.decision), ("Deferred", "defer"))

    def test_sealed_record_and_wrong_record_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H09.json"
            save_record(
                path,
                _record(
                    status="Rejected",
                    decision="reject",
                    sealed_access=True,
                    run_id="run",
                    dataset_digest="a" * 64,
                ),
            )
            arguments = dict(
                experiment_id="EXP-H09",
                dataset_id="data",
                dataset_digest="a" * 64,
                config={},
                root=directory,
            )
            with self.assertRaisesRegex(RuntimeError, "cannot overwrite"):
                record_development_decision(path, {"null_decision": "accept", "benchmark": "base"}, **arguments)
            arguments["experiment_id"] = "EXP-H10"
            with self.assertRaisesRegex(RuntimeError, "belongs to"):
                record_development_decision(path, {"null_decision": "accept", "benchmark": "base"}, **arguments)

    def test_blockers_name_dirty_code_unresolved_h1_and_mutable_objects(self):
        h1 = _record("EXP-H01")
        objects = (DatasetObject("ohlcv/x/2026.parquet", "e", 1, version_id="null"),)
        with patch("research.report.code_version", return_value="abc+dirty"):
            blockers = confirmation_blockers(root=".", record=_record(), h1_record=h1, dataset_objects=objects)
        text = " ".join(blockers)
        self.assertIn("uncommitted", text)
        self.assertIn("H1 has not been confirmed", text)
        self.assertIn("immutable version id", text)
        self.assertIn("configuration digest", text)
        with patch("research.report.code_version", return_value="abc"):
            clear = confirmation_blockers(root=".", record=_record(config_digest="d"))
        self.assertEqual(clear, [])


class TestPlots(unittest.TestCase):
    def tearDown(self):
        plt.close("all")

    def test_standard_plots_render(self):
        scores = _scores()
        qq = pd.DataFrame(
            [
                {"horizon_minutes": h, "origin": o, "model": m, "theoretical": q, "empirical": q, "sealed_access": False}
                for h in (5, 15)
                for o in ("20220101", "20220701")
                for m in ("base", "candidate")
                for q in (0.1, 0.5, 0.9)
            ]
        )
        plot_advantage_by_origin(scores, benchmark="base", candidates=("candidate",))
        plot_score_levels(scores, models=("base", "candidate"))
        plot_var_hit_rates(scores, models=("base", "candidate"), alpha=0.05)
        plot_shortfall(scores, models=("base", "candidate"))
        plot_pit_qq(qq, models=("base", "candidate"))
        sample = np.random.default_rng(1).normal(size=500)
        fig = plot_density_panels(
            {5: sample, 15: sample},
            {5: [("normal", lambda grid: -0.5 * grid**2 - 0.9189, {"color": "red"})]},
            title="t",
        )
        self.assertIn("fit failed", [text.get_text() for text in fig.axes[1].texts])


class TestDevelopmentFits(unittest.TestCase):
    def test_h20_fits_ignore_the_sealed_block_and_report_coefficients(self):
        index = pd.date_range("2024-01-01", periods=95 * 24 * 60, freq="min", tz="UTC")
        rng = np.random.default_rng(4)
        shock = rng.normal(0.0, 0.0008, len(index))
        panel = pd.DataFrame(
            {
                "BTCUSDT": 100.0 * np.exp(np.cumsum(shock)),
                "ETHUSDT": 50.0 * np.exp(np.cumsum(0.8 * shock + rng.normal(0.0, 0.0002, len(index)))),
                "BNBUSDT": 80.0 * np.exp(np.cumsum(1.1 * shock + rng.normal(0.0, 0.0002, len(index)))),
                "SPX": 400.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0003, len(index)))),
                "EUR": 1.1 * np.exp(np.cumsum(rng.normal(0.0, 0.0001, len(index)))),
                "JPY": 150.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0001, len(index)))),
            },
            index=index,
        )
        config = {
            "symbols": list(panel.columns),
            "crypto_symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
            "venues": {s: "binance" if s.endswith("USDT") else "oanda" for s in panel.columns},
            "external_features": [["spx_return", "SPX"]],
            "usd_factor_legs": [["EUR", -1], ["JPY", 1]],
            "horizons_minutes": [5],
            "sealed_months": 1,
            "own_price_volatility_window": 20,
        }
        h1 = _record("EXP-H01", result="gaussian", horizons=(5,))
        first = h20_development_fits(panel, h1, config)
        changed = panel.copy()
        changed.iloc[-10 * 24 * 60 :, 0] *= 3.0
        second = h20_development_fits(changed, h1, config)
        self.assertEqual({fit.model for fit in first}, {"own_price", "crypto_factor", "external_factor", "combined"})
        for a, b in zip(first, second):
            np.testing.assert_allclose(a.density.coefficients, b.density.coefficients)
        coefficients = conditional_coefficients(first)
        self.assertIn("usd_factor", set(coefficients["feature"]))
        self.assertEqual(set(residual_parameters(first)["family"]), {"gaussian"})
        plot_coefficients(coefficients, model="combined")
        plt.close("all")


if __name__ == "__main__":
    unittest.main()
