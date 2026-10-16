"""Cluster the pairwise correlation matrix and pick one name from each cluster.

An asset needs ``min_overlap`` of its own returns. A pair is used only when
the two names share ``min_overlap`` dates, and the correlation is computed
on their latest joint dates. Assets are then dropped, and each drop is
logged, until every remaining pair meets that overlap. The smallest
portfolio size wins a Sharpe tie.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from oanda_portfolio.frontier import shrink_correlation

logger = logging.getLogger(__name__)


class SelectionError(ValueError):
    """The eligible universe cannot form the requested portfolio."""


@dataclass
class Selection:
    symbols: tuple[str, ...]
    correlation: pd.DataFrame
    clusters: dict[str, tuple[str, ...]]
    exclusions: list[dict] = field(default_factory=list)


def _last_joint(left: pd.Series, right: pd.Series, count: int) -> pd.DataFrame | None:
    both = pd.concat([left.rename("left"), right.rename("right")], axis=1).dropna()
    if len(both) < count:
        return None
    return both.iloc[-count:]


def pairwise_correlation(
    returns: pd.DataFrame,
    min_overlap: int,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """Pairwise correlations on each pair's latest ``min_overlap`` joint dates.

    Returns the correlation, the overlap counts, and the assets removed
    because a pair did not have enough overlap.
    """
    if min_overlap < 3:
        raise ValueError("min_overlap must be at least 3")
    eligible = [
        column
        for column in returns.columns
        if int(returns[column].notna().sum()) >= min_overlap
    ]
    exclusions = [
        {"symbol": column, "reason": "fewer than min_overlap returns", "shortfall": min_overlap}
        for column in returns.columns
        if column not in eligible
    ]
    if len(eligible) < 2:
        raise SelectionError(
            f"only {len(eligible)} assets have {min_overlap} returns"
        )
    while True:
        shortfall: dict[str, int] = {name: 0 for name in eligible}
        for i, left in enumerate(eligible):
            for right in eligible[i + 1 :]:
                joint = _last_joint(returns[left], returns[right], min_overlap)
                if joint is None:
                    shortfall[left] += 1
                    shortfall[right] += 1
        worst = max(shortfall.values()) if shortfall else 0
        if worst == 0:
            break
        victim = sorted(name for name, count in shortfall.items() if count == worst)[0]
        eligible.remove(victim)
        exclusions.append(
            {
                "symbol": victim,
                "reason": "insufficient pairwise overlap",
                "shortfall": worst,
            }
        )
        logger.warning(
            "Excluded %s from clustering: %d pairs have fewer than %d overlapping days",
            victim,
            worst,
            min_overlap,
        )
        if len(eligible) < 2:
            raise SelectionError(
                "pairwise overlap filter left fewer than 2 assets"
            )
    size = len(eligible)
    corr = np.eye(size)
    counts = np.zeros((size, size))
    np.fill_diagonal(counts, min_overlap)
    for i, left in enumerate(eligible):
        for j in range(i + 1, size):
            right = eligible[j]
            joint = _last_joint(returns[left], returns[right], min_overlap)
            if joint is None:
                raise RuntimeError(f"overlap disappeared for {left}/{right}")
            rho = float(joint["left"].corr(joint["right"]))
            if not np.isfinite(rho):
                raise SelectionError(f"correlation of {left} and {right} is not finite")
            corr[i, j] = corr[j, i] = rho
            counts[i, j] = counts[j, i] = min_overlap
    labels = list(eligible)
    return (
        pd.DataFrame(corr, index=labels, columns=labels),
        pd.DataFrame(counts, index=labels, columns=labels),
        exclusions,
    )


def cluster_symbols(correlation: pd.DataFrame, portfolio_size: int) -> dict[int, tuple[str, ...]]:
    """Cut an average-linkage tree at exactly ``portfolio_size`` clusters."""
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import squareform
    except ImportError as exc:
        raise ImportError(
            "clustering requires scipy. Install notebooks/requirements.txt."
        ) from exc
    if portfolio_size < 2:
        raise ValueError("portfolio_size must be at least 2")
    if correlation.shape[0] < portfolio_size:
        raise SelectionError(
            f"cannot form {portfolio_size} clusters from {correlation.shape[0]} assets"
        )
    distance = np.clip(1.0 - correlation.to_numpy(dtype=float), 0.0, 2.0)
    np.fill_diagonal(distance, 0.0)
    condensed = squareform(distance, checks=False)
    tree = linkage(condensed, method="average")
    labels = fcluster(tree, t=portfolio_size, criterion="maxclust")
    grouped: dict[int, list[str]] = {}
    for name, label in zip(correlation.index, labels):
        grouped.setdefault(int(label), []).append(str(name))
    if len(grouped) != portfolio_size:
        raise SelectionError(
            f"clustering returned {len(grouped)} groups, requested {portfolio_size}"
        )
    return {label: tuple(sorted(names)) for label, names in grouped.items()}


def choose_representatives(
    clusters: dict[int, tuple[str, ...]],
    spreads: pd.DataFrame,
    min_overlap: int,
) -> dict[str, tuple[str, ...]]:
    """Pick the lowest median spread in each cluster. Ties use the symbol name."""
    chosen: dict[str, tuple[str, ...]] = {}
    for names in clusters.values():
        scores: list[tuple[float, str]] = []
        for name in names:
            series = spreads[name].dropna()
            if len(series) < min_overlap:
                logger.warning(
                    "Cluster member %s has %d spreads, need %d",
                    name,
                    len(series),
                    min_overlap,
                )
                continue
            window = series.iloc[-min_overlap:]
            if not np.isfinite(window.to_numpy(dtype=float)).all():
                logger.warning("Cluster member %s has a non-finite spread", name)
                continue
            scores.append((float(window.median()), name))
        if not scores:
            raise SelectionError(f"cluster {names} has no eligible representative")
        scores.sort()
        winner = scores[0][1]
        chosen[winner] = names
    return chosen


def select_portfolio(
    returns: pd.DataFrame,
    spreads: pd.DataFrame,
    *,
    portfolio_size: int,
    min_overlap: int,
) -> Selection:
    """Return one liquid representative from each of ``portfolio_size`` clusters."""
    correlation, counts, exclusions = pairwise_correlation(returns, min_overlap)
    shrunk = shrink_correlation(correlation.to_numpy(), counts.to_numpy())
    shrunk_frame = pd.DataFrame(shrunk, index=correlation.index, columns=correlation.columns)
    clusters = cluster_symbols(shrunk_frame, portfolio_size)
    representatives = choose_representatives(clusters, spreads[list(shrunk_frame.index)], min_overlap)
    symbols = tuple(sorted(representatives))
    logger.info("Selected %d assets: %s", len(symbols), ", ".join(symbols))
    return Selection(
        symbols=symbols,
        correlation=shrunk_frame,
        clusters=representatives,
        exclusions=exclusions,
    )
