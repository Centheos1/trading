"""Notebook-facing evaluation.

Development evaluators do not read the sealed test. ``confirm_h1`` and
the hypothesis-specific confirmation functions are the only functions here
that do, and only after the code revision is committed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

from research.allocation import (
    CANDIDATE,
    CONTROLS,
    DEFAULT_SIZER,
    SIZERS,
    directional_opportunity,
    rorac_ratio,
    size_position,
    training_rorac_normaliser,
)
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
from research.factors import (
    align_close_panel,
    external_market_features,
    fit_conditional_density,
    fit_factor_state,
    own_price_features,
    panel_minute_returns,
    score_conditional_density,
)
from research.manifest import (
    SCHEMA_VERSION,
    DatasetManifest,
    DatasetObject,
    PanelManifest,
    build_manifest,
    build_panel_manifest,
    verify_manifest,
    verify_panel_manifest,
)
from research.protocol import (
    BASELINE_CONFIG,
    H1_CONFIG,
    H2_CONFIG,
    H3_CONFIG,
    H4_CONFIG,
    H5_CONFIG,
    H6_CONFIG,
    H7_CONFIG,
    H20_CONFIG,
)
from research.quality import QualityReport, audit_minute_bars
from research.registry import (
    ExperimentRecord,
    code_version,
    config_digest,
    load_record,
    make_run_id,
    require_committed,
    save_record,
)
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
from research.ripple import (
    RippleQuality,
    brier_score,
    build_ripple_sample,
    fit_bernoulli,
    prospective_origins,
    require_h7_protocol,
)
from research.splits import (
    Origin,
    development_returns,
    rolling_origins,
    sealed_boundary,
    sealed_returns,
    select_available,
)
from research.tide import (
    POLICIES,
    apply_entry_constraint,
    causal_volatility_ratio,
    fit_risk_calibration,
    historical_expected_shortfall,
    holm_adjust,
    liquidation_event_count,
    one_sided_sign_pvalue,
)
from research.wave import (
    FirstPassageForecast,
    build_barrier_labels,
    first_passage_signals,
    fit_first_passage,
    fit_terminal_density,
    normalise_ohlc,
    score_probability,
    select_label_window,
    terminal_signals,
    terminal_up_probability,
)

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


def freeze_panel_dataset(
    bars_by_symbol: dict[str, pd.DataFrame],
    objects_by_symbol: dict[str, tuple[DatasetObject, ...] | list[DatasetObject]],
    *,
    dataset_id: str,
    symbols: tuple[str, ...],
    venue: str,
    timeframe: str,
    bucket: str,
    profile: str,
    venues: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, PanelManifest, dict[str, QualityReport]]:
    """Audit each member, inner-align closes, and freeze the complete-case panel."""
    if set(bars_by_symbol) != set(symbols) or set(objects_by_symbol) != set(symbols):
        raise ValueError("panel bars and objects must exactly match the declared symbols")
    if venues is not None and set(venues) != set(symbols):
        raise ValueError("panel venues must exactly match the declared symbols")
    members: list[DatasetManifest] = []
    reports: dict[str, QualityReport] = {}
    closes: dict[str, pd.Series] = {}
    for symbol in symbols:
        member_venue = venues[symbol] if venues is not None else venue
        kept, member, report = freeze_dataset(
            bars_by_symbol[symbol],
            objects_by_symbol[symbol],
            dataset_id=f"{dataset_id}-{symbol.lower()}",
            venue=member_venue,
            symbol=symbol,
            timeframe=timeframe,
            bucket=bucket,
            profile=profile,
        )
        if "close" not in kept:
            raise ValueError(f"{symbol} audited bars do not contain close")
        members.append(member)
        reports[symbol] = report
        closes[symbol] = kept["close"]
    alignment = align_close_panel(closes, symbols)
    panel = build_panel_manifest(
        dataset_id=dataset_id,
        members=members,
        start=alignment.closes.index[0].isoformat(),
        end=alignment.closes.index[-1].isoformat(),
        alignment_exclusions=alignment.dropped_timestamps,
        allow_cross_venue=venues is not None and len(set(venues.values())) > 1,
    )
    logger.info(
        "Froze panel dataset %s digest=%s rows=%d alignment_exclusions=%d",
        panel.dataset_id,
        panel.digest,
        len(alignment.closes),
        panel.alignment_exclusions,
    )
    return alignment.closes, panel, reports


def require_frozen_dataset(frozen: DatasetManifest, rebuilt: DatasetManifest) -> None:
    """Stop when a reload is not the dataset the experiment froze."""
    if frozen.digest != rebuilt.digest:
        changes = _dataset_changes(frozen, rebuilt)
        detail = "; ".join(changes) if changes else "canonical payload matches but the recorded digests differ"
        raise RuntimeError(
            f"rebuilt dataset {rebuilt.digest} does not match frozen dataset {frozen.digest}: {detail}"
        )


def align_to_frozen_panel(
    bars_by_symbol: dict[str, pd.DataFrame],
    frozen: PanelManifest,
    objects_by_symbol: dict[str, tuple[DatasetObject, ...] | list[DatasetObject]],
) -> tuple[pd.DataFrame, dict[str, QualityReport], dict[str, int]]:
    """Validate each live member against its freeze and rebuild the aligned panel."""
    verify_panel_manifest(frozen)
    symbols = frozen.symbols
    if set(bars_by_symbol) != set(symbols) or set(objects_by_symbol) != set(symbols):
        raise ValueError("loaded panel must exactly match the frozen symbols")
    closes: dict[str, pd.Series] = {}
    reports: dict[str, QualityReport] = {}
    appended: dict[str, int] = {}
    members = {member.symbol: member for member in frozen.members}
    for symbol in symbols:
        kept, report, later = align_to_frozen_dataset(
            bars_by_symbol[symbol],
            members[symbol],
            objects_by_symbol[symbol],
        )
        if "close" not in kept:
            raise ValueError(f"{symbol} frozen bars do not contain close")
        closes[symbol] = kept["close"]
        reports[symbol] = report
        appended[symbol] = later
    alignment = align_close_panel(closes, symbols)
    changes = []
    if alignment.closes.index[0].isoformat() != frozen.start:
        changes.append(f"start {frozen.start} -> {alignment.closes.index[0].isoformat()}")
    if alignment.closes.index[-1].isoformat() != frozen.end:
        changes.append(f"end {frozen.end} -> {alignment.closes.index[-1].isoformat()}")
    if alignment.dropped_timestamps != frozen.alignment_exclusions:
        changes.append(
            f"alignment_exclusions {frozen.alignment_exclusions} "
            f"-> {alignment.dropped_timestamps}"
        )
    if changes:
        raise RuntimeError(f"loaded panel does not match frozen panel: {'; '.join(changes)}")
    return alignment.closes, reports, appended


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
class H1Confirmation:
    """One sealed H1 comparison and the registry record it wrote."""

    scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp


def confirm_h1(
    close: pd.Series,
    record: ExperimentRecord,
    *,
    dataset_id: str,
    dataset_digest: str,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H1Confirmation:
    """Fit H1 on the development sample and score the sealed year once.

    The model is frozen before any sealed label is read. A dirty tree, a
    second read, or a dataset other than the recorded freeze raises before
    that read. The same log-score rule as development decides retain or reject.
    """
    version = require_committed(code_version(root))
    _require_open_h1(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h1(stored)
    digest = _sha256_digest(dataset_digest)
    if not dataset_id:
        raise ValueError("confirmation needs a dataset id")
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation dataset {digest} does not match the recorded freeze {stored.dataset_digest}"
        )
    settings = dict(H1_CONFIG)
    if config:
        settings.update(config)
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(int(item) for item in stored.horizons):
        raise ValueError(
            f"confirmation horizons {list(horizons)} do not match H1 horizons {list(stored.horizons)}"
        )
    clock = as_utc_index(close.index)
    values = pd.Series(close.to_numpy(dtype=float), index=clock, name="close").sort_index()
    boundary = sealed_boundary(values.index, months=int(settings["sealed_months"]))
    rows: list[dict] = []
    for horizon in horizons:
        built = horizon_log_returns(values, horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            raise RuntimeError(f"horizon {horizon} held no labels out for the sealed test")
        if len(development) < 30:
            raise RuntimeError(f"horizon {horizon} development sample has {len(development)} labels")
        sealed = sealed_returns(built.returns, horizon, boundary, confirmatory=True)
        if len(sealed) < 30:
            raise RuntimeError(f"horizon {horizon} sealed sample has {len(sealed)} labels")
        gaussian = fit_gaussian(development.to_numpy())
        student = fit_student_t(development.to_numpy())
        for model in (gaussian, student):
            score = score_distribution(model, sealed.to_numpy(), alpha=float(settings["var_alpha"]))
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "origin": "sealed",
                    "model": score.model,
                    "observations": score.observations,
                    "mean_log_score": score.mean_log_score,
                    "pit_ks_pvalue": score.pit_ks_pvalue,
                    "var_exceedance_rate": score.var_exceedance_rate,
                    "var_pvalue": score.var_pvalue,
                    "expected_shortfall": score.expected_shortfall,
                    "realised_shortfall": score.realised_shortfall,
                    "sealed_access": True,
                }
            )
    scores = pd.DataFrame(rows)
    verdict = h1_development_decision(h1_log_score_comparison(scores)).iloc[0]
    if verdict.null_decision == "reject":
        decision = "retain"
        status = "Research validated"
        result = "student_t"
    else:
        decision = "reject"
        status = "Rejected"
        result = "gaussian"
    configuration = config_digest(settings)
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        result=result,
        decision=decision,
        dataset_id=dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H1 confirmatory decision=%s benchmark=%s run=%s boundary=%s",
        decision,
        result,
        run_id,
        boundary.isoformat(),
    )
    return H1Confirmation(scores=scores, record=updated, boundary=boundary)


def _require_open_h1(record: ExperimentRecord) -> None:
    """Refuse a record that has already consumed the sealed test."""
    if record.experiment_id != "EXP-H01":
        raise ValueError(f"H1 confirmation needs EXP-H01, got {record.experiment_id}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H1 sealed test has already been used: decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_open_h2(record: ExperimentRecord) -> None:
    """Refuse an H2 record that has already consumed the sealed test."""
    if record.experiment_id != "EXP-H02":
        raise ValueError(f"H2 confirmation needs EXP-H02, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H01":
        raise ValueError(f"H2 confirmation needs parent EXP-H01, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H2 sealed test has already been used: decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _sha256_digest(value: str) -> str:
    """Return a lowercase sha256 hex digest, or raise."""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"dataset digest must be a sha256 hex string, got {value!r}")
    return value


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


def h2_development_decision(comparison: pd.DataFrame, benchmark: str) -> pd.DataFrame:
    """Accept or reject the H2 null from the log-score family.

    Reject the null when the regime mixture has a positive mean and median
    advantage at every horizon. Otherwise accept it. The sealed test is not
    involved, so this cannot retain the experiment by itself.
    """
    if benchmark not in {"gaussian", "student_t"}:
        raise ValueError(f"H2 decision benchmark must be gaussian or student_t, got {benchmark!r}")
    required = {"horizon_minutes", "mean_advantage", "median_advantage", "origins"}
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H2 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H2 development decision has no horizons")
    if comparison["horizon_minutes"].duplicated().any():
        raise ValueError("H2 development decision has a repeated horizon")
    if (comparison["origins"] < 1).any():
        raise ValueError("H2 development decision needs at least one origin at every horizon")
    advantages = comparison[["mean_advantage", "median_advantage"]]
    if not advantages.map(math.isfinite).all().all():
        raise ValueError("H2 log-score advantages must be finite")
    beats_benchmark = (comparison["mean_advantage"] > 0.0) & (comparison["median_advantage"] > 0.0)
    if bool(beats_benchmark.all()):
        null_decision = "reject"
        selected = "regime_mixture"
        reason = "Regime-mixture log score beats the unconditional benchmark at every horizon"
    else:
        null_decision = "accept"
        selected = benchmark
        reason = "Regime-mixture log score does not beat the unconditional benchmark at every horizon"
    return pd.DataFrame(
        [{"null_decision": null_decision, "benchmark": selected, "reason": reason}]
    )


@dataclass(frozen=True)
class H2Confirmation:
    """One sealed H2 comparison and the registry record it wrote."""

    scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    benchmark: str


def confirm_h2(
    close: pd.Series,
    record: ExperimentRecord,
    h1_record: ExperimentRecord,
    *,
    dataset_id: str,
    dataset_digest: str,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H2Confirmation:
    """Fit H2 on the development sample and score the sealed year once.

    The filter and horizon densities are frozen before any sealed label is
    read. The nested benchmark comes from the H1 record. A dirty tree, an
    unresolved benchmark, a second read, or a dataset other than the recorded
    freeze raises before that read. The same log-score rule as development
    decides retain or reject.
    """
    version = require_committed(code_version(root))
    _require_open_h2(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h2(stored)
    digest = _sha256_digest(dataset_digest)
    if not dataset_id:
        raise ValueError("confirmation needs a dataset id")
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation dataset {digest} does not match the recorded freeze {stored.dataset_digest}"
        )
    if not h1_record.dataset_digest:
        raise RuntimeError("H2 confirmation needs the H1 dataset digest")
    h1_digest = _sha256_digest(h1_record.dataset_digest)
    if h1_digest != digest:
        raise RuntimeError(f"H2 dataset {digest} does not match the H1 freeze {h1_digest}")
    settings = dict(H2_CONFIG)
    if config:
        settings.update(config)
    _require_h2_protocol(settings)
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(int(item) for item in stored.horizons):
        raise ValueError(
            f"confirmation horizons {list(horizons)} do not match H2 horizons {list(stored.horizons)}"
        )
    if horizons != tuple(int(item) for item in h1_record.horizons):
        raise ValueError(
            f"confirmation horizons {list(horizons)} do not match H1 horizons {list(h1_record.horizons)}"
        )
    benchmark = h1_benchmark(h1_record)
    if not benchmark.resolved:
        raise RuntimeError(f"H2 confirmation needs a resolved H1 benchmark: {benchmark.reason}")
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
    development_minutes = minute_returns.loc[minute_returns.index < boundary]
    if len(development_minutes) < 30:
        raise RuntimeError(f"H2 development minutes has {len(development_minutes)} observations")
    try:
        fitted = fit_clocked_gaussian_hmm(
            development_minutes,
            max_iterations=int(settings["em_iterations"]),
            tolerance=float(settings["em_tolerance"]),
        )
        probabilities, _ = filtered_probability_frame(fitted.model, minute_returns)
    except (ValueError, RuntimeError) as exc:
        raise RuntimeError(f"H2 confirmation regime fit failed: {exc}") from exc
    prepared: list[dict] = []
    alignment_exclusions = 0
    for horizon in horizons:
        built = horizon_log_returns(values, horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            raise RuntimeError(f"horizon {horizon} held no labels out for the sealed test")
        labels, weights, dropped = _align_probabilities(probabilities, development)
        alignment_exclusions += dropped
        if len(labels) < 30:
            raise RuntimeError(f"horizon {horizon} development sample has {len(labels)} labels")
        try:
            if benchmark.model == "gaussian":
                unconditional = fit_gaussian(labels.to_numpy())
            else:
                unconditional = fit_student_t(labels.to_numpy())
            components = fit_regime_densities(labels.to_numpy(), weights, benchmark.model)
        except (ValueError, RuntimeError) as exc:
            raise RuntimeError(
                f"H2 confirmation density fit failed for horizon {horizon}: {exc}"
            ) from exc
        prepared.append(
            {
                "horizon": horizon,
                "returns": built.returns,
                "unconditional": unconditional,
                "components": components,
            }
        )
    rows: list[dict] = []
    for item in prepared:
        horizon = int(item["horizon"])
        sealed = sealed_returns(item["returns"], horizon, boundary, confirmatory=True)
        if len(sealed) < 30:
            raise RuntimeError(f"horizon {horizon} sealed sample has {len(sealed)} labels")
        sealed_labels, sealed_weights, dropped = _align_probabilities(probabilities, sealed)
        alignment_exclusions += dropped
        if len(sealed_labels) < 30:
            raise RuntimeError(f"horizon {horizon} sealed sample has {len(sealed_labels)} aligned labels")
        mixture = score_regime_forecast(
            item["components"],
            sealed_weights,
            sealed_labels.to_numpy(),
            alpha=float(settings["var_alpha"]),
        )
        baseline = score_distribution(
            item["unconditional"],
            sealed_labels.to_numpy(),
            alpha=float(settings["var_alpha"]),
        )
        for score in (baseline, mixture):
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "origin": "sealed",
                    "model": score.model,
                    "benchmark": benchmark.model,
                    "benchmark_resolved": True,
                    "observations": score.observations,
                    "mean_log_score": score.mean_log_score,
                    "pit_ks_pvalue": score.pit_ks_pvalue,
                    "var_exceedance_rate": score.var_exceedance_rate,
                    "var_pvalue": score.var_pvalue,
                    "expected_shortfall": score.expected_shortfall,
                    "realised_shortfall": score.realised_shortfall,
                    "sealed_access": True,
                }
            )
    if alignment_exclusions:
        logger.warning("H2 confirmation dropped %d labels with no filtered probability", alignment_exclusions)
    scores = pd.DataFrame(rows)
    verdict = h2_development_decision(h2_log_score_comparison(scores), benchmark.model).iloc[0]
    if verdict.null_decision == "reject":
        decision = "retain"
        status = "Research validated"
    else:
        decision = "reject"
        status = "Rejected"
    result = str(verdict.benchmark)
    configuration = config_digest(settings)
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        result=result,
        decision=decision,
        dataset_id=dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H2 confirmatory decision=%s result=%s benchmark=%s run=%s boundary=%s",
        decision,
        result,
        benchmark.model,
        run_id,
        boundary.isoformat(),
    )
    return H2Confirmation(scores=scores, record=updated, boundary=boundary, benchmark=benchmark.model)


@dataclass(frozen=True)
class H3Evaluation:
    """Development-only factor-density evidence."""

    scores: pd.DataFrame
    pit_qq: pd.DataFrame
    diagnostics: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    benchmark: str
    benchmark_resolved: bool
    alignment_exclusions: int
    sealed_access: bool = False


def evaluate_h3(
    panel_closes: pd.DataFrame,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> H3Evaluation:
    """Compare lagged factor residuals with a nested own-price density."""
    settings = dict(H3_CONFIG)
    if config:
        settings.update(config)
    _require_h3_protocol(settings)
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(int(item) for item in h1_record.horizons):
        raise ValueError(
            f"H3 horizons {list(horizons)} do not match H1 horizons {list(h1_record.horizons)}"
        )
    benchmark = h1_benchmark(h1_record)
    if not benchmark.resolved:
        logger.warning("H3 benchmark is unresolved: %s", benchmark.reason)
    panel = _normalise_h3_panel(panel_closes, settings)
    boundary = sealed_boundary(panel.index, months=int(settings["sealed_months"]))
    development_panel = panel.loc[panel.index < boundary]
    returns, gap_exclusions = panel_minute_returns(development_panel)
    origins = rolling_origins(
        development_panel.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    rows: list[dict] = []
    qq_rows: list[dict] = []
    diagnostics: list[dict] = []
    failures: list[dict] = []
    alignment_exclusions = 0
    target = str(settings["target_symbol"])
    target_close = panel[target]
    development_by_horizon: dict[int, pd.Series] = {}
    for horizon in horizons:
        built = horizon_log_returns(target_close, horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            logger.warning("Horizon %s held no labels out for the sealed test", horizon)
        development_by_horizon[horizon] = development
    for origin in origins:
        span_returns = returns.loc[
            (returns.index >= origin.train_start) & (returns.index < origin.validation_end)
        ]
        try:
            feature_sets, state = _h3_feature_sets(span_returns, origin.train_start, origin.train_end, settings)
        except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
            failures.append({"horizon": None, "origin": origin.name, "reason": str(exc)})
            logger.error("H3 factor fit failed origin=%s: %s", origin.name, exc)
            continue
        diagnostics.append(
            {
                "origin": origin.name,
                "single_factor_beta": state.single_factor_beta,
                "absorption_ratio": state.absorption_ratio,
                "explained_variance_ratio": state.explained_variance_ratio,
                "gap_exclusions": gap_exclusions,
                "lag_exclusions": state.lag_exclusions,
            }
        )
        for horizon in horizons:
            development = development_by_horizon[horizon]
            train_labels = select_available(
                development, horizon, origin.train_start, origin.train_end
            )
            validation_labels = select_available(
                development, horizon, origin.train_end, origin.validation_end
            )
            try:
                fitted, train_dropped = _fit_h3_models(
                    feature_sets, train_labels, benchmark.model
                )
                scored, validation_dropped = _score_h3_models(
                    fitted,
                    feature_sets,
                    validation_labels,
                    alpha=float(settings["var_alpha"]),
                )
                alignment_exclusions += train_dropped + validation_dropped
            except (ValueError, RuntimeError) as exc:
                failures.append(
                    {"horizon": horizon, "origin": origin.name, "reason": str(exc)}
                )
                logger.error(
                    "H3 density fit failed origin=%s horizon=%s: %s",
                    origin.name,
                    horizon,
                    exc,
                )
                continue
            for score in scored:
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "origin": origin.name,
                        "expected_origins": len(origins),
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
                for theoretical, empirical in zip(
                    score.pit_qq_theoretical, score.pit_qq_empirical
                ):
                    qq_rows.append(
                        {
                            "horizon_minutes": horizon,
                            "origin": origin.name,
                            "model": score.model,
                            "theoretical": theoretical,
                            "empirical": empirical,
                            "sealed_access": False,
                        }
                    )
    if failures:
        logger.warning("H3 recorded %d skipped or failed windows", len(failures))
    if not rows:
        raise RuntimeError(f"H3 produced no development scores; failures={len(failures)}")
    return H3Evaluation(
        scores=pd.DataFrame(rows),
        pit_qq=pd.DataFrame(qq_rows),
        diagnostics=pd.DataFrame(diagnostics),
        failures=tuple(failures),
        boundary=boundary,
        benchmark=benchmark.model,
        benchmark_resolved=benchmark.resolved,
        alignment_exclusions=alignment_exclusions,
        sealed_access=False,
    )


def _normalise_h3_panel(panel_closes: pd.DataFrame, settings: dict) -> pd.DataFrame:
    symbols = tuple(str(item) for item in settings["symbols"])
    if tuple(panel_closes.columns) != symbols:
        raise ValueError(
            f"H3 panel columns must be the frozen order {list(symbols)}, "
            f"got {list(panel_closes.columns)}"
        )
    index = as_utc_index(panel_closes.index)
    panel = panel_closes.copy()
    panel.index = index
    panel = panel.sort_index()
    if panel.index.duplicated().any():
        raise ValueError("H3 panel contains duplicate timestamps")
    values = panel.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise ValueError("H3 panel contains non-positive or non-finite closes")
    return panel


def _h3_feature_sets(
    returns: pd.DataFrame,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    settings: dict,
) -> tuple[dict[str, pd.DataFrame], object]:
    target = str(settings["target_symbol"])
    lag = int(settings["feature_lag_minutes"])
    state = fit_factor_state(
        returns,
        target_symbol=target,
        train_start=train_start,
        train_end=train_end,
        components=int(settings["pca_components"]),
        lag_minutes=lag,
    )
    own = own_price_features(
        returns[target],
        volatility_window=int(settings["own_price_volatility_window"]),
        lag_minutes=lag,
    )
    common = own.join(state.features, how="inner")
    sets = {
        "own_price": common.loc[:, list(own.columns)],
        "single_factor": common.loc[:, [*own.columns, "single_factor_residual"]],
        "pca_factor": common.loc[:, [*own.columns, "pca_residual"]],
    }
    return sets, state


def _align_h3_features(
    features: pd.DataFrame, labels: pd.Series, label: str
) -> tuple[pd.DataFrame, pd.Series, int]:
    aligned = features.reindex(labels.index)
    missing = aligned.isna().any(axis=1)
    dropped = int(missing.sum())
    if dropped:
        logger.warning("Dropped %d %s labels with unavailable lagged features", dropped, label)
    kept_labels = labels.loc[~missing]
    kept_features = aligned.loc[~missing]
    if len(kept_labels) < 30:
        raise ValueError(f"{label} sample has {len(kept_labels)} aligned labels")
    return kept_features, kept_labels, dropped


def _fit_h3_models(
    feature_sets: dict[str, pd.DataFrame],
    labels: pd.Series,
    family: str,
) -> tuple[dict[str, object], int]:
    fitted: dict[str, object] = {}
    drops: int | None = None
    kept_index: pd.Index | None = None
    for name, features in feature_sets.items():
        aligned, kept, dropped = _align_h3_features(features, labels, f"{name} training")
        if drops is None:
            drops = dropped
            kept_index = kept.index
        elif dropped != drops or not kept.index.equals(kept_index):
            raise RuntimeError("conditional models do not share the same training labels")
        fitted[name] = fit_conditional_density(aligned, kept, family=family, name=name)
    return fitted, int(drops or 0)


def _score_h3_models(
    fitted: dict[str, object],
    feature_sets: dict[str, pd.DataFrame],
    labels: pd.Series,
    *,
    alpha: float,
    models: tuple[str, ...] = ("own_price", "single_factor", "pca_factor"),
) -> tuple[tuple[object, ...], int]:
    scores = []
    drops: int | None = None
    kept_index: pd.Index | None = None
    for name in models:
        aligned, kept, dropped = _align_h3_features(
            feature_sets[name], labels, f"{name} scoring"
        )
        if drops is None:
            drops = dropped
            kept_index = kept.index
        elif dropped != drops or not kept.index.equals(kept_index):
            raise RuntimeError("conditional models do not share the same scoring labels")
        scores.append(score_conditional_density(fitted[name], aligned, kept, alpha=alpha))
    return tuple(scores), int(drops or 0)


def h3_log_score_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Summarise each factor candidate's increment over own-price history."""
    required = {
        "horizon_minutes",
        "origin",
        "expected_origins",
        "model",
        "mean_log_score",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H3 scores are missing columns: {sorted(missing)}")
    pivot = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="mean_log_score",
        aggfunc="first",
    )
    models = {"own_price", "single_factor", "pca_factor"}
    if not models.issubset(pivot.columns):
        raise RuntimeError(f"H3 comparison needs {sorted(models)}, found {list(pivot.columns)}")
    expected_values = scores["expected_origins"].dropna().unique()
    if len(expected_values) != 1 or int(expected_values[0]) < 1:
        raise RuntimeError(
            f"H3 comparison needs one positive expected-origin count, found {list(expected_values)}"
        )
    expected_origins = int(expected_values[0])
    pca_vs_single = pivot["pca_factor"] - pivot["single_factor"]
    pca_vs_single_summary = pca_vs_single.groupby("horizon_minutes").agg(
        ["mean", "median"]
    )
    rows = []
    for model in ("single_factor", "pca_factor"):
        advantage = pivot[model] - pivot["own_price"]
        summary = advantage.groupby("horizon_minutes").agg(["mean", "median", "count"])
        for horizon, values in summary.iterrows():
            versus_single = pca_vs_single_summary.loc[horizon]
            rows.append(
                {
                    "horizon_minutes": int(horizon),
                    "model": model,
                    "mean_advantage": float(values["mean"]),
                    "median_advantage": float(values["median"]),
                    "mean_vs_single": (
                        float(versus_single["mean"]) if model == "pca_factor" else math.nan
                    ),
                    "median_vs_single": (
                        float(versus_single["median"]) if model == "pca_factor" else math.nan
                    ),
                    "origins": int(values["count"]),
                    "expected_origins": expected_origins,
                }
            )
    return pd.DataFrame(rows)


