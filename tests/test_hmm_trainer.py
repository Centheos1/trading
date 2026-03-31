"""
Phase 7: HMM trainer tests.

Validates Baum-Welch convergence, BIC model selection, model save/load
round-trip, and trained model structure.
"""

import sys
import os
import unittest
import tempfile
import json
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hmm.hmm_trainer import HMMTrainer, OBS_DIM
from hmm.hmm_model import HMMModel


def _generate_synthetic_data(n_samples=500, seed=42):
    """Generate synthetic evidence sequences from 3 known clusters."""
    rng = np.random.RandomState(seed)
    states = []
    obs = []

    cluster_means = [
        [0.1, 0.1, 0.1, 0.1, 0.6, 0.6],  # stabilization
        [0.8, 0.1, 0.1, 0.1, 0.1, 0.1],  # absorption
        [0.1, 0.8, 0.1, 0.1, 0.1, 0.1],  # exhaustion
    ]
    cluster_stds = 0.08

    state = 0
    transition = np.array([
        [0.85, 0.10, 0.05],
        [0.10, 0.80, 0.10],
        [0.10, 0.10, 0.80],
    ])

    for _ in range(n_samples):
        states.append(state)
        mean = cluster_means[state]
        x = rng.normal(mean, cluster_stds, size=OBS_DIM)
        x = np.clip(x, 0.0, 1.0)
        obs.append(x)
        state = rng.choice(3, p=transition[state])

    return np.array(obs), states


class TestHMMTrainer(unittest.TestCase):

    def test_fit_basic(self):
        obs, _ = _generate_synthetic_data(200)
        trainer = HMMTrainer(max_iter=30)
        model = trainer.fit(obs, K=3)
        self.assertEqual(model.K, 3)
        self.assertEqual(model.transition.shape, (3, 3))
        self.assertEqual(model.means.shape, (3, OBS_DIM))
        self.assertEqual(model.variances.shape, (3, OBS_DIM))
        self.assertEqual(len(model.state_map), 3)

    def test_transition_rows_sum_to_one(self):
        obs, _ = _generate_synthetic_data(200)
        trainer = HMMTrainer(max_iter=30)
        model = trainer.fit(obs, K=3)
        row_sums = model.transition.sum(axis=1)
        np.testing.assert_allclose(row_sums, 1.0, atol=1e-6)

    def test_transition_non_negative(self):
        obs, _ = _generate_synthetic_data(200)
        trainer = HMMTrainer(max_iter=30)
        model = trainer.fit(obs, K=4)
        self.assertTrue(np.all(model.transition >= 0))

    def test_variances_positive(self):
        obs, _ = _generate_synthetic_data(200)
        trainer = HMMTrainer(max_iter=30)
        model = trainer.fit(obs, K=3)
        self.assertTrue(np.all(model.variances > 0))

    def test_log_likelihood_improves(self):
        obs, _ = _generate_synthetic_data(300)
        trainer1 = HMMTrainer(max_iter=1)
        model1 = trainer1.fit(obs, K=3)
        trainer50 = HMMTrainer(max_iter=50)
        model50 = trainer50.fit(obs, K=3)
        self.assertGreater(model50.log_likelihood, model1.log_likelihood)

    def test_bic_computed(self):
        obs, _ = _generate_synthetic_data(200)
        trainer = HMMTrainer(max_iter=20)
        model = trainer.fit(obs, K=3)
        self.assertNotEqual(model.bic, 0.0)
        self.assertEqual(model.n_observations, 200)

    def test_model_selection(self):
        obs, _ = _generate_synthetic_data(500)
        trainer = HMMTrainer(max_iter=30)
        best, all_models = trainer.select_model(obs, k_range=[3, 4, 5])
        self.assertEqual(len(all_models), 3)
        self.assertLessEqual(best.bic, all_models[-1].bic)
        self.assertIn(best.K, [3, 4, 5])

    def test_deterministic_training(self):
        obs, _ = _generate_synthetic_data(200, seed=123)
        trainer = HMMTrainer(max_iter=30)
        m1 = trainer.fit(obs, K=3)
        m2 = trainer.fit(obs, K=3)
        np.testing.assert_array_equal(m1.transition, m2.transition)
        np.testing.assert_array_equal(m1.means, m2.means)
        self.assertEqual(m1.log_likelihood, m2.log_likelihood)


class TestHMMModel(unittest.TestCase):

    def test_json_round_trip(self):
        obs, _ = _generate_synthetic_data(100)
        trainer = HMMTrainer(max_iter=10)
        model = trainer.fit(obs, K=3)
        json_str = model.to_json()
        loaded = HMMModel.from_json(json_str)
        self.assertEqual(loaded.K, model.K)
        np.testing.assert_allclose(loaded.transition, model.transition, atol=1e-10)
        np.testing.assert_allclose(loaded.means, model.means, atol=1e-10)
        np.testing.assert_allclose(loaded.variances, model.variances, atol=1e-10)
        self.assertEqual(loaded.state_map, model.state_map)

    def test_save_load_file(self):
        obs, _ = _generate_synthetic_data(100)
        trainer = HMMTrainer(max_iter=10)
        model = trainer.fit(obs, K=4)

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name
        try:
            model.save(path)
            loaded = HMMModel.load(path)
            self.assertEqual(loaded.K, 4)
            np.testing.assert_allclose(loaded.transition, model.transition, atol=1e-10)
        finally:
            os.unlink(path)

    def test_json_valid_for_cpp(self):
        obs, _ = _generate_synthetic_data(100)
        trainer = HMMTrainer(max_iter=10)
        model = trainer.fit(obs, K=3)
        d = json.loads(model.to_json())
        self.assertIn("K", d)
        self.assertIn("transition", d)
        self.assertIn("means", d)
        self.assertIn("variances", d)
        self.assertIn("state_map", d)
        self.assertEqual(len(d["transition"]), 3)
        self.assertEqual(len(d["means"]), 3)
        self.assertEqual(len(d["variances"]), 3)
        for row in d["transition"]:
            self.assertEqual(len(row), 3)
        for row in d["means"]:
            self.assertEqual(len(row), OBS_DIM)


if __name__ == "__main__":
    unittest.main()
