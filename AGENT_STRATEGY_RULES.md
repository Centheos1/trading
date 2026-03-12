# Agent Strategy Rules

## 1. Purpose

This document is the **operational instruction manual** for coding agents working on the Tide / Wave / Ripple strategy codebase. It summarizes the critical implementation rules, guardrails, and conventions that must be followed at all times.

This document does **not** replace `strategy.md`. For full mathematical definitions, state machine specifications, and design rationale, consult the source-of-truth hierarchy below.

---

## 2. Source-of-Truth Order

When in doubt, consult these documents in priority order:

| Priority | Document | Role |
|---|---|---|
| 1 | `strategy.md` | Canonical strategy design, math, contracts |
| 2 | `implementation_plan.md` | Build plan, phases, module mapping |
| 3 | Tests | Executable specification |
| 4 | This document | Operational rules and guardrails |

**Rule:** If code behavior contradicts `strategy.md`, the code is wrong. If `implementation_plan.md` contradicts `strategy.md`, the plan must be updated first. If a test contradicts `strategy.md`, the test is wrong (update the test, not the strategy, unless the strategy has a bug).

---

## 3. Mandatory Development Rules

### 3.1 Change Discipline

- **Keep changes small and narrow.** One logical unit per commit.
- **Keep the system compile-safe.** Every commit must compile and pass existing tests.
- **Show only changed files** when making implementation steps.
- **Do not refactor unrelated code** in the same change as a behavioral modification.
- **Do not introduce dead code paths** without clear justification and a TODO for cleanup.

### 3.2 Correctness First

- **Correctness before performance.** Get behavior right, then optimize.
- **Profile before optimizing.** Do not optimize speculatively.
- **Performance optimizations must preserve correctness and interfaces** unless explicitly justified and documented.

### 3.3 Documentation

- **Update `strategy.md`** if any strategy assumption, interface, or contract changes.
- **Update `implementation_plan.md`** if any phase scope, dependency, or acceptance criteria changes.
- **Update this document** if any operational rule changes.
- **Never let code drift from docs silently.**

### 3.4 Avoid Overengineering

- **Do not build abstractions ahead of need.** If only one implementation exists, a concrete class is fine.
- **Do not add configuration options unless they will be used in V1.** Configurable does not mean good.
- **Do not add layers of indirection** unless they solve a concrete problem.

---

## 4. Mandatory Testing Rules

### 4.1 Test Requirements

- **Every meaningful behavior change requires a test.** "Meaningful" means: any change that alters outputs given the same inputs.
- **Every state machine transition must have a test.**
- **Every exit type must have a test.**
- **Every feature computation must have a test** with known inputs and expected outputs.
- **Replay determinism tests must pass** after every behavioral change.

### 4.2 Test Categories

| Category | Required from | Description |
|---|---|---|
| Unit tests | Phase 1 | Individual function/class behavior |
| Integration tests | Phase 2 | End-to-end event → decision |
| Replay determinism | Phase 2 | Same events + config → same outputs |
| Boundary tests | Phase 2 | Edge cases: empty book, zero volume, max position |
| Backtest validation | Phase 3 | Strategy produces reasonable results on historical data |
| Performance benchmarks | Phase 6 | Latency and throughput targets met |

### 4.3 Property-Based Tests

For mathematical computations, add property invariants (see strategy.md §20.6):
- $ \text{ES} \geq \text{VaR} \geq 0 $
- $ Q_{\text{final}} \in [0, Q_{\max}] $
- $ I_t \in [-1, 1] $
- $ \text{hold} + \text{break} = 1 $
- Lifecycle FSM has no deadlocks

### 4.4 Adversarial Tests

Test with malformed inputs (see strategy.md §20.7):
- NaN prices, negative quantities, zero timestamps
- Empty / single-level / crossed order books
- Duplicate trade IDs, out-of-order events
- Timestamp gaps > 60s followed by bursts

### 4.5 Test Discipline

