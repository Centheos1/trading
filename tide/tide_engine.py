"""
Tide Engine — Phase 4 deterministic implementation.

Computes risk multiplier from vol_regime and liquidity stress (strategy.md §7.4.7),
publishes ES budget and max_position_usd from configuration.

V1 scope:
  - No dynamic vol regime detection from market data.
  - No macro feature ingestion.
  - Vol regime and LSI are set externally (by caller or config).
  - Risk multiplier is a pure deterministic function of (vol_regime, lsi).
  - Updates at event-time cadence (not wall-clock).
"""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import (
    TideBias,
    TideConfig,
    TideSnapshot,
    RiskBudgetSnapshot,
    VolRegime,
)


_VOL_REGIME_INDEX = {
    VolRegime.LOW: 0,
    VolRegime.NORMAL: 1,
    VolRegime.HIGH: 2,
    VolRegime.CRISIS: 3,
}


class TideEngine:
    """Deterministic Tide layer for Phase 4.

    Responsible for:
      - Publishing ES budget (from config)
      - Publishing max_position_usd (from config)
      - Computing risk_multiplier from vol_regime + LSI (§7.4.7)
      - Cadence-gating updates (event-time, configurable interval)
    """

    def __init__(self, config: TideConfig | None = None):
        self._cfg = config or TideConfig()
        self._bias = TideBias.NEUTRAL
        self._vol_regime = VolRegime.NORMAL
        self._lsi = 0.0
        self._risk_multiplier = self._cfg.risk_multiplier_default
        self._last_update_ts: int = 0
        self._realized_vol: float = 0.0

    @property
    def config(self) -> TideConfig:
        return self._cfg

    def set_vol_regime(self, regime: VolRegime) -> None:
        self._vol_regime = regime

    def set_liquidity_stress(self, lsi: float) -> None:
        self._lsi = lsi

    def set_bias(self, bias: TideBias) -> None:
        self._bias = bias

    def set_realized_vol(self, vol: float) -> None:
        self._realized_vol = max(vol, 0.0)

    def update(self, timestamp: int) -> TideSnapshot:
        """Recompute Tide state at the given event timestamp.

        Respects cadence gating: skips if called within update_interval_ms
        of the last update (event-time).
        """
        if (self._last_update_ts > 0 and
                timestamp - self._last_update_ts < self._cfg.update_interval_ms):
            return self.get_snapshot()

        self._risk_multiplier = self._compute_risk_multiplier()
        self._last_update_ts = timestamp
        return self.get_snapshot()

    def get_snapshot(self) -> TideSnapshot:
        return TideSnapshot(
            timestamp=self._last_update_ts,
            bias=self._bias,
            risk_multiplier=self._risk_multiplier,
            max_position_usd=self._cfg.max_position_usd,
            es_budget=self._cfg.es_budget_global,
            consumed_es=0.0,
            vol_regime=self._vol_regime,
            realized_vol=self._realized_vol,
            liquidity_stress=self._lsi,
        )

    def get_risk_budget(self) -> RiskBudgetSnapshot:
        """Return current risk budget snapshot for downstream consumers."""
        return RiskBudgetSnapshot(
            timestamp=self._last_update_ts,
            es_budget=self._cfg.es_budget_global,
            consumed_es=0.0,
            risk_multiplier=self._risk_multiplier,
            max_position_usd=self._cfg.max_position_usd,
            bias=self._bias,
            vol_regime=self._vol_regime,
        )

    def _compute_risk_multiplier(self) -> float:
        """§7.4.7: deterministic risk multiplier from vol_regime + LSI."""
        idx = _VOL_REGIME_INDEX.get(self._vol_regime, 1)
        regime_list = self._cfg.risk_mult_by_regime
        if idx < len(regime_list):
            base = regime_list[idx]
        else:
            base = self._cfg.risk_multiplier_default

        if self._lsi > self._cfg.lsi_reduce_threshold:
            stress_penalty = (
                (self._lsi - self._cfg.lsi_reduce_threshold)
                * self._cfg.lsi_reduce_slope
            )
            stress_penalty = max(0.0, min(stress_penalty, base))
            base -= stress_penalty

        return max(0.0, min(base, 1.0))
