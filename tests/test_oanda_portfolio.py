"""Specification tests for the Oanda frontier research notebook."""

from __future__ import annotations

import unittest
from datetime import date

import numpy as np
import pandas as pd

from oanda_portfolio.daily import (
    aggregate_to_daily,
    completed_sessions,
    load_oanda_daily_panel,
    ny_close_dates,
)
from oanda_portfolio.frontier import (
    FrontierConfig,
    annualize_moments,
    frontier_portfolio,
    ledoit_wolf_covariance,
    shrink_correlation,
)
from oanda_portfolio.plan import build_legs, build_plan, units_from_weight
from oanda_portfolio.practice import (
    FillResult,
    PlanStore,
    PracticeBroker,
    format_units,
    net_position_units,
    submit_plan,
    trade_step,
)
from oanda_portfolio.select import select_portfolio
from oanda_portfolio.simulate import choose_size, simulate


def _minutes(timestamps, closes, spreads):
    index = pd.to_datetime(timestamps, utc=True)
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
            "spread": spreads,
            "volume": 1,
        },
        index=index,
    )


class TestDailySessions(unittest.TestCase):
    def test_seventeen_hundred_new_york_is_the_session_boundary(self):
        labels = ny_close_dates(pd.to_datetime([
            "2024-01-02 21:59:00",
            "2024-01-02 22:00:00",
            "2024-01-02 22:01:00",
        ], utc=True))
        self.assertEqual(list(labels.strftime("%Y-%m-%d")), [
            "2024-01-02",
            "2024-01-02",
            "2024-01-03",
        ])

    def test_missing_session_is_absent_and_not_a_zero_return(self):
        minutes = _minutes(
            ["2024-01-02 22:00:00", "2024-01-04 22:00:00"],
            [1.0, 1.1],
            [0.0001, 0.0002],
        )
        daily = aggregate_to_daily(minutes)
        self.assertEqual(list(daily.index.strftime("%Y-%m-%d")), ["2024-01-02", "2024-01-04"])
        self.assertNotIn("2024-01-03", set(daily.index.strftime("%Y-%m-%d")))
        returns = daily["close"].pct_change()
        self.assertTrue(np.isnan(returns.iloc[0]))
        self.assertAlmostEqual(float(returns.iloc[1]), 0.1)

    def test_an_open_session_is_excluded_until_the_new_york_close(self):
        labels = ny_close_dates(pd.to_datetime([
            "2026-10-15 21:00:00",
            "2026-10-16 02:00:00",
        ], utc=True))
        sessions = pd.DatetimeIndex(pd.unique(labels))
        before_close = pd.Timestamp("2026-10-16 02:55", tz="America/New_York")
        closed = completed_sessions(sessions, now=before_close)
        self.assertEqual(list(closed.strftime("%Y-%m-%d")), ["2026-10-15"])
        at_close = pd.Timestamp("2026-10-16 17:00", tz="America/New_York")
        closed = completed_sessions(sessions, now=at_close)
        self.assertEqual(list(closed.strftime("%Y-%m-%d")), ["2026-10-15", "2026-10-16"])

        class Store:
            def list_symbols(self, exchange, timeframe="1m"):
                return ["EUR_USD", "SUGAR_USD"]

            def read(self, exchange, symbol, timeframe, from_ts=None, to_ts=None):
                if symbol == "SUGAR_USD":
                    return _minutes(["2026-10-15 21:00:00"], [20.0], [0.02])
                return _minutes(
                    ["2026-10-15 21:00:00", "2026-10-16 02:00:00"],
                    [1.1, 1.2],
                    [0.0001, 0.0001],
                )

        closes, _, failures = load_oanda_daily_panel(Store(), now=before_close)
        self.assertEqual(failures, [])
        self.assertEqual(list(closes.index.strftime("%Y-%m-%d")), ["2026-10-15"])
        self.assertIn("SUGAR_USD", closes.columns)

    def test_a_failed_symbol_is_counted_and_the_rest_are_kept(self):
        class Store:
            def list_symbols(self, exchange, timeframe="1m"):
                return ["EUR_USD", "BAD"]

            def read(self, exchange, symbol, timeframe, from_ts=None, to_ts=None):
                if symbol == "BAD":
                    raise RuntimeError("corrupt parquet")
                return _minutes(["2024-01-02 22:00:00"], [1.2], [0.0001])

        closes, spreads, failures = load_oanda_daily_panel(Store())
        self.assertEqual(list(closes.columns), ["EUR_USD"])
        self.assertEqual(failures[0]["symbol"], "BAD")
        self.assertIn("corrupt", failures[0]["error"])
        self.assertAlmostEqual(float(spreads["EUR_USD"].iloc[0]), 0.0001)

    def test_every_symbol_failing_raises(self):
        class Store:
            def list_symbols(self, exchange, timeframe="1m"):
                return ["BAD"]

            def read(self, exchange, symbol, timeframe, from_ts=None, to_ts=None):
                raise RuntimeError("down")

        with self.assertRaises(RuntimeError):
            load_oanda_daily_panel(Store())


