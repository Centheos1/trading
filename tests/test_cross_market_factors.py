"""Synthetic tests for H20 external-market factors; no store is read."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.evaluate import (
    confirm_h20,
    evaluate_h20,
    h20_development_decision,
    h20_log_score_comparison,
)
from research.factors import external_market_features
from research.manifest import (
    DatasetObject,
    QualityExclusions,
    build_manifest,
    build_panel_manifest,
)
from research.registry import ExperimentRecord, save_record


def _minutes(periods: int = 240) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=periods, freq="min", tz="UTC")


def _member(symbol: str, venue: str = "binance", version_id: str | None = None):
    return build_manifest(
        dataset_id=f"panel-{symbol.lower()}",
        venue=venue,
        symbol=symbol,
        timeframe="1m",
        bucket="bucket",
        profile="profile",
        objects=(
            DatasetObject(
                f"ohlcv/{venue}/{symbol}/1m.parquet",
                "etag",
                100,
                version_id=version_id or f"{symbol}-version",
            ),
        ),
        start="2024-01-01T00:00:00+00:00",
        end="2024-01-01T03:59:00+00:00",
        exclusions=QualityExclusions(),
    )


def _cross_panel(version_id: str | None = None):
    members = (
        _member("BTCUSDT", version_id=version_id),
        _member("SPX500_USD", "oanda", version_id=version_id),
    )
    return build_panel_manifest(
        dataset_id="cross-market-1m",
        members=members,
        start=members[0].start,
        end=members[0].end,
        alignment_exclusions=0,
        allow_cross_venue=True,
    )


def _record(experiment_id: str, *, parent: str | None, result: str = "") -> ExperimentRecord:
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


def _comparison_row(horizon: int, model: str, advantage: float, **extra) -> dict:
    row = {
        "horizon_minutes": horizon,
        "model": model,
        "mean_advantage": advantage,
        "median_advantage": advantage,
        "mean_vs_crypto": extra.get("vs_crypto", 0.0),
        "median_vs_crypto": extra.get("vs_crypto", 0.0),
        "mean_vs_external": extra.get("vs_external", 0.0),
        "median_vs_external": extra.get("vs_external", 0.0),
        "origins": 2,
        "expected_origins": 2,
    }
    row.update({key: value for key, value in extra.items() if key not in {"vs_crypto", "vs_external"}})
    return row


class TestCrossVenuePanel(unittest.TestCase):
    def test_same_venue_panel_stays_single_venue(self):
        panel = build_panel_manifest(
            dataset_id="majors",
            members=(_member("BTCUSDT"), _member("ETHUSDT")),
            start="2024-01-01T00:00:00+00:00",
            end="2024-01-01T03:59:00+00:00",
            alignment_exclusions=0,
        )
        self.assertEqual(panel.venue, "binance")

    def test_mixed_venues_require_an_explicit_cross_venue_panel(self):
        members = (_member("BTCUSDT"), _member("SPX500_USD", "oanda"))
        with self.assertRaisesRegex(ValueError, "share venue"):
            build_panel_manifest(
                dataset_id="cross",
                members=members,
                start=members[0].start,
                end=members[0].end,
                alignment_exclusions=0,
            )
        panel = build_panel_manifest(
            dataset_id="cross",
            members=members,
            start=members[0].start,
            end=members[0].end,
            alignment_exclusions=0,
            allow_cross_venue=True,
        )
        self.assertEqual(panel.venue, "cross-venue")

    def test_cross_venue_flag_rejects_one_venue(self):
        with self.assertRaisesRegex(ValueError, "same venue"):
            build_panel_manifest(
                dataset_id="majors",
                members=(_member("BTCUSDT"), _member("ETHUSDT")),
                start="2024-01-01T00:00:00+00:00",
                end="2024-01-01T03:59:00+00:00",
                alignment_exclusions=0,
                allow_cross_venue=True,
            )


class TestExternalState(unittest.TestCase):
    def test_features_are_lagged_and_do_not_fill_missing_returns(self):
        index = _minutes(40)
        returns = pd.DataFrame(
            {
                "SPX": np.linspace(0.001, 0.002, len(index)),
                "EUR": np.linspace(0.002, 0.001, len(index)),
                "JPY": np.linspace(-0.001, 0.001, len(index)),
            },
            index=index,
        )
        state = external_market_features(
            returns,
            feature_symbols=(("spx_return", "SPX"),),
            usd_factor_legs=(("EUR", -1), ("JPY", 1)),
            lag_minutes=1,
        )
        self.assertNotIn(index[0], state.features.index)
        self.assertAlmostEqual(state.features.loc[index[2], "spx_return"], returns.loc[index[1], "SPX"])
        self.assertNotAlmostEqual(
            state.features.loc[index[2], "spx_return"], returns.loc[index[2], "SPX"]
        )
        expected_usd = ((-1 * returns.loc[index[1], "EUR"]) + returns.loc[index[1], "JPY"]) / 2
        self.assertAlmostEqual(state.features.loc[index[2], "usd_factor"], expected_usd)
        broken = returns.copy()
        broken.iloc[3, 0] = np.nan
        with self.assertRaisesRegex(ValueError, "refusing to fill"):
            external_market_features(
                broken,
                feature_symbols=(("spx_return", "SPX"),),
                usd_factor_legs=(("EUR", -1), ("JPY", 1)),
                lag_minutes=1,
            )

    def test_positive_quote_currency_move_reduces_the_usd_factor(self):
        index = _minutes(5)
        returns = pd.DataFrame(
            {"EUR": [0.0, 0.01, 0.0, 0.0, 0.0], "JPY": [0.0, 0.0, 0.0, 0.0, 0.0]},
            index=index,
        )
        state = external_market_features(
            returns,
            feature_symbols=(("spx_return", "EUR"),),
            usd_factor_legs=(("EUR", -1), ("JPY", 1)),
            lag_minutes=1,
        )
        self.assertLess(state.features.loc[index[2], "usd_factor"], 0.0)


class TestH20Decision(unittest.TestCase):
    def _frame(self, external: float, crypto: float = -0.1, combined: float | None = None, **extra):
        rows = []
        combined_advantage = external if combined is None else combined
        for horizon in (5, 15, 60):
            rows.append(_comparison_row(horizon, "crypto_factor", crypto, vs_crypto=0.0, vs_external=crypto - external))
            rows.append(_comparison_row(horizon, "external_factor", external, vs_crypto=external - crypto, vs_external=0.0))
            rows.append(
                _comparison_row(
                    horizon,
                    "combined",
                    combined_advantage,
                    vs_crypto=combined_advantage - crypto,
                    vs_external=combined_advantage - external,
                )
            )
        frame = pd.DataFrame(rows)
        for key, value in extra.items():
            frame[key] = value
        return frame

    def test_external_state_rejects_the_null_without_requiring_crypto(self):
        verdict = h20_development_decision(self._frame(0.2, crypto=-0.1, combined=0.05)).iloc[0]
        self.assertEqual(verdict.null_decision, "reject")
        self.assertEqual(verdict.benchmark, "external_factor")

    def test_crypto_only_improvement_accepts_the_external_null(self):
        verdict = h20_development_decision(self._frame(-0.2, crypto=0.2, combined=-0.1)).iloc[0]
        self.assertEqual(verdict.null_decision, "accept")
        self.assertEqual(verdict.benchmark, "crypto_factor")

    def test_combined_is_selected_only_when_it_beats_both_parents(self):
        verdict = h20_development_decision(self._frame(0.1, crypto=0.05, combined=0.3)).iloc[0]
        self.assertEqual(verdict.benchmark, "combined")
        weaker = h20_development_decision(self._frame(0.2, crypto=0.05, combined=0.1)).iloc[0]
        self.assertEqual(weaker.benchmark, "external_factor")

    def test_incomplete_origins_defer(self):
        frame = self._frame(0.2)
        frame.loc[frame.index[0], "origins"] = 1
        verdict = h20_development_decision(frame).iloc[0]
        self.assertEqual(verdict.null_decision, "defer")

    def test_comparison_requires_the_full_nested_ladder(self):
        scores = pd.DataFrame(
            [
                {
                    "horizon_minutes": 5,
                    "origin": "one",
                    "expected_origins": 1,
                    "model": model,
                    "mean_log_score": 1.0,
                }
                for model in ("own_price", "crypto_factor", "external_factor")
            ]
        )
        with self.assertRaisesRegex(RuntimeError, "combined"):
            h20_log_score_comparison(scores)


class TestH20Gates(unittest.TestCase):
    def test_development_evaluation_does_not_score_the_sealed_block(self):
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
        h1 = replace(_record("EXP-H01", parent=None, result="gaussian"), horizons=(5,))
        config = {
            "symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SPX", "EUR", "JPY"],
            "crypto_symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
            "venues": {
                "BTCUSDT": "binance",
                "ETHUSDT": "binance",
                "BNBUSDT": "binance",
                "SPX": "oanda",
                "EUR": "oanda",
                "JPY": "oanda",
            },
            "external_features": [["spx_return", "SPX"]],
            "usd_factor_legs": [["EUR", -1], ["JPY", 1]],
            "horizons_minutes": [5],
            "sealed_months": 1,
            "train_months": 1,
            "validation_months": 1,
            "step_months": 1,
            "own_price_volatility_window": 20,
        }
        first = evaluate_h20(panel, h1, config)
        changed = panel.copy()
        changed.loc[changed.index >= first.boundary, "BTCUSDT"] *= 3.0
        second = evaluate_h20(changed, h1, config)
        pd.testing.assert_frame_equal(first.scores, second.scores)
        self.assertFalse(first.sealed_access)

    def test_implied_volatility_proxy_is_rejected(self):
        index = _minutes(80)
        panel = pd.DataFrame(
            {
                "BTCUSDT": np.linspace(100, 101, len(index)),
                "ETHUSDT": np.linspace(50, 51, len(index)),
                "BNBUSDT": np.linspace(80, 81, len(index)),
                "VIX": np.linspace(15, 16, len(index)),
                "EUR": np.linspace(1.1, 1.2, len(index)),
                "JPY": np.linspace(150, 151, len(index)),
            },
            index=index,
        )
        h1 = replace(_record("EXP-H01", parent=None, result="gaussian"), horizons=(5, 15, 60))
        with self.assertRaisesRegex(ValueError, "implied-volatility"):
            evaluate_h20(
                panel,
                h1,
                {
                    "symbols": list(panel.columns),
                    "crypto_symbols": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
                    "venues": {symbol: "binance" if symbol.endswith("USDT") else "oanda" for symbol in panel.columns},
                    "external_features": [["vix_return", "VIX"]],
                    "usd_factor_legs": [["EUR", -1], ["JPY", 1]],
                },
            )

    def test_confirmation_refuses_dirty_code_before_sealed_access(self):
        record = _record("EXP-H20", parent="EXP-H01")
        h1 = replace(
            _record("EXP-H01", parent=None, result="gaussian"),
            status="Research validated",
            decision="retain",
            sealed_access=True,
            dataset_digest="a" * 64,
            run_id="h1-run",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H20.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc+dirty"):
                with self.assertRaisesRegex(RuntimeError, "refused"):
                    confirm_h20(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=_cross_panel(),
                        root=directory,
                        record_path=path,
                    )

    def test_confirmation_refuses_unresolved_h1(self):
        record = _record("EXP-H20", parent="EXP-H01")
        h1 = replace(_record("EXP-H01", parent=None), dataset_digest="a" * 64)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "EXP-H20.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "resolved H1 benchmark"):
                    confirm_h20(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=_cross_panel(),
                        root=directory,
                        record_path=path,
                    )

    def test_confirmation_refuses_mutable_panel_objects(self):
        panel = _cross_panel(version_id="null")
        record = replace(
            _record("EXP-H20", parent="EXP-H01"),
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
            path = Path(directory) / "EXP-H20.json"
            save_record(path, record)
            with patch("research.evaluate.code_version", return_value="abc"):
                with self.assertRaisesRegex(RuntimeError, "immutable version IDs"):
                    confirm_h20(
                        pd.DataFrame(),
                        record,
                        h1,
                        panel_manifest=panel,
                        root=directory,
                        record_path=path,
                    )


if __name__ == "__main__":
    unittest.main()
