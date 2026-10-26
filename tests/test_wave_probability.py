from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.evaluate import (
    confirm_h4,
    evaluate_h4,
    h4_development_decision,
    h4_economic_comparison,
)
from research.manifest import DatasetManifest, DatasetObject, QualityExclusions, content_digest
from research.protocol import H4_CONFIG
from research.registry import ExperimentRecord, config_digest, save_record
from research.report import economic_summary, probability_summary, record_development_decision
from research.wave import (
    FirstPassageForecast,
    build_barrier_labels,
    first_passage_signals,
    fit_first_passage,
    score_probability,
    terminal_signals,
)
from research.distributions import GaussianModel


def _bars(index: pd.DatetimeIndex, close: np.ndarray) -> pd.DataFrame:
    values = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "open": values,
            "high": values * 1.0002,
            "low": values * 0.9998,
            "close": values,
            "volume": np.ones(len(values)),
        },
        index=index,
    )


def _record(
    experiment_id: str,
    *,
    parent: str | None,
    result: str = "",
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
        component="research.wave",
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


class TestBarrierLabels(unittest.TestCase):
    def test_same_bar_target_and_stop_uses_registered_stop_first_rule(self):
        index = pd.date_range("2024-01-01", periods=8, freq="min", tz="UTC")
        bars = _bars(index, np.asarray([100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0]))
        bars.loc[index[3], "high"] = 1_000.0
        bars.loc[index[3], "low"] = 10.0
        built = build_barrier_labels(
            bars,
            2,
            volatility_lookback_minutes=2,
            barrier_sigma=1.0,
            intrabar_rule="stop_first",
        )
        self.assertEqual(int(built.labels.loc[index[2], "long_outcome"]), -1)
        self.assertEqual(int(built.labels.loc[index[2], "short_outcome"]), -1)
        self.assertGreaterEqual(built.intrabar_ties, 1)

    def test_expiry_is_zero_and_missing_path_is_counted(self):
        index = pd.date_range("2024-01-01", periods=10, freq="min", tz="UTC")
        close = 100.0 + np.sin(np.arange(len(index))) * 0.01
        built = build_barrier_labels(
            _bars(index, close),
            2,
            volatility_lookback_minutes=2,
            barrier_sigma=100.0,
            intrabar_rule="stop_first",
        )
        self.assertTrue((built.labels[["long_outcome", "short_outcome"]] == 0).all().all())
        gapped = build_barrier_labels(
            _bars(index, close).drop(index[5]),
            2,
            volatility_lookback_minutes=2,
            barrier_sigma=100.0,
            intrabar_rule="stop_first",
        )
        self.assertGreater(gapped.gap_exclusions + gapped.volatility_exclusions, 0)


class TestWaveForecastAndRules(unittest.TestCase):
    def test_smoothed_probabilities_are_deterministic_and_sum_to_one(self):
        labels = pd.DataFrame(
            {
                "long_outcome": [1, 1, -1, 0],
                "short_outcome": [-1, 0, 0, 1],
            }
        )
        first = fit_first_passage(labels, smoothing=1.0)
        second = fit_first_passage(labels, smoothing=1.0)
        self.assertEqual(first, second)
        self.assertAlmostEqual(first.long_target + first.long_stop + first.long_expiry, 1.0)
        self.assertAlmostEqual(first.short_target + first.short_stop + first.short_expiry, 1.0)
        score = score_probability(first.long_target, np.asarray([1.0, 0.0, 1.0]))
        self.assertGreaterEqual(score.brier_score, 0.0)

    def test_probability_rule_can_choose_no_trade_while_hard_label_stays_in_market(self):
        forecast = FirstPassageForecast(
            long_target=0.40,
            long_stop=0.39,
            long_expiry=0.21,
            short_target=0.39,
            short_stop=0.40,
            short_expiry=0.21,
            observations=100,
        )
        width = pd.Series(
            [0.0001, 0.0001],
            index=pd.date_range("2024-01-01", periods=2, freq="min", tz="UTC"),
        )
        candidate, hard = first_passage_signals(
            width,
            forecast,
            cost_bps=5.0,
            round_trip_cost_multiplier=2.0,
        )
        self.assertTrue((candidate == 0.0).all())
        self.assertTrue((hard == 1.0).all())

    def test_terminal_rule_uses_the_same_cost_gate(self):
        index = pd.date_range("2024-01-01", periods=2, freq="min", tz="UTC")
        no_trade = terminal_signals(
            index,
            GaussianModel(mean=0.0005, variance=0.001),
            cost_bps=5.0,
            round_trip_cost_multiplier=2.0,
        )
        self.assertTrue((no_trade == 0.0).all())

    def test_h4_report_summaries_refuse_sealed_rows(self):
        economics = pd.DataFrame(
            [
                {
                    "horizon_minutes": 5,
                    "model": "first_passage_probability",
                    "total_simple_return": 0.01,
                    "sharpe": 1.0,
                    "max_drawdown": -0.02,
                    "average_turnover": 0.1,
                    "average_absolute_position": 0.2,
                    "no_trade_rate": 0.5,
                    "sealed_access": True,
                }
            ]
        )
        probabilities = pd.DataFrame(
            [
                {
                    "horizon_minutes": 5,
                    "model": "first_passage_probability",
                    "event": "long_target_before_stop",
                    "probability": 0.5,
                    "event_rate": 0.5,
                    "brier_score": 0.25,
                    "log_loss": 0.69,
                    "sealed_access": True,
                }
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            economic_summary(economics)
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            probability_summary(probabilities)


class TestH4Evaluation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = pd.date_range("2024-01-01", "2024-05-15", freq="min", tz="UTC")
        rng = np.random.default_rng(44)
        shocks = rng.normal(0.0, 0.0005, len(cls.index))
        close = 100.0 * np.exp(np.cumsum(shocks))
        cls.bars = _bars(cls.index, close)
        cls.bars["high"] = np.maximum(cls.bars["high"], cls.bars["close"] * 1.0004)
        cls.bars["low"] = np.minimum(cls.bars["low"], cls.bars["close"] * 0.9996)
        cls.h1 = replace(_record("EXP-H01", parent=None, result="gaussian"), horizons=(5,))
        cls.config = dict(H4_CONFIG)
        cls.config.update(
            {
                "horizons_minutes": [5],
                "sealed_months": 1,
                "train_months": 1,
                "validation_months": 1,
                "step_months": 1,
                "barrier_volatility_lookback_minutes": 10,
            }
        )

    def test_development_is_deterministic_and_never_scores_sealed_data(self):
        first = evaluate_h4(self.bars, self.h1, self.config)
        second = evaluate_h4(self.bars, self.h1, self.config)
        pd.testing.assert_frame_equal(first.economic_scores, second.economic_scores)
        pd.testing.assert_frame_equal(first.probability_scores, second.probability_scores)
        self.assertFalse(first.sealed_access)
        self.assertFalse(first.economic_scores["sealed_access"].any())
        self.assertFalse(first.probability_scores["sealed_access"].any())
        self.assertTrue(
            set(first.economic_scores["model"])
            >= {
                "always_in_market",
                "hard_label_wave",
                "first_passage_probability",
                "terminal_return_probability",
            }
        )

    def test_component_isolation_comparison_and_incomplete_evidence_gate(self):
        evaluated = evaluate_h4(self.bars, self.h1, self.config)
        comparison = h4_economic_comparison(evaluated.economic_scores)
        verdict = h4_development_decision(comparison)
        self.assertIn(verdict.iloc[0]["null_decision"], {"accept", "reject"})
        incomplete = comparison.copy()
        incomplete.loc[0, "origins"] -= 1
        self.assertEqual(
            h4_development_decision(incomplete).iloc[0]["null_decision"],
            "defer",
        )

    def test_protocol_rejects_an_unregistered_intrabar_rule_before_data_access(self):
        changed = dict(self.config)
        changed["intrabar_rule"] = "target_first"
        with self.assertRaisesRegex(ValueError, "stop_first"):
            evaluate_h4(pd.DataFrame(), self.h1, changed)


class TestH4ConfirmationGuards(unittest.TestCase):
    def test_dirty_code_is_refused_before_data_access(self):
        record = _record("EXP-H04", parent="EXP-H01")
        with patch("research.evaluate.code_version", return_value="abc+dirty"):
            with self.assertRaisesRegex(RuntimeError, "confirmatory run refused"):
                confirm_h4(
                    pd.DataFrame(),
                    record,
                    _record("EXP-H01", parent=None, result="gaussian"),
                    dataset_manifest=_manifest(),
                    root=".",
                    record_path="unused.json",
                    config={"horizons_minutes": [5]},
                )

    def test_repeated_sealed_access_is_refused(self):
        record = replace(
            _record("EXP-H04", parent="EXP-H01"),
            status="Rejected",
            decision="reject",
            sealed_access=True,
            run_id="run",
            dataset_digest="a" * 64,
        )
        with patch("research.evaluate.code_version", return_value="abc"):
            with self.assertRaisesRegex(RuntimeError, "already been used"):
                confirm_h4(
                    pd.DataFrame(),
                    record,
                    _record("EXP-H01", parent=None, result="gaussian"),
                    dataset_manifest=_manifest(),
                    root=".",
                    record_path="unused.json",
                )

    def test_unsealed_h1_and_mutable_objects_are_refused(self):
        config = dict(H4_CONFIG)
        config["horizons_minutes"] = [5]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H04.json"
            record = replace(
                _record("EXP-H04", parent="EXP-H01"),
                dataset_digest=_manifest().digest,
                config_digest=config_digest(config),
            )
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "resolved sealed H1"):
                    confirm_h4(
                        pd.DataFrame(),
                        record,
                        _record("EXP-H01", parent=None, result="gaussian"),
                        dataset_manifest=_manifest(),
                        root=".",
                        record_path=path,
                        config=config,
                    )

            sealed_h1 = replace(
                _record("EXP-H01", parent=None, result="gaussian"),
                status="Research validated",
                decision="retain",
                sealed_access=True,
                run_id="h1-run",
                dataset_digest="b" * 64,
            )
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "immutable version IDs"):
                    confirm_h4(
                        pd.DataFrame(),
                        record,
                        sealed_h1,
                        dataset_manifest=_manifest(version_id="null"),
                        root=".",
                        record_path=path,
                        config=config,
                    )

    def test_confirmation_refuses_a_changed_configuration_digest(self):
        registered = dict(H4_CONFIG)
        registered["horizons_minutes"] = [5]
        changed = dict(registered)
        changed["cost_bps"] = 6.0
        manifest = _manifest()
        sealed_h1 = replace(
            _record("EXP-H01", parent=None, result="gaussian"),
            status="Research validated",
            decision="retain",
            sealed_access=True,
            run_id="h1-run",
            dataset_digest="b" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H04.json"
            record = replace(
                _record("EXP-H04", parent="EXP-H01"),
                dataset_digest=manifest.digest,
                config_digest=config_digest(registered),
            )
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "does not match"):
                    confirm_h4(
                        pd.DataFrame(),
                        record,
                        sealed_h1,
                        dataset_manifest=manifest,
                        root=".",
                        record_path=path,
                        config=changed,
                    )

    def test_development_record_cannot_overwrite_a_sealed_decision(self):
        sealed = replace(
            _record("EXP-H04", parent="EXP-H01"),
            status="Rejected",
            decision="reject",
            sealed_access=True,
            run_id="run",
            dataset_digest="a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H04.json"
            save_record(path, sealed)
            with self.assertRaisesRegex(RuntimeError, "cannot overwrite"):
                record_development_decision(
                    path,
                    {"null_decision": "accept", "benchmark": "hard_label_wave"},
                    experiment_id="EXP-H04",
                    dataset_id="btcusdt-1m",
                    dataset_digest="a" * 64,
                    config=H4_CONFIG,
                    root=".",
                )


if __name__ == "__main__":
    unittest.main()
