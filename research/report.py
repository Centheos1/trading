"""Shared reading, plotting, and registration for the research notebooks.

These functions read scores that an evaluator already produced. They do not
fit a forecast model and they do not read the sealed test.
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.registry import (
    ExperimentRecord,
    code_version,
    config_digest,
    load_record,
    save_record,
)

logger = logging.getLogger(__name__)

_PALETTE = ("#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2", "#b279a2")
_MUTABLE_VERSIONS = {None, "", "null"}


def model_colors(models: Sequence[str]) -> dict[str, str]:
    """Assign a fixed colour to each model in display order."""
    if len(models) > len(_PALETTE):
        raise ValueError(f"at most {len(_PALETTE)} models can be coloured, got {len(models)}")
    return {model: _PALETTE[position] for position, model in enumerate(models)}


def _require_development(scores: pd.DataFrame, label: str) -> None:
    if scores.empty:
        raise ValueError(f"{label} has no scores")
    if "sealed_access" in scores.columns and bool(scores["sealed_access"].any()):
        raise RuntimeError(f"{label} contains sealed observations")


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{label} is missing columns: {sorted(missing)}")


def _score_levels(scores: pd.DataFrame, models: Sequence[str]) -> pd.DataFrame:
    levels = scores.pivot_table(
        index=["horizon_minutes", "origin"],
        columns="model",
        values="mean_log_score",
        aggfunc="first",
    )
    absent = sorted(set(models) - set(levels.columns))
    if absent:
        raise RuntimeError(f"scores have no rows for {absent}")
    if levels[list(models)].isna().any().any():
        raise RuntimeError("a model is missing a score at one or more origins")
    return levels


def quality_table(reports: Mapping[str, object]) -> pd.DataFrame:
    """One row per series: rows read, rows kept, and every counted exclusion."""
    rows = []
    for symbol, report in reports.items():
        exclusions = report.exclusions
        rows.append(
            {
                "series": symbol,
                "rows_in": int(report.rows_in),
                "rows_kept": int(report.rows_out),
                "duplicate_timestamps": int(exclusions.duplicate_timestamps),
                "off_grid_timestamps": int(exclusions.off_grid_timestamps),
                "nonpositive_prices": int(exclusions.nonpositive_prices),
                "inconsistent_ohlc": int(exclusions.inconsistent_ohlc),
                "missing_minutes": int(exclusions.missing_minutes),
                "largest_gap_minutes": int(report.largest_gap_minutes),
            }
        )
    if not rows:
        raise ValueError("quality table needs at least one report")
    return pd.DataFrame(rows).set_index("series")


def score_reading(
    scores: pd.DataFrame,
    *,
    benchmark: str,
    candidates: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Each candidate's log-score advantage over the benchmark, per horizon and origin.

    ``density_change_pct`` is ``100 * (exp(mean advantage) - 1)``: the change
    in geometric-mean forecast density. It is not a trading return.
    """
    _require_development(scores, "score reading")
    _require_columns(scores, {"horizon_minutes", "origin", "model", "mean_log_score"}, "scores")
    if not candidates:
        raise ValueError("score reading needs at least one candidate")
    levels = _score_levels(scores, (benchmark, *candidates))
    rows = []
    origin_rows = []
    for candidate in candidates:
        advantage = levels[candidate] - levels[benchmark]
        for horizon, values in advantage.groupby(level="horizon_minutes"):
            mean = float(values.mean())
            rows.append(
                {
                    "horizon_minutes": int(horizon),
                    "candidate": candidate,
                    "benchmark": benchmark,
                    "mean_advantage": mean,
                    "median_advantage": float(values.median()),
                    "min_advantage": float(values.min()),
                    "max_advantage": float(values.max()),
                    "origins_ahead": int((values > 0.0).sum()),
                    "origins": int(values.size),
                    "density_change_pct": 100.0 * math.expm1(mean),
                    "benchmark_score": float(levels.loc[horizon, benchmark].mean()),
                    "candidate_score": float(levels.loc[horizon, candidate].mean()),
                }
            )
        for (horizon, origin), value in advantage.items():
            origin_rows.append(
                {
                    "origin": origin,
                    "candidate": candidate,
                    "horizon_minutes": int(horizon),
                    "advantage": float(value),
                }
            )
    per_origin = (
        pd.DataFrame(origin_rows)
        .pivot_table(
            index="origin",
            columns=["candidate", "horizon_minutes"],
            values="advantage",
            aggfunc="first",
        )
        .sort_index()
    )
    return pd.DataFrame(rows), per_origin


