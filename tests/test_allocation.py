from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.allocation import (
    SIZERS,
    expected_barrier_payoff,
    retained_sizer,
    rorac_ratio,
    size_position,
    training_rorac_normaliser,
)
from research.evaluate import (
    confirm_h6,
    evaluate_h6,
    h6_development_decision,
    h6_sizer_comparison,
)
from research.manifest import DatasetManifest, DatasetObject, QualityExclusions, content_digest
from research.protocol import H6_CONFIG
from research.registry import ExperimentRecord, config_digest, load_record, save_record
from research.tide import RiskCalibration, apply_entry_constraint
from research.wave import FirstPassageForecast


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


def _retained(record: ExperimentRecord, result: str) -> ExperimentRecord:
    return replace(
        record,
        status="Research validated",
        decision="retain",
        result=result,
        sealed_access=True,
        run_id="run",
        dataset_digest="a" * 64,
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
        end="2024-04-20T23:59:00+00:00",
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
            "high": values * 1.002,
            "low": values * 0.998,
            "close": values,
            "volume": np.ones(len(values)),
        },
        index=index,
    )


def _index(periods: int = 4) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=periods, freq="min", tz="UTC")


def _forecast() -> FirstPassageForecast:
    return FirstPassageForecast(
        long_target=0.6,
        long_stop=0.2,
        long_expiry=0.2,
        short_target=0.1,
        short_stop=0.7,
        short_expiry=0.2,
        observations=10,
    )


def _comparison_rows(
    *,
    rorac_sharpe: float = 2.0,
    equal_sharpe: float = 0.5,
    volatility_sharpe: float = 0.4,
    origins: int = 7,
    expected_origins: int = 7,
    breaches: int = 0,
) -> list[dict]:
    rows = []
    sharpe = {
        "rorac": rorac_sharpe,
        "equal_notional": equal_sharpe,
        "volatility_targeted": volatility_sharpe,
    }
    for horizon in (5, 15, 60):
        for origin in range(origins):
            for sizer, value in sharpe.items():
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "origin": str(origin),
                        "sizer": sizer,
                        "sharpe": value,
                        "constraint_breaches": breaches if sizer == "rorac" else 0,
                        "expected_origins": expected_origins,
                        "budget_policy": "static_budget",
                        "sealed_access": False,
                    }
                )
    return rows


