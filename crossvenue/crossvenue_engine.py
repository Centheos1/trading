"""
CrossVenueEngine — computes cross-venue confirmation features.

Features (implementation_plan Phase 8):
  - lead/lag: which venue's price moves first (cross-correlation at offsets)
  - divergence: signed difference in price returns between venues
  - correlation: rolling Pearson correlation of returns

Operates at Wave cadence (~5 s). All computations are deterministic
given identical input sequences.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CrossVenueConfig:
    """Configuration for cross-venue feature computation."""
    window_ms: int = 60_000        # rolling window for correlation / lead-lag
    cadence_ms: int = 5_000        # minimum interval between recomputes
    max_lag_steps: int = 5         # max offset for lead/lag search
    min_observations: int = 10     # min paired returns before computing features
    divergence_ema_alpha: float = 0.1  # EMA smoothing for divergence


@dataclass
class CrossVenueSnapshot:
    """Output of cross-venue feature computation."""
    timestamp: int = 0

    lead_lag: float = 0.0
    """Signed: positive = primary venue leads; negative = secondary leads.
    Magnitude is the estimated lead in number of observation intervals."""

    divergence: float = 0.0
    """EMA-smoothed signed divergence of returns (primary − secondary).
    Positive = primary outperforming; negative = secondary outperforming."""

    correlation: float = 0.0
    """Rolling Pearson correlation of returns. Range [-1, 1].
    Low or negative correlation → regime instability signal."""

    n_paired: int = 0
    """Number of paired return observations in the current window."""

    primary_venue: str = ""
    secondary_venue: str = ""


class CrossVenueEngine:
    """Computes cross-venue features from paired L1 price streams.

    Usage:
        engine = CrossVenueEngine(config, primary="binance", secondary="oanda")
        engine.on_price("binance", price, timestamp_ms)
        engine.on_price("oanda", price, timestamp_ms)
        snap = engine.update(timestamp_ms)  # recompute features
    """

    def __init__(
        self,
        config: CrossVenueConfig | None = None,
        primary: str = "binance",
        secondary: str = "oanda",
    ):
        self._cfg = config or CrossVenueConfig()
        self._primary = primary
        self._secondary = secondary

        self._prices: dict[str, deque[tuple[int, float]]] = {
            primary: deque(),
            secondary: deque(),
        }

        self._last_update_ts: int = 0
        self._divergence_ema: float = 0.0

        self._lead_lag: float = 0.0
        self._divergence: float = 0.0
        self._correlation: float = 0.0
        self._n_paired: int = 0

    def on_price(self, venue: str, price: float, timestamp_ms: int) -> None:
        """Feed an L1 price observation from a venue."""
        if price <= 0.0:
            return
        buf = self._prices.get(venue)
        if buf is None:
            return
        buf.append((timestamp_ms, price))
        self._trim(venue, timestamp_ms)

    def update(self, timestamp_ms: int) -> Optional[CrossVenueSnapshot]:
        """Recompute features. Returns None if cadence gate not met."""
        if timestamp_ms - self._last_update_ts < self._cfg.cadence_ms:
            return None

        self._last_update_ts = timestamp_ms

        pri_returns = self._compute_returns(self._primary)
        sec_returns = self._compute_returns(self._secondary)

        paired = self._align_returns(pri_returns, sec_returns)
        self._n_paired = len(paired)

        if self._n_paired >= self._cfg.min_observations:
            pr = [r for r, _ in paired]
            sr = [r for _, r in paired]
            self._correlation = _pearson(pr, sr)
            self._lead_lag = self._compute_lead_lag(pri_returns, sec_returns)
            raw_div = _mean(pr) - _mean(sr)
            alpha = self._cfg.divergence_ema_alpha
            self._divergence_ema = alpha * raw_div + (1 - alpha) * self._divergence_ema
            self._divergence = self._divergence_ema

        return CrossVenueSnapshot(
            timestamp=timestamp_ms,
            lead_lag=self._lead_lag,
            divergence=self._divergence,
            correlation=self._correlation,
            n_paired=self._n_paired,
            primary_venue=self._primary,
            secondary_venue=self._secondary,
        )

    def get_snapshot(self) -> CrossVenueSnapshot:
        """Return current snapshot without cadence gating."""
        return CrossVenueSnapshot(
            timestamp=self._last_update_ts,
            lead_lag=self._lead_lag,
            divergence=self._divergence,
            correlation=self._correlation,
            n_paired=self._n_paired,
            primary_venue=self._primary,
            secondary_venue=self._secondary,
        )

    @property
    def lead_lag(self) -> float:
        return self._lead_lag

    @property
    def divergence(self) -> float:
        return self._divergence

    @property
    def correlation(self) -> float:
        return self._correlation

    def reset(self) -> None:
        for buf in self._prices.values():
            buf.clear()
        self._last_update_ts = 0
        self._divergence_ema = 0.0
        self._lead_lag = 0.0
        self._divergence = 0.0
        self._correlation = 0.0
        self._n_paired = 0

    # ── Internals ─────────────────────────────────────────────────

    def _trim(self, venue: str, now: int) -> None:
        buf = self._prices[venue]
        cutoff = now - self._cfg.window_ms
        while buf and buf[0][0] < cutoff:
            buf.popleft()

    def _compute_returns(self, venue: str) -> list[tuple[int, float]]:
        """Compute log returns from the price buffer."""
        buf = self._prices[venue]
        if len(buf) < 2:
            return []
        returns = []
        prev_ts, prev_p = buf[0]
        for ts, p in list(buf)[1:]:
            if prev_p > 0.0 and p > 0.0:
                returns.append((ts, math.log(p / prev_p)))
            prev_ts, prev_p = ts, p
        return returns

    def _align_returns(
        self,
        a: list[tuple[int, float]],
        b: list[tuple[int, float]],
    ) -> list[tuple[float, float]]:
        """Align returns by nearest timestamp (within half-cadence tolerance)."""
        if not a or not b:
            return []
        tol = self._cfg.cadence_ms // 2
        paired = []
        bi = 0
        for ts_a, r_a in a:
            while bi < len(b) - 1 and abs(b[bi + 1][0] - ts_a) < abs(b[bi][0] - ts_a):
                bi += 1
            if abs(b[bi][0] - ts_a) <= tol:
                paired.append((r_a, b[bi][1]))
        return paired

    def _compute_lead_lag(
        self,
        pri_returns: list[tuple[int, float]],
        sec_returns: list[tuple[int, float]],
    ) -> float:
        """Cross-correlation at different offsets to estimate lead/lag.

        Positive return = primary leads (primary returns at t predict
        secondary returns at t+offset).
        """
        max_lag = self._cfg.max_lag_steps
        best_corr = -2.0
        best_lag = 0

        for lag in range(-max_lag, max_lag + 1):
            pairs = self._align_with_offset(pri_returns, sec_returns, lag)
            if len(pairs) < self._cfg.min_observations:
                continue
            pr = [r for r, _ in pairs]
            sr = [r for _, r in pairs]
            c = _pearson(pr, sr)
            if c > best_corr:
                best_corr = c
                best_lag = lag

        return float(best_lag)

    def _align_with_offset(
        self,
        a: list[tuple[int, float]],
        b: list[tuple[int, float]],
        offset: int,
    ) -> list[tuple[float, float]]:
        """Align a[i] with b[i+offset] by index (not timestamp)."""
        if offset >= 0:
            start_a, start_b = 0, offset
        else:
            start_a, start_b = -offset, 0
        n = min(len(a) - start_a, len(b) - start_b)
        if n <= 0:
            return []
        return [(a[start_a + i][1], b[start_b + i][1]) for i in range(n)]


# ── Utility functions ────────────────────────────────────────────────

def _mean(xs: list[float]) -> float:
    if not xs:
        return 0.0
    return sum(xs) / len(xs)


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. Returns 0.0 on degenerate input."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return 0.0
    mx = _mean(xs)
    my = _mean(ys)
    cov = 0.0
    vx = 0.0
    vy = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        cov += dx * dy
        vx += dx * dx
        vy += dy * dy
    denom = math.sqrt(vx * vy)
    if denom < 1e-15:
        return 0.0
    return cov / denom