def calibration_summary(
    scores: pd.DataFrame,
    coverage: pd.DataFrame,
    *,
    alpha: float,
) -> pd.DataFrame:
    """Median tail diagnostics per horizon and model, with the adjusted VaR p-value."""
    _require_development(scores, "calibration summary")
    _require_columns(
        scores,
        {"horizon_minutes", "model", "var_exceedance_rate", "pit_ks_pvalue", "expected_shortfall"},
        "scores",
    )
    _require_columns(coverage, {"horizon_minutes", "model", "bonferroni_var_pvalue"}, "coverage")
    columns = ["var_exceedance_rate", "pit_ks_pvalue", "expected_shortfall"]
    if "realised_shortfall" in scores.columns:
        columns.append("realised_shortfall")
    summary = (
        scores.groupby(["horizon_minutes", "model"])[columns]
        .median()
        .rename(columns={"var_exceedance_rate": "median_var_hit_rate"})
        .reset_index()
    )
    merged = summary.merge(
        coverage[["horizon_minutes", "model", "bonferroni_var_pvalue"]],
        on=["horizon_minutes", "model"],
        how="left",
    )
    if merged["bonferroni_var_pvalue"].isna().any():
        raise RuntimeError("calibration summary could not match every model to its VaR p-value")
    merged["var_target"] = float(alpha)
    merged["coverage_rejected"] = merged["bonferroni_var_pvalue"] < 0.05
    return merged


def economic_summary(scores: pd.DataFrame) -> pd.DataFrame:
    """Median H4 after-cost outcomes by horizon and frozen decision rule."""
    _require_development(scores, "economic summary")
    columns = {
        "horizon_minutes",
        "model",
        "total_simple_return",
        "sharpe",
        "max_drawdown",
        "average_turnover",
        "average_absolute_position",
        "no_trade_rate",
    }
    _require_columns(scores, columns, "economic scores")
    metrics = sorted(columns - {"horizon_minutes", "model"})
    return (
        scores.groupby(["horizon_minutes", "model"])[metrics]
        .median()
        .reset_index()
        .sort_values(["horizon_minutes", "model"])
    )


def probability_summary(scores: pd.DataFrame) -> pd.DataFrame:
    """Median H4 probability calibration by horizon, object, and event."""
    _require_development(scores, "probability summary")
    _require_columns(
        scores,
        {
            "horizon_minutes",
            "model",
            "event",
            "probability",
            "event_rate",
            "brier_score",
            "log_loss",
        },
        "probability scores",
    )
    metrics = ["probability", "event_rate", "brier_score", "log_loss"]
    return (
        scores.groupby(["horizon_minutes", "model", "event"])[metrics]
        .median()
        .reset_index()
        .sort_values(["horizon_minutes", "model", "event"])
    )


def tide_risk_summary(scores: pd.DataFrame) -> pd.DataFrame:
    """Median H5 economics, tail risk, and constraint activity by policy."""
    _require_development(scores, "Tide risk summary")
    columns = {
        "horizon_minutes",
        "policy",
        "mean_after_cost_return",
        "total_simple_return",
        "sharpe",
        "max_drawdown",
        "realised_es",
        "average_turnover",
        "average_absolute_position",
        "es_binds",
        "margin_binds",
        "unavailable_risk_estimates",
        "liquidation_events",
        "median_budget_utilisation",
    }
    _require_columns(scores, columns, "H5 risk scores")
    metrics = sorted(columns - {"horizon_minutes", "policy"})
    return (
        scores.groupby(["horizon_minutes", "policy"])[metrics]
        .median()
        .reset_index()
        .sort_values(["horizon_minutes", "policy"])
    )


def _label(labels: Mapping[str, str] | None, model: str) -> str:
    return (labels or {}).get(model, model)


