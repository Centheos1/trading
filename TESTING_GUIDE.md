# Testing Guide

## 1. Overview

This document is the **canonical testing reference** for the Tide / Wave / Ripple quantitative trading system. It is intended for developers and coding agents working on the codebase.

The system is a mixed **C++ / Python** application that implements a layered trading strategy:

| Layer | Role | Language |
|---|---|---|
| **Tide** | Macro bias, capital allocation, risk budgets | Python (research); C++ proxy (live) |
| **Wave** | Market structure / regime classification | Python (research); C++ proxy (live) |
| **Ripple** | Execution: order flow analysis, liquidity transitions, trade lifecycle | C++ (hot path) |

The application operates in four modes: **data collection**, **backtest**, **optimise**, and **ui**. Backtest and optimise modes require strict determinism — identical inputs must produce identical outputs on every run.

Testing is critical because:

- The strategy must be **deterministic** in replay / backtest.
- We rely on **reproducible market simulations** for validation.
- Strategy **state machines must be stable** — no deadlocks, no unreachable states.
- **Risk limits must never be bypassed** — ES budgets, position caps, and permission checks are safety-critical.
- Future RL and context modules must be validated against the **deterministic baseline**.

**Source-of-truth hierarchy for testing:**

| Priority | Document | Role |
|---|---|---|
| 1 | `strategy.md` | Canonical strategy design, math, contracts |
| 2 | `implementation_plan.md` | Build plan, phases, acceptance criteria |
| 3 | Tests | Executable specification |
| 4 | `AGENT_STRATEGY_RULES.md` | Operational rules and guardrails |
| 5 | This document | How to run, write, and organize tests |

---

## 2. Testing Philosophy

### 2.1 Core Principles

1. **Correctness before performance.** Get behavior right, then optimize.
2. **Tests are executable specifications.** If a test contradicts `strategy.md`, the test is wrong — fix the test, not the strategy (unless the strategy has a bug).
3. **Every meaningful behavior change requires a test.** "Meaningful" means any change that alters outputs given the same inputs.
4. **Tests must be written before or alongside new features.** Never defer tests to a later commit.
5. **No test may be disabled without justification.** If a test fails, fix the code or the test — do not skip it.

### 2.2 Determinism Contract

For a given sequence of events and a given configuration, the strategy **must** produce identical outputs every time. This applies to:

- All features
- All state transitions
- All trade decisions
- All fills (in paper mode)

This means:

- **No wall-clock time** in decision logic — only event timestamps.
- **No unseeded random number generation** in decision logic.
- **No floating-point non-determinism** — use consistent rounding where needed.
- **All state that affects decisions must be serializable** for snapshot/restore.

### 2.3 Test-Driven Phase Progression

Each implementation phase has specific test requirements. A phase is not complete until its tests pass.

| Phase | Required Test Types |
|---|---|
| 1 — Schema & Contracts | Serialization round-trip, config load/save, pybind11 bindings |
| 2 — Deterministic Ripple | FSM transitions, exit types, scaling, event→decision, replay determinism |
| 3 — Liquidity Map | Map components, scores, map→targets, 1-week backtest, map update latency |
| 4 — Risk Budget | ES estimation, position sizing, risk check, rejection on exhaustion |
| 5 — Wave Baseline | Feature computation, regime rules, permissions→Ripple, replay determinism |
| 6 — Optimization & UI | Full pipeline, full parameter space, optimization convergence, UI integration |
| 7 — HMM Extensions | HMM math, model load, HMM vs rule-based, HMM determinism |
| 8 — Cross-Venue | Cross-venue features, multi-venue→Wave, multi-venue replay |

---

## 3. Test Categories

### 3.1 Unit Tests

**Purpose:** Validate individual functions and components in isolation.

**When required:** From Phase 1 onward. Every non-trivial function, feature computation, and state transition must have unit tests.

**Examples:**
- Enum `to_string` round-trip
- Config struct defaults match `strategy.md` §27
- `PermissionSet.get()` returns correct values
- Feature registry completeness against `strategy.md` §16
- Wall quality score computation

### 3.2 Feature Tests

**Purpose:** Validate that feature calculations produce correct values from known inputs.

**When required:** From Phase 2 onward for Ripple features; from Phase 5 onward for Wave features.

**What to test:**
- Microprice drift computation
- Order flow imbalance (top-1, top-5)
- Impact per unit volume
- CVD slope
- Wall persistence and cancellation rate
- Trend efficiency, dispersion, absorption ratio (Wave)

**Rule:** Every feature in `strategy.md` §16 that has a computation must have a test with known inputs and expected outputs.

### 3.3 State Machine Tests

**Purpose:** Validate trade lifecycle transitions, Ripple liquidity state inference, and any FSM in the system.

**When required:** From Phase 2 onward.

