"""Causal cross-asset factor state and conditional density models for H3."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.distributions import (
    DistributionScore,
    GaussianModel,
    StudentTModel,
    fit_gaussian,
    fit_student_t,
    uniform_qq,
)

logger = logging.getLogger(__name__)
_MIN_FIT = 30


@dataclass(frozen=True)
class PanelAlignment:
    """A complete-case close panel and explicit loss accounting."""

    closes: pd.DataFrame
    union_rows: int
    aligned_rows: int
    dropped_timestamps: int
    missing_by_symbol: dict[str, int]


@dataclass(frozen=True)
class FactorState:
    """Frozen-loadings factor features available at each decision time."""

    features: pd.DataFrame
    explained_variance_ratio: tuple[float, ...]
    absorption_ratio: float
    single_factor_beta: float
    lag_exclusions: int


@dataclass(frozen=True)
class ConditionalDensityModel:
    """Linear conditional location with a frozen Gaussian/Student-t residual."""

    name: str
    feature_names: tuple[str, ...]
    feature_mean: np.ndarray
    feature_scale: np.ndarray
    coefficients: np.ndarray
    residual: GaussianModel | StudentTModel

    def location(self, features: pd.DataFrame) -> np.ndarray:
        _require_feature_columns(features, self.feature_names)
        values = features.loc[:, self.feature_names].to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{self.name} features contain non-finite values")
        standardised = (values - self.feature_mean) / self.feature_scale
        design = np.column_stack([np.ones(len(standardised)), standardised])
        return np.asarray(design @ self.coefficients, dtype=float)


def align_close_panel(
    closes: Mapping[str, pd.Series], symbols: tuple[str, ...]
) -> PanelAlignment:
    """Inner-align a declared symbol panel without filling absent minutes."""
    if len(symbols) < 2 or len(set(symbols)) != len(symbols):
        raise ValueError("factor panel needs at least two unique symbols")
    if set(closes) != set(symbols):
        missing = sorted(set(symbols) - set(closes))
        extra = sorted(set(closes) - set(symbols))
        raise ValueError(
            f"panel symbols do not match protocol: missing={missing} extra={extra}"
        )
    normalised: dict[str, pd.Series] = {}
    for symbol in symbols:
        series = closes[symbol]
        if series.empty:
            raise ValueError(f"{symbol} close series is empty")
        index = as_utc_index(series.index)
        if index.duplicated().any():
            raise ValueError(f"{symbol} close series contains duplicate timestamps")
        values = pd.Series(
            series.to_numpy(dtype=float), index=index, name=symbol
        ).sort_index()
        if not np.isfinite(values.to_numpy()).all() or (values <= 0.0).any():
            raise ValueError(
                f"{symbol} close series contains non-positive or non-finite values"
            )
        normalised[symbol] = values
    outer = pd.concat(normalised.values(), axis=1, join="outer").sort_index()
    missing_by_symbol = {symbol: int(outer[symbol].isna().sum()) for symbol in symbols}
    missing_mask = outer.isna().any(axis=1)
    aligned = outer.loc[~missing_mask, list(symbols)]
    dropped = int(missing_mask.sum())
    if aligned.empty:
        raise RuntimeError("cross-asset alignment kept no timestamps")
    if dropped:
        logger.warning(
            "Cross-asset alignment dropped %d/%d timestamps; missing_by_symbol=%s",
            dropped,
            len(outer),
            missing_by_symbol,
        )
    return PanelAlignment(
        closes=aligned,
        union_rows=int(len(outer)),
        aligned_rows=int(len(aligned)),
        dropped_timestamps=dropped,
        missing_by_symbol=missing_by_symbol,
    )


def panel_minute_returns(closes: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Build log returns only across consecutive one-minute observations."""
    if closes.empty or closes.shape[1] < 2:
        raise ValueError("panel returns need at least two non-empty close columns")
    index = as_utc_index(closes.index)
    values = closes.copy()
    values.index = index
    if values.index.duplicated().any():
        raise ValueError("panel closes contain duplicate timestamps")
    if (
        not np.isfinite(values.to_numpy(dtype=float)).all()
        or (values <= 0.0).any().any()
    ):
        raise ValueError("panel closes contain non-positive or non-finite values")
    consecutive = values.index.to_series().diff().eq(pd.Timedelta(minutes=1))
    raw = np.log(values / values.shift(1))
    valid = consecutive & raw.notna().all(axis=1)
    exclusions = int((~valid).sum())
    returns = raw.loc[valid]
    if returns.empty:
        raise RuntimeError("panel return construction kept no consecutive minutes")
    if exclusions:
        logger.warning(
            "Panel return construction excluded %d non-consecutive/initial rows",
            exclusions,
        )
    return returns, exclusions