class TestFrontier(unittest.TestCase):
    def test_self_financing_direction_sums_to_zero_and_frontier_hits_the_target(self):
        mean = pd.Series([0.1, 0.2], index=["A", "B"])
        covariance = pd.DataFrame(np.eye(2), index=mean.index, columns=mean.index)
        result = frontier_portfolio(
            mean,
            covariance,
            FrontierConfig(target_return=0.20, weight_cap=1.0),
        )
        self.assertEqual(result.method, "closed_form")
        self.assertAlmostEqual(float(result.weights.sum()), 1.0, places=8)
        self.assertAlmostEqual(float(result.weights @ mean), 0.20, places=8)
        self.assertAlmostEqual(float(result.weights["A"]), 0.0, places=6)
        self.assertAlmostEqual(float(result.weights["B"]), 1.0, places=6)

    def test_caps_lower_an_unreachable_target(self):
        names = ["A", "B", "C", "D"]
        mean = pd.Series([0.01, 0.02, 0.03, 0.50], index=names)
        covariance = pd.DataFrame(np.eye(4) * 0.01, index=names, columns=names)
        result = frontier_portfolio(
            mean,
            covariance,
            FrontierConfig(target_return=0.20, gross_cap=2.0, weight_cap=0.30),
        )
        self.assertEqual(result.method, "constrained")
        self.assertLess(result.target_used, 0.20)
        self.assertIn("lowered", result.note)
        self.assertAlmostEqual(float(result.weights.sum()), 1.0, places=5)
        self.assertLessEqual(float(result.weights.abs().max()), 0.30 + 1e-8)
        self.assertLessEqual(float(result.weights.abs().sum()), 2.0 + 1e-8)

    def test_constrained_solver_finishes_a_twenty_name_book(self):
        rng = np.random.default_rng(11)
        names = [f"N{i:02d}" for i in range(20)]
        mean = pd.Series(0.02, index=names)
        mean.iloc[0] = 0.35
        correlation = np.full((20, 20), 0.85)
        np.fill_diagonal(correlation, 1.0)
        noise = rng.normal(0.0, 0.01, size=(20, 20))
        correlation = correlation + 0.5 * (noise + noise.T)
        np.fill_diagonal(correlation, 1.0)
        correlation = (correlation + correlation.T) / 2.0
        values, vectors = np.linalg.eigh(correlation)
        correlation = vectors @ np.diag(np.clip(values, 1e-4, None)) @ vectors.T
        scale = np.sqrt(np.diag(correlation))
        correlation = correlation / np.outer(scale, scale)
        vol = np.full(20, 0.10)
        vol[0] = 0.30
        covariance = pd.DataFrame(
            np.outer(vol, vol) * correlation, index=names, columns=names
        )
        result = frontier_portfolio(
            mean,
            covariance,
            FrontierConfig(target_return=0.20, gross_cap=2.0, weight_cap=0.30),
        )
        self.assertEqual(result.method, "constrained")
        self.assertAlmostEqual(float(result.weights.sum()), 1.0, places=5)
        self.assertAlmostEqual(float(result.weights @ mean), result.target_used, places=5)
        self.assertLessEqual(float(result.weights.abs().max()), 0.30 + 1e-6)
        self.assertLessEqual(float(result.weights.abs().sum()), 2.0 + 1e-6)
        self.assertGreater(float(result.weights.abs().sum()), 1.0)

    def test_ledoit_wolf_is_positive_semidefinite_and_annualizes_like_the_notebook(self):
        rng = np.random.default_rng(7)
        index = pd.bdate_range("2020-01-01", periods=40)
        frame = pd.DataFrame(rng.normal(0.0, 0.01, size=(40, 3)), index=index, columns=list("ABC"))
        covariance, alpha = ledoit_wolf_covariance(frame)
        self.assertGreaterEqual(alpha, 0.0)
        self.assertLessEqual(alpha, 1.0)
        self.assertGreaterEqual(float(np.linalg.eigvalsh(covariance)[0]), -1e-10)
        mean, annual, factor, _ = annualize_moments(frame)
        self.assertAlmostEqual(factor, 40 / ((index[-1] - index[0]).days / 365.0))
        self.assertTrue(np.allclose(annual.to_numpy(), covariance * factor))
        self.assertTrue(np.allclose(mean.to_numpy(), frame.mean().to_numpy() * factor))

    def test_shrunk_correlation_has_unit_diagonal_and_no_negative_eigenvalue(self):
        raw = np.array([
            [1.0, 0.9, 0.9],
            [0.9, 1.0, -0.9],
            [0.9, -0.9, 1.0],
        ])
        counts = np.full((3, 3), 100.0)
        result = shrink_correlation(raw, counts)
        self.assertTrue(np.allclose(np.diag(result), 1.0))
        self.assertGreaterEqual(float(np.linalg.eigvalsh(result)[0]), -1e-8)


