"""Filtered two-state regime probabilities for horizon-return forecasts.

The forecast may use only the forward probability π_{t|t}. Baum-Welch
smoothing is confined to parameter fitting on the training window and is
never returned. A one-minute return enters the filter only at the close
that makes it known, so it cannot contain the horizon label that starts
at that same close.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.distributions import DistributionScore, GaussianModel, StudentTModel, uniform_qq
from research.registry import ExperimentRecord
from research.returns import ReturnBuild, horizon_log_returns

logger = logging.getLogger(__name__)

MIN_OBSERVATIONS = 30
BENCHMARK_MODELS = ("gaussian", "student_t")
_MINUTE = pd.Timedelta(minutes=1)


@dataclass(frozen=True)
class GaussianHMM:
    """Two-state Gaussian HMM. ``transitions[i, j]`` is P(state j | state i)."""

    means: np.ndarray
    variances: np.ndarray
    transitions: np.ndarray
    initial: np.ndarray


@dataclass(frozen=True)
class HMMFit:
    """A fitted filter. ``gap_resets`` counts missing one-minute steps."""

    model: GaussianHMM
    iterations: int
    converged: bool
    log_likelihood: float
    observations: int
    gap_resets: int


@dataclass(frozen=True)
class H1Benchmark:
    """The unconditional density H2 has to beat.

    A recorded H1 development result names that density. Retention uses the
    retained model, and rejection of Student-t leaves Gaussian. An H1 record
    with no result uses Gaussian and leaves ``resolved`` false.
    """

    model: str
    resolved: bool
    reason: str


def h1_benchmark(record: ExperimentRecord) -> H1Benchmark:
    """Resolve the nested unconditional model from the H1 registry record."""
    if record.experiment_id != "EXP-H01":
        raise ValueError(f"H2 benchmark must be an H1 record, got {record.experiment_id}")
    if record.decision == "retain":
        if record.result not in BENCHMARK_MODELS:
            raise RuntimeError(
                f"retained H1 result must name gaussian or student_t, got {record.result!r}"
            )
        return H1Benchmark(record.result, True, "H1 retained this unconditional model")
    if record.decision == "reject":
        return H1Benchmark(
            "gaussian",
            True,
            "H1 rejected Student-t, so the unconditional benchmark stays Gaussian",
        )
    if record.decision == "" and record.result in BENCHMARK_MODELS:
        return H1Benchmark(
            record.result,
            True,
            f"H1 development validation selected {record.result}",
        )
    if record.decision in {"", "defer", "revise"}:
        shown = "open" if record.decision == "" else record.decision
        return H1Benchmark(
            "gaussian",
            False,
            f"H1 is {shown}; exploratory H2 uses Gaussian until H1 retains or rejects",
        )
    raise ValueError(f"unsupported H1 decision {record.decision!r}")


def clock_gap_resets(index: pd.Index) -> int:
    """Count steps that are not exactly one minute. Those steps reset the chain."""
    _, gaps = _segment_bounds(_clock(index))
    return gaps


def minute_returns_known_at_close(close: pd.Series) -> tuple[pd.Series, ReturnBuild]:
    """Return the one-minute log return indexed at the close that reveals it.

    ``log(close[t] / close[t - 1 minute])`` is known at ``t``. The horizon
    label decided at ``t`` uses closes after ``t``, so this return is in
    the information set and is not part of that label.
    """
    built = horizon_log_returns(close, 1)
    if built.returns.empty:
        raise ValueError("no one-minute return is known at a close")
    known_at = built.returns.index + pd.Timedelta(minutes=1)
    series = pd.Series(
        built.returns.to_numpy(dtype=float),
        index=known_at,
        name="minute_log_return",
    )
    if series.index.has_duplicates:
        raise RuntimeError("minute returns known at the close have duplicate timestamps")
    return series, built


def fit_gaussian_hmm(
    sample: np.ndarray,
    *,
    max_iterations: int = 25,
    tolerance: float = 1e-6,
) -> HMMFit:
    """Fit one contiguous two-state Gaussian HMM by Baum-Welch."""
    values = _clean_observations(sample, "HMM")
    return _fit_segments(values, ((0, len(values)),), 0, max_iterations, tolerance)


def fit_clocked_gaussian_hmm(
    returns: pd.Series,
    *,
    max_iterations: int = 25,
    tolerance: float = 1e-6,
) -> HMMFit:
    """Fit a clocked series. A missing minute resets the chain."""
    index = _clock(returns.index)
    values = _clean_observations(returns.to_numpy(), "HMM")
    segments, gap_resets = _segment_bounds(index)
    return _fit_segments(values, segments, gap_resets, max_iterations, tolerance)


def filtered_probabilities(model: GaussianHMM, sample: np.ndarray) -> np.ndarray:
    """Return π_{t|t} with shape ``(n, 2)``. Row ``t`` uses ``sample[:t + 1]`` only."""
    _validate_hmm(model)
    values = _clean_observations(sample, "regime filter")
    log_alpha, _ = _forward_two(
        _log_emissions(values, model.means, model.variances),
        np.log(model.transitions),
        np.log(model.initial),
    )
    return _normalize_rows(log_alpha)


def filtered_probability_frame(model: GaussianHMM, returns: pd.Series) -> tuple[pd.DataFrame, int]:
    """Filter each contiguous minute segment from its own initial state."""
    _validate_hmm(model)
    index = _clock(returns.index)
    values = _clean_observations(returns.to_numpy(), "regime filter")
    segments, gap_resets = _segment_bounds(index)
    probabilities = np.empty((len(values), 2), dtype=float)
    for start, stop in segments:
        probabilities[start:stop] = filtered_probabilities(model, values[start:stop])
    frame = pd.DataFrame(probabilities, index=index, columns=("state_0", "state_1"))
    if gap_resets:
        logger.warning("Regime filter reset the chain at %d gaps", gap_resets)
    return frame, gap_resets


def fit_weighted_gaussian(sample: np.ndarray, weights: np.ndarray) -> GaussianModel:
    """Gaussian MLE with non-negative weights. The variance uses total weight."""
    values, mass, total = _weighted_sample(sample, weights, "weighted Gaussian")
    mean = float(np.sum(mass * values) / total)
    variance = float(np.sum(mass * (values - mean) ** 2) / total)
    if variance <= 0.0 or not math.isfinite(variance):
        raise ValueError(f"weighted Gaussian variance is not positive: {variance}")
    return GaussianModel(mean, variance)


def fit_weighted_student_t(sample: np.ndarray, weights: np.ndarray) -> StudentTModel:
    """Weighted method of moments. Non-positive excess kurtosis raises."""
    values, mass, total = _weighted_sample(sample, weights, "weighted Student-t")
    loc = float(np.sum(mass * values) / total)
    centered = values - loc
    variance = float(np.sum(mass * centered**2) / total)
    fourth = float(np.sum(mass * centered**4) / total)
    if variance <= 0.0 or not math.isfinite(variance) or not math.isfinite(fourth):
        raise RuntimeError(f"weighted Student-t variance is not usable: {variance}")
    excess = fourth / variance**2 - 3.0
    if not math.isfinite(excess) or excess <= 0.0:
        raise RuntimeError(f"weighted Student-t excess kurtosis is not positive: {excess}")
    df = 4.0 + 6.0 / excess
    if df <= 4.0:
        raise RuntimeError(f"weighted Student-t degrees of freedom are not above 4: {df}")
    scale = math.sqrt(variance * (df - 2.0) / df)
    if not math.isfinite(scale) or scale <= 0.0:
        raise RuntimeError(f"weighted Student-t scale is not positive: {scale}")
    return StudentTModel(df, loc, scale)


def fit_regime_densities(
    sample: np.ndarray,
    probabilities: np.ndarray,
    family: str,
) -> tuple[GaussianModel | StudentTModel, GaussianModel | StudentTModel]:
    """Fit one horizon density per filtered state. The family is the H1 benchmark."""
    if family not in BENCHMARK_MODELS:
        raise ValueError(f"horizon density family must be gaussian or student_t, got {family!r}")
    weights = _probability_matrix(probabilities, len(np.asarray(sample)))
    fitted = []
    for column in range(weights.shape[1]):
        if family == "gaussian":
            fitted.append(fit_weighted_gaussian(sample, weights[:, column]))
        else:
            fitted.append(fit_weighted_student_t(sample, weights[:, column]))
    return fitted[0], fitted[1]


def score_regime_forecast(
    components: tuple[GaussianModel | StudentTModel, GaussianModel | StudentTModel],
    probabilities: np.ndarray,
    sample: np.ndarray,
    alpha: float = 0.05,
) -> DistributionScore:
    """Score a per-time mixture. Each VaR uses that time's filtered probability."""
    if not 0.0 < alpha < 0.5:
        raise ValueError(f"VaR alpha must be in (0, 0.5), got {alpha}")
    values = _clean_observations(sample, "regime forecast")
    weights = _probability_matrix(probabilities, len(values))
    log_score = _mixture_log_density(components, weights, values)
    if not np.isfinite(log_score).all():
        raise RuntimeError("regime mixture produced a non-finite log density")
    pit = _mixture_cdf(components, weights, values)
    if not np.isfinite(pit).all():
        raise RuntimeError("regime mixture produced a non-finite PIT")
    low = float(np.min(pit))
    high = float(np.max(pit))
    if low < -1e-8 or high > 1.0 + 1e-8:
        raise RuntimeError(f"regime mixture produced a PIT outside [0, 1]: min={low} max={high}")
    if low < 0.0 or high > 1.0:
        logger.warning("Clipped regime-mixture PIT from [%s, %s] onto [0, 1]", low, high)
        pit = np.clip(pit, 0.0, 1.0)
    from scipy.stats import binomtest, kstest

    theoretical, empirical = uniform_qq(pit)
    ks = kstest(pit, "uniform")
    level = _mixture_quantile(components, weights, alpha)
    exceedances = int(np.sum(values < level))
    rate = exceedances / len(values)
    coverage = binomtest(exceedances, len(values), alpha)
    shortfall = _mixture_expected_shortfall(components, weights, level, alpha)
    if not np.isfinite(shortfall).all():
        raise RuntimeError("regime mixture expected shortfall is not finite")
    realised: float | None
    if exceedances == 0:
        logger.warning("regime mixture has no VaR exceedances at alpha=%s", alpha)
        realised = None
    else:
        realised = float(values[values < level].mean())
    return DistributionScore(
        model="regime_mixture",
        observations=int(len(values)),
        mean_log_score=float(log_score.mean()),
        pit_ks_statistic=float(ks.statistic),
        pit_ks_pvalue=float(ks.pvalue),
        var_alpha=float(alpha),
        var_level=float(level.mean()),
        var_exceedances=exceedances,
        var_exceedance_rate=float(rate),
        var_pvalue=float(coverage.pvalue),
        expected_shortfall=float(shortfall.mean()),
        realised_shortfall=realised,
        pit_qq_theoretical=tuple(float(value) for value in theoretical),
        pit_qq_empirical=tuple(float(value) for value in empirical),
    )


