# Coding Standards

This document is the **engineering quality bar** for all code in this repository.
It applies to every agent and developer, on every change, in every language.

It complements — does not replace — the strategy-specific guardrails. Read these
together:

| Document | Owns |
|---|---|
| `strategy.md` | Strategy design, math, contracts (source of truth) |
| `implementation_plan.md` | Build plan, phases, acceptance criteria |
| `docs/AGENT_STRATEGY_RULES.md` | Tide/Wave/Ripple architecture, determinism, hot-path latency |
| `docs/TESTING_GUIDE.md` | How to run, write, and organize tests |
| `docs/DEPLOYMENT.md` | EC2 / Docker / S3 runbook |
| **This document** | General code quality + **data-pipeline & operational reliability** |

**Why this exists.** Data collection and EC2 deployment have repeatedly broken
in ways that were silent, hard to diagnose, and expensive to recover from (e.g.
the 2026-06 Parquet-mirror freeze: a rotated HDF5 stranded the flush watermark,
so the mirror silently stopped for two weeks while everything *looked* healthy).
The root causes were not exotic — they were missing guardrails: no failure was
surfaced, no invariant was checked, no test covered the rotation path. The rules
below exist to make that class of failure impossible to ship unnoticed.

---

## 1. Non-Negotiables (the short list)

If you remember nothing else:

1. **Fail loud, never silent.** A component that cannot do its job must log at
   `WARNING`/`ERROR` and surface the failure. Never `except: pass`. Never return
   a success-shaped value on failure.
2. **No silent data loss.** Any code path that can drop, skip, or truncate data
   must count it, log it, and (where possible) quarantine the bad input rather
   than discard it.
3. **State that outlives a process must be self-healing.** Watermarks, caches,
   offsets, and checkpoints must detect when their underlying assumption broke
   (file rotated, schema changed, counter reset) and recover, not freeze.
4. **Every behaviour change ships with a test** — including the failure path you
   just fixed. A bug fix without a regression test is not done.
5. **Leave the system compile-safe and green.** Run the relevant tests before
   you call a task complete. See `docs/TESTING_GUIDE.md`.
6. **Update the docs you invalidate.** If you change behaviour described in a
   doc (`DEPLOYMENT.md`, `strategy.md`, etc.), update that doc in the same change.
7. **Keep changes narrow.** One logical unit per change. No drive-by refactors
   mixed with behavioural fixes.

The full Definition of Done lives in `AGENT_STRATEGY_RULES.md` §12 and §3.3b
(stub policy). It applies to all code, not just strategy code.

---

## 2. Code Quality Fundamentals

### 2.1 Correctness first, then clarity, then performance
- Get the behaviour provably right before optimising. Profile before claiming a
  performance problem. (Hot-path C++ latency targets: `AGENT_STRATEGY_RULES.md` §9.)
- Numerical outputs must be verified against known inputs or reference data. No
  silent approximations or placeholder math (`AGENT_STRATEGY_RULES.md` §3.2).

### 2.2 No stubs masquerading as done
- A `pass`, `return {}`, `raise NotImplementedError`, `// TODO`, or empty handler
  in the scope of your change means the task is **not complete**. If a placeholder
  is unavoidable, tag it `TODO(<phase/ticket>):` and say so explicitly
  (`AGENT_STRATEGY_RULES.md` §3.3b).

### 2.3 Naming and structure
- Descriptive names; no abbreviations that aren't already idiomatic in the repo.
- Functions do one thing. Prefer early returns over deep nesting.
- Match the conventions of the file/module you are editing.

### 2.4 Comments explain *why*, not *what*
- Do not narrate the code. Comment non-obvious intent, invariants, trade-offs,
  and the failure modes a future reader must not reintroduce. The best comments
  in this repo (see `ohlcv_store.py`, `tick_parquet_store.py`) explain the
  operational hazard a piece of code defends against — write more of those.

### 2.5 No magic constants
- Tunable values belong in config or named module-level constants with a comment
  on their unit and rationale (`AGENT_STRATEGY_RULES.md` §20). A bare `8`, `900`,
  or `0.10` in logic is a review failure.

### 2.6 Don't over-engineer
- No abstraction ahead of need; no config flags that won't be used in V1
  (`AGENT_STRATEGY_RULES.md` §3.4). Simplicity is a feature.

---

## 3. Error Handling & Observability

This is where the data pipeline keeps failing. Treat this section as mandatory.

