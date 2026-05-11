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

### 3.5 Integration ≠ Unit-Test-Complete `[lesson from 2026-05 V1 audit]`

A phase is **NOT done** when its unit tests pass. A phase is done when
the new capability is **invoked from at least one live runtime entry
point** (`ui/live_trading_session.py`, `execution/live_runner.py`, or
`main.py:<mode>`) AND that wiring is itself covered by a regression
test.

Failure mode: the 2026-05 V1 audit found that `RippleEngine::set_risk_budget`,
`set_wave_snapshot`, and `set_realized_vol` were unit-test-complete since
Phases 4 / 5 but had **never been called by any live entry point**. The
"COMPLETED" status on Phases 4, 5, 8, and 12 reflected unit-test
delivery, not end-to-end integration. The result was a backend with a
deterministic Tide → Wave → Ripple architecture and a frontend that
silently ran every live session against `DefaultTideSnapshot` and
`DefaultWaveSnapshot`.

**Operating rules:**

1. When closing a phase, add a "wired live?" row to the phase's
   acceptance criteria. The row passes only if a `grep` for the new
   API surface returns at least one production-code hit outside
   `tests/`.
2. Add a **wiring test** alongside the unit tests. A wiring test
   instantiates the runtime entry point with a stub engine and asserts
   the engine receives the expected setter calls at the expected
   cadence.
3. In the implementation plan, the §2.2 "Existing Capabilities" table
   has a `Wired live?` column. Update it honestly. `⚠️ Engine-only`
   is a permitted status — `✅` requires actual production wiring.
4. If a capability is intentionally engine-only (e.g. a research-only
   inference backend behind a flag), say so explicitly and link to the
   flag.

This is the single most expensive lesson the project has learned to
date. It is the root cause of `implementation_plan.md` §7.1 V1 Closure
Roadmap (Phase 14 series).

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

### 7.4 Live Execution Topology Contract `[V1 — Phase 14A — ENFORCED IN CODE 2026-05-09]`

The live execute path MUST consume `RippleDecision` intents, not raw `SignalEngine.Signal` events.

- `engine.set_ripple_callback(...)` is the **only** approved subscription point for the execution layer (paper or live).
- `engine.set_signal_callback(...)` is reserved for **observation / logging / recording** (e.g. `SessionRecorder`). It MUST NOT drive `ExecutionManager.on_signal` or any other order-routing entry point.
- Cooldowns, throttles, and gating in the execution layer MUST use `intent.timestamp` (event time, ms), never `time.time()` / `time.monotonic()` / `datetime.now()` (wall-clock).
- The Ripple lifecycle FSM, scaling logic, exit taxonomy, and `RiskEngine` are the source of truth for *whether* an order fires; the execution layer is the source of truth for *how* it fires (broker, sizing, retry).
- `ExecutionManager.on_signal` is preserved as a **deprecated shim** so the Phase 12 regression suite still pins the legacy surface, but it logs a one-time WARNING and is no longer wired by `live_runner.py`, `main.py:execute`, or `ui/main_window.py`.

**Rationale.** Routing the live path through raw signals bypasses
the entire Tide / Wave / Ripple architecture, the V1 §22.2 #4 exit
taxonomy contract, and the V1 §22.2 #12 risk-checks contract. The
2026-05 V1 audit found this exact gap on the live path; it is closed
by `implementation_plan.md` §7.1 Phase 14A (shipped 2026-05-09 with
23 new `test_execution_manager.py` tests, a new `test_live_runner.py`
suite of 13 tests, and a 419-test wider regression sweep). New code
MUST NOT re-introduce signal-driven routing on the live path.

**Enforcement check (CI-friendly — both must pass for any commit
that touches `execution/`, `ui/`, or `main.py`):**

```bash
# Gate 1: production code must NOT subscribe execution to signals.
# Expected output: empty (only doc references in implementation_plan /
# README / this file are matched against the broader filesystem).
grep -rn "engine\.set_signal_callback(.*on_signal" execution/ ui/ main.py

# Gate 2: any time.time() in execution_manager.py must be inside the
# deprecated on_signal / _execute_signal path (2 surviving hits at
# present). Decision logic on the canonical on_intent path is
# wall-clock-free; the new TestOnIntentEventTimeCooldown test class
# guards this invariant by patching time.time to raise.
grep -n "time\.time()" execution/execution_manager.py
```