def _fit_segments(
    sample: np.ndarray,
    segments: tuple[tuple[int, int], ...],
    gap_resets: int,
    max_iterations: int,
    tolerance: float,
) -> HMMFit:
    if max_iterations < 1:
        raise ValueError(f"HMM iterations must be positive, got {max_iterations}")
    if tolerance <= 0.0:
        raise ValueError(f"HMM tolerance must be positive, got {tolerance}")
    means, variances, transitions, initial = _initialise(sample)
    previous: float | None = None
    converged = False
    loglik = -math.inf
    iterations = 0
    for iteration in range(1, max_iterations + 1):
        iterations = iteration
        stats = _accumulate(sample, segments, means, variances, transitions, initial)
        loglik = stats[5]
        if previous is not None:
            drop = previous - loglik
            scale = max(1.0, abs(previous))
            if drop > 1e-8 * scale:
                raise RuntimeError(f"HMM likelihood fell from {previous} to {loglik}")
            if (loglik - previous) / scale < tolerance:
                converged = True
                break
        means, variances, transitions, initial = _maximisation(*stats[:5])
        previous = loglik
    if not converged:
        logger.warning("HMM did not converge after %d iterations", iterations)
    order = np.argsort(variances, kind="mergesort")
    model = GaussianHMM(
        means=means[order],
        variances=variances[order],
        transitions=transitions[np.ix_(order, order)],
        initial=initial[order],
    )
    _validate_hmm(model)
    ratio = float(model.variances[1] / model.variances[0])
    if ratio < 1.05:
        logger.warning("HMM variances are weakly separated: %s", model.variances)
    if gap_resets:
        logger.warning("HMM fit reset the chain at %d gaps", gap_resets)
    logger.info(
        "Fitted two-state Gaussian HMM observations=%d iterations=%d converged=%s loglik=%.6f",
        len(sample),
        iterations,
        converged,
        loglik,
    )
    return HMMFit(
        model=model,
        iterations=iterations,
        converged=converged,
        log_likelihood=float(loglik),
        observations=int(len(sample)),
        gap_resets=int(gap_resets),
    )