class TestAllocationPrimitives(unittest.TestCase):
    def test_rorac_uses_floored_barrier_payoff_over_unit_es(self):
        self.assertAlmostEqual(expected_barrier_payoff(0.02, 0.6, 0.2, 0.001), 0.007)
        self.assertEqual(expected_barrier_payoff(0.02, 0.2, 0.2, 0.001), 0.0)
        index = _index(1)
        ratio, floors = rorac_ratio(
            pd.Series([0.007], index=index),
            pd.Series([0.05], index=index),
            denominator_floor=0.0001,
        )
        self.assertAlmostEqual(float(ratio.iloc[0]), 0.14)
        self.assertFalse(bool(floors.iloc[0]))

        floored, floor_events = rorac_ratio(
            pd.Series([0.007], index=index),
            pd.Series([1e-8], index=index),
            denominator_floor=0.0001,
        )
        self.assertAlmostEqual(float(floored.iloc[0]), 70.0)
        self.assertTrue(bool(floor_events.iloc[0]))
        sized = size_position(
            pd.Series([1.0], index=index),
            sizer="rorac",
            max_gross=1.0,
            rorac=pd.Series([3.8], index=index),
            normaliser=7.6,
            denominator_floor_events=pd.Series([False], index=index),
        )
        self.assertAlmostEqual(float(sized.position.iloc[0]), 0.5)
        capped = size_position(
            pd.Series([-1.0], index=index),
            sizer="rorac",
            max_gross=1.0,
            rorac=pd.Series([10.0], index=index),
            normaliser=7.6,
            denominator_floor_events=pd.Series([False], index=index),
        )
        self.assertAlmostEqual(float(capped.position.iloc[0]), -1.0)
        self.assertEqual(capped.counts.gross_caps, 1)

    def test_training_normaliser_is_the_linear_p90(self):
        ratios = pd.Series([1.0, 2.0, 3.0, 4.0, 10.0])
        self.assertAlmostEqual(training_rorac_normaliser(ratios, quantile=0.9), 7.6)

    def test_rorac_at_time_t_ignores_later_opportunity(self):
        index = _index()
        opportunity = pd.Series([0.01, 0.02, 0.03, 0.04], index=index)
        risk = pd.Series([0.05, 0.05, 0.05, 0.05], index=index)
        changed = opportunity.copy()
        changed.iloc[-1] = 9.0
        first, _ = rorac_ratio(opportunity, risk, denominator_floor=0.0001)
        second, _ = rorac_ratio(changed, risk, denominator_floor=0.0001)
        pd.testing.assert_series_equal(first.iloc[:-1], second.iloc[:-1])

    def test_controls_are_equal_notional_and_volatility_targeted(self):
        index = _index(3)
        signal = pd.Series([1.0, -1.0, 0.0], index=index)
        scale = pd.Series([0.4, 0.25, 0.8], index=index)
        equal = size_position(signal, sizer="equal_notional", max_gross=1.0)
        targeted = size_position(
            signal,
            sizer="volatility_targeted",
            max_gross=1.0,
            volatility_scale=scale,
        )
        pd.testing.assert_series_equal(
            equal.position,
            pd.Series([1.0, -1.0, 0.0], index=index, name="equal_notional"),
            check_freq=False,
        )
        pd.testing.assert_series_equal(
            targeted.position,
            pd.Series([0.4, -0.25, 0.0], index=index, name="volatility_targeted"),
            check_freq=False,
        )

    def test_unavailable_risk_is_zero_size_and_counted(self):
        index = _index(2)
        with self.assertLogs("research.allocation", level="WARNING") as captured:
            ratio, floors = rorac_ratio(
                pd.Series([0.007, 0.007], index=index),
                pd.Series([0.05, np.nan], index=index),
                denominator_floor=0.0001,
            )
        self.assertTrue(any("unavailable" in line for line in captured.output))
        sized = size_position(
            pd.Series([1.0, 1.0], index=index),
            sizer="rorac",
            max_gross=1.0,
            rorac=ratio,
            normaliser=1.0,
            denominator_floor_events=floors,
        )
        self.assertEqual(float(sized.position.iloc[1]), 0.0)
        self.assertEqual(sized.counts.unavailable_estimates, 1)
        self.assertEqual(sized.counts.denominator_floors, 0)

    def test_non_finite_or_duplicate_inputs_are_rejected(self):
        index = _index(1)
        with self.assertRaisesRegex(ValueError, "non-finite"):
            rorac_ratio(
                pd.Series([np.nan], index=index),
                pd.Series([0.05], index=index),
                denominator_floor=0.0001,
            )
        duplicated = pd.Series(
            [0.01, 0.02],
            index=pd.DatetimeIndex([index[0], index[0]]),
        )
        with self.assertRaisesRegex(ValueError, "duplicate"):
            rorac_ratio(
                duplicated,
                pd.Series([0.05, 0.05], index=duplicated.index),
                denominator_floor=0.0001,
            )

    def test_sizers_share_one_budget_and_cannot_increase_exposure(self):
        index = _index()
        signal = pd.Series([1.0, -1.0, 1.0, 0.0], index=index)
        scale = pd.Series([0.4, 0.5, 0.2, 0.2], index=index)
        ratios = pd.Series([1.0, 2.0, 8.0, 0.0], index=index)
        raw = {
            sizer: size_position(
                signal,
                sizer=sizer,
                max_gross=1.0,
                volatility_scale=scale,
                rorac=ratios,
                normaliser=4.0,
            ).position
            for sizer in SIZERS
        }
        self.assertFalse(np.allclose(raw["equal_notional"], raw["rorac"]))
        calibration = RiskCalibration(0.10, 0.01, 100)
        volatility_ratio = pd.Series([1.0, 1.2, 0.8, 1.0], index=index)
        budgets = {}
        for sizer, position in raw.items():
            constrained = apply_entry_constraint(
                position,
                volatility_ratio,
                calibration,
                policy="static_budget",
                budget_fraction=0.8,
                stress_trigger_ratio=1.5,
                stress_budget_multiplier=0.5,
                max_gross=1.0,
            )
            self.assertTrue((constrained.position.abs() <= position.abs() + 1e-12).all())
            budgets[sizer] = constrained.diagnostics["budget"]
        for sizer in SIZERS:
            pd.testing.assert_series_equal(budgets[sizer], budgets["volatility_targeted"])

    def test_default_sizer_stays_volatility_targeted_until_h6_is_retained(self):
        self.assertEqual(retained_sizer(None), "volatility_targeted")
        exploratory = _record(
            "EXP-H06",
            parent="EXP-H05",
            result="rorac",
            component="research.allocation",
        )
        self.assertEqual(retained_sizer(exploratory), "volatility_targeted")
        retained = _retained(exploratory, "rorac")
        self.assertEqual(retained_sizer(retained), "rorac")

    def test_direction_uses_only_the_signalled_barrier_side(self):
        index = _index(2)
        width = pd.Series([0.02, 0.02], index=index)
        from research.allocation import directional_opportunity

        opportunity = directional_opportunity(
            width,
            pd.Series([1.0, -1.0], index=index),
            _forecast(),
            round_trip_cost=0.001,
        )
        self.assertAlmostEqual(float(opportunity.iloc[0]), 0.007)
        self.assertEqual(float(opportunity.iloc[1]), 0.0)


