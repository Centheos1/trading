"""Gaussian and Student-t density models for a declared log-return horizon."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

_MIN_FIT = 30


@dataclass(frozen=True)
class DistributionScore:
    """Calibration evidence for one frozen model on one sample."""

    model: str
    observations: int
    mean_log_score: float
    pit_ks_statistic: float
    pit_ks_pvalue: float
    var_alpha: float
    var_level: float
    var_exceedances: int
    var_exceedance_rate: float
    var_pvalue: float
    expected_shortfall: float
    realised_shortfall: float | None
    pit_qq_theoretical: tuple[float, ...]
    pit_qq_empirical: tuple[float, ...]


@dataclass(frozen=True)
class GaussianModel:
    """Gaussian MLE. Variance uses the 1/n estimator."""

    mean: float
    variance: float

    def log_density(self, sample: np.ndarray) -> np.ndarray:
        deviation = (np.asarray(sample, dtype=float) - self.mean) ** 2 / self.variance
        return -0.5 * (math.log(2.0 * math.pi) + math.log(self.variance) + deviation)

    def cdf(self, sample: np.ndarray) -> np.ndarray:
        from scipy.stats import norm

        return norm.cdf(np.asarray(sample, dtype=float), loc=self.mean, scale=math.sqrt(self.variance))

    def value_at_risk(self, alpha: float) -> float:
        from scipy.stats import norm

        return float(self.mean + math.sqrt(self.variance) * norm.ppf(alpha))

    def expected_shortfall(self, alpha: float) -> float:
        from scipy.stats import norm

        quantile = norm.ppf(alpha)
        return float(self.mean - math.sqrt(self.variance) * norm.pdf(quantile) / alpha)


@dataclass(frozen=True)
class StudentTModel:
    """Location-scale Student-t with degrees of freedom above two."""

    df: float
    loc: float
    scale: float

    def log_density(self, sample: np.ndarray) -> np.ndarray:
        from scipy.stats import t

        return t.logpdf(np.asarray(sample, dtype=float), self.df, loc=self.loc, scale=self.scale)

    def cdf(self, sample: np.ndarray) -> np.ndarray:
        from scipy.stats import t

        return t.cdf(np.asarray(sample, dtype=float), self.df, loc=self.loc, scale=self.scale)

    def value_at_risk(self, alpha: float) -> float:
        from scipy.stats import t

        return float(t.ppf(alpha, self.df, loc=self.loc, scale=self.scale))

    def expected_shortfall(self, alpha: float) -> float:
        from scipy.stats import t

        quantile = float(t.ppf(alpha, self.df))
        density = float(t.pdf(quantile, self.df))
        standardised = -density / alpha * (self.df + quantile * quantile) / (self.df - 1.0)
        return float(self.loc + self.scale * standardised)


def fit_gaussian(sample: np.ndarray) -> GaussianModel:
    """Fit a Gaussian by maximum likelihood."""
    values = _clean_sample(sample, "Gaussian")
    mean = float(values.mean())
    variance = float(np.mean((values - mean) ** 2))
    if variance <= 0.0 or not math.isfinite(variance):
        raise ValueError(f"Gaussian variance is not positive: {variance}")
    return GaussianModel(mean, variance)


def fit_student_t(sample: np.ndarray) -> StudentTModel:
    """Fit a Student-t by method of moments.

    Full maximum likelihood does not scale to several years of one-minute
    labels. The H1 estimator is therefore pre-registered as the moment
    match: mean, variance, and unbiased excess kurtosis. Excess kurtosis
    at or below zero is outside the Student-t family and raises.
    """
    values = _clean_sample(sample, "Student-t")
    from scipy.stats import kurtosis

    loc = float(values.mean())
    variance = float(np.mean((values - loc) ** 2))
    excess = float(kurtosis(values, fisher=True, bias=False))
    if not math.isfinite(excess) or excess <= 0.0 or variance <= 0.0:
        raise RuntimeError(
            f"Student-t moments are not usable: variance={variance} excess_kurtosis={excess}"
        )
    df = 4.0 + 6.0 / excess
    if df <= 4.0:
        raise RuntimeError(f"Student-t degrees of freedom are not above 4: {df}")
    scale = math.sqrt(variance * (df - 2.0) / df)
    if not math.isfinite(scale) or scale <= 0.0:
        raise RuntimeError(f"Student-t scale is not positive: {scale}")
    return StudentTModel(df, loc, scale)


def score_distribution(model: GaussianModel | StudentTModel, sample: np.ndarray, alpha: float = 0.05) -> DistributionScore:
    """Score a frozen model. This does not refit it or make a retain decision."""
    if not 0.0 < alpha < 0.5:
        raise ValueError(f"VaR alpha must be in (0, 0.5), got {alpha}")
    values = _clean_sample(sample, model.__class__.__name__)
    log_score = np.asarray(model.log_density(values), dtype=float)
    if not np.isfinite(log_score).all():
        raise RuntimeError(f"{model.__class__.__name__} produced a non-finite log density")
    pit = np.asarray(model.cdf(values), dtype=float)
    if not np.isfinite(pit).all() or (pit < 0).any() or (pit > 1).any():
        raise RuntimeError(f"{model.__class__.__name__} produced a PIT outside [0, 1]")
    from scipy.stats import binomtest, kstest

    theoretical, empirical = uniform_qq(pit)
    ks = kstest(pit, "uniform")
    level = model.value_at_risk(alpha)
    exceedances = int(np.sum(values < level))
    rate = exceedances / len(values)
    coverage = binomtest(exceedances, len(values), alpha)
    shortfall = model.expected_shortfall(alpha)
    if not math.isfinite(shortfall):
        raise RuntimeError(f"{model.__class__.__name__} expected shortfall is not finite")
    realised: float | None
    if exceedances == 0:
        logger.warning("%s has no VaR exceedances at alpha=%s", model.__class__.__name__, alpha)
        realised = None
    else:
        realised = float(values[values < level].mean())
    name = "gaussian" if isinstance(model, GaussianModel) else "student_t"
    return DistributionScore(
        model=name,
        observations=int(len(values)),
        mean_log_score=float(log_score.mean()),
        pit_ks_statistic=float(ks.statistic),
        pit_ks_pvalue=float(ks.pvalue),
        var_alpha=float(alpha),
        var_level=float(level),
        var_exceedances=exceedances,
        var_exceedance_rate=float(rate),
        var_pvalue=float(coverage.pvalue),
        expected_shortfall=float(shortfall),
        realised_shortfall=realised,
        pit_qq_theoretical=tuple(float(value) for value in theoretical),
        pit_qq_empirical=tuple(float(value) for value in empirical),
    )


def uniform_qq(pit: np.ndarray, points: int = 199) -> tuple[np.ndarray, np.ndarray]:
    """Return uniform quantiles and the matching empirical PIT quantiles.

    The probability integral transform of a perfectly calibrated model is
    uniform. Its quantile curve then lies on the diagonal.
    """
    if points < 9:
        raise ValueError(f"QQ grid needs at least 9 points, got {points}")
    values = np.asarray(pit, dtype=float)
    if values.ndim != 1 or len(values) < _MIN_FIT:
        raise ValueError(f"PIT sample needs at least {_MIN_FIT} values, got {getattr(values, 'shape', None)}")
    if not np.isfinite(values).all() or (values < 0).any() or (values > 1).any():
        raise ValueError("PIT values must be finite and inside [0, 1]")
    theoretical = np.linspace(1.0 / (points + 1.0), points / (points + 1.0), points)
    empirical = np.quantile(values, theoretical)
    return theoretical, empirical


def bonferroni(pvalues: list[float]) -> list[float]:
    """Adjust one pre-registered family. An empty family raises."""
    if not pvalues:
        raise ValueError("Bonferroni adjustment needs at least one p-value")
    width = len(pvalues)
    adjusted = []
    for value in pvalues:
        if not math.isfinite(value) or not 0.0 <= value <= 1.0:
            raise ValueError(f"p-value must be in [0, 1], got {value}")
        adjusted.append(min(1.0, value * width))
    return adjusted


def _clean_sample(sample: np.ndarray, label: str) -> np.ndarray:
    values = np.asarray(sample, dtype=float)
    if values.ndim != 1:
        raise ValueError(f"{label} sample must be one-dimensional, got shape {values.shape}")
    if len(values) < _MIN_FIT:
        raise ValueError(f"{label} sample needs at least {_MIN_FIT} observations, got {len(values)}")
    if not np.isfinite(values).all():
        raise ValueError(f"{label} sample contains a non-finite observation")
    return values
