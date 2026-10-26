"""Synthetic tests for H3 factor state; no external data is read."""

from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.evaluate import (
    align_to_frozen_panel,
    confirm_h3,
    evaluate_h3,
    h3_development_decision,
    h3_log_score_comparison,
)
from research.factors import (
    align_close_panel,
    fit_conditional_density,
    fit_factor_state,
    panel_minute_returns,
    score_conditional_density,
)
from research.manifest import (
    DatasetObject,
    QualityExclusions,
    build_manifest,
    build_panel_manifest,
    load_panel_manifest,
    save_panel_manifest,
)
from research.registry import ExperimentRecord, save_record


def _minutes(periods: int = 240) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=periods, freq="min", tz="UTC")


def _member(symbol: str, etag: str = "etag"):
    return build_manifest(
        dataset_id=f"panel-{symbol.lower()}",
        venue="binance",
        symbol=symbol,
        timeframe="1m",
        bucket="bucket",
        profile="profile",
        objects=(
            DatasetObject(
                f"ohlcv/binance/{symbol}/1m.parquet",
                etag,
                100,
                version_id=f"{symbol}-version",
            ),
        ),
        start="2024-01-01T00:00:00+00:00",
        end="2024-01-01T03:59:00+00:00",
        exclusions=QualityExclusions(),
    )


def _panel_manifest():
    members = (_member("BTCUSDT"), _member("ETHUSDT"))
    return build_panel_manifest(
        dataset_id="panel",
        members=members,
        start=members[0].start,
        end=members[0].end,
        alignment_exclusions=0,
    )


def _record(
    experiment_id: str, *, parent: str | None, result: str = ""
) -> ExperimentRecord:
    return ExperimentRecord(
        experiment_id=experiment_id,
        hypothesis="hypothesis",
        null="null",
        parent_hypothesis=parent,
        dataset_id="",
        dataset_digest="",
        code_version="",
        config_digest="",
        status="Proposed",
        result=result,
        decision="",
        component="component",
        notebook="notebook.ipynb",
        horizons=(5, 15, 60),
        sealed_access=False,
        run_id="",
    )


