"""Mean-variance frontier used by the Oanda research notebook.

The traded portfolio is the fully invested frontier portfolio

    phi = phi_a + lambda * omega

where phi_a is the minimum-variance portfolio and omega is the
self-financing direction. omega sums to 0 and has unit variance. It is
not a funded portfolio. M and Sigma are annualized by multiplying the
daily mean and the daily covariance by the notebook factor
n_obs / (calendar_days / 365).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_SUM_ATOL = 1e-8
_VAR_ATOL = 1e-6
_COND_MAX = 1e12


@dataclass(frozen=True)
class FrontierConfig:
    """Controls shared by research selection and the daily rebalance."""

    target_return: float = 0.20
    lookback: int = 756
    min_overlap: int = 756
    gross_cap: float = 2.0
    weight_cap: float = 0.30
    portfolio_sizes: tuple[int, ...] = (10, 15, 20)
    score_months: int = 12
    min_trade_units: int = 1
    research_nav: float = 100_000.0
    margin_rates: dict[str, float] | None = None


@dataclass(frozen=True)
class FrontierResult:
    weights: pd.Series
    target_requested: float
    target_used: float
    method: str
    gross: float
    note: str


def annualization_factor(index: pd.DatetimeIndex) -> float:
    """Match the course notebook: observations per year of calendar span."""
    if len(index) < 2:
        raise ValueError("annualization needs at least two dates")
    calendar_days = (index[-1] - index[0]).days
    if calendar_days <= 0:
        raise ValueError(f"calendar span is {calendar_days} days")
    return len(index) / (calendar_days / 365.0)


def ledoit_wolf_covariance(returns: pd.DataFrame) -> tuple[np.ndarray, float]:
    """Shrink the daily covariance toward a scaled identity.

    The intensity uses the 1/n covariance. The result is scaled by
    n/(n-1), so zero shrinkage matches ``DataFrame.cov`` (ddof=1).
    """
    frame = returns.dropna(how="any")
    values = np.asarray(frame.to_numpy(), dtype=float)
    n, p = values.shape
    if n < 2 or p < 1:
        raise ValueError(f"Ledoit-Wolf needs at least 2 rows and 1 column, got {values.shape}")
    centered = values - values.mean(axis=0, keepdims=True)
    sample_mle = (centered.T @ centered) / n
    mu = float(np.trace(sample_mle) / p)
    delta = sample_mle - mu * np.eye(p)
    delta2 = float(np.sum(delta * delta))
    beta2 = 0.0
    for row in centered:
        gap = np.outer(row, row) - sample_mle
        beta2 += float(np.sum(gap * gap))
    beta2 /= n * n
    beta2 = min(beta2, delta2)
    alpha = 1.0 if delta2 <= 0.0 else beta2 / delta2
    alpha = float(np.clip(alpha, 0.0, 1.0))
    shrunk_mle = (1.0 - alpha) * sample_mle + alpha * mu * np.eye(p)
    unbiased = shrunk_mle * (n / (n - 1))
    unbiased = 0.5 * (unbiased + unbiased.T)
    return unbiased, alpha


def one_factor_covariance(returns: pd.DataFrame) -> np.ndarray:
    """Rank-one factor covariance plus a diagonal residual.

    This is a research comparison. It is not inverted for traded weights.
    """
    frame = returns.dropna(how="any")
    if len(frame) < 2:
        raise ValueError("one-factor covariance needs at least two rows")
    sample = np.cov(frame.to_numpy(), rowvar=False, ddof=1)
    if sample.ndim == 0:
        sample = np.array([[float(sample)]])
    values, vectors = np.linalg.eigh(sample)
    slot = int(np.argmax(values))
    loading = vectors[:, slot] * np.sqrt(max(float(values[slot]), 0.0))
    systematic = np.outer(loading, loading)
    residual = np.clip(np.diag(sample - systematic), 0.0, None)
    cov = systematic + np.diag(residual)
    return 0.5 * (cov + cov.T)


def shrink_correlation(
    correlation: np.ndarray,
    pair_counts: np.ndarray,
) -> np.ndarray:
    """Shrink pairwise correlations toward zero, then project to a correlation matrix.

    ``pair_counts`` is the number of overlapping returns behind each entry.
    The diagonal is ignored. Missing correlations must already have been removed.
    """
    corr = np.array(correlation, dtype=float, copy=True)
    counts = np.asarray(pair_counts, dtype=float)
    if corr.ndim != 2 or corr.shape[0] != corr.shape[1]:
        raise ValueError("correlation must be square")
    if corr.shape != counts.shape:
        raise ValueError("pair_counts shape does not match correlation")
    if not np.isfinite(corr).all():
        raise ValueError("correlation contains a non-finite value")
    p = corr.shape[0]
    numerator = 0.0
    denominator = 0.0
    for i in range(p):
        for j in range(i + 1, p):
            rho = float(corr[i, j])
            n_ij = float(counts[i, j])
            variance = (1.0 - rho * rho) ** 2 / max(n_ij - 1.0, 1.0)
            numerator += variance
            denominator += rho * rho
    alpha = 1.0 if denominator <= 0.0 else float(np.clip(numerator / denominator, 0.0, 1.0))
    shrunk = (1.0 - alpha) * corr
    np.fill_diagonal(shrunk, 1.0)
    shrunk = 0.5 * (shrunk + shrunk.T)
    return _nearest_correlation(shrunk)


def _nearest_correlation(matrix: np.ndarray, *, iterations: int = 64) -> np.ndarray:
    """Project to a symmetric matrix with unit diagonal and non-negative eigenvalues."""
    current = 0.5 * (matrix + matrix.T)
    np.fill_diagonal(current, 1.0)
    for _ in range(iterations):
        values, vectors = np.linalg.eigh(current)
        values = np.clip(values, 0.0, None)
        positive = (vectors * values) @ vectors.T
        current = 0.5 * (positive + positive.T)
        scale = np.sqrt(np.clip(np.diag(current), 1e-18, None))
        current = current / np.outer(scale, scale)
        np.fill_diagonal(current, 1.0)
    values = np.linalg.eigvalsh(current)
    if values[0] < -1e-8:
        raise RuntimeError(f"correlation projection left a negative eigenvalue {values[0]}")
    return current


def _invert(sigma: np.ndarray) -> np.ndarray:
    cond = float(np.linalg.cond(sigma))
    if not np.isfinite(cond) or cond > _COND_MAX:
        raise ValueError(f"covariance is not invertible (condition number {cond})")
    return np.linalg.inv(sigma)


def _minimum_variance(sigma_inv: np.ndarray) -> tuple[np.ndarray, float, float]:
    ones = np.ones(sigma_inv.shape[0])
    a = float(ones @ sigma_inv @ ones)
    if not np.isfinite(a) or a <= 0.0:
        raise ValueError(f"minimum-variance scalar a={a} is not positive")
    phi_a = (sigma_inv @ ones) / a
    return phi_a, a, float(ones @ sigma_inv @ np.ones(sigma_inv.shape[0]))


def _self_financing(mean: np.ndarray, sigma: np.ndarray, sigma_inv: np.ndarray) -> np.ndarray:
    ones = np.ones(len(mean))
    a = float(ones @ sigma_inv @ ones)
    b = float(mean @ sigma_inv @ ones)
    excess = mean - (b / a) * ones
    scale_sq = float(excess @ sigma_inv @ excess)
    if not np.isfinite(scale_sq) or scale_sq <= 0.0:
        raise ValueError("self-financing direction is degenerate")
    omega = (sigma_inv @ excess) / np.sqrt(scale_sq)
    if not np.isclose(float(omega @ ones), 0.0, atol=_SUM_ATOL):
        raise RuntimeError(f"self-financing weights sum to {float(omega @ ones)}, expected 0")
    variance = float(omega @ sigma @ omega)
    if not np.isclose(variance, 1.0, atol=_VAR_ATOL):
        raise RuntimeError(f"self-financing variance is {variance}, expected 1")
    return omega


def _within_caps(weights: np.ndarray, gross_cap: float, weight_cap: float) -> bool:
    return bool(
        np.isclose(weights.sum(), 1.0, atol=1e-6)
        and np.max(np.abs(weights)) <= weight_cap + 1e-8
        and np.sum(np.abs(weights)) <= gross_cap + 1e-8
    )


def _long_short(weights: np.ndarray) -> np.ndarray:
    """Split signed weights into long and short legs. Overlap is removed."""
    long = np.maximum(weights, 0.0)
    short = np.maximum(-weights, 0.0)
    return np.concatenate([long, short])


def _signed(exposure: np.ndarray, n: int) -> np.ndarray:
    return exposure[:n] - exposure[n:]


def _optimize(
    mean: np.ndarray,
    sigma: np.ndarray,
    *,
    target: float | None,
    gross_cap: float,
    weight_cap: float,
    maximize_return: bool = False,
    minimize_return: bool = False,
    start: np.ndarray | None = None,
) -> np.ndarray:
    try:
        from scipy.optimize import minimize
    except ImportError as exc:
        raise ImportError(
            "constrained frontier weights require scipy. "
            "Install notebooks/requirements.txt."
        ) from exc

    n = len(mean)
    if weight_cap * n < 1.0 - 1e-12:
        raise ValueError(
            f"weight cap {weight_cap} cannot fund a fully invested portfolio of {n} assets"
        )
    if gross_cap < 1.0:
        raise ValueError(f"gross cap {gross_cap} is below full investment")

    ones = np.ones(n)

    # A simultaneous long and short of one name does not change the signed
    # weight. A small gross penalty makes that direction costly so SLSQP
    # does not stall on it.
    overlap_penalty = 1e-8

    def objective(exposure: np.ndarray) -> float:
        weights = _signed(exposure, n)
        penalty = overlap_penalty * float(np.sum(exposure))
        if maximize_return:
            return float(-weights @ mean) + penalty
        if minimize_return:
            return float(weights @ mean) + penalty
        return float(weights @ sigma @ weights) + penalty

    def objective_jac(exposure: np.ndarray) -> np.ndarray:
        weights = _signed(exposure, n)
        if maximize_return:
            gradient = -mean
        elif minimize_return:
            gradient = mean
        else:
            gradient = 2.0 * (sigma @ weights)
        return np.concatenate(
            [gradient + overlap_penalty, -gradient + overlap_penalty]
        )

    constraints: list[dict] = [
        {
            "type": "eq",
            "fun": lambda exposure: float(np.sum(_signed(exposure, n)) - 1.0),
            "jac": lambda exposure: np.concatenate([ones, -ones]),
        },
        {
            "type": "ineq",
            "fun": lambda exposure: float(gross_cap - np.sum(exposure)),
            "jac": lambda exposure: -np.ones(2 * n),
        },
    ]
    for index in range(n):
        constraints.append(
            {
                "type": "ineq",
                "fun": lambda exposure, index=index: float(
                    weight_cap - exposure[index] - exposure[n + index]
                ),
                "jac": lambda exposure, index=index: np.concatenate(
                    [
                        np.array([-1.0 if i == index else 0.0 for i in range(n)]),
                        np.array([-1.0 if i == index else 0.0 for i in range(n)]),
                    ]
                ),
            }
        )
    if target is not None:
        constraints.append(
            {
                "type": "eq",
                "fun": lambda exposure, level=target: float(_signed(exposure, n) @ mean - level),
                "jac": lambda exposure: np.concatenate([mean, -mean]),
            }
        )
    if start is None:
        guess = _long_short(np.full(n, 1.0 / n))
    else:
        seeded = np.asarray(start, dtype=float)
        seeded = np.clip(seeded, -weight_cap, weight_cap)
        if not np.isfinite(seeded).all() or abs(float(seeded.sum())) < 1e-12:
            seeded = np.full(n, 1.0 / n)
        guess = _long_short(seeded)
    result = minimize(
        objective,
        guess,
        method="SLSQP",
        jac=objective_jac,
        bounds=[(0.0, weight_cap)] * (2 * n),
        constraints=constraints,
        options={"maxiter": 1000, "ftol": 1e-10},
    )
    if not result.success:
        raise ValueError(f"portfolio solver failed: {result.message}")
    exposure = np.asarray(result.x, dtype=float)
    overlap = np.minimum(exposure[:n], exposure[n:])
    exposure[:n] -= overlap
    exposure[n:] -= overlap
    weights = _signed(exposure, n)
    if not _within_caps(weights, gross_cap, weight_cap):
        raise ValueError(
            f"solver returned weights outside the caps (sum={weights.sum()}, "
            f"gross={np.sum(np.abs(weights))}, max={np.max(np.abs(weights))})"
        )
    if target is not None and not np.isclose(float(weights @ mean), target, atol=1e-5):
        raise ValueError(
            f"solver missed the target return: got {float(weights @ mean)} requested {target}"
        )
    return weights


def frontier_portfolio(
    mean: pd.Series | np.ndarray,
    covariance: pd.DataFrame | np.ndarray,
    config: FrontierConfig,
) -> FrontierResult:
    """Fully invested frontier portfolio at ``config.target_return``.

    Uses the closed form when it respects the gross and per-asset caps.
    Otherwise solves the same problem with those inequalities. A target
    above the highest feasible return is lowered. A target below the
    constrained minimum-variance return is raised to that return. Either
    adjustment is recorded on ``note`` and ``target_used``.
    """
    if isinstance(mean, pd.Series):
        names = list(mean.index)
        mu = mean.to_numpy(dtype=float)
    else:
        mu = np.asarray(mean, dtype=float)
        names = [str(i) for i in range(len(mu))]
    sigma = np.asarray(covariance, dtype=float)
    if sigma.shape != (len(mu), len(mu)):
        raise ValueError(f"covariance shape {sigma.shape} does not match {len(mu)} assets")
    if config.gross_cap < 1.0 or config.weight_cap <= 0.0:
        raise ValueError("caps must allow a fully invested portfolio")

    sigma_inv = _invert(sigma)
    phi_a, _, _ = _minimum_variance(sigma_inv)
    omega = _self_financing(mu, sigma, sigma_inv)
    m_a = float(phi_a @ mu)
    m_omega = float(omega @ mu)
    if abs(m_omega) <= 1e-12:
        raise ValueError("self-financing portfolio has no return spread")
    lam = (config.target_return - m_a) / m_omega
    closed = phi_a + lam * omega
    if _within_caps(closed, config.gross_cap, config.weight_cap) and np.isclose(
        float(closed @ mu), config.target_return, atol=1e-6
    ):
        weights = pd.Series(closed, index=names, name="weight")
        return FrontierResult(
            weights=weights,
            target_requested=config.target_return,
            target_used=config.target_return,
            method="closed_form",
            gross=float(np.sum(np.abs(closed))),
            note="",
        )

    logger.info(
        "Closed-form frontier breaches the caps (gross=%.3f max=%.3f); resolving with constraints",
        float(np.sum(np.abs(closed))),
        float(np.max(np.abs(closed))),
    )
    high = _optimize(
        mu, sigma, target=None, gross_cap=config.gross_cap,
        weight_cap=config.weight_cap, maximize_return=True, start=closed,
    )
    low = _optimize(
        mu, sigma, target=None, gross_cap=config.gross_cap,
        weight_cap=config.weight_cap, minimize_return=True, start=phi_a,
    )
    r_max = float(high @ mu)
    r_min = float(low @ mu)
    used = float(config.target_return)
    note = ""
    if used > r_max + 1e-6:
        used = r_max
        note = f"target lowered from {config.target_return:.6f} to {used:.6f}"
        logger.warning(note)
    elif used < r_min - 1e-6:
        used = r_min
        note = (
            f"target raised from {config.target_return:.6f} to the constrained "
            f"minimum-variance return {used:.6f}"
        )
        logger.warning(note)
    span = r_max - r_min
    if span > 1e-12:
        blend = float(np.clip((used - r_min) / span, 0.0, 1.0))
        seeded = (1.0 - blend) * low + blend * high
    else:
        seeded = low
    solved = _optimize(
        mu, sigma, target=used, gross_cap=config.gross_cap,
        weight_cap=config.weight_cap, start=seeded,
    )
    weights = pd.Series(solved, index=names, name="weight")
    return FrontierResult(
        weights=weights,
        target_requested=config.target_return,
        target_used=used,
        method="constrained",
        gross=float(np.sum(np.abs(solved))),
        note=note,
    )


def annualize_moments(
    returns: pd.DataFrame,
) -> tuple[pd.Series, pd.DataFrame, float, float]:
    """Annualized sample mean and Ledoit-Wolf covariance of a complete return window."""
    frame = returns.dropna(how="any")
    if len(frame) < 2:
        raise ValueError("moment window has fewer than two complete rows")
    factor = annualization_factor(pd.DatetimeIndex(frame.index))
    daily_cov, alpha = ledoit_wolf_covariance(frame)
    mean = frame.mean(axis=0) * factor
    covariance = pd.DataFrame(daily_cov * factor, index=frame.columns, columns=frame.columns)
    return mean, covariance, factor, alpha