def plain_english_result(
    *,
    question: str,
    verdict: Mapping[str, object],
    reading: pd.DataFrame,
    calibration: pd.DataFrame,
    failures: int,
    labels: Mapping[str, str] | None = None,
) -> list[str]:
    """State the development result as sentences a reader can act on."""
    decision = str(verdict["null_decision"])
    selected = str(verdict["benchmark"])
    reason = str(verdict["reason"])
    if decision not in {"accept", "reject", "defer"}:
        raise ValueError(f"unsupported null decision {decision!r}")
    if reading.empty or calibration.empty:
        raise ValueError("plain-English result needs a reading and a calibration summary")
    benchmark = str(reading["benchmark"].iloc[0])
    candidates = list(dict.fromkeys(reading["candidate"]))
    lines = [f"Question: {question}"]
    if decision == "reject":
        lines.append(
            f"Answer: yes, on development data. The null is rejected and the working forecast is "
            f"{_label(labels, selected)}."
        )
    elif decision == "accept":
        lines.append(
            f"Answer: no. The null is accepted and the working forecast is {_label(labels, selected)}."
        )
    else:
        lines.append("Answer: not yet. The evidence is incomplete, so the decision is deferred.")
    lines.append(f"Why: {reason}.")
    for candidate in candidates:
        rows = reading.loc[reading["candidate"] == candidate].sort_values("horizon_minutes")
        parts = [
            f"{row.density_change_pct:+.4f}% at {int(row.horizon_minutes)} minutes "
            f"(ahead at {int(row.origins_ahead)} of {int(row.origins)} origins)"
            for row in rows.itertuples(index=False)
        ]
        lines.append(
            f"Against {_label(labels, benchmark)}, {_label(labels, candidate)} changed the "
            f"geometric-mean forecast density by " + "; ".join(parts) + "."
        )
    hit_low = float(calibration["median_var_hit_rate"].min())
    hit_high = float(calibration["median_var_hit_rate"].max())
    target = float(calibration["var_target"].iloc[0])
    if bool(calibration["coverage_rejected"].all()):
        if hit_high < target:
            direction = "Every model breaches its VaR less often than it should, so each overstates tail risk."
        elif hit_low > target:
            direction = "Every model breaches its VaR more often than it should, so each understates tail risk."
        else:
            direction = "Breach rates sit on both sides of the target."
        lines.append(
            f"Tail calibration: no model is calibrated. The {target:.0%} VaR is breached "
            f"{hit_low:.2%} to {hit_high:.2%} of the time, and the Bonferroni test rejects coverage "
            f"for every model and horizon. {direction} None of these densities is ready as a "
            f"tail-risk estimate."
        )
    else:
        calibrated = calibration.loc[~calibration["coverage_rejected"]]
        names = ", ".join(
            f"{_label(labels, row.model)} at {int(row.horizon_minutes)} minutes"
            for row in calibrated.itertuples(index=False)
        )
        lines.append(
            f"Tail calibration: coverage is not rejected for {names}. Breach rates range "
            f"{hit_low:.2%} to {hit_high:.2%} against a {target:.0%} target."
        )
    if failures:
        lines.append(
            f"{failures} walk-forward window(s) failed and are excluded. A failed window is not a "
            f"zero advantage."
        )
    else:
        lines.append("Every registered walk-forward window was scored.")
    candidate_labels = [_label(labels, name) for name in candidates]
    candidate_names = (
        candidate_labels[0]
        if len(candidate_labels) == 1
        else ", ".join(candidate_labels[:-1]) + " or " + candidate_labels[-1]
    )
    if decision == "reject":
        lines.append(
            f"What this means: use {_label(labels, selected)} as the development working forecast. "
            f"It is not retained until the sealed year confirms it."
        )
    elif decision == "accept":
        lines.append(
            f"What this means: do not add {candidate_names} to Wave or Tide on this evidence. "
            f"Keep {_label(labels, selected)} as the working forecast."
        )
    else:
        lines.append("What this means: resolve the failed windows and rerun before reading the result.")
    lines.append("This is development evidence. The sealed final year has not been read.")
    return lines


def record_development_decision(
    record_path: Path | str,
    verdict: Mapping[str, object],
    *,
    experiment_id: str,
    dataset_id: str,
    dataset_digest: str,
    config: dict,
    root: Path | str,
) -> ExperimentRecord:
    """Write the development result without claiming a sealed decision.

    Accept or reject of the development null leaves the lifecycle decision
    blank. Incomplete evidence defers. Only a confirmation function writes
    retain or reject.
    """
    decision = str(verdict["null_decision"])
    current = load_record(record_path)
    if current.experiment_id != experiment_id:
        raise RuntimeError(f"record path belongs to {current.experiment_id}, not {experiment_id}")
    if current.sealed_access or current.decision in {"retain", "reject"}:
        raise RuntimeError(
            f"development notebook cannot overwrite a sealed {experiment_id} decision: "
            f"decision={current.decision!r} sealed_access={current.sealed_access}"
        )
    if decision == "defer":
        status, lifecycle = "Deferred", "defer"
    elif decision in {"accept", "reject"}:
        status, lifecycle = "Exploratory", ""
    else:
        raise RuntimeError(f"unsupported development decision {decision!r}")
    if not dataset_id or not dataset_digest:
        raise ValueError("a development record needs a dataset id and digest")
    updated = replace(
        current,
        status=status,
        result=str(verdict["benchmark"]),
        decision=lifecycle,
        dataset_id=dataset_id,
        dataset_digest=dataset_digest,
        code_version=code_version(root),
        config_digest=config_digest(config),
        sealed_access=False,
        run_id="",
    )
    save_record(record_path, updated)
    logger.info(
        "Recorded %s development result=%s status=%s", experiment_id, updated.result, status
    )
    return updated


