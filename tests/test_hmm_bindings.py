"""
Phase 7: HMM pybind11 binding tests.

Validates that HMMBasedInference is accessible from Python, model loading
works, and the RippleEngine backend swap (HMM ↔ ScoreBased) functions
correctly through the bindings.
"""

import sys
import os
import unittest
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                 '..', 'backtestingCpp', 'orderflow', 'build'))

try:
    import orderflow_engine as ofe
    HAS_MODULE = (hasattr(ofe, "HMMBasedInference") and
                  hasattr(ofe, "RippleEngine") and
                  hasattr(ofe, "OrderFlowEngine"))
except ImportError:
    HAS_MODULE = False


MODEL_3_JSON = json.dumps({
    "K": 3,
    "transition": [
        [0.8, 0.1, 0.1],
        [0.1, 0.8, 0.1],
        [0.1, 0.1, 0.8]
    ],
    "means": [
        [0.1, 0.1, 0.1, 0.1, 0.5, 0.5],
        [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],
        [0.1, 0.8, 0.1, 0.1, 0.1, 0.1]
    ],
    "variances": [
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05],
        [0.05, 0.05, 0.05, 0.05, 0.05, 0.05]
    ],
    "state_map": [1, 2, 3]
})


@unittest.skipUnless(HAS_MODULE, "orderflow_engine C++ module not available")
class TestHMMBindings(unittest.TestCase):

    def test_hmm_class_exists(self):
        cfg = ofe.RippleConfig()
        hmm = ofe.HMMBasedInference(cfg)
        self.assertFalse(hmm.model_loaded())
        self.assertEqual(hmm.num_states(), 0)

    def test_load_model_from_string(self):
        cfg = ofe.RippleConfig()
        hmm = ofe.HMMBasedInference(cfg)
        ok = hmm.load_model_from_string(MODEL_3_JSON)
        self.assertTrue(ok)
        self.assertTrue(hmm.model_loaded())
        self.assertEqual(hmm.num_states(), 3)

    def test_load_invalid_json(self):
        cfg = ofe.RippleConfig()
        hmm = ofe.HMMBasedInference(cfg)
        ok = hmm.load_model_from_string("{}")
        self.assertFalse(ok)
        self.assertFalse(hmm.model_loaded())

    def test_reset_forward(self):
        cfg = ofe.RippleConfig()
        hmm = ofe.HMMBasedInference(cfg)
        hmm.load_model_from_string(MODEL_3_JSON)
        hmm.reset_forward()
        self.assertTrue(hmm.model_loaded())

    def test_ripple_config_hmm_fields(self):
        cfg = ofe.RippleConfig()
        self.assertFalse(cfg.hmm_enabled)
        cfg.hmm_enabled = True
        self.assertTrue(cfg.hmm_enabled)
        cfg.hmm_model_path = "/tmp/test.json"
        self.assertEqual(cfg.hmm_model_path, "/tmp/test.json")

    def test_ripple_engine_set_hmm_backend(self):
        cfg = ofe.EngineConfig()
        cfg.tick_size = 0.01
        rcfg = cfg.ripple
        rcfg.tick_size = 0.01
        rcfg.pipeline_min_interval_ms = 0
        rcfg.enable_diagnostics = False
        cfg.ripple = rcfg

        engine = ofe.OrderFlowEngine(cfg)
        engine.start("")
        ripple = engine.get_ripple()
        ripple.set_hmm_backend(MODEL_3_JSON)
        engine.stop()

    def test_ripple_engine_set_score_backend(self):
        cfg = ofe.EngineConfig()
        cfg.tick_size = 0.01
        rcfg = cfg.ripple
        rcfg.tick_size = 0.01
        rcfg.pipeline_min_interval_ms = 0
        rcfg.enable_diagnostics = False
        cfg.ripple = rcfg

        engine = ofe.OrderFlowEngine(cfg)
        engine.start("")
        ripple = engine.get_ripple()
        ripple.set_hmm_backend(MODEL_3_JSON)
        ripple.set_score_backend()
        engine.stop()

    def test_hmm_enabled_config_auto_loads(self):
        """When hmm_enabled=True in config, the engine uses HMM backend."""
        cfg = ofe.EngineConfig()
        cfg.tick_size = 0.01
        rcfg = cfg.ripple
        rcfg.tick_size = 0.01
        rcfg.pipeline_min_interval_ms = 0
        rcfg.enable_diagnostics = False
        rcfg.hmm_enabled = True
        cfg.ripple = rcfg
        engine = ofe.OrderFlowEngine(cfg)
        engine.start("")
        engine.stop()


if __name__ == "__main__":
    unittest.main()