**What to test:**
- Every valid transition in the lifecycle FSM (SETUP → ENTRY → CONFIRMATION → ...)
- Invalid transitions are rejected
- Terminal states (CANCELLED, COOLDOWN → idle)
- No deadlocks — every non-terminal state has at least one outgoing transition
- No unreachable states
- Cooldown timer prevents re-entry

### 3.4 Replay / Determinism Tests

**Purpose:** Ensure that feeding identical event sequences produces identical outputs.

**When required:** From Phase 2 onward. Must pass after every behavioral change.

**What to test:**
- Same events + same config → same feature values
- Same events + same config → same trade decisions
- Same events + same config → same PnL

### 3.5 Integration Tests

**Purpose:** Ensure modules work together correctly end-to-end.

**When required:** From Phase 2 onward.

**What to test:**
- Event ingestion → feature computation → state inference → trade decision
- Wave permissions propagate correctly to Ripple
- Tide risk budgets propagate correctly to risk engine
- Fill events update trade lifecycle and risk state

### 3.6 Risk Tests

**Purpose:** Ensure capital limits, ES budgeting, and position caps are never violated.

**When required:** From Phase 4 onward.

**What to test:**
- No position exceeds `max_position_usd`
- No trade exceeds ES budget
- Risk-budget exit triggers when consumed ES reaches threshold
- Position sizing formula produces correct values
- Order rejected when budget is exhausted

### 3.7 Strategy Tests

**Purpose:** Validate bounce/breakout detection logic and trade management.

**When required:** From Phase 2 onward.

**What to test:**
- Bounce setup detection from wall + flow state
- Breakout setup detection from wall depletion + flow
- Confirmation logic for each archetype
- Exit type triggers (invalidation, target, exhaustion, time, risk-budget)
- Scale-in and scale-out conditions

### 3.8 Regression Tests

**Purpose:** Ensure behavior does not change unexpectedly when code is modified.

**When required:** Always. Before/after comparison for any behavioral change.

**Approach:** Capture known-good outputs (feature streams, trade logs, PnL) from a reference run. After changes, compare against the reference. Any difference must be explained and documented.

### 3.9 Performance Tests

**Purpose:** Ensure hot-path components remain efficient and meet latency targets.

**When required:** From Phase 6 onward for full benchmarks. Individual component benchmarks should be added as components are built.

**Latency targets (from `AGENT_STRATEGY_RULES.md` §9.2):**

| Operation | Target |
|---|---|
| Per-event feature update (C++) | < 10 µs |
| State machine transition (C++) | < 1 µs |
| Full Ripple tick-to-decision (C++) | < 100 µs |
| Risk check (C++) | < 1 µs |
| Wave update (Python, 5s cadence) | < 1 ms |
| Tide update (Python, 60s cadence) | < 10 ms |
| UI render cycle | < 50 ms |

**Rule:** Any regression > 20% on key latency metrics must be investigated and resolved.

### 3.10 Boundary / Adversarial Tests

**Purpose:** Validate correct behavior under edge cases and malformed inputs.

**When required:** From Phase 2 onward.

**What to test (from `strategy.md` §20.7):**
- NaN prices, negative quantities, zero timestamps
- Empty order book (no bids, no asks, or both)
- Single-level book
- Crossed book (best bid > best ask)
- Price gaps > 10% in a single event
- Timestamp discontinuities (gap > 60s, then burst)
- Maximum position reached mid-scale-in
- ES budget exhausted between entry and first scale-in
- Duplicate trade IDs in the event stream
- Out-of-order events (depth update with future timestamp)
- Zero-volume trade events

### 3.11 Property-Based Tests

**Purpose:** Verify mathematical invariants that must hold regardless of input.

**Invariants (from `strategy.md` §20.6):**
- ES >= VaR >= 0
- Final position quantity in [0, Q_max]
- Final position = 0 when risk budget = 0
- Imbalance in [-1, 1] for all order book states
- Wall quality score >= 0
- hold_score + break_score = 1
- Permissions matrix lookup never returns an undefined state
- Trade lifecycle FSM has no unreachable states and no deadlocks (except terminal states)

### 3.12 Cadence and Timing Tests

**Purpose:** Verify that layer update cadences are respected.

**What to test (from `strategy.md` §20.8):**
- Tide does not update more than once per `tide_update_interval_ms` (event-time)
- Wave does not update more than once per `wave_update_interval_ms` (event-time)
- Ripple processes every qualifying event
- Cooldown timer prevents re-entry for the configured duration
- Confirmation window expires at the correct event-time boundary

---

## 4. Running Tests

### 4.1 Prerequisites

**Python:**
```bash
# From the project root
pip install pytest
```

If `pytest` is not installed, you can also use the built-in `unittest` runner (see §6).

**C++:**
The C++ tests are built through CMake. The build directory is at:
```
backtestingCpp/orderflow/build/
```

To set up the build system:
```bash
cd backtestingCpp/orderflow
mkdir -p build && cd build
cmake ..
```

### 4.2 Quick Start

Run everything from the project root:

```bash
# Python tests
python -m unittest discover -s tests -v

# C++ tests (after cmake setup)
cd backtestingCpp/orderflow/build
make test_schemas test_ripple test_trade_lifecycle test_liquidity_map
./test_schemas
./test_ripple
./test_trade_lifecycle
./test_liquidity_map
```

---

## 5. C++ Test Commands

### 5.1 Build Tests

```bash
cd backtestingCpp/orderflow/build

# Reconfigure if CMakeLists.txt changed
cmake ..

# Build all test targets
make test_schemas test_ripple test_trade_lifecycle test_liquidity_map

# Build everything (including pybind11 module)
make -j$(sysctl -n hw.ncpu)
```

### 5.2 Run Tests

```bash
# Schema and data contract tests (Phase 1)
./test_schemas

# Ripple pipeline tests (wall detection, features, evidence, inference, decisions)
./test_ripple

# Trade lifecycle FSM tests (Phase 2)
./test_trade_lifecycle

# Liquidity map, VP/CVD, and scale-out tests (Phase 3)
./test_liquidity_map
```

### 5.3 Standalone Compilation (no cmake)

For quick iteration on schema tests without the full build system:

```bash
g++ -std=c++20 \
    -I backtestingCpp/orderflow \
    -o /tmp/test_schemas \
    backtestingCpp/orderflow/ripple/tests/test_schemas.cpp

/tmp/test_schemas
```

### 5.4 Available C++ Test Targets

| Target | File | What it tests | Phase |
|---|---|---|---|
| `test_schemas` | `ripple/tests/test_schemas.cpp` | Enums, data contracts, config defaults, feature registry, StrategySnapshot, pre-phase defaults | 1 |
| `test_ripple` | `ripple/tests/test_ripple.cpp` | RippleContext, WallDetector, RippleFeatureEngine, RippleEvidenceEngine, ScoreBasedInference, RippleStateTracker, TriggerDecisionEngine, RippleEngine full pipeline | 1 |
| `test_trade_lifecycle` | `ripple/tests/test_trade_lifecycle.cpp` | TradeLifecycleEngine FSM transitions, all 5 exit types, scale-in logic, trailing stop, permission checks, intent generation, determinism, TickContext, LifecycleConfig | 2 |
| `test_liquidity_map` | `ripple/tests/test_liquidity_map.cpp` | LiquidityMapEngine level collection (walls, VP, flow, structural), hold/break/dest scoring, void corridor detection, destinations query, CVD divergence boost, scale-out plan, determinism | 3 |

### 5.5 Discovering C++ Tests

All C++ test files live under:
```
backtestingCpp/orderflow/ripple/tests/
```

Test executables are declared in `backtestingCpp/orderflow/CMakeLists.txt` as `add_executable(...)` entries.

### 5.6 C++ Test Output Format

C++ tests use a lightweight `CHECK` macro that prints `FAIL: <message>` for failures and a final summary:

```
396 / 396 tests passed.
```

Exit code `0` means all tests passed; `1` means at least one failure.

---

## 6. Python Test Commands

### 6.1 Using pytest

```bash
# Run all Python tests
pytest tests/ -v

# Run a specific test file
pytest tests/test_schemas.py -v

# Run tests matching a keyword
pytest tests/ -k "feature" -v

# Stop on first failure
pytest tests/ --maxfail=1

# Run with short output
pytest tests/ -q

# Run a specific test class
pytest tests/test_schemas.py::TestStrategySnapshot -v

# Run a specific test method
pytest tests/test_schemas.py::TestFeatureRegistry::test_canonical_s16_completeness -v
```

### 6.2 Using unittest (no pytest required)

```bash
# Run all tests in the tests/ directory
python -m unittest discover -s tests -v

# Run a specific test file
python -m unittest tests.test_schemas -v

# Run a specific test class
python -m unittest tests.test_schemas.TestStrategySnapshot -v

# Run a specific test method
python -m unittest tests.test_schemas.TestFeatureRegistry.test_canonical_s16_completeness -v
```

### 6.3 Available Python Test Files

| File | What it tests |
|---|---|
| `tests/test_schemas.py` | Enums, data contracts, defaults, config JSON round-trip, feature registry completeness, StrategySnapshot construction/determinism/serialization, pre-phase defaults |

### 6.4 Discovering Python Tests

All Python test files live under:
```
tests/
```

Test files follow the `test_*.py` naming convention. Test classes inherit from `unittest.TestCase`. Test methods are named `test_*`.

---

## 7. Strategy-Specific Tests

### 7.1 Liquidity Transition Tests

Liquidity transitions are the core of the Ripple execution layer. Each transition must be tested with synthetic order book and trade data.

**Absorption:**
- Wall retains quantity despite aggressive flow directed at it
- Evidence scoring: absorption score rises above threshold
- State transitions to ABSORBING when absorption dominates

**Exhaustion:**
- Wall being consumed (depletion percentage increasing)
- Refill rate low relative to depletion
- Impact per unit volume rising
- State transitions to EXHAUSTING