def _accumulate(sample, segments, means, variances, transitions, initial):
    log_transition = np.log(transitions)
    log_initial = np.log(initial)
    emissions = _log_emissions(sample, means, variances)
    weight = np.zeros(2)
    weight_y = np.zeros(2)
    weight_y2 = np.zeros(2)
    start_weight = np.zeros(2)
    transition_weight = np.zeros((2, 2))
    loglik = 0.0
    used = 0
    for start, stop in segments:
        length = stop - start
        if length < 1:
            continue
        emission = emissions[start:stop]
        block = sample[start:stop]
        log_alpha, segment_loglik = _forward_two(emission, log_transition, log_initial)
        if not math.isfinite(segment_loglik):
            raise RuntimeError("HMM forward likelihood is not finite")
        log_beta = _backward_two(emission, log_transition)
        gamma = _normalize_rows(log_alpha + log_beta)
        weight += gamma.sum(axis=0)
        weight_y += gamma.T @ block
        weight_y2 += gamma.T @ (block**2)
        start_weight += gamma[0]
        loglik += segment_loglik
        used += length
        if length == 1:
            continue
        log_xi = np.empty((length - 1, 2, 2))
        past = log_alpha[:-1]
        future_emission = emission[1:]
        future_beta = log_beta[1:]
        for origin in range(2):
            for destination in range(2):
                log_xi[:, origin, destination] = (
                    past[:, origin]
                    + log_transition[origin, destination]
                    + future_emission[:, destination]
                    + future_beta[:, destination]
                    - segment_loglik
                )
        responsibility = np.exp(log_xi - np.max(log_xi, axis=(1, 2), keepdims=True))
        row_mass = responsibility.sum(axis=(1, 2), keepdims=True)
        if np.any(row_mass <= 0.0) or not np.isfinite(row_mass).all():
            raise RuntimeError("HMM transition responsibilities are not finite")
        responsibility /= row_mass
        transition_weight += responsibility.sum(axis=0)
    if used < MIN_OBSERVATIONS:
        raise RuntimeError(f"HMM fit saw {used} observations, below {MIN_OBSERVATIONS}")
    return weight, weight_y, weight_y2, start_weight, transition_weight, loglik