- Run the full test suite before submitting any change.
- If a test fails, fix it before moving on. Do not disable tests.
- If a test is wrong (tests incorrect behavior), fix the test and document why.

---

## 5. Tide / Wave / Ripple Boundary Rules

These rules are **non-negotiable**. They define the architectural invariants of the system.

### 5.1 Tide Rules

- **Tide does NOT directly trigger trades.** It produces bias, risk budgets, and sizing throttles.
- **Tide does NOT depend on L2 data.** It uses L1 prices and macro indicators.
- **Tide owns top-down risk allocation.** It sets ES budgets, max positions, and risk multipliers.
- **Tide updates at minute-scale cadence.** It is the slowest layer.

### 5.2 Wave Rules

- **Wave does NOT directly trigger trades.** It produces regime labels and permissions.
- **Wave is a filter / context / permission layer for Ripple.** It modulates, not decides.
- **Wave does NOT depend on L2 data.** It uses L1 prices and cross-asset features.
- **Wave updates at 5-second to minute-scale cadence.**

### 5.3 Ripple Rules

- **Ripple IS the execution layer.** It is the only layer that triggers trades.
- **Ripple depends on Binance L2 data, trades, CVD, Volume Profile, and the liquidity map.**
- **Ripple must respect Wave permissions.** If Wave disables an archetype, Ripple must not enter it.
- **Ripple must respect Tide risk budgets.** No position may exceed the granted budget.
- **Ripple updates per-event (trade, depth update).** It is the fastest layer.

### 5.4 Information Flow

- **Downward:** Tide → Wave → Ripple (allocation, permission).
- **Upward:** Ripple → Tide (consumed risk, position state).
- **No layer may override constraints set by a layer above it.**

### 5.5 V1 Operating Constraints

- **Single symbol at a time.** All state is per-symbol. No shared global mutable state between symbols.
- **At most one concurrent trade per symbol.** A new SETUP cannot begin while another trade is in any active or cooldown state.
- **Event-time only.** No wall-clock time in any decision or state transition logic.
- **`double` (float64) everywhere.** Never use `float` (float32) for prices, quantities, or features on the hot path.
- **Default leverage = 1×.** Leverage is a configuration parameter, not hardcoded.
- **Fees are explicit.** Maker and taker fees are configuration parameters; PnL calculations must include them.

---

## 6. Feature Schema Rules

### 6.1 Naming

- All features use **snake_case**.
- All features are prefixed with their **namespace**: `tide.`, `wave.`, `ripple.`, `liquidity_map.`, `risk.`, `trade.`.
- Example: `ripple.imbalance`, `wave.trend_efficiency`, `tide.risk_multiplier`.

### 6.2 Adding Features

1. Define the feature in `strategy.md` §16 first (name, type, cadence, category).
2. Implement in the appropriate module.
3. Add a unit test with known input → expected output.
4. Update the feature schema registry if one exists.

### 6.3 Changing Features

1. Update `strategy.md` §16 first.
2. Audit all consumers of the changed feature.
3. Update implementation and tests.
4. If the change breaks backward compatibility with stored data, document a migration path.

### 6.4 Feature Categories

| Category | Meaning | Example |
|---|---|---|
| Raw | Direct observation | `tide.funding_rate` |
| Derived | Computed from raw data | `ripple.imbalance` |
| Model output | Produced by a model or state machine | `wave.regime` |
| State | Part of a managed state object | `trade.state` |

---

## 7. Deterministic V1 Rules

### 7.1 Determinism Requirements

- **All decision logic must use event timestamps, not wall-clock time.**
- **No unseeded random number generation in decision logic.**
- **No floating-point non-determinism.** Use consistent rounding where needed.
- **All state that affects decisions must be serializable** for snapshot/restore.

### 7.2 Probabilistic / ML Extensions

- **HMM, ML, or other probabilistic methods must NOT be introduced before deterministic baselines are complete** (Phase 7 per `implementation_plan.md`), unless explicitly requested by the project owner.
- **When a probabilistic method is added, the deterministic baseline must remain functional and testable.**
- **Probabilistic components must be opt-in behind a configuration flag.**

