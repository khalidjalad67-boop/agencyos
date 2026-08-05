# AgencyOS — Project Status (as of this session)

Carry this forward as context. `ARCHITECTURE.md` and `ROADMAP.md` remain the
source of truth for design/phases — this file is a status snapshot plus
decisions made in conversation that aren't written into those docs yet.

---

## Where things actually stand

- **Phase 0 → 0.6: COMPLETE and verified.**
- **Phase 0.7: UNVERIFIED / RECONCILED.** Forensic audit of the 2026-08-04
  VPS soak run found duplicate executions (up to 12x), rejection-gate
  bypasses, and unpersisted DB tables. Root cause traced: a legacy Phase
  0.6 loop ran the first 157 tasks with no persistence at all, and —
  important — the dedicated Phase 0.7 `AutonomousEngine` also reproduced
  the same class of bugs when it actually ran post-blackout (it
  re-executed a quality-rejected opportunity and duplicated two
  already-completed ones). Phase 0.7's own eligibility-check logic was
  broken, not just unwired. Its protections are being rebuilt properly in
  Phase 0.8.
- **Phase 0.8 (Engine Stabilization): IN PROGRESS.**
  - **Checkpoint 0.8A1 (Database Schema): COMPLETE & VERIFIED ✅**
    (2026-08-05). Deterministic DB path resolution (`src/db.py`,
    `src/migrations.py`), strict 12-state `CHECK` constraint,
    `UNIQUE(opportunity_id)`, `FOREIGN KEY ON DELETE RESTRICT` on `budget`
    and `approvals`, legacy orphan quarantine into
    `_legacy_orphaned_rows`. **Independently re-verified**: migration run
    against a fresh copy of the actual audited `agencyos.db` quarantined
    exactly 23 orphan rows (10 budget + 13 approvals), left `tasks`/
    `budget`/`approvals` at 0 rows, `PRAGMA foreign_key_check` returned
    zero violations, and quarantined row content matched the original
    audit byte-for-byte. This checkpoint also caught and fixed a real
    incident along the way: an early version of the path-resolution fix
    let the test suite write 14 fabricated stub rows into the real
    `agencyos.db`; test/prod DB isolation (`is_in_test_mode()`) was added
    to make that structurally impossible going forward.
  - **Checkpoint 0.8A2 (Live Persistence & Idempotency): COMPLETE &
    VERIFIED ✅** (2026-08-05). Verified directly against source, not
    summary: `save_task()`/`update_task_state()` deleted from `Database`
    entirely (Option A structural enforcement — non-atomic task mutation
    is now impossible, not just discouraged); `execute_atomic_transition()`
    wraps task+audit+budget+approval writes in one `BEGIN IMMEDIATE`
    transaction with real rollback (confirmed via cursor-level crash
    injection mid-transaction, not a mocked no-op); both named regression
    tests for the historical bypass cases
    (`4335223464` quality-rejection re-execution,
    `4844615862`/`4827149379` 12x duplication) pass against real seeded
    states. One bug found and fixed during review: the worker-idempotency
    cache-hit path in `engine.py` never persisted `task["worker_result"]`
    back to the DB, meaning a task recovering through that path could
    reach `COMPLETED` with `worker_result_json` permanently `NULL` — fixed
    to mirror the fresh-execution branch.
  - **Checkpoint 0.8B (State Machine, Scheduler & Recovery): PLAN
    APPROVED, IMPLEMENTATION NOT YET STARTED.** Full transition
    enumeration verified line-by-line against `engine.py` (L80–299) and
    `recovery.py` (L1–90). Two gaps caught and closed during plan review
    before any code was written: `LEGAL_TRANSITIONS` was initially missing
    `PLANNED→BLOCKED`, `EXECUTING→EXECUTING`, and `REVIEW→REVIEW` (the
    last one — the crash-recovery resume path — would have made 0.8B's
    own crash-recovery test throw `InvalidStateTransitionError` on its
    first resume, had it shipped as originally drafted); and the watchdog
    `_in_warning` flag had no reset path outside a full stall cycle,
    which would have permanently disabled `WATCHDOG_WARNING` after the
    first warning-without-stall. Architecture decision made: `APPROVED`/
    `DELIVERED` are not real persisted states (engine goes
    `WAITING_APPROVAL → COMPLETED` directly) — `ARCHITECTURE.md` corrected
    to match code, rather than adding unused states to code. Awaiting
    implementation + evidence (real files, full-suite run, and raw
    output from the subprocess-based SIGKILL crash-recovery test
    specifically — that's the one test in this checkpoint exercising the
    actual failure mode Phase 0.8 exists to fix).
  - **Next Checkpoint after 0.8B: 0.8C (Telemetry & Budget Accuracy).**
