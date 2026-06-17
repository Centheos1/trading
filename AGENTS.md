# AGENTS.md

Guidance for AI agents and developers working in this repository. Read this
first, then the documents it points to **before** making changes.

## Read before you code

| Read this | For |
|---|---|
| `docs/CODING_STANDARDS.md` | The engineering quality bar — error handling, data-pipeline & operational reliability, Definition of Done. **Applies to every change.** |
| `docs/AGENT_STRATEGY_RULES.md` | Tide/Wave/Ripple architecture, determinism contract, hot-path latency rules, stub policy (§3.3b), Definition of Done (§12). |
| `docs/TESTING_GUIDE.md` | How to run/write/organize Python and C++ tests. |
| `docs/DEPLOYMENT.md` | EC2 / Docker / S3 collector runbook and recovery procedures. |
| `strategy.md` | Canonical strategy design, math, and contracts (source of truth). |
| `implementation_plan.md` | Build plan, phases, acceptance criteria. |

## Source-of-truth order

`strategy.md` → `implementation_plan.md` → tests → `docs/AGENT_STRATEGY_RULES.md`
(strategy / hot path) and `docs/CODING_STANDARDS.md` (general + data/infra) →
`docs/TESTING_GUIDE.md`. If code contradicts `strategy.md`, the code is wrong.

## The rules that keep getting broken (see `docs/CODING_STANDARDS.md`)

1. Fail loud, never silent — no swallowed exceptions, no success-shaped returns on failure.
2. No silent data loss — count and log everything you skip/drop; quarantine bad input.
3. Persistent state (watermarks/caches/offsets) must self-heal across rotation/reset, not freeze.
4. Treat external files as possibly corrupt/mid-write; read defensively.
5. Every behaviour change — and every bug fix's failure path — ships with a test.
6. Verify pipelines produce *recent* data, not just *some* data.
7. Narrow changes, green tree, and update any doc whose behaviour you changed.

## Definition of Done

`docs/AGENT_STRATEGY_RULES.md` §12 (and the stub policy §3.3b) apply to **all**
code, not just strategy code. A task with an unresolved stub/TODO in scope is not
complete.

**When unsure of scope, preferences, or intent — ask.** A wrong assumption in
data-collection code can cost days of unrecoverable market data.
