from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.evaluate import (
    confirm_h5,
    evaluate_h5,
    h5_development_decision,
    h5_policy_comparison,
)
from research.manifest import DatasetManifest, DatasetObject, QualityExclusions, content_digest
from research.protocol import H5_CONFIG
from research.registry import ExperimentRecord, config_digest, save_record
from research.tide import (
    RiskCalibration,
    apply_entry_constraint,
    causal_volatility_ratio,
    historical_expected_shortfall,
    holm_adjust,
)


def _record(
    experiment_id: str,
    *,
    parent: str | None,
    result: str,
    component: str,
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=experiment_id,
        hypothesis="hypothesis",
        null="null",
        parent_hypothesis=parent,
        dataset_id="btcusdt-1m",
        dataset_digest="",
        code_version="",
        config_digest="",
        status="Proposed",
        result=result,
        decision="",
        component=component,
        notebook="notebook.ipynb",
        horizons=(5,),
        sealed_access=False,
        run_id="",
    )


def _manifest(version_id: str = "v1") -> DatasetManifest:
    provisional = DatasetManifest(
        dataset_id="btcusdt-1m",
        venue="binance",
        symbol="BTCUSDT",
        timeframe="1m",
        bucket="bucket",
        profile="profile",
        objects=(DatasetObject("bars.parquet", "etag", 10, version_id),),
        start="2024-01-01T00:00:00+00:00",
        end="2024-04-30T23:59:00+00:00",
        schema_version="minute-bars-v1",
        exclusions=QualityExclusions(),
        digest="",
    )
    return replace(provisional, digest=content_digest(provisional))


def _bars(index: pd.DatetimeIndex, close: np.ndarray) -> pd.DataFrame:
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": values,
            "high": values * 1.0004,
            "low": values * 0.9996,
            "close": values,
            "volume": np.ones(len(values)),
        },
        index=index,
    )


class TestTideRiskPrimitives(unittest.TestCase):
    def test_expected_shortfall_uses_positive_loss_convention(self):
        returns = pd.Series([-0.10, -0.05, 0.01, 0.02])
        self.assertAlmostEqual(historical_expected_shortfall(returns, 0.75), 0.10)
        with self.assertRaisesRegex(ValueError, "confidence"):
            historical_expected_shortfall(returns, 0.50)
        with self.assertRaisesRegex(ValueError, "non-finite"):
            historical_expected_shortfall(pd.Series([-0.10, np.nan, 0.02]), 0.75)

    def test_causal_volatility_does_not_see_future_returns(self):
        index = pd.date_range("2024-01-01", periods=12, freq="min", tz="UTC")
        base = pd.Series(np.linspace(-0.002, 0.002, len(index)), index=index)
        changed = base.copy()
        changed.iloc[8:] = 1.0
        decisions = index[4:9]
        first = causal_volatility_ratio(
            base,
            decisions,
            lookback_minutes=4,
            reference_volatility=0.001,
            ratio_floor=0.1,
            ratio_cap=100.0,
        )
        second = causal_volatility_ratio(
            changed,
            decisions,
            lookback_minutes=4,
            reference_volatility=0.001,
            ratio_floor=0.1,
            ratio_cap=100.0,
        )
        pd.testing.assert_series_equal(first.iloc[:-1], second.iloc[:-1])

    def test_all_policies_only_reduce_parent_exposure_and_count_unavailable(self):
        index = pd.date_range("2024-01-01", periods=4, freq="min", tz="UTC")
        parent = pd.Series([1.0, -0.5, 0.25, 1.0], index=index)
        ratios = pd.Series([2.0, 1.0, 2.0, np.nan], index=index)
        calibration = RiskCalibration(0.10, 0.01, 100)
        constrained = {}
        for policy in H5_CONFIG["budget_policies"]:
            result = apply_entry_constraint(
                parent,
                ratios,
                calibration,
                policy=policy,
                budget_fraction=0.8,
                stress_trigger_ratio=1.5,
                stress_budget_multiplier=0.5,
                max_gross=1.0,
            )
            self.assertTrue((result.position.abs() <= parent.abs() + 1e-12).all())
            self.assertEqual(result.counts.unavailable_risk_estimates, 1)
            constrained[policy] = result.position
        self.assertLess(
            abs(constrained["stress_scaled_budget"].iloc[0]),
            abs(constrained["volatility_scaled_budget"].iloc[0]),
        )

    def test_holm_adjustment_is_monotone_and_named(self):
        adjusted = holm_adjust({"a": 0.01, "b": 0.03, "c": 0.20})
        self.assertEqual(set(adjusted), {"a", "b", "c"})
        self.assertAlmostEqual(adjusted["a"], 0.03)
        self.assertGreaterEqual(adjusted["b"], adjusted["a"])


