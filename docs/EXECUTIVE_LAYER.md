# Executive Layer — Consolidated CFO / CTO / COO Decisions

This document consolidates existing CFO, CTO, and COO decisions per Phase 3's narrow scope.
- **Canonical Architecture**: See `ARCHITECTURE.md` for the overarching system design and `ROADMAP.md` for phase sequence and scope.
- **Evidence & Reasoning Trail**: See `PROJECT_STATUS.md` for historical calibration data, incident logs, and decision rationale.

> **Scope Notice**: This document consolidates decisions that are already active and enforced in production today. It does not introduce any new logic, new abstractions, or speculative routing layers.

---

## 1. CFO — Budget & Approval Logic

All budget and approval policies are defined in `config/settings.yaml` and enforced via `src/budget_guard.py` and `src/approval.py`.

### Budget Ceilings & Hard-Stop Controls
- **Per-Task Cost Limit**: `$0.25` (`budget.per_task_limit: 0.25`). Tasks with an estimated cost exceeding this threshold are blocked before calling the Worker (`BUDGET_BLOCKED`).
- **Daily Spend Limit**: `$2.00` (`budget.daily_limit: 2.00`). Total cumulative spend within a UTC calendar day cannot exceed this ceiling.
- **Hard-Stop Enforcement**: Enabled (`budget.hard_stop: true`). Enforced strictly in `src/budget_guard.py:check_budget()` against the SQLite `budget` table.

### Per-Repository Auto-Approval Tiering
- **Auto-Approve Enabled**: `approval.auto_approve: true`
- **Human Review Gate for LLM Judgments**: `approval.require_human_review_for_llm_judged: true`
- **Trusted Repositories**:
  - `pydantic/pydantic`
  - `ansible/ansible`
- **Enforcement Point**: `src/approval.py` in `ApprovalGate.request_approval()`.
- **Fail-Safe Default**: If an execution is evaluated via `llm_judged` and passes:
  - If the repository is explicitly listed in `trusted_repos`, it auto-approves autonomously (`Autonomous approval: <repo> is a trusted repository`).
  - For all other repositories (e.g. `psf/requests`, `pandas-dev/pandas`, `scikit-learn/scikit-learn`), it transitions to `WAITING_APPROVAL` with `PENDING` status for human review via `tools/review_queue.py`.

---

## 2. CTO — Model Selection & Repository Boundary

### Current Production Reality vs. Stated Intent
- **Current Production Configuration**: Both the **Worker** (`src/worker.py`) and the **Reviewer** (`src/reviewer.py`) default to `gemini-3.5-flash-lite` for all live executions.
- **Stated (Unenforced) Intent**: An earlier conceptual tiering (Planner = Opus 4.8 / Sonnet 5, Worker = Sonnet 5, Reviewer = Haiku 4.5) was discussed in early architecture notes, but **was never wired into code**. Per a direct query against the real production database (vmi3481882, run 2026-08-22), completed tasks using the real Worker/Reviewer pipeline (`review_method == llm_judged`) average `$0.002036`/task (17 tasks, `$0.034618` total) — filtered separately from the 260 pre-fix `heuristic_fallback` tasks (`$0.000137`/task average), which used template output and old pricing and are not representative of current behavior. At `$0.002036`/task, execution operates with ~123x headroom under the `$0.25` per-task ceiling, so model tiering remains deliberately deferred until volume demands it. (Note: `kpis`'s top-line "Average Cost / Task" figure blends both populations — 321 tasks, `$0.000587`/task — and should not be used for this purpose; see `PROJECT_STATUS.md` for the same filtering caveat.)

### Repository Inclusions and Exclusions
- **Supported Repositories**: Defined in `src/opportunity.py:OpportunityFetcher.SUPPORTED_REPOS`:
  - `psf/requests`
  - `scikit-learn/scikit-learn`
  - `pydantic/pydantic`
  - `ansible/ansible`
  - `pandas-dev/pandas`
  - `pallets/flask`
  - `fastapi/fastapi`
- **Excluded Repository (`python/cpython`)**:
  - `python/cpython` is explicitly commented out in `src/opportunity.py`.
  - **Rationale**: C-internals work reliably produces confident, plausible-looking but fabricated output (such as invented C-macro bindings like `curses.window.getattrs()`, confirmed across four independent LLM evaluations; see `PROJECT_STATUS.md` for the full evidence trail).

---

## 3. COO — Task Routing & Domain Tagging

### Manager Rule-Based Domain Tagging
- **Domain Tagging**: In `src/engine.py` (during the `DISCOVERED -> PLANNED` lifecycle transition), the Manager inspects the task's repository against `approval_gate.trusted_repos` and tags `domain_trusted: true/false` directly into the `TASK_PLANNED` audit log payload.
- **Current Behavioral Boundary**: Domain tagging is currently **informational only**. It provides structured data for review tools and telemetry, but does not alter execution routing or bypass pipeline stages for untrusted repos (all tasks pass through the unified Worker → Tester → Reviewer pipeline).

---

## 4. Explicitly Out of Scope

To preserve architectural discipline and avoid premature abstraction:
- **No New Budget Thresholds**: Existing `$0.25` / `$2.00` rules in `config/settings.yaml` remain authoritative.
- **No Routing Rules for Hypothetical Units**: No multi-department dispatch logic until Phase 5 introduces a genuine second business unit.
- **No Speculative Model Reassignment**: Production remains on `gemini-3.5-flash-lite` without adding unnecessary model abstraction layers.
- **No Wrapper Classes**: No new Python classes wrapping or duplicating `config/settings.yaml`.
- **Roadmap Verification Status**: Phase 3's Definition of Done in `ROADMAP.md` (*"Adding a second business unit requires config changes only"*) remains an unchecked checkbox until Phase 5 provides a real second department to prove it.