def _maximisation(weight, weight_y, weight_y2, start_weight, transition_weight):
    if np.any(weight <= 0.0) or not np.isfinite(weight).all():
        raise RuntimeError(f"HMM state responsibilities are not usable: {weight}")
    means = weight_y / weight
    variances = weight_y2 / weight - means**2
    if np.any(variances <= 0.0) or not np.isfinite(variances).all() or not np.isfinite(means).all():
        raise RuntimeError(f"HMM M-step variances are not positive: {variances}")
    row = transition_weight.sum(axis=1)
    if np.any(row <= 0.0) or not np.isfinite(row).all():
        raise RuntimeError(f"HMM state has no observed transitions: {transition_weight}")
    transitions = transition_weight / row[:, None]
    if start_weight.sum() <= 0.0 or not np.isfinite(start_weight).all():
        raise RuntimeError(f"HMM initial state is not usable: {start_weight}")
    initial = start_weight / start_weight.sum()
    return means, variances, transitions, initial


def _initialise(sample: np.ndarray):
    centre = float(np.median(sample))
    scale = float(np.median(np.abs(sample - centre)))
    if not math.isfinite(scale) or scale <= 0.0:
        raise RuntimeError(f"HMM initialisation has no scale: median_absolute_deviation={scale}")
    high = np.abs(sample - centre) > scale
    low = ~high
    if int(low.sum()) < MIN_OBSERVATIONS or int(high.sum()) < MIN_OBSERVATIONS:
        raise RuntimeError(
            "HMM initialisation could not separate two regimes: "
            f"low={int(low.sum())} high={int(high.sum())}"
        )
    groups = (sample[low], sample[high])
    means = np.array([float(group.mean()) for group in groups])
    variances = np.array([float(np.mean((group - mean) ** 2)) for group, mean in zip(groups, means)])
    if np.any(variances <= 0.0) or not np.isfinite(variances).all():
        raise RuntimeError(f"HMM initial variances are not positive: {variances}")
    transitions = np.array([[0.95, 0.05], [0.05, 0.95]])
    initial = np.array([0.5, 0.5])
    return means, variances, transitions, initial


