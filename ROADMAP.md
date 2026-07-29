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

## Phase 0.6 — Data-Driven Opportunity Filtering (Deferred)

> **Governing Principle**: Every new subsystem must be justified by operational evidence, not architectural prediction.

- Run the hardened loop across 100+ real opportunities.
- Analyze telemetry and audit logs to discover empirical failure patterns (stale issues, duplicate tasks, low-information payloads).
- Build targeted opportunity filters (`src/quality.py`) based strictly on observed evidence rather than speculative predictions.

**Definition of Done:**
- [ ] 100+ real tasks executed and logged
- [ ] Opportunity filters implemented based on empirical failure patterns

---

## Phase 1 — Kernel Foundations

Only build what Phase 0 is now straining against:
- **Event Bus** — only if more than one worker needs to react to task state
- **Audit Log** — structured log of every decision (cheap, do early regardless)
- **Budget Manager** — tracks spend per task, hard-stops at a ceiling
- **Policy Engine** — starts as a config file of rules, not a service

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
