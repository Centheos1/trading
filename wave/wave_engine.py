"""
WaveEngine — deterministic regime classifier and permission publisher.

Implements strategy.md §8:
  - Structural features: trend efficiency (§8.5.4), distance-to-structure (§8.5.5)
  - Regime state machine (§8.8): NEUTRAL, MEAN_REVERSION, BREAKOUT, BREAKDOWN
  - Permissions matrix (§18): maps (TideBias × WaveRegime) → PermissionSet

V1 notes:
  - Dispersion (§8.5.2) and absorption ratio (§8.5.3) require multi-asset data.
    In V1 single-symbol mode they are externally settable; defaults keep the
    state machine functional (trend efficiency is the primary driver).
  - No HMM, no PCA, no residual dislocation.

Phase 8 extensions:
  - Cross-venue features (lead/lag, divergence, correlation) from CrossVenueEngine.
  - Low cross-venue correlation boosts effective absorption ratio → BREAKDOWN.
  - Large divergence boosts effective dispersion → BREAKDOWN.
  - High correlation confirms regime stability.
"""

from __future__ import annotations

from collections import deque
from typing import Dict, Optional

from schemas import (
    TideBias,
    WaveRegime,
    PermissionLevel,
    PermissionSet,
    WaveSnapshot,
    WaveConfig,
)


# ── Permissions matrix (§18) ──────────────────────────────────────────

_PERM = PermissionLevel
_F, _R, _D = _PERM.FULL, _PERM.REDUCED, _PERM.DISABLED

_PERMISSIONS_TABLE: Dict[
    tuple[TideBias, WaveRegime],
    tuple[PermissionLevel, PermissionLevel, PermissionLevel, PermissionLevel],
] = {
    # (TideBias, WaveRegime) → (long_bounce, short_bounce, long_breakout, short_breakout)
    (TideBias.LONG, WaveRegime.MEAN_REVERSION): (_F, _R, _F, _D),
    (TideBias.LONG, WaveRegime.BREAKOUT):       (_R, _D, _F, _D),
    (TideBias.LONG, WaveRegime.BREAKDOWN):      (_D, _D, _D, _D),
    (TideBias.LONG, WaveRegime.NEUTRAL):        (_F, _R, _R, _D),
    (TideBias.SHORT, WaveRegime.MEAN_REVERSION): (_R, _F, _D, _F),
    (TideBias.SHORT, WaveRegime.BREAKOUT):       (_D, _R, _D, _F),
    (TideBias.SHORT, WaveRegime.BREAKDOWN):      (_D, _D, _D, _D),
    (TideBias.SHORT, WaveRegime.NEUTRAL):        (_R, _F, _D, _R),
    (TideBias.NEUTRAL, WaveRegime.MEAN_REVERSION): (_F, _F, _R, _R),
    (TideBias.NEUTRAL, WaveRegime.BREAKOUT):       (_R, _R, _F, _F),
    (TideBias.NEUTRAL, WaveRegime.BREAKDOWN):      (_D, _D, _R, _R),
    (TideBias.NEUTRAL, WaveRegime.NEUTRAL):        (_F, _F, _R, _R),
}


def lookup_permissions(
    bias: TideBias,
    regime: WaveRegime,
    reduced_size_fraction: float = 0.5,
) -> PermissionSet:
    """§18 permissions matrix lookup. Never returns an undefined state."""
    lb, sb, lbo, sbo = _PERMISSIONS_TABLE[(bias, regime)]
    return PermissionSet(
        long_bounce=lb,
        short_bounce=sb,
        long_breakout=lbo,
        short_breakout=sbo,
        reduced_size_fraction=reduced_size_fraction,
    )


# ── Feature computation helpers ───────────────────────────────────────

def compute_trend_efficiency(prices: list[float] | deque) -> float:
    """§8.5.4 — η = |Σ ΔPᵢ| / Σ|ΔPᵢ|.  Returns 0.0 on insufficient data."""
    if len(prices) < 2:
        return 0.0
    net = 0.0
    gross = 0.0
    prev = prices[0]
    for p in list(prices)[1:]:
        d = p - prev
        net += d
        gross += abs(d)
        prev = p
    if gross == 0.0:
        return 0.0
    return abs(net) / gross


