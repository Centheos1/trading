"""Phase 14F.2 — Wave/Tide STRAT_PARAMS ↔ dataclass drift audit.

Mirrors the Phase 13Y orderflow binding-drift guard for the
``optimiser.Nsga2`` → ``strategies.orderflow`` C++ surface, but for the
Python-only Wave / Tide optimisation paths.

The Wave / Tide GAs do ``setattr(params, name, value)`` for each
``ParamSpec.name`` they sample (see ``_apply`` in
``wave/wave_optimiser.py`` and ``tide/tide_optimiser.py``). A typo or
silent rename produces a stale attribute that is never read by the
backtester — the GA "optimises" the parameter but it has zero effect.

This suite pins both directions of the contract:

1.  **Forward audit.** Every ``ParamSpec.name`` MUST be a real field on
    the corresponding ``WaveStrategyParams`` / ``TideStrategyParams``
    dataclass. Otherwise the GA writes a phantom attribute.

2.  **Reverse audit.** Every dataclass field MUST either be in the
    optimiser's ``param_space`` OR explicitly listed below as
    "fixed / not optimised". A field that drops off the param_space
    silently (e.g. someone renames ``vol_window`` to ``volatility_window``
    on ``TideStrategyParams`` but forgets the ParamSpec) flips from
    "in audit" to "implicitly whitelisted", which this test catches by
    requiring an explicit whitelist update.

3.  **Cross-check.** Each Wave key under ``STRAT_PARAMS["orderflow"]``
    (the legacy C++ orderflow optimiser entry that also exposes Wave
    knobs per the Phase 6 plan) MUST be a real field on
    ``schemas.WaveConfig``.

The whitelists below are deliberately verbose so any rename forces a
maintainer to either add the field to the GA or explicitly mark it as
"not tunable" with a one-line justification.
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import fields as dc_fields

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import STRAT_PARAMS  # noqa: E402
from schemas import WaveConfig  # noqa: E402
from wave.wave_backtest import WaveStrategyParams  # noqa: E402
from wave.wave_optimiser import WaveOptimiserConfig  # noqa: E402
from tide.tide_backtest import TideStrategyParams  # noqa: E402
from tide.tide_optimiser import TideOptimiserConfig  # noqa: E402


# ──────────────────────────────────────────────────────────────────────
# Whitelists: fields that are intentionally NOT part of the GA search
# space. Each entry MUST have a one-line justification — without it the
# audit is useless. Reverse-audit failures surface as
# "field X is on the dataclass but not in param_space and not
#  whitelisted; either add it to the GA or whitelist it explicitly".
# ──────────────────────────────────────────────────────────────────────

# WaveStrategyParams fields NOT tuned by the NSGA-II in wave_optimiser.
WAVE_NON_OPTIMISED_FIELDS: dict[str, str] = {
    # Sizing / capital / fees — environment, not strategy.
    "sizing_mode": "fixed at construction; switching modes is a regime decision, not a GA axis",
    "initial_capital": "fixed at construction; we report relative metrics",
    "max_position_usd": "fixed at construction; sizing cap, not a tunable",
    "max_position_base": "fixed at construction; sizing cap, not a tunable",
    "leverage": "fixed at construction; venue setting",
    "taker_fee_bps": "fee model, not a GA axis",
    "maker_fee_bps": "fee model, not a GA axis",
    "use_taker_fees": "fee model toggle, not a GA axis",
    "liquidation_equity_frac": "Phase 9 hardening — risk floor, not a tunable",
    "slippage_bps": "execution model, not a GA axis",
    "slippage_per_unit_bps": "execution model, not a GA axis",
    # Identity / data routing.
    "symbol": "identity, not tunable",
    "exchange": "identity, not tunable",
    "bar_seconds": "auto-derived from timeframe via with_timeframe()",
    "mode": "isolated|stacked is a CLI mode flag, not a tunable",
    "accuracy_horizons": "auto-derived per timeframe (see _HORIZONS_BY_TF)",
    # Engine state knobs that the Wave backtest does not currently expose.
    "update_interval_ms": "0 = update every bar in the backtester; not user-tunable",
}

# TideStrategyParams fields NOT tuned by the NSGA-II in tide_optimiser.
TIDE_NON_OPTIMISED_FIELDS: dict[str, str] = {
    # Currently not in the GA (deliberate fixed-point design choice).
    "lsi_weights": "vector parameter; the GA exposes only the scalar threshold/slope today",
    "es_budget_global": "fixed risk budget; not part of strategy parameter tuning",
    "update_interval_ms": "fixed cadence; not exposed to the GA",
    # Sizing / capital / fees — environment, not strategy.
    "sizing_mode": "fixed at construction; switching modes is a regime decision",
    "initial_capital": "fixed at construction; we report relative metrics",
    "max_position_usd": "fixed at construction; sizing cap",
    "max_position_base": "fixed at construction; sizing cap",
    "leverage": "fixed at construction; venue setting",
    "taker_fee_bps": "fee model",
    "maker_fee_bps": "fee model",
    "use_taker_fees": "fee model toggle",
    # Identity / data routing.
    "symbol": "identity",
    "exchange": "identity",
    "bar_seconds": "auto-derived from timeframe",
    "accuracy_horizons": "auto-derived; horizons are scaled per timeframe",
}


def _field_names(dc_cls) -> set[str]:
    return {f.name for f in dc_fields(dc_cls)}


def _param_space_names(cfg) -> set[str]:
    return {spec.name for spec in cfg.param_space}


class TestWaveParamSpaceForward(unittest.TestCase):
    """Every WaveOptimiserConfig.param_space name must be a real
    WaveStrategyParams field."""

    def test_every_param_space_name_is_a_real_field(self):
        names = _param_space_names(WaveOptimiserConfig())
        fields = _field_names(WaveStrategyParams)
        missing = names - fields
        self.assertFalse(
            missing,
            f"WaveOptimiserConfig.param_space references non-existent "
            f"WaveStrategyParams fields: {sorted(missing)}. Either "
            f"add the fields or fix the param_space.",
        )


class TestWaveParamSpaceReverse(unittest.TestCase):
    """Every WaveStrategyParams field must either be in
    WaveOptimiserConfig.param_space OR explicitly whitelisted as
    'fixed / not optimised'."""

    def test_every_field_is_either_in_param_space_or_whitelisted(self):
        fields = _field_names(WaveStrategyParams)
        names = _param_space_names(WaveOptimiserConfig())
        whitelisted = set(WAVE_NON_OPTIMISED_FIELDS)
        unaccounted = fields - names - whitelisted
        self.assertFalse(
            unaccounted,
            f"WaveStrategyParams fields are neither in the GA "
            f"param_space nor whitelisted: {sorted(unaccounted)}. "
            f"Either add them to WaveOptimiserConfig.param_space "
            f"OR add them to WAVE_NON_OPTIMISED_FIELDS with a "
            f"one-line justification.",
        )

    def test_whitelist_does_not_reference_phantom_fields(self):
        fields = _field_names(WaveStrategyParams)
        stale = set(WAVE_NON_OPTIMISED_FIELDS) - fields
        self.assertFalse(
            stale,
            f"WAVE_NON_OPTIMISED_FIELDS whitelists fields that no "
            f"longer exist on WaveStrategyParams: {sorted(stale)}. "
            f"Remove the stale entries.",
        )


class TestTideParamSpaceForward(unittest.TestCase):
    """Every TideOptimiserConfig.param_space name must be a real
    TideStrategyParams field."""

    def test_every_param_space_name_is_a_real_field(self):
        names = _param_space_names(TideOptimiserConfig())
        fields = _field_names(TideStrategyParams)
        missing = names - fields
        self.assertFalse(
            missing,
            f"TideOptimiserConfig.param_space references non-existent "
            f"TideStrategyParams fields: {sorted(missing)}. Either "
            f"add the fields or fix the param_space.",
        )


class TestTideParamSpaceReverse(unittest.TestCase):
    """Every TideStrategyParams field must either be in
    TideOptimiserConfig.param_space OR explicitly whitelisted."""

    def test_every_field_is_either_in_param_space_or_whitelisted(self):
        fields = _field_names(TideStrategyParams)
        names = _param_space_names(TideOptimiserConfig())
        whitelisted = set(TIDE_NON_OPTIMISED_FIELDS)
        unaccounted = fields - names - whitelisted
        self.assertFalse(
            unaccounted,
            f"TideStrategyParams fields are neither in the GA "
            f"param_space nor whitelisted: {sorted(unaccounted)}. "
            f"Either add them to TideOptimiserConfig.param_space "
            f"OR add them to TIDE_NON_OPTIMISED_FIELDS with a "
            f"one-line justification.",
        )

    def test_whitelist_does_not_reference_phantom_fields(self):
        fields = _field_names(TideStrategyParams)
        stale = set(TIDE_NON_OPTIMISED_FIELDS) - fields
        self.assertFalse(
            stale,
            f"TIDE_NON_OPTIMISED_FIELDS whitelists fields that no "
            f"longer exist on TideStrategyParams: {sorted(stale)}. "
            f"Remove the stale entries.",
        )


class TestStratParamsWaveCrossCheck(unittest.TestCase):
    """STRAT_PARAMS['orderflow'] exposes a Wave subset for the legacy
    C++ orderflow optimiser. Each Wave key MUST exist on
    schemas.WaveConfig — the canonical Python dataclass the runtime
    uses to translate snapshots into the C++ engine."""

    # Subset of STRAT_PARAMS["orderflow"] that are documented as Wave
    # parameters (Phase 6 plan, lines 65-70 of utils.py).
    WAVE_KEYS_IN_STRAT_PARAMS = {
        "eta_mr_threshold",
        "eta_bo_threshold",
        "eta_neutral_threshold",
        "reduced_size_fraction",
    }

    def test_each_wave_key_in_strat_params_exists_on_wave_config(self):
        wave_config_fields = _field_names(WaveConfig)
        of_keys = set(STRAT_PARAMS["orderflow"].keys())
        # Pin that the documented Wave subset is actually still in the
        # legacy orderflow STRAT_PARAMS (catches accidental deletion).
        self.assertTrue(
            self.WAVE_KEYS_IN_STRAT_PARAMS.issubset(of_keys),
            f"STRAT_PARAMS['orderflow'] no longer contains the "
            f"documented Wave subset; missing: "
            f"{sorted(self.WAVE_KEYS_IN_STRAT_PARAMS - of_keys)}",
        )
        # And pin that each Wave key maps to a real WaveConfig field.
        missing = self.WAVE_KEYS_IN_STRAT_PARAMS - wave_config_fields
        self.assertFalse(
            missing,
            f"STRAT_PARAMS Wave keys missing on schemas.WaveConfig: "
            f"{sorted(missing)} — silent drift between the legacy C++ "
            f"optimiser's exposed knobs and the canonical config.",
        )


if __name__ == "__main__":
    unittest.main()