### 7.5 Layered Strategy Wiring Contract `[V1 — Phase 14B — ENFORCED IN CODE 2026-05-11]`

Every live runtime entry point (`ui/live_trading_session.py`,
`execution/live_runner.py`, `main.py:execute`) MUST push the latest
Tide budget, Wave snapshot, and realized volatility into the C++
engine on every tick at the cadences specified in `strategy.md` §5.3
(Tide 60 s, Wave 5 s, RV 1 s).

The setters that MUST be called:
- `RippleEngine::set_risk_budget(es_budget, max_position_usd, risk_multiplier)` — Tide → Risk wiring.
- `RippleEngine::set_wave_snapshot(WaveSnapshot)` — Wave → permissions wiring.
- `RippleEngine::set_realized_vol(double)` — Vol → sizing wiring.
- `RippleEngine::set_inventory(InventorySnapshot)` — broker position → engine wiring (when inventory tracking is added in V2).

**Rationale.** Without these calls the engine runs against
`DefaultTideSnapshot::make()` (NEUTRAL, no budget) and
`DefaultWaveSnapshot::make()` (NEUTRAL, all permissions FULL) — which
silently disables the entire upper architecture. Unit tests for the
setters do NOT exempt a runtime path from calling them.

**Implementation reference (post-2026-05-11):** The translator
`execution.models.wave_snapshot_to_ofe(snap, ofe_module)` converts a
Python `schemas.WaveSnapshot` to an `ofe.WaveSnapshot`; the helper
`execution.models.compute_realized_vol_from_prices(prices)` produces
the RV scalar. Both are pure functions (no side effects, no
wall-clock reads) and are unit-tested in
`tests/test_layered_live_wiring.py`. The free functions
`execution.live_runner._layered_push_step(...)` and
`execution.live_runner._run_layered_push_loop(...)` are the canonical
push primitives — new live entry points MUST reuse them rather than
re-implement.

**Determinism note.** The push thread uses `Event.wait(timeout=...)`
for its loop cadence (wall-clock interval, default 1 s). This is
acceptable because the push thread does NOT make trading decisions —
it merely keeps the engine's Tide / Wave / RV state fresh. All
TRADING-decision logic (lifecycle FSM, cooldown gating, RiskEngine
checks) is still event-time, per §7.1. Do NOT route trading
decisions through this thread.

**Enforcement check (CI-friendly — all three MUST return ≥1
production hit outside `tests/`):**

```bash
grep -rn "set_risk_budget"   ui/ execution/ main.py
grep -rn "set_wave_snapshot" ui/ execution/ main.py
grep -rn "set_realized_vol"  ui/ execution/ main.py
```

### 7.6 Live Risk-Gate Contract `[V1 — Phase 14C — ENFORCED IN CODE 2026-05-12]`

Every Python live entry point that forwards a Ripple intent to a real
broker MUST first call
`execution.models.intent_risk_block_reason(intent, engine,
current_position_usd=..., ofe_module=ofe)` and short-circuit when the
helper returns a non-`None` reason string.

**Why.** The C++ `RippleEngine` applies its Wave permission gate and
`RiskEngine::check_new_order` gate *inside*
`on_trigger_decision` (RippleEngine.cpp:282–307) — but only to
suppress the **internal** lifecycle setup and any paper fill. The
`set_ripple_callback` channel still fires for every non-`NO_ACTION`
decision, so unless the Python side re-applies the gate, a real
broker would receive an order even when the engine itself decided
NOT to open a trade. That is exactly the V1 §22.2 #12 contract
violation Phase 14C closes.