**Liquidity withdrawal:**
- Wall depth shrinks via cancellation, not fills
- Cancellation rate dominates total shrinkage
- Spread shock (liquidity pulled)
- State transitions to WITHDRAWING

**Refill:**
- Depth reappearing after depletion
- Refill rate exceeds depletion rate
- Aggression reversed or balanced
- State transitions to REFILLING

### 7.2 Trade Archetype Tests

**Bounce trade:**
- Wall detected with sufficient quality score
- Price within proximity zone (proximity_sigma)
- Microprice shifting away from wall (confirmation)
- Imbalance above threshold (bounce_imbalance_thresh)
- Risk evidence (breakout, withdrawal) below safety thresholds

**Breakout trade:**
- Wall detected with sufficient quality score
- Wall depth failing (below breakout_depth_fail_ratio)
- CVD slope above threshold (breakout_cvd_slope_thresh)
- Absorption evidence below safety threshold

### 7.3 Trade Lifecycle Tests

Test every transition in the lifecycle FSM defined in `strategy.md` §12:

```
SETUP → ENTRY → CONFIRMATION → EXPANSION → MATURATION → EXIT → COOLDOWN
                                                                   ↓
SETUP → CANCELLED                                               (idle)
```

| Transition | Trigger | Test |
|---|---|---|
| SETUP → ENTRY | Confirmation conditions met within window | Feed events that satisfy archetype-specific confirmation |
| SETUP → CANCELLED | Confirmation window expires | Feed events with timestamps past window |
| ENTRY → CONFIRMATION | Fill received | Feed a fill event |
| CONFIRMATION → EXPANSION | Favorable move > threshold | Feed price movement exceeding expand_threshold_sigma |
| EXPANSION → MATURATION | Max hold time reached or targets hit | Feed events at time boundary |
| Any active → EXIT | Exit signal fires | Feed conditions for each exit type |
| EXIT → COOLDOWN | Exit complete | Verify transition after exit |
| COOLDOWN → idle | Cooldown timer expires | Feed event after cooldown_ms elapsed |

### 7.4 Exit Type Tests

Test each exit type independently (from `strategy.md` §13):

| Exit Type | Trigger | Priority | Test |
|---|---|---|---|
| Risk-budget | consumed_es >= 90% budget OR risk_mult = 0 | Highest | Set risk budget near exhaustion, verify exit |
| Invalidation | Stop price hit | Highest | Move price through stop |
| Time | hold_time > max_hold_time_ms | Normal | Advance event timestamp past max hold time |
| Target | Price crosses target level | Normal | Move price through target |
| Exhaustion | CVD fading + rate declining + impact rising | Normal | Feed exhaustion pattern |

**Evaluation order:** Risk-budget → Invalidation → Time → Target → Exhaustion (first match fires).

---

## 8. Replay / Backtest Determinism Tests

### 8.1 Core Approach

1. Prepare a fixed sequence of events (trades + depth updates) with known timestamps.
2. Configure the strategy with a fixed configuration.
3. Run the event sequence through the full pipeline twice.
4. Assert that all outputs are bitwise identical.

### 8.2 What to Compare

| Output | Comparison |
|---|---|
| Feature values | Exact float equality (or within `testing.replay_tolerance`) |
| Liquidity state | Enum equality |
| Trade decisions | Identical intents, timestamps, prices, quantities |
| Lifecycle states | Identical state at every event |
| PnL | Exact equality |
| Fill events | Identical count, prices, quantities, sides |

### 8.3 Strategy for Implementation

```python
# Pseudocode for a replay determinism test
def test_replay_determinism():
    events = load_fixed_event_sequence()
    config = load_fixed_config()

    outputs_a = run_pipeline(events, config)
    outputs_b = run_pipeline(events, config)

    assert outputs_a.features == outputs_b.features
    assert outputs_a.decisions == outputs_b.decisions
    assert outputs_a.pnl == outputs_b.pnl
```

### 8.4 Common Determinism Violations

| Violation | Symptom | Fix |
|---|---|---|
| Wall-clock time in logic | Different outputs on different runs | Replace with event timestamps |
| Unseeded RNG | Different outputs on different runs | Seed all PRNGs from config |
| Map iteration order | Different outputs on different platforms | Use ordered containers |
| Float accumulation order | Tiny differences growing over time | Use consistent reduction order |
| Uninitialized memory | Non-deterministic initial values | Zero-initialize all structs |

---

## 9. Risk Engine Tests

### 9.1 Position Limits

- Verify that no position exceeds `tide.max_position_usd`.
- Verify that `RiskEngine::check_new_order` returns `false` when a new order would exceed the limit.
- Verify that `RiskEngine::get_allowed_size` clips the requested size to the available budget.

### 9.2 ES Budget Limits

- Verify that `consumed_es` never exceeds `es_budget`.
- Verify that the risk-budget exit triggers when `consumed_es >= budget_exit_threshold * es_budget`.
- Verify that `Q_final = 0` when `remaining_budget = 0`.

