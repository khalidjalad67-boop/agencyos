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

## Phase 0.8 — Engine Stabilization [COMPLETE ✅ — HERMES GATE CLOSED]

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

One pipeline. This is the entire legal transition graph — anything not on this list is an illegal transition and must be rejected/logged, not silently allowed. This is the same state machine documented canonically in `ARCHITECTURE.md`'s "Task State Machine" section — treat that as the single source of truth if the two ever look inconsistent again:

```
DISCOVERED → PLANNED → READY → EXECUTING → REVIEW → WAITING_APPROVAL
    → COMPLETED   (approved and review passed)
    → BLOCKED     (human rejection, or review failed)
```

`APPROVED` and `DELIVERED` are reserved for a future delivery phase and
are not persisted states today — `engine.py` transitions
`WAITING_APPROVAL` directly to `COMPLETED` or `BLOCKED`. (This section
originally listed `APPROVED`/`DELIVERED` as part of the legal graph, from
before the 0.8B architecture decision corrected this — that stale text
caused a false alarm during the manual audit before being caught and
fixed. Confirmed against the actual `audit_log.jsonl` from the closing
soak: the real event sequence is exactly `DISCOVERED → PLANNED → READY →
EXECUTING → WORKER_EXECUTED → REVIEW_COMPLETED → WAITING_APPROVAL →
COMPLETED`, matching this corrected diagram, not the old one.)

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