**The gates that MUST be mirrored, in order:**
1. **Wave permission** — `wave_snap.permissions.size_fraction(arch, side) > 0` for the (`TradeArchetype`, `TradeSide`) implied by the intent's `action` field. A `DISABLED` permission must drop the intent.
2. **Tide CRISIS** — `risk.risk_multiplier > 0`. A zero multiplier means `compute_position_size` would return 0 in the C++ path.
3. **ES exhaustion** — when `risk.es_budget > 0`, require `risk.consumed_es < risk.es_budget`.
4. **Max position** — when `risk.max_position_usd > 0`, require the caller-supplied `current_position_usd` to be strictly less than the cap.

**Exits are NEVER blocked.** Intent types `"exit"`, `"cancel"`,
`"rearm"`, `"prepare"`, and intents with an unmapped action all
short-circuit the helper with `None`. V1 must always allow
risk-reducing flows to fire so open positions can be unwound even
when the engine is otherwise locked down.

**Where the helper MUST be wired (V1):**
- `execution/live_runner.py::_ripple_cb` — both the recorder-attached
  and the no-recorder branches must call it before `exec_mgr.on_intent`.
- `ui/main_window.py::_on_ripple_received` — the `StrategyMode.LIVE`
  branch must call it before `self._exec_manager.on_intent`.

**Determinism note.** The gate is a pure function over the strategy
snapshot at the moment the decision arrives — no wall-clock reads,
no random number generation, no shared mutable state besides the
engine the caller already owns. This preserves the §7.1
deterministic-replay contract: replaying the same Ripple decision
stream against the same Tide/Wave/RV history will produce the same
gate decisions every time.

**Enforcement check (CI-friendly — must return ≥1 production hit
outside `tests/` for each callsite):**

```bash
grep -rn "intent_risk_block_reason" execution/ ui/ main.py
# Expected production hits (post-2026-05-12):
#   execution/models.py        — def intent_risk_block_reason
#   execution/live_runner.py   — import + 1 call from _gate_and_dispatch
#   ui/main_window.py          — import + 1 call from _on_ripple_received (LIVE branch)
```

**And the acceptance suite must run without skips:**

```bash
grep -nE "@(pytest\.mark\.skip|unittest\.skip)\(" tests/test_live_execution_v1_compliance.py
# Expected: empty.
```

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
| Wiring drift (engine-only delivery) | Capability is unit-test-complete in C++/Python but never invoked by any live entry point — engine silently runs against `Default*Snapshot` defaults | §3.5 wiring rule + `Wired live?` column on §2.2 of `implementation_plan.md` + grep-based CI checks (see §7.4 / §7.5) |
| Signal-driven live execution | Live `ExecutionManager` consumes raw `SignalEngine.Signal` events instead of `RippleDecision` intents — bypasses lifecycle FSM, scaling, exits, and `RiskEngine` | §7.4 live execution topology contract; `engine.set_signal_callback` is for observation only |
| Wall-clock decision logic | `time.time()` / `datetime.now()` in cooldown / throttle / gating logic — breaks event-time replay determinism | §7.1 + §7.4; CI grep gate on `execution/execution_manager.py` |

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
| Depth-trade timestamp skew | `chart_now` shifts visible window away from trade data | Cap depth lead in `chart_now` (see §11.4) |
| Trade-derived data disappearing | Bubbles/CVD vanish while heatmap is still live | Regression guard + pipeline diagnostics (see §11.4) |
| Pruning with wrong time reference | Using `chart_now` instead of trade-derived time for trade pruning | Prune on `_last_trade_ts` only, never depth-influenced time (see §11.4) |

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

### 11.4 UI Visualization Pipeline Failures

These failures cause trade-derived visualizations (bubbles, CVD) to silently disappear while other layers (heatmap, volume profile) continue rendering. They have caused multiple regressions and must be explicitly guarded against.