class TestH5Decision(unittest.TestCase):
    def test_policy_family_gate_selects_largest_passing_es_reduction(self):
        rows = []
        policies = list(H5_CONFIG["budget_policies"])
        for horizon in (5, 15, 60):
            for origin in range(7):
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "origin": str(origin),
                        "policy": "unconstrained_h4",
                        "realised_es": 0.10,
                        "mean_after_cost_return": 0.01,
                        "expected_origins": 7,
                    }
                )
                for offset, policy in enumerate(policies, start=1):
                    rows.append(
                        {
                            "horizon_minutes": horizon,
                            "origin": str(origin),
                            "policy": policy,
                            "realised_es": 0.10 - offset * 0.01,
                            "mean_after_cost_return": 0.009,
                            "expected_origins": 7,
                        }
                    )
        comparison = h5_policy_comparison(pd.DataFrame(rows))
        verdict = h5_development_decision(comparison).iloc[0]
        self.assertEqual(verdict.null_decision, "reject")
        self.assertEqual(verdict.benchmark, "stress_scaled_budget")

    def test_incomplete_origin_defers(self):
        comparison = pd.DataFrame(
            [{
                "policy": "static_budget",
                "mean_es_reduction": 0.1,
                "mean_noninferiority": True,
                "holm_policy_pvalue": 0.01,
                "origins": 6,
                "expected_origins": 7,
            }]
        )
        self.assertEqual(
            h5_development_decision(comparison).iloc[0].null_decision,
            "defer",
        )


class TestH5Evaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = pd.date_range("2024-01-01", "2024-05-15", freq="min", tz="UTC")
        rng = np.random.default_rng(55)
        close = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.0005, len(cls.index))))
        cls.bars = _bars(cls.index, close)
        cls.parent = _record(
            "EXP-H04",
            parent="EXP-H01",
            result="first_passage_probability",
            component="research.wave",
        )
        cls.config = dict(H5_CONFIG)
        cls.config.update(
            {
                "horizons_minutes": [5],
                "sealed_months": 1,
                "train_months": 1,
                "validation_months": 1,
                "step_months": 1,
                "barrier_volatility_lookback_minutes": 10,
                "volatility_lookback_minutes": 30,
            }
        )

    def test_development_is_deterministic_and_never_reads_sealed_data(self):
        first = evaluate_h5(self.bars, self.parent, self.config)
        second = evaluate_h5(self.bars, self.parent, self.config)
        pd.testing.assert_frame_equal(first.risk_scores, second.risk_scores)
        pd.testing.assert_frame_equal(first.diagnostics, second.diagnostics)
        self.assertFalse(first.sealed_access)
        self.assertFalse(first.risk_scores["sealed_access"].any())
        self.assertEqual(
            set(first.risk_scores["policy"]),
            {"unconstrained_h4", *H5_CONFIG["budget_policies"]},
        )

    def test_protocol_rejects_policy_search_before_data_access(self):
        changed = dict(self.config)
        changed["budget_policies"] = ["static_budget"]
        with self.assertRaisesRegex(ValueError, "registered ladder"):
            evaluate_h5(pd.DataFrame(), self.parent, changed)


class TestH5ConfirmationGuards(unittest.TestCase):
    def test_confirmation_refuses_unsealed_h4_parent(self):
        config = dict(H5_CONFIG)
        config["horizons_minutes"] = [5]
        manifest = _manifest()
        h5 = replace(
            _record("EXP-H05", parent="EXP-H04", result="static_budget", component="research.tide"),
            dataset_digest=manifest.digest,
            config_digest=config_digest(config),
        )
        h4 = _record(
            "EXP-H04",
            parent="EXP-H01",
            result="first_passage_probability",
            component="research.wave",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H05.json"
            save_record(path, h5)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "sealed-retained H4"):
                    confirm_h5(
                        pd.DataFrame(),
                        h5,
                        h4,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=config,
                    )


if __name__ == "__main__":
    unittest.main()
