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
- **Phase 0.8 (Engine Stabilization): COMPLETE. HERMES GATE SUPERSEDED BY SYSTEMD GATE — CLOSED ✅ (2026-08-15).** Process supervision is systemd, not Hermes (see standing decision above).
  All checkpoints 0.8A1–0.8E verified. Fourth soak (2026-08-10→11,
  24.29h, 1760 audit_log rows = 1760 JSONL lines, 351 `TELEMETRY_REPORT`
  events with values confirmed correct and monotonic throughout) is the
  soak of record. Manual audit sign-off given 2026-08-11, confirmed
  against the correct soak's files after an initial mix-up where a
  second AI assistant's audit had checked the *third* soak's numbers
  (25.24h, 1320 rows, 0 `TELEMETRY_REPORT`) — the exact soak the
  telemetry-gap fix was written to supersede. Also flagged and correctly
  rejected: a claimed `APPROVED`/`DELIVERED` state-machine gap, which is
  the same already-resolved false alarm from the third-soak audit,
  raised again by not having read `ARCHITECTURE.md`'s Task State Machine
  section (which explicitly documents those states as intentionally not
  persisted) — see lesson below.
  - Tagged `v0.8-stable`, soak artifacts backed up as
    `agencyos.db.v0.8-soak` / `audit_log.jsonl.v0.8-soak`.
  - **systemd boundary test: PASS ✅ (2026-08-15, idle-restart variant).** Run live against the production VPS (vmi3481882), evidence captured directly, not claimed:
    - Unit installed at `/etc/systemd/system/agencyos.service` (`Restart=on-failure`, `RestartSec=5`). Zero changes to AgencyOS code or repo — `git status` clean before and after, only the same pre-existing untracked files (`.venv`, soak backups).
    - Baseline: 170 tasks (169 `COMPLETED`, 1 `QUALITY_REJECTED`), `audit_log` max id 2073.
    - `kill -9` issued against PID 188739 at 2026-08-15 01:35:24 UTC.
    - systemd detected the kill the same second (`code=killed, status=9/KILL`) and restarted the process at 01:35:29 — exactly the configured 5s `RestartSec` — new PID 189279.
    - AgencyOS's own recovery fired correctly: `STARTUP_RECOVERY_STARTED` (audit id 2074, `total_tasks`: 170) → `STARTUP_RECOVERY_COMPLETED` (audit id 2075, `recovered_tasks`: 0, `reset_tasks`: 0, `ignored_tasks`: 169).
    - Post-state identical to baseline: still 170 tasks, still 169 `COMPLETED` / 1 `QUALITY_REJECTED`. All 6 new audit rows (ids 2074–2079) individually accounted for: the recovery pair, 2 `SCHEDULER_HEARTBEAT`, 2 `TELEMETRY_REPORT` — nothing unexplained, no duplicate execution, no duplicate cost.
    - Caveat, not a blocker: no task was `EXECUTING` at kill time (queue was idle), so this specifically proves systemd-detect + cold-recovery, not systemd-detect + interrupted-task-recovery. The interrupted-task case was already proven once, separately, in Phase 0.8B's own SIGKILL test (without systemd in the loop). Not required to reopen this gate — logged as a minor follow-up: repeat just the kill/recovery check next time AgencyOS is naturally mid-task, opportunistically, not as a blocking requirement.
    - Gate closed. Phase 1 (Kernel Foundations) is now open.
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
  - **0.8D (Automated Verification, Replay & 24h Soak): TOOLS BUILT &
    VERIFIED, CHECKPOINT NOT YET COMPLETE.** The soak is 0.8D's own final
    DoD item, not a separate step after it — per `ROADMAP.md`: "24-hour
    soak, re-audited from scratch... PASS — 0.8A1 through 0.8D all hold
    simultaneously on the same soak run." `tools/verify_audit.py` and
    `tools/replay_audit.py` are built and independently verified against
    real corruption cases (2026-08-07), reusing `LEGAL_TRANSITIONS` from
    `src/db.py` rather than reimplementing it. Several real issues caught
    and fixed during this work:
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
  - **Third 24h+ soak (2026-08-08→09, 25.24h): CLEAN — 0.8D CLOSED ✅.**
    Independently re-verified from scratch against the true final state
    (confirmed via explicit `ps aux` empty-output check after two prior
    rounds where the process turned out to still be running when
    "stopped" was reported): JSONL/SQLite parity held perfectly through
    the entire run and the actual shutdown — `1320 == 1320`, zero drift
    anywhere, including exactly the shutdown moment that broke the
    second soak. Zero duplicate executions, zero state-machine
    violations across 122 tasks (full replay), budget reconciling to 8
    decimals, zero orphans, zero backwards timestamps, genuinely diverse
    real data across 5 repos. `verify_audit.py` and `replay_audit.py`
    both exit 0 — independently confirmed, not trusted from pasted
    output. **This satisfies `ROADMAP.md`'s literal 0.8D DoD**: "0.8A1
    through 0.8D all hold simultaneously on the same soak run, confirmed
    by both tools reporting zero failures." Three soak attempts total —
    first invalidated by the JSONL-sync gap, second invalidated by the
    shutdown-durability race, third clean after both were fixed and the
    fix itself proven under a 30-iteration Linux SIGTERM stress test
    before this soak even started.
  - **0.8E (Regression Suite): COMPLETE ✅.** One permanent test per
    historical bug found across 0.8A1–0.8D, in `tests/
    test_phase0_8e_regression.py` and `tests/test_phase0_8e_sigterm.py`.
    Each test verified to exercise the real production code path that
    had the bug, not just a symptom-level check — most notably the 0.8B
    `REVIEW→REVIEW` test, which went through two drafts: the first only
    called `execute_atomic_transition()` directly to force that
    transition (proving the `LEGAL_TRANSITIONS` table entry exists,
    which was never in question), the second genuinely seeds a
    mid-crash task, routes it through the real `run_startup_recovery()`,
    and lets `engine.process_task()` complete the review naturally —
    this version would actually have caught the original bug had it
    still been present. Full suite: **97 tests, 0 failures, 1
    environment-appropriate skip** (Windows skip for the Linux-specific
    SIGTERM stress test). `test_phase0_8d_verifier.py`'s existing
    historical-bypass tests were correctly identified as already
    covering the 0.8D case and were not duplicated.
  - **Manual audit: RAN 2026-08-09, FOUND 2 ISSUES, VERDICT NOT PASS.**
    A genuinely thorough independent audit (122 tasks, 122 unique
    opportunities, 0 duplicates, 0 orphans, exact JSONL/DB parity,
    monotonic timestamps, exact budget reconciliation, all 122 completed
    — same category of checks as the original 2026-08-04 forensic audit
    that started this phase) surfaced two things:
    1. **Not a code bug, a doc bug — fixed.** The audit flagged the
       absence of `APPROVED`/`DELIVERED` transition events against
       `ROADMAP.md`'s Phase 0.8 "Explicit state machine" section, which
       still had the *pre-0.8B* diagram — nobody had gone back to fix
       that specific spot after the 0.8B decision to drop those states
       (flagged as a "follow-up worth doing, not urgent" at the time and
       then never done). The real system's behavior was already correct
       and matched `ARCHITECTURE.md`'s corrected diagram — confirmed by
       checking the actual soak's `audit_log.jsonl` event sequence
       directly. `ROADMAP.md` corrected to match `ARCHITECTURE.md`.
    2. **Real, confirmed gap.** Independently verified: zero
       `TELEMETRY_REPORT` events anywhere in the closing soak's 1320
       total events. `get_telemetry_metrics()` computes correctly when
       called directly (0.8C proved that), but `main.py`'s live
       autonomous loop only ever prints telemetry to console each tick
       — it never logs it as a real audit event. This means
       `verify_audit.py`'s telemetry-accuracy check (built in 0.8D)
       silently no-op'd for the entire 25-hour soak instead of actually
       verifying anything (`if telemetry_events:` — empty list, check
       skipped). The soak's clean `verify_audit.py` exit code never
       exercised this guarantee at all.
  - **Fourth soak (2026-08-10→11, 24.29h): CLEAN, INCLUDING THE
    TELEMETRY FIX.** Independently re-verified from scratch: zero
    duplicate executions, zero state-machine violations across 132
    tasks, budget reconciling to 8 decimals, zero orphans, JSONL/SQLite
    parity exact (1760=1760) through the real shutdown, zero backwards
    timestamps, genuinely diverse real data. **The specific gap the
    third-soak manual audit found is confirmed fixed**: 351
    `TELEMETRY_REPORT` events (one per tick, matching
    `SCHEDULER_HEARTBEAT` exactly, not zero this time). Checked
    specifically — not just presence, but correctness — at 5 points
    spread across the run (start, 25%, 50%, 75%, end): every sampled
    snapshot's `total_tasks`/`successful_executions` was internally
    consistent, and across all 351 events the values were monotonically
    non-decreasing (105→132, never dipped or drifted). Final snapshot
    matches raw DB state exactly: `total_tasks`, `successful_executions`,
    `total_cost` all exact, `categories_partition_total: True`,
    `cost_reconciled: True`.
  - **Manual audit: SIGNED OFF 2026-08-11 ✅.** After a second AI
    assistant's review initially cited the wrong soak (the third,
    pre-telemetry-fix run) and a since-resolved `APPROVED`/`DELIVERED`
    false alarm, the human confirmed the fourth soak's files directly on
    the VPS — `audit_log rows: 1760`, `TELEMETRY_REPORT count: 351`,
    start/end timestamps matching exactly — before giving sign-off. This
    is the real gate closure: confirmed against the correct evidence,
    not assumed. Tagged `v0.8-stable`; soak artifacts backed up as
    `agencyos.db.v0.8-soak` / `audit_log.jsonl.v0.8-soak`.