def confirmation_blockers(
    *,
    root: Path | str,
    record: ExperimentRecord,
    h1_record: ExperimentRecord | None = None,
    parent_record: ExperimentRecord | None = None,
    dataset_objects: Iterable[object] = (),
) -> list[str]:
    """Reasons the sealed confirmation would refuse to run now."""
    blockers: list[str] = []
    version = code_version(root)
    if version.endswith("+dirty"):
        blockers.append(f"the research code is uncommitted ({version})")
    if record.sealed_access or record.decision in {"retain", "reject"}:
        blockers.append(f"{record.experiment_id} has already used its sealed year")
    if not record.config_digest:
        blockers.append(f"{record.experiment_id} has no pre-registered configuration digest")
    if h1_record is not None and not (
        h1_record.decision in {"retain", "reject"} and h1_record.sealed_access
    ):
        blockers.append("H1 has not been confirmed on its sealed year")
    if parent_record is not None and not (
        parent_record.status == "Research validated"
        and parent_record.decision == "retain"
        and parent_record.sealed_access
    ):
        blockers.append(
            f"{parent_record.experiment_id} has not been sealed and retained"
        )
    mutable = [
        str(getattr(item, "key", item))
        for item in dataset_objects
        if getattr(item, "version_id", None) in _MUTABLE_VERSIONS
    ]
    if mutable:
        blockers.append(
            f"{len(mutable)} stored object(s) have no immutable version id, for example {mutable[0]}"
        )
    return blockers


def plot_tide_es_tradeoff(scores: pd.DataFrame):
    """H5 realised ES against mean after-cost return by policy and horizon."""
    import matplotlib.pyplot as plt

    summary = tide_risk_summary(scores)
    policies = list(dict.fromkeys(summary["policy"]))
    colors = model_colors(policies)
    markers = dict(zip(_horizons(summary), ("o", "s", "^", "D", "v")))
    fig, axis = plt.subplots(figsize=(8, 6))
    for policy in policies:
        part = summary.loc[summary["policy"] == policy]
        for row in part.itertuples(index=False):
            axis.scatter(
                row.realised_es,
                row.mean_after_cost_return,
                marker=markers[int(row.horizon_minutes)],
                color=colors[policy],
                label=f"{policy}, {int(row.horizon_minutes)} min",
            )
    axis.set_xlabel("realised 97.5% expected shortfall")
    axis.set_ylabel("mean after-cost one-minute return")
    axis.set_title("Tide risk/return trade-off")
    axis.legend(fontsize=7)
    fig.tight_layout()
    return fig


def plot_tide_constraint_activity(scores: pd.DataFrame):
    """Median ES binds and unavailable risk estimates by policy."""
    import matplotlib.pyplot as plt

    summary = tide_risk_summary(scores)
    constrained = summary.loc[summary["policy"] != "unconstrained_h4"].copy()
    if constrained.empty:
        raise ValueError("constraint activity plot needs at least one constrained policy")
    constrained["label"] = (
        constrained["policy"]
        + "\n"
        + constrained["horizon_minutes"].astype(int).astype(str)
        + "m"
    )
    fig, axis = plt.subplots(figsize=(11, 4.5))
    positions = np.arange(len(constrained))
    axis.bar(positions, constrained["es_binds"], label="ES binds")
    axis.bar(
        positions,
        constrained["unavailable_risk_estimates"],
        bottom=constrained["es_binds"],
        label="risk unavailable",
    )
    axis.set_xticks(positions)
    axis.set_xticklabels(constrained["label"], rotation=35, ha="right")
    axis.set_ylabel("median decision count per origin")
    axis.set_title("Tide constraint activity")
    axis.legend()
    fig.tight_layout()
    return fig


def allocation_summary(scores: pd.DataFrame) -> pd.DataFrame:
    """Median H6 after-cost outcomes and budget activity by sizer."""
    _require_development(scores, "allocation summary")
    columns = {
        "horizon_minutes",
        "sizer",
        "mean_after_cost_return",
        "total_simple_return",
        "sharpe",
        "max_drawdown",
        "realised_es",
        "average_turnover",
        "average_absolute_position",
        "es_binds",
        "denominator_floors",
        "unavailable_estimates",
        "gross_caps",
        "constraint_breaches",
        "liquidation_events",
        "median_budget_utilisation",
    }
    _require_columns(scores, columns, "H6 allocation scores")
    metrics = sorted(columns - {"horizon_minutes", "sizer"})
    return (
        scores.groupby(["horizon_minutes", "sizer"])[metrics]
        .median()
        .reset_index()
        .sort_values(["horizon_minutes", "sizer"])
    )