def compute_trend_direction(prices: list[float] | deque) -> float:
    """Sign of the net price change over the price window.

    Returns +1.0 if price ended higher, -1.0 if lower, 0.0 if flat or
    insufficient data.  Used alongside η to distinguish BREAKOUT (up) from
    BREAKDOWN (down) — η itself discards direction (it takes abs(net)).
    """
    if len(prices) < 2:
        return 0.0
    net = list(prices)[-1] - list(prices)[0]
    if net > 1e-10:
        return 1.0
    if net < -1e-10:
        return -1.0
    return 0.0


def compute_distance_to_structure(
    price: float,
    *,
    session_vwap: float = 0.0,
    session_high: float = 0.0,
    session_low: float = 0.0,
) -> Dict[str, float]:
    """§8.5.5 — δⱼ = (P − Sⱼ) / P for each structural level."""
    if price <= 0.0:
        return {}
    out: Dict[str, float] = {}
    if session_vwap > 0.0:
        out["distance_vwap"] = (price - session_vwap) / price
    if session_high > 0.0:
        out["distance_session_high"] = (price - session_high) / price
    if session_low > 0.0:
        out["distance_session_low"] = (price - session_low) / price
    return out


# ── WaveEngine ────────────────────────────────────────────────────────

class WaveEngine:
    """Deterministic Wave regime classifier (strategy.md §8).

    Usage:
        engine = WaveEngine(config)
        engine.on_price(price, timestamp_ms)       # feed L1 prices
        engine.set_dispersion(value)                # set externally (V1)
        engine.set_absorption_ratio(value)          # set externally (V1)
        snap = engine.update(timestamp_ms, bias)    # produces WaveSnapshot
    """

    def __init__(self, config: WaveConfig | None = None):
        self._cfg = config or WaveConfig()
        self._regime: WaveRegime = WaveRegime.NEUTRAL
        self._last_update_ts: int = 0

        # Price buffer for trend efficiency (§8.5.4)
        self._price_window_ms: int = 60_000  # 60 s default
        self._prices: deque[tuple[int, float]] = deque()  # (ts_ms, price)

        # V1: externally set features (multi-asset features not computable from single symbol)
        self._dispersion: float = 0.0
        self._absorption_ratio: float = 0.5

        # Phase 8: cross-venue features
        self._cv_correlation: float = 1.0   # default: perfectly correlated (no cross-venue data)
        self._cv_divergence: float = 0.0
        self._cv_lead_lag: float = 0.0
        self._cv_available: bool = False

        # Cached features
        self._trend_efficiency: float = 0.5
        self._trend_direction: float = 0.0   # +1 up, -1 down, 0 flat/unknown
        self._distance_to_structure: Dict[str, float] = {}

        # Session structure levels (set externally or derived)
        self._session_vwap: float = 0.0
        self._session_high: float = 0.0
        self._session_low: float = 0.0
        self._last_price: float = 0.0

    # ── Configuration ─────────────────────────────────────────────

    @property
    def config(self) -> WaveConfig:
        return self._cfg

    def set_price_window_ms(self, ms: int) -> None:
        self._price_window_ms = max(ms, 1000)

    # ── Data ingestion ────────────────────────────────────────────

    def on_price(self, price: float, timestamp_ms: int) -> None:
        """Feed an L1 price observation."""
        if price <= 0.0:
            return
        self._prices.append((timestamp_ms, price))
        self._last_price = price
        self._trim_window(timestamp_ms)

    def set_session_structure(
        self,
        vwap: float = 0.0,
        high: float = 0.0,
        low: float = 0.0,
    ) -> None:
        self._session_vwap = vwap
        self._session_high = high
        self._session_low = low

    def set_dispersion(self, value: float) -> None:
        """Set cross-sectional dispersion externally (V1 single-symbol)."""
        self._dispersion = max(value, 0.0)

    def set_absorption_ratio(self, value: float) -> None:
        """Set absorption ratio externally (V1 single-symbol)."""
        self._absorption_ratio = max(0.0, min(value, 1.0))

    def set_crossvenue_snapshot(
        self,
        correlation: float,
        divergence: float,
        lead_lag: float = 0.0,
    ) -> None:
        """Set cross-venue features from CrossVenueEngine (Phase 8).

        Args:
            correlation: Pearson correlation of returns between venues [-1, 1].
            divergence: EMA-smoothed return divergence (primary − secondary).
            lead_lag: estimated lead/lag in observation intervals.
        """
        self._cv_correlation = max(-1.0, min(correlation, 1.0))
        self._cv_divergence = divergence
        self._cv_lead_lag = lead_lag
        self._cv_available = True

    # ── Main update ───────────────────────────────────────────────

    def update(
        self,
        timestamp_ms: int,
        bias: TideBias = TideBias.NEUTRAL,
    ) -> Optional[WaveSnapshot]:
        """Compute features, classify regime, publish snapshot.

        Returns None if cadence gate blocks the update (too soon since last).
        """
        if not self._check_cadence(timestamp_ms):
            return None
        self._last_update_ts = timestamp_ms

        self._compute_features()
        self._classify_regime()

        perms = lookup_permissions(
            bias, self._regime, self._cfg.reduced_size_fraction
        )

        return WaveSnapshot(
            timestamp=timestamp_ms,
            regime=self._regime,
            trend_efficiency=self._trend_efficiency,
            dispersion=self._dispersion,
            absorption_ratio=self._absorption_ratio,
            permissions=perms,
        )

    def get_snapshot(self, bias: TideBias = TideBias.NEUTRAL) -> WaveSnapshot:
        """Return the current state as a snapshot (no cadence gate)."""
        perms = lookup_permissions(
            bias, self._regime, self._cfg.reduced_size_fraction
        )
        return WaveSnapshot(
            timestamp=self._last_update_ts,
            regime=self._regime,
            trend_efficiency=self._trend_efficiency,
            dispersion=self._dispersion,
            absorption_ratio=self._absorption_ratio,
            permissions=perms,
        )

    # ── Read accessors ────────────────────────────────────────────

    @property
    def regime(self) -> WaveRegime:
        return self._regime

    @property
    def trend_efficiency(self) -> float:
        return self._trend_efficiency

    @property
    def trend_direction(self) -> float:
        return self._trend_direction

    @property
    def dispersion(self) -> float:
        return self._dispersion

    @property
    def absorption_ratio(self) -> float:
        return self._absorption_ratio

    @property
    def distance_to_structure(self) -> Dict[str, float]:
        return dict(self._distance_to_structure)

    @property
    def last_update_ts(self) -> int:
        return self._last_update_ts

    @property
    def cv_correlation(self) -> float:
        return self._cv_correlation

    @property
    def cv_divergence(self) -> float:
        return self._cv_divergence

    @property
    def cv_lead_lag(self) -> float:
        return self._cv_lead_lag

    @property
    def cv_available(self) -> bool:
        return self._cv_available

    def reset(self) -> None:
        self._regime = WaveRegime.NEUTRAL
        self._last_update_ts = 0
        self._prices.clear()
        self._dispersion = 0.0
        self._absorption_ratio = 0.5
        self._cv_correlation = 1.0
        self._cv_divergence = 0.0
        self._cv_lead_lag = 0.0
        self._cv_available = False
        self._trend_efficiency = 0.5
        self._trend_direction = 0.0
        self._distance_to_structure = {}
        self._session_vwap = 0.0
        self._session_high = 0.0
        self._session_low = 0.0
        self._last_price = 0.0

    # ── Internal ──────────────────────────────────────────────────

    def _trim_window(self, now_ms: int) -> None:
        cutoff = now_ms - self._price_window_ms
        while self._prices and self._prices[0][0] < cutoff:
            self._prices.popleft()

    def _check_cadence(self, timestamp_ms: int) -> bool:
        if self._last_update_ts == 0:
            return True
        return (timestamp_ms - self._last_update_ts) >= self._cfg.update_interval_ms

    def _compute_features(self) -> None:
        prices = [p for _, p in self._prices]
        self._trend_efficiency = compute_trend_efficiency(prices)
        self._trend_direction = compute_trend_direction(prices)

        if self._last_price > 0.0:
            self._distance_to_structure = compute_distance_to_structure(
                self._last_price,
                session_vwap=self._session_vwap,
                session_high=self._session_high,
                session_low=self._session_low,
            )

    def _classify_regime(self) -> None:
        """§8.8 state machine — deterministic threshold-based transitions.

        Direction-aware design
        ──────────────────────
        η = |Σ ΔPᵢ| / Σ|ΔPᵢ| measures trend *strength* but discards sign.
        `_trend_direction` (sign of net price change) is used alongside η so
        the engine can distinguish BREAKOUT (up-trending) from BREAKDOWN
        (down-trending).

        Regime semantics:
          BREAKOUT    — strong trend, price direction UP
          BREAKDOWN   — strong trend, price direction DOWN  OR  extreme stress
                        (both AR stress-proxy and dispersion are critical)
          MEAN_REVERSION — low efficiency, low dispersion (choppy/oscillating)
          NEUTRAL     — everything else

        Phase 8: when cross-venue data is available, low correlation boosts
        the effective AR (instability signal) and large absolute divergence
        boosts effective dispersion.  Extreme combined stress can trigger
        BREAKDOWN even when price direction is up (genuine dislocation).
        """
        eta = self._trend_efficiency
        d = self._dispersion
        ar = self._absorption_ratio
        direction = self._trend_direction   # +1 up / -1 down / 0 unknown
        cfg = self._cfg

        if self._cv_available:
            # Phase 14D — boost factors lifted to WaveConfig. Defaults
            # (2.0 / 0.5) reproduce pre-14D behaviour exactly.
            corr_deficit = max(0.0, 0.5 - self._cv_correlation)
            d = d + abs(self._cv_divergence) * cfg.crossvenue_divergence_boost
            ar = min(ar + corr_deficit * cfg.crossvenue_correlation_boost, 1.0)

        # ── Derived conditions ────────────────────────────────────────────
        trending_up   = eta > cfg.eta_bo_threshold and direction > 0
        trending_down = eta > cfg.eta_bo_threshold and direction < 0

        # Extreme combined stress (dislocation): BOTH AR and dispersion must
        # be elevated so AR alone (which can sit ~0.9 in normal markets) does
        # not trigger BREAKDOWN during an orderly bull run.
        extreme_stress = ar > cfg.ar_critical and d > cfg.dispersion_threshold
        # Independently, a critical dispersion spike overrides direction.
        dispersion_crisis = d > cfg.dispersion_critical

        stress = extreme_stress or dispersion_crisis

        # ── State transitions ─────────────────────────────────────────────
        if self._regime == WaveRegime.NEUTRAL:
            if trending_up:
                self._regime = WaveRegime.BREAKOUT
            elif trending_down or stress:
                self._regime = WaveRegime.BREAKDOWN
            elif eta < cfg.eta_mr_threshold and d < cfg.dispersion_threshold:
                self._regime = WaveRegime.MEAN_REVERSION

        elif self._regime == WaveRegime.MEAN_REVERSION:
            if trending_up:
                self._regime = WaveRegime.BREAKOUT
            elif trending_down or stress:
                self._regime = WaveRegime.BREAKDOWN
            elif eta > cfg.eta_neutral_threshold:
                self._regime = WaveRegime.NEUTRAL

        elif self._regime == WaveRegime.BREAKOUT:
            if trending_down or stress:
                # Direction reversed or extreme dislocation
                self._regime = WaveRegime.BREAKDOWN
            elif eta < cfg.eta_neutral_threshold:
                self._regime = WaveRegime.NEUTRAL

        elif self._regime == WaveRegime.BREAKDOWN:
            if trending_up and ar < cfg.ar_recover:
                # Price recovered AND stress normalised → BREAKOUT
                self._regime = WaveRegime.BREAKOUT
            elif not trending_down and ar < cfg.ar_recover and d < cfg.dispersion_threshold:
                # Momentum faded AND stress cleared → NEUTRAL
                self._regime = WaveRegime.NEUTRAL