### 7.3 Replay Contract

For a given sequence of events and a given configuration:
- The strategy MUST produce identical outputs every time.
- This applies to all features, state transitions, trade decisions, and fills (in paper mode).

---

## 8. Documentation Update Rules

### 8.1 When to Update

| Trigger | What to update |
|---|---|
| New feature added | `strategy.md` §16 + implementation code + tests |
| Schema change | `strategy.md` §17 + C++ structs + Python dataclasses + bindings + tests |
| New state machine state or transition | `strategy.md` §8/§9/§12 + code + tests |
| Phase completed | `implementation_plan.md` status |
| New operational rule | This document |
| Interface change | `strategy.md` + `implementation_plan.md` + consuming code + tests |

### 8.2 Documentation Style

- Use the same terminology as `strategy.md`. Do not invent synonyms.
- Use the same notation as `strategy.md` §6.
- Keep mathematical precision. Do not hand-wave.

---

## 9. Performance Rules

### 9.1 Hot Path

- **No heap allocation** on the per-event C++ hot path. Use pre-allocated buffers.
- **No string operations** on the hot path.
- **No Python callbacks** on the per-event C++ hot path.
- **Bounded data structures** with explicit eviction. No unbounded growth.
- **Use `double` (not `float`)** for all prices, quantities, and feature values on the hot path.

### 9.1.1 Bounded Data Structure Limits

Every rolling buffer, history, or map must have an explicit maximum size (see strategy.md §21.1 for defaults). When adding a new data structure, you must:
1. Declare its `maxlen` or capacity.
2. Specify its eviction policy (FIFO, LRU, or lowest-score).
3. Document what happens when capacity is exceeded (silent drop is **not** acceptable — count drops).
4. Add it to the memory constraints table in strategy.md §21.1.

### 9.2 Latency Targets

| Operation | Target |
|---|---|
| Per-event feature update (C++) | < 10 µs |
| State machine transition (C++) | < 1 µs |
| Full Ripple tick-to-decision (C++) | < 100 µs |
| Risk check (C++) | < 1 µs |
| Wave update (Python, 5s cadence) | < 1 ms |
| Tide update (Python, 60s cadence) | < 10 ms |
| UI render cycle | < 50 ms |

### 9.3 Performance Testing

- Add benchmarks for new hot-path code.
- Profile before making performance claims.
- Any regression > 20% on key latency metrics must be investigated and resolved.

---

## 10. Safe Strategy Evolution Rules

### 10.1 Adding a New Trade Archetype

1. Define it in `strategy.md` §11 (setup, confirmation, entry, target, stop).
2. Add it to the permissions matrix in `strategy.md` §18.
3. Implement in `TriggerDecisionEngine`.
4. Wire into `TradeLifecycleEngine`.
5. Add unit and integration tests.
6. Verify replay determinism.

### 10.2 Adding a New Exit Type

1. Define it in `strategy.md` §13.
2. Assign a priority.
3. Implement in `TradeLifecycleEngine`.
4. Add tests.
5. Verify existing exits still work.

### 10.3 Changing Risk Logic

1. Update `strategy.md` §15.
2. Implement in `RiskEngine`.
3. Verify all position sizing formulas.
4. Run backtest to validate no unintended position sizing changes.

### 10.4 Changing the Permissions Matrix

1. Update `strategy.md` §18.
2. Update `WaveEngine` or the lookup table.
3. Test that permissions are enforced.
4. Run backtest to validate impact.

### 10.5 Upgrading from Rule-Based to Probabilistic

1. Keep the rule-based implementation as the default fallback.
2. Add the probabilistic implementation behind a configuration flag.
3. Test that both paths produce valid outputs.
4. Compare backtest metrics.
5. Do not remove the rule-based path until the probabilistic path is proven.

---

## 11. Common Failure Modes to Avoid

