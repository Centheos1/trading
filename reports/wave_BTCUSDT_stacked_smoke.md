# Wave Backtest Report
**Symbol:** BTCUSDT | **Timeframe:** 1h | **Mode:** STACKED | **Exchange:** binance  
**Period:** 2022-12-31 05:00:00 → 2024-08-13 21:00:00 | **Bars:** 14,201

## Returns & Growth
| Metric | Value |
|---|---|
| Total Return | 9.48% |
| CAGR | 5.74% |
| Initial Capital | $10,000.00 |
| Final Equity | $10,947.53 |

## Risk
| Metric | Value |
|---|---|
| Ann. Volatility | 12.55% |
| Ann. Downside Vol | 7.65% |
| Max Drawdown | -8.12% |
| Max DD Duration (bars) | 4,023 |
| VaR 95% | 0.00% |
| CVaR 95% (ES) | 0.01% |
| VaR 99% | 0.16% |
| CVaR 99% (ES) | 0.60% |
| Ulcer Index | 0.0368 |
| Skewness | 7.3148 |
| Excess Kurtosis | 416.8344 |

## Risk-Adjusted
| Metric | Value |
|---|---|
| Sharpe Ratio | 0.4656 |
| Sortino Ratio | 0.7637 |
| Calmar Ratio | 0.7077 |

## Activity & Trades
| Metric | Value |
|---|---|
| Num Trades | 185 |
| Exposure % | 3.2% |
| Win Rate | 36.36% |
| Profit Factor | 1.4178 |
| Avg Win | $178.10 |
| Avg Loss | $-71.78 |
| Expectancy / Bar | 0.0007% |

## Per-Regime Breakdown
| Regime | Time % | PnL Contribution | Sharpe | N Bars |
|---|---|---|---|---|
| BREAKOUT | 0.0% | $0.00 | 0.00 | 2 |
| MEAN_REVERSION | 34.0% | $232.50 | 0.31 | 4,835 |
| BREAKDOWN | 62.6% | $-152.96 | -4.16 | 8,894 |
| NEUTRAL | 3.3% | $868.00 | 3.04 | 470 |

## Monthly Returns
Best month: 3.06%  |  Worst month: -2.33%  |  Positive months: 50.0%

| Month | Return |
|---|---|
| 2023-01 | 1.56% |
| 2023-02 | 1.02% |
| 2023-03 | 2.41% |
| 2023-04 | 1.81% |
| 2023-05 | -0.51% |
| 2023-06 | -2.33% |
| 2023-07 | -0.91% |
| 2023-08 | 0.33% |
| 2023-09 | 1.50% |
| 2023-10 | 1.68% |
| 2023-11 | -0.30% |
| 2023-12 | 2.57% |
| 2024-01 | 3.06% |
| 2024-02 | -0.60% |
| 2024-03 | 0.00% |
| 2024-04 | -2.17% |
| 2024-05 | 2.34% |
| 2024-06 | -0.65% |
| 2024-07 | -0.18% |
| 2024-08 | -1.33% |

## Wave Regime Accuracy

### Overall Hit Rate & IC
| Horizon (bars) | Hit Rate | IC |
|---|---|---|
| 1 | 49.21% | 0.0020 |
| 2 | 49.12% | 0.0002 |
| 4 | 49.39% | -0.0015 |
| 8 | 48.84% | -0.0052 |
| 12 | 48.20% | -0.0149 |
| 24 | 46.83% | -0.0338 |

### Per-Regime Forward Return

**Horizon h=1**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4835 | 0.011% | 0.009% | 0.00% |
| BREAKOUT | 2 | 0.025% | 0.025% | 50.00% |
| BREAKDOWN | 8893 | 0.008% | 0.007% | 49.21% |
| NEUTRAL | 470 | 0.001% | 0.007% | 0.00% |

**Horizon h=2**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4835 | 0.020% | 0.017% | 0.00% |
| BREAKOUT | 2 | -0.037% | -0.037% | 50.00% |
| BREAKDOWN | 8892 | 0.018% | 0.008% | 49.12% |
| NEUTRAL | 470 | 0.007% | -0.011% | 0.00% |

**Horizon h=4**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4835 | 0.029% | 0.023% | 0.00% |
| BREAKOUT | 2 | 0.071% | 0.071% | 100.00% |
| BREAKDOWN | 8890 | 0.038% | 0.009% | 49.38% |
| NEUTRAL | 470 | 0.092% | -0.016% | 0.00% |

**Horizon h=8**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4833 | 0.044% | 0.040% | 0.00% |
| BREAKOUT | 2 | 0.195% | 0.195% | 100.00% |
| BREAKDOWN | 8888 | 0.079% | 0.018% | 48.83% |
| NEUTRAL | 470 | 0.266% | 0.055% | 0.00% |

**Horizon h=12**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4829 | 0.051% | 0.040% | 0.00% |
| BREAKOUT | 2 | 0.090% | 0.090% | 50.00% |
| BREAKDOWN | 8888 | 0.129% | 0.040% | 48.20% |
| NEUTRAL | 470 | 0.319% | 0.020% | 0.00% |

**Horizon h=24**
| Regime | N | Mean Fwd Return | Median Fwd Return | Hit Rate |
|---|---|---|---|---|
| MEAN_REVERSION | 4829 | 0.071% | 0.054% | 0.00% |
| BREAKOUT | 2 | -0.155% | -0.155% | 0.00% |
| BREAKDOWN | 8877 | 0.283% | 0.117% | 46.84% |
| NEUTRAL | 469 | 0.472% | 0.250% | 0.00% |

### Regime Transition Matrix (probabilities)
| From \ To | MEAN_REVERSION | BREAKOUT | BREAKDOWN | NEUTRAL |
|---|---|---|---|---|
| MEAN_REVERSION | 0.949 | 0.000 | 0.051 | 0.000 |
| BREAKOUT | 0.000 | 0.500 | 0.000 | 0.500 |
| BREAKDOWN | 0.000 | 0.000 | 0.970 | 0.030 |
| NEUTRAL | 0.523 | 0.000 | 0.040 | 0.436 |

### Regime Stability (mean run length, bars)
| Regime | Mean Run (bars) |
|---|---|
| MEAN_REVERSION | 19.6 |
| BREAKOUT | 2.0 |
| BREAKDOWN | 33.7 |
| NEUTRAL | 1.8 |

## Parameters
| Parameter | Value |
|---|---|
| symbol | BTCUSDT |
| exchange | binance |
| timeframe | 1h |
| mode | stacked |
| sizing_mode | base |
| max_position_base | 1.0 |
| max_position_usd | 10000.0 |
| initial_capital | 10000.0 |
| leverage | 1.0 |
| taker_fee_bps | 4.0 |
| eta_window | 30 |
| vwap_window | 24 |
| disp_window | 30 |
| ar_window | 60 |
| disp_scale | 0.05 |
| eta_mr_threshold | 0.3 |
| eta_bo_threshold | 0.7 |
| eta_neutral_threshold | 0.5 |
| dispersion_threshold | 0.5 |
| dispersion_critical | 1.2 |
| ar_critical | 0.85 |
| ar_recover | 0.7 |
| reduced_size_fraction | 0.5 |
| update_interval_ms | 0 |
| accuracy_horizons | [1, 2, 4, 8, 12, 24] |
| bar_seconds | 3600.0 |