def fit_factor_state(
    panel_returns: pd.DataFrame,
    *,
    target_symbol: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    components: int,
    lag_minutes: int,
) -> FactorState:
    """Fit factor loadings on the training window and transform the full span."""
    if target_symbol not in panel_returns:
        raise ValueError(f"target symbol {target_symbol} is absent from panel returns")
    if components < 1 or components >= panel_returns.shape[1]:
        raise ValueError(
            f"PCA components must be in [1, {panel_returns.shape[1] - 1}], got {components}"
        )
    if lag_minutes < 1:
        raise ValueError(
            f"factor features must be lagged by at least one minute, got {lag_minutes}"
        )
    train = panel_returns.loc[
        (panel_returns.index >= train_start) & (panel_returns.index < train_end)
    ]
    if len(train) < _MIN_FIT:
        raise ValueError(
            f"factor training sample needs at least {_MIN_FIT} rows, got {len(train)}"
        )
    mean = train.mean(axis=0)
    scale = train.std(axis=0, ddof=0)
    if not np.isfinite(scale.to_numpy()).all() or (scale <= 0.0).any():
        raise ValueError(
            "factor training sample has a non-positive or non-finite scale"
        )
    train_z = (train - mean) / scale
    values = train_z.to_numpy(dtype=float)
    correlation = values.T @ values / len(values)
    eigenvalues, eigenvectors = np.linalg.eigh(correlation)
    order = np.argsort(eigenvalues)[::-1]
    eigenvalues = np.maximum(eigenvalues[order], 0.0)
    loadings = eigenvectors[:, order[:components]].T.copy()
    for row in loadings:
        anchor = int(np.argmax(np.abs(row)))
        if row[anchor] < 0.0:
            row *= -1.0
    all_z = (panel_returns - mean) / scale
    scores = all_z.to_numpy(dtype=float) @ loadings.T
    reconstruction = scores @ loadings
    target_position = int(panel_returns.columns.get_loc(target_symbol))
    pca_residual = (
        all_z.iloc[:, target_position].to_numpy(dtype=float)
        - reconstruction[:, target_position]
    ) * float(scale.iloc[target_position])

    peers = [column for column in panel_returns.columns if column != target_symbol]
    market = panel_returns.loc[:, peers].mean(axis=1)
    train_market = market.reindex(train.index).to_numpy(dtype=float)
    train_target = train[target_symbol].to_numpy(dtype=float)
    denominator = float(train_market @ train_market)
    if denominator <= 0.0 or not math.isfinite(denominator):
        raise ValueError("single-factor market return has no usable variance")
    beta = float((train_market @ train_target) / denominator)
    single_residual = panel_returns[target_symbol] - beta * market

    features = pd.DataFrame(index=panel_returns.index)
    features["market_factor"] = market
    features["single_factor_residual"] = single_residual
    for component in range(components):
        features[f"pca_factor_{component + 1}"] = scores[:, component]
    features["pca_residual"] = pca_residual
    features = features.shift(lag_minutes)
    lag_exclusions = int(features.isna().any(axis=1).sum())
    features = features.dropna()
    if features.empty:
        raise RuntimeError("factor lagging kept no feature rows")
    ratios = eigenvalues / eigenvalues.sum()
    absorption = float(ratios[:components].sum())
    return FactorState(
        features=features,
        explained_variance_ratio=tuple(float(value) for value in ratios[:components]),
        absorption_ratio=absorption,
        single_factor_beta=beta,
        lag_exclusions=lag_exclusions,
    )