def _forward_two(log_emission: np.ndarray, log_transition: np.ndarray, log_initial: np.ndarray):
    count = log_emission.shape[0]
    log_alpha = np.empty((count, 2))
    log_alpha[0, 0] = log_initial[0] + log_emission[0, 0]
    log_alpha[0, 1] = log_initial[1] + log_emission[0, 1]
    stay_low, enter_high = log_transition[0]
    enter_low, stay_high = log_transition[1]
    for time in range(1, count):
        previous_low = log_alpha[time - 1, 0]
        previous_high = log_alpha[time - 1, 1]
        log_alpha[time, 0] = log_emission[time, 0] + _logsumexp_pair(
            previous_low + stay_low, previous_high + enter_low
        )
        log_alpha[time, 1] = log_emission[time, 1] + _logsumexp_pair(
            previous_low + enter_high, previous_high + stay_high
        )
    if not np.isfinite(log_alpha).all():
        raise RuntimeError("HMM forward pass produced a non-finite probability")
    return log_alpha, _logsumexp_pair(log_alpha[-1, 0], log_alpha[-1, 1])


def _backward_two(log_emission: np.ndarray, log_transition: np.ndarray) -> np.ndarray:
    count = log_emission.shape[0]
    log_beta = np.empty((count, 2))
    log_beta[-1, 0] = 0.0
    log_beta[-1, 1] = 0.0
    stay_low, enter_high = log_transition[0]
    enter_low, stay_high = log_transition[1]
    for time in range(count - 2, -1, -1):
        next_low = log_emission[time + 1, 0] + log_beta[time + 1, 0]
        next_high = log_emission[time + 1, 1] + log_beta[time + 1, 1]
        log_beta[time, 0] = _logsumexp_pair(stay_low + next_low, enter_high + next_high)
        log_beta[time, 1] = _logsumexp_pair(enter_low + next_low, stay_high + next_high)
    if not np.isfinite(log_beta).all():
        raise RuntimeError("HMM backward pass produced a non-finite probability")
    return log_beta


def _log_emissions(sample: np.ndarray, means: np.ndarray, variances: np.ndarray) -> np.ndarray:
    deviation = (sample.reshape(-1, 1) - means.reshape(1, 2)) ** 2 / variances.reshape(1, 2)
    return -0.5 * (math.log(2.0 * math.pi) + np.log(variances.reshape(1, 2)) + deviation)


