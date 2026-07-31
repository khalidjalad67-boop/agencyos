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

## Phase 0.7 — Autonomous Operations [COMPLETE ✅]

Transition AgencyOS from a manually triggered execution loop into a restart-safe autonomous service that operates without a live terminal while preserving correctness across failures.

This phase validates **behavior**, not uptime. Phase 0.8 validates uptime.

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

- [x] Scheduler executes tasks automatically on interval
- [x] Task state survives restart
- [x] Approval queue survives restart
- [x] Watchdog backs off repeated failures
- [x] Disabled sources recover after cooldown
- [x] No duplicate execution after restart
- [x] No duplicate Worker or Reviewer billing after restart
- [x] Verified by forcibly terminating the process (SIGKILL / kill -9) during EXECUTING and confirming correct recovery
- [x] Budget enforcement remains correct after restart
- [x] Live GitHub fetching from Phase 0.6 remains unchanged in normal operation
- [x] Fixtures/mocks are used ONLY inside the SIGKILL crash-recovery test harness to isolate state-machine correctness from network variability

---

## Phase 0.8 — Stability Validation (Deferred)

This phase validates durability rather than new functionality.

It introduces no new architecture beyond Phase 0.7.

Only continuous execution and observation.

### Definition of Done

- [ ] Operates continuously for 24 hours
- [ ] No duplicate execution
- [ ] No approval loss
- [ ] No task corruption
- [ ] Automatic recovery after restart
- [ ] Audit log remains consistent
- [ ] RSS memory growth stays within 10% of baseline over 24 hours
- [ ] No orphaned workers after restart

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

There is no dedicated "Infrastructure phase." Infrastructure is extracted
into `shared/` only when two real callers need the same thing — see the
Governing Rule in ARCHITECTURE.md.

---

## Next Prompt for the Agentic IDE

```
Read ARCHITECTURE.md and ROADMAP.md completely. Treat ARCHITECTURE.md as
the stable source of truth and ROADMAP.md as the implementation plan.
Implement only Phase 0. Do not anticipate future phases. Do not create
abstractions, folders, services, or interfaces unless they are required by
Phase 0 or explicitly mandated by the architecture. When faced with
multiple implementation choices, prefer the simplest solution that
satisfies the Design Principles and Definition of Done.
```