class TestPanelManifest(unittest.TestCase):
    def test_round_trip_and_member_change_alters_digest(self):
        members = (_member("BTCUSDT"), _member("ETHUSDT"))
        panel = build_panel_manifest(
            dataset_id="majors",
            members=members,
            start=members[0].start,
            end=members[0].end,
            alignment_exclusions=3,
        )
        changed = build_panel_manifest(
            dataset_id="majors",
            members=(members[0], _member("ETHUSDT", "changed")),
            start=members[0].start,
            end=members[0].end,
            alignment_exclusions=3,
        )
        self.assertNotEqual(panel.digest, changed.digest)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            save_panel_manifest(path, panel)
            self.assertEqual(load_panel_manifest(path), panel)

    def test_duplicate_symbols_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate symbols"):
            build_panel_manifest(
                dataset_id="majors",
                members=(_member("BTCUSDT"), _member("BTCUSDT", "other")),
                start="2024-01-01T00:00:00+00:00",
                end="2024-01-01T03:59:00+00:00",
                alignment_exclusions=0,
            )

    def test_corrupt_panel_digest_is_rejected(self):
        members = (_member("BTCUSDT"), _member("ETHUSDT"))
        panel = build_panel_manifest(
            dataset_id="majors",
            members=members,
            start=members[0].start,
            end=members[0].end,
            alignment_exclusions=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.json"
            save_panel_manifest(path, panel)
            payload = json.loads(path.read_text())
            payload["alignment_exclusions"] = 1
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                load_panel_manifest(path)

    def test_alignment_rejects_an_unverified_panel_object(self):
        members = (_member("BTCUSDT"), _member("ETHUSDT"))
        panel = build_panel_manifest(
            dataset_id="majors",
            members=members,
            start=members[0].start,
            end=members[0].end,
            alignment_exclusions=0,
        )
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            align_to_frozen_panel({}, replace(panel, alignment_exclusions=1), {})


class TestFactorState(unittest.TestCase):
    def test_alignment_counts_missing_minutes_without_filling(self):
        index = _minutes(80)
        closes = {
            "BTCUSDT": pd.Series(np.linspace(100.0, 101.0, len(index)), index=index),
            "ETHUSDT": pd.Series(
                np.linspace(50.0, 51.0, len(index) - 1), index=index.delete(17)
            ),
        }
        result = align_close_panel(closes, ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(result.union_rows, 80)
        self.assertEqual(result.aligned_rows, 79)
        self.assertEqual(result.dropped_timestamps, 1)
        self.assertNotIn(index[17], result.closes.index)

    def test_return_builder_drops_the_row_after_a_clock_gap(self):
        index = _minutes(80).delete(20)
        closes = pd.DataFrame(
            {
                "BTCUSDT": np.exp(np.linspace(4.0, 4.1, len(index))),
                "ETHUSDT": np.exp(np.linspace(3.0, 3.1, len(index))),
            },
            index=index,
        )
        returns, excluded = panel_minute_returns(closes)
        self.assertEqual(excluded, 2)
        self.assertNotIn(index[20], returns.index)

    def test_factor_features_are_lagged_and_pca_recovers_shared_state(self):
        index = _minutes(300)
        rng = np.random.default_rng(9)
        common = rng.normal(0.0, 0.002, len(index))
        returns = pd.DataFrame(
            {
                "BTCUSDT": common + rng.normal(0.0, 0.0001, len(index)),
                "ETHUSDT": 0.8 * common + rng.normal(0.0, 0.0001, len(index)),
                "BNBUSDT": 1.2 * common + rng.normal(0.0, 0.0001, len(index)),
            },
            index=index,
        )
        state = fit_factor_state(
            returns,
            target_symbol="BTCUSDT",
            train_start=index[0],
            train_end=index[220],
            components=1,
            lag_minutes=1,
        )
        self.assertGreater(state.explained_variance_ratio[0], 0.95)
        changed = returns.copy()
        changed.loc[index[250], "ETHUSDT"] += 0.5
        changed_state = fit_factor_state(
            changed,
            target_symbol="BTCUSDT",
            train_start=index[0],
            train_end=index[220],
            components=1,
            lag_minutes=1,
        )
        pd.testing.assert_series_equal(
            state.features.loc[index[250]],
            changed_state.features.loc[index[250]],
        )
        self.assertFalse(
            np.allclose(
                state.features.loc[index[251]].to_numpy(),
                changed_state.features.loc[index[251]].to_numpy(),
            )
        )

    def test_conditional_density_rejects_missing_scoring_features(self):
        index = _minutes(100)
        feature = pd.DataFrame({"x": np.linspace(-1.0, 1.0, len(index))}, index=index)
        labels = pd.Series(
            0.2 * feature["x"].to_numpy() + np.sin(np.arange(len(index))) * 0.01,
            index=index,
        )
        model = fit_conditional_density(
            feature.iloc[:70], labels.iloc[:70], family="gaussian", name="own_price"
        )
        score = score_conditional_density(
            model, feature.iloc[70:], labels.iloc[70:], alpha=0.05
        )
        self.assertEqual(score.model, "own_price")
        broken = feature.iloc[70:].copy()
        broken.iloc[0, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "missing"):
            score_conditional_density(model, broken, labels.iloc[70:], alpha=0.05)


class TestH3Gates(unittest.TestCase):
    def test_development_evaluation_does_not_score_the_sealed_block(self):
        index = pd.date_range("2024-01-01", periods=95 * 24 * 60, freq="min", tz="UTC")
        rng = np.random.default_rng(21)
        common = rng.normal(0.0, 0.0008, len(index))
        panel = pd.DataFrame(index=index)
        for multiplier, symbol in zip(
            (1.0, 0.8, 1.2), ("BTCUSDT", "ETHUSDT", "BNBUSDT")
        ):
            returns = multiplier * common + rng.normal(0.0, 0.0002, len(index))
            panel[symbol] = 100.0 * np.exp(np.cumsum(returns))
        h1 = replace(_record("EXP-H01", parent=None, result="gaussian"), horizons=(5,))
        config = {
            "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
            "horizons_minutes": [5],
            "sealed_months": 1,
            "train_months": 1,
            "validation_months": 1,
            "step_months": 1,
            "pca_components": 1,
            "own_price_volatility_window": 20,
        }
        first = evaluate_h3(panel, h1, config)
        changed = panel.copy()
        changed.loc[changed.index >= first.boundary, "BTCUSDT"] *= 3.0
        second = evaluate_h3(changed, h1, config)
        pd.testing.assert_frame_equal(first.scores, second.scores)
        self.assertFalse(first.sealed_access)

    def test_decision_selects_pca_only_when_every_horizon_improves(self):
        rows = []
        for horizon in (5, 15, 60):
            rows.extend(
                [
                    {
                        "horizon_minutes": horizon,
                        "model": "single_factor",
                        "mean_advantage": 0.1,
                        "median_advantage": 0.1,
                        "mean_vs_single": np.nan,
                        "median_vs_single": np.nan,
                        "origins": 2,
                        "expected_origins": 2,
                    },
                    {
                        "horizon_minutes": horizon,
                        "model": "pca_factor",
                        "mean_advantage": 0.2,
                        "median_advantage": 0.2,
                        "mean_vs_single": 0.1,
                        "median_vs_single": 0.1,
                        "origins": 2,
                        "expected_origins": 2,
                    },
                ]
            )
        verdict = h3_development_decision(pd.DataFrame(rows)).iloc[0]
        self.assertEqual(verdict.null_decision, "reject")
        self.assertEqual(verdict.benchmark, "pca_factor")
        rows[-1]["median_advantage"] = -0.1
        verdict = h3_development_decision(pd.DataFrame(rows)).iloc[0]
        self.assertEqual(verdict.benchmark, "single_factor")
        rows[0]["origins"] = 1
        verdict = h3_development_decision(pd.DataFrame(rows)).iloc[0]
        self.assertEqual(verdict.null_decision, "defer")

    def test_comparison_requires_all_three_nested_models(self):
        scores = pd.DataFrame(
            [
                {
                    "horizon_minutes": 5,
                    "origin": "one",
                    "expected_origins": 1,
                    "model": "own_price",
                    "mean_log_score": 1.0,
                },
                {
                    "horizon_minutes": 5,
                    "origin": "one",
                    "expected_origins": 1,
                    "model": "single_factor",
                    "mean_log_score": 1.1,
                },
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "pca_factor"):
            h3_log_score_comparison(scores)

    def test_confirmation_refuses_dirty_code_before_sealed_access(self):
        record = _record("EXP-H03", parent="EXP-H01")
        h1 = replace(
            _record("EXP-H01", parent=None, result="gaussian"),
            status="Research validated",
            decision="retain",
            sealed_access=True,
            dataset_digest="a" * 64,
            run_id="h1-run",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H03.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc+dirty"):
                with self.assertRaisesRegex(RuntimeError, "refused"):
                    confirm_h3(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=_panel_manifest(),
                        root=directory,
                        record_path=path,
                    )

    def test_confirmation_refuses_unresolved_h1_before_panel_access(self):
        record = _record("EXP-H03", parent="EXP-H01")
        h1 = replace(
            _record("EXP-H01", parent=None),
            dataset_digest="a" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H03.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "resolved H1 benchmark"):
                    confirm_h3(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=_panel_manifest(),
                        root=directory,
                        record_path=path,
                    )

    def test_confirmation_refuses_unregistered_configuration_before_panel_access(self):
        panel = _panel_manifest()
        record = replace(
            _record("EXP-H03", parent="EXP-H01"),
            dataset_id="panel",
            dataset_digest=panel.digest,
            config_digest="wrong",
        )
        h1 = replace(
            _record("EXP-H01", parent=None, result="gaussian"),
            status="Research validated",
            decision="retain",
            sealed_access=True,
            dataset_digest="a" * 64,
            run_id="h1-run",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H03.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "pre-registered config"):
                    confirm_h3(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=panel,
                        root=directory,
                        record_path=path,
                    )

    def test_confirmation_refuses_mutable_panel_objects(self):
        mutable_member = replace(
            _member("ETHUSDT"),
            objects=(
                DatasetObject(
                    "ohlcv/binance/ETHUSDT/1m/2026.parquet",
                    "etag",
                    100,
                    version_id="null",
                ),
            ),
            digest="",
        )
        mutable_member = build_manifest(
            dataset_id=mutable_member.dataset_id,
            venue=mutable_member.venue,
            symbol=mutable_member.symbol,
            timeframe=mutable_member.timeframe,
            bucket=mutable_member.bucket,
            profile=mutable_member.profile,
            objects=mutable_member.objects,
            start=mutable_member.start,
            end=mutable_member.end,
            exclusions=mutable_member.exclusions,
        )
        panel = build_panel_manifest(
            dataset_id="panel",
            members=(_member("BTCUSDT"), mutable_member),
            start=mutable_member.start,
            end=mutable_member.end,
            alignment_exclusions=0,
        )
        record = replace(
            _record("EXP-H03", parent="EXP-H01"),
            dataset_id=panel.dataset_id,
            dataset_digest=panel.digest,
        )
        h1 = replace(
            _record("EXP-H01", parent=None, result="gaussian"),
            status="Research validated",
            decision="retain",
            sealed_access=True,
            run_id="h1-run",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H03.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "immutable version IDs"):
                    confirm_h3(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=panel,
                        root=directory,
                        record_path=path,
                    )


if __name__ == "__main__":
    unittest.main()
