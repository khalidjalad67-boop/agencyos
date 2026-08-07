# AgencyOS — Project Status (as of this session)

Carry this forward as context. `ARCHITECTURE.md` and `ROADMAP.md` remain the
source of truth for design/phases — this file is a status snapshot plus
decisions made in conversation that aren't written into those docs yet.

---

## Where things actually stand

- **Phase 0 → 0.6: COMPLETE and verified.**
- **Phase 0.7: UNVERIFIED — superseded by Phase 0.8.** Forensic audit
  confirmed `AutonomousEngine` itself reproduced the duplicate-execution
  and rejection-bypass bugs when it actually ran, not just that it wasn't
  wired in. `ROADMAP.md`'s Phase 0.7 section and DoD checkboxes have been
  corrected to reflect this (previously said `[COMPLETE ✅]` with all
  boxes checked while the verification flag said the opposite — that
  contradiction is now fixed). Phase 0.8's checkpoints are the real,
  independently-verified source of truth for these guarantees.
- **Phase 0.8 (Engine Stabilization): 0.8A–0.8D COMPLETE & VERIFIED.
  Second 24h+ soak IN PROGRESS.**
  - **0.8A1 (Database Schema): COMPLETE & VERIFIED ✅.** Deterministic
    path resolution, 12-state `CHECK` constraint, `UNIQUE(opportunity_id)`,
    `FOREIGN KEY ON DELETE RESTRICT`, legacy orphan quarantine (23 rows,
    independently reproduced). Caught and fixed a real incident: an
    early path-resolution fix let the test suite write 14 fabricated stub
    rows into the real `agencyos.db`; test/prod isolation
    (`is_in_test_mode()`) added to make that structurally impossible.
  - **0.8A2 (Live Persistence & Idempotency): COMPLETE & VERIFIED ✅.**
    `save_task()`/`update_task_state()` deleted entirely — non-atomic
    task mutation is structurally impossible, not just discouraged.
    `execute_atomic_transition()` verified via real cursor-level crash
    injection (not a mocked no-op). Named regression tests for both
    historical bypass cases pass against real seeded states. Fixed a
    worker-idempotency cache-hit bug that could leave `worker_result_json`
    permanently `NULL` on recovered tasks.
  - **0.8B (State Machine, Scheduler & Recovery): COMPLETE & VERIFIED ✅.**
    `LEGAL_TRANSITIONS` guard, watchdog stall/recovery lifecycle, idle
    backoff, SIGKILL crash-recovery test. Verified against a real crash
    DB after two prior uploads didn't match their own console output.
    Fixed `LEGAL_TRANSITIONS` missing `REVIEW→REVIEW` (would have broken
    every crash-recovery resume) and a watchdog `_in_warning` reset bug.
  - **0.8C (Telemetry & Budget Accuracy): COMPLETE & VERIFIED ✅**
    (2026-08-05). `Database.get_telemetry_metrics()` computes every field
    via live SQL. **Independently re-verified**: re-ran every category
    and cost-reconciliation query by hand against the real
    `test_phase0_8c_telemetry.db` and matched exactly — 5 tasks
    partitioned 1/1/1/1/1 with zero overlap, cost sums agreeing at
    $0.10 from two independently-written columns. Caught and fixed a
    real double-counting bug: `budget_blocked_executions` was originally
    `COUNT(*) WHERE state='BLOCKED'` with no cause disambiguation, since
    `BLOCKED` is written by both the budget-check path and the
    approval/review-rejection path — this double-counted
    approval-rejected tasks (category sum was 6 against 5 real tasks).
    Fixed via `error_reason LIKE 'BUDGET_BLOCKED:%'` disambiguation,
    verified against the real (free-text, non-prefixed) `error_reason`
    values `engine.py` actually writes.
  - **0.8D (Automated Verification, Replay & Corruption Suite): COMPLETE
    & VERIFIED ✅** (2026-08-07, after significant rework). Built
    `tools/verify_audit.py` and `tools/replay_audit.py`, reusing
    `LEGAL_TRANSITIONS` from `src/db.py` rather than reimplementing it.
    Several real issues caught and fixed during this checkpoint:
    - The impossible-transition event dispatch was initially missing
      `TASK_BLOCKED`/`OPPORTUNITY_REJECTED` (the real event names) and had
      a dead entry for a `"QUALITY_REJECTED"` event that's never emitted
      — meaning the tool couldn't detect a repeat of either of the two
      actual historical bypass cases. Fixed and independently verified by
      hand-tracing both corruption scenarios against the real dispatch
      logic and reproducing the exact reported error messages myself.
    - An interval-propagation bug in `scheduler.py` (passing the base
      interval to `watchdog.check_heartbeat()` instead of the backed-off
      one) was found and fixed, with `verify_audit.py` updated to use the
      logged `expected_interval` per-heartbeat instead of a hardcoded 30s.
    - **Serious incident, caught and reverted**: while debugging an
      unrelated test failure, an uncommitted edit to `src/opportunity.py`
      added a fallback that silently synthesized mock opportunities on
      the *live* fetch path whenever GitHub rate-limited — reachable in
      production, no distinguishing log event, added purely to make
      `unittest discover` pass without a `GITHUB_TOKEN`. This is the exact
      "synthetic data passed off as live" pattern from lesson #2, and
      violated the Phase 0.8 freeze on new opportunity sources. Reverted
      via `git checkout`, confirmed clean against the actual file
      content. The underlying CI problem it was (wrongly) solving is now
      handled correctly via `AGENTOS_SKIP_NETWORK_TESTS`, matching the
      existing `AGENTOS_SKIP_PROCESS_TESTS` pattern.
    - Two corruption-artifact submissions were fabricated/hand-written
      rather than real tool output (invented event types that don't
      exist in `engine.py`'s vocabulary, missing the `TASK_WAITING_APPROVAL`
      event needed to actually exercise the real bypass path) before a
      third submission produced genuinely verifiable output, confirmed by
      hand-tracing the dispatch logic myself and reproducing both error
      messages exactly.
  - **First real 24h+ soak (2026-08-05→06, ~25.18h): RAN CLEAN
    OPERATIONALLY BUT INVALIDATED AS EVIDENCE.** 123 real tasks across 5
    real repos, genuinely varied issue text (confirmed via direct
    diversity check — not synthetic), zero crashes, one clean
    cold-start recovery, correct backoff behavior. But both
    `verify_audit.py` and `replay_audit.py` correctly failed on the real
    result, exposing a structural gap that had been invisible through
    every prior checkpoint:
    - **Real production bug**: `execute_atomic_transition()` only ever
      wrote to SQLite `audit_log`, never to `audit_log.jsonl`. Since
      0.8A2 made it the sole path for all task-mutating events, the
      JSONL file had received almost nothing but `SCHEDULER_HEARTBEAT`
      since that checkpoint (1324 DB rows vs. 343 JSONL lines — 343 =
      exactly the heartbeat count). This had been masked in every 0.8B–
      0.8D test because those test fixtures manually kept JSONL and DB
      in sync by hand (e.g. `test_phase0_8b_crash_recovery.py`'s
      `add_event()` helper) — the real production code path never did
      this itself. Only a genuinely organic, non-fixture-driven run
      could have caught it, which is exactly what this soak was for.
    - **Separate tooling bug** (not production): `verify_audit.py`'s
      heartbeat-gap check compared each gap against the *previous* row's
      `expected_interval` instead of the *current* row's — an off-by-one
      that flagged every legitimate backoff step as an unexplained gap.
      The soak's actual backoff behavior (30→60→120→240) was correct;
      the checker was wrong.
    - Fix required two rounds: the first fix moved JSONL-writing into
      `Database.log_event()`/`execute_atomic_transition()` but
      reintroduced the *same bug class* through `AuditLogger.log_event()`
      (which already wrote its own JSONL line AND called
      `self.db.log_event()` internally) — caught before it reached the
      VPS by tracing the actual delegation chain, not trusting "fixed"
      as a claim. Second round correctly collapsed to one writer:
      `AuditLogger.log_event()` now delegates entirely to
      `self.db.log_event()`. Also added `resolve_log_path()`, mirroring
      `resolve_db_path()`, since `log_filepath` had the same
      test/production path-isolation gap 0.8A1 fixed for `db_path`.
    - **Local 15-minute smoke test after both fixes: clean.** 848 SQLite
      rows == 848 JSONL lines exactly, across genuinely mixed event
      sources (heartbeats via `AuditLogger`, task events via
      `execute_atomic_transition`) for the first time. Both tools exit 0.
  - **Second 24h+ soak: IN PROGRESS on the VPS.** Two failed start
    attempts first (a crashed/orphaned process left a stale `agencyos.db`
    with 105 tasks all sharing one identical `created_at` timestamp,
    which a subsequent start silently "recovered" as
    `ignored_tasks: 105` instead of starting genuinely fresh) — caught by
    checking `STARTUP RECOVERY` output and process/session state directly
    rather than assuming a restart worked. **Confirmed clean start:
    2026-08-07 14:52:33 UTC** (`'ignored_tasks': 0` across the board).
    Target completion: **2026-08-08 14:52:33 UTC**. Not yet verified —
    do not run `verify_audit.py`/`replay_audit.py` until it's genuinely
    past that mark, and specifically re-check SQLite/JSONL line-count
    parity partway through this time, not just at the end.
  - **Next after the soak: 0.8E (Regression Suite)** — one permanent
    test per historical bug found across this entire phase, including
    the JSONL-sync gap and the verifier's own off-by-one.