class TestH6Decision(unittest.TestCase):
    def test_candidate_must_beat_both_controls_at_every_horizon(self):
        comparison = h6_sizer_comparison(pd.DataFrame(_comparison_rows()))
        verdict = h6_development_decision(comparison).iloc[0]
        self.assertEqual(verdict.null_decision, "reject")
        self.assertEqual(verdict.benchmark, "rorac")
        self.assertLessEqual(float(comparison["holm_control_pvalue"].max()), 0.05)

        one_control = h6_sizer_comparison(
            pd.DataFrame(_comparison_rows(volatility_sharpe=3.0))
        )
        self.assertEqual(
            h6_development_decision(one_control).iloc[0].benchmark,
            "volatility_targeted",
        )

    def test_budget_breach_or_incomplete_origins_do_not_select_rorac(self):
        breached = h6_sizer_comparison(pd.DataFrame(_comparison_rows(breaches=2)))
        self.assertEqual(h6_development_decision(breached).iloc[0].null_decision, "accept")
        incomplete = h6_sizer_comparison(
            pd.DataFrame(_comparison_rows(origins=6, expected_origins=7))
        )
        self.assertEqual(h6_development_decision(incomplete).iloc[0].null_decision, "defer")

    def test_mixed_budgets_are_rejected(self):
        rows = _comparison_rows(origins=1, expected_origins=1)
        rows[-1]["budget_policy"] = "stress_scaled_budget"
        with self.assertRaisesRegex(RuntimeError, "one ES budget"):
            h6_sizer_comparison(pd.DataFrame(rows))


class TestH6Evaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = pd.date_range("2024-01-01", "2024-04-20", freq="min", tz="UTC")
        rng = np.random.default_rng(6)
        shocks = rng.normal(0.0008, 0.003, len(cls.index))
        close = 100.0 * np.exp(np.cumsum(shocks))
        cls.bars = _bars(cls.index, close)
        cls.h4 = _record(
            "EXP-H04",
            parent="EXP-H01",
            result="first_passage_probability",
            component="research.wave",
        )
        cls.h5 = _retained(
            _record(
                "EXP-H05",
                parent="EXP-H04",
                result="static_budget",
                component="research.tide",
            ),
            "static_budget",
        )
        cls.config = dict(H6_CONFIG)
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

    def test_development_is_deterministic_and_never_reads_the_sealed_block(self):
        def _sealed(*args, **kwargs):
            raise AssertionError("development read the sealed block")

        with patch("research.evaluate.sealed_returns", side_effect=_sealed):
            first = evaluate_h6(self.bars, self.h5, self.h4, self.config)
        second = evaluate_h6(self.bars, self.h5, self.h4, self.config)
        self.assertFalse(first.deferred)
        self.assertFalse(first.scores.empty)
        pd.testing.assert_frame_equal(first.scores, second.scores)
        pd.testing.assert_frame_equal(first.diagnostics, second.diagnostics)
        self.assertFalse(first.sealed_access)
        self.assertFalse(first.scores["sealed_access"].any())
        self.assertEqual(set(first.scores["sizer"]), set(SIZERS))
        self.assertEqual(set(first.scores["budget_policy"]), {"static_budget"})
        checksums = first.scores.groupby(["horizon_minutes", "origin"])[
            ["parent_checksum", "budget_checksum"]
        ].nunique()
        self.assertTrue((checksums == 1).all().all())
        self.assertEqual(int(first.scores["constraint_breaches"].sum()), 0)

    def test_protocol_rejects_sizer_and_budget_search_before_data_access(self):
        changed_sizers = dict(self.config)
        changed_sizers["sizers"] = ["rorac"]
        with self.assertRaisesRegex(ValueError, "registered ladder"):
            evaluate_h6(pd.DataFrame(), self.h5, self.h4, changed_sizers)
        changed_budget = dict(self.config)
        changed_budget["budget_policies"] = ["static_budget"]
        with self.assertRaisesRegex(ValueError, "cannot search"):
            evaluate_h6(pd.DataFrame(), self.h5, self.h4, changed_budget)

    def test_unretained_h5_defers_before_market_data(self):
        unretained = _record(
            "EXP-H05",
            parent="EXP-H04",
            result="static_budget",
            component="research.tide",
        )
        evaluation = evaluate_h6(
            pd.DataFrame({"bad": [1]}),
            unretained,
            self.h4,
            self.config,
        )
        self.assertTrue(evaluation.deferred)
        self.assertTrue(evaluation.scores.empty)
        self.assertIn("sealed", evaluation.defer_reason)
        unconstrained = replace(self.h5, result="unconstrained_h4")
        deferred = evaluate_h6(
            pd.DataFrame({"bad": [1]}),
            unconstrained,
            self.h4,
            self.config,
        )
        self.assertTrue(deferred.deferred)
        self.assertIn("unconstrained_h4", deferred.defer_reason)


