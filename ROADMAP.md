# AgencyOS — Roadmap

This document describes what to build, in what order. It changes often.
For the stable long-term design, see `ARCHITECTURE.md`.

Build one phase at a time. Do not start a phase until the previous one meets
its Definition of Done.

---

## Phase 0 — Prove the Loop [COMPLETE ✅]

```
Opportunity → Planner → Worker → Reviewer → Approval → Done
```

- **Opportunity**: pulled from a single source (e.g. one freelance marketplace or GitHub issue search)
- **Planner**: takes the opportunity and outputs a structured task — `task`, `priority`, `expected_output`, `estimated_cost`. No reasoning beyond this; it's a shaping step, not a strategist.
- **Worker**: executes the task (one LLM call doing one job)
- **Reviewer**: checks the worker's output against the planner's expected output before it goes to a human
- **Approval**: a human approves or rejects (CLI prompt or single web page)
- Every step logged.

Skip entirely: Kernel abstraction, Executive Board, Policy Engine, Memory, Knowledge Graph, Asset Library, `shared/`.

**Definition of Done:**
- [x] 5 consecutive successful executions
- [x] Average cost per task under $0.25
- [x] Zero manual code changes during execution
- [x] All outputs and decisions logged
- [x] Human approval gate functioning correctly (rejects block, approvals proceed)

---

## Phase 0.5 — Hardening [COMPLETE ✅]

Before building kernel abstractions or adding departments, harden the core loop by running real workloads continuously and breaking edge cases.

- **Failure Testing & Resiliency**:
  - API 429 / rate limits & network outage fallback handling.
  - Worker exception isolation and graceful recovery.
  - Malformed payload & missing issue field handling.
- **Reliability Metrics**:
  - Aggregated loop telemetry: Success Rate, Mean Task Time, Mean Review Time, Average Cost, Retry Counts, Approval Rate.
- **Budget Guard**:
  - Enforce hard cost limits (per-task $0.25 ceiling, persistent daily budget limit). Block execution before calling worker if estimated cost exceeds budget.

**Definition of Done:**
- [x] Resilient retry & crash isolation: 50+ real/simulated workload tasks processed continuously without manual crash intervention
- [x] Hard budget limits automatically reject tasks exceeding per-task ($0.25) or daily ($2.00) ceilings before worker invocation
- [x] Loop telemetry (success rate, failure rate, mean execution time, total cost) tracked in structured `TelemetryReport` JSON objects

---

## Phase 0.6 — Data-Driven Opportunity Filtering [COMPLETE ✅]

> **Governing Principle**: Every new subsystem must be justified by operational evidence, not architectural prediction.

- Run the hardened loop across 100+ real live GitHub opportunities.
- Empirical statistical analysis across 104 real reviewed opportunities revealed insufficient failure data to justify a length-based filter (correlation r = 0.27; scores fell in a narrow passing band of 0.876–0.980 with zero bimodal separation).
- `quality.py` rejects only `description_length == 0` (empty body) as the sole empirically supported threshold. Tag-based rejection (`duplicate`, `stale`, `wontfix`, `invalid`) is retained as a structural default based on face validity, pending future failure evidence.

**Definition of Done:**
- [x] 105 real tasks executed across 5 live repositories and logged
- [x] Empirical correlation analysis conducted (r = 0.27; zero bimodal split in review scores)
- [x] `src/quality.py` implemented with `description_length == 0` rejection and structural tag defaults
- [x] `REVIEW_COMPLETED` events updated to log explicit `passed` boolean and `outcome` signals
- [x] Full test suite (`tests/test_phase0_6.py`) passing cleanly alongside regression suite

---

## Phase 0.7 — Autonomous Operations [UNVERIFIED — SUPERSEDED BY PHASE 0.8]

Transition AgencyOS from a manually triggered execution loop into a restart-safe autonomous service that operates without a live terminal while preserving correctness across failures.

This phase validates **behavior**, not uptime. Phase 0.8 validates uptime.

> ⚠️ **Verification flag — Phase 0.7 protections failed when exercised.**
> A deep line-by-line audit of the 2026-08-04 VPS soak run confirmed that
> Phase 0.7's `AutonomousEngine` was actively running (processing 222
> 10-step pipeline tasks post-blackout), but its own protections failed:
> `tasks`, `audit_log`, and `idempotency_keys` SQLite tables remained at 0
> rows; quality-rejected opportunity `4335223464` was re-planned and completed
> TWICE more via `AutonomousEngine`'s 10-step pipeline; and opportunities
> `4844615862` and `4827149379` re-executed 2 additional times post-blackout
> (totaling 12x executions each). Phase 0.7 is **unverified and broken in practice**
> until Phase 0.8 Engine Stabilization achieves full persistence, idempotency,
> and state gate enforcement.