- **Do not start Phase 1 until Phase 0.8's Hermes gate DoD is confirmed
  with real data from a soak that hasn't been invalidated.**

## Hard-won lessons from this build (apply going forward)

1. **"Tests pass" ≠ "the feature works."** Confirmed again, twice, this
   session: the JSONL-sync gap survived every 0.8B–0.8D test because all
   of them used fixtures that manually kept JSONL and DB in sync by hand
   — only a genuinely organic 24h+ run exposed that the real production
   code never did this itself. And the first attempted fix "eliminated
   duplicate writes" per its own summary while actually reintroducing the
   same bug class through a different call path (`AuditLogger.log_event()`
   still calling both its own `write_jsonl()` and `self.db.log_event()`)
   — only caught by tracing the actual delegation chain, not trusting the
   claim.
2. **Synthetic/templated data has been passed off as "live" data
   repeatedly** — most recently, the reverted `opportunity.py` mock
   fallback during 0.8D, and hand-fabricated corruption test evidence
   (invented event types, missing required events) submitted twice before
   genuine tool output was provided. Verify data authenticity directly,
   and treat "generated evidence" claims with the same scrutiny as
   production code claims.
3. **Operational/infrastructure state needs the same evidence bar as
   code.** The second soak's two false starts (orphaned process, stale
   DB silently "recovered" instead of flagged) weren't caught by trusting
   "started successfully" — they were caught by checking `ps aux`,
   `tmux ls`, and the actual `STARTUP RECOVERY` summary line directly.
   Assume a restart didn't work until confirmed otherwise.
