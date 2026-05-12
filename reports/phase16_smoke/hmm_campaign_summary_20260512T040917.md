# HMM A/B Campaign Summary — Phase 16

> Aggregate of all (symbol, window) pairs run in this campaign. The `CampaignVerdict` block at the bottom is the **hard evidence gate** for Phase 17 — paste it into `implementation_plan.md §7.2` to record the run.

## Campaign metadata

- **Timestamp:** 2026-05-12T04:09:17Z
- **Seed:** `42`
- **Config snapshot hash (SHA-256):** `8a3a9078c20db6f56011558626ec1f658af0a02bf975b75d336e4e1231ac5448`
- **Pair count:** 4
- **Symbols:** `BTCUSDT`, `ETHUSDT`
- **Windows:** `30d`, `60d`
- **Verdict thresholds:** `win_ratio ≥ 0.60` AND `median_sharpe_delta ≥ +0.10`

## Per-pair results

| Symbol | Window | `pnl` (HMM) | `max_drawdown` (HMM) | `sharpe` (HMM) | `cagr` (HMM) | `trade_count` (HMM) | `win_ratio` | `winner` |
|---|---|---:|---:|---:|---:|---:|---:|---|
| `BTCUSDT` | `30d` | +2.000000 | 0.050000 | +0.750000 | +1.500000 | 6 | 0.7500 | **hmm** |
| `BTCUSDT` | `60d` | +2.000000 | 0.050000 | +0.750000 | +1.500000 | 6 | 0.7500 | **hmm** |
| `ETHUSDT` | `30d` | +2.000000 | 0.050000 | +0.750000 | +1.500000 | 6 | 0.7500 | **hmm** |
| `ETHUSDT` | `60d` | +2.000000 | 0.050000 | +0.750000 | +1.500000 | 6 | 0.7500 | **hmm** |

## CampaignVerdict

```
Phase 16 CampaignVerdict (recorded):
  promote:              true
  win_ratio:            1.000000
  median_sharpe_delta:  +0.250000
  median_cagr_delta:    +0.500000
  max_drawdown_delta:   +0.000000
  trade_count_delta:    +1
  symbols:              ['BTCUSDT', 'ETHUSDT']
  windows:              ['30d', '60d']
  config_snapshot_hash: 8a3a9078c20db6f56011558626ec1f658af0a02bf975b75d336e4e1231ac5448
  recorded_by:          tools.hmm_abtest
  recorded_at:          2026-05-12T04:09:17Z
```

---

**Phase 17 hard gate:** Phase 17 work must not begin until `promote=True` is recorded in `implementation_plan.md §7.2`. If `promote=False`, the project owner must explicitly override in writing before any Phase 17 code is written.