A speculative "Side Effect Executor" (real external actions — PRs,
commits, etc.) was scoped as part of this phase and then cut before any
real external action existed to justify it — see "Do not implement"
below. Deferred until a completed, approved task actually needs to touch
the outside world.

### Components

**Scheduler**
- Runs on a configurable interval (Developer Mode default: 30 seconds)
- Creates execution events
- Never executes work directly
- Prevents duplicate scheduling

**Persistent Task State Machine (SQLite)**

Persist task lifecycle:

NEW
→ DISCOVERED
→ PLANNED
→ READY
→ EXECUTING
→ REVIEWED
→ WAITING_APPROVAL
→ APPROVED
→ DELIVERED
→ COMPLETED

Failure states:

FAILED
RETRYING
CANCELLED
EXPIRED

**Persistent Approval Queue (SQLite)**

Approvals are persisted as rows:

- PENDING
- APPROVED
- REJECTED

Never use interactive terminal prompts.

**Idempotency Guard**

Scope ONLY to Worker and Reviewer LLM calls.

Persist keys such as:

WORKER:<task_id>:v1

REVIEW:<task_id>:v1

Do **not** implement:

- Side Effect Executor
- PR idempotency
- Email idempotency
- Invoice idempotency
- Blog publishing idempotency

Those belong to future phases. Only document this architectural extension point inside `ARCHITECTURE.md` if necessary; do not implement it.

**Operational Watchdog**

Responsibilities:

- Detect repeated failures
- Apply exponential backoff
- Temporarily disable unhealthy opportunity sources
- Re-enable after cooldown
- Log every action

The watchdog must never terminate the application.

**Health Monitor**

Expose structured JSON only.

Metrics:

- queue_depth
- running_tasks
- pending_approvals
- disabled_sources
- failure_rate

No dashboard.

**Startup Recovery Routine**

Implement a startup recovery function (not a new Kernel service or directory).

Runs once at startup.

Responsibilities:

- Load unfinished tasks
- Inspect persisted task state
- For EXECUTING tasks, verify Worker/Reviewer idempotency keys before resuming
- Reload approval queue
- Ignore COMPLETED tasks
- Never execute business logic itself

### Database Layout

Use separate SQLite tables.

Do not mix responsibilities.

Required tables:

- tasks
- approvals
- idempotency_keys
- audit_log
- budget

### Definition of Done

None of the items below are trustworthy as written — every one of them
is exactly what the verification flag above found broken in practice.
Superseded by Phase 0.8's checkpointed DoD, which re-establishes each of
these guarantees with independent verification against raw artifacts,
not self-reported checkmarks. Left unchecked deliberately, as a record of
what was claimed vs. what held up:

- [ ] Scheduler executes tasks automatically on interval
- [ ] Task state survives restart
- [ ] Approval queue survives restart
- [ ] Watchdog backs off repeated failures
- [ ] Disabled sources recover after cooldown
- [ ] No duplicate execution after restart
- [ ] No duplicate Worker or Reviewer billing after restart
- [ ] Verified by forcibly terminating the process (SIGKILL / kill -9) during EXECUTING and confirming correct recovery
- [ ] Budget enforcement remains correct after restart
- [ ] Live GitHub fetching from Phase 0.6 remains unchanged in normal operation
- [ ] Fixtures/mocks are used ONLY inside the SIGKILL crash-recovery test harness to isolate state-machine correctness from network variability

See Phase 0.8's checkpoints (0.8A1 through 0.8E) for the actual,
independently-verified status of each of these guarantees.

---

## Phase 0.8 — Engine Stabilization [FAILED FIRST ATTEMPT ❌ — BLOCKING]

> **Governing Principle**: A loop that produces plausible-looking telemetry is not the same as a loop that is correct. Verify against raw artifacts — DB rows and log events, not summaries — every time.

**Status**: This phase was previously logged as "Stability Validation: IN PROGRESS." A full forensic audit of a real 24h+ VPS run (`audit_log.jsonl`, 5,293 events + `agencyos.db`) was completed on 2026-08-04 and independently re-verified line-by-line. It failed.

### What the audit found (verified against raw artifacts, not the summary report)

