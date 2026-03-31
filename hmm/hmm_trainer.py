"""
Baum-Welch (EM) trainer for the Ripple HMM inference backend (strategy.md §9.10).

Trains a Gaussian-emission HMM on sequences of RippleEvidence observations
labeled with RippleState ground truth from V1 backtest runs.

Model selection via BIC across K ∈ {3, 4, 5, 6}.
"""

import numpy as np
from typing import List, Tuple, Optional
from .hmm_model import HMMModel

OBS_DIM = 6  # evidence vector dimension


def _log_sum_exp(a: np.ndarray) -> float:
    mx = np.max(a)
    if mx < -1e20:
        return -1e30
    return mx + np.log(np.sum(np.exp(a - mx)))


def _log_gaussian(x: np.ndarray, mu: np.ndarray, var: np.ndarray) -> float:
    """Log probability of x under diagonal Gaussian(mu, diag(var))."""
    d = x.shape[0]
    diff = x - mu
    return -0.5 * (d * np.log(2.0 * np.pi) + np.sum(np.log(var)) +
                    np.sum(diff * diff / var))


class HMMTrainer:
    """Baum-Welch trainer with BIC model selection."""

    def __init__(self, max_iter: int = 100, tol: float = 1e-4,
                 var_floor: float = 1e-4):
        self.max_iter = max_iter
        self.tol = tol
        self.var_floor = var_floor

    def fit(self, observations: np.ndarray, K: int,
            state_map: Optional[List[int]] = None) -> HMMModel:
        """Train a K-state HMM on a single observation sequence.

        Args:
            observations: (T, OBS_DIM) array of evidence vectors.
            K: number of hidden states.
            state_map: mapping from HMM state index to RippleState int.

        Returns:
            Trained HMMModel.
        """
        T, D = observations.shape
        assert D == OBS_DIM

        if state_map is None:
            state_map = list(range(1, K + 1))

        # Initialize: uniform transition with self-bias, k-means-ish emission
        A = np.full((K, K), 0.3 / max(K - 1, 1))
        np.fill_diagonal(A, 0.7)
        A /= A.sum(axis=1, keepdims=True)

        pi = np.ones(K) / K

        # Spread means across quantiles of the data
        indices = np.linspace(0, T - 1, K + 2, dtype=int)[1:-1]
        mu = observations[indices].copy()
        var = np.full((K, D), np.var(observations, axis=0).clip(min=self.var_floor))

        log_A = np.log(A + 1e-300)
        log_pi = np.log(pi + 1e-300)

        prev_ll = -np.inf

        for iteration in range(self.max_iter):
            # E-step: forward-backward
            log_B = np.zeros((T, K))
            for t in range(T):
                for k in range(K):
                    log_B[t, k] = _log_gaussian(observations[t], mu[k], var[k])

            # Forward
            log_alpha = np.full((T, K), -1e30)
            for k in range(K):
                log_alpha[0, k] = log_pi[k] + log_B[0, k]

            for t in range(1, T):
                for j in range(K):
                    terms = log_alpha[t - 1] + log_A[:, j]
                    log_alpha[t, j] = _log_sum_exp(terms) + log_B[t, j]

            # Log-likelihood
            ll = _log_sum_exp(log_alpha[T - 1])

            # Backward
            log_beta = np.full((T, K), -1e30)
            log_beta[T - 1] = 0.0

            for t in range(T - 2, -1, -1):
                for i in range(K):
                    terms = log_A[i, :] + log_B[t + 1, :] + log_beta[t + 1, :]
                    log_beta[t, i] = _log_sum_exp(terms)

            # Posteriors γ(t,k) and ξ(t,i,j)
            log_gamma = log_alpha + log_beta
            for t in range(T):
                log_gamma[t] -= _log_sum_exp(log_gamma[t])
            gamma = np.exp(log_gamma)

            # M-step
            # Transition
            for i in range(K):
                for j in range(K):
                    terms = np.array([
                        log_alpha[t, i] + log_A[i, j] + log_B[t + 1, j] + log_beta[t + 1, j]
                        for t in range(T - 1)
                    ])
                    log_A[i, j] = _log_sum_exp(terms) if len(terms) > 0 else -1e30
                row_norm = _log_sum_exp(log_A[i])
                log_A[i] -= row_norm

            A = np.exp(log_A)
            A = np.clip(A, 1e-10, None)
            A /= A.sum(axis=1, keepdims=True)
            log_A = np.log(A)

            # Prior
            pi = gamma[0]
            pi = np.clip(pi, 1e-10, None)
            pi /= pi.sum()
            log_pi = np.log(pi)

            # Emission means and variances
            for k in range(K):
                gk = gamma[:, k]
                gk_sum = gk.sum()
                if gk_sum > 1e-10:
                    mu[k] = (gk[:, None] * observations).sum(axis=0) / gk_sum
                    diff = observations - mu[k]
                    var[k] = (gk[:, None] * diff * diff).sum(axis=0) / gk_sum
                    var[k] = np.clip(var[k], self.var_floor, None)

            if abs(ll - prev_ll) < self.tol:
                break
            prev_ll = ll

        # Build model
        model = HMMModel(K=K)
        model.transition = A
        model.means = mu
        model.variances = var
        model.state_map = state_map
        model.log_prior = log_pi
        model.log_likelihood = ll
        model.n_observations = T

        n_params = K * K + K * D + K * D  # A + mu + var
        model.bic = -2.0 * ll + n_params * np.log(T)

        return model

    def select_model(self, observations: np.ndarray,
                     k_range: List[int] = None) -> Tuple[HMMModel, List[HMMModel]]:
        """Train HMMs for each K and select the best by BIC.

        Args:
            observations: (T, OBS_DIM) evidence sequence.
            k_range: list of K values to try (default: [3, 4, 5, 6]).

        Returns:
            (best_model, all_models) sorted by BIC.
        """
        if k_range is None:
            k_range = [3, 4, 5, 6]

        models = []
        for K in k_range:
            state_map = list(range(1, K + 1))
            model = self.fit(observations, K, state_map)
            models.append(model)

        models.sort(key=lambda m: m.bic)
        return models[0], models