| Failure Mode | Root Cause | Symptom | Prevention |
|---|---|---|---|
| Depth-trade skew shifts visible window | `chart_now = max(depth_ts, trade_ts)` lets depth WS event time advance far ahead of trade WS trade time. Caused by network jitter, asyncio scheduling differences, and sparse trade periods. | Bubbles and CVD vanish after a few minutes. Heatmap unaffected. `fT` (filtered-by-time) counter climbs. PERF log shows growing `skew`. | `chart_now` must cap depth lead: `max(t, min(d, t + MAX_DEPTH_LEAD_MS))`. Currently capped at 2 seconds. |
| Pruning uses depth-influenced time | Trade deque pruning cutoff derived from `chart_now` (which includes depth timestamps). When depth is ahead, cutoff becomes too aggressive and wipes trades that should be visible. | `_trades` deque empties or shrinks drastically. `prn` (pruned) counter spikes. Bubbles disappear even though trades are arriving. | Prune only on `_last_trade_ts`, never `chart_now` or `_last_depth_ts`. Keep 2× visible window buffer. Mirrors CVD's pruning approach. |
| Render-time filtering too aggressive | `t_start = chart_now - visible_window_ms` with uncapped `chart_now` filters out all trades whose timestamps are behind depth time. | `fT` counter grows while `trd` (total in deque) stays healthy. `bub` declines proportionally to skew growth. | Capping `chart_now` (first row) prevents this. Any change to `t_start` calculation must consider depth-trade skew. |
| Silent pipeline collapse | No diagnostic, no warning — trade-derived layers simply stop rendering. | User sees heatmap but no bubbles or CVD. Hard to diagnose without instrumentation. | `_bubble_diag` counters + `failure_stage` inference + `BUBBLES_DEAD_WHILE_TRADES_LIVE` regression guard in `main_window.py`. Always-on, cheap. |

**Critical invariants** (must be preserved by any future change to the heatmap/bubble/CVD path):

1. **`chart_now` must be capped.** `chart_now = max(trade_ts, min(depth_ts, trade_ts + MAX_DEPTH_LEAD_MS))` when both streams are live. Do not revert to `max(depth_ts, trade_ts)`.
2. **Trade pruning must use trade-derived time only.** The cutoff in `add_depth_column` must be based on `_last_trade_ts`, not `chart_now` or `_last_depth_ts`.
3. **Render-time `t_start` inherits the cap from `chart_now`.** Do not compute `t_start` from uncapped values.
4. **`_bubble_diag` instrumentation must remain active.** It is cheap (counter increments only) and is the only way to diagnose pipeline failures without a live debugger.
5. **The `failure_stage` diagnostic must be exposed** in the PERF log and status panel. Any future pipeline change must update the failure-stage inference if new failure modes are introduced.
6. **`test_chart_now_caps_depth_lead` and `test_pruning_uses_trade_time_not_depth_time`** in `tests/test_bubble_pipeline.py` are mandatory regression tests. They must pass before any change to the bubble/CVD rendering path is merged.

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

---

## 21. UI Visualization Pipeline Rules

These rules govern the real-time UI rendering pipeline (`heatmap_widget.py`, `cvd_widget.py`, `main_window.py`). They exist because **two separate regressions** silently killed the bubble layer and CVD, both caused by timestamp handling errors in the trade-derived rendering path.

### 21.1 Timestamp Discipline

The UI receives data from two independent WebSocket streams with independent timestamps:

| Stream | Field | Meaning | Update Rate |
|---|---|---|---|
| Depth WS (`@depth@100ms`) | `E` (event time) | When Binance generated the depth batch | Every 100ms, continuous |
| Trade WS (`@trade`) | `T` (trade time) | When the trade was executed | Per-trade, sparse in quiet markets |

**Key fact:** These timestamps can and do drift apart. Depth `E` advances continuously; trade `T` advances only when trades execute. Network batching, asyncio scheduling, and sparse trade periods cause depth to lead trade by seconds to tens of seconds. This is normal and must be handled.

### 21.2 `chart_now` Rules

`chart_now` is the unified "now" timestamp used for:
- Computing the visible window: `t_start = chart_now - visible_window_ms`
- CVD time reference: `set_time_ref(chart_now, ...)`
- PERF log reporting

**Invariant:** `chart_now` must be capped to prevent depth-trade skew from shifting the visible window:

```
chart_now = max(trade_ts, min(depth_ts, trade_ts + MAX_DEPTH_LEAD_MS))
```

