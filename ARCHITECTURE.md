# AgencyOS — Architecture

This document describes what AgencyOS *is* and *why*. It should change rarely.
For what to build next and in what order, see `ROADMAP.md`.

---

## Mission

AgencyOS exists to maximize long-term enterprise value by discovering
opportunities, executing profitable work, creating reusable assets, and
continuously improving — while operating safely under human oversight.

Every feature must trace back to this mission. If it doesn't, it doesn't
belong in the system yet.

---

## Design Principles

These resolve ambiguity when you or an agentic IDE aren't sure how to build
something. Treat them as the constitution.

- Build the smallest working system.
- Every abstraction must have at least two real use cases before it's extracted.
- Prefer composition over inheritance.
- Prefer policies (rules/config) over agents until reasoning is genuinely required.
- Managers coordinate; workers execute.
- Human approval is required for irreversible actions.
- Every completed task produces measurable data.
- Every successful task should improve future tasks.
- Optimize for simplicity before scalability.

---

## Capability Levels

Progress is measured by what the system can *do*, not by file count or
subsystem count.

```
Level 0 — Complete one task
Level 1 — Operate one business unit
Level 2 — Learn from previous work
Level 3 — Generate reusable assets
Level 4 — Operate multiple business units
Level 5 — Launch new ventures
```

Learning and asset generation (2, 3) come before scaling to multiple units (4).
A system that scales before it learns just replicates inefficiency instead of
compounding improvement.

---

## System Overview (long-term shape)

This is the ceiling the system grows toward — not the starting point. See
ROADMAP.md for what actually gets built first.

```
Founder
  ↓
Human Approval Gateway
  ↓
Executive Board (starts as logic/rules, may become agents later)
  ↓
Organization Kernel
  ├── Scheduler
  ├── Planner
  ├── Event Bus / Task Queue
  ├── Approvals
  ├── Memory
  ├── Policy Engine
  ├── Budget Manager
  └── Audit Log
  ↓
Business Units (each owns: Manager, Workers, SOPs, Templates, KPIs)
```

`shared/` (notifications, storage, auth, etc.) is intentionally **not**
pre-populated. Per the Design Principles, nothing gets extracted into shared
infrastructure until two real business units independently need it. Building
this speculatively was tried in an earlier version of this spec and rejected —
it produced coupling to guessed interfaces instead of real ones.

---

## Task State Machine

Every unit of work (a "task") moves through exactly one canonical
lifecycle. This applies to every business unit, not just the current
GitHub-issue pipeline — it's the substrate the Scheduler, Approvals, Audit
Log, and Budget Manager are all built on top of.

```
DISCOVERED → PLANNED → READY → EXECUTING → REVIEW → WAITING_APPROVAL
    → COMPLETED   (approved and review passed)
    → BLOCKED     (human rejection, or review failed)
```

`APPROVED` and `DELIVERED` are reserved for a future delivery phase, once
the engine gains real side-effect execution (PR merge, invoice send,
etc.). They are not persisted states today — the engine transitions
`WAITING_APPROVAL` directly to `COMPLETED` or `BLOCKED`. Don't add them
back speculatively; per the Governing Rule below, they need two real
callers first, same as any other abstraction.

Terminal states (zero outgoing transitions — reachable only once, and never
left except through an explicit, logged manual override):
- `COMPLETED`
- `BLOCKED` (human approval rejection, or a failed review)
- `QUALITY_REJECTED` (automated quality-gate rejection)
- `WORKER_FAILED`

Any transition not on this graph is illegal and must be rejected and
logged at the point of write — not merely detected after the fact by an
audit. This includes self-transitions (e.g. a task resuming mid-review
after a restart writing `REVIEW → REVIEW`) — those are legal exactly
because they're idempotent re-entries into the same state, not because
the guard is loose.

---

## Persistence & State Integrity

Task state is the one thing every other guarantee in this system —
budget enforcement, approval gating, telemetry, idempotency — is built on
top of. If it can drift or be bypassed, everything built on top of it
inherits the same failure silently. These rules exist to make that
structurally impossible, not just discouraged:

- **Single source of truth.** No module may directly INSERT, UPDATE, or
  DELETE task state. All task state mutations pass through one persistence
  service responsible for transactions, audit logging, state validation,
  and constraint enforcement. No in-memory task state (a Python dict, set,
  or counter) is ever authoritative — every execution decision must
  originate from a query against persisted state. In-memory structures are
  permitted only as a read-through cache of the DB, never as the thing
  checked before acting.
- **No implicit state creation.** A helper written to record one kind of
  fact (an approval, a budget entry) must never silently create a
  different kind of record (a task) as a side effect of a missing
  reference. If a referenced entity doesn't exist, that's a bug to surface
  loudly — via a constraint violation or an explicit error — not to paper
  over by fabricating the missing row.
- **State mutations are transactional.** Any single state transition that
  touches more than one table (task state + audit log + budget + approval)
  commits as one atomic unit. Partial writes — a task updated but its
  audit row lost to a crash — are exactly what this system's audit
  guarantees cannot tolerate.
- **Test and production environments are structurally isolated.** Test
  code must be incapable of resolving to a production data path, by
  construction — not by convention, not by someone remembering to pass a
  flag. A test suite that can silently write to real operational data
  poisons the one thing (audit history) this system exists to keep
  trustworthy.
- **The illegal-transition guard assumes single-writer execution.**
  Checking a task's current state and then writing its next state are two
  separate steps today (a read, then a transaction). That's safe only
  because the system runs one scheduler and one engine loop, sequentially,
  in a single process. If this system ever grows concurrent workers or
  multiple engine instances, that read-then-write must become one atomic
  operation (e.g. a conditional update inside the same transaction as the
  read), or two writers can race past the same stale state check. Don't
  add concurrency without closing this gap first.

---

## Target Repository Shape

This is where the structure converges over time, not what exists on day one.

```
AgencyOS/
  docs/
    ARCHITECTURE.md
    ROADMAP.md
  kernel/
    scheduler/
    planner/
    queue/
    approvals/
    memory/
  business_units/
    software_agency/
  shared/            (empty until 2+ real callers justify extraction)
  plugins/
  tests/
```

---

## Governing Rule for Implementation

Any abstraction without two concrete callers is prohibited, unless it is
explicitly required by this architecture (e.g. the approval gate, audit
logging — these are safety-critical and exist from Level 0). The Task
State Machine and Persistence & State Integrity rules above are the other
standing exception: they are not subject to the two-callers test, because
they are what makes every other guarantee in this document trustworthy.