def plot_allocation_sharpe(scores: pd.DataFrame):
    """Median after-cost Sharpe for each sizer under the shared ES budget."""
    import matplotlib.pyplot as plt

    summary = allocation_summary(scores)
    sizers = list(dict.fromkeys(summary["sizer"]))
    horizons = _horizons(summary)
    colors = model_colors(sizers)
    width = 0.8 / len(sizers)
    fig, axis = plt.subplots(figsize=(8, 4.5))
    for offset, sizer in enumerate(sizers):
        part = summary.loc[summary["sizer"] == sizer].set_index("horizon_minutes")
        heights = [
            float(part.loc[horizon, "sharpe"]) if horizon in part.index else 0.0
            for horizon in horizons
        ]
        axis.bar(
            np.arange(len(horizons)) + offset * width,
            heights,
            width,
            label=sizer,
            color=colors[sizer],
        )
    axis.set_xticks(np.arange(len(horizons)) + width * (len(sizers) - 1) / 2)
    axis.set_xticklabels([f"{horizon}m" for horizon in horizons])
    axis.set_ylabel("median after-cost Sharpe")
    axis.set_title("Allocation Sharpe under one ES budget")
    axis.legend()
    fig.tight_layout()
    return fig


def plot_allocation_activity(scores: pd.DataFrame):
    """Median denominator floors, unavailable ratios, and ES binds by sizer."""
    import matplotlib.pyplot as plt

    summary = allocation_summary(scores).copy()
    summary["label"] = (
        summary["sizer"] + "\n" + summary["horizon_minutes"].astype(int).astype(str) + "m"
    )
    fig, axis = plt.subplots(figsize=(11, 4.5))
    positions = np.arange(len(summary))
    axis.bar(positions, summary["denominator_floors"], label="denominator floors")
    axis.bar(
        positions,
        summary["unavailable_estimates"],
        bottom=summary["denominator_floors"],
        label="unavailable estimates",
    )
    axis.bar(
        positions,
        summary["es_binds"],
        bottom=summary["denominator_floors"] + summary["unavailable_estimates"],
        label="ES binds",
    )
    axis.set_xticks(positions)
    axis.set_xticklabels(summary["label"], rotation=35, ha="right")
    axis.set_ylabel("median decision count per origin")
    axis.set_title("Allocation activity under one ES budget")
    axis.legend()
    fig.tight_layout()
    return fig


def _horizons(scores: pd.DataFrame) -> list[int]:
    return sorted(int(value) for value in scores["horizon_minutes"].unique())


def plot_advantage_by_origin(
    scores: pd.DataFrame,
    *,
    benchmark: str,
    candidates: Sequence[str],
    labels: Mapping[str, str] | None = None,
):
    """Bars of candidate minus benchmark mean log score at each origin."""
    import matplotlib.pyplot as plt

    _require_development(scores, "advantage plot")
    levels = _score_levels(scores, (benchmark, *candidates))
    horizons = _horizons(scores)
    colors = model_colors(list(candidates))
    fig, axes = plt.subplots(len(horizons), 1, figsize=(11, 3.2 * len(horizons)), squeeze=False)
    width = 0.8 / len(candidates)
    for axis, horizon in zip(axes[:, 0], horizons):
        window = levels.xs(horizon).sort_index()
        positions = np.arange(len(window))
        for offset, candidate in enumerate(candidates):
            axis.bar(
                positions + (offset - (len(candidates) - 1) / 2) * width,
                window[candidate] - window[benchmark],
                width,
                color=colors[candidate],
                label=_label(labels, candidate),
            )
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xticks(positions)
        axis.set_xticklabels(window.index, rotation=30)
        axis.set_ylabel("nats")
        axis.set_title(f"{horizon}-minute log-score advantage over {_label(labels, benchmark)}")
    axes[0, 0].legend()
    fig.tight_layout()
    return fig


def plot_score_levels(
    scores: pd.DataFrame,
    *,
    models: Sequence[str],
    labels: Mapping[str, str] | None = None,
):
    """Mean validation log score for each model at each origin."""
    import matplotlib.pyplot as plt

    _require_development(scores, "score-level plot")
    levels = _score_levels(scores, models)
    horizons = _horizons(scores)
    colors = model_colors(list(models))
    fig, axes = plt.subplots(1, len(horizons), figsize=(4.2 * len(horizons), 4), squeeze=False)
    for axis, horizon in zip(axes[0], horizons):
        window = levels.xs(horizon).sort_index()
        for model in models:
            axis.plot(window.index, window[model], marker="o", color=colors[model], label=_label(labels, model))
        axis.set_title(f"{horizon} min")
        axis.tick_params(axis="x", labelrotation=45)
    axes[0, 0].set_ylabel("mean log score")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle("Validation mean log score by origin")
    fig.tight_layout()
    return fig