- **Phase 1 (Kernel Foundations): COMPLETE & VERIFIED ✅ (2026-08-17).** Signed off by the human after independent code review, raw 102-test suite output, and a live production deploy on the VPS confirming zero data loss (248 tasks before and after restart, STARTUP_RECOVERY_STARTED/COMPLETED fired correctly).
  - **Policy Engine**: Flat YAML configuration `config/settings.yaml` establishes explicit rules for `budget` (per-task $0.25 ceiling, daily $2.00 limit, hard stop), `quality` (min description length = 1, structural reject labels), `approval` (auto_approve rule), `watchdog` (consecutive failure threshold, cooldown duration, heartbeat/stall multipliers), `scheduler` (tick interval, backoff multiplier, max idle interval), and `network` retries/timeouts.
  - **Event Bus**: Explicitly evaluated and deferred per Governing Rule — AgencyOS remains a single-writer sequential pipeline with exactly one consumer; no second caller exists to justify multi-consumer dispatch.
  - **Cost-Per-Task Visibility**: First-class `cost` attribute populated on all task records (`get_task`, `get_tasks_by_state`, `get_all_tasks`), dedicated SQL query method `Database.get_task_cost(task_id)`, `TASK_COMPLETED` audit event payload `cost`, `budget` table spend rows, and independent reconciliation via `get_telemetry_metrics()` and `verify_audit.py`.
  - **Definition of Done satisfied**:
    1. Phase 0 loop runs unattended except at defined approval gates (verified in `tests/test_phase1.py` with both autonomous approvals and human approval gate rejections).
    2. A cost-per-task number is visible for every completed task (verified in `tests/test_phase1.py` across single tasks and batch execution).
  - Full test suite: 102 tests passing (98 regression + 4 Phase 1 unit/integration tests), 0 failures, 1 environment skip (Linux SIGTERM stress test).
