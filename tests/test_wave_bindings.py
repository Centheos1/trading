"""Tests for pybind11 bindings of Wave-related RippleEngine methods (Phase 5).

These tests require the C++ module compiled against the venv Python.
When running with the system Python and the module is unavailable, tests
are gracefully skipped.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    build_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backtestingCpp", "orderflow", "build",
    )
    sys.path.insert(0, build_dir)
    import orderflow_engine as ofe
    HAS_MODULE = hasattr(ofe, "RippleEngine") and hasattr(ofe, "WaveSnapshot")
except ImportError:
    HAS_MODULE = False


@unittest.skipUnless(HAS_MODULE, "orderflow_engine C++ module not available")
class TestWaveBindings(unittest.TestCase):
    def test_wave_snapshot_class_exists(self):
        self.assertTrue(hasattr(ofe, "WaveSnapshot"))

    def test_wave_snapshot_fields(self):
        ws = ofe.WaveSnapshot()
        self.assertEqual(ws.regime, ofe.WaveRegime.NEUTRAL)
        self.assertAlmostEqual(ws.trend_efficiency, 0.5)
        self.assertAlmostEqual(ws.dispersion, 0.0)
        self.assertAlmostEqual(ws.absorption_ratio, 0.5)
        self.assertIsNotNone(ws.permissions)

    def test_permission_set_binding(self):
        ps = ofe.PermissionSet()
        self.assertEqual(ps.long_bounce, ofe.PermissionLevel.FULL)
        self.assertEqual(ps.short_bounce, ofe.PermissionLevel.FULL)
        self.assertTrue(ps.is_allowed(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG))
        self.assertAlmostEqual(ps.size_fraction(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG), 1.0)

    def test_permission_set_disabled(self):
        ps = ofe.PermissionSet()
        ps.long_bounce = ofe.PermissionLevel.DISABLED
        self.assertFalse(ps.is_allowed(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG))
        self.assertAlmostEqual(ps.size_fraction(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG), 0.0)

    def test_permission_set_reduced(self):
        ps = ofe.PermissionSet()
        ps.long_bounce = ofe.PermissionLevel.REDUCED
        ps.reduced_size_fraction = 0.4
        self.assertTrue(ps.is_allowed(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG))
        self.assertAlmostEqual(ps.size_fraction(ofe.TradeArchetype.BOUNCE, ofe.TradeSide.LONG), 0.4)

    def test_default_wave_snapshot(self):
        ws = ofe.DefaultWaveSnapshot.make()
        self.assertEqual(ws.regime, ofe.WaveRegime.NEUTRAL)
        self.assertAlmostEqual(ws.trend_efficiency, 0.5)

    def test_wave_config_fields(self):
        wc = ofe.WaveConfig()
        self.assertEqual(wc.update_interval_ms, 5000)
        self.assertAlmostEqual(wc.eta_mr_threshold, 0.3)
        self.assertAlmostEqual(wc.eta_bo_threshold, 0.7)
        self.assertAlmostEqual(wc.ar_critical, 0.85)
        self.assertAlmostEqual(wc.ar_recover, 0.70)
        self.assertAlmostEqual(wc.reduced_size_fraction, 0.5)

    def test_ripple_engine_set_wave_snapshot(self):
        """RippleEngine.set_wave_snapshot() / wave_snapshot() / has_wave_snapshot()."""
        cfg = ofe.EngineConfig()
        engine = ofe.OrderFlowEngine(cfg)
        re = engine.get_ripple()
        self.assertFalse(re.has_wave_snapshot())

        ws = ofe.WaveSnapshot()
        ws.regime = ofe.WaveRegime.BREAKOUT
        ws.trend_efficiency = 0.8
        ws.permissions.long_bounce = ofe.PermissionLevel.DISABLED
        re.set_wave_snapshot(ws)

        self.assertTrue(re.has_wave_snapshot())
        s = re.wave_snapshot()
        self.assertEqual(s.regime, ofe.WaveRegime.BREAKOUT)
        self.assertAlmostEqual(s.trend_efficiency, 0.8)
        self.assertEqual(s.permissions.long_bounce, ofe.PermissionLevel.DISABLED)

    def test_ripple_engine_wave_reset(self):
        cfg = ofe.EngineConfig()
        engine = ofe.OrderFlowEngine(cfg)
        re = engine.get_ripple()
        ws = ofe.WaveSnapshot()
        ws.regime = ofe.WaveRegime.BREAKDOWN
        re.set_wave_snapshot(ws)
        self.assertTrue(re.has_wave_snapshot())
        re.reset()
        self.assertFalse(re.has_wave_snapshot())
        self.assertEqual(re.wave_snapshot().regime, ofe.WaveRegime.NEUTRAL)

    def test_wave_regime_enum(self):
        self.assertEqual(ofe.WaveRegime.MEAN_REVERSION.name, "MEAN_REVERSION")
        self.assertEqual(ofe.WaveRegime.BREAKOUT.name, "BREAKOUT")
        self.assertEqual(ofe.WaveRegime.BREAKDOWN.name, "BREAKDOWN")
        self.assertEqual(ofe.WaveRegime.NEUTRAL.name, "NEUTRAL")

    def test_permission_level_enum(self):
        self.assertEqual(ofe.PermissionLevel.FULL.name, "FULL")
        self.assertEqual(ofe.PermissionLevel.REDUCED.name, "REDUCED")
        self.assertEqual(ofe.PermissionLevel.DISABLED.name, "DISABLED")


if __name__ == "__main__":
    unittest.main()