Where `MAX_DEPTH_LEAD_MS = 2000` (configurable via `_MAX_DEPTH_LEAD_MS`).

**Prohibited:** `chart_now = max(depth_ts, trade_ts)` — this is the exact pattern that caused both regressions.

### 21.3 Trade Pruning Rules

Trade deques (`_trades`) must be pruned using **trade-derived time only**:

```
trade_now = _last_trade_ts
trade_cutoff = trade_now - visible_window_ms * 2
```

**Prohibited:** Pruning based on `chart_now`, `_last_depth_ts`, or any depth-influenced timestamp. This is what CVD does correctly (using `trade_time_only`) and what the bubble path previously got wrong.

### 21.4 Render-Time Filtering

When drawing bubbles, the visible window is:
```
t_start = chart_now - visible_window_ms   (chart_now is capped)
t_end   = chart_now
```

Because `chart_now` is capped, trade-derived data (bubbles, CVD bins) always falls within this window as long as trade data is flowing.

### 21.5 Pipeline Diagnostics (Always-On)

The `_bubble_diag` dictionary in `heatmap_widget.py` tracks stage-by-stage counters:

| Stage | Key Counters | What They Detect |
|---|---|---|
| A. Trade Input | `trades_added_total`, `trades_rejected_price`, `last_trade_add_ts` | Trade feed alive? Ingestion working? |
| B. Storage | `total_in_deque`, `max_deque_depth`, `trades_pruned_total` | Over-pruning? Deque growing unbounded? |
| C. Render Filter | `filtered_by_time`, `filtered_by_x`, `filtered_by_y`, `visible` | Where are trades being lost? |
| D. Render Output | `min_radius`, `max_radius`, `min_alpha`, `max_alpha` | Visible but invisible (zero radius/alpha)? |
| E. Failure Stage | `failure_stage` | Single string: `NONE`, `NO_TRADES_IN`, `PRUNED_TO_ZERO`, `OFFSCREEN_PRICE`, etc. |

These counters are cheap (integer increments) and must remain active in production. Do not gate them behind a debug flag.

### 21.6 Regression Guard

`main_window.py` must emit a throttled `BUBBLES_DEAD_WHILE_TRADES_LIVE` warning when:
- Trade feed is live (`trades_added_total > 0`)
- But visible bubble count is zero for N consecutive frames
- While heatmap is still rendering

The warning must include: `failure_stage`, `depth-trade skew`, `trades_pruned_total`, `filtered_by_time`, `deque depth`, and `p95_size`.

### 21.7 Mandatory Regression Tests

Any change to the bubble/CVD rendering path must pass these tests in `tests/test_bubble_pipeline.py`:

| Test | What It Validates |
|---|---|
| `test_pruning_uses_trade_time_not_depth_time` | Pruning survives 120-second depth-ahead skew |
| `test_chart_now_caps_depth_lead` | `chart_now` capped at `MAX_DEPTH_LEAD_MS` when depth leads |
| `test_stress_harness` | Visible bubbles never collapse to zero under 30-second growing skew |
| `test_bubble_visibility_in_time_window` | Trades within visible window are rendered |
| `test_trade_burst_does_not_wipe_visible` | Bursty high-volume trades don't wipe the visible set |

### 21.8 Common Mistakes to Avoid

1. **Do not use `max(depth_ts, trade_ts)` for any rendering-time calculation.** Always use `chart_now` (which is capped).
2. **Do not prune trade deques based on depth timestamps.** Use `_last_trade_ts` only.
3. **Do not remove `_bubble_diag` instrumentation.** It is the only way to diagnose pipeline failures without a live debugger.
4. **Do not assume depth and trade timestamps are synchronized.** They drift by seconds in normal operation.
5. **Do not add new time-dependent rendering logic without checking `test_bubble_pipeline.py`.** If you touch `t_start`, `chart_now`, pruning cutoffs, or visible window calculations, add a test that injects a 14-second depth-trade skew and verifies the data survives.
6. **Do not use local system time (`datetime.now()`, `time.time()`) for any rendering timestamp.** The initial REST depth snapshot already does this (a known wart); it must not be introduced elsewhere.