def _mixture_log_density(components, probabilities, sample: np.ndarray) -> np.ndarray:
    columns = [_component_log_density(component, sample) for component in components]
    logged = np.column_stack(columns)
    logged_weights = np.full(probabilities.shape, -np.inf)
    positive = probabilities > 0.0
    logged_weights[positive] = np.log(probabilities[positive])
    return _row_logsumexp(logged_weights + logged)


def _mixture_cdf(components, probabilities, sample: np.ndarray) -> np.ndarray:
    total = np.zeros(len(sample))
    for component, column in zip(components, probabilities.T):
        total += column * _component_cdf(component, sample)
    return total


def _mixture_quantile(components, probabilities: np.ndarray, alpha: float) -> np.ndarray:
    count = len(probabilities)
    tail = min(alpha * 1e-4, 1e-6)
    low = np.full(count, min(component.value_at_risk(tail) for component in components))
    high = np.full(count, max(component.value_at_risk(1.0 - tail) for component in components))
    if not np.isfinite(low).all() or not np.isfinite(high).all() or np.any(low >= high):
        raise RuntimeError("mixture quantile bounds are not ordered")
    for _ in range(60):
        mid = 0.5 * (low + high)
        cdf = _mixture_cdf(components, probabilities, mid)
        go_left = cdf >= alpha
        high = np.where(go_left, mid, high)
        low = np.where(go_left, low, mid)
    quantile = 0.5 * (low + high)
    error = float(np.max(np.abs(_mixture_cdf(components, probabilities, quantile) - alpha)))
    if error > 1e-4:
        raise RuntimeError(f"mixture quantile CDF error is {error}")
    return quantile


def _mixture_expected_shortfall(components, probabilities, quantile: np.ndarray, alpha: float) -> np.ndarray:
    partial = np.zeros(len(quantile))
    for component, column in zip(components, probabilities.T):
        partial += column * _partial_expectation(component, quantile)
    return partial / alpha


def _partial_expectation(component, quantile: np.ndarray) -> np.ndarray:
    """Return E[X 1_{X < q}] at each quantile."""
    if isinstance(component, GaussianModel):
        from scipy.stats import norm

        scale = math.sqrt(component.variance)
        standardised = (quantile - component.mean) / scale
        return component.mean * norm.cdf(standardised) - scale * norm.pdf(standardised)
    from scipy.stats import t

    standardised = (quantile - component.loc) / component.scale
    density = t.pdf(standardised, component.df)
    probability = t.cdf(standardised, component.df)
    tail = -density * (component.df + standardised**2) / (component.df - 1.0)
    return component.loc * probability + component.scale * tail


def _component_log_density(component, sample: np.ndarray) -> np.ndarray:
    return np.asarray(component.log_density(sample), dtype=float)


def _component_cdf(component, sample: np.ndarray) -> np.ndarray:
    return np.asarray(component.cdf(sample), dtype=float)


def _validate_hmm(model: GaussianHMM) -> None:
    shapes = (model.means.shape, model.variances.shape, model.initial.shape, model.transitions.shape)
    if shapes != ((2,), (2,), (2,), (2, 2)):
        raise ValueError(f"H2 Gaussian HMM is pre-registered with two states, got shapes {shapes}")
    if not np.isfinite(model.means).all() or not np.isfinite(model.variances).all():
        raise ValueError("HMM means and variances must be finite")
    if np.any(model.variances <= 0.0):
        raise ValueError(f"HMM variances must be positive, got {model.variances}")
    if np.any(model.transitions <= 0.0) or not np.allclose(model.transitions.sum(axis=1), 1.0):
        raise ValueError(f"HMM transitions must be positive and row-stochastic, got {model.transitions}")
    if np.any(model.initial <= 0.0) or not math.isclose(float(model.initial.sum()), 1.0):
        raise ValueError(f"HMM initial distribution must be positive and sum to one, got {model.initial}")


