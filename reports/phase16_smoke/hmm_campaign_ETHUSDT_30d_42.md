# HMM Campaign Pair — ETHUSDT / 30d

> Phase 16 — HMM A/B Campaign at Scale (per-(symbol, window) row).

## Campaign metadata

- **Symbol:** `ETHUSDT`
- **Window:** `30d`
- **Seed:** `42`
- **Config snapshot hash (SHA-256):** `8a3a9078c20db6f56011558626ec1f658af0a02bf975b75d336e4e1231ac5448`
- **Timestamp:** 2026-05-12T04:09:16Z

## Phase 16 verdict (this pair)

| Field | Value |
|---|---|
| `winner` | **hmm** |
| `win_ratio` | 0.7500 |
| `median_sharpe_delta` | +0.250000 |
| `median_cagr_delta` | +0.500000 |
| `max_drawdown` (HMM run) | 0.050000 |
| `trade_count` (HMM run) | 6 |

---
# HMM vs. Rule-Based A/B — ETHUSDT

> Phase 7V validation harness (closes the validation gap documented in `implementation_plan.md` Phase 7).

## Run metadata

- **Symbol:** `ETHUSDT`
- **Window:** 1744408000000 → 1747000000000 (epoch ms)
- **Captured at:** 2026-05-12T04:09:16Z
- **Observations captured (V1 evidence):** 60
- **K candidates evaluated:** [3, 4, 5, 6]
- **K chosen (BIC argmin):** 3
- **HMM BIC:** -1048.0671
- **HMM log-likelihood:** 616.1563
- **Trained model JSON:** `reports/phase16_smoke/models/hmm_ETHUSDT_campaign_ETHUSDT_30d_20260512T040916.json`

## State map (HMM hidden state → RippleState int)

- hidden state 0 → RippleState int 2
- hidden state 1 → RippleState int 2
- hidden state 2 → RippleState int 3

## Metric comparison

| Metric | Rule-based | HMM | Δ (HMM-rule) | Δ % | Direction | Winner |
|---|---:|---:|---:|---:|---|---|
| `pnl` | 1.000000 | 2.000000 | +1.000000 | +100.00% | higher better | **hmm** |
| `max_drawdown` | 0.050000 | 0.050000 | +0.000000 | +0.00% | lower better | **tie** |
| `num_trades` | 5 | 6 | +1 | +20.00% | informational | **n/a** |
| `sharpe_ratio` | 0.500000 | 0.750000 | +0.250000 | +50.00% | higher better | **hmm** |
| `cagr` | 1.000000 | 1.500000 | +0.500000 | +50.00% | higher better | **hmm** |

## Decision counts and state distributions

- Rule-based ripple decisions captured: 60
- HMM ripple decisions captured: 60

**Triggering-state histogram (rule-based run):** 2:30, 3:30

**Triggering-state histogram (HMM run):** 2:30, 3:30

## Verdict

**HMM improves at least one metric without regression.**