def plot_var_hit_rates(
    scores: pd.DataFrame,
    *,
    models: Sequence[str],
    alpha: float,
    labels: Mapping[str, str] | None = None,
):
    """Share of validation returns below each model's VaR, against the target."""
    import matplotlib.pyplot as plt

    _require_development(scores, "VaR plot")
    _require_columns(scores, {"var_exceedance_rate"}, "scores")
    horizons = _horizons(scores)
    colors = model_colors(list(models))
    fig, axes = plt.subplots(1, len(horizons), figsize=(4.2 * len(horizons), 4), sharey=True, squeeze=False)
    for axis, horizon in zip(axes[0], horizons):
        part = scores.loc[scores["horizon_minutes"] == horizon]
        axis.axhline(alpha, color="black", linewidth=0.8, label=f"{alpha:.0%} target")
        for model in models:
            series = part.loc[part["model"] == model].sort_values("origin")
            axis.plot(series["origin"], series["var_exceedance_rate"], marker="o", color=colors[model], label=_label(labels, model))
        axis.set_title(f"{horizon} min")
        axis.tick_params(axis="x", labelrotation=45)
    axes[0, 0].set_ylabel("VaR hit rate")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle("Share of validation returns below the VaR")
    fig.tight_layout()
    return fig


def plot_shortfall(
    scores: pd.DataFrame,
    *,
    models: Sequence[str],
    labels: Mapping[str, str] | None = None,
):
    """Model expected shortfall against the realised mean of VaR breaches."""
    import matplotlib.pyplot as plt

    _require_development(scores, "shortfall plot")
    _require_columns(scores, {"expected_shortfall", "realised_shortfall"}, "scores")
    missing = scores["realised_shortfall"].isna()
    if missing.any():
        logger.warning("%d window(s) had no VaR breach and are absent from the shortfall plot", int(missing.sum()))
    usable = scores.loc[~missing]
    if usable.empty:
        raise RuntimeError("no window has a realised shortfall to plot")
    colors = model_colors(list(models))
    markers = dict(zip(_horizons(scores), ("o", "s", "^", "D", "v")))
    fig, axis = plt.subplots(figsize=(6.5, 6))
    for model in models:
        for horizon, marker in markers.items():
            window = usable.loc[(usable["model"] == model) & (usable["horizon_minutes"] == horizon)]
            axis.scatter(
                window["expected_shortfall"],
                window["realised_shortfall"],
                marker=marker,
                color=colors[model],
                label=f"{_label(labels, model)}, {horizon} min",
            )
    low = min(usable["expected_shortfall"].min(), usable["realised_shortfall"].min())
    high = max(usable["expected_shortfall"].max(), usable["realised_shortfall"].max())
    axis.plot([low, high], [low, high], color="black", linewidth=0.8)
    axis.set_xlabel("model expected shortfall")
    axis.set_ylabel("realised shortfall")
    axis.set_title("Expected shortfall against the realised tail mean\n(below the diagonal: the tail was worse than forecast)")
    axis.legend(fontsize=7)
    fig.tight_layout()
    return fig


def plot_pit_qq(
    pit_qq: pd.DataFrame,
    *,
    models: Sequence[str],
    labels: Mapping[str, str] | None = None,
):
    """PIT quantiles against Uniform(0, 1). Faint lines are origins; heavy lines their mean."""
    import matplotlib.pyplot as plt

    _require_development(pit_qq, "PIT QQ")
    _require_columns(pit_qq, {"horizon_minutes", "origin", "model", "theoretical", "empirical"}, "PIT QQ")
    horizons = _horizons(pit_qq)
    colors = model_colors(list(models))
    fig, axes = plt.subplots(1, len(horizons), figsize=(4.2 * len(horizons), 4), sharex=True, sharey=True, squeeze=False)
    for axis, horizon in zip(axes[0], horizons):
        axis.plot([0, 1], [0, 1], color="black", linewidth=0.8)
        part = pit_qq.loc[pit_qq["horizon_minutes"] == horizon]
        for model in models:
            model_part = part.loc[part["model"] == model]
            if model_part.empty:
                raise RuntimeError(f"no PIT QQ for {model} at {horizon} minutes")
            for _, origin_part in model_part.groupby("origin"):
                curve = origin_part.sort_values("theoretical")
                axis.plot(curve["theoretical"], curve["empirical"], color=colors[model], alpha=0.2, linewidth=0.8)
            mean_curve = model_part.groupby("theoretical", as_index=False)["empirical"].mean()
            axis.plot(mean_curve["theoretical"], mean_curve["empirical"], color=colors[model], linewidth=2, label=_label(labels, model))
        axis.set_title(f"{horizon} min")
        axis.set_xlabel("uniform quantile")
    axes[0, 0].set_ylabel("PIT quantile")
    axes[0, 0].legend(fontsize=8)
    fig.suptitle("PIT QQ against Uniform(0, 1)")
    fig.tight_layout()
    return fig


