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
- **Phase 0.8 (Engine Stabilization): ALL CHECKPOINTS 0.8A1–0.8E
  COMPLETE & VERIFIED. Manual audit ran and found one real gap — fix in
  progress, fresh soak needed after.**
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
  - **Fix needed before re-audit**: `main.py` must periodically log a
    real `TELEMETRY_REPORT` event (not just print to console) during the
    autonomous loop, plus a regression test confirming a live-run event's
    values match a direct `get_telemetry_metrics()` call. This is narrow
    and well-understood, unlike the previous two soak-invalidating bugs
    — but per the Hermes gate's own rule, still requires a fresh soak
    once fixed, since it changes what's written to `audit_log.jsonl`.
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

1. Fix the confirmed gap: `main.py`'s autonomous loop must periodically
   log a real `TELEMETRY_REPORT` audit event (`logger.log_event(
   "TELEMETRY_REPORT", {"telemetry": db.get_telemetry_metrics()})`), not
   just print telemetry to console each tick. Add a regression test
   confirming a live-run event's values match a direct
   `get_telemetry_metrics()` call at the same moment. Run the full
   suite. Do not touch the durability fix (`PRAGMA synchronous=FULL`,
   the SIGTERM handler) or any other 0.8A–0.8E code while doing this.
2. Once fixed and tested: a fresh soak is required — the Hermes gate's
   own rule is "no exceptions for 'it's probably fine now,'" and this
   change alters what gets written to `audit_log.jsonl`. Expected to be
   narrower/faster to resolve than the previous two soak-invalidating
   bugs, but that's an expectation, not a substitute for re-running it.
3. Re-run the manual audit against the new soak's real files — same
   rigor as before, specifically re-checking that `TELEMETRY_REPORT`
   events now exist and their values are correct against raw DB counts.
4. Once the manual audit passes: the full Hermes gate is satisfied
   (0.8A–0.8E, the clean soak, both verification tools clean, manual
   audit). Migrate the pipeline to Hermes.
5. Only then begin Phase 1 (Event Bus / Policy Engine — build only what
   Hermes doesn't already cover).