class TestSelection(unittest.TestCase):
    def test_lowest_spread_represents_the_cluster_and_tie_uses_the_smaller_size(self):
        index = pd.bdate_range("2023-01-02", periods=40)
        returns = pd.DataFrame(
            {
                "AAA": np.linspace(0.001, 0.002, 40),
                "AAB": np.linspace(0.001, 0.002, 40) + 0.00001,
                "ZZZ": -np.linspace(0.001, 0.002, 40),
            },
            index=index,
        )
        spreads = pd.DataFrame(
            {"AAA": 0.0004, "AAB": 0.0001, "ZZZ": 0.0002},
            index=index,
        )
        selection = select_portfolio(returns, spreads, portfolio_size=2, min_overlap=30)
        self.assertIn("AAB", selection.symbols)
        self.assertNotIn("AAA", selection.symbols)
        self.assertIn("ZZZ", selection.symbols)
        self.assertEqual(choose_size({10: 0.5, 15: 0.5, 20: 0.2}), 10)
        self.assertEqual(choose_size({10: 0.2, 15: 0.8, 20: 0.8}), 15)


class TestOrderPlan(unittest.TestCase):
    def test_sign_change_flattens_before_it_opens(self):
        legs, skips = build_legs(
            session_date=date(2026, 10, 13),
            current_units={"EUR_USD": 1000},
            target_units={"EUR_USD": -200},
            min_trade_units=1,
        )
        self.assertEqual([leg.phase for leg in legs], ["reduce", "increase"])
        self.assertEqual(legs[0].units, -1000)
        self.assertEqual(legs[1].units, -200)
        self.assertEqual(legs[0].position_fill, "REDUCE_ONLY")
        self.assertEqual(legs[1].position_fill, "OPEN_ONLY")
        self.assertEqual(skips, [])

    def test_dust_and_a_blocked_flip_are_counted(self):
        legs, skips = build_legs(
            session_date="2026-10-13",
            current_units={"EUR_USD": 2, "GBP_USD": 100},
            target_units={"EUR_USD": -50, "GBP_USD": 103},
            min_trade_units=10,
        )
        self.assertEqual(legs, [])
        self.assertEqual(len(skips), 3)
        reasons = {item["reason"] for item in skips}
        self.assertIn("flatten below minimum trade size", reasons)
        self.assertIn("open blocked because the current position could not be flattened", reasons)
        self.assertIn("delta below minimum trade size", reasons)

    def test_units_round_to_the_trade_step(self):
        self.assertEqual(units_from_weight(0.30, 100_000, 1.25, step=1), 24000)
        self.assertEqual(units_from_weight(0.25, 10_000, 8000, step=0.1), 0.3)
        self.assertEqual(format_units(0.3), "0.3")
        self.assertEqual(format_units(24000), "24000")
        self.assertEqual(format_units(-1.5199999999999996), "-1.52")
        legs, _ = build_legs(
            session_date="2026-10-15",
            current_units={"LTC_USD": 7.02},
            target_units={"LTC_USD": 5.5},
            min_trade_units=1,
            trade_steps={"LTC_USD": 0.01},
        )
        self.assertEqual(legs[0].units, -1.52)
        self.assertEqual(format_units(legs[0].units), "-1.52")
        with self.assertRaises(ValueError):
            units_from_weight(0.1, 100_000, 0.0)

    def test_a_negative_short_side_stays_short(self):
        self.assertEqual(net_position_units(0.0, -26762.0), -26762.0)
        self.assertEqual(net_position_units(100.0, 0.0), 100.0)
        self.assertEqual(net_position_units(0.0, 50.0), -50.0)

    def test_fractional_minimum_trade_size_is_kept(self):
        self.assertEqual(trade_step({"name": "AU200_AUD", "minimumTradeSize": "0.1"}), 0.1)
        self.assertEqual(
            trade_step({"name": "AU200_AUD", "minimumTradeSize": "0", "tradeUnitsPrecision": 1}),
            0.1,
        )
        with self.assertRaises(RuntimeError):
            trade_step({"name": "AU200_AUD", "minimumTradeSize": "0"})
        plan = build_plan(
            session_date="2026-10-13",
            list_version="v1",
            nav=10_000,
            weights={"AU200_AUD": 0.25},
            mids={"AU200_AUD": 8000},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
            trade_steps={"AU200_AUD": 0.1},
        )
        self.assertEqual(plan.target_units["AU200_AUD"], 0.3)
        self.assertEqual(plan.legs[0].units, 0.3)
        self.assertEqual(format_units(plan.legs[0].units), "0.3")

    def test_quote_conversion_and_margin_budget_scale_execution(self):
        plan = build_plan(
            session_date="2026-10-13",
            list_version="v1",
            nav=100_000,
            weights={"XAU_XAG": 0.30},
            mids={"XAU_XAG": 80.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
            trade_steps={"XAU_XAG": 0.1},
            quote_home_conversions={"XAU_XAG": 50.0},
            margin_rates={"XAU_XAG": 0.10},
            margin_budget=1_500,
        )
        self.assertLess(plan.allocation_scale, 1.0)
        self.assertEqual(plan.target_units["XAU_XAG"], 3.7)
        self.assertLessEqual(plan.estimated_margin, plan.margin_budget)
        self.assertIn("allocation scaled", plan.note)

    def test_a_short_is_reduced_by_buying(self):
        plan = build_plan(
            session_date="2026-10-15",
            list_version="v1",
            nav=100_000,
            weights={"CAD_SGD": -0.16495},
            mids={"CAD_SGD": 1.0},
            current_units={"CAD_SGD": -26762},
            min_trade_units=1,
            target_return_used=0.2,
        )
        self.assertEqual(plan.target_units["CAD_SGD"], -16495)
        self.assertEqual([(leg.phase, leg.units, leg.position_fill) for leg in plan.legs], [
            ("reduce", 10267, "REDUCE_ONLY"),
        ])

    def test_margin_target_scales_up_without_passing_the_ceiling(self):
        plan = build_plan(
            session_date="2026-10-15",
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.10},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
            quote_home_conversions={"EUR_USD": 1.0},
            margin_rates={"EUR_USD": 1.0},
            margin_capacity=100_000,
            weight_cap=1.0,
            gross_cap=2.0,
        )
        self.assertAlmostEqual(plan.allocation_scale, 2.5, places=4)
        self.assertAlmostEqual(plan.margin_utilization, 0.25, places=3)
        self.assertLessEqual(plan.estimated_margin, 100_000 * 0.30)

    def test_margin_target_scales_past_the_research_weight_cap(self):
        plan = build_plan(
            session_date="2026-10-15",
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.30},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
            quote_home_conversions={"EUR_USD": 1.0},
            margin_rates={"EUR_USD": 0.05},
            margin_capacity=100_000,
        )
        self.assertGreater(plan.allocation_scale, 1.0)
        self.assertAlmostEqual(plan.margin_utilization, 0.25, places=3)
        self.assertLessEqual(plan.estimated_margin, 100_000 * 0.30)
        self.assertIn("past the research cap", plan.note)


