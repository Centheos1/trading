"""Notebook-facing evaluation. The sealed test is not read here."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from research.baselines import (
    apply_costs,
    buy_and_hold_position,
    consecutive_simple_returns,
    mean_reversion_position,
    momentum_position,
    performance,
    volatility_scale,
)
from research.clock import as_utc_index
from research.distributions import bonferroni, fit_gaussian, fit_student_t, score_distribution
from research.manifest import SCHEMA_VERSION, DatasetManifest, DatasetObject, build_manifest
from research.protocol import BASELINE_CONFIG, H1_CONFIG, H2_CONFIG
from research.quality import QualityReport, audit_minute_bars
from research.registry import ExperimentRecord
from research.regimes import (
    clock_gap_resets,
    fit_clocked_gaussian_hmm,
    fit_regime_densities,
    filtered_probability_frame,
    h1_benchmark,
    minute_returns_known_at_close,
    score_regime_forecast,
)
from research.returns import horizon_log_returns
from research.splits import development_returns, rolling_origins, sealed_boundary, select_available

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class H1Evaluation:
    """Development scores only. ``sealed_access`` stays false."""

    scores: pd.DataFrame
    pit_qq: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    sealed_access: bool = False


def freeze_dataset(
    bars: pd.DataFrame,
    objects: tuple[DatasetObject, ...] | list[DatasetObject],
    *,
    dataset_id: str,
    venue: str,
    symbol: str,
    timeframe: str,
    bucket: str,
    profile: str,
) -> tuple[pd.DataFrame, DatasetManifest, QualityReport]:
    """Audit bars and bind the surviving sample to the listed stored objects."""
    kept, report = audit_minute_bars(bars)
    if kept.empty:
        raise RuntimeError("dataset audit kept no rows")
    manifest = build_manifest(
        dataset_id=dataset_id,
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        bucket=bucket,
        profile=profile,
        objects=objects,
        start=kept.index[0].isoformat(),
        end=kept.index[-1].isoformat(),
        exclusions=report.exclusions,
    )
    logger.info(
        "Froze dataset %s digest=%s rows=%d",
        manifest.dataset_id,
        manifest.digest,
        len(kept),
    )
    return kept, manifest, report


def require_frozen_dataset(frozen: DatasetManifest, rebuilt: DatasetManifest) -> None:
    """Stop when a reload is not the dataset the experiment froze."""
    if frozen.digest != rebuilt.digest:
        changes = _dataset_changes(frozen, rebuilt)
        detail = "; ".join(changes) if changes else "canonical payload matches but the recorded digests differ"
        raise RuntimeError(
            f"rebuilt dataset {rebuilt.digest} does not match frozen dataset {frozen.digest}: {detail}"
        )


def align_to_frozen_dataset(
    bars: pd.DataFrame,
    frozen: DatasetManifest,
    objects: tuple[DatasetObject, ...] | list[DatasetObject],
) -> tuple[pd.DataFrame, QualityReport, int]:
    """Return the frozen sample, leaving out minutes appended after the freeze.

    The collector rewrites the open year object as it appends. That object's
    etag and size move, and the loaded series ends later than the manifest.
    Rows after the frozen end are counted, logged, and excluded. The returned
    bars keep the frozen start, end, and exclusion counts. A closed object
    that changes, an open object that shrinks, or an audited window that no
    longer matches the manifest still raises.
    """
    if bars.empty:
        raise RuntimeError("loaded bars are empty")
    frame = bars.copy()
    frame.index = as_utc_index(bars.index)
    frame = frame.sort_index()
    end = _bound_timestamp(frozen.end)
    later_mask = frame.index > end
    appended = int(later_mask.sum())
    if appended == 0:
        kept, rebuilt, report = freeze_dataset(
            frame,
            objects,
            dataset_id=frozen.dataset_id,
            venue=frozen.venue,
            symbol=frozen.symbol,
            timeframe=frozen.timeframe,
            bucket=frozen.bucket,
            profile=frozen.profile,
        )
        require_frozen_dataset(frozen, rebuilt)
        return kept, report, 0

    notes = _collector_append_notes(frozen, tuple(objects), frame.index[later_mask])
    window = frame.loc[~later_mask]
    if window.empty:
        raise RuntimeError(f"every loaded row is after frozen end {frozen.end}")
    kept, report = audit_minute_bars(window)
    mismatches = _frozen_window_changes(frozen, kept, report)
    if mismatches:
        detail = "; ".join(mismatches)
        raise RuntimeError(
            f"collector append after {frozen.end} leaves a different frozen window: {detail}"
        )
    logger.warning(
        "Excluded %d collector rows after frozen end %s; %s",
        appended,
        frozen.end,
        "; ".join(notes),
    )
    return kept, report, appended


def _bound_timestamp(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError(f"dataset bound {value} is naive; research clocks must be UTC")
    return stamp.tz_convert("UTC")


def _frozen_window_changes(
    frozen: DatasetManifest,
    kept: pd.DataFrame,
    report: QualityReport,
) -> tuple[str, ...]:
    changes: list[str] = []
    start = kept.index[0].isoformat()
    end = kept.index[-1].isoformat()
    if start != frozen.start:
        changes.append(f"start {frozen.start} -> {start}")
    if end != frozen.end:
        changes.append(f"end {frozen.end} -> {end}")
    if frozen.schema_version != SCHEMA_VERSION:
        changes.append(f"schema_version {frozen.schema_version} -> {SCHEMA_VERSION}")
    if report.exclusions != frozen.exclusions:
        changes.append(f"exclusions {frozen.exclusions} -> {report.exclusions}")
    return tuple(changes)


def _collector_append_notes(
    frozen: DatasetManifest,
    objects: tuple[DatasetObject, ...],
    later_index: pd.DatetimeIndex,
) -> tuple[str, ...]:
    """Name append-only object changes. Any other change raises."""
    end = _bound_timestamp(frozen.end)
    open_key = _open_collector_key(frozen, end.year)
    frozen_objects = {item.key: item for item in frozen.objects}
    live_objects = {item.key: item for item in objects}
    if len(live_objects) != len(objects):
        raise RuntimeError("loaded objects contain a duplicate key")
    for key, old in frozen_objects.items():
        new = live_objects.get(key)
        if new is None:
            raise RuntimeError(f"rebuilt dataset removed object {key}")
        if key != open_key and new != old:
            raise RuntimeError(
                f"{key} changed and is not the open collector year: "
                f"etag {old.etag} -> {new.etag}, size {old.size} -> {new.size}"
            )
    open_old = frozen_objects[open_key]
    open_new = live_objects[open_key]
    later_years = {int(stamp.year) for stamp in later_index}
    notes: list[str] = []
    if end.year in later_years:
        if open_new.size <= open_old.size:
            raise RuntimeError(
                f"{open_key} must grow to hold rows after {frozen.end}, "
                f"size {open_old.size} -> {open_new.size}"
            )
        notes.append(
            f"{open_key} etag {open_old.etag} -> {open_new.etag}, size {open_old.size} -> {open_new.size}"
        )
    elif open_new != open_old:
        raise RuntimeError(
            f"{open_key} changed without rows after {frozen.end} in {end.year}: "
            f"etag {open_old.etag} -> {open_new.etag}, size {open_old.size} -> {open_new.size}"
        )
    added_years: dict[int, str] = {}
    for key in live_objects:
        if key in frozen_objects:
            continue
        year = _later_collector_year(key, frozen, end.year)
        if year is None:
            raise RuntimeError(f"added object {key} is not a collector year after {end.year}")
        added_years[year] = key
        notes.append(f"added collector object {key}")
    for year in sorted(later_years):
        if year == end.year:
            continue
        if year < end.year:
            raise RuntimeError(f"rows after {frozen.end} have year {year}")
        if year not in added_years:
            raise RuntimeError(
                f"rows in {year} are after the freeze but no {year} collector object was added"
            )
    for year, key in added_years.items():
        if year not in later_years:
            raise RuntimeError(f"added collector object {key} has no rows after {frozen.end}")
    if not notes:
        raise RuntimeError(
            f"loaded bars include rows after {frozen.end} but the stored objects match the freeze"
        )
    return tuple(notes)


def _open_collector_key(frozen: DatasetManifest, year: int) -> str:
    suffix = f"/{frozen.timeframe}/{year}.parquet"
    matches = [item.key for item in frozen.objects if item.key.endswith(suffix)]
    if len(matches) != 1:
        raise RuntimeError(
            f"frozen dataset {frozen.dataset_id} needs one {year} collector object "
            f"ending in {suffix}, found {matches}"
        )
    return matches[0]


def _later_collector_year(key: str, frozen: DatasetManifest, frozen_year: int) -> int | None:
    prefix = f"ohlcv/{frozen.venue}/{frozen.symbol}/{frozen.timeframe}/"
    suffix = ".parquet"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    token = key[len(prefix) : -len(suffix)]
    if len(token) != 4 or not token.isdigit():
        return None
    year = int(token)
    if year <= frozen_year:
        return None
    return year


def _dataset_changes(frozen: DatasetManifest, rebuilt: DatasetManifest) -> tuple[str, ...]:
    """Name the manifest fields that differ. The digest itself is not one of them."""
    changes: list[str] = []
    for label in ("start", "end", "schema_version"):
        old = getattr(frozen, label)
        new = getattr(rebuilt, label)
        if old != new:
            changes.append(f"{label} {old} -> {new}")
    if frozen.exclusions != rebuilt.exclusions:
        changes.append(f"exclusions {frozen.exclusions} -> {rebuilt.exclusions}")
    old_objects = {item.key: item for item in frozen.objects}
    new_objects = {item.key: item for item in rebuilt.objects}
    for key in sorted(set(old_objects) | set(new_objects)):
        old = old_objects.get(key)
        new = new_objects.get(key)
        if old is None:
            changes.append(f"added object {key}")
        elif new is None:
            changes.append(f"removed object {key}")
        elif old != new:
            changes.append(f"{key} etag {old.etag} -> {new.etag}, size {old.size} -> {new.size}")
    return tuple(changes)


def evaluate_baselines(close: pd.Series, config: dict | None = None) -> pd.DataFrame:
    """Score the pre-registered baselines on the development sample only."""
    settings = dict(BASELINE_CONFIG)
    if config:
        settings.update(config)
    clock = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=clock).sort_index()
    boundary = sealed_boundary(values.index, months=int(settings["sealed_months"]))
    visible = values.loc[values.index + pd.Timedelta(minutes=1) < boundary]
    if len(visible) < 3:
        raise RuntimeError("baseline development sample has fewer than three closes")
    returns, excluded = consecutive_simple_returns(visible)
    lookback = int(settings["lookback_minutes"])
    scale = volatility_scale(
        visible,
        lookback,
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    positions = {
        "buy_and_hold": buy_and_hold_position(returns.index),
        "momentum": momentum_position(visible, lookback).reindex(returns.index).fillna(0.0),
        "mean_reversion": mean_reversion_position(visible, lookback).reindex(returns.index).fillna(0.0),
        "volatility_targeted_long": scale.reindex(returns.index).fillna(0.0),
    }
    missing_scale = int(scale.reindex(returns.index).isna().sum())
    if excluded or missing_scale:
        logger.warning(
            "Baseline exclusions: nonconsecutive_returns=%d missing_volatility_scale=%d",
            excluded,
            missing_scale,
        )
    rows = []
    for name, position in positions.items():
        net = apply_costs(position, returns, float(settings["cost_bps"]))
        result = performance(net, position)
        rows.append({"baseline": name, "sealed_access": False, **result.__dict__})
    logger.info("Scored %d baselines before %s", len(rows), boundary.isoformat())
    return pd.DataFrame(rows)


def evaluate_h1(close: pd.Series, config: dict | None = None) -> H1Evaluation:
    """Fit Gaussian and Student-t models on rolling development windows.

    The sealed calendar block is used only to draw the boundary. No sealed
    label is scored.
    """
    settings = dict(H1_CONFIG)
    if config:
        settings.update(config)
    clock = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=clock, name="close").sort_index()
    boundary = sealed_boundary(values.index, months=int(settings["sealed_months"]))
    origins = rolling_origins(
        values.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    rows: list[dict] = []
    qq_rows: list[dict] = []
    failures: list[dict] = []
    for horizon in settings["horizons_minutes"]:
        built = horizon_log_returns(values, int(horizon))
        development, held_out = development_returns(built.returns, int(horizon), boundary)
        if held_out == 0:
            logger.warning("Horizon %s held no labels out for the sealed test", horizon)
        for origin in origins:
            train = select_available(development, int(horizon), origin.train_start, origin.train_end)
            validation = select_available(
                development, int(horizon), origin.train_end, origin.validation_end
            )
            if len(train) < 30 or len(validation) < 30:
                failures.append(
                    {
                        "horizon": int(horizon),
                        "origin": origin.name,
                        "reason": f"train={len(train)} validation={len(validation)}",
                    }
                )
                logger.warning(
                    "Skipped H1 origin %s horizon %s: train=%d validation=%d",
                    origin.name,
                    horizon,
                    len(train),
                    len(validation),
                )
                continue
            try:
                gaussian = fit_gaussian(train.to_numpy())
                student = fit_student_t(train.to_numpy())
            except (ValueError, RuntimeError) as exc:
                failures.append({"horizon": int(horizon), "origin": origin.name, "reason": str(exc)})
                logger.error("H1 fit failed origin=%s horizon=%s: %s", origin.name, horizon, exc)
                continue
            for model in (gaussian, student):
                score = score_distribution(model, validation.to_numpy(), alpha=float(settings["var_alpha"]))
                rows.append(
                    {
                        "horizon_minutes": int(horizon),
                        "origin": origin.name,
                        "model": score.model,
                        "observations": score.observations,
                        "mean_log_score": score.mean_log_score,
                        "pit_ks_pvalue": score.pit_ks_pvalue,
                        "var_exceedance_rate": score.var_exceedance_rate,
                        "var_pvalue": score.var_pvalue,
                        "expected_shortfall": score.expected_shortfall,
                        "realised_shortfall": score.realised_shortfall,
                        "sealed_access": False,
                    }
                )
                if len(score.pit_qq_theoretical) != len(score.pit_qq_empirical):
                    raise RuntimeError(f"{score.model} PIT QQ axes have different lengths")
                for theoretical, empirical in zip(score.pit_qq_theoretical, score.pit_qq_empirical):
                    qq_rows.append(
                        {
                            "horizon_minutes": int(horizon),
                            "origin": origin.name,
                            "model": score.model,
                            "theoretical": theoretical,
                            "empirical": empirical,
                            "sealed_access": False,
                        }
                    )
    if failures:
        logger.warning("H1 recorded %d skipped or failed windows", len(failures))
    if not rows:
        raise RuntimeError(f"H1 produced no development scores; failures={len(failures)}")
    scores = pd.DataFrame(rows)
    pit_qq = pd.DataFrame(qq_rows)
    if pit_qq.empty:
        raise RuntimeError("H1 produced scores without PIT QQ curves")
    logger.info(
        "H1 development scores=%d pit_qq=%d failures=%d sealed_access=false boundary=%s",
        len(scores),
        len(pit_qq),
        len(failures),
        boundary.isoformat(),
    )
    return H1Evaluation(
        scores=scores,
        pit_qq=pit_qq,
        failures=tuple(failures),
        boundary=boundary,
        sealed_access=False,
    )


@dataclass(frozen=True)
class H2Evaluation:
    """Development scores only. The sealed test is not read."""

    scores: pd.DataFrame
    pit_qq: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    benchmark: str
    benchmark_resolved: bool
    gap_resets: int
    alignment_exclusions: int
    sealed_access: bool = False


def evaluate_h2(
    close: pd.Series,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> H2Evaluation:
    """Compare filtered regime mixtures with the unconditional H1 benchmark.

    Parameters are fit on each training window. Validation probabilities are
    the forward filter of the frozen training model. No sealed label is scored,
    and an unresolved H1 record cannot make this a confirmatory result.
    """
    settings = dict(H2_CONFIG)
    if config:
        settings.update(config)
    _require_h2_protocol(settings)
    if tuple(int(item) for item in settings["horizons_minutes"]) != tuple(int(item) for item in h1_record.horizons):
        raise ValueError(
            f"H2 horizons {list(settings['horizons_minutes'])} do not match H1 horizons {list(h1_record.horizons)}"
        )
    benchmark = h1_benchmark(h1_record)
    if not benchmark.resolved:
        logger.warning("H2 benchmark is unresolved: %s", benchmark.reason)
    clock = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=clock, name="close").sort_index()
    boundary = sealed_boundary(values.index, months=int(settings["sealed_months"]))
    minute_returns, minute_build = minute_returns_known_at_close(values)
    if minute_build.gap_exclusions or minute_build.nonpositive_exclusions:
        logger.warning(
            "Minute information set excluded %d gaps and %d non-positive returns",
            minute_build.gap_exclusions,
            minute_build.nonpositive_exclusions,
        )
    origins = rolling_origins(
        values.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    rows: list[dict] = []
    qq_rows: list[dict] = []
    failures: list[dict] = []
    alignment_exclusions = 0
    for origin in origins:
        train_minutes = minute_returns.loc[
            (minute_returns.index >= origin.train_start) & (minute_returns.index < origin.train_end)
        ]
        span = minute_returns.loc[
            (minute_returns.index >= origin.train_start) & (minute_returns.index < origin.validation_end)
        ]
        if len(train_minutes) < 30 or len(span) < 30:
            failures.append(
                {
                    "horizon": None,
                    "origin": origin.name,
                    "reason": f"train_minutes={len(train_minutes)} span={len(span)}",
                }
            )
            logger.warning(
                "Skipped H2 origin %s: train_minutes=%d span=%d",
                origin.name,
                len(train_minutes),
                len(span),
            )
            continue
        try:
            fitted = fit_clocked_gaussian_hmm(
                train_minutes,
                max_iterations=int(settings["em_iterations"]),
                tolerance=float(settings["em_tolerance"]),
            )
            probabilities, _ = filtered_probability_frame(fitted.model, span)
        except (ValueError, RuntimeError) as exc:
            failures.append({"horizon": None, "origin": origin.name, "reason": str(exc)})
            logger.error("H2 regime fit failed origin=%s: %s", origin.name, exc)
            continue
        for horizon in settings["horizons_minutes"]:
            built = horizon_log_returns(values, int(horizon))
            development, held_out = development_returns(built.returns, int(horizon), boundary)
            if held_out == 0:
                logger.warning("Horizon %s held no labels out for the sealed test", horizon)
            train = select_available(development, int(horizon), origin.train_start, origin.train_end)
            validation = select_available(
                development, int(horizon), origin.train_end, origin.validation_end
            )
            train_labels, train_weights, train_dropped = _align_probabilities(probabilities, train)
            validation_labels, validation_weights, validation_dropped = _align_probabilities(
                probabilities, validation
            )
            alignment_exclusions += train_dropped + validation_dropped
            if len(train_labels) < 30 or len(validation_labels) < 30:
                failures.append(
                    {
                        "horizon": int(horizon),
                        "origin": origin.name,
                        "reason": f"train={len(train_labels)} validation={len(validation_labels)}",
                    }
                )
                logger.warning(
                    "Skipped H2 origin %s horizon %s: train=%d validation=%d",
                    origin.name,
                    horizon,
                    len(train_labels),
                    len(validation_labels),
                )
                continue
            try:
                if benchmark.model == "gaussian":
                    unconditional = fit_gaussian(train_labels.to_numpy())
                else:
                    unconditional = fit_student_t(train_labels.to_numpy())
                components = fit_regime_densities(
                    train_labels.to_numpy(), train_weights, benchmark.model
                )
            except (ValueError, RuntimeError) as exc:
                failures.append({"horizon": int(horizon), "origin": origin.name, "reason": str(exc)})
                logger.error(
                    "H2 density fit failed origin=%s horizon=%s: %s", origin.name, horizon, exc
                )
                continue
            mixture = score_regime_forecast(
                components,
                validation_weights,
                validation_labels.to_numpy(),
                alpha=float(settings["var_alpha"]),
            )
            baseline = score_distribution(
                unconditional,
                validation_labels.to_numpy(),
                alpha=float(settings["var_alpha"]),
            )
            for score in (baseline, mixture):
                rows.append(
                    {
                        "horizon_minutes": int(horizon),
                        "origin": origin.name,
                        "model": score.model,
                        "benchmark": benchmark.model,
                        "benchmark_resolved": benchmark.resolved,
                        "observations": score.observations,
                        "mean_log_score": score.mean_log_score,
                        "pit_ks_pvalue": score.pit_ks_pvalue,
                        "var_exceedance_rate": score.var_exceedance_rate,
                        "var_pvalue": score.var_pvalue,
                        "expected_shortfall": score.expected_shortfall,
                        "realised_shortfall": score.realised_shortfall,
                        "sealed_access": False,
                    }
                )
                if len(score.pit_qq_theoretical) != len(score.pit_qq_empirical):
                    raise RuntimeError(f"{score.model} PIT QQ axes have different lengths")
                for theoretical, empirical in zip(score.pit_qq_theoretical, score.pit_qq_empirical):
                    qq_rows.append(
                        {
                            "horizon_minutes": int(horizon),
                            "origin": origin.name,
                            "model": score.model,
                            "theoretical": theoretical,
                            "empirical": empirical,
                            "sealed_access": False,
                        }
                    )
    if failures:
        logger.warning("H2 recorded %d skipped or failed windows", len(failures))
    if not rows:
        raise RuntimeError(f"H2 produced no development scores; failures={len(failures)}")
    scores = pd.DataFrame(rows)
    pit_qq = pd.DataFrame(qq_rows)
    if pit_qq.empty:
        raise RuntimeError("H2 produced scores without PIT QQ curves")
    logger.info(
        "H2 development scores=%d benchmark=%s resolved=%s failures=%d sealed_access=false boundary=%s",
        len(scores),
        benchmark.model,
        benchmark.resolved,
        len(failures),
        boundary.isoformat(),
    )
    return H2Evaluation(
        scores=scores,
        pit_qq=pit_qq,
        failures=tuple(failures),
        boundary=boundary,
        benchmark=benchmark.model,
        benchmark_resolved=benchmark.resolved,
        gap_resets=clock_gap_resets(minute_returns.index),
        alignment_exclusions=int(alignment_exclusions),
        sealed_access=False,
    )


def _require_h2_protocol(settings: dict) -> None:
    if settings.get("state_model") != "two_state_gaussian_hmm":
        raise ValueError(f"H2 state model must be two_state_gaussian_hmm, got {settings.get('state_model')!r}")
    if settings.get("probability") != "filtered":
        raise ValueError(f"H2 probabilities must be filtered, got {settings.get('probability')!r}")
    if int(settings.get("states", 0)) != 2:
        raise ValueError(f"H2 is pre-registered with two states, got {settings.get('states')!r}")
    if settings.get("forecast") != "density":
        raise ValueError(f"H2 forecast must be density, got {settings.get('forecast')!r}")
    if settings.get("first_passage") != "not_scored":
        raise ValueError(
            "H2 does not score first-passage until a barrier definition is pre-registered, "
            f"got {settings.get('first_passage')!r}"
        )


def _align_probabilities(probabilities: pd.DataFrame, labels: pd.Series) -> tuple[pd.Series, np.ndarray, int]:
    aligned = probabilities.reindex(labels.index)
    missing = aligned.isna().any(axis=1)
    dropped = int(missing.sum())
    if dropped:
        logger.warning("Dropped %d labels with no filtered regime probability", dropped)
    kept_labels = labels.loc[~missing]
    kept_weights = aligned.loc[~missing].to_numpy(dtype=float)
    return kept_labels, kept_weights, dropped


def h2_log_score_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Average regime-mixture minus unconditional log score for each horizon."""
    required = {"horizon_minutes", "origin", "model", "benchmark", "mean_log_score"}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H2 scores are missing columns: {sorted(missing)}")
    benchmarks = scores["benchmark"].dropna().unique()
    if len(benchmarks) != 1:
        raise RuntimeError(f"H2 comparison needs one benchmark, found {list(benchmarks)}")
    benchmark = str(benchmarks[0])
    pivot = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="mean_log_score",
        aggfunc="first",
    )
    if not {"regime_mixture", benchmark}.issubset(pivot.columns):
        raise RuntimeError(f"H2 comparison needs regime_mixture and {benchmark}, found {list(pivot.columns)}")
    advantage = (pivot["regime_mixture"] - pivot[benchmark]).rename("regime_minus_benchmark")
    summary = advantage.groupby("horizon_minutes").agg(["mean", "median", "count"])
    summary.columns = ["mean_advantage", "median_advantage", "origins"]
    return summary.reset_index()


