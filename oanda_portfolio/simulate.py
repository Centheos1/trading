"""Walk-forward the frontier on daily panels. This module does not place orders."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from oanda_portfolio.daily import simple_returns
from oanda_portfolio.frontier import (
    FrontierConfig,
    annualization_factor,
    annualize_moments,
    frontier_portfolio,
    one_factor_covariance,
)
from oanda_portfolio.plan import MARGIN_TARGET, RebalancePlan, build_plan, margin_notional_weights
from oanda_portfolio.select import SelectionError, select_portfolio

logger = logging.getLogger(__name__)


@dataclass
class DayDecision:
    session_date: str
    portfolio_size: int
    net_return: float | None
    weights: dict[str, float]
    skipped: bool
    reason: str
    symbols: tuple[str, ...]
    target_used: float | None
    note: str = ""


@dataclass
class SimulationResult:
    config: FrontierConfig
    paths: dict[int, list[DayDecision]]
    chosen_size: dict[str, int]
    chosen_returns: pd.Series
    plans: list[RebalancePlan]
    skips: list[dict] = field(default_factory=list)
    exclusions: list[dict] = field(default_factory=list)
    final_symbols: tuple[str, ...] = ()
    final_weights: pd.Series | None = None
    final_mix: pd.Series | None = None
    covariance_comparison: dict | None = None
    sharpe_by_size: dict[int, float] = field(default_factory=dict)


def choose_size(scores: dict[int, float]) -> int | None:
    """Pick the highest Sharpe. A tie returns the smaller portfolio."""
    finite = {size: score for size, score in scores.items() if np.isfinite(score)}
    if not finite:
        return None
    best = max(finite.values())
    return min(size for size, score in finite.items() if np.isclose(score, best, atol=1e-12))


def _month_key(day: pd.Timestamp) -> tuple[int, int]:
    return int(day.year), int(day.month)


def _sharpe(values: list[float], index: pd.DatetimeIndex) -> float:
    series = pd.Series(values, index=index, dtype=float).dropna()
    if len(series) < 2:
        return float("nan")
    deviation = float(series.std(ddof=1))
    if deviation == 0.0 or not np.isfinite(deviation):
        return float("nan")
    factor = annualization_factor(pd.DatetimeIndex(series.index))
    return float(series.mean() / deviation * np.sqrt(factor))


def _mark_to_market(
    previous: dict[str, float],
    today_return: pd.Series,
    new_weights: dict[str, float],
    spreads: pd.Series,
    mids: pd.Series,
) -> float | None:
    if not previous:
        return None
    names = [name for name, weight in previous.items() if weight != 0.0]
    if any(name not in today_return.index or not np.isfinite(today_return[name]) for name in names):
        return None
    gross = float(sum(previous[name] * float(today_return[name]) for name in names))
    cost = 0.0
    symbols = set(previous) | set(new_weights)
    for name in symbols:
        delta = abs(float(new_weights.get(name, 0.0)) - float(previous.get(name, 0.0)))
        if delta == 0.0:
            continue
        mid = float(mids.get(name, np.nan))
        spread = float(spreads.get(name, np.nan))
        if not np.isfinite(mid) or mid <= 0.0 or not np.isfinite(spread) or spread < 0.0:
            return None
        cost += delta * spread / (2.0 * mid)
    return gross - cost


def _optimize_names(
    names: tuple[str, ...],
    past_returns: pd.DataFrame,
    config: FrontierConfig,
) -> tuple[dict[str, float], float, str] | None:
    window = past_returns.loc[:, list(names)].dropna(how="any")
    if len(window) < config.lookback:
        return None
    window = window.iloc[-config.lookback :]
    mean, covariance, _, _ = annualize_moments(window)
    result = frontier_portfolio(mean, covariance, config)
    weights = {name: float(result.weights[name]) for name in names}
    return weights, float(result.target_used), result.note


def _can_adopt(
    candidate: tuple[str, ...],
    held: dict[str, float],
    today_close: pd.Series,
) -> tuple[bool, list[str]]:
    dropped = [
        name
        for name, weight in held.items()
        if abs(weight) > 0.0 and name not in candidate
    ]
    missing = [
        name
        for name in list(candidate) + dropped
        if name not in today_close.index or not np.isfinite(today_close[name]) or today_close[name] <= 0
    ]
    return not missing, missing


def simulate(
    closes: pd.DataFrame,
    spreads: pd.DataFrame,
    config: FrontierConfig | None = None,
) -> SimulationResult:
    """Run the monthly list choice and the daily frontier on one panel.

    ``closes`` and ``spreads`` are session-dated panels. The returned plans
    use ``config.research_nav`` and are marked ``sent_to_broker=False``.
    """
    config = config or FrontierConfig()
    if closes.empty:
        raise ValueError("close panel is empty")
    if config.margin_rates is not None:
        missing = [column for column in closes.columns if column not in config.margin_rates]
        if missing:
            preview = ", ".join(missing[:8])
            raise ValueError(
                f"margin rates missing for {len(missing)} symbols, including {preview}"
            )
        logger.info(
            "Walk-forward marks to market margin notionals at %.0f%% of NAV using %d margin rates",
            MARGIN_TARGET * 100,
            len(config.margin_rates),
        )
    spreads = spreads.reindex(index=closes.index, columns=closes.columns)
    returns = simple_returns(closes)
    dates = list(pd.DatetimeIndex(closes.index).sort_values())
    paths: dict[int, list[DayDecision]] = {size: [] for size in config.portfolio_sizes}
    month_returns: dict[int, list[list[float]]] = {size: [] for size in config.portfolio_sizes}
    month_index: dict[int, list[pd.DatetimeIndex]] = {size: [] for size in config.portfolio_sizes}
    held: dict[int, dict[str, float]] = {size: {} for size in config.portfolio_sizes}
    held_mix: dict[int, dict[str, float]] = {size: {} for size in config.portfolio_sizes}
    active: dict[int, tuple[str, ...] | None] = {size: None for size in config.portfolio_sizes}
    candidate: dict[int, tuple[str, ...] | None] = {size: None for size in config.portfolio_sizes}
    exclusions: list[dict] = []
    skips: list[dict] = []
    chosen_size: dict[str, int] = {}
    chosen_rows: list[tuple[pd.Timestamp, float]] = []
    plans: list[RebalancePlan] = []
    research_units: dict[str, int] = {}
    winner: int | None = None
    current_month: tuple[int, int] | None = None
    selection_attempted = False
    month_bucket: dict[int, list[float]] = {size: [] for size in config.portfolio_sizes}
    month_dates: dict[int, list[pd.Timestamp]] = {size: [] for size in config.portfolio_sizes}

    def close_month() -> None:
        nonlocal winner
        for size in config.portfolio_sizes:
            month_returns[size].append(list(month_bucket[size]))
            month_index[size].append(pd.DatetimeIndex(month_dates[size]))
            month_bucket[size] = []
            month_dates[size] = []
        if any(len(month_returns[size]) < config.score_months for size in config.portfolio_sizes):
            winner = None
            return
        scores: dict[int, float] = {}
        for size in config.portfolio_sizes:
            values: list[float] = []
            idx: list[pd.Timestamp] = []
            for bucket, stamps in zip(
                month_returns[size][-config.score_months :],
                month_index[size][-config.score_months :],
            ):
                values.extend(bucket)
                idx.extend(list(stamps))
            scores[size] = _sharpe(values, pd.DatetimeIndex(idx)) if idx else float("nan")
        winner = choose_size(scores)
        if winner is None:
            logger.error("No finite walk-forward Sharpe; the next month has no chosen list")
            return
        logger.info("Walk-forward chose %d assets (Sharpe by size: %s)", winner, scores)

    for day in dates:
        key = _month_key(day)
        if current_month is None:
            current_month = key
        elif key != current_month:
            close_month()
            current_month = key
            candidate = {size: None for size in config.portfolio_sizes}
            selection_attempted = False
        if not selection_attempted:
            past = returns.loc[returns.index < day]
            past_spreads = spreads.loc[spreads.index < day]
            for size in config.portfolio_sizes:
                try:
                    selection = select_portfolio(
                        past,
                        past_spreads,
                        portfolio_size=size,
                        min_overlap=config.min_overlap,
                    )
                except SelectionError as exc:
                    logger.error("Selection of %d assets failed on %s: %s", size, day.date(), exc)
                    exclusions.append({"session_date": str(day.date()), "portfolio_size": size, "error": str(exc)})
                    candidate[size] = None
                    continue
                candidate[size] = selection.symbols
                for item in selection.exclusions:
                    exclusions.append({"session_date": str(day.date()), "portfolio_size": size, **item})
            selection_attempted = True
        today_close = closes.loc[day]
        today_return = returns.loc[day]
        today_spread = spreads.loc[day]
        for size in config.portfolio_sizes:
            proposed = candidate[size]
            names = active[size]
            reason = ""
            if proposed is not None:
                adopted, missing = _can_adopt(proposed, held_mix[size], today_close)
                if adopted:
                    active[size] = proposed
                    names = proposed
                elif names is None:
                    reason = f"list not adopted, missing {missing}"
                else:
                    reason = f"rollover deferred, missing {missing}"
                    skips.append({"session_date": str(day.date()), "portfolio_size": size, "reason": reason, "symbols": missing})
                    logger.warning("%s %s", day.date(), reason)
            if names is None:
                decision = DayDecision(
                    str(day.date()), size, None, {}, True, reason or "no eligible list", (), None
                )
                paths[size].append(decision)
                continue
            missing_active = [
                name for name in names
                if not np.isfinite(today_close.get(name, np.nan)) or today_close.get(name, 0) <= 0
            ]
            previous = dict(held[size])
            if missing_active:
                reason = f"missing daily candle: {missing_active}"
                skips.append({"session_date": str(day.date()), "portfolio_size": size, "reason": reason, "symbols": missing_active})
                logger.warning("%s size=%d %s", day.date(), size, reason)
                mix = dict(held_mix[size])
                traded = previous
                skipped = True
                target_used = None
                note = ""
            else:
                history = returns.loc[returns.index <= day, list(names)]
                try:
                    optimized = _optimize_names(names, history, config)
                except Exception as exc:
                    logger.error("Frontier failed on %s size=%d: %s", day.date(), size, exc)
                    optimized = None
                    reason = str(exc)
                if optimized is None:
                    reason = reason or f"fewer than {config.lookback} joint sessions"
                    skips.append({"session_date": str(day.date()), "portfolio_size": size, "reason": reason, "symbols": list(names)})
                    mix = dict(held_mix[size])
                    traded = previous
                    skipped = True
                    target_used = None
                    note = ""
                else:
                    mix, target_used, note = optimized
                    traded = (
                        margin_notional_weights(
                            mix,
                            config.margin_rates or {},
                            margin_fraction=MARGIN_TARGET,
                        )
                        if config.margin_rates
                        else mix
                    )
                    skipped = False
                    reason = ""
            net = _mark_to_market(previous, today_return, traded, today_spread, today_close)
            if net is not None:
                month_bucket[size].append(net)
                month_dates[size].append(day)
            held[size] = traded
            held_mix[size] = mix
            paths[size].append(
                DayDecision(
                    session_date=str(day.date()),
                    portfolio_size=size,
                    net_return=net,
                    weights=mix,
                    skipped=skipped,
                    reason=reason,
                    symbols=names,
                    target_used=target_used,
                    note=note,
                )
            )
            if winner == size and not skipped and mix:
                chosen_size[str(day.date())] = size
                if net is not None:
                    chosen_rows.append((day, net))
                mids = {name: float(today_close[name]) for name in mix}
                plan = build_plan(
                    session_date=day.date(),
                    list_version=f"{day.date().isoformat()}-k{size}",
                    nav=config.research_nav,
                    weights=mix,
                    mids=mids,
                    current_units=research_units,
                    min_trade_units=config.min_trade_units,
                    target_return_used=target_used,
                    note=note,
                    sent_to_broker=False,
                    margin_rates=config.margin_rates,
                    margin_capacity=config.research_nav if config.margin_rates else None,
                )
                plan.status = "simulated"
                plans.append(plan)
                for leg in plan.legs:
                    research_units[leg.instrument] = int(
                        research_units.get(leg.instrument, 0) + leg.units
                    )
                for name in list(research_units):
                    if research_units[name] == 0:
                        research_units.pop(name)
                for skip in plan.skips:
                    skips.append({"session_date": str(day.date()), "portfolio_size": size, **skip})

    close_month()
    chosen = pd.Series(
        [value for _, value in chosen_rows],
        index=pd.DatetimeIndex([day for day, _ in chosen_rows]),
        name="net_return",
    )
    final_symbols: tuple[str, ...] = ()
    final_weights = None
    final_mix = None
    comparison = None
    sharpe_by_size = {
        size: _sharpe(
            [item for bucket in month_returns[size] for item in bucket],
            pd.DatetimeIndex([stamp for stamps in month_index[size] for stamp in stamps]),
        )
        for size in config.portfolio_sizes
    }
    if plans:
        last = plans[-1]
        final_symbols = tuple(sorted(last.mix_weights or last.weights))
        final_weights = pd.Series(last.weights, name="weight")
        final_mix = pd.Series(last.mix_weights or last.weights, name="mix")
        window = returns.loc[:, list(final_symbols)].dropna(how="any").iloc[-config.lookback :]
        if len(window) >= config.lookback:
            _, traded, _, alpha = annualize_moments(window)
            sample = window.cov()
            factor = one_factor_covariance(window)
            comparison = {
                "shrinkage": alpha,
                "ledoit_wolf": traded,
                "sample": sample,
                "one_factor": pd.DataFrame(factor, index=window.columns, columns=window.columns),
            }
    logger.info(
        "Simulation finished: sessions=%d plans=%d skips=%d exclusions=%d",
        len(dates),
        len(plans),
        len(skips),
        len(exclusions),
    )
    return SimulationResult(
        config=config,
        paths=paths,
        chosen_size=chosen_size,
        chosen_returns=chosen,
        plans=plans,
        skips=skips,
        exclusions=exclusions,
        final_symbols=final_symbols,
        final_weights=final_weights,
        final_mix=final_mix,
        covariance_comparison=comparison,
        sharpe_by_size=sharpe_by_size,
    )


@dataclass(frozen=True)
class SessionBook:
    """Target book for one closed session.

    ``held`` is true when that session did not rebalance. The weights are
    the book carried forward, and ``reason`` says why the session was skipped.
    """

    session_date: str
    weights: dict[str, float]
    list_version: str
    target_return_used: float | None
    held: bool
    reason: str


def _portfolio_size(list_version: str) -> int:
    marker = "-k"
    if marker not in list_version:
        raise RuntimeError(f"list version {list_version!r} has no portfolio size")
    suffix = list_version.rsplit(marker, 1)[-1]
    if not suffix.isdigit():
        raise RuntimeError(f"list version {list_version!r} has no portfolio size")
    return int(suffix)


def _decision_on(
    result: SimulationResult,
    session_date: str,
    size: int,
) -> DayDecision | None:
    for item in result.paths.get(size, []):
        if item.session_date == session_date:
            return item
    return None


def book_for_session(result: SimulationResult, session_date: str) -> SessionBook:
    """Return the chosen book's targets on one closed session.

    A session that rebalanced returns that plan. A session that kept the
    previous book, because a held name had no candle, returns those carried
    weights. A session with no carried book raises.
    """
    plans = [plan for plan in result.plans if plan.session_date <= session_date]
    if not plans:
        raise RuntimeError(f"no research plan exists on or before {session_date}")
    latest = plans[-1]
    mix = latest.mix_weights or latest.weights
    if latest.session_date == session_date:
        return SessionBook(
            session_date=latest.session_date,
            weights=dict(mix),
            list_version=latest.list_version,
            target_return_used=latest.target_return_used,
            held=False,
            reason=latest.note,
        )
    size = _portfolio_size(latest.list_version)
    decision = _decision_on(result, session_date, size)
    if decision is None or not decision.skipped or not decision.weights:
        detail = decision.reason if decision is not None and decision.reason else "no carried book"
        raise RuntimeError(
            f"latest session {session_date} has no plan "
            f"(last plan is {latest.session_date}; {detail})"
        )
    logger.warning(
        "Session %s has no new plan; holding %s weights because %s",
        session_date,
        latest.session_date,
        decision.reason,
    )
    return SessionBook(
        session_date=session_date,
        weights=dict(decision.weights),
        list_version=latest.list_version,
        target_return_used=latest.target_return_used,
        held=True,
        reason=decision.reason,
    )