def h3_development_decision(comparison: pd.DataFrame) -> pd.DataFrame:
    """Select a factor model only when it beats own-price at every horizon."""
    required = {
        "horizon_minutes",
        "model",
        "mean_advantage",
        "median_advantage",
        "mean_vs_single",
        "median_vs_single",
        "origins",
        "expected_origins",
    }
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H3 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H3 development decision has no horizons")
    if (comparison["origins"] != comparison["expected_origins"]).any():
        return pd.DataFrame(
            [
                {
                    "null_decision": "defer",
                    "benchmark": "own_price",
                    "reason": "H3 evidence is incomplete because one or more registered origins failed",
                }
            ]
        )
    candidates = {}
    expected_horizons: set[int] | None = None
    for model in ("single_factor", "pca_factor"):
        subset = comparison.loc[comparison["model"] == model]
        if subset.empty or subset["horizon_minutes"].duplicated().any():
            raise ValueError(f"H3 decision needs one row per horizon for {model}")
        horizons = {int(value) for value in subset["horizon_minutes"]}
        if expected_horizons is None:
            expected_horizons = horizons
        elif horizons != expected_horizons:
            raise ValueError("H3 candidates must report the same horizon family")
        values = subset[["mean_advantage", "median_advantage"]]
        if not values.map(math.isfinite).all().all() or (subset["origins"] < 1).any():
            raise ValueError(f"H3 decision received invalid evidence for {model}")
        candidates[model] = bool(
            ((subset["mean_advantage"] > 0.0) & (subset["median_advantage"] > 0.0)).all()
        )
    pca = comparison.loc[comparison["model"] == "pca_factor"]
    pca_increment = pca[["mean_vs_single", "median_vs_single"]]
    if not pca_increment.map(math.isfinite).all().all():
        raise ValueError("H3 PCA-versus-single-factor advantages must be finite")
    pca_beats_single = bool(
        ((pca["mean_vs_single"] > 0.0) & (pca["median_vs_single"] > 0.0)).all()
    )
    if candidates["pca_factor"] and pca_beats_single:
        selected = "pca_factor"
        null_decision = "reject"
        reason = "PCA factor state beats own-price and single-factor state at every horizon"
    elif candidates["single_factor"]:
        selected = "single_factor"
        null_decision = "reject"
        reason = "Single-factor state beats own-price history at every horizon"
    elif candidates["pca_factor"]:
        selected = "pca_factor"
        null_decision = "reject"
        reason = "PCA factor state beats own-price history, but not single-factor state, at every horizon"
    else:
        selected = "own_price"
        null_decision = "accept"
        reason = "No factor state beats own-price history at every horizon"
    return pd.DataFrame(
        [{"null_decision": null_decision, "benchmark": selected, "reason": reason}]
    )


def h3_var_coverage_family(scores: pd.DataFrame) -> pd.DataFrame:
    """Bonferroni-adjust the fixed H3 horizon-by-model calibration family."""
    return _var_coverage_family(
        scores, {"own_price", "single_factor", "pca_factor"}, "H3"
    )


@dataclass(frozen=True)
class H3Confirmation:
    """One sealed H3 comparison and the registry record it wrote."""

    scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    benchmark: str


def confirm_h3(
    panel_closes: pd.DataFrame,
    record: ExperimentRecord,
    h1_record: ExperimentRecord,
    *,
    panel_manifest: PanelManifest,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H3Confirmation:
    """Fit H3 before reading the sealed labels, then consume them once.

    H1 contributes the retained residual-density family. H3 is a separate
    panel experiment, so its target history is bound by the panel manifest
    rather than required to reuse H1's single-series dataset digest.
    """
    version = require_committed(code_version(root))
    _require_open_h3(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h3(stored)
    verify_panel_manifest(panel_manifest)
    dataset_id = panel_manifest.dataset_id
    digest = _sha256_digest(panel_manifest.digest)
    mutable_objects = [
        item.key
        for member in panel_manifest.members
        for item in member.objects
        if item.version_id in {None, "", "null"}
    ]
    if mutable_objects:
        raise RuntimeError(
            "H3 confirmation needs immutable version IDs for every panel object; "
            f"mutable objects={mutable_objects}"
        )
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation panel {digest} does not match recorded freeze {stored.dataset_digest}"
        )
    benchmark = h1_benchmark(h1_record)
    if (
        not benchmark.resolved
        or h1_record.decision not in {"retain", "reject"}
        or not h1_record.sealed_access
    ):
        raise RuntimeError(f"H3 confirmation needs a resolved H1 benchmark: {benchmark.reason}")
    settings = dict(H3_CONFIG)
    if config:
        settings.update(config)
    _require_h3_protocol(settings)
    configuration = config_digest(settings)
    if not stored.config_digest:
        raise RuntimeError("H3 confirmation needs a pre-registered configuration digest")
    if stored.config_digest != configuration:
        raise RuntimeError(
            f"H3 confirmation config {configuration} does not match "
            f"pre-registered config {stored.config_digest}"
        )
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(stored.horizons) or horizons != tuple(h1_record.horizons):
        raise ValueError("H3 confirmation horizons must match H3 and H1 records")
    panel = _normalise_h3_panel(panel_closes, settings)
    boundary = sealed_boundary(panel.index, months=int(settings["sealed_months"]))
    returns, _ = panel_minute_returns(panel)
    feature_sets, _ = _h3_feature_sets(returns, panel.index.min(), boundary, settings)
    target_close = panel[str(settings["target_symbol"])]
    prepared = []
    for horizon in horizons:
        built = horizon_log_returns(target_close, horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            raise RuntimeError(f"horizon {horizon} held no labels out for the sealed test")
        fitted, _ = _fit_h3_models(feature_sets, development, benchmark.model)
        prepared.append((horizon, built.returns, fitted))
    rows = []
    for horizon, returns_at_horizon, fitted in prepared:
        sealed = sealed_returns(returns_at_horizon, horizon, boundary, confirmatory=True)
        scored, _ = _score_h3_models(
            fitted, feature_sets, sealed, alpha=float(settings["var_alpha"])
        )
        for score in scored:
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "origin": "sealed",
                    "expected_origins": 1,
                    "model": score.model,
                    "benchmark": benchmark.model,
                    "benchmark_resolved": True,
                    "observations": score.observations,
                    "mean_log_score": score.mean_log_score,
                    "pit_ks_pvalue": score.pit_ks_pvalue,
                    "var_exceedance_rate": score.var_exceedance_rate,
                    "var_pvalue": score.var_pvalue,
                    "expected_shortfall": score.expected_shortfall,
                    "realised_shortfall": score.realised_shortfall,
                    "sealed_access": True,
                }
            )
    scores = pd.DataFrame(rows)
    verdict = h3_development_decision(h3_log_score_comparison(scores)).iloc[0]
    decision = "retain" if verdict.null_decision == "reject" else "reject"
    status = "Research validated" if decision == "retain" else "Rejected"
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        result=str(verdict.benchmark),
        decision=decision,
        dataset_id=dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H3 confirmatory decision=%s result=%s run=%s boundary=%s",
        decision,
        verdict.benchmark,
        run_id,
        boundary.isoformat(),
    )
    return H3Confirmation(
        scores=scores, record=updated, boundary=boundary, benchmark=benchmark.model
    )