def h1_log_score_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Average Student-t minus Gaussian log score for each horizon."""
    required = {"horizon_minutes", "origin", "model", "mean_log_score"}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H1 scores are missing columns: {sorted(missing)}")
    pivot = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="mean_log_score",
        aggfunc="first",
    )
    if not {"gaussian", "student_t"}.issubset(pivot.columns):
        raise RuntimeError(f"H1 comparison needs both models, found {list(pivot.columns)}")
    advantage = (pivot["student_t"] - pivot["gaussian"]).rename("student_minus_gaussian")
    summary = advantage.groupby("horizon_minutes").agg(["mean", "median", "count"])
    summary.columns = ["mean_advantage", "median_advantage", "origins"]
    return summary.reset_index()


def h1_development_decision(comparison: pd.DataFrame) -> pd.DataFrame:
    """Accept or reject the H1 null from the development log-score family.

    Reject the null when Student-t has a positive mean and median advantage
    at every horizon. Otherwise accept it. The sealed test is not involved,
    so this cannot retain the experiment.
    """
    required = {"horizon_minutes", "mean_advantage", "median_advantage", "origins"}
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H1 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H1 development decision has no horizons")
    if comparison["horizon_minutes"].duplicated().any():
        raise ValueError("H1 development decision has a repeated horizon")
    if (comparison["origins"] < 1).any():
        raise ValueError("H1 development decision needs at least one origin at every horizon")
    advantages = comparison[["mean_advantage", "median_advantage"]]
    if not advantages.map(math.isfinite).all().all():
        raise ValueError("H1 log-score advantages must be finite")
    beats_gaussian = (comparison["mean_advantage"] > 0.0) & (comparison["median_advantage"] > 0.0)
    if bool(beats_gaussian.all()):
        null_decision = "reject"
        benchmark = "student_t"
        reason = "Student-t log score beats Gaussian at every horizon"
    else:
        null_decision = "accept"
        benchmark = "gaussian"
        reason = "Student-t log score does not beat Gaussian at every horizon"
    return pd.DataFrame(
        [{"null_decision": null_decision, "benchmark": benchmark, "reason": reason}]
    )


def h1_var_coverage_family(scores: pd.DataFrame) -> pd.DataFrame:
    """Median VaR coverage p-value for each horizon and model, Bonferroni-adjusted.

    The family is the pre-registered horizon-by-model grid. The adjustment
    uses that grid, so a missing model must not shrink the penalty.
    """
    required = {"horizon_minutes", "model", "var_pvalue"}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H1 scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError("H1 VaR family has no scores")
    pvalues = scores["var_pvalue"]
    if not pd.api.types.is_numeric_dtype(pvalues) or not pvalues.map(_finite_pvalue).all():
        raise ValueError("H1 VaR p-values must be finite numbers in [0, 1]")
    models = set(scores["model"].unique())
    if models != {"gaussian", "student_t"}:
        raise RuntimeError(f"H1 VaR family needs both models, found {sorted(models)}")
    family = scores.groupby(["horizon_minutes", "model"])["var_pvalue"].median().sort_index()
    horizons = family.index.get_level_values("horizon_minutes").unique()
    for horizon in horizons:
        present = set(family.loc[horizon].index)
        if present != {"gaussian", "student_t"}:
            raise RuntimeError(f"H1 VaR family at {horizon} minutes is missing a model: {sorted(present)}")
    if not family.map(_finite_pvalue).all():
        raise RuntimeError("H1 VaR family contains a non-finite median p-value")
    table = family.rename("median_var_pvalue").reset_index()
    table["bonferroni_var_pvalue"] = bonferroni(table["median_var_pvalue"].tolist())
    return table


def _finite_pvalue(value: object) -> bool:
    """True when a coverage p-value can enter the Bonferroni family."""
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and 0.0 <= number <= 1.0