def _segment_bounds(index: pd.DatetimeIndex) -> tuple[tuple[tuple[int, int], ...], int]:
    if len(index) == 1:
        return ((0, 1),), 0
    gaps = 0
    starts = [0]
    elapsed = index.to_series().diff().iloc[1:]
    for position, step in enumerate(elapsed, start=1):
        if step == _MINUTE:
            continue
        if not isinstance(step, pd.Timedelta) or step <= pd.Timedelta(0):
            raise ValueError(f"regime clock is not increasing at {index[position]}")
        if step.total_seconds() % 60 != 0:
            raise ValueError(f"regime clock has a non-minute step of {step} at {index[position]}")
        gaps += 1
        starts.append(position)
    bounds = tuple((start, end) for start, end in zip(starts, [*starts[1:], len(index)]))
    return bounds, gaps


def _clock(index: pd.Index) -> pd.DatetimeIndex:
    clock = as_utc_index(index)
    if not clock.is_monotonic_increasing or clock.has_duplicates:
        raise ValueError("regime clock must be sorted and unique")
    return clock


def _clean_observations(sample: np.ndarray, label: str) -> np.ndarray:
    values = np.asarray(sample, dtype=float)
    if values.ndim != 1:
        raise ValueError(f"{label} sample must be one-dimensional, got shape {values.shape}")
    if len(values) < 1:
        raise ValueError(f"{label} sample is empty")
    if not np.isfinite(values).all():
        raise ValueError(f"{label} sample contains a non-finite observation")
    return values


def _weighted_sample(sample: np.ndarray, weights: np.ndarray, label: str):
    values = np.asarray(sample, dtype=float)
    mass = np.asarray(weights, dtype=float)
    if values.ndim != 1 or mass.shape != values.shape:
        raise ValueError(f"{label} sample and weights must share one shape")
    if len(values) < MIN_OBSERVATIONS:
        raise ValueError(f"{label} needs at least {MIN_OBSERVATIONS} observations, got {len(values)}")
    if not np.isfinite(values).all() or not np.isfinite(mass).all() or np.any(mass < 0.0):
        raise ValueError(f"{label} contains a non-finite value or a negative weight")
    total = float(mass.sum())
    if total <= 0.0:
        raise ValueError(f"{label} weights sum to {total}")
    effective = total**2 / float(np.sum(mass**2))
    if effective < MIN_OBSERVATIONS:
        raise ValueError(f"{label} effective sample size is {effective:.1f}, below {MIN_OBSERVATIONS}")
    return values, mass, total


def _probability_matrix(probabilities: np.ndarray, count: int) -> np.ndarray:
    weights = np.asarray(probabilities, dtype=float)
    if weights.shape != (count, 2):
        raise ValueError(f"filtered probabilities must have shape {(count, 2)}, got {weights.shape}")
    if not np.isfinite(weights).all() or np.any(weights < 0.0):
        raise ValueError("filtered probabilities must be finite and non-negative")
    totals = weights.sum(axis=1)
    if not np.allclose(totals, 1.0, atol=1e-6):
        raise ValueError("filtered probability rows must sum to one")
    return weights / totals.reshape(-1, 1)


def _normalize_rows(log_scores: np.ndarray) -> np.ndarray:
    if not np.isfinite(log_scores).all():
        raise RuntimeError("regime probabilities are not finite")
    peak = np.max(log_scores, axis=1, keepdims=True)
    weights = np.exp(log_scores - peak)
    total = weights.sum(axis=1, keepdims=True)
    if np.any(total <= 0.0):
        raise RuntimeError("regime filter could not normalise a state probability")
    return weights / total


def _row_logsumexp(log_scores: np.ndarray) -> np.ndarray:
    peak = np.max(log_scores, axis=1, keepdims=True)
    return (peak + np.log(np.sum(np.exp(log_scores - peak), axis=1, keepdims=True))).ravel()


def _logsumexp_pair(left: float, right: float) -> float:
    if not math.isfinite(left) and not math.isfinite(right):
        return -math.inf
    if left > right:
        peak, other = left, right
    else:
        peak, other = right, left
    if not math.isfinite(other):
        return float(peak)
    return float(peak + math.log1p(math.exp(other - peak)))