- [ ] `tasks.state` constrained to the explicit state machine above via `CHECK(state IN ('DISCOVERED','PLANNED','READY','EXECUTING','REVIEW','WAITING_APPROVAL','APPROVED','DELIVERED','COMPLETED','BLOCKED','QUALITY_REJECTED','WORKER_FAILED'))`. (This checklist item predates the 0.8B decision to drop `APPROVED`/`DELIVERED` as persisted states — if the deployed SQL `CHECK` constraint still lists them as allowed values, that's harmless, not a bug: the constraint is a ceiling on what SQLite will accept, `LEGAL_TRANSITIONS` in `db.py` is the actual application-layer enforcement, and nothing ever writes those two values. Not worth a migration to tighten unless verified otherwise.)
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

#### 0.8E — Regression Suite [COMPLETE ✅]

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

**Status**: `tests/test_phase0_8e_regression.py` implements one permanent
regression test per historical bug found across 0.8A1–0.8D, plus
`tests/test_phase0_8e_sigterm.py` for the shutdown-durability race found
during the second soak. Each test was checked to exercise the real
production code path that had the bug, not just the surface symptom —
notably the 0.8B `REVIEW→REVIEW` test, which went through two drafts
before it actually routed through `run_startup_recovery()` and
`engine.process_task()` for real rather than just confirming a
`LEGAL_TRANSITIONS` table entry existed. Full suite: 97 tests, 0
failures, 1 skip (Windows-only skip for the Linux-specific SIGTERM
stress test). See `PROJECT_STATUS.md` for the full verification trail.

### Hermes gate

Strictly sequential — no parallel work, no "almost done":

```
0.8A1 PASS ✅ → 0.8A2 PASS ✅ → 0.8B PASS ✅ → 0.8C PASS ✅ → 0.8D PASS ✅ → 0.8E PASS ✅
  → fresh 24-hour soak on clean artifacts PASS ✅ (fourth attempt,
    2026-08-10→11, 24.29h — soak of record. First soak invalidated by a
    JSONL-sync gap; second by a shutdown-durability race; third by a
    missing TELEMETRY_REPORT audit trail (both tools passed anyway
    because verify_audit.py's telemetry check silently no-ops on empty
    data); fourth came back clean on all counts including the fix:
    1760 audit_log rows = 1760 JSONL lines, 351 TELEMETRY_REPORT events
    with values confirmed correct and monotonic throughout, zero
    duplicate executions, zero state-machine violations across 132
    tasks, budget reconciled to 8 decimals, zero orphans — independently
    re-verified, not trusted from pasted output)
  → verify_audit.py: zero failures ✅ (independently reproduced against
    the fourth soak's real files)
  → replay_audit.py: zero diffs ✅ (same)
  → manual audit: PASSED ✅ (2026-08-11) — first attempt on the third
    soak found the real TELEMETRY_REPORT gap (see above, now fixed) plus
    a false alarm about missing `APPROVED`/`DELIVERED` transitions
    (stale pre-0.8B documentation, corrected, not a code bug). A second
    review pass, from a different AI assistant, initially blocked sign-
    off again by citing the *third* soak's numbers (1320 rows, 0
    TELEMETRY_REPORT) rather than the fourth — caught by having the
    human directly re-query the actual current VPS files before
    accepting either verdict. Final sign-off confirmed against the
    correct evidence: `audit_log rows: 1760`, `TELEMETRY_REPORT count:
    351`, timestamps matching the fourth soak exactly.
  → GATE CLOSED. Tagged `v0.8-stable`; soak artifacts preserved as
    `agencyos.db.v0.8-soak` / `audit_log.jsonl.v0.8-soak`.
  → only then: Hermes/Phase 1 begins
```

No exceptions for "it's probably fine now" — re-run both tools on fresh data before Hermes work starts.

---

## Phase 1 — Kernel Foundations [COMPLETE ✅]

Only build what Phase 0/0.5 is genuinely straining against:
- **Event Bus** — evaluated and deferred (single-writer sequential pipeline with 1 consumer today; zero real callers exist for multi-consumer dispatch per Governing Rule).
- **Policy Engine** — implemented as a flat config file of rules (`config/settings.yaml`) defining budget limits, quality thresholds, approval behavior, watchdog triggers, scheduler intervals, and network timeouts.
- **Cost-Per-Task Visibility** — directly surfaced as a top-level `cost` attribute on task records in `get_task()`, `get_tasks_by_state()`, `get_all_tasks()`, and via `Database.get_task_cost(task_id)`, backed by `TASK_COMPLETED` audit payloads and `budget` table spend rows.

> *Note: Audit Log and Budget Manager are already fully satisfied by `src/logger.py` and `src/budget_guard.py` built in Phase 0 / 0.5 — no rebuild needed.*

**Definition of Done:**
- [x] Phase 0 loop runs unattended except at defined approval gates
- [x] A cost-per-task number is visible for every completed task

---

## Phase 2 — First Real Business Unit [COMPLETE ✅]

Pick one unit (Software Agency) and build it properly:
- Manager (routes tasks to the right worker)
- 2–3 workers with distinct roles
- SOPs as versioned prompt templates
- KPIs (tasks completed, approval rate, revenue if applicable)

Do not build other units yet.

**Implementation note**: built as 1 well-specialized worker rather
than 2-3, per real evidence gathered during implementation --
task narrowness (mechanical/scoped vs. broad/architectural) proved to
be the dominant predictor of reliability, not pipeline-stage role
specialization. See PROJECT_STATUS.md's Phase 2 entries for the full
evidence trail (calibration data across 5+ repos, the KPIs detector-
signal-quality finding, the cpython removal rationale). Manager
implemented as rule-based domain tagging; SOPs implemented as a
single versioned prompt template (sops/worker_v1.md); KPIs implemented
as `tools/review_queue.py kpis`.

**Definition of Done:**
- [x] This unit could run as a standalone product on its own

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
Read ARCHITECTURE.md, ROADMAP.md, and PROJECT_STATUS.md completely.

CURRENT STATE — Phase 0 through Phase 0.8 are COMPLETE and
independently verified (systemd boundary test PASSED 2026-08-15,
Hermes deferred). Phase 1 (Kernel Foundations) has been implemented
— flat Policy Engine config, cost-per-task visibility, Event Bus
correctly deferred per the Governing Rule — and is PENDING HUMAN
SIGN-OFF. It is not yet marked complete in this file.

DO NOT begin Phase 2 or any other new phase. If you are running this
prompt, your task is whatever the human has explicitly asked for in
this session — this block is a placeholder until Phase 1 is signed
off and a real Phase 2 kickoff prompt is written to replace it.
```
