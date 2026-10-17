"""Pre-registered controls for the first research notebooks.

Changing a value here changes the experiment configuration digest. It does
not by itself change an experiment decision.
"""

H1_CONFIG = {
    "experiment_id": "EXP-H01",
    "symbol": "BTCUSDT",
    "venue": "binance",
    "timeframe": "1m",
    "bucket": "trading-data-centheos",
    "profile": "trading",
    "region": "ap-southeast-2",
    "horizons_minutes": [5, 15, 60],
    "sealed_months": 12,
    "train_months": 24,
    "validation_months": 6,
    "step_months": 6,
    "var_alpha": 0.05,
    "student_t_estimator": "method_of_moments",
}

H2_CONFIG = {
    "experiment_id": "EXP-H02",
    "parent_experiment_id": "EXP-H01",
    "symbol": "BTCUSDT",
    "venue": "binance",
    "timeframe": "1m",
    "bucket": "trading-data-centheos",
    "profile": "trading",
    "region": "ap-southeast-2",
    "horizons_minutes": [5, 15, 60],
    "sealed_months": 12,
    "train_months": 24,
    "validation_months": 6,
    "step_months": 6,
    "var_alpha": 0.05,
    "states": 2,
    "state_model": "two_state_gaussian_hmm",
    "probability": "filtered",
    "em_iterations": 25,
    "em_tolerance": 1e-6,
    "forecast": "density",
    "first_passage": "not_scored",
}

BASELINE_CONFIG = {
    "notebook": "notebooks/research/01_baselines.ipynb",
    "symbol": "BTCUSDT",
    "venue": "binance",
    "timeframe": "1m",
    "lookback_minutes": 60,
    "target_annual_volatility": 0.50,
    "max_gross": 1.0,
    "cost_bps": 5.0,
    "sealed_months": 12,
}
