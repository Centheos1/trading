"""Tests for the BTCUSDT research core. These do not read the research bucket."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.baselines import (
    apply_costs,
    buy_and_hold_position,
    consecutive_simple_returns,
    momentum_position,
    performance,
    volatility_scale,
)
from research.clock import as_utc_index
from research.evaluate import (
    align_to_frozen_dataset,
    evaluate_baselines,
    confirm_h1,
    evaluate_h1,
    evaluate_h2,
    freeze_dataset,
    h1_development_decision,
    h1_var_coverage_family,
    h2_log_score_comparison,
    require_frozen_dataset,
)
from research.protocol import BASELINE_CONFIG, H1_CONFIG, H2_CONFIG
from research.distributions import (
    GaussianModel,
    StudentTModel,
    bonferroni,
    fit_gaussian,
    fit_student_t,
    score_distribution,
    uniform_qq,
)
from research.load import load_research_bars, normalize_bars
from research.manifest import (
    DatasetObject,
    QualityExclusions,
    build_manifest,
    list_ohlcv_objects,
    load_manifest,
    save_manifest,
)
from research.quality import audit_minute_bars
from research.regimes import (
    GaussianHMM,
    clock_gap_resets,
    filtered_probabilities,
    filtered_probability_frame,
    fit_gaussian_hmm,
    fit_regime_densities,
    fit_weighted_gaussian,
    h1_benchmark,
    minute_returns_known_at_close,
    score_regime_forecast,
)
from research.registry import (
    code_version,
    config_digest,
    load_record,
    make_run_id,
    require_committed,
    save_record,
)
from research.returns import horizon_log_returns
from research.splits import (
    development_returns,
    rolling_origins,
    sealed_boundary,
    sealed_returns,
    select_available,
)


def _minutes(start: str, periods: int, freq: str = "min") -> pd.DatetimeIndex:
    return pd.date_range(start, periods=periods, freq=freq, tz="UTC")


def _bars(index: pd.DatetimeIndex, close: np.ndarray | None = None) -> pd.DataFrame:
    values = np.linspace(100.0, 101.0, len(index)) if close is None else np.asarray(close, dtype=float)
    return pd.DataFrame(
        {"open": values, "high": values + 0.1, "low": values - 0.1, "close": values, "volume": 1.0},
        index=index,
    )


class TestClockAndQuality(unittest.TestCase):
    def test_naive_and_local_clocks_are_rejected(self):
        naive = pd.date_range("2024-01-01", periods=2, freq="min")
        with self.assertRaises(ValueError):
            as_utc_index(naive)
        local = pd.date_range("2024-01-01", periods=2, freq="min", tz="Australia/Sydney")
        with self.assertRaises(ValueError):
            as_utc_index(local)

    def test_bad_rows_are_counted_and_gaps_are_not_filled(self):
        index = _minutes("2024-01-01", 5)
        bars = _bars(index[[0, 2, 3, 4]])
        bars.loc[index[2], "high"] = 10.0
        bars.loc[index[3], "close"] = -1.0
        duplicate = _bars(index[[0]])
        duplicate.loc[index[0], ["open", "high", "low", "close"]] = [149.0, 151.0, 148.0, 150.0]
        off_grid = _bars(index[[4]])
        off_grid.index = index[[4]] + pd.Timedelta(seconds=30)
        kept, report = audit_minute_bars(pd.concat([bars, duplicate, off_grid]))
        self.assertEqual(report.exclusions.duplicate_timestamps, 1)
        self.assertEqual(report.exclusions.off_grid_timestamps, 1)
        self.assertEqual(report.exclusions.inconsistent_ohlc, 1)
        self.assertEqual(report.exclusions.nonpositive_prices, 1)
        self.assertEqual(report.exclusions.missing_minutes, 3)
        self.assertEqual(list(kept.index), [index[0], index[4]])
        self.assertAlmostEqual(float(kept.loc[index[0], "close"]), 150.0)

    def test_an_empty_audit_raises(self):
        index = _minutes("2024-01-01", 2)
        bars = _bars(index, close=np.array([-1.0, -2.0]))
        with self.assertRaises(RuntimeError):
            audit_minute_bars(bars)


class TestLabelsAndSplits(unittest.TestCase):
    def test_a_missing_minute_does_not_create_a_longer_return(self):
        index = _minutes("2024-01-01", 6)
        close = pd.Series(np.arange(100, 106), index=index, dtype=float).drop(index[2])
        built = horizon_log_returns(close, 1)
        self.assertGreaterEqual(built.gap_exclusions, 1)
        self.assertNotIn(index[1], built.returns.index)
        self.assertIn(index[3], built.returns.index)
        self.assertAlmostEqual(float(built.returns.loc[index[3]]), np.log(104 / 103))

    def test_labels_that_cross_a_cutoff_leave_the_training_block(self):
        index = _minutes("2024-01-01", 10)
        close = pd.Series(np.linspace(100, 110, len(index)), index=index)
        built = horizon_log_returns(close, 5)
        train = select_available(built.returns, 5, index[0], index[6])
        self.assertTrue(((train.index + pd.Timedelta(minutes=5)) < index[6]).all())
        self.assertNotIn(index[1], train.index)

    def test_development_cannot_read_the_sealed_block_unless_confirmed(self):
        index = pd.date_range("2023-01-01", "2023-04-15", freq="min", tz="UTC")
        close = pd.Series(np.linspace(100, 200, len(index)), index=index)
        built = horizon_log_returns(close, 5)
        boundary = sealed_boundary(index, months=1)
        development, held = development_returns(built.returns, 5, boundary)
        self.assertGreater(held, 0)
        self.assertTrue(((development.index + pd.Timedelta(minutes=5)) < boundary).all())
        with self.assertRaises(RuntimeError):
            sealed_returns(built.returns, 5, boundary, confirmatory=False)
        sealed = sealed_returns(built.returns, 5, boundary, confirmatory=True)
        self.assertTrue(((sealed.index + pd.Timedelta(minutes=5)) >= boundary).all())
        self.assertTrue(set(development.index).isdisjoint(sealed.index))

    def test_rolling_origins_stop_at_the_sealed_boundary(self):
        index = pd.date_range("2023-01-01", "2023-08-01", freq="min", tz="UTC")
        boundary = sealed_boundary(index, months=1)
        origins = rolling_origins(
            index, boundary, train_months=2, validation_months=1, step_months=1
        )
        self.assertGreaterEqual(len(origins), 2)
        self.assertTrue(all(origin.validation_end <= boundary for origin in origins))
        with self.assertRaises(ValueError):
            sealed_boundary(_minutes("2024-01-01", 10), months=12)


class TestManifestAndRegistry(unittest.TestCase):
    def test_digest_changes_when_the_stored_object_changes(self):
        first = build_manifest(
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
            objects=(DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "abc", 10),),
            start="2024-01-01T00:00:00+00:00",
            end="2024-02-01T00:00:00+00:00",
            exclusions=QualityExclusions(missing_minutes=2),
        )
        second = build_manifest(
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
            objects=(DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "changed", 10),),
            start="2024-01-01T00:00:00+00:00",
            end="2024-02-01T00:00:00+00:00",
            exclusions=QualityExclusions(missing_minutes=2),
        )
        self.assertNotEqual(first.digest, second.digest)


class TestResearchLoad(unittest.TestCase):
    def test_epoch_milliseconds_and_naive_clocks_become_utc(self):
        epoch = normalize_bars(pd.DataFrame({"timestamp": [0, 60_000], "close": [1.0, 2.0]}))
        self.assertEqual(str(epoch.index.tz), "UTC")
        self.assertEqual(epoch.index[1], pd.Timestamp("1970-01-01 00:01:00+00:00"))
        naive = normalize_bars(
            pd.DataFrame({"close": [3.0]}, index=pd.to_datetime(["2024-01-01 00:00:00"]))
        )
        self.assertEqual(naive.index[0], pd.Timestamp("2024-01-01 00:00:00+00:00"))

    def test_loader_reads_the_archive_before_the_yearly_shard(self):
        archive = pd.DataFrame({"timestamp": [0, 60_000], "close": [1.0, 2.0]})
        yearly = pd.DataFrame({"timestamp": [120_000], "close": [3.0]})
        objects = (
            DatasetObject("ohlcv/binance/BTCUSDT/1m.parquet", "a", 1),
            DatasetObject("ohlcv/binance/BTCUSDT/1m/2026.parquet", "b", 1),
        )
        with patch("research.load.list_ohlcv_objects", return_value=objects), patch(
            "research.load.pd.read_parquet", side_effect=[archive, yearly]
        ) as read:
            bars, found = load_research_bars(venue="binance", symbol="BTCUSDT", timeframe="1m")
        self.assertEqual(found, objects)
        self.assertEqual(list(bars["close"]), [1.0, 2.0, 3.0])
        self.assertEqual(
            read.call_args_list[0].args[0],
            "s3://trading-data-centheos/ohlcv/binance/BTCUSDT/1m.parquet",
        )
        self.assertEqual(read.call_args_list[0].kwargs["storage_options"], {"profile": "trading"})

    def test_a_failed_object_read_raises(self):
        objects = (DatasetObject("ohlcv/binance/BTCUSDT/1m.parquet", "a", 1),)
        with patch("research.load.list_ohlcv_objects", return_value=objects), patch(
            "research.load.pd.read_parquet", side_effect=OSError("denied")
        ):
            with self.assertRaises(RuntimeError):
                load_research_bars(venue="binance", symbol="BTCUSDT", timeframe="1m")

    def test_listing_includes_the_archive_and_skips_a_missing_one(self):
        class Missing(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "404"}}

        class Client:
            def list_objects_v2(self, **kwargs):
                return {
                    "Contents": [{"Key": "ohlcv/binance/BTCUSDT/1m/2026.parquet"}],
                    "IsTruncated": False,
                }

            def head_object(self, Bucket, Key):
                if Key.endswith("1m.parquet"):
                    raise Missing()
                return {"ETag": '"year"', "ContentLength": 8}

        yearly_only = list_ohlcv_objects(
            venue="binance", symbol="BTCUSDT", timeframe="1m", client=Client()
        )
        self.assertEqual([item.key for item in yearly_only], ["ohlcv/binance/BTCUSDT/1m/2026.parquet"])

        class Both(Client):
            def head_object(self, Bucket, Key):
                return {"ETag": f'"{Key[-8:]}"', "ContentLength": 8}

        both = list_ohlcv_objects(
            venue="binance", symbol="BTCUSDT", timeframe="1m", client=Both()
        )
        self.assertEqual(
            [item.key for item in both],
            [
                "ohlcv/binance/BTCUSDT/1m.parquet",
                "ohlcv/binance/BTCUSDT/1m/2026.parquet",
            ],
        )

    def test_listing_fails_when_no_object_exists(self):
        class Missing(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "NoSuchKey"}}

        class Client:
            def list_objects_v2(self, **kwargs):
                return {"Contents": [], "IsTruncated": False}

            def head_object(self, Bucket, Key):
                raise Missing()

        with self.assertRaises(RuntimeError):
            list_ohlcv_objects(venue="binance", symbol="BTCUSDT", timeframe="1m", client=Client())

    def test_an_identification_error_is_not_treated_as_absence(self):
        class Denied(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "AccessDenied"}}

        class Client:
            def list_objects_v2(self, **kwargs):
                return {"Contents": [], "IsTruncated": False}

            def head_object(self, Bucket, Key):
                raise Denied()

        with self.assertRaises(RuntimeError):
            list_ohlcv_objects(venue="binance", symbol="BTCUSDT", timeframe="1m", client=Client())

    def test_a_tampered_manifest_cannot_load(self):
        manifest = build_manifest(
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
            objects=(DatasetObject("ohlcv/a.parquet", "abc", 10, "v1"),),
            start="2024-01-01T00:00:00+00:00",
            end="2024-02-01T00:00:00+00:00",
            exclusions=QualityExclusions(),
        )
        path = self._tmp("manifest.json")
        save_manifest(path, manifest)
        payload = json.loads(path.read_text())
        payload["start"] = "2023-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload))
        with self.assertRaises(ValueError):
            load_manifest(path)

    def test_retention_requires_a_named_sealed_run(self):
        record = load_record("research/registry/EXP-H01.json")
        self.assertIn(record.status, {"Proposed", "Exploratory"})
        self.assertNotEqual(record.decision, "retain")
        self.assertFalse(record.sealed_access)
        if record.result:
            self.assertIn(record.result, {"gaussian", "student_t"})
        retained = replace(
            record,
            status="Research validated",
            decision="retain",
            dataset_digest="a" * 64,
            sealed_access=False,
        )
        with self.assertRaises(ValueError):
            save_record(self._tmp("record.json"), retained)
        version = code_version(".")
        self.assertTrue(version)
        with self.assertRaises(RuntimeError):
            require_committed(version + "+dirty")
        digest = config_digest({"b": 1, "a": [5, 15, 60]})
        self.assertEqual(digest, config_digest({"a": [5, 15, 60], "b": 1}))
        self.assertEqual(len(make_run_id("EXP-H01", "d" * 64, "abc", digest)), 16)

    def _tmp(self, name: str):
        import tempfile
        from pathlib import Path

        root = Path(tempfile.mkdtemp(prefix="research-"))
        self.addCleanup(lambda: None)
        return root / name


class TestBaselines(unittest.TestCase):
    def test_momentum_and_volatility_do_not_read_the_next_close(self):
        index = _minutes("2024-01-01", 40)
        close = pd.Series(100 + np.sin(np.linspace(0, 3, len(index))), index=index)
        position = momentum_position(close, 1)
        changed = close.copy()
        changed.iloc[-1] = 1_000.0
        self.assertTrue(np.array_equal(position.iloc[:-1].to_numpy(), momentum_position(changed, 1).iloc[:-1].to_numpy()))
        scale = volatility_scale(close, 5, 0.5)
        changed_scale = volatility_scale(changed, 5, 0.5)
        self.assertTrue(np.allclose(scale.iloc[:-1].fillna(0), changed_scale.iloc[:-1].fillna(0)))

    def test_gaps_are_excluded_and_entry_has_a_cost(self):
        index = _minutes("2024-01-01", 5)
        close = pd.Series([100, 101, 102, 103, 104], index=index, dtype=float).drop(index[2])
        returns, excluded = consecutive_simple_returns(close)
        self.assertGreaterEqual(excluded, 1)
        self.assertNotIn(index[1], returns.index)
        position = buy_and_hold_position(returns.index)
        net = apply_costs(position, returns, cost_bps=10)
        gross = float(returns.iloc[0])
        self.assertAlmostEqual(float(net.iloc[0]), gross - 0.001)
        summary = performance(net, position)
        self.assertEqual(summary.observations, len(net))
        self.assertGreater(summary.average_turnover, 0)


class TestDistributions(unittest.TestCase):
    def test_gaussian_shortfall_matches_a_numerical_integral(self):
        from scipy.integrate import quad
        from scipy.stats import norm

        model = GaussianModel(0.0, 1.0)
        alpha = 0.05
        level = model.value_at_risk(alpha)
        moment, _ = quad(lambda value: value * norm.pdf(value), -np.inf, level, limit=200)
        self.assertAlmostEqual(model.expected_shortfall(alpha), moment / alpha, places=6)

    def test_student_t_shortfall_matches_a_numerical_integral(self):
        from scipy.integrate import quad
        from scipy.stats import t

        model = StudentTModel(df=6.0, loc=0.2, scale=1.5)
        alpha = 0.05
        level = model.value_at_risk(alpha)
        moment, _ = quad(lambda value: value * t.pdf(value, model.df, loc=model.loc, scale=model.scale), -80, level)
        self.assertAlmostEqual(model.expected_shortfall(alpha), moment / alpha, places=4)

    def test_heavy_tails_favour_the_student_model_and_flat_data_fails(self):
        rng = np.random.default_rng(7)
        train = rng.standard_t(5, size=20_000)
        holdout = rng.standard_t(5, size=8_000)
        student = score_distribution(fit_student_t(train), holdout)
        gaussian = score_distribution(fit_gaussian(train), holdout)
        self.assertGreater(student.mean_log_score, gaussian.mean_log_score)
        self.assertGreater(fit_student_t(train).df, 4.0)
        with self.assertRaises(ValueError):
            fit_gaussian(np.zeros(40))
        with self.assertRaises(RuntimeError):
            fit_student_t(np.linspace(-1, 1, 80))
        self.assertEqual(bonferroni([0.02, 0.03]), [0.04, 0.06])
        with self.assertRaises(ValueError):
            bonferroni([])

    def test_var_coverage_family_reports_the_bonferroni_adjustment(self):
        scores = pd.DataFrame(
            {
                "horizon_minutes": [5, 5, 5, 5, 15, 15],
                "model": ["gaussian", "gaussian", "student_t", "student_t", "gaussian", "student_t"],
                "var_pvalue": [0.02, 0.04, 0.01, 0.03, 0.20, 0.50],
            }
        )
        family = h1_var_coverage_family(scores)
        self.assertEqual(family["horizon_minutes"].tolist(), [5, 5, 15, 15])
        self.assertEqual(family["model"].tolist(), ["gaussian", "student_t", "gaussian", "student_t"])
        self.assertEqual(family["median_var_pvalue"].tolist(), [0.03, 0.02, 0.20, 0.50])
        self.assertEqual(family["bonferroni_var_pvalue"].tolist(), [0.12, 0.08, 0.80, 1.0])
        with self.assertRaises(RuntimeError):
            h1_var_coverage_family(scores[scores["model"] != "student_t"])
        partial = pd.concat(
            [
                scores,
                pd.DataFrame({"horizon_minutes": [60], "model": ["gaussian"], "var_pvalue": [0.1]}),
            ],
            ignore_index=True,
        )
        with self.assertRaises(RuntimeError):
            h1_var_coverage_family(partial)
        with self.assertRaises(ValueError):
            h1_var_coverage_family(scores.drop(columns=["var_pvalue"]))
        with self.assertRaises(ValueError):
            h1_var_coverage_family(scores.iloc[0:0])
        broken = scores.copy()
        broken.loc[0, "var_pvalue"] = float("nan")
        with self.assertRaises(ValueError):
            h1_var_coverage_family(broken)

    def test_development_decision_rejects_the_null_only_when_every_horizon_improves(self):
        better = pd.DataFrame(
            {
                "horizon_minutes": [5, 15, 60],
                "mean_advantage": [0.18, 0.17, 0.16],
                "median_advantage": [0.17, 0.17, 0.16],
                "origins": [7, 7, 7],
            }
        )
        rejected = h1_development_decision(better).iloc[0]
        self.assertEqual(rejected.null_decision, "reject")
        self.assertEqual(rejected.benchmark, "student_t")
        mixed = better.copy()
        mixed.loc[2, "median_advantage"] = -0.01
        accepted = h1_development_decision(mixed).iloc[0]
        self.assertEqual(accepted.null_decision, "accept")
        self.assertEqual(accepted.benchmark, "gaussian")
        with self.assertRaises(ValueError):
            h1_development_decision(better.iloc[0:0])

    def test_confirmation_scores_the_sealed_block_once(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        index = pd.date_range("2024-01-01", "2024-03-15", freq="min", tz="UTC")
        rng = np.random.default_rng(4)
        shocks = rng.standard_t(5, size=len(index)) * 0.001
        close = pd.Series(100 * np.exp(np.cumsum(shocks)), index=index)
        config = dict(H1_CONFIG)
        config.update({"sealed_months": 1, "horizons_minutes": [5]})
        digest = "a" * 64

        def opened():
            return replace(
                load_record("research/registry/EXP-H01.json"),
                horizons=(5,),
                decision="",
                result="",
                status="Proposed",
                dataset_id="",
                dataset_digest="",
                code_version="",
                config_digest="",
                sealed_access=False,
                run_id="",
            )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EXP-H01.json"
            record = opened()
            save_record(path, record)
            with patch("research.evaluate.sealed_returns", side_effect=AssertionError("sealed read")):
                with patch("research.evaluate.code_version", return_value="abc123+dirty"):
                    with self.assertRaises(RuntimeError):
                        confirm_h1(
                            close,
                            record,
                            dataset_id="btcusdt-1m",
                            dataset_digest=digest,
                            root=".",
                            record_path=path,
                            config=config,
                        )
            self.assertFalse(load_record(path).sealed_access)
            with patch("research.evaluate.code_version", return_value="abc123"):
                confirmed = confirm_h1(
                    close,
                    record,
                    dataset_id="btcusdt-1m",
                    dataset_digest=digest,
                    root=".",
                    record_path=path,
                    config=config,
                )
            loaded = load_record(path)
            self.assertTrue(confirmed.record.sealed_access)
            self.assertEqual(loaded, confirmed.record)
            self.assertEqual(loaded.decision, "retain")
            self.assertEqual(loaded.result, "student_t")
            self.assertEqual(loaded.status, "Research validated")
            self.assertEqual(loaded.dataset_digest, digest)
            self.assertEqual(len(loaded.run_id), 16)
            self.assertTrue((confirmed.scores["sealed_access"] == True).all())
            self.assertEqual(set(confirmed.scores["origin"]), {"sealed"})
            sealed_score = confirmed.scores.set_index("model")["mean_log_score"]
            self.assertGreater(float(sealed_score["student_t"]), float(sealed_score["gaussian"]))
            built = horizon_log_returns(close, 5)
            development, _ = development_returns(built.returns, 5, confirmed.boundary)
            sealed = sealed_returns(built.returns, 5, confirmed.boundary, confirmatory=True)
            self.assertTrue(set(development.index).isdisjoint(sealed.index))
            self.assertEqual(int(confirmed.scores["observations"].iloc[0]), len(sealed))
            with patch("research.evaluate.code_version", return_value="abc123"):
                with self.assertRaises(RuntimeError):
                    confirm_h1(
                        close,
                        loaded,
                        dataset_id="btcusdt-1m",
                        dataset_digest=digest,
                        root=".",
                        record_path=path,
                        config=config,
                    )
            self.assertEqual(load_record(path), loaded)

            other = Path(tmp) / "again.json"
            save_record(other, opened())
            shifted = close.copy()
            shifted.loc[shifted.index >= confirmed.boundary] *= 1.2
            with patch("research.evaluate.code_version", return_value="abc123"):
                moved = confirm_h1(
                    shifted,
                    opened(),
                    dataset_id="btcusdt-1m",
                    dataset_digest=digest,
                    root=".",
                    record_path=other,
                    config=config,
                )
            self.assertNotAlmostEqual(
                float(confirmed.scores["mean_log_score"].iloc[0]),
                float(moved.scores["mean_log_score"].iloc[0]),
            )
            rejected_path = Path(tmp) / "bad-digest.json"
            save_record(rejected_path, opened())
            with patch("research.evaluate.sealed_returns", side_effect=AssertionError("sealed read")):
                with patch("research.evaluate.code_version", return_value="abc123"):
                    with self.assertRaises(ValueError):
                        confirm_h1(
                            close,
                            opened(),
                            dataset_id="btcusdt-1m",
                            dataset_digest="not-a-digest",
                            root=".",
                            record_path=rejected_path,
                            config=config,
                        )
            self.assertFalse(load_record(rejected_path).sealed_access)
            mismatched = replace(opened(), dataset_digest="b" * 64, dataset_id="btcusdt-1m", status="Exploratory")
            mismatch_path = Path(tmp) / "mismatch.json"
            save_record(mismatch_path, mismatched)
            with patch("research.evaluate.sealed_returns", side_effect=AssertionError("sealed read")):
                with patch("research.evaluate.code_version", return_value="abc123"):
                    with self.assertRaises(RuntimeError):
                        confirm_h1(
                            close,
                            mismatched,
                            dataset_id="btcusdt-1m",
                            dataset_digest=digest,
                            root=".",
                            record_path=mismatch_path,
                            config=config,
                        )
            self.assertFalse(load_record(mismatch_path).sealed_access)

    def test_a_calibrated_pit_stays_near_the_diagonal(self):
        rng = np.random.default_rng(11)
        sample = rng.normal(loc=0.1, scale=1.4, size=8_000)
        theoretical, empirical = uniform_qq(fit_gaussian(sample).cdf(sample))
        self.assertEqual(len(theoretical), len(empirical))
        self.assertLess(float(np.max(np.abs(theoretical - empirical))), 0.03)
        shifted = rng.normal(loc=0.0, scale=2.5, size=8_000)
        wrong_theoretical, wrong_empirical = uniform_qq(GaussianModel(0.0, 1.0).cdf(shifted))
        self.assertGreater(
            float(np.max(np.abs(wrong_theoretical - wrong_empirical))),
            0.05,
        )
        with self.assertRaises(ValueError):
            uniform_qq(np.linspace(-0.1, 0.5, 40))

    def test_development_evaluation_does_not_touch_the_sealed_test(self):
        index = pd.date_range("2023-01-01", "2023-05-15", freq="min", tz="UTC")
        rng = np.random.default_rng(4)
        shocks = rng.standard_t(5, size=len(index)) * 0.001
        close = pd.Series(100 * np.exp(np.cumsum(shocks)), index=index)
        h1_config = dict(H1_CONFIG)
        h1_config.update(
            {
                "sealed_months": 1,
                "train_months": 1,
                "validation_months": 1,
                "step_months": 1,
                "horizons_minutes": [5],
            }
        )
        evaluated = evaluate_h1(close, h1_config)
        self.assertFalse(evaluated.sealed_access)
        self.assertTrue((evaluated.scores["sealed_access"] == False).all())
        self.assertIn("student_t", set(evaluated.scores["model"]))
        self.assertTrue((evaluated.pit_qq["sealed_access"] == False).all())
        self.assertEqual(
            set(evaluated.pit_qq["model"]),
            set(evaluated.scores["model"]),
        )
        self.assertTrue(evaluated.pit_qq["theoretical"].between(0, 1).all())
        self.assertTrue(evaluated.pit_qq["empirical"].between(0, 1).all())
        baseline_config = dict(BASELINE_CONFIG)
        baseline_config.update({"sealed_months": 1, "lookback_minutes": 30})
        baselines = evaluate_baselines(close, baseline_config)
        self.assertEqual(set(baselines["baseline"]), {
            "buy_and_hold", "momentum", "mean_reversion", "volatility_targeted_long"
        })
        self.assertTrue((baselines["sealed_access"] == False).all())
        frozen = build_manifest(
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
            objects=(DatasetObject("ohlcv/a.parquet", "abc", 10),),
            start="2024-01-01T00:00:00+00:00",
            end="2024-02-01T00:00:00+00:00",
            exclusions=QualityExclusions(),
        )
        require_frozen_dataset(frozen, frozen)
        changed = build_manifest(
            dataset_id=frozen.dataset_id,
            venue=frozen.venue,
            symbol=frozen.symbol,
            timeframe=frozen.timeframe,
            bucket=frozen.bucket,
            profile=frozen.profile,
            objects=(DatasetObject("ohlcv/a.parquet", "changed", 11),),
            start=frozen.start,
            end=frozen.end,
            exclusions=frozen.exclusions,
        )
        with self.assertRaises(RuntimeError) as caught:
            require_frozen_dataset(frozen, changed)
        message = str(caught.exception)
        self.assertIn("ohlcv/a.parquet", message)
        self.assertIn("etag abc -> changed", message)
        self.assertIn("size 10 -> 11", message)
        with self.assertRaises(RuntimeError) as digest_only:
            require_frozen_dataset(frozen, replace(frozen, digest="0" * 64))
        self.assertIn("canonical payload matches", str(digest_only.exception))

    def test_collector_append_is_clipped_to_the_frozen_window(self):
        index = _minutes("2024-06-01", 5)
        objects = (
            DatasetObject("ohlcv/binance/BTCUSDT/1m.parquet", "legacy", 100),
            DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "year-v1", 50),
        )
        kept, frozen, _ = freeze_dataset(
            _bars(index),
            objects,
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
        )
        extended = _bars(_minutes("2024-06-01", 8))
        grown = (
            objects[0],
            DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "year-v2", 80),
        )
        with self.assertLogs("research.evaluate", level="WARNING") as captured:
            aligned, report, appended = align_to_frozen_dataset(extended, frozen, grown)
        self.assertEqual(appended, 3)
        self.assertEqual(list(aligned.index), list(kept.index))
        self.assertEqual(report.rows_out, len(kept))
        self.assertEqual(report.exclusions, frozen.exclusions)
        self.assertTrue(any("Excluded 3 collector rows" in line for line in captured.output))
        self.assertTrue(any("year-v1 -> year-v2" in line for line in captured.output))
        self.assertTrue(any("size 50 -> 80" in line for line in captured.output))

        exact, exact_report, exact_appended = align_to_frozen_dataset(_bars(index), frozen, objects)
        self.assertEqual(exact_appended, 0)
        self.assertEqual(list(exact.index), list(kept.index))
        self.assertEqual(exact_report.exclusions, frozen.exclusions)

        with self.assertRaises(RuntimeError) as closed:
            align_to_frozen_dataset(
                extended,
                frozen,
                (
                    DatasetObject("ohlcv/binance/BTCUSDT/1m.parquet", "changed", 101),
                    grown[1],
                ),
            )
        self.assertIn("ohlcv/binance/BTCUSDT/1m.parquet", str(closed.exception))
        self.assertIn("not the open collector year", str(closed.exception))

        with self.assertRaises(RuntimeError) as shrunk:
            align_to_frozen_dataset(
                extended,
                frozen,
                (
                    objects[0],
                    DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "year-v2", 40),
                ),
            )
        self.assertIn("must grow", str(shrunk.exception))

        rewritten = extended.copy()
        rewritten.iloc[0, rewritten.columns.get_loc("close")] = -1.0
        with self.assertRaises(RuntimeError) as changed_window:
            align_to_frozen_dataset(rewritten, frozen, grown)
        self.assertIn("different frozen window", str(changed_window.exception))
        self.assertIn("nonpositive_prices", str(changed_window.exception))

        with self.assertRaises(RuntimeError) as same_end:
            align_to_frozen_dataset(_bars(index), frozen, grown)
        self.assertIn("does not match frozen dataset", str(same_end.exception))

    def test_a_new_collector_year_is_left_outside_the_frozen_window(self):
        index = _minutes("2024-12-31 23:58", 2)
        objects = (
            DatasetObject("ohlcv/binance/BTCUSDT/1m.parquet", "legacy", 100),
            DatasetObject("ohlcv/binance/BTCUSDT/1m/2024.parquet", "year-v1", 50),
        )
        kept, frozen, _ = freeze_dataset(
            _bars(index),
            objects,
            dataset_id="btcusdt-1m",
            venue="binance",
            symbol="BTCUSDT",
            timeframe="1m",
            bucket="trading-data-centheos",
            profile="trading",
        )
        extended = _bars(_minutes("2024-12-31 23:58", 3))
        with self.assertRaises(RuntimeError) as missing_year:
            align_to_frozen_dataset(extended, frozen, objects)
        self.assertIn("2025 collector object was added", str(missing_year.exception))

        with self.assertLogs("research.evaluate", level="WARNING") as captured:
            aligned, _, appended = align_to_frozen_dataset(
                extended,
                frozen,
                objects + (DatasetObject("ohlcv/binance/BTCUSDT/1m/2025.parquet", "next", 10),),
            )
        self.assertEqual(appended, 1)
        self.assertEqual(list(aligned.index), list(kept.index))
        self.assertTrue(any("added collector object" in line for line in captured.output))


class TestRegimes(unittest.TestCase):
    def test_filter_matches_a_hand_calculated_forward_pass(self):
        from scipy.stats import norm

        model = GaussianHMM(
            means=np.array([0.0, 0.0]),
            variances=np.array([1.0, 4.0]),
            transitions=np.array([[0.9, 0.1], [0.2, 0.8]]),
            initial=np.array([0.5, 0.5]),
        )
        sample = np.array([0.0, 3.0])
        filtered = filtered_probabilities(model, sample)
        first = model.initial * norm.pdf(0.0, loc=0.0, scale=np.array([1.0, 2.0]))
        first = first / first.sum()
        second = (first @ model.transitions) * norm.pdf(3.0, loc=0.0, scale=np.array([1.0, 2.0]))
        second = second / second.sum()
        self.assertTrue(np.allclose(filtered[0], first))
        self.assertTrue(np.allclose(filtered[1], second))
        self.assertTrue(np.allclose(filtered.sum(axis=1), 1.0))

    def test_filter_ignores_the_future_and_resets_after_a_gap(self):
        rng = np.random.default_rng(5)
        model = GaussianHMM(
            means=np.array([0.0, 0.0]),
            variances=np.array([1.0e-6, 4.0e-4]),
            transitions=np.array([[0.97, 0.03], [0.04, 0.96]]),
            initial=np.array([0.6, 0.4]),
        )
        sample = rng.normal(scale=0.001, size=180)
        original = filtered_probabilities(model, sample)
        changed = sample.copy()
        changed[-12:] += 0.05
        revised = filtered_probabilities(model, changed)
        self.assertTrue(np.allclose(original[:-12], revised[:-12]))
        self.assertFalse(np.allclose(original[-1], revised[-1]))

        index = _minutes("2024-01-01", 90)
        returns = pd.Series(sample[:90], index=index).drop(index[30:40])
        frame, gaps = filtered_probability_frame(model, returns)
        self.assertGreaterEqual(gaps, 1)
        self.assertEqual(clock_gap_resets(returns.index), gaps)
        second = returns.loc[returns.index >= index[40]]
        alone, alone_gaps = filtered_probability_frame(model, second)
        self.assertEqual(alone_gaps, 0)
        pd.testing.assert_frame_equal(frame.loc[second.index], alone)

        close = pd.Series(100 * np.exp(np.cumsum(sample[:90])), index=index)
        known, _ = minute_returns_known_at_close(close)
        shocked = close.copy()
        shocked.iloc[-1] *= 1.2
        known_shocked, _ = minute_returns_known_at_close(shocked)
        self.assertTrue(np.allclose(known.iloc[:-1].to_numpy(), known_shocked.iloc[:-1].to_numpy()))
        self.assertNotAlmostEqual(float(known.iloc[-1]), float(known_shocked.iloc[-1]))
        horizon = horizon_log_returns(close, 5).returns
        horizon_shocked = horizon_log_returns(shocked, 5).returns
        decision = horizon.index[-1]
        self.assertNotAlmostEqual(float(horizon.loc[decision]), float(horizon_shocked.loc[decision]))
        filtered_close, _ = filtered_probability_frame(model, known)
        filtered_shocked, _ = filtered_probability_frame(model, known_shocked)
        self.assertTrue(np.allclose(filtered_close.loc[decision].to_numpy(), filtered_shocked.loc[decision].to_numpy()))

    def test_collapsed_weights_and_smoothed_probabilities_fail(self):
        with self.assertRaises(RuntimeError):
            fit_gaussian_hmm(np.zeros(80))
        with self.assertRaises(ValueError):
            fit_weighted_gaussian(np.linspace(-1, 1, 40), np.zeros(40))
        with self.assertRaises(ValueError):
            fit_weighted_gaussian(np.linspace(-1, 1, 40), np.full(40, np.nan))
        index = _minutes("2024-01-01", 30)
        close = pd.Series(np.linspace(100, 101, len(index)), index=index)
        record = load_record("research/registry/EXP-H01.json")
        with self.assertRaises(ValueError):
            evaluate_h2(close, record, {**H2_CONFIG, "probability": "smoothed"})
        with self.assertRaises(ValueError):
            evaluate_h2(close, record, {**H2_CONFIG, "first_passage": "fixed_barrier"})
        with self.assertRaises(ValueError):
            h1_benchmark(load_record("research/registry/EXP-H02.json"))
        with self.assertRaises(RuntimeError):
            h1_benchmark(replace(record, decision="retain", result=""))

    def test_h1_benchmark_uses_student_t_only_when_h1_retains_it(self):
        record = load_record("research/registry/EXP-H01.json")
        open_record = h1_benchmark(replace(record, decision="", result=""))
        self.assertEqual(open_record.model, "gaussian")
        self.assertFalse(open_record.resolved)
        selected = h1_benchmark(replace(record, decision="", result="student_t"))
        self.assertEqual(selected.model, "student_t")
        self.assertTrue(selected.resolved)
        retained = h1_benchmark(replace(record, decision="retain", result="student_t"))
        self.assertEqual(retained.model, "student_t")
        self.assertTrue(retained.resolved)
        rejected = h1_benchmark(replace(record, decision="reject", result=""))
        self.assertEqual(rejected.model, "gaussian")
        self.assertTrue(rejected.resolved)

    def test_mixture_shortfall_matches_a_numerical_integral(self):
        from scipy.integrate import quad
        from scipy.stats import norm

        components = (GaussianModel(0.0, 1.0), GaussianModel(0.5, 4.0))
        weights = np.array([0.3, 0.7])

        def density(value: float) -> float:
            return float(weights[0] * norm.pdf(value, 0.0, 1.0) + weights[1] * norm.pdf(value, 0.5, 2.0))

        def distribution(value: float) -> float:
            return float(weights[0] * norm.cdf(value, 0.0, 1.0) + weights[1] * norm.cdf(value, 0.5, 2.0))

        from scipy.optimize import brentq

        level = brentq(lambda value: distribution(value) - 0.05, -20, 20)
        moment, _ = quad(lambda value: value * density(value), -40, level, limit=200)
        probabilities = np.tile(weights, (40, 1))
        probabilities[:, 0] += 1e-7
        scored = score_regime_forecast(components, probabilities, np.linspace(-1, 1, 40))
        self.assertAlmostEqual(scored.expected_shortfall, moment / 0.05, places=3)
        self.assertEqual(scored.model, "regime_mixture")

    def test_separated_regimes_improve_the_holdout_log_score(self):
        rng = np.random.default_rng(9)
        count = 5000
        state = 0
        values = np.empty(count)
        for time in range(count):
            values[time] = rng.normal(0.0, 0.001 if state == 0 else 0.02)
            if rng.random() > 0.98:
                state = 1 - state
        horizon = 5
        train_end = 3500
        fitted = fit_gaussian_hmm(values[:train_end])
        self.assertLess(fitted.model.variances[0], fitted.model.variances[1])
        probabilities = filtered_probabilities(fitted.model, values)
        train_times = np.arange(0, train_end - horizon)
        labels = np.array([values[time + 1:time + 1 + horizon].sum() for time in train_times])
        components = fit_regime_densities(labels, probabilities[train_times], "gaussian")
        test_times = np.arange(train_end, count - horizon)
        test_labels = np.array([values[time + 1:time + 1 + horizon].sum() for time in test_times])
        mixture = score_regime_forecast(components, probabilities[test_times], test_labels)
        baseline = score_distribution(fit_gaussian(labels), test_labels)
        self.assertGreater(mixture.mean_log_score, baseline.mean_log_score)

    def test_development_evaluation_does_not_touch_the_sealed_test(self):
        index = pd.date_range("2024-01-01", "2024-04-20", freq="min", tz="UTC")
        rng = np.random.default_rng(4)
        state = np.zeros(len(index), dtype=int)
        for time in range(1, len(index)):
            state[time] = state[time - 1] if rng.random() < 0.99 else 1 - state[time - 1]
        degrees = 6.0
        unit = rng.standard_t(degrees, size=len(index)) / np.sqrt(degrees / (degrees - 2.0))
        shocks = unit * np.where(state == 0, 0.0004, 0.004)
        close = pd.Series(100 * np.exp(np.cumsum(shocks)), index=index)
        record = replace(
            load_record("research/registry/EXP-H01.json"),
            horizons=(5,),
            decision="",
            result="",
        )
        config = dict(H2_CONFIG)
        config.update(
            {
                "sealed_months": 1,
                "train_months": 1,
                "validation_months": 1,
                "step_months": 1,
                "horizons_minutes": [5],
                "em_iterations": 3,
            }
        )
        evaluated = evaluate_h2(close, record, config)
        self.assertFalse(evaluated.sealed_access)
        self.assertFalse(evaluated.benchmark_resolved)
        self.assertEqual(evaluated.benchmark, "gaussian")
        self.assertIn("regime_mixture", set(evaluated.scores["model"]))
        self.assertTrue((evaluated.scores["sealed_access"] == False).all())
        self.assertGreater(evaluated.alignment_exclusions, 0)
        comparison = h2_log_score_comparison(evaluated.scores)
        self.assertIn("mean_advantage", comparison.columns)
        changed = close.copy()
        changed.loc[changed.index >= evaluated.boundary] *= 1.01
        repeated = evaluate_h2(changed, record, config)
        pd.testing.assert_frame_equal(evaluated.scores.reset_index(drop=True), repeated.scores.reset_index(drop=True))
        retained = replace(record, decision="retain", result="student_t")
        student = evaluate_h2(close, retained, config)
        self.assertTrue(student.benchmark_resolved)
        self.assertEqual(set(student.scores["model"]), {"student_t", "regime_mixture"})
        proposal = load_record("research/registry/EXP-H02.json")
        self.assertEqual(proposal.status, "Proposed")
        self.assertEqual(proposal.decision, "")
        self.assertEqual(proposal.parent_hypothesis, "EXP-H01")
        self.assertFalse(proposal.sealed_access)


if __name__ == "__main__":
    unittest.main()