### 3.1 Never swallow exceptions
```python
# BAD — failure vanishes, pipeline looks healthy while it silently stalls
try:
    rows = store.flush_from_h5(path, symbol, exchange)
except Exception:
    pass

# GOOD — surface it, with enough context to diagnose without a debugger
try:
    rows = store.flush_from_h5(path, symbol, exchange)
except Exception as exc:
    logger.warning("Parquet flush failed for %s/%s at %s: %s",
                   exchange, symbol, path, exc)
    # decide explicitly: retry, skip-and-count, or re-raise — never ignore
```

### 3.2 Catch narrowly, act explicitly
- Catch the specific exception you expect (`OSError`, `ClientError`, …), not bare
  `Exception`, unless you are at a top-level boundary that logs and re-raises.
- On every caught error, make a deliberate choice and document it in code:
  **retry**, **skip-and-count**, **quarantine**, or **abort**. "Continue silently"
  is never one of the choices.

### 3.3 Count and surface everything you drop
- Any skip/drop/dedupe/truncate must increment a counter that is logged. Borrow
  the pattern already used in `s3_sync.sh` (`UPLOADED`/`FAILED`) and
  `tick_parquet_store._flush_dataset` (read-failure streak before skip).
- A producer that reports activity while its consumer sees nothing must emit a
  throttled warning (see the bubble-pipeline `failure_stage` instrumentation,
  `AGENT_STRATEGY_RULES.md` §11.4).

### 3.4 Log levels mean something
- `ERROR`: data was or will be lost; human action needed.
- `WARNING`: degraded/recoverable; self-healing in progress.
- `INFO`: normal lifecycle milestones (startup, flush counts, rotation).
- `DEBUG`: per-iteration detail.
- Do **not** log per-write spam at `INFO` (the `aiobotocore` credential-log flood
  that filled the EC2 disk is a documented incident — `DEPLOYMENT.md`).

### 3.5 Health must be observable, not inferred
- Long-running workers must expose a heartbeat/health signal that distinguishes
  "running" from "running and actually making progress" (the freeze had a
  *healthy* container whose flush thread was a no-op). Prefer "last successful
  unit of work" timestamps/counters over mere liveness.

---

## 4. Data-Pipeline & Storage Reliability

These rules are derived directly from incidents. Violating them is how the
collector "ran for weeks without working."

### 4.1 Persistent progress state must be self-validating
Watermarks, offsets, cursors, and checkpoints encode an assumption about an
external resource. When that resource is replaced (file rotated, volume reset,
schema bumped) the state becomes a landmine.

- **Detect the discontinuity and recover.** Example (the rotation fix in
  `tick_parquet_store._flush_dataset`): a stored row-watermark greater than the
  current row count means the file was rotated → reset to 0 and re-flush, don't
  freeze.
- Make recovery **idempotent** (de-dup on write) so re-processing is safe.
- Add a test that simulates the discontinuity (see
  `tests/test_tick_parquet_store.py::TestRotationResetsWatermark`).

### 4.2 Treat every external file as possibly corrupt or mid-write
- Files synced from S3 / written by another process may be truncated or have
  inconsistent metadata (the live HDF5 is uploaded mid-write; its `trades` chunk
  index can be unreadable even though the file is 8 GB).
- Read defensively: chunk-by-chunk with per-chunk error handling so one bad block
  doesn't abort the whole read (`notebooks/utils._safe_read_2d`). Quarantine
  corrupt inputs, don't overwrite them (`tick_parquet_store._append_parquet`).

### 4.3 An immutable, queryable mirror is the durable store — and it must not depend on a fragile source
- The canonical fast store (HDF5) is fragile; the per-day Parquet mirror is the
  durable one. New analysis reads Parquet by date window, not multi-GB HDF5
  (`notebooks/utils.load_ticks_parquet`).
- **The durable store must not depend on reading a concurrently-written, mutable,
  corruption-prone file.** The 2026-06 freeze happened because durability flowed
  `HDF5 → separate reader → Parquet`; one corrupt HDF5 silently froze the mirror
  for 10 days. The mirror is now fed **directly from the live feed**
  (`LiveParquetMirror`); HDF5 is disposable. Full post-mortem:
  `docs/INCIDENT_2026-06_tick_pipeline.md`. Do not reintroduce a
  read-from-fragile-source durability path.
- Anything that maintains the mirror must guarantee forward progress and be
  monitored for staleness (see §3.5 and `DEPLOYMENT.md` pipeline-health checks).

### 4.4 Bound everything that grows
- Every buffer, cache, history, and on-disk dataset needs an explicit cap and an
  eviction/rotation policy with a documented "what happens at the limit"
  (`AGENT_STRATEGY_RULES.md` §9.1.1). HDF5 rotation, local Parquet retention, and
  Docker log caps are existing examples — match that discipline for anything new.

