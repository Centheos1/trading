"""
Phase 4 tests for RiskEngine pybind11 bindings.

These tests verify that the C++ RiskEngine is correctly exposed to Python
and that the core operations work as expected across the language boundary.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Attempt import — skip gracefully if the C++ module isn't built or lacks Phase 4 bindings
try:
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtestingCpp", "orderflow", "build"))
    import orderflow_engine as ofe
    HAS_MODULE = hasattr(ofe, "RiskEngine")
except ImportError:
    HAS_MODULE = False


@unittest.skipUnless(HAS_MODULE, "orderflow_engine C++ module not built or missing RiskEngine")
class TestRiskEngineBindings(unittest.TestCase):

    def test_construction(self):
        re = ofe.RiskEngine()
        self.assertAlmostEqual(re.get_consumed_es(), 0.0)
        self.assertAlmostEqual(re.get_position_qty(), 0.0)
        self.assertTrue(re.is_budget_exhausted())

    def test_set_budget(self):
        re = ofe.RiskEngine()
        re.set_budget(1000.0, 10000.0, 0.8)
        snap = re.get_snapshot()
        self.assertAlmostEqual(snap.es_budget, 1000.0)
        self.assertAlmostEqual(snap.max_position_usd, 10000.0)
        self.assertAlmostEqual(snap.risk_multiplier, 0.8)

    def test_check_new_order(self):
        re = ofe.RiskEngine()
        re.set_budget(1000.0, 100000.0, 1.0)
        re.set_volatility(0.80)
        self.assertTrue(re.check_new_order(1.0, 100.0))

    def test_check_new_order_reject(self):
        re = ofe.RiskEngine()
        re.set_budget(5.0, 1000000.0, 1.0)
        re.set_volatility(0.80)
        self.assertFalse(re.check_new_order(100.0, 1000.0))

    def test_get_allowed_size(self):
        re = ofe.RiskEngine()
        re.set_budget(1000.0, 100000.0, 1.0)
        re.set_volatility(0.80)
        allowed = re.get_allowed_size(10.0, 100.0)
        self.assertAlmostEqual(allowed, 10.0, places=4)

    def test_compute_position_size(self):
        cfg = ofe.RiskConfig()
        cfg.target_risk_usd = 50.0
        cfg.sigma_target = 0.60
        re = ofe.RiskEngine(cfg)
        re.set_budget(1000.0, 100000.0, 1.0)
        re.set_volatility(0.60)
        qty = re.compute_position_size(100.0, 95.0)
        self.assertAlmostEqual(qty, 10.0, places=1)

    def test_on_fill(self):
        re = ofe.RiskEngine()
        re.set_budget(10000.0, 100000.0, 1.0)
        re.set_volatility(0.80)

        fill = ofe.FillEvent()
        fill.timestamp = 1000
        fill.side = ofe.OrderSide.BUY
        fill.price = 100.0
        fill.quantity = 5.0
        re.on_fill(fill)

        self.assertAlmostEqual(re.get_position_qty(), 5.0, places=6)
        self.assertGreater(re.get_consumed_es(), 0.0)

    def test_unrealized_pnl(self):
        re = ofe.RiskEngine()
        re.set_budget(10000.0, 100000.0, 1.0)

        fill = ofe.FillEvent()
        fill.timestamp = 1000
        fill.side = ofe.OrderSide.BUY
        fill.price = 100.0
        fill.quantity = 2.0
        re.on_fill(fill)

        re.on_price_update(110.0, 2000)
        self.assertAlmostEqual(re.get_unrealized_pnl(), 20.0, places=4)

    def test_reset(self):
        re = ofe.RiskEngine()
        re.set_budget(1000.0, 10000.0, 0.5)
        fill = ofe.FillEvent()
        fill.timestamp = 1000
        fill.side = ofe.OrderSide.BUY
        fill.price = 100.0
        fill.quantity = 5.0
        re.on_fill(fill)

        re.reset()
        self.assertAlmostEqual(re.get_consumed_es(), 0.0)
        self.assertAlmostEqual(re.get_position_qty(), 0.0)

    def test_snapshot_type(self):
        re = ofe.RiskEngine()
        re.set_budget(1000.0, 10000.0, 0.75)
        snap = re.get_snapshot()
        self.assertIsInstance(snap, ofe.RiskBudgetSnapshot)

    def test_determinism(self):
        """Same operations → same results."""
        def run():
            cfg = ofe.RiskConfig()
            cfg.target_risk_usd = 50.0
            cfg.sigma_target = 0.60
            re = ofe.RiskEngine(cfg)
            re.set_budget(1000.0, 50000.0, 0.8)
            re.set_volatility(0.75)

            fill = ofe.FillEvent()
            fill.timestamp = 1000
            fill.side = ofe.OrderSide.BUY
            fill.price = 100.0
            fill.quantity = 3.0
            re.on_fill(fill)
            re.on_price_update(105.0, 2000)
            return (re.get_consumed_es(), re.get_position_qty(),
                    re.compute_position_size(105.0, 100.0))

        r1 = run()
        r2 = run()
        self.assertEqual(r1, r2)


if __name__ == "__main__":
    unittest.main()