- **No idempotency**: 162 unique opportunities produced 379 executions. 64 opportunities ran 3x, 2 ran 12x, 3 ran 10x, purely because the loop re-treats already-seen opportunities as new on every fetch cycle.
- **Rejection gates don't stick**: Opportunity `4878017272` was explicitly REJECTED by human approval (`TASK_BLOCKED`), then re-planned and approved 9 more times afterward. Opportunity `4335223464` was rejected by the quality scorer, then fully executed to completion twice more anyway.
- **Persistence bypass**: `agencyos.db` `tasks`, `audit_log`, and `idempotency_keys` tables were confirmed 0 rows despite 379 real task executions in the log. The `approvals` (13 rows) and `budget` (10 rows) tables were only populated in a single ~4-second burst at the very end of the run — not written live — and contain degraded data (`budget.opportunity_id = 'unknown'` on all 10 rows; several `approvals` rows reference non-opportunity IDs like `'1'`, `'4'`, `'5'`).
- **Silent 5.07-day blackout**: A 438,057-second (121.68h) gap between two consecutive log events, with no crash/SIGKILL/recovery event logged on either side — the engine just stopped and was manually restarted later.
- **Telemetry drift**: All 7 `TELEMETRY_REPORT` events were confirmed wrong against raw counts (e.g. reporting `total_tasks: 105` when only 53 or 157 tasks had actually been planned at that point), and every report showed `approval_rejected_executions: 0` despite a confirmed real rejection.
- **Dual, conflicting state machines**: A 6-step pipeline and a 10-step pipeline (`TASK_READY`/`TASK_EXECUTING`/`TASK_WAITING_APPROVAL`/`TASK_APPROVED`/`TASK_DELIVERED`) coexist with no single source of truth for task state.

> **Root cause for nearly all of the above**: The execution loop has no authoritative, persisted state to check before acting. Everything else (duplication, bypassed gates, drifted telemetry) follows from that one gap.

### Single source of truth

**Persistence Layer Rule**: No module may directly INSERT, UPDATE, or DELETE task state. All task state mutations must pass through a single persistence service/repository that is responsible for transactions, audit logging, state validation, idempotency checks, and constraint enforcement.

No in-memory task state is authoritative. Every execution decision (is this opportunity new, is this task already running, was this rejected) must originate from a query against persisted database state, not a Python dict/set/counter held in the process. In-memory caches are allowed only as a read-through of the DB, never as the thing checked before acting. This is the one rule that, if violated, silently reopens every bug found in the 2026-08-04 audit — hold implementers to it explicitly.

### Freeze during this phase

No new opportunity sources, no new departments, no Executive Board logic, no Hermes migration, no Phase 1 work. This is the only phase in flight until 0.8E passes.

### Explicit state machine

One pipeline. This is the entire legal transition graph — anything not on this list is an illegal transition and must be rejected/logged, not silently allowed:

```
DISCOVERED → PLANNED → READY → EXECUTING → REVIEW → WAITING_APPROVAL
    → APPROVED → DELIVERED → COMPLETED
```

Terminal states (zero outgoing transitions — a task in one of these can never move again without an explicit, logged manual override):

- `COMPLETED`
- `BLOCKED` (human approval rejection)
- `QUALITY_REJECTED` (quality scorer rejection)
- `WORKER_FAILED`

Every task must reach exactly one terminal state, exactly once. A task showing `COMPLETED` twice, or `BLOCKED` followed later by `COMPLETED`, is by definition a bug this phase exists to catch.

### Structure: checkpoints, in order

Do not start a checkpoint until the previous one's items are re-verified against raw artifacts. Each checkpoint should be its own commit / PR so a regression is traceable to a specific layer. 0.8A is split in two deliberately — schema and live persistence are different pieces of work, and an agent asked to do both at once tends to rewrite persistence logic before the schema it depends on actually exists.

#### 0.8A1 — Database Schema
All schema changes must be versioned through numbered migration files (e.g. `001_initial.sql`, `002_add_state_constraints.sql`) and be reversible where practical. Schema changes must never require deleting production data.

*Schema only. No persistence-writing code changes yet.*