- **Do not start Phase 1 until Phase 0.8's Hermes gate DoD is confirmed
  with real data.**

## Hard-won lessons from this build (apply going forward)

1. **"Tests pass" ≠ "the feature works."** This has now happened at least
   three times: the persistence layer disconnected while tests reported
   100% pass; Phase 0.7's `AutonomousEngine` reproduced the exact bugs it
   claimed to fix; and this file itself briefly marked 0.8A2 "COMPLETE &
   VERIFIED" on the strength of an IDE summary before independent
   verification caught three open gaps. Always request the raw DB/log
   artifact, not just a summary, before marking a phase done — including
   in this status file.
2. **Synthetic/templated data has been passed off as "live" data before**
   (clustered description lengths, sequential template titles; also the
   14 fabricated stub task rows that leaked into the real `agencyos.db`
   during 0.8A1 testing). Verify data authenticity directly whenever a
   claim involves "live" or "production" state.
3. **The evidence-first rule has held up every time it was tested**: cut
   `quality.py`'s speculative filters (twice), cut the anticipatory
   "Infrastructure phase," cut Phase 0.7's Side Effect Executor before any
   real external action existed, caught the 0.8A2 premature-complete claim
   before it propagated further. Keep applying it by default.

## Standing decisions made in conversation (not yet in ROADMAP/ARCHITECTURE)

- **LLM tiering**: Planner = Opus 4.8 (or Sonnet 5 if cost-conscious),
  Worker = Sonnet 5, Reviewer/search/classification = Haiku 4.5. Sub-Haiku
  options (DeepSeek V4 Flash, Gemini Flash-Lite) identified for
  search/tagging tasks but explicitly **not wired in yet** — current cost
  (~$0.008/task) has ~1000x headroom under the $0.25 budget ceiling, so this
  is deferred until real volume justifies it.
- **Hermes migration timing**: migrate the *proven* Phase 0.x pipeline into
  Hermes right before Phase 1 (Kernel Foundations) begins — not before, not
  after full project completion. Rationale: Hermes's native memory/skills
  system likely covers much of what Phase 1 would otherwise hand-build.
- **Antigravity vs. Claude Code vs. Hermes**: Antigravity's Mission Control
  suits later multi-department parallel execution (Phase 5+); Claude Code's
  subagent model suits single-department pipelines (current); Hermes is the
  intended deployment target for persistence/self-generating skills.
- **Department count**: 8 target business units total (Software Agency,
  Website Studio, SaaS Studio, Content Studio, Digital Products, AI
  Automation, Sales, Marketing). Currently 1 of 8 built (Software Agency,
  implicit in current GitHub-issue pipeline). No second department planned
  until Software Agency's real usage indicates which one is actually needed.
- **Executive Board / unpaid layer**: intentionally not built — the person
  is currently playing CEO/COO/CFO/CTO manually. Automate only once
  coordinating between 2+ departments becomes real manual overhead.

## Immediate next actions, in order

1. Implement 0.8B per the approved plan: `LEGAL_TRANSITIONS` guard in
   `db.py`, watchdog stall/recovery lifecycle, idle heartbeat backoff in
   `Scheduler.tick()`, and the subprocess-based SIGKILL crash-recovery
   test.
2. Verify against real evidence before marking complete: actual files
   (`db.py`, `scheduler.py`, `watchdog.py`, new test files,
   `agencyos.db`), the full-suite run (39+ tests expected), and raw
   output/DB state from the crash-recovery test specifically.
3. Continue 0.8C → 0.8D → 0.8E in order, per `ROADMAP.md`, each
   re-verified against raw artifacts before the next starts.
4. Only after the full Hermes gate passes (fresh 24h soak, both
   verification tools clean, manual audit) — migrate the pipeline to
   Hermes.
5. Only then begin Phase 1 (Event Bus / Policy Engine — build only what
   Hermes doesn't already cover).