DensityCurve = tuple[str, Callable[[np.ndarray], np.ndarray], dict]


def plot_density_panels(
    samples: Mapping[int, np.ndarray],
    curves: Mapping[int, Sequence[DensityCurve]],
    *,
    title: str,
    sample_label: str = "development returns",
    xlabel: str = "log return",
):
    """Histogram and fitted densities per horizon: body on top, log-scale tails below.

    A horizon with a sample but no curves is drawn as a failed fit.
    """
    import matplotlib.pyplot as plt

    horizons = sorted(samples)
    if not horizons:
        raise ValueError("density panels need at least one sample")
    fig, axes = plt.subplots(2, len(horizons), figsize=(4.2 * len(horizons), 7), squeeze=False)
    for column, horizon in enumerate(horizons):
        values = np.asarray(samples[horizon], dtype=float)
        if values.size < 30 or not np.isfinite(values).all():
            raise ValueError(f"{horizon}-minute density sample is too small or non-finite")
        body = np.quantile(values, [0.005, 0.995])
        tails = np.quantile(values, [0.00001, 0.99999])
        for row, (window, log_scale) in enumerate(((body, False), (tails, True))):
            axis = axes[row, column]
            bin_width = (float(body[1]) - float(body[0])) / 80
            bins = min(max(int(np.ceil((float(window[1]) - float(window[0])) / bin_width)), 80), 400)
            counts, edges = np.histogram(values, bins=bins, range=(float(window[0]), float(window[1])))
            density = counts / (len(values) * np.diff(edges))
            axis.bar(edges[:-1], np.where(density > 0, density, np.nan), width=np.diff(edges), align="edge", color="0.78", label=sample_label)
            peaks = [float(np.nanmax(density))]
            horizon_curves = list(curves.get(horizon, ()))
            if not horizon_curves:
                axis.text(0.5, 0.5, "fit failed", ha="center", va="center", transform=axis.transAxes)
            grid = np.linspace(float(window[0]), float(window[1]), 500)
            for name, log_density, style in horizon_curves:
                values_on_grid = np.exp(np.asarray(log_density(grid), dtype=float))
                axis.plot(grid, values_on_grid, label=name, **style)
                peaks.append(float(np.nanmax(values_on_grid)))
            axis.set_xlim(window)
            if log_scale:
                axis.set_yscale("log")
                axis.set_ylim(1e-6, max(peaks) * 3)
            if row == 0:
                axis.set_title(f"{horizon} min")
            if column == 0:
                axis.set_ylabel("log density" if log_scale else "density")
            axis.set_xlabel(xlabel)
    axes[0, 0].legend(fontsize=7)
    fig.suptitle(title)
    fig.tight_layout()
    return fig


def conditional_coefficients(fits: Iterable[object]) -> pd.DataFrame:
    """Expected-return shift from a one-standard-deviation move in each feature.

    Coefficients are fitted on standardised features, so each is the change
    in the forecast mean, in log-return units, per one training SD.
    """
    rows = []
    for fit in fits:
        density = fit.density
        for name, coefficient in zip(density.feature_names, density.coefficients[1:]):
            rows.append(
                {
                    "horizon_minutes": int(fit.horizon_minutes),
                    "model": fit.model,
                    "feature": name,
                    "shift_per_sd": float(coefficient),
                    "shift_per_sd_bps": float(coefficient) * 1e4,
                }
            )
    if not rows:
        raise ValueError("no conditional fits were supplied")
    return pd.DataFrame(rows)


def residual_parameters(fits: Iterable[object]) -> pd.DataFrame:
    """Residual family parameters for each fitted conditional density."""
    rows = []
    for fit in fits:
        residual = fit.density.residual
        row = {
            "horizon_minutes": int(fit.horizon_minutes),
            "model": fit.model,
            "observations": int(fit.observations),
            "intercept": float(fit.density.coefficients[0]),
        }
        if hasattr(residual, "df"):
            row.update({"family": "student_t", "df": residual.df, "scale": residual.scale})
        else:
            row.update({"family": "gaussian", "df": math.nan, "scale": math.sqrt(residual.variance)})
        rows.append(row)
    if not rows:
        raise ValueError("no conditional fits were supplied")
    return pd.DataFrame(rows)