### 4.4b Bound per-operation cost, and measure it under realistic full load
- A unit test with 5 rows proves correctness, not viability. Any code on a hot
  path (per-event, per-flush, per-tick) must have a cost — CPU **and memory** —
  that does **not** grow with the total accumulated data. Re-reading,
  re-sorting, or re-deduplicating a whole day on every flush is an O(day)
  operation masquerading as O(batch); it scales into an OOM as volume grows.
- Profile against a realistic *full-day / peak-load* profile before shipping.
  Concretely: the live mirror writer was measured over a simulated 6 M-row day —
  a pandas read-modify-write accumulator peaked at **2.6 GB** RSS; the chunked
  pyarrow accumulator that shipped peaks at **~0.35 GB**. That difference is the
  line between "stable on a 4 GB box" and "nightly OOM." Record the numbers in
  the relevant doc.

### 4.5 Idempotency and recovery are features, not afterthoughts
- Re-running a sync, flush, or backfill must not duplicate or corrupt data.
- Provide a manual recovery path for every automated pipeline (e.g.
  `collect_ticks.py --backfill-parquet`). If you build an automated flow, you own
  its break-glass procedure and it goes in `DEPLOYMENT.md`.

### 4.6 Verify the pipeline produces *recent* data, not just *some* data
- "Files exist in S3" is not "the pipeline works." Check that the latest day is
  current and that row counts are advancing. A check that would have caught the
  freeze on day one belongs in the deploy runbook (it now does — `DEPLOYMENT.md`).

---

## 5. Dependencies & Environment

The first symptom in the 2026-06 investigation was an `s3fs`/`aiobotocore`
vs `botocore` version mismatch that broke S3 Parquet listing.

- **Pin and keep compatible.** `s3fs`/`aiobotocore` are tightly coupled to a
  `botocore` range; bumping one without the others breaks S3 access. Verify the
  matrix after any change to AWS/dependency versions.
- **One environment of record.** The notebook kernel, the container, and CI must
  agree. Don't debug against a different interpreter than the one that runs the
  code (the kernel was Anaconda; `pip` pointed elsewhere).
- **Add dependencies via the package manager at a real version**, never a guessed
  pin. Record why a pin exists if it's non-obvious.

---

## 6. Testing (summary — full detail in `docs/TESTING_GUIDE.md`)

- Every meaningful behaviour change requires a test; every bug fix requires a
  regression test for the failure path.
- Data/infra code is testable too: simulate rotation, truncation, corrupt inputs,
  missing files, and version skew with synthetic fixtures (no network, no real
  S3). See `tests/test_tick_parquet_store.py` and `tests/test_collect_ticks.py`.
- Determinism contract for strategy code is non-negotiable
  (`AGENT_STRATEGY_RULES.md` §7, `TESTING_GUIDE.md` §2.2).
- Never disable a test to make a change "pass." Fix the code or fix the test and
  document why.

---

## 7. Operational Code (collectors, sync jobs, deployment scripts)

- **Assume the process will be killed mid-write** (Docker stop, OOM, rotation,
  SIGTERM). Design for clean shutdown and crash-safe restart. Use atomic
  write-then-rename for files (`*.tmp` → final), as the existing stores do.
- **Make restarts safe and self-healing**, not dependent on manual cleanup.
- **Every cron/timer/worker must log a clear start/end with a result summary**
  and exit non-zero on failure so the supervisor surfaces it (`s3_sync.sh` is the
  model: it fails loudly when `S3_BUCKET` is unset rather than exiting 0).
- **Shell scripts:** `set -euo pipefail`, quote variables, and never delete local
  data before confirming the durable copy succeeded (`s3_sync.sh` deletes Parquet
  only after a confirmed S3 sync).
- **Document the operational contract** of anything you add (inputs, env vars,
  failure behaviour, recovery) in `DEPLOYMENT.md`.

---

## 8. Change Workflow & Definition of Done

A change is **done** only when all of the following hold (full table:
`AGENT_STRATEGY_RULES.md` §12):

- [ ] Compiles / imports cleanly; relevant tests pass.
- [ ] New tests cover the new behaviour **and** the failure path fixed.
- [ ] No unresolved stubs/TODOs in the changed scope.
- [ ] No swallowed errors; all drops counted and logged.
- [ ] Persistent state changes are self-healing and idempotent.
- [ ] Linter clean on changed files.
- [ ] Docs updated where behaviour changed (esp. `DEPLOYMENT.md` for ops changes).
- [ ] Change is narrow and review-ready; rationale captured.

**When unsure, ask.** A wrong assumption in data-collection code costs days of
lost, unrecoverable market data — clarify scope or intent before guessing.
