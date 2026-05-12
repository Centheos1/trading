"""HMM layer — latent-state inference training and model management (strategy.md §9.10).

Phase 16 also exposes the campaign harness primitives (``run_campaign``,
``aggregate_verdict``, ``CampaignVerdict``, ``VerdictThreshold``) so
downstream tooling and tests can import them via ``from hmm import ...``
without reaching into the submodule.
"""

from .hmm_model import HMMModel
from .hmm_trainer import HMMTrainer

# Phase 16 — exported for the CLI (``tools.hmm_abtest``) and tests.
from .abtest import (
    AbtestSummary,
    CampaignVerdict,
    DEFAULT_CAMPAIGN_SEED,
    DEFAULT_CAMPAIGN_SYMBOLS,
    DEFAULT_CAMPAIGN_WINDOWS,
    DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN,
    DEFAULT_VERDICT_WIN_RATIO_MIN,
    SHORTHAND_WINDOWS_DAYS,
    VerdictThreshold,
    aggregate_verdict,
    campaign_verdict_to_dict,
    compute_config_snapshot_hash,
    format_campaign_pair_report,
    format_campaign_summary_report,
    parse_window_arg,
    run_campaign,
    write_config_snapshot,
)

__all__ = [
    "HMMTrainer",
    "HMMModel",
    "AbtestSummary",
    "CampaignVerdict",
    "DEFAULT_CAMPAIGN_SEED",
    "DEFAULT_CAMPAIGN_SYMBOLS",
    "DEFAULT_CAMPAIGN_WINDOWS",
    "DEFAULT_VERDICT_MEDIAN_SHARPE_DELTA_MIN",
    "DEFAULT_VERDICT_WIN_RATIO_MIN",
    "SHORTHAND_WINDOWS_DAYS",
    "VerdictThreshold",
    "aggregate_verdict",
    "campaign_verdict_to_dict",
    "compute_config_snapshot_hash",
    "format_campaign_pair_report",
    "format_campaign_summary_report",
    "parse_window_arg",
    "run_campaign",
    "write_config_snapshot",
]
