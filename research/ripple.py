"""Exchange-time trade-flow features for the H7 information test.

The stored tape has one clock, the exchange timestamp. Depth diffs are not
reconstructed into a book. Snapshot features stay outside the primary model.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.clock import as_utc_index
from research.protocol import H7_CONFIG
from research.splits import Origin
from research.wave import trailing_log_volatility

logger = logging.getLogger(__name__)

_TRADE_COLUMNS = ("timestamp", "price", "quantity", "is_buyer_maker")
_NS = 1_000_000_000


@dataclass(frozen=True)
class HorizonExclusions:
    """Labels dropped from one horizon, with the reason counted."""

    horizon_minutes: int
    gap_exclusions: int
    endpoint_exclusions: int
    labels: int


@dataclass(frozen=True)
class RippleQuality:
    """What the trade tape kept, and what the join excluded."""

    rows_in: int
    rows_kept: int
    duplicate_rows: int
    nonpositive_rows: int
    coverage_days: int
    decisions: int
    feature_exclusions: int
    horizon_exclusions: tuple[HorizonExclusions, ...]
    depth_update_objects: int
    depth_update_bytes: int
    unreadable_objects: int
    clocks: tuple[str, ...]
    depth_sequence_ids: bool


@dataclass(frozen=True)
class RippleSample:
    """Joined slow state, trade flow, and labels. Snapshots are separate."""

    features: pd.DataFrame
    labels: dict[int, pd.DataFrame]
    quality: RippleQuality
    snapshot_features: pd.DataFrame
    snapshot_matched: int
    snapshot_identified: bool


@dataclass(frozen=True)
class BernoulliModel:
    """Frozen logistic probability. Coefficients follow the feature order."""

    name: str
    feature_names: tuple[str, ...]
    feature_mean: np.ndarray
    feature_scale: np.ndarray
    coefficients: np.ndarray
    clip: float

    def probability(self, features: pd.DataFrame) -> np.ndarray:
        if tuple(features.columns) != self.feature_names:
            raise ValueError(
                f"{self.name} features must be {list(self.feature_names)}, "
                f"got {list(features.columns)}"
            )
        values = features.to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{self.name} features contain non-finite values")
        standardised = (values - self.feature_mean) / self.feature_scale
        design = np.column_stack([np.ones(len(standardised)), standardised])
        eta = np.clip(design @ self.coefficients, -30.0, 30.0)
        probability = 1.0 / (1.0 + np.exp(-eta))
        return np.clip(probability, self.clip, 1.0 - self.clip)


def require_h7_protocol(settings: dict) -> None:
    """Reject a configuration that is not the registered H7 information test."""
    if settings.get("experiment_id") != "EXP-H07":
        raise ValueError("H7 protocol must use EXP-H07")
    if tuple(int(item) for item in settings.get("horizons_minutes", ())) != tuple(
        H7_CONFIG["horizons_minutes"]
    ):
        raise ValueError("H7 horizons are not the registered family")
    if tuple(settings.get("z_features", ())) != tuple(H7_CONFIG["z_features"]):
        raise ValueError("H7 slow-state features are not the registered set")
    if tuple(settings.get("o_features", ())) != tuple(H7_CONFIG["o_features"]):
        raise ValueError("H7 trade-flow features are not the registered set")
    if tuple(settings.get("models", ())) != tuple(H7_CONFIG["models"]):
        raise ValueError("H7 models are not the registered pair")
    if tuple(settings.get("endpoints", ())) != tuple(H7_CONFIG["endpoints"]):
        raise ValueError("H7 endpoints are not the registered pair")
    if settings.get("nested_model") != H7_CONFIG["nested_model"]:
        raise ValueError("H7 nested model must be the frozen slow state")
    if settings.get("candidate_model") != H7_CONFIG["candidate_model"]:
        raise ValueError("H7 candidate model must be trade flow")
    if settings.get("residual_family") != "student_t":
        raise ValueError("H7 residual family must be the working Student-t")
    if settings.get("economic_evaluation") != "predictive_only":
        raise ValueError("H7 is predictive only")
    if settings.get("forecast_multiple_testing") != H7_CONFIG["forecast_multiple_testing"]:
        raise ValueError("H7 must require every horizon and both endpoints")
    if tuple(settings.get("clocks", ())) != ("exchange",):
        raise ValueError("H7 development clock is the stored exchange timestamp")
    if settings.get("depth_sequence_ids") is not False:
        raise ValueError("H7 does not treat depth sequence ids as present")
    if settings.get("aggressor_buy") != "not_buyer_maker":
        raise ValueError("H7 aggressor buy is a trade that is not buyer-maker")
    if tuple(int(item) for item in settings.get("flow_windows_seconds", ())) != (60, 300):
        raise ValueError("H7 flow windows must be 60 seconds and 5 minutes")
    integers = {
        "sealed_days": 14,
        "train_days": 14,
        "validation_days": 7,
        "step_days": 7,
        "minimum_coverage_days": 35,
        "minimum_window_rows": 30,
        "return_lookback_minutes": 60,
        "volatility_lookback_minutes": 60,
        "gap_seconds": 60,
        "snapshot_max_age_seconds": 60,
        "snapshot_minimum_decisions": 30,
        "logistic_iterations": 25,
    }
    for key, expected in integers.items():
        if int(settings.get(key, -1)) != expected:
            raise ValueError(f"H7 {key} is not the registered value")
    floats = {
        "barrier_sigma": 1.0,
        "var_alpha": 0.05,
        "logistic_l2": 1e-6,
        "probability_clip": 1e-6,
    }
    for key, expected in floats.items():
        if float(settings.get(key, -1.0)) != expected:
            raise ValueError(f"H7 {key} is not the registered value")
    if tuple(settings.get("trade_prefixes", ())) != tuple(H7_CONFIG["trade_prefixes"]):
        raise ValueError("H7 trade prefixes are not the registered layouts")


def trailing_log_return(close: pd.Series, lookback_minutes: int) -> pd.Series:
    """Log change over an exact lookback. A missing minute is not a return."""
    if lookback_minutes < 1:
        raise ValueError(f"return lookback must be positive, got {lookback_minutes}")
    index = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=index).sort_index()
    if values.index.has_duplicates:
        raise ValueError("close series contains duplicate timestamps")
    previous = values.shift(lookback_minutes)
    span = values.index.to_series().diff(lookback_minutes)
    exact = span.eq(pd.Timedelta(minutes=lookback_minutes))
    usable = exact & np.isfinite(values) & np.isfinite(previous) & (values > 0) & (previous > 0)
    result = pd.Series(np.nan, index=values.index, name=f"own_return_{lookback_minutes}m")
    result.loc[usable] = np.log(values.loc[usable].to_numpy()) - np.log(previous.loc[usable].to_numpy())
    return result


def snapshot_ablation(
    snapshots: pd.DataFrame,
    decisions: pd.DatetimeIndex,
    *,
    kind: str,
    max_age_seconds: int,
    minimum_decisions: int,
) -> tuple[pd.DataFrame, int, bool]:
    """Top-of-book features from full snapshots at or before each decision.

    Depth updates are diffs. They are refused rather than assembled into a book.
    """
    if kind != "depth_snapshots":
        raise ValueError("depth updates are diffs and are not reconstructed into a book")
    if max_age_seconds < 1:
        raise ValueError(f"snapshot age must be positive, got {max_age_seconds}")
    if minimum_decisions < 1:
        raise ValueError(f"snapshot minimum must be positive, got {minimum_decisions}")
    columns = ["spread", "imbalance", "microprice"]
    if len(decisions) == 0:
        empty = pd.DataFrame(columns=columns)
        empty.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        return empty, 0, False
    clock = as_utc_index(decisions)
    empty = pd.DataFrame(columns=columns, index=clock[:0])
    if snapshots.empty:
        logger.warning("Snapshot ablation has no full-book observations")
        return empty, 0, False
    missing = {"timestamp", "side", "price", "quantity"} - set(snapshots.columns)
    if missing:
        raise ValueError(f"snapshots are missing columns: {sorted(missing)}")
    books = _snapshot_books(snapshots)
    if books.empty:
        logger.warning("Snapshot ablation kept no two-sided books")
        return empty, 0, False
    decision_ns = _ns(clock)
    book_ns = books["timestamp_ns"].to_numpy(dtype=np.int64)
    positions = np.searchsorted(book_ns, decision_ns, side="right") - 1
    age_limit = int(max_age_seconds) * _NS
    matched_rows = []
    matched_index = []
    stale = 0
    for offset, (position, decision) in enumerate(zip(positions, clock)):
        if position < 0 or int(decision_ns[offset] - book_ns[position]) > age_limit:
            stale += 1
            continue
        book = books.iloc[int(position)]
        matched_index.append(decision)
        matched_rows.append(
            {
                "spread": float(book["spread"]),
                "imbalance": float(book["imbalance"]),
                "microprice": float(book["microprice"]),
            }
        )
    if stale:
        logger.warning("Snapshot ablation excluded %d decisions without a fresh book", stale)
    features = pd.DataFrame(matched_rows, index=pd.DatetimeIndex(matched_index, tz="UTC"))
    features.index.name = "timestamp"
    identified = len(features) >= minimum_decisions
    if not identified:
        logger.warning(
            "Snapshot ablation is not identified: matched=%d minimum=%d",
            len(features),
            minimum_decisions,
        )
    return features, int(len(features)), bool(identified)


def build_ripple_sample(
    trades: pd.DataFrame,
    bars: pd.DataFrame,
    config: dict | None = None,
    *,
    snapshots: pd.DataFrame | None = None,
    depth_update_objects: int = 0,
    depth_update_bytes: int = 0,
    unreadable_objects: int = 0,
) -> RippleSample:
    """Join frozen slow state to trade flow and build trade-path labels.

    A print after the decision does not enter that decision's features.
    A horizon containing a gap above the registered limit is excluded and
    counted. Nothing is interpolated across the gap.
    """
    settings = dict(H7_CONFIG)
    if config:
        settings.update(config)
    require_h7_protocol(settings)
    if depth_update_objects < 0 or depth_update_bytes < 0 or unreadable_objects < 0:
        raise ValueError("inventory counts cannot be negative")
    clean, rows_in, duplicate_rows, nonpositive_rows = normalise_trades(trades)
    coverage = _coverage_days(clean.index) if not clean.empty else 0
    frame = bars.loc[:, ["close"]].copy()
    frame.index = as_utc_index(frame.index)
    frame = frame.sort_index()
    if frame.index.has_duplicates:
        raise ValueError("Wave bars contain duplicate timestamps")
    close = frame["close"].astype(float)
    slow = pd.DataFrame(
        {
            "own_return_60m": trailing_log_return(close, int(settings["return_lookback_minutes"])),
            "own_vol_60m": trailing_log_volatility(close, int(settings["volatility_lookback_minutes"])),
        }
    )
    decisions = slow.dropna().index
    if clean.empty or len(decisions) == 0:
        quality = _quality(
            rows_in=rows_in,
            rows_kept=len(clean),
            duplicate_rows=duplicate_rows,
            nonpositive_rows=nonpositive_rows,
            coverage_days=coverage,
            decisions=0,
            feature_exclusions=int(len(slow) - len(decisions)),
            horizons=(),
            depth_update_objects=depth_update_objects,
            depth_update_bytes=depth_update_bytes,
            unreadable_objects=unreadable_objects,
        )
        logger.warning("H7 join kept no decisions: coverage_days=%d", coverage)
        return RippleSample(
            features=pd.DataFrame(columns=list(settings["z_features"]) + list(settings["o_features"])),
            labels={},
            quality=quality,
            snapshot_features=pd.DataFrame(),
            snapshot_matched=0,
            snapshot_identified=False,
        )
    trade_ns = _ns(clean.index)
    signed = clean["signed_quantity"].to_numpy(dtype=float)
    prices = clean["price"].to_numpy(dtype=float)
    decision_ns = _ns(decisions)
    window = int(settings["flow_windows_seconds"][0]) * _NS
    slow_window = int(settings["flow_windows_seconds"][1]) * _NS
    right = np.searchsorted(trade_ns, decision_ns, side="right")
    left_fast = np.searchsorted(trade_ns, decision_ns - window, side="right")
    left_slow = np.searchsorted(trade_ns, decision_ns - slow_window, side="right")
    fast_count = right - left_fast
    available = fast_count > 0
    feature_exclusions = int((~available).sum())
    if feature_exclusions:
        logger.warning("H7 excluded %d decisions with no trade in the prior 60 seconds", feature_exclusions)
    kept_ns = decision_ns[available]
    kept_index = decisions[available]
    cumulative = np.concatenate((np.zeros(1, dtype=float), np.cumsum(signed)))
    signed_fast = cumulative[right[available]] - cumulative[left_fast[available]]
    count_fast = fast_count[available].astype(float)
    signed_slow = cumulative[right[available]] - cumulative[left_slow[available]]
    features = slow.loc[kept_index].copy()
    features["signed_volume_60s"] = signed_fast
    features["trade_count_60s"] = count_fast
    features["signed_volume_5m"] = signed_slow
    features = features.loc[:, list(settings["z_features"]) + list(settings["o_features"])]
    features.index.name = "timestamp"
    labels, horizon_rows = _labels(
        kept_ns,
        kept_index,
        trade_ns,
        prices,
        features["own_vol_60m"].to_numpy(dtype=float),
        settings,
    )
    snapshot_frame = snapshots if snapshots is not None else pd.DataFrame()
    snapshot_features, snapshot_matched, snapshot_identified = snapshot_ablation(
        snapshot_frame,
        kept_index,
        kind="depth_snapshots",
        max_age_seconds=int(settings["snapshot_max_age_seconds"]),
        minimum_decisions=int(settings["snapshot_minimum_decisions"]),
    )
    quality = _quality(
        rows_in=rows_in,
        rows_kept=len(clean),
        duplicate_rows=duplicate_rows,
        nonpositive_rows=nonpositive_rows,
        coverage_days=coverage,
        decisions=len(features),
        feature_exclusions=feature_exclusions,
        horizons=horizon_rows,
        depth_update_objects=depth_update_objects,
        depth_update_bytes=depth_update_bytes,
        unreadable_objects=unreadable_objects,
    )
    logger.info(
        "H7 sample decisions=%d coverage_days=%d snapshot_matched=%d identified=%s",
        len(features),
        coverage,
        snapshot_matched,
        snapshot_identified,
    )
    return RippleSample(
        features=features,
        labels=labels,
        quality=quality,
        snapshot_features=snapshot_features,
        snapshot_matched=snapshot_matched,
        snapshot_identified=snapshot_identified,
    )


def normalise_trades(trades: pd.DataFrame) -> tuple[pd.DataFrame, int, int, int]:
    """Sort the tape, count exact duplicate rows, and quarantine bad prints."""
    missing = set(_TRADE_COLUMNS) - set(trades.columns)
    if missing:
        raise ValueError(f"trades are missing columns: {sorted(missing)}")
    rows_in = int(len(trades))
    if rows_in == 0:
        logger.warning("Trade tape is empty")
        empty = pd.DataFrame(columns=["price", "quantity", "signed_quantity"])
        empty.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        return empty, 0, 0, 0
    clock = _trade_clock(trades["timestamp"])
    out_of_order = int((clock[1:] < clock[:-1]).sum()) if len(clock) > 1 else 0
    if out_of_order:
        logger.warning("Trade tape has %d out-of-order adjacent prints; sorting before use", out_of_order)
    frame = pd.DataFrame(
        {
            "timestamp": clock,
            "price": trades["price"].to_numpy(dtype=float),
            "quantity": trades["quantity"].to_numpy(dtype=float),
            "is_buyer_maker": trades["is_buyer_maker"].to_numpy(dtype=bool),
        }
    )
    duplicate_rows = int(frame.duplicated(keep="last").sum())
    if duplicate_rows:
        logger.warning("Quarantined %d exact duplicate trade rows; the last copy is kept", duplicate_rows)
        frame = frame.drop_duplicates(keep="last")
    frame = frame.sort_values("timestamp")
    bad = (
        ~np.isfinite(frame["price"].to_numpy())
        | ~np.isfinite(frame["quantity"].to_numpy())
        | (frame["price"].to_numpy() <= 0.0)
        | (frame["quantity"].to_numpy() <= 0.0)
    )
    nonpositive_rows = int(bad.sum())
    if nonpositive_rows:
        logger.warning("Quarantined %d trades with a non-positive or non-finite price or quantity", nonpositive_rows)
        frame = frame.loc[~bad]
    if frame.empty:
        logger.warning("Trade quarantine removed every print")
        empty = pd.DataFrame(columns=["price", "quantity", "signed_quantity"])
        empty.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        return empty, rows_in, duplicate_rows, nonpositive_rows
    signed = np.where(frame["is_buyer_maker"].to_numpy(), -frame["quantity"].to_numpy(), frame["quantity"].to_numpy())
    clean = pd.DataFrame(
        {
            "price": frame["price"].to_numpy(dtype=float),
            "quantity": frame["quantity"].to_numpy(dtype=float),
            "signed_quantity": signed.astype(float),
        },
        index=pd.DatetimeIndex(frame["timestamp"].to_numpy(), tz="UTC", name="timestamp"),
    )
    return clean, rows_in, duplicate_rows, nonpositive_rows


def fit_bernoulli(
    features: pd.DataFrame,
    events: pd.Series,
    *,
    name: str,
    l2: float,
    iterations: int,
    clip: float,
) -> BernoulliModel:
    """Fit a logistic probability on a binary target-before-stop event."""
    if l2 < 0.0 or iterations < 1 or not 0.0 < clip < 0.5:
        raise ValueError("logistic penalty, iterations, and clip are not usable")
    if tuple(features.columns) == ():
        raise ValueError(f"{name} logistic model needs features")
    aligned = features.reindex(events.index)
    if aligned.isna().any(axis=1).any():
        raise ValueError(f"{name} training features are missing")
    y = events.to_numpy(dtype=float)
    if len(y) < 30:
        raise ValueError(f"{name} training sample needs at least 30 events, got {len(y)}")
    if not np.isin(y, (0.0, 1.0)).all():
        raise ValueError(f"{name} events must be binary")
    if len(set(y.tolist())) < 2:
        raise RuntimeError(f"{name} target-before-stop sample has one class")
    values = aligned.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"{name} training features contain non-finite values")
    mean = values.mean(axis=0)
    scale = values.std(axis=0, ddof=0)
    if not np.isfinite(scale).all() or (scale <= 0.0).any():
        raise ValueError(f"{name} training feature has zero or non-finite scale")
    standardised = (values - mean) / scale
    design = np.column_stack([np.ones(len(standardised)), standardised])
    beta = np.zeros(design.shape[1], dtype=float)
    converged = False
    for _ in range(int(iterations)):
        eta = np.clip(design @ beta, -30.0, 30.0)
        probability = 1.0 / (1.0 + np.exp(-eta))
        weight = np.maximum(probability * (1.0 - probability), 1e-6)
        working = eta + (y - probability) / weight
        weighted = design * np.sqrt(weight)[:, None]
        target = working * np.sqrt(weight)
        gram = weighted.T @ weighted
        gram[1:, 1:] += float(l2)
        if np.linalg.matrix_rank(gram) != gram.shape[0]:
            raise RuntimeError(f"{name} logistic design is rank deficient")
        updated = np.linalg.solve(gram, weighted.T @ target)
        if float(np.max(np.abs(updated - beta))) < 1e-8:
            beta = updated
            converged = True
            break
        beta = updated
    if not converged or not np.isfinite(beta).all():
        raise RuntimeError(f"{name} logistic regression did not converge")
    return BernoulliModel(
        name=name,
        feature_names=tuple(features.columns),
        feature_mean=mean,
        feature_scale=scale,
        coefficients=np.asarray(beta, dtype=float),
        clip=float(clip),
    )


def brier_score(probability: np.ndarray, events: pd.Series | np.ndarray) -> float:
    """Mean squared error of a binary probability. Lower is better."""
    predicted = np.asarray(probability, dtype=float)
    observed = np.asarray(events, dtype=float)
    if predicted.shape != observed.shape or predicted.ndim != 1 or len(predicted) < 2:
        raise ValueError("Brier score needs at least two aligned probabilities and events")
    if not np.isfinite(predicted).all() or (predicted < 0.0).any() or (predicted > 1.0).any():
        raise ValueError("Brier probabilities must be finite and inside [0, 1]")
    if not np.isin(observed, (0.0, 1.0)).all():
        raise ValueError("Brier events must be binary")
    return float(np.mean((predicted - observed) ** 2))


def prospective_origins(
    index: pd.DatetimeIndex,
    *,
    train_days: int,
    validation_days: int,
    step_days: int,
    sealed_days: int,
) -> tuple[tuple[Origin, ...], pd.Timestamp]:
    """Day windows that finish before the unread tail."""
    if min(train_days, validation_days, step_days, sealed_days) < 1:
        raise ValueError("H7 day windows must be positive")
    clock = as_utc_index(index)
    boundary = clock.max() - pd.Timedelta(days=sealed_days)
    if boundary <= clock.min():
        raise ValueError("trade history cannot reserve the registered sealed block")
    cursor = clock.min()
    origins: list[Origin] = []
    while True:
        train_end = cursor + pd.Timedelta(days=train_days)
        validation_end = train_end + pd.Timedelta(days=validation_days)
        if validation_end > boundary:
            break
        origins.append(
            Origin(
                name=pd.Timestamp(train_end).strftime("%Y%m%d"),
                train_start=pd.Timestamp(cursor),
                train_end=pd.Timestamp(train_end),
                validation_end=pd.Timestamp(validation_end),
            )
        )
        cursor = cursor + pd.Timedelta(days=step_days)
    if not origins:
        raise ValueError("no H7 origin fits before the sealed block")
    logger.info("Built %d H7 origins before %s", len(origins), boundary.isoformat())
    return tuple(origins), pd.Timestamp(boundary)


def _label_arrays(
    decision_ns: np.ndarray,
    trade_ns: np.ndarray,
    prices: np.ndarray,
    volatility: np.ndarray,
    horizon_minutes: int,
    gap_ns: int,
    sigma: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int]:
    """Numba path scan. A gap drops the label; nothing is filled."""
    from numba import njit

    compiled = _compiled_labels()
    return compiled(decision_ns, trade_ns, prices, volatility, horizon_minutes, gap_ns, sigma)


def _compiled_labels():
    from numba import njit

    if _compiled_labels.fn is None:
        @njit(cache=True)
        def _scan(decision_ns, trade_ns, prices, volatility, horizon_minutes, gap_ns, sigma):
            count = len(decision_ns)
            returns = np.empty(count, dtype=np.float64)
            events = np.empty(count, dtype=np.float64)
            keep = np.zeros(count, dtype=np.bool_)
            gaps = 0
            endpoints = 0
            last_trade = len(trade_ns) - 1
            horizon_ns = horizon_minutes * 60 * 1_000_000_000
            root = np.sqrt(np.float64(horizon_minutes))
            for i in range(count):
                decision = decision_ns[i]
                stop = np.searchsorted(trade_ns, decision, side="right")
                horizon_end = decision + horizon_ns
                if stop <= 0 or horizon_end > trade_ns[last_trade]:
                    endpoints += 1
                    continue
                last = np.searchsorted(trade_ns, horizon_end, side="right")
                if last <= stop:
                    gaps += 1
                    continue
                previous = decision
                bad = False
                hit = -1
                entry = prices[stop - 1]
                width = volatility[i] * root * sigma
                if not np.isfinite(width) or width <= 0.0 or entry <= 0.0:
                    endpoints += 1
                    continue
                upper = entry * np.exp(width)
                lower = entry * np.exp(-width)
                for j in range(stop, last):
                    stamp = trade_ns[j]
                    if stamp - previous > gap_ns:
                        bad = True
                        break
                    previous = stamp
                    if hit < 0:
                        price = prices[j]
                        if price >= upper:
                            hit = 1
                        elif price <= lower:
                            hit = 0
                if bad or horizon_end - previous > gap_ns:
                    gaps += 1
                    continue
                terminal = prices[last - 1]
                if terminal <= 0.0 or not np.isfinite(terminal):
                    endpoints += 1
                    continue
                returns[i] = np.log(terminal) - np.log(entry)
                events[i] = 1.0 if hit == 1 else 0.0
                keep[i] = True
            return returns, events, keep, gaps, endpoints

        _compiled_labels.fn = _scan
    return _compiled_labels.fn


_compiled_labels.fn = None


def _labels(
    decision_ns: np.ndarray,
    decisions: pd.DatetimeIndex,
    trade_ns: np.ndarray,
    prices: np.ndarray,
    volatility: np.ndarray,
    settings: dict,
) -> tuple[dict[int, pd.DataFrame], tuple[HorizonExclusions, ...]]:
    labels: dict[int, pd.DataFrame] = {}
    rows: list[HorizonExclusions] = []
    gap_ns = int(settings["gap_seconds"]) * _NS
    sigma = float(settings["barrier_sigma"])
    for horizon in (int(item) for item in settings["horizons_minutes"]):
        if len(decision_ns) == 0 or len(trade_ns) == 0:
            returns = np.empty(0, dtype=float)
            events = np.empty(0, dtype=float)
            keep = np.empty(0, dtype=bool)
            gaps = 0
            endpoints = 0
        else:
            returns, events, keep, gaps, endpoints = _label_arrays(
                np.asarray(decision_ns, dtype=np.int64),
                np.asarray(trade_ns, dtype=np.int64),
                np.asarray(prices, dtype=np.float64),
                np.asarray(volatility, dtype=np.float64),
                horizon,
                gap_ns,
                sigma,
            )
        kept = decisions[np.asarray(keep, dtype=bool)]
        if gaps or endpoints:
            logger.warning(
                "H7 %d-minute labels excluded gaps=%d endpoints=%d",
                horizon,
                gaps,
                endpoints,
            )
        label = pd.DataFrame(
            {
                "log_return": np.asarray(returns, dtype=float)[np.asarray(keep, dtype=bool)],
                "target_before_stop": np.asarray(events, dtype=float)[np.asarray(keep, dtype=bool)],
            },
            index=pd.DatetimeIndex(kept, tz="UTC", name="timestamp"),
        )
        labels[horizon] = label
        rows.append(
            HorizonExclusions(
                horizon_minutes=horizon,
                gap_exclusions=gaps,
                endpoint_exclusions=endpoints,
                labels=len(label),
            )
        )
    return labels, tuple(rows)


def _snapshot_books(snapshots: pd.DataFrame) -> pd.DataFrame:
    clock = _trade_clock(snapshots["timestamp"])
    working = pd.DataFrame(
        {
            "timestamp_ns": _ns(pd.DatetimeIndex(clock)),
            "side": snapshots["side"].to_numpy(dtype=int),
            "price": snapshots["price"].to_numpy(dtype=float),
            "quantity": snapshots["quantity"].to_numpy(dtype=float),
        }
    )
    usable = (
        np.isfinite(working["price"])
        & np.isfinite(working["quantity"])
        & (working["price"] > 0.0)
        & (working["quantity"] > 0.0)
        & working["side"].isin((0, 1))
    )
    dropped = int((~usable).sum())
    if dropped:
        logger.warning("Quarantined %d snapshot levels with bad price, quantity, or side", dropped)
    working = working.loc[usable]
    rows = []
    for timestamp, book in working.groupby("timestamp_ns", sort=True):
        bids = book.loc[book["side"] == 0]
        asks = book.loc[book["side"] == 1]
        if bids.empty or asks.empty:
            continue
        bid = bids.loc[bids["price"].idxmax()]
        ask = asks.loc[asks["price"].idxmin()]
        bid_price = float(bid["price"])
        ask_price = float(ask["price"])
        bid_qty = float(bid["quantity"])
        ask_qty = float(ask["quantity"])
        if ask_price < bid_price or bid_qty + ask_qty <= 0.0:
            continue
        rows.append(
            {
                "timestamp_ns": int(timestamp),
                "spread": ask_price - bid_price,
                "imbalance": (bid_qty - ask_qty) / (bid_qty + ask_qty),
                "microprice": ask_price * bid_qty / (bid_qty + ask_qty) + bid_price * ask_qty / (bid_qty + ask_qty),
            }
        )
    return pd.DataFrame(rows)


def _quality(
    *,
    rows_in: int,
    rows_kept: int,
    duplicate_rows: int,
    nonpositive_rows: int,
    coverage_days: int,
    decisions: int,
    feature_exclusions: int,
    horizons: tuple[HorizonExclusions, ...],
    depth_update_objects: int,
    depth_update_bytes: int,
    unreadable_objects: int,
) -> RippleQuality:
    return RippleQuality(
        rows_in=rows_in,
        rows_kept=rows_kept,
        duplicate_rows=duplicate_rows,
        nonpositive_rows=nonpositive_rows,
        coverage_days=coverage_days,
        decisions=decisions,
        feature_exclusions=feature_exclusions,
        horizon_exclusions=horizons,
        depth_update_objects=depth_update_objects,
        depth_update_bytes=depth_update_bytes,
        unreadable_objects=unreadable_objects,
        clocks=("exchange",),
        depth_sequence_ids=False,
    )


def _coverage_days(index: pd.DatetimeIndex) -> int:
    if len(index) == 0:
        return 0
    return int(as_utc_index(index).normalize().nunique())


def _trade_clock(values: pd.Series) -> pd.DatetimeIndex:
    if pd.api.types.is_datetime64_any_dtype(values):
        clock = pd.DatetimeIndex(pd.to_datetime(values, utc=True))
    else:
        numeric = pd.to_numeric(values, errors="coerce")
        if numeric.isna().any():
            raise ValueError("trade timestamps must be datetimes or epoch milliseconds")
        clock = pd.DatetimeIndex(pd.to_datetime(numeric.to_numpy(dtype="int64"), unit="ms", utc=True))
    if clock.tz is None or not (pd.Index(clock.strftime("%z")) == "+0000").all():
        raise ValueError("trade timestamps must be UTC")
    return clock


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    clock = pd.DatetimeIndex(index)
    if clock.tz is None:
        raise ValueError("nanosecond conversion requires UTC timestamps")
    converted = clock.tz_convert("UTC")
    try:
        converted = converted.as_unit("ns")
    except AttributeError:
        converted = pd.DatetimeIndex(converted.asi8, tz="UTC")
    return np.asarray(converted.asi8, dtype=np.int64)