### 9.3 Position Sizing Formula

- Verify that position sizing respects `risk.target_risk_usd`, `risk.sigma_target`, and the Tide risk multiplier.
- Verify that `Q_final` is clamped to `[0, Q_max]`.
- Verify the vol-ratio clamp in `ripple.vol_ratio_clamp`.

### 9.4 Fee Accounting

- Verify that PnL calculations include maker and taker fees.
- Verify that fee configuration parameters propagate correctly from `risk.maker_fee_bps` and `risk.taker_fee_bps`.

### 9.5 Strategy Sleeve Budgets (V2+)

Not required in V1. Reserve test stubs for hierarchical ES decomposition.

---

## 10. Liquidity Map Tests

### 10.1 Component Tests

Each component of the liquidity map must be tested independently:

| Component | Test |
|---|---|
| Resting liquidity levels | Walls contribute depth at correct prices |
| Traded liquidity levels | Volume profile contributes profile_volume |
| Flow-pressure levels | Trade flow contributes net_flow |
| Structural anchors | VWAP, session high/low are correct |
| Void corridors | Corridors detected between significant levels |

### 10.2 Score Tests

- `hold_score + break_score = 1` for every level.
- `dest_score` ranks destinations correctly.
- Scores update correctly as new data arrives.

### 10.3 Integration

- Targets derived from the liquidity map match the highest-scoring destinations.
- Scale-out points align with destination levels.
- The map respects the bounded level count (max 100 levels per `strategy.md` §21.1).

### 10.4 Performance

- `LiquidityMapEngine::update` should complete in < 50 µs (bounded number of levels).
- Void corridor detection: < 10 µs.

---

## 11. Trade State Machine Tests

### 11.1 Lifecycle FSM Completeness

Verify that the FSM defined in `strategy.md` §12 is correctly implemented:

- All 8 states exist: SETUP, ENTRY, CONFIRMATION, EXPANSION, MATURATION, EXIT, COOLDOWN, CANCELLED.
- All valid transitions are reachable.
- No invalid transitions are possible.
- Terminal states (CANCELLED, COOLDOWN→idle) do not have outgoing transitions to active states.

### 11.2 Single-Trade Constraint (V1)

- At most one trade is active per symbol at any time.
- A new SETUP cannot begin while another trade is in any active or cooldown state.

### 11.3 Scaling Tests

- Scale-in only occurs in EXPANSION state.
- Scale-in requires favorable move > `scale_in_threshold_sigma` × σ.
- Scale-in requires CVD momentum intact.
- Scale-in requires risk budget allows additional size.
- Max scale-ins is enforced (default: 1 add, 2 tranches total).
- Stop moves to breakeven after scale-in fill.
- Scale-out targets are ranked by `dest_score`.
- Scale-out fractions sum to 1.0.
- Stop moves to breakeven after first scale-out.
- After all scale-outs, trailing stop engages.
- Trailing stop only tightens, never loosens.

---

## 12. Integration Tests

### 12.1 Event-to-Decision Pipeline

Feed a sequence of synthetic events and verify:
1. Order book updates correctly.
2. Features compute correctly.
3. Evidence scores are reasonable.
4. State inference transitions correctly.
5. Trigger decision fires at the right moment.
6. Trade lifecycle manages the trade correctly.
7. Exit triggers at the right condition.

### 12.2 Cross-Layer Integration

- Wave permissions propagate to Ripple: disabled archetype is never entered.
- Tide risk budgets propagate to risk engine: position size is throttled.
- Fill events propagate from execution layer back to trade lifecycle and risk engine.

### 12.3 Mode Compatibility

- **Backtest mode:** Uses `ReplayFeed`, produces deterministic results.
- **Optimise mode:** Runs backtest in a loop with parameter variation, each run is deterministic.
- **UI mode:** Strategy state exposed to widgets without blocking the render thread.

---

## 13. Performance Tests

### 13.1 Benchmarking Approach

Performance tests measure latency and throughput of hot-path components. They should:

1. Warm up the component (discard first N iterations).
2. Measure wall-clock time over many iterations.
3. Report median, p95, and p99 latencies.
4. Compare against target latencies from `AGENT_STRATEGY_RULES.md` §9.2.

### 13.2 What to Benchmark

| Component | Metric | Target |
|---|---|---|
| Order book update | Time per depth update | < 10 µs |
| Feature calculation | Time per event | < 10 µs |
| Evidence scoring | Time per event | < 5 µs |
| State inference | Time per event | < 1 µs |
| Full Ripple pipeline | Tick-to-decision latency | < 100 µs |
| Risk check | Time per check | < 1 µs |
| Liquidity map update | Time per update | < 50 µs |
| Memory usage | Per-symbol memory | < 50 MB |

### 13.3 Example C++ Benchmark