### 11.1 Architectural Failures

| Failure Mode | Description | Prevention |
|---|---|---|
| Layer violation | Tide triggers a trade, or Wave depends on L2 data | Review against §5 boundary rules |
| Permission bypass | Ripple ignores Wave permissions | Enforce check in `TriggerDecisionEngine` |
| Budget override | Ripple exceeds Tide risk budget | Enforce check in `RiskEngine` on order path |
| Non-deterministic replay | Different outputs for same inputs | Replay tests after every behavioral change |

### 11.2 Implementation Failures

| Failure Mode | Description | Prevention |
|---|---|---|
| Schema drift | C++ and Python schemas diverge | Single-update process, binding tests |
| Unbounded buffer | Rolling window grows without limit | Explicit `maxlen` or eviction on all buffers |
| Hot-path allocation | Memory allocation per event | Pre-allocate, profile |
| Silent exception | Error swallowed, pipeline stalls | Per-operation try/catch with logging |
| Phase skipping | Implementing Phase 7 before Phase 2 complete | Follow phase order in `implementation_plan.md` |
| Test gap | Behavior changes without test updates | Mandatory test with every behavioral change |
| Doc rot | Code changes, docs don't | Update docs with every interface/behavior change |

### 11.3 Strategy Failures

| Failure Mode | Description | Prevention |
|---|---|---|
| Naive S/R entries | Using support/resistance as entry signals | S/R in Ripple is for exits and destination modeling |
| Scale-in before confirmation | Adding to a losing position | Scale-in only in `EXPANSION` state |
| Overcomplicated V1 | Too many features, too many states | Stick to V1 boundaries in `strategy.md` §22 |
| Multiple concurrent trades in V1 | State machine complexity, risk accounting | V1 enforces max 1 trade per symbol (§5.5) |
| Using `float` instead of `double` | Precision loss, non-deterministic replay | All hot-path numerics must be `double` (§9.1) |
| Feature leakage | Using future information in backtest | Strict causal features, lookback windows |
| Overfitting | Over-optimized on historical data | Out-of-sample validation, parameter stability checks |

---

## 12. Definition of Done for a Change

A change is **done** when all of the following are true:

| Criterion | Check |
|---|---|
| Compiles | `make` / `cmake --build` succeeds |
| Tests pass | All existing tests pass; new tests added for new behavior |
| Replay deterministic | Same events + config → same outputs |
| Linter clean | No new linter warnings in changed files |
| Docs updated | `strategy.md` / `implementation_plan.md` / this file updated if needed |
| Performance acceptable | No regression > 20% on key metrics |
| Change is narrow | One logical unit; no unrelated modifications |
| Review-ready | Changed files clearly identified; rationale documented |

---

## 13. Quick Reference: All Enum Values

Canonical enum names from `strategy.md` §29. C++ and Python must use these exact identifiers.

```
TideBias:       LONG | SHORT | NEUTRAL
VolRegime:      LOW | NORMAL | HIGH | CRISIS
WaveRegime:     MEAN_REVERSION | BREAKOUT | BREAKDOWN | NEUTRAL
PermissionLevel: FULL | REDUCED | DISABLED
LiquidityState: STABLE | ABSORPTION | EXHAUSTION | WITHDRAWAL | REFILL
WallSide:       BID | ASK
LevelType:      WALL_BID | WALL_ASK | HVN | LVN | POC | VWAP | VOID_BOUNDARY
TradeArchetype: BOUNCE | BREAKOUT
TradeSide:      LONG | SHORT
LifecycleState: SETUP | ENTRY | CONFIRMATION | EXPANSION | MATURATION | EXIT | COOLDOWN | CANCELLED
ExitType:       INVALIDATION | TARGET | EXHAUSTION | TIME | RISK_BUDGET
OrderSide:      BUY | SELL
OrderType:      MARKET | LIMIT
IntentType:     ENTRY | SCALE_IN | SCALE_OUT | EXIT
Urgency:        IMMEDIATE | NORMAL
ExitReason:     INVALIDATION | TARGET | EXHAUSTION | TIME | RISK_BUDGET | NONE
EventType:      TRADE | DEPTH_UPDATE | DEPTH_SNAPSHOT
```