def own_price_features(
    target_returns: pd.Series, *, volatility_window: int, lag_minutes: int
) -> pd.DataFrame:
    """Construct the nested own-price information set at the same lag."""
    if volatility_window < 2:
        raise ValueError(
            f"volatility window must be at least two minutes, got {volatility_window}"
        )
    if lag_minutes < 1:
        raise ValueError(f"own-price features must be lagged, got {lag_minutes}")
    features = pd.DataFrame(index=target_returns.index)
    features["own_return"] = target_returns
    features["own_realised_volatility"] = target_returns.rolling(
        volatility_window, min_periods=volatility_window
    ).std(ddof=0)
    return features.shift(lag_minutes).dropna()


@dataclass(frozen=True)
class ExternalFactorState:
    """Lagged external-market state. Closed sessions are not filled."""

    features: pd.DataFrame
    usd_factor_legs: tuple[tuple[str, int], ...]
    lag_exclusions: int


def external_market_features(
    panel_returns: pd.DataFrame,
    *,
    feature_symbols: tuple[tuple[str, str], ...] | list[tuple[str, str]] | list[list[str]],
    usd_factor_legs: tuple[tuple[str, int], ...] | list,
    lag_minutes: int,
) -> ExternalFactorState:
    """Build lagged external returns and a frozen-sign USD factor.

    Missing returns are rejected. This function does not forward-fill a
    closed session into later minutes.
    """
    if lag_minutes < 1:
        raise ValueError(
            f"external-market features must be lagged by at least one minute, got {lag_minutes}"
        )
    if panel_returns.empty:
        raise ValueError("external-market returns are empty")
    if panel_returns.isna().any().any():
        raise ValueError(
            "external-market returns contain missing values; refusing to fill them"
        )
    declared = tuple((str(name), str(symbol)) for name, symbol in feature_symbols)
    if not declared or len({name for name, _ in declared}) != len(declared):
        raise ValueError("external features need unique names")
    columns: dict[str, pd.Series] = {}
    for name, symbol in declared:
        if symbol not in panel_returns.columns:
            raise ValueError(f"external feature {name} needs {symbol} in the return panel")
        columns[name] = panel_returns[symbol]
    legs = tuple((str(symbol), int(sign)) for symbol, sign in usd_factor_legs)
    if len(legs) < 2 or len({symbol for symbol, _ in legs}) != len(legs):
        raise ValueError("USD factor needs at least two unique frozen legs")
    signed = []
    for symbol, sign in legs:
        if sign not in (-1, 1):
            raise ValueError(f"USD factor sign for {symbol} must be -1 or 1, got {sign}")
        if symbol not in panel_returns.columns:
            raise ValueError(f"USD factor leg {symbol} is absent from the return panel")
        signed.append(sign * panel_returns[symbol])
    columns["usd_factor"] = sum(signed) / len(signed)
    features = pd.DataFrame(columns, index=panel_returns.index)
    features = features.shift(lag_minutes)
    lag_exclusions = int(features.isna().any(axis=1).sum())
    features = features.dropna()
    if features.empty:
        raise RuntimeError("external-market lagging kept no feature rows")
    return ExternalFactorState(
        features=features,
        usd_factor_legs=legs,
        lag_exclusions=lag_exclusions,
    )


