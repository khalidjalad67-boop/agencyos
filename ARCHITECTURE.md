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
logging — these are safety-critical and exist from Level 0).