---

## 14. Quick Reference: Pre-Phase Defaults

Before Tide/Wave are built, use these hardcoded defaults (see `strategy.md` §7.5):

```
DefaultTideSnapshot:
    bias             = NEUTRAL
    risk_multiplier  = 1.0
    max_position_usd = <from config>
    es_budget        = <from config>
    vol_regime       = NORMAL

DefaultWaveSnapshot:
    regime           = NEUTRAL
    permissions      = all FULL
    trend_efficiency = 0.5
```

Define as `static constexpr` in C++ `Schemas.h` and as constants in Python `schemas.py`.

---

## 15. Quick Reference: Detection Algorithm Cross-References

| Algorithm | strategy.md Section | Inputs | Output |
|---|---|---|---|
| Wall detection | §9.6.1 | OrderBook levels, config | `vector<Wall>` with quality scores |
| Cancellation rate | §9.6.2 | Wall depth changes, trade volume | `wall.cancellation_rate` |
| Evidence scoring | §9.7.1 | Ripple features, walls | 4 evidence scores [0,1] |
| State inference | §9.7.2 | Evidence scores, config thresholds | `LiquidityState` enum |
| Bounce detection | §11.1.1 | Walls, ripple state, lmap | `BounceSetup` or None |
| Bounce confirmation | §11.1.1 | Setup, ripple state, config | CONFIRMED / WAIT / CANCEL |
| Breakout detection | §11.2.1 | Walls, ripple state, lmap | `BreakoutSetup` or None |
| Breakout confirmation | §11.2.1 | Setup, ripple state, config | CONFIRMED / WAIT / CANCEL |
| Invalidation exit | §13.3.1 | Trade, ripple state | bool |
| Target exit | §13.3.2 | Trade, ripple state, lmap | bool |
| Exhaustion exit | §13.3.3 | Trade, ripple state, config | bool |
| Time exit | §13.3.4 | Trade, timestamp, config | bool |
| Risk-budget exit | §13.3.5 | Risk snapshot, config | bool |
| Scale-in trigger | §14.1.1 | Trade, ripple, risk, config | bool |
| Scale-out plan | §14.2.1 | Trade, lmap, config | `vector<ScaleOutLevel>` |
| Trailing stop | §14.3 | Trade, ripple, config | Updated stop\_price |
| Risk multiplier | §7.4.7 | Vol regime, LSI, config | float64 ∈ [0,1] |
| Position sizing | §14.4 | Risk budget, entry/stop prices, vol | `Q_final` |

**Rule:** Implement each algorithm exactly as specified in the referenced section. Do not invent alternative detection logic.

---

## 16. Quick Reference: Layer Responsibilities

```
┌─────────────────────────────────────────────────────────┐
│  TIDE (slowest)                                         │
│  ● Macro bias        ● ES budget       ● Risk mult     │
│  ● Position caps     ● Vol regime      ● Capital alloc  │
│  ● Does NOT trigger trades                              │
├─────────────────────────────────────────────────────────┤
│  WAVE (middle)                                          │
│  ● Regime label      ● Trend eff       ● Dispersion    │
│  ● Absorption ratio  ● Permissions     ● Structure ctx  │
│  ● Does NOT trigger trades                              │
│  ● Does NOT use L2 data                                 │
├─────────────────────────────────────────────────────────┤
│  RIPPLE (fastest)                                       │
│  ● Walls             ● Liquidity map   ● CVD / VP      │
│  ● Microprice        ● OFI / imbalance ● LSI           │
│  ● Bounce / breakout archetypes                         │
│  ● Trade lifecycle   ● Exit taxonomy   ● Scaling        │
│  ● TRIGGERS trades                                      │
│  ● RESPECTS Wave permissions and Tide budgets           │
└─────────────────────────────────────────────────────────┘
```

