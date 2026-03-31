"""HMM layer — latent-state inference training and model management (strategy.md §9.10)."""

from .hmm_trainer import HMMTrainer
from .hmm_model import HMMModel

__all__ = ["HMMTrainer", "HMMModel"]