- **Post-Phase-1 finding and fix: Worker and Reviewer were both non-functional in production (2026-08-17/18).**
  - **The discovery.** All 259+ tasks completed through Phase 0–0.8, including the entire soak-test history, went through a fallback code path, not a real LLM call. `src/worker.py`'s real LLM path (`_execute_gemini_http`/`_execute_openai_http`) only activates if an API key is present in the environment — none was ever set on the production VPS. Every "completed" task was a fixed template string with the issue title interpolated in, timestamped and cost-tracked as if it were real. `src/reviewer.py` had no LLM call at all — its "dynamic evaluation score" was pure keyword-overlap arithmetic against the Worker's own templated output, which is why scores clustered narrowly (0.876–0.980, the same band Phase 0.6's empirical analysis found and correctly attributed to weak correlation, without knowing the underlying cause was a template scoring itself). This was caught by first noticing the Worker's output text read like fixed boilerplate regardless of the actual issue, then confirming via direct code read.
  - **The fix — Worker.** Real key wired in (Gemini, via `GEMINI_API_KEY` in a systemd override, `.venv` has no separate isolation from system Python). Two retired-model issues found and fixed live against real API errors, not guessed: `gemini-1.5-flash` returns 404 (retired), `gemini-2.5-flash-lite` also returns 404 (retired same window) — Google's own 404 body pointed directly at the replacement, `gemini-3.5-flash-lite`, confirmed working with a real `200` response and genuine generated text before being set as the new default. `per_repo_target` in `src/opportunity.py` widened 25→100 (all 8 configured repos' near-term issue pool had already been exhausted by the fake-output era's task volume; confirmed via direct fetch test showing 171 genuinely new opportunities once widened).
  - **The fix — Reviewer.** Rewritten to make a real second LLM call (same model, `gemini-3.5-flash-lite`) that reads the original issue, the expected criteria, and the Worker's full output, and returns structured JSON (`passed`/`score`/`reasoning`). Heuristic keyword scoring kept only as an explicit no-key fallback, now labeled via a new `review_method` field (`llm_judged` vs `heuristic_fallback`) so the two can never again look identical in stored data. Verified via a real calibration test: correctly failed three known-fake pre-fix tasks (score 0.0 each, feedback correctly identifying "generic boilerplate") and correctly passed the first genuine post-fix task (score 0.95, feedback specific to the actual proposal).
  - **A real, confirmed limitation found the same night.** The LLM-judged Reviewer passed a CPython `_cursesmodule.c` proposal at 1.0 (maximum score) that a human read caught as containing invented C symbols (`WINDOW_ATTRIBUTES`, `WINDOW_RESTORE_ATTRIBUTES` — not real ncurses/CPython macros) and a fabricated Python API (`stdscr.getattrs()` — does not exist), plus a test that would fail to even import (missing `self` in a `unittest.TestCase` method). A second CPython task (`typeobject.c` memory management) was individually checked and failed the same way — also scored highly, also invented symbols, also self-labeled "conceptual patch" in its own output. **Confirmed pattern, not a one-off**: the Reviewer judges plausibility and structure, not actual correctness — it has no mechanism to verify a symbol exists or code compiles. This concentrates specifically in C/interpreter-internals work, where jargon-fluency is easy to fake and hard to catch without either compiling the code or being a domain expert.
  - **Contrasting calibration data, same night, other repos.** A `pydantic-core` proposal (Python-level API design, `to_json`/date-serialization) and an `ansible` proposal (Python-level module code, xattr preservation in the `copy` module) were both individually read in full and held up: real function/module names throughout, syntactically valid code and tests, no fabricated symbols. One further pydantic task was flagged and rejected specifically because the Reviewer's own feedback used the word "conceptual implementation" — the same hedge-language pattern that predicted both confirmed CPython failures.
  - **Working calibration read as of tonight (not yet exhaustively proven — see gaps below): Python-level application/library code (pydantic, ansible) is trustworthy from this pipeline; CPython C-internals work is not.** Treat this as a strong lead, not a settled fact — see "Known gaps" below.
  - **The human review gate — built, and a real bug in it caught before deployment.** `ARCHITECTURE.md` requires human approval before consequential actions; "marking AI-proposed work as done" now qualifies, since Worker output is real. Added `require_human_review_for_llm_judged` config flag: any task that is `review_method == llm_judged` and `passed == true` now routes to `WAITING_APPROVAL` instead of auto-completing, regardless of the global `auto_approve` setting. A new CLI tool, `tools/review_queue.py` (`list` / `explain <id>` / `approve <id>` / `reject <id> --reason`), lets a human inspect and decide without needing to read code — `list` shows score/feedback plus an automatic hedge-language warning (deterministic keyword scan for phrases like "conceptual patch", "illustrative", "the actual implementation" — not an LLM call, kept free and reliable); `explain` prints a full, clean, copy-paste-ready block (issue, instruction, full worker output, full review) suitable for pasting into a second AI chat for a non-coding human to get a plain-English second opinion.
  - **Real bug found and fixed before this reached the VPS**: the initial implementation only checked `request_approval()`'s existing approval status against `("APPROVED", "REJECTED")` — a task sitting at `PENDING` (the new hold state) fell through on its second scheduler tick to the default auto-approve path, silently auto-completing itself roughly one scheduler interval after entering the queue. Caught by tracing the code path before deployment, not after. Fixed by adding an explicit `PENDING` + hold-comment check that keeps returning the hold decision on every subsequent call. Regression test added that specifically simulates two calls to `request_approval()` on the same task (the exact scenario that exposed the bug) — confirmed to fail against the pre-fix code and pass against the fix.
  - **Real-world use, night one**: 13 tasks accumulated in the queue while tooling was being built. Two (one CPython, one ansible) were individually read in full and judged directly. The remaining 11 were decided by policy, generalizing from the two individually-verified results plus the pydantic result from earlier in the session: all 4 remaining CPython C-internals tasks rejected, all 7 remaining Python-level tasks (pydantic ×2, ansible ×4, one already-checked ansible) approved, one pydantic task rejected specifically for Reviewer-stated hedge language. Every decision logged with a real reason via `HUMAN_APPROVAL_GRANTED`/`HUMAN_APPROVAL_REJECTED` audit events, `verify_audit.py`/`replay_audit.py` updated to recognize both as valid `WAITING_APPROVAL → COMPLETED/BLOCKED` transitions.
  - **Pricing corrected**: both `worker.py` and `reviewer.py` were still costing calls at old Gemini 1.5 Flash rates ($0.000075/$0.000300 per 1k tokens). Updated to real, confirmed-current `gemini-3.5-flash-lite` pricing ($0.0003/$0.0025 per 1k tokens, i.e. $0.30/$2.50 per 1M), cross-checked against multiple independent pricing sources before applying.
  - **All ~259 pre-fix tasks were left in place, not deleted** — consistent with this project's immutable-audit-log principle (`ARCHITECTURE.md`, and the Company History design in `blueprint.txt` — corrections get a new entry, not a silent edit/delete). They remain queryable and distinguishable via `json_extract(worker_result_json, '$.model')` (old = `gemini-1.5-flash` + `review_method` absent/heuristic; new = `gemini-3.5-flash-lite` + `review_method` present). Anyone computing aggregate metrics from `tasks` going forward must filter on this or risk averaging real and fake work together.

  **Known gaps — deliberately left open, not yet closed:**
  - **ansible's "trustworthy" verdict rests on thin evidence**: one task individually read in full and held up; four more approved by policy riding on that one result plus the separate pydantic pattern. Not yet at the same confidence level as the pydantic (2 individually read) or cpython (2 individually read, both failed) findings. Treat ansible as "promising, not fully proven" until at least one more individual read confirms or contradicts it.
  - **The review gate is currently all-or-nothing per repo/domain**: every `llm_judged` pass across every repo waits for a human, even in domains already showing a strong reliable pattern (pydantic, ansible). This does not scale to genuine unattended autonomy — a human checking the queue is still required, just not per-task in real time (tasks wait indefinitely with no penalty, so this is not urgent, but it is a real limitation on the "autonomous" part of AgencyOS's mission). **Next planned step**: scope `require_human_review_for_llm_judged` per-repo instead of globally, so proven-reliable repos can auto-complete while CPython-style repos keep requiring review, tightening or loosening automatically as more calibration data comes in.
  - **No mechanism verifies actual code correctness** (compiles, applies, passes its own proposed tests) anywhere in the pipeline yet — both the Worker and the Reviewer are judged/judging on plausibility of written text, not execution. This is the deeper, structural version of the C-internals gap above and would benefit both domains, not just the unreliable one. Not started; a real future task, likely larger in scope than anything done tonight (sandboxed checkout, patch application, test execution against the real repo).

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
5. **A test skipped on the wrong platform is a test that hasn't run.**
   The SIGTERM stress test that actually proves the durability fix works
   was decorated to skip on Windows — and the whole review session had
   been running on Windows, so "8/8 pass" was true and also meaningless
   for the one test that mattered. Cross-platform-sensitive fixes need
   their proof run on the platform the bug actually occurs on, not
   wherever the IDE happens to be running.
6. **"I ran pkill" isn't the same as "the process is dead."** The third
   soak's stop sequence was reported twice as complete while `ps aux`
   still showed the original PID alive both times — caught only by
   insisting on the literal empty-output confirmation rather than
   accepting "exit 0" from the verification tools as implicit proof the
   process had stopped. A live process can still pass read-only checks
   against a stable-looking snapshot; that's not the same guarantee as
   a genuinely final, unchanging state.
7. **A check that silently no-ops on empty data isn't a passing check.**
   `verify_audit.py`'s telemetry-accuracy check only runs
   `if telemetry_events:` — with zero `TELEMETRY_REPORT` events in the
   entire closing soak, it never actually ran, and "exit 0" reflected
   that nothing was checked, not that everything was fine. This is
   exactly why the manual audit step exists as a distinct, human-only
   gate item separate from the automated tools — an automated verifier
   can only catch what it's given data to check against, and won't
   necessarily notice when the data itself is missing.
8. **When multiple tools/sessions are checking the same evidence, always
   confirm which artifact is actually in front of them.** A second AI
   assistant's audit cited the third soak's exact numbers (25.24h, 1320
   rows, 0 `TELEMETRY_REPORT`) as reason to block the Hermes gate — but
   that was the pre-fix soak the telemetry work was written to
   supersede, not the current fourth soak. Caught by asking for a live
   re-query against the actual VPS files rather than trusting numbers
   pasted from elsewhere. With four soak runs' worth of similarly-named
   files having existed over this phase, "which run is this" is now a
   standing question worth asking explicitly, not assuming.

## Standing decisions made in conversation (not yet in ROADMAP/ARCHITECTURE)

- **LLM tiering**: Planner = Opus 4.8 (or Sonnet 5 if cost-conscious),
  Worker = Sonnet 5, Reviewer/search/classification = Haiku 4.5. Sub-Haiku
  options (DeepSeek V4 Flash, Gemini Flash-Lite) identified for
  search/tagging tasks but explicitly **not wired in yet** — current cost
  (~$0.008/task) has ~1000x headroom under the $0.25 budget ceiling, so this
  is deferred until real volume justifies it.
- **Process supervision**: RESOLVED: systemd, not Hermes (2026-08-15). systemd keeps AgencyOS alive and restarts it on failure. AgencyOS is not modified to accommodate systemd; its existing SQLite startup recovery handles internal state. Hermes is evaluated and explicitly deferred for process supervision.
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

1. **Phase 1 — Kernel Foundations: COMPLETE & VERIFIED ✅.** Signed off
   2026-08-17.
2. **Per-repo human review gate tiering — real blocker before Phase 2
   scales.** `require_human_review_for_llm_judged` is currently
   global: every LLM-judged pass across every repo waits for a human,
   even in domains already showing a reliable pattern (pydantic,
   ansible). Phase 2 will multiply task volume across specialized
   workers; without this, the review queue becomes an unmanageable
   backlog rather than a targeted check. Scope the flag per-repo (or
   per-domain) so proven-reliable repos can auto-complete while
   CPython-style repos keep requiring review. Not started.
3. **Documentation gap: `blueprint.txt` vision not linked from
   `ROADMAP.md` — real confusion risk for future readers/IDE
   sessions.** `blueprint.txt` describes a full Revenue Acquisition
   department (multiple opportunity sources: bounty platforms,
   freelance marketplaces, referrals, inbound via company
   website/SEO — GitHub is explicitly only one of many) and
   distinguishes two different "Marketing" concepts (Growth
   Marketing — grows AgencyOS's own client pipeline, vs. a future
   Marketing Business Unit — sells marketing work to clients).
   `ROADMAP.md` currently gives no indication either of this exists;
   someone reading only `ROADMAP.md` would assume GitHub search is
   the entire opportunity-finding design. Add a short pointer in
   `ROADMAP.md`'s "Explicitly Deferred" section directing readers to
   `blueprint.txt` for the fuller picture, and note the Growth
   Marketing / Marketing Business Unit distinction explicitly. Not
   started.
4. **AGENTS.md / SKILL.md for Worker accuracy — follow-up from
   tonight's CPython findings.** The Worker has no grounding beyond
   its own training data — this is a likely contributor to the
   confirmed CPython C-internals failures (invented macros/functions
   that don't exist). A domain-specific skill file (e.g. real,
   verified CPython/ncurses API references) could reduce this failure
   mode, though it would not eliminate it — this does not replace the
   human review gate. Distinct from AGENTS.md (repo-level conventions)
   and WORKFLOW.md (process sequencing) — naming and scope not yet
   decided. Not started; lower priority than items 2–3.
5. **Deferred, not urgent — noted so it isn't lost**: a real policy
   decision is needed before adding any opportunity source beyond
   GitHub's sanctioned API — specifically, whether/how AgencyOS is
   allowed to interact with platforms using bot protection (e.g.
   Cloudflare-protected sites). Default position discussed: prefer
   platforms with a real, sanctioned API (same posture as GitHub);
   treat scraping a bot-protected site as explicitly out of scope
   without a specific, deliberate human decision. No source beyond
   GitHub exists yet, so this is not currently blocking anything.
6. Once items 2–3 above are done: Phase 2 — First Real Business Unit
   (Software Agency) becomes the next task. Not started.
7. **Optional, non-blocking**: opportunistically repeat the systemd
   kill test while a task is genuinely EXECUTING, to close the one
   caveat from the 2026-08-15 test. Not required before or during
   Phase 2 work.