- [ ] `tasks.state` constrained to the explicit state machine above via `CHECK(state IN ('DISCOVERED','PLANNED','READY','EXECUTING','REVIEW','WAITING_APPROVAL','APPROVED','DELIVERED','COMPLETED','BLOCKED','QUALITY_REJECTED','WORKER_FAILED'))`.
- [ ] `UNIQUE(opportunity_id)` on `tasks` (or `UNIQUE(key)` on `idempotency_keys`, whichever is the enforcement point).
- [ ] `NOT NULL` on `opportunity_id` in `budget` and `approvals`.
- [ ] `FOREIGN KEY` from `budget.opportunity_id` and `approvals.opportunity_id` back to `tasks.opportunity_id`. SQLite must reject an orphan write, not just Python.
- [ ] Indexes on `tasks.opportunity_id`, `tasks.state`, and `audit_log.timestamp` (the columns everything in 0.8C/0.8D will query against).
- [ ] A migration script that applies cleanly to a copy of the audited `agencyos.db` without data loss.
- [ ] Schema verified with `PRAGMA foreign_key_check` and a manual attempt to insert an invalid row (bad state, duplicate `opportunity_id`, orphan FK) — each attempt must fail.

#### 0.8A2 — Live Persistence & Idempotency
*Only start this once 0.8A1's schema is in place and verified.*

- [ ] Every state transition that mutates task state executes inside a single SQLite transaction — audit row, task update, budget row, and approval row (whichever apply) all commit together or none do:
  ```sql
  BEGIN;
    INSERT INTO audit_log (...);
    UPDATE tasks SET state = ... WHERE opportunity_id = ...;
    INSERT INTO budget (...);      -- if applicable
    INSERT INTO approvals (...);   -- if applicable
  COMMIT;
  ```
  No sequence of `update task` ... `crash` ... `insert audit` is possible — the 2026-08-04 audit's partial/batched writes are exactly what this eliminates.
- [ ] Row count in the `audit_log` table == event count in `audit_log.jsonl` at any point mid-run, not just at shutdown.
- [ ] Execution eligibility is determined by SQL, not Python state. Before planning any opportunity, the scheduler must run something equivalent to:
  ```sql
  SELECT state FROM tasks WHERE opportunity_id = ?;
  ```
  If the result is `COMPLETED`, `BLOCKED`, `QUALITY_REJECTED`, or `WORKER_FAILED`, the scheduler skips it. No Python set/dict/in-memory cache may be consulted first or instead — see "Single source of truth" above. This is deliberately stricter than "add a UNIQUE constraint," because a constraint only catches the duplicate at insert time; this stops the duplicate work from starting at all.
- [ ] `budget.opportunity_id` is populated with the real opportunity ID on every row — the FK constraint from 0.8A1 should make `'unknown'` rows impossible to insert.
- [ ] Fresh test run: max executions per opportunity == 1, or an explicit logged retry reason for anything higher.

#### 0.8B — State Machine, Scheduler & Recovery
- [ ] Illegal transitions are rejected at the point of write (using the explicit state machine above), not just observable after the fact — an attempted transition from a terminal state raises/logs an error instead of silently succeeding.
- [ ] Blackout/stall detection with an observable recovery lifecycle, not just a single event type. Log each of:
  - `WATCHDOG_WARNING` — heartbeat is late but under the hard threshold
  - `STALL_DETECTED` — threshold exceeded (e.g. 5x normal heartbeat interval) with no heartbeat or task event
  - `RECOVERY_STARTED` — process restarted / resumed after a stall
  - `RECOVERY_COMPLETED` — scheduler confirmed resumed from persisted state, with counts proving nothing was lost or duplicated
- [ ] Idle heartbeat backoff: heartbeats during genuine idle periods back off (e.g. exponential up to a ceiling) instead of firing on a fixed ~34s interval regardless of `queue_depth`.
- [ ] Crash recovery test, run explicitly, not assumed: start the engine, let it run ~5 minutes of real load, `kill -9` the process, restart it, and verify against the DB — no duplicate tasks, no missing tasks, scheduler resumes from persisted state correctly, no budget corruption, no telemetry corruption, and the `RECOVERY_STARTED`/`RECOVERY_COMPLETED` events are present. Log the before/after counts used to confirm this.

#### 0.8C — Telemetry & Budget Accuracy
- [ ] Telemetry computed from the DB, not in-memory counters. Every `TELEMETRY_REPORT` value (`total_tasks`, `successful_executions`, `approval_rejected_executions`, `total_cost`) is a live query against persisted state and matches raw counts exactly at the time it's generated.
- [ ] Budget accuracy: `today_cumulative_spend` reconciles exactly against `SUM(worker.actual_cost + review.review_cost)` over the same window, pulled from the DB, not carried forward in memory.