def fit_conditional_density(
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    family: str,
    name: str,
) -> ConditionalDensityModel:
    """Fit a linear location and the H1-selected residual density."""
    aligned = features.reindex(labels.index)
    missing = aligned.isna().any(axis=1)
    if missing.any():
        raise ValueError(
            f"{name} training features are missing for {int(missing.sum())} labels"
        )
    if len(labels) < _MIN_FIT:
        raise ValueError(f"{name} training sample needs at least {_MIN_FIT} labels")
    x = aligned.to_numpy(dtype=float)
    y = labels.to_numpy(dtype=float)
    if not np.isfinite(x).all() or not np.isfinite(y).all():
        raise ValueError(f"{name} training sample contains non-finite values")
    mean = x.mean(axis=0)
    scale = x.std(axis=0, ddof=0)
    if not np.isfinite(scale).all() or (scale <= 0.0).any():
        raise ValueError(f"{name} training feature has zero or non-finite scale")
    standardised = (x - mean) / scale
    design = np.column_stack([np.ones(len(x)), standardised])
    gram = design.T @ design
    if np.linalg.matrix_rank(gram) != design.shape[1]:
        raise ValueError(f"{name} training design is rank deficient")
    coefficients = np.linalg.solve(gram, design.T @ y)
    residuals = y - design @ coefficients
    if family == "gaussian":
        residual = fit_gaussian(residuals)
    elif family == "student_t":
        residual = fit_student_t(residuals)
    else:
        raise ValueError(
            f"conditional density family must be gaussian or student_t, got {family!r}"
        )
    return ConditionalDensityModel(
        name=name,
        feature_names=tuple(features.columns),
        feature_mean=mean,
        feature_scale=scale,
        coefficients=np.asarray(coefficients, dtype=float),
        residual=residual,
    )


def score_conditional_density(
    model: ConditionalDensityModel,
    features: pd.DataFrame,
    labels: pd.Series,
    *,
    alpha: float,
) -> DistributionScore:
    """Score a frozen conditional-location density without refitting it."""
    if not 0.0 < alpha < 0.5:
        raise ValueError(f"VaR alpha must be in (0, 0.5), got {alpha}")
    aligned = features.reindex(labels.index)
    if aligned.isna().any(axis=1).any():
        raise ValueError(f"{model.name} scoring features are missing")
    y = labels.to_numpy(dtype=float)
    location = model.location(aligned)
    residual_values = y - location
    log_score = np.asarray(model.residual.log_density(residual_values), dtype=float)
    pit = np.asarray(model.residual.cdf(residual_values), dtype=float)
    if not np.isfinite(log_score).all() or not np.isfinite(pit).all():
        raise RuntimeError(f"{model.name} produced non-finite density scores")
    if (pit < 0.0).any() or (pit > 1.0).any():
        raise RuntimeError(f"{model.name} produced PIT values outside [0, 1]")
    from scipy.stats import binomtest, kstest

    theoretical, empirical = uniform_qq(pit)
    residual_var = model.residual.value_at_risk(alpha)
    levels = location + residual_var
    exceedance_mask = y < levels
    exceedances = int(exceedance_mask.sum())
    coverage = binomtest(exceedances, len(y), alpha)
    residual_es = model.residual.expected_shortfall(alpha)
    expected_shortfalls = location + residual_es
    realised = float(y[exceedance_mask].mean()) if exceedances else None
    if exceedances == 0:
        logger.warning("%s has no VaR exceedances at alpha=%s", model.name, alpha)
    ks = kstest(pit, "uniform")
    return DistributionScore(
        model=model.name,
        observations=int(len(y)),
        mean_log_score=float(log_score.mean()),
        pit_ks_statistic=float(ks.statistic),
        pit_ks_pvalue=float(ks.pvalue),
        var_alpha=float(alpha),
        var_level=float(levels.mean()),
        var_exceedances=exceedances,
        var_exceedance_rate=float(exceedances / len(y)),
        var_pvalue=float(coverage.pvalue),
        expected_shortfall=float(expected_shortfalls.mean()),
        realised_shortfall=realised,
        pit_qq_theoretical=tuple(float(value) for value in theoretical),
        pit_qq_empirical=tuple(float(value) for value in empirical),
    )


def _require_feature_columns(features: pd.DataFrame, names: tuple[str, ...]) -> None:
    if tuple(features.columns) != names:
        raise ValueError(
            f"conditional features must be {list(names)}, got {list(features.columns)}"
        )