def ripple_quality_table(quality) -> pd.DataFrame:
    """One row for the trade tape and one row per horizon exclusion."""
    rows = [
        {
            "item": "trades",
            "rows_in": int(quality.rows_in),
            "rows_kept": int(quality.rows_kept),
            "duplicate_rows": int(quality.duplicate_rows),
            "nonpositive_rows": int(quality.nonpositive_rows),
            "coverage_days": int(quality.coverage_days),
            "decisions": int(quality.decisions),
            "feature_exclusions": int(quality.feature_exclusions),
            "gap_exclusions": 0,
            "endpoint_exclusions": 0,
            "labels": 0,
            "depth_update_objects": int(quality.depth_update_objects),
            "depth_update_bytes": int(quality.depth_update_bytes),
            "unreadable_objects": int(quality.unreadable_objects),
            "clocks": ",".join(quality.clocks),
            "depth_sequence_ids": bool(quality.depth_sequence_ids),
        }
    ]
    for horizon in quality.horizon_exclusions:
        rows.append(
            {
                "item": f"{horizon.horizon_minutes}m",
                "rows_in": int(quality.rows_in),
                "rows_kept": int(quality.rows_kept),
                "duplicate_rows": int(quality.duplicate_rows),
                "nonpositive_rows": int(quality.nonpositive_rows),
                "coverage_days": int(quality.coverage_days),
                "decisions": int(quality.decisions),
                "feature_exclusions": int(quality.feature_exclusions),
                "gap_exclusions": int(horizon.gap_exclusions),
                "endpoint_exclusions": int(horizon.endpoint_exclusions),
                "labels": int(horizon.labels),
                "depth_update_objects": int(quality.depth_update_objects),
                "depth_update_bytes": int(quality.depth_update_bytes),
                "unreadable_objects": int(quality.unreadable_objects),
                "clocks": ",".join(quality.clocks),
                "depth_sequence_ids": bool(quality.depth_sequence_ids),
            }
        )
    return pd.DataFrame(rows).set_index("item")


def ripple_comparison_table(comparison: pd.DataFrame) -> pd.DataFrame:
    """Development advantages. A positive value means trade flow scored better."""
    columns = ["horizon_minutes", "endpoint", "mean_advantage", "origins", "expected_origins"]
    missing = set(columns) - set(comparison.columns)
    if missing:
        raise ValueError(f"H7 comparison is missing columns: {sorted(missing)}")
    if comparison.empty:
        raise ValueError("H7 comparison table has no rows")
    if "sealed_access" in comparison.columns and bool(comparison["sealed_access"].any()):
        raise RuntimeError("H7 comparison table contains sealed observations")
    return comparison.loc[:, columns].sort_values(["endpoint", "horizon_minutes"]).reset_index(drop=True)


def plot_ripple_advantage(comparison: pd.DataFrame):
    """Bar chart of the trade-flow advantage at each horizon and endpoint."""
    import matplotlib.pyplot as plt

    table = ripple_comparison_table(comparison)
    endpoints = list(dict.fromkeys(table["endpoint"]))
    fig, axes = plt.subplots(1, len(endpoints), figsize=(5 * len(endpoints), 4), sharey=False)
    if len(endpoints) == 1:
        axes = [axes]
    for axis, endpoint in zip(axes, endpoints):
        part = table.loc[table["endpoint"] == endpoint]
        axis.bar(part["horizon_minutes"].astype(str), part["mean_advantage"], color="#4c78a8")
        axis.axhline(0.0, color="black", linewidth=0.8)
        axis.set_xlabel("horizon (minutes)")
        axis.set_title(endpoint)
    axes[0].set_ylabel("trade-flow advantage")
    fig.tight_layout()
    return fig


def plot_coefficients(
    coefficients: pd.DataFrame,
    *,
    model: str,
    labels: Mapping[str, str] | None = None,
):
    """Bar chart of one model's expected-return shift per feature SD, by horizon."""
    import matplotlib.pyplot as plt

    _require_columns(coefficients, {"horizon_minutes", "model", "feature", "shift_per_sd_bps"}, "coefficients")
    part = coefficients.loc[coefficients["model"] == model]
    if part.empty:
        raise ValueError(f"no coefficients for {model}")
    table = part.pivot_table(index="feature", columns="horizon_minutes", values="shift_per_sd_bps", aggfunc="first")
    table = table.reindex(list(dict.fromkeys(part["feature"])))
    fig, axis = plt.subplots(figsize=(10, 0.5 * len(table) + 2.5))
    table.plot.barh(ax=axis)
    axis.axvline(0.0, color="black", linewidth=0.8)
    axis.set_xlabel("forecast mean shift per one-SD feature move (basis points)")
    axis.set_title(f"{_label(labels, model)} coefficients, fitted on all development labels")
    axis.legend(title="horizon (min)")
    fig.tight_layout()
    return fig