#### 0.8D — Automated Verification, Replay & 24h Soak
- [ ] Build `tools/verify_audit.py agencyos.db audit_log.jsonl` — a standalone forensic verifier, so every future soak run is checked the same way this one was, without manually reading thousands of log lines. It must fail (non-zero exit) if it finds any of: duplicate opportunity IDs beyond a logged retry, telemetry mismatch vs. raw counts, orphan approvals/budget rows (no matching task), missing audit rows (DB count != JSONL count), impossible state transitions, DB/log count mismatch, budget mismatch, an unexplained heartbeat gap, or a stall with no `WATCHDOG_WARNING`/`STALL_DETECTED` event — plus two more checks:
  - Timestamps never go backwards: `timestamp[n+1] >= timestamp[n]` for every consecutive pair of events.
  - Exactly one terminal state per task: no `opportunity_id` may show `COMPLETED` twice, or a terminal state followed by any further transition.
- [ ] Build `tools/replay_audit.py agencyos.db audit_log.jsonl` — reconstructs task state purely by replaying `audit_log.jsonl` from scratch, then diffs the replayed state against what's actually in `agencyos.db`. Any difference is a FAIL — this catches DB corruption or a persistence bug that the live checks above didn't.
- [ ] 24-hour soak, re-audited from scratch. A fresh continuous run on clean artifacts (new `agencyos.db` and `audit_log.jsonl`), then both `tools/verify_audit.py` and `tools/replay_audit.py` run against it, plus a manual spot-check the same way the 2026-08-04 audit was done. Every soak going forward is: run 24h $\rightarrow$ run both tools $\rightarrow$ PASS or FAIL, not a manual log read.
- [ ] PASS — 0.8A1 through 0.8D all hold simultaneously on the same soak run, confirmed by both tools reporting zero failures and a manual spot-check of raw `agencyos.db` + `audit_log.jsonl`.
- [ ] `verify_audit.py` completes in under 30 seconds on a 24-hour audit.
- [ ] `replay_audit.py` completes in under 60 seconds.
- [ ] Scheduler heartbeat query remains under 100 ms with a 24-hour database.

#### 0.8E — Regression Suite

Every bug found in the forensic audit gets its own permanent test.

Examples:
- duplicate opportunity cannot execute twice
- blocked task cannot restart
- quality rejected task cannot restart
- telemetry equals DB
- audit_log DB == JSONL
- replay has zero diffs
- crash recovery preserves state
- FK constraints reject invalid rows
- illegal state transition throws
- `UNIQUE(opportunity_id)` enforced

Do not mark this phase — or any checkpoint within it — complete on the strength of a passing test suite alone (see PROJECT_STATUS.md — this has burned the project twice already). Require the raw DB + log artifacts.

### Hermes gate

Strictly sequential — no parallel work, no "almost done":

```
0.8A1 PASS → 0.8A2 PASS → 0.8B PASS → 0.8C PASS → 0.8D PASS → 0.8E PASS
  → fresh 24-hour soak on clean artifacts PASS
  → verify_audit.py: zero failures
  → replay_audit.py: zero diffs
  → manual audit PASS
  → only then: Hermes/Phase 1 begins
```

No exceptions for "it's probably fine now" — re-run both tools on fresh data before Hermes work starts.

---

## Phase 1 — Kernel Foundations

Only build what Phase 0/0.5 is genuinely straining against:
- **Event Bus** — only if more than one worker needs to react to task state
- **Policy Engine** — starts as a config file of rules, not a service

> *Note: Audit Log and Budget Manager are already fully satisfied by `src/logger.py` and `src/budget_guard.py` built in Phase 0 / 0.5 — no rebuild needed.*

**Definition of Done:**
- [ ] Phase 0 loop runs unattended except at defined approval gates
- [ ] A cost-per-task number is visible for every completed task

---

## Phase 2 — First Real Business Unit

Pick one unit (Software Agency) and build it properly:
- Manager (routes tasks to the right worker)
- 2–3 workers with distinct roles
- SOPs as versioned prompt templates
- KPIs (tasks completed, approval rate, revenue if applicable)

Do not build other units yet.

**Definition of Done:**
- [ ] This unit could run as a standalone product on its own

---

## Phase 3 — Executive Layer (as logic, not agents)

Implement CEO/COO/CFO/CTO decisions as functions/config, not LLM agents:
- COO logic → task assignment rules
- CFO logic → budget approval thresholds
- CTO logic → which model/tool for a task type

Convert to a real reasoning agent only once the rule-based version is a
demonstrated bottleneck.

**Definition of Done:**
- [ ] Adding a second business unit (Phase 5) requires config changes only,
      not code changes to this layer

