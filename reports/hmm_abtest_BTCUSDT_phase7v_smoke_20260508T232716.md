# HMM vs. Rule-Based A/B — BTCUSDT

> Phase 7V validation harness (closes the validation gap documented in `implementation_plan.md` Phase 7).

## Run metadata

- **Symbol:** `BTCUSDT`
- **Window:** 1772841600000 → 1773100799999 (epoch ms)
- **Captured at:** 2026-05-08T23:27:16Z
- **Observations captured (V1 evidence):** 13
- **K candidates evaluated:** [3, 4, 5]
- **K chosen (BIC argmin):** 3
- **HMM BIC:** -31.0912
- **HMM log-likelihood:** 73.2570
- **Trained model JSON:** `/Users/clintsellen/Documents/Trading/app/backtest/models/hmm_BTCUSDT_phase7v_smoke_20260508T232716.json`

## State map (HMM hidden state → RippleState int)

- hidden state 0 → RippleState int 1
- hidden state 1 → RippleState int 2
- hidden state 2 → RippleState int 3

## Metric comparison

| Metric | Rule-based | HMM | Δ (HMM-rule) | Δ % | Direction | Winner |
|---|---:|---:|---:|---:|---|---|
| `pnl` | 0.000000 | 3.336452 | +3.336452 | n/a | higher better | **hmm** |
| `max_drawdown` | 0.000000 | 0.000151 | +0.000151 | n/a | lower better | **score** |
| `num_trades` | 1 | 16 | +15 | +1500.00% | informational | **n/a** |
| `sharpe_ratio` | 0.289721 | 0.289721 | +0.000000 | +0.00% | higher better | **tie** |
| `cagr` | 5337.121322 | 5337.121322 | +0.000000 | +0.00% | higher better | **tie** |

## Decision counts and state distributions

- Rule-based ripple decisions captured: 13
- HMM ripple decisions captured: 9

**Triggering-state histogram (rule-based run):** 1:3, 2:3, 3:4, 5:1, 6:2

**Triggering-state histogram (HMM run):** 1:7, 2:2

## Verdict

**Mixed: HMM wins 1, rule-based wins 1, ties 2. Inspect per-metric deltas before promoting.**