class FakeBroker:
    def __init__(self, fills):
        self.fills = list(fills)
        self.orders = []
        self.live = False

    def assert_practice(self):
        if self.live:
            raise RuntimeError("live account")

    def place_market(self, instrument, units, client_order_id, position_fill):
        self.orders.append((instrument, units, client_order_id, position_fill))
        fill = self.fills.pop(0)
        status, filled = fill[:2]
        message = fill[2] if len(fill) > 2 else (
            "" if status != "rejected" else "rejected"
        )
        return FillResult(status, filled, 1.1, message=message)


class TestPracticeExecution(unittest.TestCase):
    def test_live_account_type_is_refused_before_an_order(self):
        with self.assertRaises(RuntimeError):
            PracticeBroker(account_id="abc", access_token="token", account_type="live")

    def test_reducing_fill_precedes_the_open_and_a_partial_stops_the_book(self):
        plan = build_plan(
            session_date=date(2026, 10, 13),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": -0.2},
            mids={"EUR_USD": 1.0},
            current_units={"EUR_USD": 100},
            min_trade_units=1,
            target_return_used=0.2,
        )
        broker = FakeBroker([("partial", -40)])
        store = PlanStore(self._path("partial"))
        result = submit_plan(plan, broker, store)
        self.assertEqual(len(broker.orders), 1)
        self.assertEqual(broker.orders[0][3], "REDUCE_ONLY")
        self.assertEqual(result.status, "reducing")
        reloaded = store.load("2026-10-13")
        self.assertEqual(reloaded.legs[0].filled_units, -40)
        self.assertEqual(reloaded.target_units["EUR_USD"], plan.target_units["EUR_USD"])

    def test_restart_sends_only_the_remainder_and_a_second_reject_fails_the_day(self):
        plan = build_plan(
            session_date=date(2026, 10, 13),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 1.0},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
        )
        store = PlanStore(self._path("retry"))
        first = FakeBroker([("rejected", 0)])
        stopped = submit_plan(plan, first, store)
        self.assertEqual(stopped.status, "increasing")
        self.assertEqual(len(first.orders), 1)
        second = FakeBroker([("rejected", 0)])
        failed = submit_plan(plan, second, store)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(second.orders[0][1], plan.legs[0].units)
        third = FakeBroker([("filled", plan.legs[0].units)])
        unchanged = submit_plan(plan, third, store)
        self.assertEqual(unchanged.status, "failed")
        self.assertEqual(third.orders, [])

    def test_a_simulated_plan_is_not_sent(self):
        plan = build_plan(
            session_date=date(2026, 10, 13),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 1.0},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
        )
        plan.status = "simulated"
        broker = FakeBroker([("filled", 1)])
        with self.assertRaises(RuntimeError):
            submit_plan(plan, broker, PlanStore(self._path("sim")))
        self.assertEqual(broker.orders, [])

    def test_insufficient_margin_fails_immediately_and_is_not_retried(self):
        plan = build_plan(
            session_date=date(2026, 10, 13),
            list_version="v1",
            nav=100_000,
            weights={"XAU_XAG": 0.3},
            mids={"XAU_XAG": 80.0},
            current_units={},
            min_trade_units=0.1,
            target_return_used=0.2,
        )
        store = PlanStore(self._path("margin"))
        first = FakeBroker([("rejected", 0, "INSUFFICIENT_MARGIN")])
        failed = submit_plan(plan, first, store)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(len(first.orders), 1)
        second = FakeBroker([("filled", plan.legs[0].units)])
        unchanged = submit_plan(plan, second, store)
        self.assertEqual(unchanged.status, "failed")
        self.assertEqual(second.orders, [])

    def test_restart_rebuilds_legs_from_the_broker_position(self):
        plan = build_plan(
            session_date=date(2026, 10, 15),
            list_version="v1",
            nav=100_000,
            weights={"CAD_SGD": -0.16495},
            mids={"CAD_SGD": 1.0},
            current_units={"CAD_SGD": 26762},
            min_trade_units=1,
            target_return_used=0.2,
        )
        self.assertEqual(plan.legs[0].units, -26762)
        store = PlanStore(self._path("reconcile"))
        store.save(plan)

        class Broker(FakeBroker):
            def positions(self):
                return {"CAD_SGD": -26762}

        broker = Broker([("filled", 10267)])
        resumed = submit_plan(plan, broker, store)
        self.assertEqual(broker.orders[0][1], 10267)
        self.assertEqual(broker.orders[0][3], "REDUCE_ONLY")
        self.assertEqual(resumed.status, "complete")

    def test_an_increase_above_the_margin_ceiling_is_not_sent(self):
        plan = build_plan(
            session_date=date(2026, 10, 15),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.10},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
            quote_home_conversions={"EUR_USD": 1.0},
            margin_rates={"EUR_USD": 0.05},
            margin_capacity=100_000,
            weight_cap=1.0,
        )

        class Broker(FakeBroker):
            def positions(self):
                return {}

            def account_summary(self):
                return {"marginUsed": "90000", "marginAvailable": "10000"}

        broker = Broker([("filled", plan.legs[0].units)])
        store = PlanStore(self._path("ceiling"))
        stopped = submit_plan(plan, broker, store)
        self.assertEqual(broker.orders, [])
        self.assertIn("ceiling", stopped.note)

    def test_a_halted_increase_is_skipped_and_the_next_order_is_sent(self):
        plan = build_plan(
            session_date=date(2026, 10, 15),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.1, "GBP_USD": 0.1},
            mids={"EUR_USD": 1.0, "GBP_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
        )
        broker = FakeBroker([
            ("rejected", 0, "MARKET_HALTED"),
            ("filled", plan.legs[1].units),
        ])
        result = submit_plan(plan, broker, PlanStore(self._path("halted")))
        self.assertEqual([order[0] for order in broker.orders], ["EUR_USD", "GBP_USD"])
        self.assertEqual(result.status, "complete")
        halted = next(leg for leg in result.legs if leg.instrument == "EUR_USD")
        self.assertEqual(halted.status, "skipped")

    def test_a_larger_margin_scale_replaces_an_unfinished_book(self):
        small = build_plan(
            session_date=date(2026, 10, 15),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.10},
            mids={"EUR_USD": 1.0},
            current_units={},
            min_trade_units=1,
            target_return_used=0.2,
        )
        small.legs[0].status = "filled"
        small.legs[0].filled_units = small.legs[0].units
        small.status = "increasing"
        store = PlanStore(self._path("rescale"))
        store.save(small)
        held = small.target_units["EUR_USD"]
        larger = build_plan(
            session_date=date(2026, 10, 15),
            list_version="v1",
            nav=100_000,
            weights={"EUR_USD": 0.10},
            mids={"EUR_USD": 1.0},
            current_units={"EUR_USD": held},
            min_trade_units=1,
            target_return_used=0.2,
            quote_home_conversions={"EUR_USD": 1.0},
            margin_rates={"EUR_USD": 0.05},
            margin_capacity=100_000,
            weight_cap=1.0,
        )
        gap = larger.target_units["EUR_USD"] - held

        class Broker(FakeBroker):
            def positions(self):
                return {"EUR_USD": held}

            def account_summary(self):
                return {"marginUsed": "0", "marginAvailable": "100000"}

        broker = Broker([("filled", gap)])
        resumed = submit_plan(larger, broker, store)
        self.assertEqual(broker.orders[0][1], gap)
        self.assertNotEqual(broker.orders[0][2], small.legs[0].client_order_id)
        self.assertGreater(resumed.allocation_scale, 1.0)
        self.assertEqual(resumed.status, "complete")

    def _path(self, name):
        import tempfile
        from pathlib import Path
        root = Path(tempfile.mkdtemp(prefix=f"oanda-plan-{name}-"))
        self.addCleanup(lambda: None)
        return root