```cpp
#include <chrono>

void benchmark_feature_update() {
    RippleEngine engine(config);
    // ... setup with realistic book state ...

    constexpr int WARMUP = 1000;
    constexpr int ITERS  = 100000;

    for (int i = 0; i < WARMUP; ++i)
        engine.on_trade(make_trade(/* ... */));

    auto start = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < ITERS; ++i)
        engine.on_trade(make_trade(/* ... */));
    auto end = std::chrono::high_resolution_clock::now();

    auto ns = std::chrono::duration_cast<std::chrono::nanoseconds>(end - start).count();
    double us_per_iter = static_cast<double>(ns) / ITERS / 1000.0;
    std::cout << "Feature update: " << us_per_iter << " µs/iter" << std::endl;
}
```

### 13.4 Memory Profiling

For sustained replay tests, monitor memory usage over time to ensure bounded data structures do not grow beyond limits defined in `strategy.md` §21.1:

| Data Structure | Maximum Size |
|---|---|
| Order book levels per side | 500 levels |
| Tracked walls | 50 per side |
| Liquidity map levels | 100 |
| Volume profile bins | 1000 |
| Trade history (rolling) | 100,000 events |
| CVD bins | 10,000 |

---

## 14. Regression Tests

### 14.1 Approach

1. **Capture a reference run:** Run the full pipeline on a known dataset with a known configuration. Save all outputs (features, decisions, fills, PnL).
2. **After any change:** Run the same pipeline on the same dataset with the same configuration. Compare outputs against the reference.
3. **Any difference must be explained.** If the difference is intentional, update the reference. If not, the change has introduced a regression.

### 14.2 What to Capture

- Feature snapshot stream (one per event)
- Trade lifecycle state stream
- Fill event log
- Final PnL and drawdown
- Risk budget utilization

### 14.3 Automation

Regression tests should be automated as part of the test suite once Phase 6 is reached. Before that, manual comparison is acceptable if documented.

---

## 15. Continuous Integration Expectations

### 15.1 Pre-Commit Requirements

Before submitting any change, the developer must:

1. Run the full Python test suite: `python -m unittest discover -s tests -v`
2. Build and run all C++ tests: `make test_schemas test_ripple test_trade_lifecycle test_liquidity_map && ./test_schemas && ./test_ripple && ./test_trade_lifecycle && ./test_liquidity_map`
3. Verify that the pybind11 module compiles: `make orderflow_engine`
4. Check for linter errors in changed files.

### 15.2 Definition of Done

A change is **done** when:

| Criterion | Check |
|---|---|
| Compiles | `cmake --build .` succeeds |
| Tests pass | All existing tests pass; new tests added for new behavior |
| Replay deterministic | Same events + config → same outputs |
| Linter clean | No new linter warnings in changed files |
| Docs updated | `strategy.md` / `implementation_plan.md` updated if needed |
| Performance acceptable | No regression > 20% on key metrics |
| Change is narrow | One logical unit; no unrelated modifications |

### 15.3 Post-Phase-6 Expectations

After Phase 6:
- Performance benchmarks become part of the test suite.
- Replay determinism tests cover the full parameter space.
- A paper-trade soak test (24+ hours) is required before live deployment.

---

## 16. Adding New Tests

### 16.1 Mandatory Rules

1. **Every new feature requires a unit test.**
2. **Every state machine transition must have a test.**
3. **Every exit type must have a test.**
4. **Every risk constraint must have a test.**
5. **Every feature computation must have a test** with known inputs and expected outputs.
6. **Strategy logic must be deterministic** — replay tests must pass after every behavioral change.
7. **Performance-sensitive code should have benchmarks.**

### 16.2 Where to Put Tests

| Language | Location | Naming |
|---|---|---|
| Python | `tests/test_*.py` | Classes inherit `unittest.TestCase`, methods named `test_*` |
| C++ | `backtestingCpp/orderflow/ripple/tests/test_*.cpp` | Functions named `test_*`, registered in `main()` |

### 16.3 C++ Test Template

```cpp
#include <cassert>
#include <iostream>
#include "../../Schemas.h"  // or whatever headers are needed

using namespace orderflow;

static int tests_run = 0;
static int tests_passed = 0;

#define CHECK(cond, msg)                                           \
    do {                                                           \
        tests_run++;                                               \
        if (!(cond)) {                                             \
            std::cerr << "FAIL: " << msg << " (" << __FILE__      \
                      << ":" << __LINE__ << ")" << std::endl;      \
        } else {                                                   \
            tests_passed++;                                        \
        }                                                          \
    } while (0)

void test_my_feature() {
    // Arrange
    // Act
    // Assert using CHECK()
}

int main() {
    test_my_feature();
    std::cout << tests_passed << " / " << tests_run << " tests passed." << std::endl;
    return (tests_passed == tests_run) ? 0 : 1;
}
```

After creating the file, add a build target in `backtestingCpp/orderflow/CMakeLists.txt`:

```cmake
add_executable(test_my_feature ripple/tests/test_my_feature.cpp)
target_link_libraries(test_my_feature PRIVATE orderflow_core)
```

