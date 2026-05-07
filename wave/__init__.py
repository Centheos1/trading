"""Wave layer — meso-scale regime classification and structural context (strategy.md §8)."""

from .wave_engine import WaveEngine
from .wave_features import WaveFeatureParams, build_wave_feature_frame
from .wave_backtest import WaveBacktester, WaveBacktestResult, WaveStrategyParams, SizingMode
from .wave_accuracy import RegimeAccuracyReport, compute_regime_accuracy_report
from .wave_metrics import WavePerformanceReport, compute_wave_report
from .wave_report import render_text, render_markdown, write_report
from .wave_optimiser import WaveOptimiser, WaveOptimiserConfig

# wave_cli is intentionally NOT imported here.  Eager import of the CLI into
# __init__.py causes a runpy conflict when running `python -m wave.wave_cli`
# because Python's standard-library 'wave' module shares the same top-level
# name and __init__.py is executed before runpy can set __name__ = '__main__'.
# Import wave_cli explicitly where needed:  from wave.wave_cli import run_cli

__all__ = [
    "WaveEngine",
    "WaveFeatureParams",
    "build_wave_feature_frame",
    "WaveBacktester",
    "WaveBacktestResult",
    "WaveStrategyParams",
    "SizingMode",
    "RegimeAccuracyReport",
    "compute_regime_accuracy_report",
    "WavePerformanceReport",
    "compute_wave_report",
    "render_text",
    "render_markdown",
    "write_report",
    "WaveOptimiser",
    "WaveOptimiserConfig",
]