class TestWalkForward(unittest.TestCase):
    def test_simulation_records_plans_without_sending_and_skips_a_missing_candle(self):
        index = pd.bdate_range("2024-01-02", periods=140)
        rng = np.random.default_rng(1)
        shock = rng.normal(0.0, 0.002, size=(len(index), 4))
        drift = np.array([0.0002, 0.0002, -0.0001, 0.0004])
        returns = shock + drift
        closes = pd.DataFrame(
            100 * np.exp(np.cumsum(returns, axis=0)),
            index=index,
            columns=["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"],
        )
        spreads = pd.DataFrame(
            {"EUR_USD": 0.0002, "GBP_USD": 0.0003, "USD_JPY": 0.02, "XAU_USD": 0.5},
            index=index,
        )
        hole = index[100]
        closes.loc[hole, "EUR_USD"] = np.nan
        config = FrontierConfig(
            target_return=0.05,
            lookback=30,
            min_overlap=30,
            gross_cap=2.0,
            weight_cap=0.8,
            portfolio_sizes=(2, 3),
            score_months=2,
            min_trade_units=1,
            research_nav=50_000,
        )
        result = simulate(closes, spreads, config)
        self.assertTrue(result.plans)
        self.assertTrue(all(plan.sent_to_broker is False for plan in result.plans))
        self.assertTrue(all(plan.status == "simulated" for plan in result.plans))
        self.assertTrue(any("missing daily candle" in item["reason"] for item in result.skips))
        self.assertTrue(result.final_weights is not None)
        self.assertAlmostEqual(float(result.final_weights.sum()), 1.0, places=5)
        self.assertLessEqual(float(result.final_weights.abs().max()), 0.8 + 1e-6)
        traded = result.covariance_comparison["ledoit_wolf"]
        sample = result.covariance_comparison["sample"]
        self.assertFalse(np.allclose(traded.to_numpy(), sample.to_numpy()))


if __name__ == "__main__":
    unittest.main()
