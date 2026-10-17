# AGENTS.md

Guidance for AI agents and developers working in this repository. Read this
first, then the documents it points to **before** making changes.

## Read before you code

| Read this | For |
|---|---|
| `docs/CODING_STANDARDS.md` | The engineering quality bar — error handling, data-pipeline & operational reliability, Definition of Done. **Applies to every change.** |
| `docs/AGENT_STRATEGY_RULES.md` | Operational rules for the **frozen historical V1** engine: determinism, hot-path latency, stub policy (§3.3b), Definition of Done (§12). |
| `docs/TESTING_GUIDE.md` | How to run/write/organize tests. Its strategy citations refer to the frozen V1. |
| `docs/DEPLOYMENT.md` | EC2 / Docker / S3 collector runbook and recovery procedures. |
| `strategy.md` | Current research programme and strategy contract (source of truth for new work). |
| `docs/history/strategy_v1.md` | Archived V1 specification. Authority for the frozen baseline only. |
| `docs/implementation_plan.md` | Build plan for that frozen V1 baseline, not for the current research programme. |
| `CONTEXT-MAP.md` | Boundary between the research programme and the separate OANDA strategy. |

## Source-of-truth order

Current research: `strategy.md` → research tests → `docs/CODING_STANDARDS.md`.

Frozen V1 baseline: `docs/history/strategy_v1.md` → `docs/implementation_plan.md` → its tests → `docs/AGENT_STRATEGY_RULES.md`.

Do not change frozen V1 behaviour merely because it differs from an unvalidated research hypothesis. Do not implement the current programme by extending V1 trade archetypes. A contradiction inside one of these two scopes is a defect in that scope.

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