class TestH6ConfirmationGuards(unittest.TestCase):
    def _config(self) -> dict:
        config = dict(H6_CONFIG)
        config["horizons_minutes"] = [5]
        return config

    def _parents(self):
        h4 = _retained(
            _record(
                "EXP-H04",
                parent="EXP-H01",
                result="first_passage_probability",
                component="research.wave",
            ),
            "first_passage_probability",
        )
        h5 = _retained(
            _record(
                "EXP-H05",
                parent="EXP-H04",
                result="static_budget",
                component="research.tide",
            ),
            "static_budget",
        )
        return h4, h5

    def test_dirty_code_and_repeated_sealed_access_are_refused(self):
        record = _record(
            "EXP-H06",
            parent="EXP-H05",
            result="rorac",
            component="research.allocation",
        )
        h4, h5 = self._parents()
        with patch("research.evaluate.code_version", return_value="abc+dirty"):
            with self.assertRaisesRegex(RuntimeError, "confirmatory run refused"):
                confirm_h6(
                    pd.DataFrame(),
                    record,
                    h5,
                    h4,
                    dataset_manifest=_manifest(),
                    root=".",
                    record_path="unused.json",
                    config=self._config(),
                )
        sealed = replace(
            record,
            status="Rejected",
            decision="reject",
            sealed_access=True,
            run_id="run",
            dataset_digest="a" * 64,
        )
        with patch("research.evaluate.code_version", return_value="abc"):
            with self.assertRaisesRegex(RuntimeError, "already been used"):
                confirm_h6(
                    pd.DataFrame(),
                    sealed,
                    h5,
                    h4,
                    dataset_manifest=_manifest(),
                    root=".",
                    record_path="unused.json",
                    config=self._config(),
                )

    def test_confirmation_requires_a_sealed_h5_budget_and_rorac_result(self):
        config = self._config()
        manifest = _manifest()
        h4, h5 = self._parents()
        stored = replace(
            _record(
                "EXP-H06",
                parent="EXP-H05",
                result="rorac",
                component="research.allocation",
            ),
            status="Exploratory",
            dataset_digest=manifest.digest,
            config_digest=config_digest(config),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H06.json"
            save_record(path, stored)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "sealed-retained H5"):
                    confirm_h6(
                        pd.DataFrame(),
                        stored,
                        _record(
                            "EXP-H05",
                            parent="EXP-H04",
                            result="static_budget",
                            component="research.tide",
                        ),
                        h4,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=config,
                    )
                with self.assertRaisesRegex(RuntimeError, "unconstrained_h4"):
                    confirm_h6(
                        pd.DataFrame(),
                        stored,
                        replace(h5, result="unconstrained_h4"),
                        h4,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=config,
                    )
                volatility_result = replace(stored, result="volatility_targeted")
                save_record(path, volatility_result)
                with self.assertRaisesRegex(RuntimeError, "development-selected rorac"):
                    confirm_h6(
                        pd.DataFrame(),
                        volatility_result,
                        h5,
                        h4,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=config,
                    )

    def test_confirmation_refuses_mutable_objects_and_digest_mismatch(self):
        config = self._config()
        manifest = _manifest()
        h4, h5 = self._parents()
        stored = replace(
            _record(
                "EXP-H06",
                parent="EXP-H05",
                result="rorac",
                component="research.allocation",
            ),
            status="Exploratory",
            dataset_digest=manifest.digest,
            config_digest=config_digest(config),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H06.json"
            save_record(path, stored)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "immutable version IDs"):
                    confirm_h6(
                        pd.DataFrame(),
                        stored,
                        h5,
                        h4,
                        dataset_manifest=_manifest(version_id="null"),
                        root=".",
                        record_path=path,
                        config=config,
                    )
                changed = dict(config)
                changed["train_months"] = 18
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    confirm_h6(
                        pd.DataFrame(),
                        stored,
                        h5,
                        h4,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=changed,
                    )


class TestH6Registry(unittest.TestCase):
    def test_registry_record_is_proposed_and_parented_to_h5(self):
        record = load_record(Path("research/registry/EXP-H06.json"))
        self.assertEqual(record.experiment_id, "EXP-H06")
        self.assertEqual(record.parent_hypothesis, "EXP-H05")
        self.assertEqual(record.status, "Proposed")
        self.assertEqual(record.decision, "")
        self.assertEqual(record.result, "")
        self.assertEqual(record.component, "research.allocation")
        self.assertEqual(record.horizons, (5, 15, 60))
        self.assertFalse(record.sealed_access)


if __name__ == "__main__":
    unittest.main()