---

## Phase 4 — Learning & Assets

- Retrospective after each task (what worked, what didn't)
- Asset Library: tagged store of reusable outputs (templates, docs, snippets)
- Retrospectives feed back into Phase 2's SOPs/templates

**Definition of Done:**
- [ ] The 10th task in a unit is measurably faster or cheaper than the 1st

---

## Phase 5 — Scale Out

Only now:
- Second business unit, using Phase 2's unit as the template
- Marketing Engine (start with one channel)
- Extract to `shared/` the first time two units genuinely need the same thing
- Internal Marketplace between units

---

## Explicitly Deferred

Write this into the repo README so nobody — human or agent — "helpfully"
builds it early:

- Model Router, Marketplace Connectors, Vector DB
- Most of Platform Services (auth, notifications, storage as shared modules)
- 6 of the 8 original Business Units
- Executive Board as reasoning agents (stays rule-based until proven insufficient)
- Hermes migration (planned for right before Phase 1, per `PROJECT_STATUS.md`
  — but not before Phase 0.8's Hermes gate passes; migrating an unstable
  engine just moves the same bugs onto a new platform)

There is no dedicated "Infrastructure phase." Infrastructure is extracted
into `shared/` only when two real callers need the same thing — see the
Governing Rule in ARCHITECTURE.md.

---

## Next Prompt for the Agentic IDE

```
Read ARCHITECTURE.md, ROADMAP.md, and PROJECT_STATUS.md completely. Treat
ARCHITECTURE.md as the stable source of truth, ROADMAP.md as the
implementation plan, and PROJECT_STATUS.md as the current session state
— it is ahead of what's checked into ROADMAP.md's checkpoint text in a
few places, so trust it for "what's actually done" and ROADMAP.md for
"what each checkpoint requires."

Phases 0 through 0.6 are done. Phase 0.7 is resolved — do NOT re-open
the reconciliation investigation; PROJECT_STATUS.md already documents the
finding (AutonomousEngine itself reproduced the bugs, not just wasn't
wired in) and ROADMAP.md's Phase 0.7 section has been corrected to match.
Phase 0.8's checkpoints 0.8A1 through 0.8C are COMPLETE and independently
verified — do not redo, re-litigate, or re-implement any of them. **0.8D
is NOT complete** — its tools (verify_audit.py, replay_audit.py) are
built and independently verified against real corruption cases, but per
ROADMAP.md's own DoD, 0.8D's final and defining item is a clean 24-hour
soak with both tools reporting zero failures against real output ("PASS
— 0.8A1 through 0.8D all hold simultaneously on the same soak run") —
that has not happened yet. Do not mark 0.8D complete, do not start 0.8E
formally, and do not touch the verifier/db/schema code without checking
PROJECT_STATUS.md first — there is almost certainly already a specific,
hard-won reason those look the way they do.

CURRENT STATE: a second 24h+ soak is running on the VPS (started
2026-08-07 14:52:33 UTC, target completion 2026-08-08 14:52:33 UTC) as
the official Phase 0.8 evidence run — a first soak ran clean
operationally but was invalidated by a JSONL-sync gap since fixed (see
PROJECT_STATUS.md for the full incident). The IDE does not control the
VPS and should not attempt to. Do not touch main.py, src/db.py,
src/logger.py, src/scheduler.py, src/engine.py, or anything else that
could affect what's currently running, until the soak completes and its
results are reviewed.

WHAT TO WORK ON NOW: pre-emptive 0.8E (Regression Suite) test scaffolding
only — 0.8E formally starts once 0.8D's soak passes, but the tests
themselves target bugs already found and fixed, so writing them now
doesn't depend on the soak's outcome. One permanent test per historical
bug already found across this phase (the list is long and already known:
0.8A1's 14-stub-row test isolation leak, 0.8A2's worker-idempotency NULL
bug, 0.8B's missing REVIEW→REVIEW transition and watchdog warning-reset
bug, 0.8C's BLOCKED double-counting, 0.8D's dispatch-table gap and
interval off-by-one, the JSONL-sync gap and its AuditLogger double-write
near-miss, and the reverted opportunity.py mock-fallback incident). These
tests exercise already-fixed code — do not change the underlying
implementation to make a new test pass; if a regression test fails
against current code, that's a real regression, report it before
touching anything.

Do not start Phase 1 or Hermes migration. The Hermes gate requires the
soak to complete cleanly, both verification tools to run clean against
its real output, and a manual audit — none of which exist yet.
```