### 16.4 Python Test Template

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schemas import StrategySnapshot, TideBias  # import what you need


class TestMyFeature(unittest.TestCase):

    def test_basic_behavior(self):
        # Arrange
        # Act
        # Assert
        self.assertEqual(expected, actual)


if __name__ == "__main__":
    unittest.main()
```

### 16.5 Registering Tests

- **Python:** Place the file in `tests/` with a `test_` prefix. Both `pytest` and `unittest discover` will find it automatically.
- **C++:** Add an `add_executable` + `target_link_libraries` entry in `CMakeLists.txt`. Then rebuild with `cmake .. && make`.

---

## 17. Debugging Failed Tests

### 17.1 C++ Test Failures

When a C++ test fails, the output looks like:

```
FAIL: expected X but got Y (test_schemas.cpp:142)
395 / 396 tests passed.
```

Steps:
1. Read the failure message and line number.
2. Open the test file at the indicated line.
3. Run the specific test function in isolation if needed (comment out others in `main()`).
4. Use `std::cerr` to print intermediate values.
5. If the test is wrong (testing incorrect behavior per `strategy.md`), fix the test and document why.

### 17.2 Python Test Failures

```bash
# Run with verbose output
python -m unittest tests.test_schemas -v

# Run a single failing test
python -m unittest tests.test_schemas.TestMyClass.test_my_method -v
```

Steps:
1. Read the assertion error and traceback.
2. Add `print()` statements or use `pdb` for debugging.
3. Check that test expectations match `strategy.md`.

### 17.3 Build Failures After Schema Changes

If `Schemas.h` or `schemas.py` changes, you may see cascading failures:
1. Rebuild the C++ library: `cd build && cmake .. && make -j$(sysctl -n hw.ncpu)`
2. Rebuild the pybind11 module: `make orderflow_engine`
3. Run both C++ and Python test suites.
4. If a schema change is required, follow the update order: `strategy.md` §17 → C++ structs → Python dataclasses → pybind11 bindings → tests.

---

## 18. Test Data and Fixtures

### 18.1 Synthetic Data

Most tests use **synthetic data** — manually constructed order books, trades, and depth updates. This ensures tests are:
- Fast (no I/O).
- Deterministic (no market randomness).
- Portable (no external data dependencies).

C++ test helpers for creating synthetic data:

```cpp
// From test_ripple.cpp — reusable helpers
static DepthUpdate make_snapshot(int64_t ts,
    std::vector<std::pair<double,double>> bids,
    std::vector<std::pair<double,double>> asks);

static DepthUpdate make_depth_update(int64_t ts,
    std::vector<std::pair<double,double>> bids,
    std::vector<std::pair<double,double>> asks);

static Trade make_trade(int64_t ts, double price, double qty, bool is_buyer_maker);
```

### 18.2 Historical Data (Phase 3+)

For backtest validation tests, historical tick data is stored in HDF5 files:
```
data/<exchange>_ticks.h5
```

These are accessed through `TickStore` and `ReplayFeed`. Historical data tests are used from Phase 3 onward to validate that the strategy produces sensible results on real market data.

### 18.3 Configuration Fixtures

The default configuration file is:
```
config.json
```

Tests that depend on configuration should either:
- Use `StrategyConfig()` with defaults (preferred for unit tests).
- Load from a test-specific JSON file (for integration tests that need specific parameters).
- Never depend on the global `config.json` for correctness — it is a convenience default, not a test fixture.

---

## 19. Testing Roadmap

### 19.1 Current State

| Test Suite | Tests | Status | Phase |
|---|---|---|---|
| `test_schemas.py` (Python) | 56 tests | Passing | 1 |
| `test_schemas.cpp` (C++) | 396 checks | Passing | 1 |
| `test_ripple.cpp` (C++) | 163 checks | Passing | 1 |
| `test_trade_lifecycle.cpp` (C++) | 126 checks | Passing | 2 |
| `test_liquidity_map.cpp` (C++) | 112 checks | Passing | 3 |

### 19.2 Planned Test Suites by Phase

| Phase | New Test Suite | Focus | Status |
|---|---|---|---|
| 2 | `test_trade_lifecycle.cpp` | Lifecycle FSM transitions, exit types, scaling, trailing stop, permissions, intents, determinism | **Done** (99 checks) |
| 2 | `test_trigger_detection.cpp` | Bounce/breakout detection (covered partially via lifecycle tests; dedicated suite deferred) | Deferred |
| 3 | `test_liquidity_map.cpp` | Map components, scores, void corridors, CVD divergence boost, scale-out plan, wall quality filter, determinism | **Done** (112 checks) |
| 4 | `test_risk_engine.cpp` | ES estimation, position sizing, budget enforcement | Planned |
| 5 | `test_wave_engine.py` | Feature computation, regime classification, permissions | Planned |
| 6 | `test_replay_determinism.py` | Full pipeline replay comparison | Planned |
| 6 | `benchmark_pipeline.cpp` | Latency benchmarks for full pipeline | Planned |

### 19.3 Test Architecture

```mermaid
flowchart TD
    UT[Unit Tests] --> FT[Feature Tests]
    FT --> SMT[State Machine Tests]
    SMT --> ST[Strategy Tests]
    ST --> IT[Integration Tests]
    IT --> RT[Replay / Determinism Tests]
    RT --> PT[Performance Tests]

    style UT fill:#2d6a4f,color:#fff
    style FT fill:#2d6a4f,color:#fff
    style SMT fill:#40916c,color:#fff
    style ST fill:#40916c,color:#fff
    style IT fill:#52b788,color:#000
    style RT fill:#52b788,color:#000
    style PT fill:#95d5b2,color:#000
