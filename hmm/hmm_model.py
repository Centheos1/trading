"""
HMM model data structure and JSON serialization.

The JSON format is consumed by the C++ HMMBasedInference::load_model_from_string().
"""

import json
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class HMMModel:
    """Trained HMM parameters for the Ripple state inference backend."""

    K: int = 5
    transition: np.ndarray = field(default_factory=lambda: np.zeros((5, 5)))
    means: np.ndarray = field(default_factory=lambda: np.zeros((5, 6)))
    variances: np.ndarray = field(default_factory=lambda: np.ones((5, 6)) * 0.05)
    state_map: List[int] = field(default_factory=lambda: [1, 2, 3, 4, 5])
    log_prior: Optional[np.ndarray] = None
    bic: float = 0.0
    log_likelihood: float = 0.0
    n_observations: int = 0

    def to_json(self) -> str:
        d = {
            "K": self.K,
            "transition": self.transition.tolist(),
            "means": self.means.tolist(),
            "variances": self.variances.tolist(),
            "state_map": self.state_map,
        }
        if self.log_prior is not None:
            d["log_prior"] = self.log_prior.tolist()
        return json.dumps(d, indent=2)

    def save(self, path: str) -> None:
        with open(path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def load(cls, path: str) -> "HMMModel":
        with open(path) as f:
            return cls.from_json(f.read())

    @classmethod
    def from_json(cls, s: str) -> "HMMModel":
        d = json.loads(s)
        m = cls(K=d["K"])
        m.transition = np.array(d["transition"])
        m.means = np.array(d["means"])
        m.variances = np.array(d["variances"])
        m.state_map = d["state_map"]
        if "log_prior" in d:
            m.log_prior = np.array(d["log_prior"])
        return m