4. **The evidence-first rule has held up every time it's been tested** —
   now including catching a premature "duplicate writes eliminated" claim
   before it reached the VPS, and two rounds of fabricated soak-restart
   evidence before a genuinely clean start was confirmed.

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

1. Let the second soak run to completion — **2026-08-08 14:52:33 UTC**.
   Do not stop it early, even if a spot-check looks slightly off; bring
   anything unusual for review while it's still running rather than
   restarting the clock.
2. At completion: `pkill -f "python3 main.py"`, confirm the process is
   actually dead, then run `tools/verify_audit.py` and
   `tools/replay_audit.py` against the real resulting files and send raw
   output plus the actual `agencyos.db` + `audit_log.jsonl` — same
   evidence bar as every checkpoint before this.
3. Specifically re-check SQLite/JSONL line-count parity at completion
   (and ideally at a mid-run spot-check too) to confirm the JSONL-sync
   fix held for the full duration, not just the first 15 minutes.
4. Only if the soak is genuinely clean: implement 0.8E (Regression
   Suite) — one permanent test per historical bug found across this
   entire phase.
5. Only after the full Hermes gate passes (0.8A–0.8E, a soak that hasn't
   been invalidated, `verify_audit.py` clean, `replay_audit.py` clean,
   manual audit) — migrate the pipeline to Hermes.
6. Only then begin Phase 1 (Event Bus / Policy Engine — build only what
   Hermes doesn't already cover).