def _require_open_h3(record: ExperimentRecord) -> None:
    if record.experiment_id != "EXP-H03":
        raise ValueError(f"H3 confirmation needs EXP-H03, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H01":
        raise ValueError(f"H3 confirmation needs parent EXP-H01, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H3 sealed test has already been used: "
            f"decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_h3_protocol(settings: dict) -> None:
    if settings.get("forecast") != "conditional_density":
        raise ValueError("H3 forecast must be conditional_density")
    if settings.get("first_passage") != "not_scored":
        raise ValueError("H3 does not score first passage")
    if int(settings.get("feature_lag_minutes", 0)) < 1:
        raise ValueError("H3 factor state must be lagged by at least one minute")
    if settings.get("forecast_multiple_testing") != "all_horizons_conjunction":
        raise ValueError("H3 forecast family must use the all-horizons conjunction rule")
    if settings.get("calibration_multiple_testing") != "bonferroni_horizon_by_model":
        raise ValueError("H3 calibration family must use Bonferroni adjustment")
    symbols = tuple(settings.get("symbols", ()))
    if str(settings.get("target_symbol")) not in symbols:
        raise ValueError("H3 target symbol must belong to the frozen panel")
    if tuple(settings.get("factor_models", ())) != ("single_factor", "pca_factor"):
        raise ValueError("H3 factor models must be single_factor then pca_factor")


_H20_MODELS = ("own_price", "crypto_factor", "external_factor", "combined")


@dataclass(frozen=True)
class H20Evaluation:
    """Development-only external-market density evidence."""

    scores: pd.DataFrame
    pit_qq: pd.DataFrame
    diagnostics: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    benchmark: str
    benchmark_resolved: bool
    alignment_exclusions: int
    sealed_access: bool = False


def evaluate_h20(
    panel_closes: pd.DataFrame,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> H20Evaluation:
    """Compare lagged external-market state with own-price and crypto-factor densities."""
    settings = dict(H20_CONFIG)
    if config:
        settings.update(config)
    _require_h20_protocol(settings)
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(int(item) for item in h1_record.horizons):
        raise ValueError(
            f"H20 horizons {list(horizons)} do not match H1 horizons {list(h1_record.horizons)}"
        )
    benchmark = h1_benchmark(h1_record)
    if not benchmark.resolved:
        logger.warning("H20 benchmark is unresolved: %s", benchmark.reason)
    panel = _normalise_h20_panel(panel_closes, settings)
    boundary = sealed_boundary(panel.index, months=int(settings["sealed_months"]))
    development_panel = panel.loc[panel.index < boundary]
    returns, gap_exclusions = panel_minute_returns(development_panel)
    origins = rolling_origins(
        development_panel.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    rows: list[dict] = []
    qq_rows: list[dict] = []
    diagnostics: list[dict] = []
    failures: list[dict] = []
    alignment_exclusions = 0
    target = str(settings["target_symbol"])
    development_by_horizon: dict[int, pd.Series] = {}
    for horizon in horizons:
        built = horizon_log_returns(panel[target], horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            logger.warning("Horizon %s held no labels out for the sealed test", horizon)
        development_by_horizon[horizon] = development
    for origin in origins:
        span_returns = returns.loc[
            (returns.index >= origin.train_start) & (returns.index < origin.validation_end)
        ]
        try:
            feature_sets, state = _h20_feature_sets(
                span_returns, origin.train_start, origin.train_end, settings
            )
        except (ValueError, RuntimeError, np.linalg.LinAlgError) as exc:
            failures.append({"horizon": None, "origin": origin.name, "reason": str(exc)})
            logger.error("H20 factor fit failed origin=%s: %s", origin.name, exc)
            continue
        diagnostics.append(
            {
                "origin": origin.name,
                "single_factor_beta": state.single_factor_beta,
                "gap_exclusions": gap_exclusions,
                "crypto_lag_exclusions": state.lag_exclusions,
                "external_columns": tuple(feature_sets["external_factor"].columns),
            }
        )
        for horizon in horizons:
            development = development_by_horizon[horizon]
            train_labels = select_available(
                development, horizon, origin.train_start, origin.train_end
            )
            validation_labels = select_available(
                development, horizon, origin.train_end, origin.validation_end
            )
            try:
                fitted, train_dropped = _fit_h3_models(
                    feature_sets, train_labels, benchmark.model
                )
                scored, validation_dropped = _score_h3_models(
                    fitted,
                    feature_sets,
                    validation_labels,
                    alpha=float(settings["var_alpha"]),
                    models=_H20_MODELS,
                )
                alignment_exclusions += train_dropped + validation_dropped
            except (ValueError, RuntimeError) as exc:
                failures.append(
                    {"horizon": horizon, "origin": origin.name, "reason": str(exc)}
                )
                logger.error(
                    "H20 density fit failed origin=%s horizon=%s: %s",
                    origin.name,
                    horizon,
                    exc,
                )
                continue
            for score in scored:
                rows.append(
                    {
                        "horizon_minutes": horizon,
                        "origin": origin.name,
                        "expected_origins": len(origins),
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
                for theoretical, empirical in zip(
                    score.pit_qq_theoretical, score.pit_qq_empirical
                ):
                    qq_rows.append(
                        {
                            "horizon_minutes": horizon,
                            "origin": origin.name,
                            "model": score.model,
                            "theoretical": theoretical,
                            "empirical": empirical,
                            "sealed_access": False,
                        }
                    )
    if failures:
        logger.warning("H20 recorded %d skipped or failed windows", len(failures))
    if not rows:
        raise RuntimeError(f"H20 produced no development scores; failures={len(failures)}")
    return H20Evaluation(
        scores=pd.DataFrame(rows),
        pit_qq=pd.DataFrame(qq_rows),
        diagnostics=pd.DataFrame(diagnostics),
        failures=tuple(failures),
        boundary=boundary,
        benchmark=benchmark.model,
        benchmark_resolved=benchmark.resolved,
        alignment_exclusions=alignment_exclusions,
        sealed_access=False,
    )


def _normalise_h20_panel(panel_closes: pd.DataFrame, settings: dict) -> pd.DataFrame:
    symbols = tuple(str(item) for item in settings["symbols"])
    if tuple(panel_closes.columns) != symbols:
        raise ValueError(
            f"H20 panel columns must be the frozen order {list(symbols)}, "
            f"got {list(panel_closes.columns)}"
        )
    index = as_utc_index(panel_closes.index)
    panel = panel_closes.copy()
    panel.index = index
    panel = panel.sort_index()
    if panel.index.duplicated().any():
        raise ValueError("H20 panel contains duplicate timestamps")
    values = panel.to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values <= 0.0).any():
        raise ValueError("H20 panel contains non-positive or non-finite closes")
    return panel


def _h20_feature_sets(
    returns: pd.DataFrame,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    settings: dict,
) -> tuple[dict[str, pd.DataFrame], object]:
    target = str(settings["target_symbol"])
    lag = int(settings["feature_lag_minutes"])
    crypto_symbols = [str(item) for item in settings["crypto_symbols"]]
    state = fit_factor_state(
        returns.loc[:, crypto_symbols],
        target_symbol=target,
        train_start=train_start,
        train_end=train_end,
        components=int(settings["pca_components"]),
        lag_minutes=lag,
    )
    if settings.get("crypto_state") != "single_factor_residual":
        raise ValueError("H20 crypto ablation must use the single-factor residual")
    own = own_price_features(
        returns[target],
        volatility_window=int(settings["own_price_volatility_window"]),
        lag_minutes=lag,
    )
    external = external_market_features(
        returns,
        feature_symbols=settings["external_features"],
        usd_factor_legs=settings["usd_factor_legs"],
        lag_minutes=lag,
    )
    common = own.join(state.features[["single_factor_residual"]], how="inner").join(
        external.features, how="inner"
    )
    external_columns = list(external.features.columns)
    sets = {
        "own_price": common.loc[:, list(own.columns)],
        "crypto_factor": common.loc[:, [*own.columns, "single_factor_residual"]],
        "external_factor": common.loc[:, [*own.columns, *external_columns]],
        "combined": common.loc[:, [*own.columns, "single_factor_residual", *external_columns]],
    }
    return sets, state


def h20_log_score_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Summarise each nested candidate's increment over own-price history."""
    required = {
        "horizon_minutes",
        "origin",
        "expected_origins",
        "model",
        "mean_log_score",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H20 scores are missing columns: {sorted(missing)}")
    pivot = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="mean_log_score",
        aggfunc="first",
    )
    if not set(_H20_MODELS).issubset(pivot.columns):
        raise RuntimeError(
            f"H20 comparison needs {list(_H20_MODELS)}, found {list(pivot.columns)}"
        )
    expected_values = scores["expected_origins"].dropna().unique()
    if len(expected_values) != 1 or int(expected_values[0]) < 1:
        raise RuntimeError(
            f"H20 comparison needs one positive expected-origin count, found {list(expected_values)}"
        )
    expected_origins = int(expected_values[0])
    rows = []
    for model in ("crypto_factor", "external_factor", "combined"):
        advantage = pivot[model] - pivot["own_price"]
        versus_crypto = pivot[model] - pivot["crypto_factor"]
        versus_external = pivot[model] - pivot["external_factor"]
        summary = advantage.groupby("horizon_minutes").agg(["mean", "median", "count"])
        crypto_summary = versus_crypto.groupby("horizon_minutes").agg(["mean", "median"])
        external_summary = versus_external.groupby("horizon_minutes").agg(["mean", "median"])
        for horizon, values in summary.iterrows():
            rows.append(
                {
                    "horizon_minutes": int(horizon),
                    "model": model,
                    "mean_advantage": float(values["mean"]),
                    "median_advantage": float(values["median"]),
                    "mean_vs_crypto": float(crypto_summary.loc[horizon, "mean"]),
                    "median_vs_crypto": float(crypto_summary.loc[horizon, "median"]),
                    "mean_vs_external": float(external_summary.loc[horizon, "mean"]),
                    "median_vs_external": float(external_summary.loc[horizon, "median"]),
                    "origins": int(values["count"]),
                    "expected_origins": expected_origins,
                }
            )
    return pd.DataFrame(rows)


def h20_development_decision(comparison: pd.DataFrame) -> pd.DataFrame:
    """Reject the external-market null only when that state wins at every horizon."""
    required = {
        "horizon_minutes",
        "model",
        "mean_advantage",
        "median_advantage",
        "mean_vs_crypto",
        "median_vs_crypto",
        "mean_vs_external",
        "median_vs_external",
        "origins",
        "expected_origins",
    }
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H20 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H20 development decision has no horizons")
    if (comparison["origins"] != comparison["expected_origins"]).any():
        return pd.DataFrame(
            [
                {
                    "null_decision": "defer",
                    "benchmark": "own_price",
                    "reason": "H20 evidence is incomplete because one or more registered origins failed",
                }
            ]
        )

    def _beats(model: str, mean_column: str, median_column: str) -> bool:
        subset = comparison.loc[comparison["model"] == model]
        if subset.empty or subset["horizon_minutes"].duplicated().any():
            raise ValueError(f"H20 decision needs one row per horizon for {model}")
        values = subset[[mean_column, median_column]]
        if not values.map(math.isfinite).all().all() or (subset["origins"] < 1).any():
            raise ValueError(f"H20 decision received invalid evidence for {model}")
        return bool(((subset[mean_column] > 0.0) & (subset[median_column] > 0.0)).all())

    horizon_sets = [
        frozenset(int(value) for value in comparison.loc[comparison["model"] == model, "horizon_minutes"])
        for model in ("crypto_factor", "external_factor", "combined")
    ]
    if len(set(horizon_sets)) != 1:
        raise ValueError("H20 candidates must report the same horizon family")
    crypto_beats = _beats("crypto_factor", "mean_advantage", "median_advantage")
    external_beats = _beats("external_factor", "mean_advantage", "median_advantage")
    combined_beats_own = _beats("combined", "mean_advantage", "median_advantage")
    combined_beats_crypto = _beats("combined", "mean_vs_crypto", "median_vs_crypto")
    combined_beats_external = _beats("combined", "mean_vs_external", "median_vs_external")
    if combined_beats_own and combined_beats_crypto and combined_beats_external:
        selected = "combined"
        null_decision = "reject"
        reason = (
            "Combined external and crypto state beats own-price, crypto-only, "
            "and external-only state at every horizon"
        )
    elif external_beats:
        selected = "external_factor"
        null_decision = "reject"
        reason = "External-market state beats own-price history at every horizon"
    elif crypto_beats:
        selected = "crypto_factor"
        null_decision = "accept"
        reason = "Crypto factor state beats own-price, but external-market state does not"
    else:
        selected = "own_price"
        null_decision = "accept"
        reason = "External-market state does not beat own-price history at every horizon"
    return pd.DataFrame(
        [{"null_decision": null_decision, "benchmark": selected, "reason": reason}]
    )


def h20_var_coverage_family(scores: pd.DataFrame) -> pd.DataFrame:
    """Bonferroni-adjust the fixed H20 horizon-by-model calibration family."""
    return _var_coverage_family(scores, set(_H20_MODELS), "H20")


@dataclass(frozen=True)
class H20Confirmation:
    """One sealed H20 comparison and the registry record it wrote."""

    scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    benchmark: str


def confirm_h20(
    panel_closes: pd.DataFrame,
    record: ExperimentRecord,
    h1_record: ExperimentRecord,
    *,
    panel_manifest: PanelManifest,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H20Confirmation:
    """Fit H20 before reading the sealed labels, then consume them once.

    H3 is an ablation inside this experiment. Its retain or reject decision
    is not a gate. H1 contributes the residual-density family.
    """
    version = require_committed(code_version(root))
    _require_open_h20(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h20(stored)
    verify_panel_manifest(panel_manifest)
    if panel_manifest.venue != "cross-venue":
        raise RuntimeError(
            f"H20 confirmation needs a cross-venue panel, got {panel_manifest.venue!r}"
        )
    dataset_id = panel_manifest.dataset_id
    digest = _sha256_digest(panel_manifest.digest)
    mutable_objects = [
        item.key
        for member in panel_manifest.members
        for item in member.objects
        if item.version_id in {None, "", "null"}
    ]
    if mutable_objects:
        raise RuntimeError(
            "H20 confirmation needs immutable version IDs for every panel object; "
            f"mutable objects={mutable_objects}"
        )
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation panel {digest} does not match recorded freeze {stored.dataset_digest}"
        )
    benchmark = h1_benchmark(h1_record)
    if (
        not benchmark.resolved
        or h1_record.decision not in {"retain", "reject"}
        or not h1_record.sealed_access
    ):
        raise RuntimeError(f"H20 confirmation needs a resolved H1 benchmark: {benchmark.reason}")
    settings = dict(H20_CONFIG)
    if config:
        settings.update(config)
    _require_h20_protocol(settings)
    configuration = config_digest(settings)
    if not stored.config_digest:
        raise RuntimeError("H20 confirmation needs a pre-registered configuration digest")
    if stored.config_digest != configuration:
        raise RuntimeError(
            f"H20 confirmation config {configuration} does not match "
            f"pre-registered config {stored.config_digest}"
        )
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(stored.horizons) or horizons != tuple(h1_record.horizons):
        raise ValueError("H20 confirmation horizons must match H20 and H1 records")
    panel = _normalise_h20_panel(panel_closes, settings)
    boundary = sealed_boundary(panel.index, months=int(settings["sealed_months"]))
    returns, _ = panel_minute_returns(panel)
    feature_sets, _ = _h20_feature_sets(returns, panel.index.min(), boundary, settings)
    target_close = panel[str(settings["target_symbol"])]
    prepared = []
    for horizon in horizons:
        built = horizon_log_returns(target_close, horizon)
        development, held_out = development_returns(built.returns, horizon, boundary)
        if held_out == 0:
            raise RuntimeError(f"horizon {horizon} held no labels out for the sealed test")
        fitted, _ = _fit_h3_models(feature_sets, development, benchmark.model)
        prepared.append((horizon, built.returns, fitted))
    rows = []
    for horizon, returns_at_horizon, fitted in prepared:
        sealed = sealed_returns(returns_at_horizon, horizon, boundary, confirmatory=True)
        scored, _ = _score_h3_models(
            fitted,
            feature_sets,
            sealed,
            alpha=float(settings["var_alpha"]),
            models=_H20_MODELS,
        )
        for score in scored:
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "origin": "sealed",
                    "expected_origins": 1,
                    "model": score.model,
                    "benchmark": benchmark.model,
                    "benchmark_resolved": True,
                    "observations": score.observations,
                    "mean_log_score": score.mean_log_score,
                    "pit_ks_pvalue": score.pit_ks_pvalue,
                    "var_exceedance_rate": score.var_exceedance_rate,
                    "var_pvalue": score.var_pvalue,
                    "expected_shortfall": score.expected_shortfall,
                    "realised_shortfall": score.realised_shortfall,
                    "sealed_access": True,
                }
            )
    scores = pd.DataFrame(rows)
    verdict = h20_development_decision(h20_log_score_comparison(scores)).iloc[0]
    decision = "retain" if verdict.null_decision == "reject" else "reject"
    status = "Research validated" if decision == "retain" else "Rejected"
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        result=str(verdict.benchmark),
        decision=decision,
        dataset_id=dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H20 confirmatory decision=%s result=%s run=%s boundary=%s",
        decision,
        verdict.benchmark,
        run_id,
        boundary.isoformat(),
    )
    return H20Confirmation(
        scores=scores, record=updated, boundary=boundary, benchmark=benchmark.model
    )


def _require_open_h20(record: ExperimentRecord) -> None:
    if record.experiment_id != "EXP-H20":
        raise ValueError(f"H20 confirmation needs EXP-H20, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H01":
        raise ValueError(f"H20 confirmation needs parent EXP-H01, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H20 sealed test has already been used: "
            f"decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_h20_protocol(settings: dict) -> None:
    if settings.get("forecast") != "conditional_density":
        raise ValueError("H20 forecast must be conditional_density")
    if settings.get("first_passage") != "not_scored":
        raise ValueError("H20 does not score first passage")
    if int(settings.get("feature_lag_minutes", 0)) < 1:
        raise ValueError("H20 external state must be lagged by at least one minute")
    if settings.get("forecast_multiple_testing") != "all_horizons_conjunction":
        raise ValueError("H20 forecast family must use the all-horizons conjunction rule")
    if settings.get("calibration_multiple_testing") != "bonferroni_horizon_by_model":
        raise ValueError("H20 calibration family must use Bonferroni adjustment")
    if settings.get("crypto_state") != "single_factor_residual":
        raise ValueError("H20 must not retune the H3 crypto state")
    if tuple(settings.get("factor_models", ())) != ("crypto_factor", "external_factor", "combined"):
        raise ValueError("H20 factor models must be crypto, external, then combined")
    if list(settings.get("unavailable_classes", ())) != ["equity_implied_volatility"]:
        raise ValueError("H20 must record the absent equity-implied-volatility class")
    symbols = tuple(str(item) for item in settings.get("symbols", ()))
    crypto_symbols = tuple(str(item) for item in settings.get("crypto_symbols", ()))
    target = str(settings.get("target_symbol"))
    if target not in crypto_symbols or not set(crypto_symbols).issubset(symbols):
        raise ValueError("H20 crypto symbols must contain the target and belong to the panel")
    if len(crypto_symbols) < 3:
        raise ValueError("H20 crypto ablation needs the target and at least two peers")
    venues = settings.get("venues", {})
    if set(venues) != set(symbols):
        raise ValueError("H20 venues must name every frozen symbol")
    if len(set(venues.values())) < 2:
        raise ValueError("H20 panel must include at least two venues")
    referenced = {target}
    for name, symbol in settings.get("external_features", ()):
        if "vix" in str(name).lower() or "vix" in str(symbol).lower():
            raise ValueError("H20 has no implied-volatility series and must not proxy one")
        referenced.add(str(symbol))
    for symbol, sign in settings.get("usd_factor_legs", ()):
        if int(sign) not in (-1, 1):
            raise ValueError(f"USD factor sign for {symbol} must be -1 or 1")
        referenced.add(str(symbol))
    if len(tuple(settings.get("usd_factor_legs", ()))) < 2:
        raise ValueError("H20 USD factor needs at least two frozen legs")
    missing = sorted(referenced - set(symbols))
    if missing:
        raise ValueError(f"H20 panel is missing declared series: {missing}")


@dataclass(frozen=True)
class ConditionalFit:
    """One conditional density fit on the whole development sample."""

    horizon_minutes: int
    model: str
    density: object
    residuals: np.ndarray
    observations: int


def h3_development_fits(
    panel_closes: pd.DataFrame,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> tuple[ConditionalFit, ...]:
    """Fit each H3 model once on every pre-boundary label, for display only."""
    settings = dict(H3_CONFIG)
    if config:
        settings.update(config)
    _require_h3_protocol(settings)
    panel = _normalise_h3_panel(panel_closes, settings)
    return _development_conditional_fits(panel, settings, h1_record, _h3_feature_sets)


def h20_development_fits(
    panel_closes: pd.DataFrame,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> tuple[ConditionalFit, ...]:
    """Fit each H20 model once on every pre-boundary label, for display only."""
    settings = dict(H20_CONFIG)
    if config:
        settings.update(config)
    _require_h20_protocol(settings)
    panel = _normalise_h20_panel(panel_closes, settings)
    return _development_conditional_fits(panel, settings, h1_record, _h20_feature_sets)


def _development_conditional_fits(
    panel: pd.DataFrame,
    settings: dict,
    h1_record: ExperimentRecord,
    builder,
) -> tuple[ConditionalFit, ...]:
    benchmark = h1_benchmark(h1_record)
    boundary = sealed_boundary(panel.index, months=int(settings["sealed_months"]))
    development_panel = panel.loc[panel.index < boundary]
    if development_panel.empty:
        raise RuntimeError("no development rows precede the sealed boundary")
    returns, _ = panel_minute_returns(development_panel)
    feature_sets, _ = builder(returns, development_panel.index.min(), boundary, settings)
    target = str(settings["target_symbol"])
    fits: list[ConditionalFit] = []
    for horizon in (int(item) for item in settings["horizons_minutes"]):
        built = horizon_log_returns(development_panel[target], horizon)
        labels, _ = development_returns(built.returns, horizon, boundary)
        fitted, _ = _fit_h3_models(feature_sets, labels, benchmark.model)
        for name, density in fitted.items():
            aligned, kept, _ = _align_h3_features(feature_sets[name], labels, f"{name} display")
            residuals = kept.to_numpy(dtype=float) - density.location(aligned)
            fits.append(
                ConditionalFit(
                    horizon_minutes=horizon,
                    model=name,
                    density=density,
                    residuals=residuals,
                    observations=int(len(kept)),
                )
            )
    return tuple(fits)


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
    return _var_coverage_family(scores, {"gaussian", "student_t"}, "H1")


def h2_var_coverage_family(scores: pd.DataFrame) -> pd.DataFrame:
    """Median VaR coverage p-value for the mixture and its benchmark, Bonferroni-adjusted.

    The family is the horizon-by-model grid. The adjustment uses that grid,
    so a missing model must not shrink the penalty.
    """
    if "benchmark" not in scores.columns:
        raise ValueError("H2 scores are missing columns: ['benchmark']")
    if scores.empty:
        raise ValueError("H2 VaR family has no scores")
    benchmarks = scores["benchmark"].dropna().unique()
    if len(benchmarks) != 1:
        raise RuntimeError(f"H2 VaR family needs one benchmark, found {list(benchmarks)}")
    benchmark = str(benchmarks[0])
    if benchmark not in {"gaussian", "student_t"}:
        raise RuntimeError(f"H2 VaR family benchmark must be gaussian or student_t, got {benchmark}")
    return _var_coverage_family(scores, {benchmark, "regime_mixture"}, "H2")


def _var_coverage_family(scores: pd.DataFrame, models: set[str], label: str) -> pd.DataFrame:
    """Median VaR p-value on a fixed horizon-by-model grid, Bonferroni-adjusted."""
    required = {"horizon_minutes", "model", "var_pvalue"}
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"{label} scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError(f"{label} VaR family has no scores")
    pvalues = scores["var_pvalue"]
    if not pd.api.types.is_numeric_dtype(pvalues) or not pvalues.map(_finite_pvalue).all():
        raise ValueError(f"{label} VaR p-values must be finite numbers in [0, 1]")
    found = set(scores["model"].unique())
    if found != models:
        raise RuntimeError(f"{label} VaR family needs {sorted(models)}, found {sorted(found)}")
    family = scores.groupby(["horizon_minutes", "model"])["var_pvalue"].median().sort_index()
    horizons = family.index.get_level_values("horizon_minutes").unique()
    for horizon in horizons:
        present = set(family.loc[horizon].index)
        if present != models:
            raise RuntimeError(f"{label} VaR family at {horizon} minutes is missing a model: {sorted(present)}")
    if not family.map(_finite_pvalue).all():
        raise RuntimeError(f"{label} VaR family contains a non-finite median p-value")
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


_H4_MODELS = (
    "always_in_market",
    "hard_label_wave",
    "first_passage_probability",
    "terminal_return_probability",
)


@dataclass(frozen=True)
class H4Evaluation:
    """Development-only probability and economic evidence for Wave."""

    economic_scores: pd.DataFrame
    probability_scores: pd.DataFrame
    diagnostics: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    h1_family: str
    sealed_access: bool = False


def evaluate_h4(
    bars: pd.DataFrame,
    h1_record: ExperimentRecord,
    config: dict | None = None,
) -> H4Evaluation:
    """Evaluate probability-aware Wave decisions without reading the sealed year."""
    settings = dict(H4_CONFIG)
    if config:
        settings.update(config)
    _require_h4_protocol(settings)
    benchmark = h1_benchmark(h1_record)
    if not benchmark.resolved:
        raise RuntimeError(f"H4 needs a declared H1 forecast family: {benchmark.reason}")
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(int(item) for item in h1_record.horizons):
        raise ValueError(
            f"H4 horizons {list(horizons)} do not match H1 horizons {list(h1_record.horizons)}"
        )
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    origins = rolling_origins(
        frame.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning(
            "H4 development has %d unavailable one-minute economic returns",
            minute_exclusions,
        )
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    economic_rows: list[dict] = []
    probability_rows: list[dict] = []
    failures: list[dict] = []
    for horizon in horizons:
        labels, terminal = builds[horizon]
        for origin in origins:
            try:
                economic, probability = _score_h4_window(
                    frame,
                    labels.labels,
                    terminal.returns,
                    horizon=horizon,
                    family=benchmark.model,
                    train_start=origin.train_start,
                    train_end=origin.train_end,
                    score_start=origin.train_end,
                    score_end=origin.validation_end,
                    origin=origin.name,
                    settings=settings,
                    sealed_access=False,
                    minute_returns=minute_returns,
                    sizer_scale=sizer_scale,
                )
            except (ValueError, RuntimeError, FloatingPointError, ArithmeticError) as exc:
                failure = {
                    "horizon_minutes": horizon,
                    "origin": origin.name,
                    "error": str(exc),
                }
                failures.append(failure)
                logger.warning(
                    "H4 development origin failed horizon=%d origin=%s: %s",
                    horizon,
                    origin.name,
                    exc,
                )
                continue
            economic_rows.extend(economic)
            probability_rows.extend(probability)
    expected = len(origins)
    for row in economic_rows:
        row["expected_origins"] = expected
    for row in probability_rows:
        row["expected_origins"] = expected
    diagnostics = _h4_diagnostics(builds)
    return H4Evaluation(
        economic_scores=pd.DataFrame(economic_rows),
        probability_scores=pd.DataFrame(probability_rows),
        diagnostics=diagnostics,
        failures=tuple(failures),
        boundary=boundary,
        h1_family=benchmark.model,
    )


def _h4_builds(frame: pd.DataFrame, settings: dict) -> dict[int, tuple[object, object]]:
    builds = {}
    for horizon in (int(item) for item in settings["horizons_minutes"]):
        barrier = build_barrier_labels(
            frame,
            horizon,
            volatility_lookback_minutes=int(settings["barrier_volatility_lookback_minutes"]),
            barrier_sigma=float(settings["barrier_sigma"]),
            intrabar_rule=str(settings["intrabar_rule"]),
        )
        terminal = horizon_log_returns(frame["close"], horizon)
        builds[horizon] = (barrier, terminal)
    return builds


def _h4_diagnostics(builds: dict[int, tuple[object, object]]) -> pd.DataFrame:
    rows = []
    for horizon, (barrier, terminal) in builds.items():
        rows.append(
            {
                "horizon_minutes": horizon,
                "barrier_labels": len(barrier.labels),
                "barrier_gap_exclusions": barrier.gap_exclusions,
                "barrier_volatility_exclusions": barrier.volatility_exclusions,
                "barrier_endpoint_exclusions": barrier.endpoint_exclusions,
                "intrabar_stop_first_ties": barrier.intrabar_ties,
                "terminal_gap_exclusions": terminal.gap_exclusions,
                "terminal_nonpositive_exclusions": terminal.nonpositive_exclusions,
                "terminal_endpoint_exclusions": terminal.endpoint_exclusions,
            }
        )
    return pd.DataFrame(rows)


def _score_h4_window(
    frame: pd.DataFrame,
    barrier_labels: pd.DataFrame,
    terminal_returns: pd.Series,
    *,
    horizon: int,
    family: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    origin: str,
    settings: dict,
    sealed_access: bool,
    minute_returns: pd.Series,
    sizer_scale: pd.Series,
) -> tuple[list[dict], list[dict]]:
    train_barriers = select_label_window(
        barrier_labels, horizon, train_start, train_end
    )
    scored_barriers = select_label_window(
        barrier_labels, horizon, score_start, score_end
    )
    train_terminal = select_available(
        terminal_returns, horizon, train_start, train_end
    )
    scored_terminal = select_available(
        terminal_returns, horizon, score_start, score_end
    )
    if len(scored_barriers) < 2 or len(scored_terminal) < 2:
        raise RuntimeError(
            f"H4 {horizon}-minute {origin} window has fewer than two scored labels"
        )
    forecast = fit_first_passage(
        train_barriers,
        smoothing=float(settings["probability_smoothing"]),
    )
    terminal_model = fit_terminal_density(train_terminal, family)
    probability_rows = _h4_probability_rows(
        scored_barriers,
        scored_terminal,
        forecast,
        terminal_model,
        horizon=horizon,
        origin=origin,
        sealed_access=sealed_access,
    )
    economic_rows = _h4_economic_rows(
        frame,
        scored_barriers,
        forecast,
        terminal_model,
        horizon=horizon,
        origin=origin,
        score_start=score_start,
        score_end=score_end,
        settings=settings,
        sealed_access=sealed_access,
        minute_returns=minute_returns,
        sizer_scale=sizer_scale,
    )
    return economic_rows, probability_rows


def _h4_probability_rows(
    barriers: pd.DataFrame,
    terminal: pd.Series,
    forecast,
    terminal_model,
    *,
    horizon: int,
    origin: str,
    sealed_access: bool,
) -> list[dict]:
    specifications = (
        (
            "first_passage_probability",
            "long_target_before_stop",
            forecast.long_target,
            (barriers["long_outcome"] == 1).astype(float),
        ),
        (
            "first_passage_probability",
            "short_target_before_stop",
            forecast.short_target,
            (barriers["short_outcome"] == 1).astype(float),
        ),
        (
            "terminal_return_probability",
            "positive_terminal_return",
            terminal_up_probability(terminal_model),
            (terminal > 0.0).astype(float),
        ),
    )
    rows = []
    for model, event, probability, events in specifications:
        score = score_probability(probability, events)
        rows.append(
            {
                "horizon_minutes": horizon,
                "origin": origin,
                "model": model,
                "event": event,
                "observations": score.observations,
                "probability": score.probability,
                "event_rate": score.event_rate,
                "brier_score": score.brier_score,
                "log_loss": score.log_loss,
                "sealed_access": sealed_access,
            }
        )
    return rows


def _h4_economic_rows(
    frame: pd.DataFrame,
    barriers: pd.DataFrame,
    forecast,
    terminal_model,
    *,
    horizon: int,
    origin: str,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    settings: dict,
    sealed_access: bool,
    minute_returns: pd.Series,
    sizer_scale: pd.Series,
) -> list[dict]:
    minute_index = as_utc_index(minute_returns.index)
    selected = (
        (minute_index >= pd.Timestamp(score_start).tz_convert("UTC"))
        & (minute_index + pd.Timedelta(minutes=1) < pd.Timestamp(score_end).tz_convert("UTC"))
        & minute_index.isin(barriers.index)
    )
    realised = minute_returns.loc[selected]
    if len(realised) < 2:
        raise RuntimeError(f"H4 {horizon}-minute {origin} economic path is too short")
    widths = barriers["barrier_log_width"].reindex(realised.index)
    candidate, hard = first_passage_signals(
        widths,
        forecast,
        cost_bps=float(settings["cost_bps"]),
        round_trip_cost_multiplier=float(settings["round_trip_cost_multiplier"]),
    )
    terminal = terminal_signals(
        realised.index,
        terminal_model,
        cost_bps=float(settings["cost_bps"]),
        round_trip_cost_multiplier=float(settings["round_trip_cost_multiplier"]),
    )
    signals = {
        "always_in_market": pd.Series(1.0, index=realised.index),
        "hard_label_wave": hard,
        "first_passage_probability": candidate,
        "terminal_return_probability": terminal,
    }
    scale = sizer_scale.reindex(realised.index)
    missing_scale = int(scale.isna().sum())
    if missing_scale:
        logger.warning(
            "H4 %dm %s economic path has %d missing volatility scales; positions are zero",
            horizon,
            origin,
            missing_scale,
        )
    scale = scale.fillna(0.0)
    rows = []
    for model in _H4_MODELS:
        position = signals[model].reindex(realised.index).fillna(0.0) * scale
        net = apply_costs(position, realised, float(settings["cost_bps"]))
        summary = performance(net, position)
        sharpe = summary.sharpe if math.isfinite(summary.sharpe) else 0.0
        rows.append(
            {
                "horizon_minutes": horizon,
                "origin": origin,
                "model": model,
                "observations": summary.observations,
                "total_simple_return": summary.total_simple_return,
                "sharpe": sharpe,
                "max_drawdown": summary.max_drawdown,
                "average_turnover": summary.average_turnover,
                "average_absolute_position": summary.average_absolute_position,
                "no_trade_rate": float((signals[model] == 0.0).mean()),
                "sealed_access": sealed_access,
            }
        )
    return rows


def h4_economic_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Compare the primary H4 endpoint with both registered controls."""
    required = {
        "horizon_minutes",
        "origin",
        "model",
        "sharpe",
        "expected_origins",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H4 economic scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError("H4 economic comparison has no scores")
    pivot = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="sharpe",
        aggfunc="first",
    )
    needed = {"first_passage_probability", "hard_label_wave", "always_in_market"}
    if not needed.issubset(pivot.columns):
        raise RuntimeError(
            f"H4 comparison needs {sorted(needed)}, found {sorted(pivot.columns)}"
        )
    first = pivot["first_passage_probability"]
    rows = []
    expected_by_horizon = scores.groupby("horizon_minutes")["expected_origins"].max()
    for horizon, values in first.groupby(level="horizon_minutes"):
        candidate = values.droplevel("horizon_minutes")
        horizon_scores = pivot.xs(horizon, level="horizon_minutes")
        hard = horizon_scores["hard_label_wave"]
        always = horizon_scores["always_in_market"]
        rows.append(
            {
                "horizon_minutes": int(horizon),
                "mean_sharpe": float(candidate.mean()),
                "median_sharpe": float(candidate.median()),
                "mean_vs_hard_label": float((candidate - hard).mean()),
                "median_vs_hard_label": float((candidate - hard).median()),
                "mean_vs_always": float((candidate - always).mean()),
                "median_vs_always": float((candidate - always).median()),
                "origins": int(candidate.size),
                "expected_origins": int(expected_by_horizon.loc[horizon]),
            }
        )
    return pd.DataFrame(rows)


def h4_development_decision(comparison: pd.DataFrame) -> pd.DataFrame:
    """Apply the registered all-horizons after-cost Sharpe conjunction."""
    required = {
        "horizon_minutes",
        "mean_vs_hard_label",
        "median_vs_hard_label",
        "mean_vs_always",
        "median_vs_always",
        "origins",
        "expected_origins",
    }
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H4 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H4 decision has no horizons")
    if (comparison["origins"] != comparison["expected_origins"]).any():
        return pd.DataFrame(
            [
                {
                    "null_decision": "defer",
                    "benchmark": "first_passage_probability",
                    "reason": "H4 evidence is incomplete because one or more registered origins failed",
                }
            ]
        )
    columns = [
        "mean_vs_hard_label",
        "median_vs_hard_label",
        "mean_vs_always",
        "median_vs_always",
    ]
    if not comparison[columns].map(math.isfinite).all().all():
        raise ValueError("H4 Sharpe advantages must be finite")
    wins = (comparison[columns] > 0.0).all(axis=1)
    if bool(wins.all()):
        decision = "reject"
        benchmark = "first_passage_probability"
        reason = "first-passage decisions beat both controls in mean and median Sharpe at every horizon"
    else:
        decision = "accept"
        benchmark = "hard_label_wave"
        reason = "first-passage decisions do not beat both controls at every horizon"
    return pd.DataFrame(
        [{"null_decision": decision, "benchmark": benchmark, "reason": reason}]
    )


@dataclass(frozen=True)
class H4Confirmation:
    """One sealed H4 economic comparison and its registry update."""

    economic_scores: pd.DataFrame
    probability_scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    h1_family: str


def confirm_h4(
    bars: pd.DataFrame,
    record: ExperimentRecord,
    h1_record: ExperimentRecord,
    *,
    dataset_manifest: DatasetManifest,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H4Confirmation:
    """Fit before the boundary and consume the H4 sealed decision stream once."""
    version = require_committed(code_version(root))
    _require_open_h4(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h4(stored)
    verify_manifest(dataset_manifest)
    mutable = [
        item.key
        for item in dataset_manifest.objects
        if item.version_id in {None, "", "null"}
    ]
    if mutable:
        raise RuntimeError(
            "H4 confirmation needs immutable version IDs for every dataset object; "
            f"mutable objects={mutable}"
        )
    digest = _sha256_digest(dataset_manifest.digest)
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation dataset {digest} does not match recorded freeze {stored.dataset_digest}"
        )
    benchmark = h1_benchmark(h1_record)
    if (
        not benchmark.resolved
        or h1_record.decision not in {"retain", "reject"}
        or not h1_record.sealed_access
    ):
        raise RuntimeError(f"H4 confirmation needs a resolved sealed H1 benchmark: {benchmark.reason}")
    settings = dict(H4_CONFIG)
    if config:
        settings.update(config)
    _require_h4_protocol(settings)
    configuration = config_digest(settings)
    if not stored.config_digest:
        raise RuntimeError("H4 confirmation needs a pre-registered configuration digest")
    if stored.config_digest != configuration:
        raise RuntimeError(
            f"H4 confirmation config {configuration} does not match "
            f"pre-registered config {stored.config_digest}"
        )
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(stored.horizons) or horizons != tuple(h1_record.horizons):
        raise ValueError("H4 confirmation horizons must match H4 and H1 records")
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning(
            "H4 confirmation has %d unavailable one-minute economic returns",
            minute_exclusions,
        )
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    economic_rows = []
    probability_rows = []
    sealed_end = frame.index[-1] + pd.Timedelta(minutes=1)
    for horizon in horizons:
        barrier, terminal = builds[horizon]
        economic, probability = _score_h4_window(
            frame,
            barrier.labels,
            terminal.returns,
            horizon=horizon,
            family=benchmark.model,
            train_start=frame.index.min(),
            train_end=boundary,
            score_start=boundary,
            score_end=sealed_end,
            origin="sealed",
            settings=settings,
            sealed_access=True,
            minute_returns=minute_returns,
            sizer_scale=sizer_scale,
        )
        economic_rows.extend(economic)
        probability_rows.extend(probability)
    for row in economic_rows:
        row["expected_origins"] = 1
    for row in probability_rows:
        row["expected_origins"] = 1
    economic_scores = pd.DataFrame(economic_rows)
    comparison = h4_economic_comparison(economic_scores)
    verdict = h4_development_decision(comparison).iloc[0]
    decision = "retain" if verdict.null_decision == "reject" else "reject"
    status = "Research validated" if decision == "retain" else "Rejected"
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        result=str(verdict.benchmark),
        decision=decision,
        dataset_id=dataset_manifest.dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H4 confirmatory decision=%s result=%s run=%s boundary=%s",
        decision,
        verdict.benchmark,
        run_id,
        boundary.isoformat(),
    )
    return H4Confirmation(
        economic_scores=economic_scores,
        probability_scores=pd.DataFrame(probability_rows),
        record=updated,
        boundary=boundary,
        h1_family=benchmark.model,
    )


def _require_open_h4(record: ExperimentRecord) -> None:
    if record.experiment_id != "EXP-H04":
        raise ValueError(f"H4 confirmation needs EXP-H04, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H01":
        raise ValueError(f"H4 confirmation needs parent EXP-H01, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H4 sealed test has already been used: "
            f"decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_h4_protocol(settings: dict) -> None:
    if settings.get("experiment_id") != "EXP-H04":
        raise ValueError("H4 protocol must use EXP-H04")
    if settings.get("parent_experiment_id") != "EXP-H01":
        raise ValueError("H4 protocol must use EXP-H01 as its parent")
    horizons = tuple(int(item) for item in settings.get("horizons_minutes", ()))
    if not horizons or len(set(horizons)) != len(horizons) or min(horizons) < 1:
        raise ValueError("H4 horizons must be unique positive minutes")
    if settings.get("primary_endpoint") != "first_passage_probability":
        raise ValueError("H4 primary endpoint must be first_passage_probability")
    if settings.get("secondary_endpoint") != "terminal_return_probability":
        raise ValueError("H4 secondary endpoint must be terminal_return_probability")
    if settings.get("barrier") != "symmetric_trailing_volatility":
        raise ValueError("H4 barriers must be symmetric and trailing-volatility scaled")
    if settings.get("intrabar_rule") != "stop_first":
        raise ValueError("H4 intrabar ties must resolve stop_first")
    if settings.get("sizer") != "volatility_targeted":
        raise ValueError("H4 must use the frozen volatility-targeted sizer")
    if tuple(settings.get("benchmarks", ())) != (
        "hard_label_wave",
        "always_in_market",
    ):
        raise ValueError("H4 benchmarks must be hard_label_wave then always_in_market")
    if settings.get("economic_metric") != "sharpe":
        raise ValueError("H4 primary economic metric must be Sharpe")
    if settings.get("economic_multiple_testing") != "all_horizons_conjunction":
        raise ValueError("H4 economic family must use the all-horizons conjunction rule")
    if float(settings.get("barrier_sigma", 0.0)) <= 0.0:
        raise ValueError("H4 barrier sigma must be positive")
    if int(settings.get("barrier_volatility_lookback_minutes", 0)) < 2:
        raise ValueError("H4 volatility lookback must cover at least two minutes")
    if float(settings.get("probability_smoothing", 0.0)) <= 0.0:
        raise ValueError("H4 probability smoothing must be positive")
    if float(settings.get("cost_bps", -1.0)) < 0.0:
        raise ValueError("H4 cost must be non-negative")
    if float(settings.get("round_trip_cost_multiplier", 0.0)) <= 0.0:
        raise ValueError("H4 round-trip cost multiplier must be positive")
    if float(settings.get("target_annual_volatility", 0.0)) <= 0.0:
        raise ValueError("H4 volatility target must be positive")
    if float(settings.get("max_gross", 0.0)) <= 0.0:
        raise ValueError("H4 gross cap must be positive")


@dataclass(frozen=True)
class H5Evaluation:
    """Development-only economic and risk evidence for Tide constraints."""

    risk_scores: pd.DataFrame
    diagnostics: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp
    parent_rule: str
    sealed_access: bool = False


def evaluate_h5(
    bars: pd.DataFrame,
    h4_record: ExperimentRecord,
    config: dict | None = None,
) -> H5Evaluation:
    """Evaluate entry-time Tide constraints without reading the sealed year."""
    settings = dict(H5_CONFIG)
    if config:
        settings.update(config)
    _require_h5_protocol(settings)
    parent_rule = _h5_parent_rule(h4_record, settings, require_confirmed=False)
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    origins = rolling_origins(
        frame.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning("H5 development has %d unavailable one-minute returns", minute_exclusions)
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    rows: list[dict] = []
    diagnostics: list[dict] = []
    failures: list[dict] = []
    for horizon in (int(item) for item in settings["horizons_minutes"]):
        barrier, terminal = builds[horizon]
        for origin in origins:
            try:
                scored, audit = _score_h5_window(
                    frame,
                    barrier.labels,
                    terminal.returns,
                    horizon=horizon,
                    parent_rule=parent_rule,
                    train_start=origin.train_start,
                    train_end=origin.train_end,
                    score_start=origin.train_end,
                    score_end=origin.validation_end,
                    origin=origin.name,
                    settings=settings,
                    sealed_access=False,
                    minute_returns=minute_returns,
                    sizer_scale=sizer_scale,
                )
            except (ValueError, RuntimeError, FloatingPointError, ArithmeticError) as exc:
                failure = {
                    "horizon_minutes": horizon,
                    "origin": origin.name,
                    "error": str(exc),
                }
                failures.append(failure)
                logger.warning(
                    "H5 development origin failed horizon=%d origin=%s: %s",
                    horizon,
                    origin.name,
                    exc,
                )
                continue
            rows.extend(scored)
            diagnostics.extend(audit)
    expected = len(origins)
    for row in rows:
        row["expected_origins"] = expected
    return H5Evaluation(
        risk_scores=pd.DataFrame(rows),
        diagnostics=pd.DataFrame(diagnostics),
        failures=tuple(failures),
        boundary=boundary,
        parent_rule=parent_rule,
    )


def _score_h5_window(
    frame: pd.DataFrame,
    barrier_labels: pd.DataFrame,
    terminal_returns: pd.Series,
    *,
    horizon: int,
    parent_rule: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    origin: str,
    settings: dict,
    sealed_access: bool,
    minute_returns: pd.Series,
    sizer_scale: pd.Series,
) -> tuple[list[dict], list[dict]]:
    del frame
    train_barriers = select_label_window(barrier_labels, horizon, train_start, train_end)
    scored_barriers = select_label_window(barrier_labels, horizon, score_start, score_end)
    train_horizon_log = select_available(
        terminal_returns,
        horizon,
        train_start,
        train_end,
    )
    train_minute = select_available(minute_returns, 1, train_start, train_end)
    if len(scored_barriers) < 2 or len(train_horizon_log) < 2 or len(train_minute) < 2:
        raise RuntimeError(f"H5 {horizon}-minute {origin} window has insufficient observations")
    forecast = fit_first_passage(
        train_barriers,
        smoothing=float(settings["probability_smoothing"]),
    )
    minute_index = as_utc_index(minute_returns.index)
    selected = (
        (minute_index >= pd.Timestamp(score_start).tz_convert("UTC"))
        & (minute_index + pd.Timedelta(minutes=1) < pd.Timestamp(score_end).tz_convert("UTC"))
        & minute_index.isin(scored_barriers.index)
    )
    realised = minute_returns.loc[selected]
    if len(realised) < 2:
        raise RuntimeError(f"H5 {horizon}-minute {origin} economic path is too short")
    widths = scored_barriers["barrier_log_width"].reindex(realised.index)
    candidate, hard = first_passage_signals(
        widths,
        forecast,
        cost_bps=float(settings["cost_bps"]),
        round_trip_cost_multiplier=float(H4_CONFIG["round_trip_cost_multiplier"]),
    )
    signals = {
        "first_passage_probability": candidate,
        "hard_label_wave": hard,
    }
    scale = sizer_scale.reindex(realised.index)
    missing_scale = int(scale.isna().sum())
    if missing_scale:
        logger.warning(
            "H5 %dm %s has %d missing frozen volatility scales; positions are zero",
            horizon,
            origin,
            missing_scale,
        )
    parent = signals[parent_rule].reindex(realised.index).fillna(0.0) * scale.fillna(0.0)
    calibration = fit_risk_calibration(
        pd.Series(np.expm1(train_horizon_log.to_numpy(dtype=float)), index=train_horizon_log.index),
        train_minute,
        confidence=float(settings["es_confidence"]),
    )
    volatility_ratio = causal_volatility_ratio(
        minute_returns,
        realised.index,
        lookback_minutes=int(settings["volatility_lookback_minutes"]),
        reference_volatility=calibration.reference_volatility,
        ratio_floor=float(settings["volatility_ratio_floor"]),
        ratio_cap=float(settings["volatility_ratio_cap"]),
    )
    rows = [
        _h5_score_path(
            parent,
            realised,
            policy="unconstrained_h4",
            confidence=float(settings["es_confidence"]),
            cost_bps=float(settings["cost_bps"]),
            horizon=horizon,
            origin=origin,
            sealed_access=sealed_access,
            counts=None,
            diagnostics=None,
        )
    ]
    audit_rows: list[dict] = []
    for policy in settings["budget_policies"]:
        constrained = apply_entry_constraint(
            parent,
            volatility_ratio,
            calibration,
            policy=str(policy),
            budget_fraction=float(settings["budget_fraction"]),
            stress_trigger_ratio=float(settings["stress_trigger_ratio"]),
            stress_budget_multiplier=float(settings["stress_budget_multiplier"]),
            max_gross=float(settings["max_gross"]),
        )
        rows.append(
            _h5_score_path(
                constrained.position,
                realised,
                policy=str(policy),
                confidence=float(settings["es_confidence"]),
                cost_bps=float(settings["cost_bps"]),
                horizon=horizon,
                origin=origin,
                sealed_access=sealed_access,
                counts=constrained.counts,
                diagnostics=constrained.diagnostics,
            )
        )
        post_risk = (
            constrained.position.abs()
            * calibration.expected_shortfall
            * constrained.diagnostics["volatility_ratio"]
        )
        available = ~constrained.diagnostics["risk_unavailable"]
        breaches = int(
            (
                post_risk.loc[available]
                > constrained.diagnostics.loc[available, "budget"] + 1e-12
            ).sum()
        )
        audit_rows.append(
            {
                "horizon_minutes": horizon,
                "origin": origin,
                "policy": str(policy),
                "training_observations": calibration.observations,
                "training_expected_shortfall": calibration.expected_shortfall,
                "training_reference_volatility": calibration.reference_volatility,
                "constraint_breaches": breaches,
                "sealed_access": sealed_access,
            }
        )
    return rows, audit_rows


def _h5_score_path(
    position: pd.Series,
    realised: pd.Series,
    *,
    policy: str,
    confidence: float,
    cost_bps: float,
    horizon: int,
    origin: str,
    sealed_access: bool,
    counts,
    diagnostics: pd.DataFrame | None,
) -> dict:
    net = apply_costs(position, realised, cost_bps)
    summary = performance(net, position)
    realised_es = historical_expected_shortfall(net, confidence)
    liquidations = liquidation_event_count(net)
    if counts is None:
        es_binds = margin_binds = unavailable = 0
        utilisation = 0.0
    else:
        es_binds = counts.es_binds
        margin_binds = counts.margin_binds
        unavailable = counts.unavailable_risk_estimates
        valid_budget = diagnostics["budget"] > 0.0
        utilisation_values = (
            diagnostics.loc[valid_budget, "estimated_es"]
            / diagnostics.loc[valid_budget, "budget"]
        )
        utilisation = (
            float(utilisation_values.median()) if not utilisation_values.empty else 0.0
        )
    return {
        "horizon_minutes": horizon,
        "origin": origin,
        "policy": policy,
        "observations": summary.observations,
        "mean_after_cost_return": float(net.mean()),
        "total_simple_return": summary.total_simple_return,
        "sharpe": summary.sharpe if math.isfinite(summary.sharpe) else 0.0,
        "max_drawdown": summary.max_drawdown,
        "realised_es": realised_es,
        "average_turnover": summary.average_turnover,
        "average_absolute_position": summary.average_absolute_position,
        "es_binds": int(es_binds),
        "margin_binds": int(margin_binds),
        "unavailable_risk_estimates": int(unavailable),
        "liquidation_events": int(liquidations),
        "median_budget_utilisation": utilisation,
        "sealed_access": sealed_access,
    }


def h5_policy_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Compare every registered policy with the identical unconstrained H4 path."""
    required = {
        "horizon_minutes",
        "origin",
        "policy",
        "realised_es",
        "mean_after_cost_return",
        "expected_origins",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H5 risk scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError("H5 policy comparison has no scores")
    policies = [str(item) for item in H5_CONFIG["budget_policies"] if item in set(scores["policy"])]
    if not policies:
        raise RuntimeError("H5 comparison has no registered budget policy")
    expected_by_horizon = scores.groupby("horizon_minutes")["expected_origins"].max()
    rows: list[dict] = []
    policy_pvalues: dict[str, float] = {}
    for policy in policies:
        horizon_pvalues = []
        for horizon in sorted(int(value) for value in scores["horizon_minutes"].unique()):
            part = scores.loc[scores["horizon_minutes"] == horizon]
            es = part.pivot_table(index="origin", columns="policy", values="realised_es", aggfunc="first")
            mean = part.pivot_table(
                index="origin",
                columns="policy",
                values="mean_after_cost_return",
                aggfunc="first",
            )
            needed = {"unconstrained_h4", policy}
            if not needed.issubset(es.columns) or not needed.issubset(mean.columns):
                raise RuntimeError(f"H5 comparison at {horizon}m is missing {sorted(needed)}")
            joined = es[list(needed)].dropna()
            mean_joined = mean[list(needed)].dropna()
            common = joined.index.intersection(mean_joined.index)
            joined = joined.loc[common]
            mean_joined = mean_joined.loc[common]
            reductions = joined["unconstrained_h4"] - joined[policy]
            pvalue = one_sided_sign_pvalue(reductions)
            horizon_pvalues.append(pvalue)
            parent_mean = float(mean_joined["unconstrained_h4"].mean())
            candidate_mean = float(mean_joined[policy].mean())
            threshold = (
                parent_mean * float(H5_CONFIG["mean_return_retention"])
                if parent_mean >= 0.0
                else parent_mean
            )
            retention = (
                candidate_mean / parent_mean if parent_mean > 0.0 else math.nan
            )
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "policy": policy,
                    "mean_parent_es": float(joined["unconstrained_h4"].mean()),
                    "mean_policy_es": float(joined[policy].mean()),
                    "mean_es_reduction": float(reductions.mean()),
                    "median_es_reduction": float(reductions.median()),
                    "mean_parent_return": parent_mean,
                    "mean_policy_return": candidate_mean,
                    "mean_return_retention": retention,
                    "mean_noninferiority": candidate_mean >= threshold,
                    "es_sign_pvalue": pvalue,
                    "origins": int(len(common)),
                    "expected_origins": int(expected_by_horizon.loc[horizon]),
                }
            )
        policy_pvalues[policy] = max(horizon_pvalues)
    adjusted = holm_adjust(policy_pvalues)
    for row in rows:
        row["holm_policy_pvalue"] = adjusted[str(row["policy"])]
    return pd.DataFrame(rows)


def h5_development_decision(
    comparison: pd.DataFrame,
    config: dict | None = None,
) -> pd.DataFrame:
    """Apply the registered all-horizon ES/non-inferiority/Holm conjunction."""
    settings = dict(H5_CONFIG)
    if config:
        settings.update(config)
    _require_h5_protocol(settings)
    required = {
        "policy",
        "mean_es_reduction",
        "mean_noninferiority",
        "holm_policy_pvalue",
        "origins",
        "expected_origins",
    }
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H5 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H5 decision has no policy evidence")
    if (comparison["origins"] != comparison["expected_origins"]).any():
        return pd.DataFrame(
            [{
                "null_decision": "defer",
                "benchmark": "unconstrained_h4",
                "reason": "H5 evidence is incomplete because one or more registered origins failed",
            }]
        )
    if not comparison[["mean_es_reduction", "holm_policy_pvalue"]].map(math.isfinite).all().all():
        raise ValueError("H5 ES effects and corrected p-values must be finite")
    passing = []
    for policy, rows in comparison.groupby("policy"):
        if (
            bool((rows["mean_es_reduction"] > 0.0).all())
            and bool(rows["mean_noninferiority"].all())
            and float(rows["holm_policy_pvalue"].iloc[0]) <= float(settings["family_alpha"])
        ):
            passing.append((str(policy), float(rows["mean_es_reduction"].mean())))
    if passing:
        selected = max(passing, key=lambda item: (item[1], item[0]))[0]
        return pd.DataFrame(
            [{
                "null_decision": "reject",
                "benchmark": selected,
                "reason": (
                    "the selected Tide budget lowers 97.5% ES at every horizon, "
                    "passes the 80% mean-return floor, and survives Holm correction"
                ),
            }]
        )
    return pd.DataFrame(
        [{
            "null_decision": "accept",
            "benchmark": "unconstrained_h4",
            "reason": (
                "no Tide budget lowers ES at every horizon while preserving the "
                "registered mean-return floor under Holm correction"
            ),
        }]
    )


@dataclass(frozen=True)
class H5Confirmation:
    """One sealed H5 comparison and its registry update."""

    risk_scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    parent_rule: str


def confirm_h5(
    bars: pd.DataFrame,
    record: ExperimentRecord,
    h4_record: ExperimentRecord,
    *,
    dataset_manifest: DatasetManifest,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H5Confirmation:
    """Consume the sealed H5 path once, after H4 itself is sealed-retained."""
    version = require_committed(code_version(root))
    _require_open_h5(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(f"record path belongs to {stored.experiment_id}, not {record.experiment_id}")
    _require_open_h5(stored)
    settings = dict(H5_CONFIG)
    if config:
        settings.update(config)
    _require_h5_protocol(settings)
    parent_rule = _h5_parent_rule(h4_record, settings, require_confirmed=True)
    if stored.result not in set(settings["budget_policies"]):
        raise RuntimeError("H5 confirmation needs one development-selected budget policy")
    verify_manifest(dataset_manifest)
    mutable = [item.key for item in dataset_manifest.objects if item.version_id in {None, "", "null"}]
    if mutable:
        raise RuntimeError(
            "H5 confirmation needs immutable version IDs for every dataset object; "
            f"mutable objects={mutable}"
        )
    digest = _sha256_digest(dataset_manifest.digest)
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation dataset {digest} does not match recorded freeze {stored.dataset_digest}"
        )
    configuration = config_digest(settings)
    if not stored.config_digest or stored.config_digest != configuration:
        raise RuntimeError("H5 confirmation configuration does not match the pre-registered digest")
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(stored.horizons) or horizons != tuple(h4_record.horizons):
        raise ValueError("H5 confirmation horizons must match H5 and H4 records")
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning("H5 confirmation has %d unavailable one-minute returns", minute_exclusions)
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    rows: list[dict] = []
    sealed_end = frame.index[-1] + pd.Timedelta(minutes=1)
    for horizon in horizons:
        barrier, terminal = builds[horizon]
        scored, _ = _score_h5_window(
            frame,
            barrier.labels,
            terminal.returns,
            horizon=horizon,
            parent_rule=parent_rule,
            train_start=frame.index.min(),
            train_end=boundary,
            score_start=boundary,
            score_end=sealed_end,
            origin="sealed",
            settings=settings,
            sealed_access=True,
            minute_returns=minute_returns,
            sizer_scale=sizer_scale,
        )
        rows.extend(
            row for row in scored
            if row["policy"] in {"unconstrained_h4", stored.result}
        )
    risk_scores = pd.DataFrame(rows)
    retain = True
    for horizon in horizons:
        part = risk_scores.loc[risk_scores["horizon_minutes"] == horizon].set_index("policy")
        control = part.loc["unconstrained_h4"]
        candidate = part.loc[stored.result]
        threshold = (
            float(control.mean_after_cost_return) * float(settings["mean_return_retention"])
            if float(control.mean_after_cost_return) >= 0.0
            else float(control.mean_after_cost_return)
        )
        retain &= (
            float(candidate.realised_es) < float(control.realised_es)
            and float(candidate.mean_after_cost_return) >= threshold
        )
    decision = "retain" if retain else "reject"
    status = "Research validated" if retain else "Rejected"
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        decision=decision,
        dataset_id=dataset_manifest.dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H5 confirmatory decision=%s result=%s run=%s boundary=%s",
        decision,
        stored.result,
        run_id,
        boundary.isoformat(),
    )
    return H5Confirmation(
        risk_scores=risk_scores,
        record=updated,
        boundary=boundary,
        parent_rule=parent_rule,
    )


def _h5_parent_rule(
    h4_record: ExperimentRecord,
    settings: dict,
    *,
    require_confirmed: bool,
) -> str:
    if h4_record.experiment_id != "EXP-H04":
        raise ValueError(f"H5 needs EXP-H04 as its parent, got {h4_record.experiment_id}")
    if h4_record.dataset_id != settings["dataset_id"]:
        raise ValueError("H5 and H4 dataset identifiers must match")
    if tuple(h4_record.horizons) != tuple(int(item) for item in settings["horizons_minutes"]):
        raise ValueError("H5 and H4 horizons must match")
    if h4_record.result not in set(settings["parent_rules"]):
        raise RuntimeError(f"H5 cannot reconstruct H4 parent rule {h4_record.result!r}")
    if require_confirmed and not (
        h4_record.status == "Research validated"
        and h4_record.decision == "retain"
        and h4_record.sealed_access
    ):
        raise RuntimeError("H5 confirmation requires sealed-retained H4")
    return h4_record.result


def _require_open_h5(record: ExperimentRecord) -> None:
    if record.experiment_id != "EXP-H05":
        raise ValueError(f"H5 confirmation needs EXP-H05, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H04":
        raise ValueError(f"H5 confirmation needs parent EXP-H04, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H5 sealed test has already been used: "
            f"decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_h5_protocol(settings: dict) -> None:
    if settings.get("experiment_id") != "EXP-H05":
        raise ValueError("H5 protocol must use EXP-H05")
    if settings.get("parent_experiment_id") != "EXP-H04":
        raise ValueError("H5 protocol must use EXP-H04 as its parent")
    horizons = tuple(int(item) for item in settings.get("horizons_minutes", ()))
    if not horizons or len(set(horizons)) != len(horizons) or min(horizons) < 1:
        raise ValueError("H5 horizons must be unique positive minutes")
    if tuple(settings.get("budget_policies", ())) != POLICIES:
        raise ValueError("H5 budget policy ladder is not the registered ladder")
    if settings.get("multiple_testing") != "holm_policy_family_all_horizons_conjunction":
        raise ValueError("H5 must use the registered Holm all-horizons rule")
    if settings.get("sizer") != "volatility_targeted":
        raise ValueError("H5 must hold the H4 volatility-targeted sizer fixed")
    if settings.get("barrier") != "symmetric_trailing_volatility":
        raise ValueError("H5 must hold the H4 symmetric trailing barrier fixed")
    if settings.get("intrabar_rule") != "stop_first":
        raise ValueError("H5 must hold the H4 stop_first tie rule fixed")
    confidence = float(settings.get("es_confidence", 0.0))
    if not 0.5 < confidence < 1.0:
        raise ValueError("H5 ES confidence must be in (0.5, 1)")
    if not 0.0 < float(settings.get("budget_fraction", 0.0)) <= 1.0:
        raise ValueError("H5 budget fraction must be in (0, 1]")
    if int(settings.get("volatility_lookback_minutes", 0)) < 2:
        raise ValueError("H5 volatility lookback must cover at least two returns")
    ratio_floor = float(settings.get("volatility_ratio_floor", 0.0))
    ratio_cap = float(settings.get("volatility_ratio_cap", 0.0))
    if not 0.0 < ratio_floor <= ratio_cap:
        raise ValueError("H5 volatility ratio bounds are invalid")
    if not 0.0 < float(settings.get("stress_budget_multiplier", 0.0)) <= 1.0:
        raise ValueError("H5 stress multiplier must be in (0, 1]")
    if float(settings.get("stress_trigger_ratio", 0.0)) <= 0.0:
        raise ValueError("H5 stress trigger must be positive")
    if not 0.0 < float(settings.get("mean_return_retention", 0.0)) <= 1.0:
        raise ValueError("H5 mean-return retention must be in (0, 1]")
    if not 0.0 < float(settings.get("family_alpha", 0.0)) < 1.0:
        raise ValueError("H5 family alpha must be in (0, 1)")
    if float(settings.get("max_gross", 0.0)) != float(H4_CONFIG["max_gross"]):
        raise ValueError("H5 must inherit H4 max gross")
    if float(settings.get("cost_bps", -1.0)) != float(H4_CONFIG["cost_bps"]):
        raise ValueError("H5 must inherit H4 costs")


@dataclass(frozen=True)
class H6Evaluation:
    """Development-only sizing evidence under one retained H5 budget."""

    scores: pd.DataFrame
    diagnostics: pd.DataFrame
    failures: tuple[dict, ...]
    boundary: pd.Timestamp | None
    parent_rule: str
    budget_policy: str
    deferred: bool = False
    defer_reason: str = ""
    sealed_access: bool = False


def evaluate_h6(
    bars: pd.DataFrame,
    h5_record: ExperimentRecord,
    h4_record: ExperimentRecord,
    config: dict | None = None,
) -> H6Evaluation:
    """Compare registered sizers under H5, or defer before reading the market."""
    settings = dict(H6_CONFIG)
    if config:
        settings.update(config)
    _require_h6_protocol(settings)
    parent_rule = _h6_parent_rule(h4_record, settings, require_confirmed=False)
    budget_policy = _h6_budget_policy(h5_record, settings, require_confirmed=False)
    if budget_policy is None:
        return H6Evaluation(
            scores=pd.DataFrame(),
            diagnostics=pd.DataFrame(),
            failures=(),
            boundary=None,
            parent_rule=parent_rule,
            budget_policy="",
            deferred=True,
            defer_reason=_h6_defer_reason(h5_record),
        )
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    origins = rolling_origins(
        frame.index,
        boundary,
        train_months=int(settings["train_months"]),
        validation_months=int(settings["validation_months"]),
        step_months=int(settings["step_months"]),
    )
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning("H6 development has %d unavailable one-minute returns", minute_exclusions)
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    rows: list[dict] = []
    diagnostics: list[dict] = []
    failures: list[dict] = []
    for horizon in (int(item) for item in settings["horizons_minutes"]):
        barrier, terminal = builds[horizon]
        for origin in origins:
            try:
                scored, audit = _score_h6_window(
                    barrier.labels,
                    terminal.returns,
                    horizon=horizon,
                    parent_rule=parent_rule,
                    budget_policy=budget_policy,
                    train_start=origin.train_start,
                    train_end=origin.train_end,
                    score_start=origin.train_end,
                    score_end=origin.validation_end,
                    origin=origin.name,
                    settings=settings,
                    sealed_access=False,
                    minute_returns=minute_returns,
                    sizer_scale=sizer_scale,
                )
            except (ValueError, RuntimeError, FloatingPointError, ArithmeticError) as exc:
                failure = {
                    "horizon_minutes": horizon,
                    "origin": origin.name,
                    "error": str(exc),
                }
                failures.append(failure)
                logger.warning(
                    "H6 development origin failed horizon=%d origin=%s: %s",
                    horizon,
                    origin.name,
                    exc,
                )
                continue
            rows.extend(scored)
            diagnostics.extend(audit)
    expected = len(origins)
    for row in rows:
        row["expected_origins"] = expected
    return H6Evaluation(
        scores=pd.DataFrame(rows),
        diagnostics=pd.DataFrame(diagnostics),
        failures=tuple(failures),
        boundary=boundary,
        parent_rule=parent_rule,
        budget_policy=budget_policy,
    )


def _score_h6_window(
    barrier_labels: pd.DataFrame,
    terminal_returns: pd.Series,
    *,
    horizon: int,
    parent_rule: str,
    budget_policy: str,
    train_start: pd.Timestamp,
    train_end: pd.Timestamp,
    score_start: pd.Timestamp,
    score_end: pd.Timestamp,
    origin: str,
    settings: dict,
    sealed_access: bool,
    minute_returns: pd.Series,
    sizer_scale: pd.Series,
) -> tuple[list[dict], list[dict]]:
    train_barriers = select_label_window(barrier_labels, horizon, train_start, train_end)
    scored_barriers = select_label_window(barrier_labels, horizon, score_start, score_end)
    train_horizon_log = select_available(terminal_returns, horizon, train_start, train_end)
    train_minute = select_available(minute_returns, 1, train_start, train_end)
    if len(scored_barriers) < 2 or len(train_horizon_log) < 2 or len(train_minute) < 2:
        raise RuntimeError(f"H6 {horizon}-minute {origin} window has insufficient observations")
    forecast = fit_first_passage(
        train_barriers,
        smoothing=float(settings["probability_smoothing"]),
    )
    minute_index = as_utc_index(minute_returns.index)
    selected = (
        (minute_index >= pd.Timestamp(score_start).tz_convert("UTC"))
        & (minute_index + pd.Timedelta(minutes=1) < pd.Timestamp(score_end).tz_convert("UTC"))
        & minute_index.isin(scored_barriers.index)
    )
    realised = minute_returns.loc[selected]
    if len(realised) < 2:
        raise RuntimeError(f"H6 {horizon}-minute {origin} economic path is too short")
    round_trip_cost = (
        float(settings["cost_bps"]) / 10_000.0 * float(settings["round_trip_cost_multiplier"])
    )
    widths = scored_barriers["barrier_log_width"].reindex(realised.index)
    candidate, hard = first_passage_signals(
        widths,
        forecast,
        cost_bps=float(settings["cost_bps"]),
        round_trip_cost_multiplier=float(settings["round_trip_cost_multiplier"]),
    )
    signals = {
        "first_passage_probability": candidate,
        "hard_label_wave": hard,
    }
    signal = signals[parent_rule].reindex(realised.index).fillna(0.0)
    scale = sizer_scale.reindex(realised.index)
    calibration = fit_risk_calibration(
        pd.Series(np.expm1(train_horizon_log.to_numpy(dtype=float)), index=train_horizon_log.index),
        train_minute,
        confidence=float(settings["es_confidence"]),
    )
    volatility_ratio = causal_volatility_ratio(
        minute_returns,
        realised.index,
        lookback_minutes=int(settings["volatility_lookback_minutes"]),
        reference_volatility=calibration.reference_volatility,
        ratio_floor=float(settings["volatility_ratio_floor"]),
        ratio_cap=float(settings["volatility_ratio_cap"]),
    )
    train_widths = train_barriers["barrier_log_width"]
    train_candidate, train_hard = first_passage_signals(
        train_widths,
        forecast,
        cost_bps=float(settings["cost_bps"]),
        round_trip_cost_multiplier=float(settings["round_trip_cost_multiplier"]),
    )
    train_signals = {
        "first_passage_probability": train_candidate,
        "hard_label_wave": train_hard,
    }
    train_signal = train_signals[parent_rule]
    train_volatility = causal_volatility_ratio(
        minute_returns,
        train_signal.index,
        lookback_minutes=int(settings["volatility_lookback_minutes"]),
        reference_volatility=calibration.reference_volatility,
        ratio_floor=float(settings["volatility_ratio_floor"]),
        ratio_cap=float(settings["volatility_ratio_cap"]),
    )
    denominator_floor = float(settings["denominator_floor"])
    train_opportunity = directional_opportunity(
        train_widths,
        train_signal,
        forecast,
        round_trip_cost=round_trip_cost,
    )
    train_ratios, _ = rorac_ratio(
        train_opportunity,
        calibration.expected_shortfall * train_volatility,
        denominator_floor=denominator_floor,
    )
    normaliser = training_rorac_normaliser(
        train_ratios.where(train_signal.reindex(train_ratios.index).fillna(0.0) != 0.0),
        quantile=float(settings["rorac_quantile"]),
    )
    opportunity = directional_opportunity(
        widths,
        signal,
        forecast,
        round_trip_cost=round_trip_cost,
    )
    ratios, floor_events = rorac_ratio(
        opportunity,
        calibration.expected_shortfall * volatility_ratio,
        denominator_floor=denominator_floor,
    )
    parent_checksum = float(signal.abs().sum())
    rows: list[dict] = []
    audit_rows: list[dict] = []
    budget_checksums: list[float] = []
    for sizer in SIZERS:
        sized = size_position(
            signal,
            sizer=sizer,
            max_gross=float(settings["max_gross"]),
            volatility_scale=scale,
            rorac=ratios,
            normaliser=normaliser,
            denominator_floor_events=floor_events if sizer == "rorac" else None,
        )
        constrained = apply_entry_constraint(
            sized.position,
            volatility_ratio,
            calibration,
            policy=budget_policy,
            budget_fraction=float(settings["budget_fraction"]),
            stress_trigger_ratio=float(settings["stress_trigger_ratio"]),
            stress_budget_multiplier=float(settings["stress_budget_multiplier"]),
            max_gross=float(settings["max_gross"]),
        )
        budget_checksum = float(constrained.diagnostics["budget"].sum())
        budget_checksums.append(budget_checksum)
        post_risk = (
            constrained.position.abs()
            * calibration.expected_shortfall
            * constrained.diagnostics["volatility_ratio"]
        )
        available = ~constrained.diagnostics["risk_unavailable"]
        breaches = int(
            (
                post_risk.loc[available]
                > constrained.diagnostics.loc[available, "budget"] + 1e-12
            ).sum()
        )
        rows.append(
            _h6_score_path(
                constrained.position,
                realised,
                sizer=sizer,
                confidence=float(settings["es_confidence"]),
                cost_bps=float(settings["cost_bps"]),
                horizon=horizon,
                origin=origin,
                budget_policy=budget_policy,
                sealed_access=sealed_access,
                allocation_counts=sized.counts,
                constraint_counts=constrained.counts,
                diagnostics=constrained.diagnostics,
                parent_checksum=parent_checksum,
                budget_checksum=budget_checksum,
                constraint_breaches=breaches,
            )
        )
        audit_rows.append(
            {
                "horizon_minutes": horizon,
                "origin": origin,
                "sizer": sizer,
                "budget_policy": budget_policy,
                "training_rorac_normaliser": normaliser,
                "training_positive_ratios": int((train_ratios.where(train_signal != 0.0) > 0.0).sum()),
                "training_expected_shortfall": calibration.expected_shortfall,
                "constraint_breaches": breaches,
                "denominator_floors": sized.counts.denominator_floors,
                "unavailable_estimates": sized.counts.unavailable_estimates,
                "gross_caps": sized.counts.gross_caps,
                "sealed_access": sealed_access,
            }
        )
    if max(budget_checksums) - min(budget_checksums) > 1e-6:
        raise RuntimeError("H6 sizers did not share one ES budget")
    return rows, audit_rows


def _h6_score_path(
    position: pd.Series,
    realised: pd.Series,
    *,
    sizer: str,
    confidence: float,
    cost_bps: float,
    horizon: int,
    origin: str,
    budget_policy: str,
    sealed_access: bool,
    allocation_counts,
    constraint_counts,
    diagnostics: pd.DataFrame,
    parent_checksum: float,
    budget_checksum: float,
    constraint_breaches: int,
) -> dict:
    net = apply_costs(position, realised, cost_bps)
    summary = performance(net, position)
    realised_es = historical_expected_shortfall(net, confidence)
    valid_budget = diagnostics["budget"] > 0.0
    utilisation_values = (
        diagnostics.loc[valid_budget, "estimated_es"] / diagnostics.loc[valid_budget, "budget"]
    )
    utilisation = float(utilisation_values.median()) if not utilisation_values.empty else 0.0
    return {
        "horizon_minutes": horizon,
        "origin": origin,
        "sizer": sizer,
        "budget_policy": budget_policy,
        "observations": summary.observations,
        "mean_after_cost_return": float(net.mean()),
        "total_simple_return": summary.total_simple_return,
        "sharpe": summary.sharpe if math.isfinite(summary.sharpe) else 0.0,
        "max_drawdown": summary.max_drawdown,
        "realised_es": realised_es,
        "average_turnover": summary.average_turnover,
        "average_absolute_position": summary.average_absolute_position,
        "es_binds": int(constraint_counts.es_binds),
        "margin_binds": int(constraint_counts.margin_binds),
        "unavailable_risk_estimates": int(constraint_counts.unavailable_risk_estimates),
        "unavailable_estimates": int(allocation_counts.unavailable_estimates),
        "denominator_floors": int(allocation_counts.denominator_floors),
        "gross_caps": int(allocation_counts.gross_caps),
        "liquidation_events": int(liquidation_event_count(net)),
        "constraint_breaches": int(constraint_breaches),
        "median_budget_utilisation": utilisation,
        "parent_checksum": parent_checksum,
        "budget_checksum": budget_checksum,
        "sealed_access": sealed_access,
    }


def h6_sizer_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Compare RORAC with both controls under one shared budget."""
    required = {
        "horizon_minutes",
        "origin",
        "sizer",
        "sharpe",
        "constraint_breaches",
        "expected_origins",
        "budget_policy",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H6 allocation scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError("H6 sizer comparison has no scores")
    if "sealed_access" in scores.columns and bool(scores["sealed_access"].fillna(False).any()):
        raise RuntimeError("H6 comparison cannot include sealed observations")
    policies = sorted(set(scores["budget_policy"].astype(str)))
    if policies != [policies[0]] or policies[0] not in POLICIES:
        raise RuntimeError("H6 comparison must use one ES budget")
    needed = set(SIZERS)
    if not needed.issubset(set(scores["sizer"])):
        raise RuntimeError(f"H6 comparison needs {sorted(needed)}")
    expected_by_horizon = scores.groupby("horizon_minutes")["expected_origins"].max()
    rows: list[dict] = []
    control_pvalues: dict[str, float] = {}
    for control in CONTROLS:
        horizon_pvalues = []
        for horizon in sorted(int(value) for value in scores["horizon_minutes"].unique()):
            part = scores.loc[scores["horizon_minutes"] == horizon]
            sharpe = part.pivot_table(
                index="origin",
                columns="sizer",
                values="sharpe",
                aggfunc="first",
            )
            breaches = part.pivot_table(
                index="origin",
                columns="sizer",
                values="constraint_breaches",
                aggfunc="first",
            )
            if not needed.issubset(sharpe.columns) or not needed.issubset(breaches.columns):
                raise RuntimeError(f"H6 comparison at {horizon}m is missing a registered sizer")
            common = sharpe.dropna().index.intersection(breaches.dropna().index)
            sharpe = sharpe.loc[common]
            breaches = breaches.loc[common]
            advantage = sharpe[CANDIDATE] - sharpe[control]
            pvalue = one_sided_sign_pvalue(advantage)
            horizon_pvalues.append(pvalue)
            rows.append(
                {
                    "horizon_minutes": horizon,
                    "control": control,
                    "budget_policy": policies[0],
                    "mean_sharpe_advantage": float(advantage.mean()),
                    "median_sharpe_advantage": float(advantage.median()),
                    "sign_pvalue": pvalue,
                    "candidate_breaches": int(breaches[CANDIDATE].sum()),
                    "origins": int(len(common)),
                    "expected_origins": int(expected_by_horizon.loc[horizon]),
                }
            )
        control_pvalues[control] = max(horizon_pvalues)
    adjusted = holm_adjust(control_pvalues)
    for row in rows:
        row["holm_control_pvalue"] = adjusted[str(row["control"])]
    return pd.DataFrame(rows)


def h6_development_decision(
    comparison: pd.DataFrame,
    config: dict | None = None,
) -> pd.DataFrame:
    """Apply the registered Sharpe, Holm, and common-budget conjunction."""
    settings = dict(H6_CONFIG)
    if config:
        settings.update(config)
    _require_h6_protocol(settings)
    required = {
        "control",
        "mean_sharpe_advantage",
        "holm_control_pvalue",
        "candidate_breaches",
        "origins",
        "expected_origins",
        "budget_policy",
    }
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H6 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H6 decision has no sizer evidence")
    if comparison["budget_policy"].nunique() != 1:
        raise RuntimeError("H6 decision requires one ES budget")
    if (comparison["origins"] != comparison["expected_origins"]).any():
        return pd.DataFrame(
            [{
                "null_decision": "defer",
                "benchmark": "volatility_targeted",
                "reason": "H6 evidence is incomplete because one or more registered origins failed",
            }]
        )
    numeric = ["mean_sharpe_advantage", "holm_control_pvalue", "candidate_breaches"]
    if not comparison[numeric].map(math.isfinite).all().all():
        raise ValueError("H6 Sharpe effects, p-values, and breaches must be finite")
    passes = (
        bool((comparison["mean_sharpe_advantage"] > 0.0).all())
        and bool((comparison["candidate_breaches"] == 0).all())
        and bool(
            (comparison["holm_control_pvalue"] <= float(settings["family_alpha"])).all()
        )
        and set(comparison["control"]) == set(CONTROLS)
    )
    if passes:
        return pd.DataFrame(
            [{
                "null_decision": "reject",
                "benchmark": "rorac",
                "reason": (
                    "RORAC beats both controls in after-cost Sharpe at every horizon, "
                    "survives Holm correction, and stays inside the shared ES budget"
                ),
            }]
        )
    return pd.DataFrame(
        [{
            "null_decision": "accept",
            "benchmark": "volatility_targeted",
            "reason": (
                "RORAC does not beat both controls at every horizon under Holm "
                "correction while satisfying the shared ES budget"
            ),
        }]
    )


@dataclass(frozen=True)
class H6Confirmation:
    """One sealed H6 sizing comparison and its registry update."""

    scores: pd.DataFrame
    record: ExperimentRecord
    boundary: pd.Timestamp
    parent_rule: str
    budget_policy: str


def confirm_h6(
    bars: pd.DataFrame,
    record: ExperimentRecord,
    h5_record: ExperimentRecord,
    h4_record: ExperimentRecord,
    *,
    dataset_manifest: DatasetManifest,
    root: Path | str,
    record_path: Path | str,
    config: dict | None = None,
) -> H6Confirmation:
    """Consume the sealed H6 path once, after H5 has retained one budget."""
    version = require_committed(code_version(root))
    _require_open_h6(record)
    stored = load_record(record_path)
    if stored.experiment_id != record.experiment_id:
        raise RuntimeError(
            f"record path belongs to {stored.experiment_id}, not {record.experiment_id}"
        )
    _require_open_h6(stored)
    settings = dict(H6_CONFIG)
    if config:
        settings.update(config)
    _require_h6_protocol(settings)
    budget_policy = _h6_budget_policy(h5_record, settings, require_confirmed=True)
    parent_rule = _h6_parent_rule(h4_record, settings, require_confirmed=True)
    if stored.result != "rorac":
        raise RuntimeError("H6 confirmation needs the development-selected rorac allocator")
    if budget_policy is None:
        raise RuntimeError("H6 confirmation requires sealed-retained H5")
    verify_manifest(dataset_manifest)
    mutable = [
        item.key
        for item in dataset_manifest.objects
        if item.version_id in {None, "", "null"}
    ]
    if mutable:
        raise RuntimeError(
            "H6 confirmation needs immutable version IDs for every dataset object; "
            f"mutable objects={mutable}"
        )
    digest = _sha256_digest(dataset_manifest.digest)
    if stored.dataset_digest and stored.dataset_digest != digest:
        raise RuntimeError(
            f"confirmation dataset {digest} does not match recorded freeze {stored.dataset_digest}"
        )
    configuration = config_digest(settings)
    if not stored.config_digest or stored.config_digest != configuration:
        raise RuntimeError("H6 confirmation configuration does not match the pre-registered digest")
    horizons = tuple(int(item) for item in settings["horizons_minutes"])
    if horizons != tuple(stored.horizons) or horizons != tuple(h4_record.horizons):
        raise ValueError("H6 confirmation horizons must match H6 and H4 records")
    if horizons != tuple(h5_record.horizons):
        raise ValueError("H6 confirmation horizons must match the retained H5 record")
    frame = normalise_ohlc(bars)
    boundary = sealed_boundary(frame.index, months=int(settings["sealed_months"]))
    builds = _h4_builds(frame, settings)
    minute_returns, minute_exclusions = consecutive_simple_returns(frame["close"])
    if minute_exclusions:
        logger.warning("H6 confirmation has %d unavailable one-minute returns", minute_exclusions)
    sizer_scale = volatility_scale(
        frame["close"],
        int(settings["barrier_volatility_lookback_minutes"]),
        float(settings["target_annual_volatility"]),
        max_gross=float(settings["max_gross"]),
    )
    rows: list[dict] = []
    sealed_end = frame.index[-1] + pd.Timedelta(minutes=1)
    for horizon in horizons:
        barrier, terminal = builds[horizon]
        scored, _ = _score_h6_window(
            barrier.labels,
            terminal.returns,
            horizon=horizon,
            parent_rule=parent_rule,
            budget_policy=budget_policy,
            train_start=frame.index.min(),
            train_end=boundary,
            score_start=boundary,
            score_end=sealed_end,
            origin="sealed",
            settings=settings,
            sealed_access=True,
            minute_returns=minute_returns,
            sizer_scale=sizer_scale,
        )
        rows.extend(scored)
    allocation_scores = pd.DataFrame(rows)
    retain = stored.result == "rorac"
    for horizon in horizons:
        part = allocation_scores.loc[allocation_scores["horizon_minutes"] == horizon].set_index("sizer")
        candidate = part.loc["rorac"]
        retain &= int(candidate.constraint_breaches) == 0
        for control in CONTROLS:
            retain &= float(candidate.sharpe) > float(part.loc[control].sharpe)
    decision = "retain" if retain else "reject"
    status = "Research validated" if retain else "Rejected"
    run_id = make_run_id(stored.experiment_id, digest, version, configuration)
    updated = replace(
        stored,
        status=status,
        decision=decision,
        dataset_id=dataset_manifest.dataset_id,
        dataset_digest=digest,
        code_version=version,
        config_digest=configuration,
        sealed_access=True,
        run_id=run_id,
    )
    save_record(record_path, updated)
    logger.warning(
        "H6 confirmatory decision=%s result=%s run=%s boundary=%s",
        decision,
        stored.result,
        run_id,
        boundary.isoformat(),
    )
    return H6Confirmation(
        scores=allocation_scores,
        record=updated,
        boundary=boundary,
        parent_rule=parent_rule,
        budget_policy=budget_policy,
    )


def _h6_parent_rule(
    h4_record: ExperimentRecord,
    settings: dict,
    *,
    require_confirmed: bool,
) -> str:
    if h4_record.experiment_id != "EXP-H04":
        raise ValueError(f"H6 needs EXP-H04 as the decision parent, got {h4_record.experiment_id}")
    if h4_record.dataset_id != settings["dataset_id"]:
        raise ValueError("H6 and H4 dataset identifiers must match")
    if tuple(h4_record.horizons) != tuple(int(item) for item in settings["horizons_minutes"]):
        raise ValueError("H6 and H4 horizons must match")
    if h4_record.result not in set(settings["parent_rules"]):
        raise RuntimeError(f"H6 cannot reconstruct H4 parent rule {h4_record.result!r}")
    if require_confirmed and not (
        h4_record.status == "Research validated"
        and h4_record.decision == "retain"
        and h4_record.sealed_access
    ):
        raise RuntimeError("H6 confirmation requires sealed-retained H4")
    return h4_record.result


def _h6_budget_policy(
    h5_record: ExperimentRecord,
    settings: dict,
    *,
    require_confirmed: bool,
) -> str | None:
    if h5_record.experiment_id != "EXP-H05":
        raise ValueError(f"H6 needs EXP-H05 as its budget parent, got {h5_record.experiment_id}")
    if h5_record.dataset_id != settings["dataset_id"]:
        raise ValueError("H6 and H5 dataset identifiers must match")
    if tuple(h5_record.horizons) != tuple(int(item) for item in settings["horizons_minutes"]):
        raise ValueError("H6 and H5 horizons must match")
    if h5_record.result == "unconstrained_h4":
        if require_confirmed:
            raise RuntimeError("H6 confirmation cannot use unconstrained_h4 as the H5 budget")
        return None
    retained = (
        h5_record.status == "Research validated"
        and h5_record.decision == "retain"
        and h5_record.sealed_access
        and h5_record.result in POLICIES
    )
    if not retained:
        if require_confirmed:
            raise RuntimeError("H6 confirmation requires sealed-retained H5")
        return None
    return str(h5_record.result)


def _h6_defer_reason(h5_record: ExperimentRecord) -> str:
    if h5_record.result == "unconstrained_h4":
        return "H5 retained no ES budget; H6 cannot score sizing against unconstrained_h4"
    if h5_record.result not in POLICIES:
        return "H5 has no registered budget policy for H6 to hold fixed"
    return "H5 has not been sealed and retained, so H6 defers before scoring"


def _require_open_h6(record: ExperimentRecord) -> None:
    if record.experiment_id != "EXP-H06":
        raise ValueError(f"H6 confirmation needs EXP-H06, got {record.experiment_id}")
    if record.parent_hypothesis != "EXP-H05":
        raise ValueError(f"H6 confirmation needs parent EXP-H05, got {record.parent_hypothesis!r}")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"H6 sealed test has already been used: "
            f"decision={record.decision!r} sealed_access={record.sealed_access}"
        )


def _require_h6_protocol(settings: dict) -> None:
    if settings.get("experiment_id") != "EXP-H06":
        raise ValueError("H6 protocol must use EXP-H06")
    if settings.get("parent_experiment_id") != "EXP-H05":
        raise ValueError("H6 protocol must use EXP-H05 as its parent")
    if "budget_policies" in settings or "budget_policy" in settings:
        raise ValueError("H6 cannot search H5 budget policies")
    if tuple(settings.get("sizers", ())) != SIZERS or tuple(H6_CONFIG["sizers"]) != SIZERS:
        raise ValueError("H6 sizer ladder is not the registered ladder")
    if tuple(settings.get("controls", ())) != CONTROLS:
        raise ValueError("H6 controls are not the registered controls")
    if settings.get("candidate_sizer") != CANDIDATE or settings.get("default_sizer") != DEFAULT_SIZER:
        raise ValueError("H6 candidate and default sizers are not registered")
    if settings.get("sizer") != "volatility_targeted":
        raise ValueError("H6 must keep volatility-targeted sizing as the default")
    if settings.get("economic_metric") != "sharpe":
        raise ValueError("H6 economic metric must be after-cost Sharpe")
    if settings.get("multiple_testing") != "holm_sizer_family_all_horizons_conjunction":
        raise ValueError("H6 must use the registered Holm all-horizons rule")
    if settings.get("opportunity") != "direction_specific_expected_barrier_payoff":
        raise ValueError("H6 opportunity definition is not the registered payoff")
    if settings.get("risk_capital") != "point_in_time_unit_expected_shortfall":
        raise ValueError("H6 risk capital is not the registered unit ES")
    if settings.get("rorac_normalisation") != "training_p90":
        raise ValueError("H6 must normalise RORAC with the training p90")
    if float(settings.get("rorac_quantile", 0.0)) != float(H6_CONFIG["rorac_quantile"]):
        raise ValueError("H6 RORAC quantile is not the registered quantile")
    if float(settings.get("denominator_floor", 0.0)) != float(H6_CONFIG["denominator_floor"]):
        raise ValueError("H6 denominator floor is not the registered floor")
    if float(settings.get("family_alpha", 0.0)) != float(H6_CONFIG["family_alpha"]):
        raise ValueError("H6 family alpha is not the registered alpha")
    horizons = tuple(int(item) for item in settings.get("horizons_minutes", ()))
    if not horizons or len(set(horizons)) != len(horizons) or min(horizons) < 1:
        raise ValueError("H6 horizons must be unique positive minutes")
    if tuple(settings.get("parent_rules", ())) != tuple(H5_CONFIG["parent_rules"]):
        raise ValueError("H6 must hold the H4 parent-rule set fixed")
    if settings.get("barrier") != H4_CONFIG["barrier"] or settings.get("intrabar_rule") != "stop_first":
        raise ValueError("H6 must hold the H4 barrier and stop_first rule fixed")
    inherited = (
        "es_confidence",
        "budget_fraction",
        "volatility_ratio_floor",
        "volatility_ratio_cap",
        "stress_trigger_ratio",
        "stress_budget_multiplier",
        "max_gross",
        "cost_bps",
        "target_annual_volatility",
        "barrier_sigma",
        "probability_smoothing",
    )
    for key in inherited:
        source = H5_CONFIG if key in H5_CONFIG else H4_CONFIG
        if float(settings.get(key, 0.0)) != float(source[key]):
            raise ValueError(f"H6 must inherit {key}")
    if float(settings.get("round_trip_cost_multiplier", 0.0)) != float(
        H4_CONFIG["round_trip_cost_multiplier"]
    ):
        raise ValueError("H6 must inherit the H4 round-trip cost multiplier")
    if int(settings.get("volatility_lookback_minutes", 0)) < 2:
        raise ValueError("H6 volatility lookback must cover at least two returns")


@dataclass(frozen=True)
class H7Evaluation:
    """Development scores for the trade-flow information test."""

    scores: pd.DataFrame
    comparison: pd.DataFrame
    quality: RippleQuality
    snapshot_features: pd.DataFrame
    snapshot_matched: int
    snapshot_identified: bool
    failures: tuple[dict, ...]
    boundary: pd.Timestamp | None
    deferred: bool
    defer_reason: str
    sealed_access: bool = False
    skipped_origins: tuple[str, ...] = ()


def evaluate_h7(
    bars: pd.DataFrame,
    trades: pd.DataFrame,
    config: dict | None = None,
    *,
    snapshots: pd.DataFrame | None = None,
    depth_update_objects: int = 0,
    depth_update_bytes: int = 0,
    unreadable_objects: int = 0,
) -> H7Evaluation:
    """Score trade flow against the frozen slow state, or defer before fitting."""
    settings = dict(H7_CONFIG)
    if config:
        settings.update(config)
    require_h7_protocol(settings)
    sample = build_ripple_sample(
        trades,
        bars,
        settings,
        snapshots=snapshots,
        depth_update_objects=depth_update_objects,
        depth_update_bytes=depth_update_bytes,
        unreadable_objects=unreadable_objects,
    )
    empty_scores = pd.DataFrame()
    if sample.quality.rows_kept == 0 or sample.quality.coverage_days < int(settings["minimum_coverage_days"]):
        reason = (
            f"trade coverage is {sample.quality.coverage_days} calendar days; "
            f"H7 scores only at {int(settings['minimum_coverage_days'])} or more"
        )
        logger.warning("H7 deferred: %s", reason)
        return _h7_deferred(sample, reason)
    if sample.quality.decisions == 0 or sample.features.empty:
        reason = "the closed bars and the trade tape share no decisions"
        logger.warning("H7 deferred: %s", reason)
        return _h7_deferred(sample, reason)
    try:
        origins, boundary = prospective_origins(
            sample.features.index,
            train_days=int(settings["train_days"]),
            validation_days=int(settings["validation_days"]),
            step_days=int(settings["step_days"]),
            sealed_days=int(settings["sealed_days"]),
        )
    except ValueError as exc:
        reason = str(exc)
        logger.warning("H7 deferred: %s", reason)
        return _h7_deferred(sample, reason)
    minimum_rows = int(settings["minimum_window_rows"])
    eligible: list[Origin] = []
    skipped: list[str] = []
    for origin in origins:
        if _h7_origin_is_scoreable(sample.labels, origin, minimum_rows):
            eligible.append(origin)
        else:
            skipped.append(origin.name)
    if skipped:
        logger.warning(
            "H7 left %d sparse origins unscored; a train or validation window has fewer than %d labels: %s",
            len(skipped),
            minimum_rows,
            ", ".join(skipped),
        )
    if not eligible:
        reason = (
            "no origin has a 14-day train block and a later 7-day validation block "
            f"with at least {minimum_rows} labels"
        )
        logger.warning("H7 deferred: %s", reason)
        return _h7_deferred(sample, reason)
    origins = tuple(eligible)
    rows: list[dict] = []
    failures: list[dict] = []
    expected = len(origins)
    for horizon, labels in sample.labels.items():
        for origin in origins:
            try:
                scored = _score_h7_origin(
                    sample.features,
                    labels,
                    horizon=int(horizon),
                    origin=origin,
                    settings=settings,
                    expected_origins=expected,
                )
            except (ValueError, RuntimeError, FloatingPointError, ArithmeticError) as exc:
                failure = {"horizon_minutes": int(horizon), "origin": origin.name, "error": str(exc)}
                failures.append(failure)
                logger.warning(
                    "H7 development origin failed horizon=%d origin=%s: %s",
                    int(horizon),
                    origin.name,
                    exc,
                )
                continue
            rows.extend(scored)
    scores = pd.DataFrame(rows)
    comparison = h7_comparison(scores) if not scores.empty else empty_scores
    return H7Evaluation(
        scores=scores,
        comparison=comparison,
        quality=sample.quality,
        snapshot_features=sample.snapshot_features,
        snapshot_matched=sample.snapshot_matched,
        snapshot_identified=sample.snapshot_identified,
        failures=tuple(failures),
        boundary=boundary,
        deferred=False,
        defer_reason="",
        sealed_access=False,
        skipped_origins=tuple(skipped),
    )


def h7_comparison(scores: pd.DataFrame) -> pd.DataFrame:
    """Candidate advantage by horizon. Positive means trade flow scored better."""
    required = {
        "horizon_minutes",
        "origin",
        "model",
        "endpoint",
        "score",
        "expected_origins",
        "sealed_access",
    }
    missing = required - set(scores.columns)
    if missing:
        raise ValueError(f"H7 scores are missing columns: {sorted(missing)}")
    if scores.empty:
        raise ValueError("H7 comparison has no scores")
    if bool(scores["sealed_access"].fillna(False).any()):
        raise RuntimeError("H7 comparison cannot include sealed observations")
    expected_values = scores["expected_origins"].dropna().unique()
    if len(expected_values) != 1 or int(expected_values[0]) < 1:
        raise RuntimeError("H7 comparison needs one positive expected-origin count")
    expected = int(expected_values[0])
    rows = []
    for endpoint, better_is_larger in (("log_score", True), ("brier", False)):
        part = scores.loc[scores["endpoint"] == endpoint]
        if part.empty:
            raise RuntimeError(f"H7 comparison is missing endpoint {endpoint}")
        pivot = part.pivot_table(
            index=["horizon_minutes", "origin"],
            columns="model",
            values="score",
            aggfunc="first",
        )
        needed = {"wave_tide", "trade_flow"}
        if not needed.issubset(pivot.columns):
            raise RuntimeError(f"H7 {endpoint} comparison is missing a model")
        advantage = pivot["trade_flow"] - pivot["wave_tide"]
        if not better_is_larger:
            advantage = -advantage
        summary = advantage.groupby("horizon_minutes").agg(["mean", "count"])
        for horizon, values in summary.iterrows():
            rows.append(
                {
                    "horizon_minutes": int(horizon),
                    "endpoint": endpoint,
                    "mean_advantage": float(values["mean"]),
                    "origins": int(values["count"]),
                    "expected_origins": expected,
                }
            )
    return pd.DataFrame(rows)


def h7_development_decision(evaluation: H7Evaluation) -> pd.DataFrame:
    """Accept the null unless both endpoints improve at every horizon."""
    if evaluation.sealed_access:
        raise RuntimeError("H7 development decision cannot read a sealed evaluation")
    if evaluation.deferred or evaluation.scores.empty or evaluation.comparison.empty:
        reason = evaluation.defer_reason or "H7 produced no development scores"
        if evaluation.failures and not evaluation.defer_reason:
            reason = "H7 evidence is incomplete because one or more registered origins failed"
        return pd.DataFrame(
            [{"null_decision": "defer", "benchmark": "wave_tide", "reason": reason}]
        )
    comparison = evaluation.comparison
    required = {"horizon_minutes", "endpoint", "mean_advantage", "origins", "expected_origins"}
    missing = required - set(comparison.columns)
    if missing:
        raise ValueError(f"H7 comparison is missing columns: {sorted(missing)}")
    horizons = {int(value) for value in comparison["horizon_minutes"]}
    endpoints = set(comparison["endpoint"])
    complete = (
        horizons == {5, 15, 60}
        and endpoints == {"log_score", "brier"}
        and bool((comparison["origins"] == comparison["expected_origins"]).all())
    )
    if not complete or not comparison["mean_advantage"].map(math.isfinite).all():
        return pd.DataFrame(
            [{
                "null_decision": "defer",
                "benchmark": "wave_tide",
                "reason": "H7 evidence is incomplete because one or more registered origins failed",
            }]
        )
    if bool((comparison["mean_advantage"] > 0.0).all()):
        return pd.DataFrame(
            [{
                "null_decision": "reject",
                "benchmark": "trade_flow",
                "reason": (
                    "Trade flow improves log score and Brier score at every horizon "
                    "after the frozen slow state"
                ),
            }]
        )
    return pd.DataFrame(
        [{
            "null_decision": "accept",
            "benchmark": "wave_tide",
            "reason": (
                "Trade flow does not improve both log score and Brier score at every horizon"
            ),
        }]
    )


def confirm_h7(*_args, config: dict | None = None, **_kwargs) -> None:
    """Refuse confirmation while the archive has one clock and no book sequence ids."""
    settings = dict(H7_CONFIG)
    if config:
        settings.update(config)
    require_h7_protocol(settings)
    raise RuntimeError(
        "H7 confirmation refused: the stored archive has an exchange timestamp only "
        "and no depth sequence ids"
    )


def h7_confirmation_blockers(*, root: Path | str, record: ExperimentRecord) -> list[str]:
    """Reasons the sealed H7 test cannot be retained on this archive."""
    blockers = []
    version = code_version(root)
    if version.endswith("+dirty") or not version:
        blockers.append(f"the research code is uncommitted ({version})")
    if record.experiment_id != "EXP-H07":
        blockers.append(f"record is {record.experiment_id}, not EXP-H07")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        blockers.append("EXP-H07 has already used a confirmatory decision")
    blockers.append("the stored trade tape has an exchange timestamp and no receipt timestamp")
    blockers.append("depth sequence ids are absent, so books are not reconstructed")
    return blockers


def _h7_deferred(sample, reason: str) -> H7Evaluation:
    return H7Evaluation(
        scores=pd.DataFrame(),
        comparison=pd.DataFrame(),
        quality=sample.quality,
        snapshot_features=sample.snapshot_features,
        snapshot_matched=sample.snapshot_matched,
        snapshot_identified=sample.snapshot_identified,
        failures=(),
        boundary=None,
        deferred=True,
        defer_reason=reason,
        sealed_access=False,
    )


def _h7_origin_is_scoreable(
    labels: dict[int, pd.DataFrame],
    origin: Origin,
    minimum_rows: int,
) -> bool:
    """A sparse calendar step is not a registered origin."""
    if minimum_rows < 2:
        raise ValueError("H7 minimum window must cover at least two rows")
    for horizon, frame in labels.items():
        train = _h7_label_window(frame, int(horizon), origin.train_start, origin.train_end)
        scored = _h7_label_window(frame, int(horizon), origin.train_end, origin.validation_end)
        if len(train) < minimum_rows or len(scored) < minimum_rows:
            return False
    return True


def _score_h7_origin(
    features: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    horizon: int,
    origin: Origin,
    settings: dict,
    expected_origins: int,
) -> list[dict]:
    train = _h7_label_window(labels, horizon, origin.train_start, origin.train_end)
    scored = _h7_label_window(labels, horizon, origin.train_end, origin.validation_end)
    if len(train) < 30 or len(scored) < 30:
        raise RuntimeError(f"H7 {horizon}-minute {origin.name} window is too small")
    nested_columns = list(settings["z_features"])
    candidate_columns = list(settings["z_features"]) + list(settings["o_features"])
    rows = []
    for name, columns in (("wave_tide", nested_columns), ("trade_flow", candidate_columns)):
        density = fit_conditional_density(
            features.loc[train.index, columns],
            train["log_return"],
            family=str(settings["residual_family"]),
            name=f"{name}_{horizon}m",
        )
        density_score = score_conditional_density(
            density,
            features.loc[scored.index, columns],
            scored["log_return"],
            alpha=float(settings["var_alpha"]),
        )
        bernoulli = fit_bernoulli(
            features.loc[train.index, columns],
            train["target_before_stop"],
            name=f"{name}_{horizon}m",
            l2=float(settings["logistic_l2"]),
            iterations=int(settings["logistic_iterations"]),
            clip=float(settings["probability_clip"]),
        )
        brier = brier_score(
            bernoulli.probability(features.loc[scored.index, columns]),
            scored["target_before_stop"],
        )
        rows.append(
            _h7_score_row(
                horizon=horizon,
                origin=origin.name,
                model=name,
                endpoint="log_score",
                score=density_score.mean_log_score,
                observations=density_score.observations,
                expected_origins=expected_origins,
            )
        )
        rows.append(
            _h7_score_row(
                horizon=horizon,
                origin=origin.name,
                model=name,
                endpoint="brier",
                score=brier,
                observations=len(scored),
                expected_origins=expected_origins,
            )
        )
    return rows


def _h7_label_window(
    labels: pd.DataFrame,
    horizon: int,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    index = as_utc_index(labels.index)
    start = pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp(end).tz_convert("UTC")
    if start >= end:
        raise ValueError(f"H7 window start {start} is not before end {end}")
    label_time = index + pd.Timedelta(minutes=horizon)
    chosen = (index >= start) & (label_time < end)
    return labels.loc[chosen]


def _h7_score_row(
    *,
    horizon: int,
    origin: str,
    model: str,
    endpoint: str,
    score: float,
    observations: int,
    expected_origins: int,
) -> dict:
    if not math.isfinite(score):
        raise RuntimeError(f"H7 {model} {endpoint} score is not finite")
    return {
        "horizon_minutes": horizon,
        "origin": origin,
        "model": model,
        "endpoint": endpoint,
        "score": float(score),
        "observations": int(observations),
        "expected_origins": int(expected_origins),
        "sealed_access": False,
    }