```

Tests form a **pyramid** — unit tests at the base are the most numerous and fastest. Each layer above builds on the confidence established below.

### 19.4 Test Coverage of Strategy Layers

```mermaid
flowchart TD
    subgraph TIDE ["Tide Layer Tests"]
        TT[Tide Config Tests]
        TT --> RiskTests[Risk Budget Tests]
        TT --> BiasTests[Bias / Vol Regime Tests]
    end

    subgraph WAVE ["Wave Layer Tests"]
        WT[Wave Config Tests]
        WT --> RegimeTests[Regime Classification Tests]
        WT --> PermTests[Permission Matrix Tests]
    end

    subgraph RIPPLE ["Ripple Layer Tests"]
        RPT[Feature Computation Tests]
        RPT --> EvidenceTests[Evidence Scoring Tests]
        EvidenceTests --> InferenceTests[State Inference Tests]
        InferenceTests --> TriggerTests[Trigger / Archetype Tests]
        TriggerTests --> LifecycleTests[Trade Lifecycle Tests]
        LifecycleTests --> ExitTests[Exit Taxonomy Tests]
    end

    RiskTests --> LifecycleTests
    PermTests --> TriggerTests
    ExitTests --> ReplayTests[Replay Determinism Validation]

    style TIDE fill:#1a4731,color:#fff
    style WAVE fill:#2d6a4f,color:#fff
    style RIPPLE fill:#40916c,color:#fff
    style ReplayTests fill:#95d5b2,color:#000
```

---

## 20. Glossary

| Term | Definition |
|---|---|
| **Absorption** | A wall retaining its depth despite aggressive flow directed at it. |
| **Archetype** | A recognized trade setup pattern — currently BOUNCE or BREAKOUT. |
| **Boundary test** | A test that exercises edge cases (empty inputs, maximums, zeros). |
| **Breakout** | A trade archetype where price pushes through a failing wall. |
| **Bounce** | A trade archetype where price reverses at a wall that is holding. |
| **CVD** | Cumulative Volume Delta — running sum of (buy volume - sell volume). |
| **Determinism** | The property that identical inputs always produce identical outputs. |
| **ES** | Expected Shortfall — average loss in the worst α% of outcomes. |
| **Exhaustion** | Aggressive flow losing momentum; depletion without refill. |
| **Exit type** | One of: INVALIDATION, TARGET, EXHAUSTION, TIME, RISK_BUDGET. |
| **Feature** | A named numeric or categorical value computed from market data. |
| **FSM** | Finite State Machine — the lifecycle model for trades. |
| **Hot path** | The per-event C++ code path that must meet strict latency targets. |
| **HMM** | Hidden Markov Model — probabilistic state inference (V2+). |
| **Imbalance** | Ratio of bid-to-ask depth at the top of book, normalized to [-1, 1]. |
| **Lifecycle state** | One of: SETUP, ENTRY, CONFIRMATION, EXPANSION, MATURATION, EXIT, COOLDOWN, CANCELLED. |
| **Liquidity map** | A composite view of resting depth, traded volume, flow, and structural levels. |
| **LSI** | Liquidity Stress Index — a composite measure of market stress. |
| **Microprice** | A depth-weighted mid-price that reflects order book asymmetry. |
| **OFI** | Order Flow Imbalance — net aggressive volume per unit time. |
| **Permission** | FULL / REDUCED / DISABLED — Wave's control over which archetypes Ripple may execute. |
| **Property-based test** | A test that asserts a mathematical invariant holds for all inputs. |
| **Regression test** | A test that compares current outputs against a known-good reference. |
| **Replay** | Feeding stored historical events through the strategy pipeline deterministically. |
| **Ripple** | The execution layer — processes L2 data, detects setups, manages trades. |
| **Scale-in** | Adding to an existing position after confirmation in the EXPANSION state. |
| **Scale-out** | Partial exits at liquidity map destination levels. |
| **Tide** | The macro layer — bias, capital allocation, ES budgets. |
| **VPIN** | Volume-Synchronized Probability of Informed Trading. |
| **Wall** | A large resting order in the order book that acts as a structural level. |
| **Wave** | The regime layer — market structure classification and trade permissions. |