---

## 17. Quick Reference: Support / Resistance Usage

| S/R Type | Layer | Primary Usage |
|---|---|---|
| Structural (session H/L, prior POC) | Wave | Context, distance-to-structure |
| Order book walls | Ripple | Entry setups (bounce/breakout) |
| Statistical anchors (VWAP, rolling mean) | Wave / Ripple | Fair-value reference, targets |
| Volume profile levels (HVN, LVN) | Ripple | **Destination modeling, exits** |

**Key rule:** Support/resistance in Ripple is used **primarily for exits and destination modeling**, not as naive entry signals.

---

## 18. Quick Reference: Exit Types

**Evaluation order (first match fires):** Risk-budget → Invalidation → Time → Target → Exhaustion

| Exit | Trigger | Priority | Order Type | Quantity | Detection §ref |
|---|---|---|---|---|---|
| Risk-budget | consumed\_es ≥ 90% budget OR risk\_mult = 0 | Highest | MARKET | 100% | §13.3.5 |
| Invalidation | Stop hit (archetype-specific) | Highest | MARKET | 100% | §13.3.1 |
| Time | hold\_time > max\_hold\_time\_ms | Normal | MARKET | 100% | §13.3.4 |
| Target | Price crosses target level | Normal | LIMIT | Scale-out fraction | §13.3.2 |
| Exhaustion | CVD fading + rate declining + impact rising | Normal | LIMIT | 100% | §13.3.3 |

---

## 19. Quick Reference: Scaling Rules

| Rule | Detail | §ref |
|---|---|---|
| Scale-in timing | Only in `EXPANSION` state | §14.1 |
| Scale-in trigger | Favorable move > `scale_in_threshold_sigma` × σ AND CVD momentum intact AND risk budget allows | §14.1.1 |
| Scale-in max (V1) | 1 add (2 tranches total) | §14.1 |
| Scale-in stop update | Move combined stop to entry price (breakeven) after fill | §14.1.1 |
| Max concurrent trades (V1) | 1 per symbol | §12.3 |
| Scale-out targets | Top destinations from liquidity map ranked by `dest_score` | §14.2.1 |
| Scale-out fractions | Configurable (default: [0.33, 0.33, 0.34]) | §27.3 |
| Post 1st scale-out | Move stop to entry price (breakeven) | §14.2.1 |
| Post 2nd+ scale-out | Move stop to prior target level | §14.2.1 |
| After all scale-outs | Trailing stop at `trailing_stop_sigma` × σ | §14.3 |
| Trailing stop direction | Only tightens, never loosens | §14.3 |

---

## 20. Configuration Groups

Agents should organize configuration parameters into these groups:

| Group | Examples |
|---|---|
| `tide.*` | `risk_multiplier_default`, `es_budget_global`, `vol_regime_thresholds` |
| `wave.*` | `trend_efficiency_thresholds`, `dispersion_thresholds`, `ar_thresholds`, `update_interval_ms` |
| `ripple.*` | `wall_min_quality`, `confirmation_window_ms`, `exhaustion_cvd_slope_thresh`, `proximity_sigma`, `bounce_microprice_shift_ticks`, `breakout_depth_fail_ratio`, `breakout_cvd_slope_thresh`, `max_scale_ins`, `trailing_stop_sigma`, `state_activation_threshold`, `state_margin` |
| `risk.*` | `es_confidence_level`, `max_position_usd`, `sizing_target_risk_usd`, `sigma_target`, `maker_fee_bps`, `taker_fee_bps`, `leverage` |
| `execution.*` | `bounce_order_type`, `breakout_order_type`, `slippage_tolerance_bps`, `cooldown_ms` |
| `testing.*` | `replay_tolerance`, `benchmark_latency_target_us` |

**Rule:** Do not scatter magic constants. All tunable parameters belong in configuration with documented defaults and valid ranges.
