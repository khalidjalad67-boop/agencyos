# AgencyOS — systemd Boundary Test: Directive, Unit File, Doc Updates

Resolves the process-supervision question ahead of Phase 1. Supersedes
all prior Hermes-as-supervisor drafts. Hermes is deferred, not deleted —
see the Track 2 note preserved below.

---

## 1. Finalized directive

```
ENGINEERING DIRECTIVE — systemd Process Supervision Boundary Test

AgencyOS 0.8 is frozen and passed its stabilization gate (v0.8-stable).
Process supervision is delegated to systemd. This directive is the sole
authority on this decision — do not reinterpret it from prior Hermes-
related conversation history.

DECISION (do not re-litigate without new evidence):
  - Process supervision is infrastructure, not an AI-agent responsibility.
  - Hermes is explicitly OUT of the execution/infrastructure layer.
    Hermes will not supervise, restart, or access AgencyOS. This is
    deferred, not rejected — Hermes remains a candidate for a future,
    unrelated role (Learning System / Asset Factory, roughly Phase 4),
    which has no bearing on this decision.
  - systemd keeps AgencyOS alive and restarts it on death. It has zero
    access to task state, SQLite, or execution decisions — its only
    inputs are process exit/restart, nothing else.
  - AgencyOS is not modified to "accommodate" systemd. If the existing
    startup/recovery mechanism already handles a cold process start
    correctly (already proven across four soaks), it needs nothing
    systemd-specific added. Any code change proposed "to work with
    systemd" is out of scope unless it's fixing a genuine pre-existing
    bug systemd's restart exposes — not a bug introduced by this task.

1. SUPERVISOR LOCATION
   - A systemd unit file (see Section 2), external to the AgencyOS
     Python codebase. No changes to src/, no new module, no new
     dependency added to AgencyOS itself.
   - Restart policy: Restart=on-failure (or always — decide based on
     whether a clean intentional stop should also auto-restart; default
     to on-failure unless you have a specific reason for always).

2. ENVIRONMENT GATE (fast — no third-party service to verify this time)
   - Confirm systemd is available on the target host (it is, on any
     standard Linux VPS) and confirm you have permission to install a
     unit (root or sudo).
   - Confirm the exact start command AgencyOS needs (entry point,
     working directory, required env vars) — get this from how it's
     started manually today, don't guess.

3. BOUNDARY TEST
   - Start AgencyOS under systemd (systemctl start agencyos).
   - Confirm healthy operation; capture a DB/audit snapshot (task count,
     opportunity_id set, current states, latest audit_log id/timestamp).
   - kill -9 the AgencyOS process directly (not systemctl stop — the
     test is proving systemd detects an actual crash, not a managed
     stop).
   - Confirm systemd restarts it automatically (journalctl -u agencyos,
     or systemctl status agencyos showing a fresh PID and recent start
     time).
   - Confirm AgencyOS's own recovery lifecycle fires
     (STARTUP_RECOVERY_STARTED / STARTUP_RECOVERY_COMPLETED in the
     audit log, matching the same event shape already proven in prior
     soaks).
   - Confirm the interrupted task resumes and reaches exactly one
     terminal state.

4. ISOLATION RULE
   systemd never reads, writes, or decides AgencyOS task/DB state. If
   the unit file, a wrapper script, or anything added for this task
   touches agencyos.db or task state in any way, that's a FAIL — the
   boundary has been violated.

5. INVARIANT
   No task or opportunity may be created, duplicated, lost, or executed
   twice as a result of the restart. Only the interrupted task's
   legitimately persisted state may advance, and every change between
   the pre-kill and post-recovery snapshots must be traceable to a
   specific, logged recovery-lifecycle event.

6. EVIDENCE REQUIRED
   - kill timestamp
   - systemd's restart timestamp (from journalctl/systemctl status, not
     paraphrased)
   - the actual RECOVERY_STARTED / RECOVERY_COMPLETED log lines from
     AgencyOS's own audit log
   - DB state comparison before vs. after: task count, opportunity_id
     set, and every difference mapped to a specific recovery event
   - the resumed task's full state progression and confirmation of
     exactly one terminal state
   - confirmation of no duplicate worker/reviewer execution or cost
   - git diff of the AgencyOS repo — must be empty. A non-empty diff is
     itself a FAIL signal unless it's fixing a pre-existing bug systemd
     exposed (must be called out explicitly, not silently included)

7. GATE
   - PASS (all of the above, with evidence) → Phase 1 begins.
   - FAIL on any point → STOP. Fix only the specific failing item. Do
     not proceed to Phase 1. Do not add scope.

No speculative code. No AgencyOS refactor beyond a genuine pre-existing
bug fix. No "probably working." The only thing being proven: systemd
can keep the already-proven AgencyOS recovery mechanism alive without
becoming part of AgencyOS's execution/state machinery.
```

---

## 2. Reference systemd unit file

Adjust `User`, `WorkingDirectory`, `ExecStart`, and `Environment` lines
to match the real deployment — these are placeholders, not verified
against your actual VPS paths.

```ini
# /etc/systemd/system/agencyos.service
[Unit]
Description=AgencyOS autonomous execution loop
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=agencyos
WorkingDirectory=/opt/agencyos
ExecStart=/opt/agencyos/venv/bin/python /opt/agencyos/main.py
Restart=on-failure
RestartSec=5
# Optional but recommended: cap restart storms if something is
# genuinely broken, rather than restart-looping forever.
StartLimitIntervalSec=300
StartLimitBurst=5

# No database or task-state env vars beyond what AgencyOS already
# needs to run standalone — systemd must not inject anything that
# changes AgencyOS's own behavior.
EnvironmentFile=-/opt/agencyos/.env

[Install]
WantedBy=multi-user.target
```

Commands for the boundary test itself:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agencyos
sudo systemctl status agencyos          # confirm healthy, note PID

# --- snapshot before ---
sqlite3 agencyos.db "SELECT COUNT(*) FROM tasks;"
sqlite3 agencyos.db "SELECT opportunity_id, state FROM tasks ORDER BY opportunity_id;"
tail -5 audit_log.jsonl

# --- kill ---
sudo kill -9 <PID>
date -u +%s   # record kill timestamp

# --- wait for systemd restart ---
sudo systemctl status agencyos          # confirm new PID, recent start time
sudo journalctl -u agencyos --since "1 min ago"

# --- snapshot after ---
sqlite3 agencyos.db "SELECT COUNT(*) FROM tasks;"
sqlite3 agencyos.db "SELECT opportunity_id, state FROM tasks ORDER BY opportunity_id;"
tail -20 audit_log.jsonl | grep -E "RECOVERY|opportunity_id_of_interrupted_task"

git status   # must be clean
```

---

## 3. Doc updates

### PROJECT_STATUS.md — replace the Hermes standing-decision bullet with:

> **Process supervision — RESOLVED: systemd, not Hermes.** Process
> supervision is infrastructure, not an AI-agent responsibility. Using
> an AI agent to poll `ps` and interpret liveness adds a
> non-deterministic component to a deterministic job, and contradicts
> `ARCHITECTURE.md`'s own principle of preferring policy/config over
> agents until reasoning is genuinely required. systemd (`Restart=on-
> failure`) supervises the AgencyOS process with zero access to task
> state, SQLite, or execution decisions. AgencyOS is not modified to
> accommodate systemd — its existing, already-proven startup/recovery
> routine is sufficient as-is. See the systemd boundary-test directive
> for the exact test and evidence requirements. This gates Phase 1.
>
> **Hermes — deferred, not rejected.** Confirmed via Paperclip's
> published adapters (`hermes_local`/`hermes_gateway`, built by Nous
> Research) that Hermes Agent has no native process-supervisor
> primitive — it's designed to be the worker being orchestrated, not
> the thing watching another process's liveness. This confirms the
> earlier finding independently. Hermes remains a candidate for a
> **later, unrelated** role: the Learning System / Asset Factory
> (Phase 4), built on Hermes's native skill-creation and memory system,
> once that capability has a real caller. It has zero role between now
> and Phase 4.
>
> **Paperclip (paperclipai/paperclip) — evaluated as reference
> material, not adopted.** An open-source multi-agent orchestration
> platform with real production usage and a public issue tracker
> showing genuine hardening work (orphaned-process recovery, stale-run
> reaper bugs, recovery-loop depth limits). Confirmed to lack an
> equivalent to AgencyOS's forensic replay verification
> (`verify_audit.py`/`replay_audit.py`) — several of their currently
> open issues are exactly the class of bug that tooling would catch.
> Two ideas banked as future design considerations for Phase 1 (see
> `ARCHITECTURE.md`), not implemented now — two-callers rule still
> applies. No integration planned.

### ARCHITECTURE.md — add after "Persistence & State Integrity":

```markdown
---

## External Process Supervision

AgencyOS's process is supervised externally via **systemd**
(`Restart=on-failure`), not by any AI agent. Process liveness detection
and restart is a deterministic infrastructure concern — introducing an
AI agent to poll and interpret process state adds a non-deterministic
component to a job that has a decades-old deterministic solution, and
violates the Design Principle of preferring policy/config over agents
until reasoning is genuinely required.

The supervisor has zero access to task state, SQLite, or execution
decisions — its only inputs are process exit and restart. AgencyOS is
not modified to accommodate the supervisor; its own startup/recovery
routine (`STARTUP_RECOVERY_STARTED`/`COMPLETED`) is fully responsible
for correctness after any restart, supervised or not.

Hermes (Nous Research's Hermes Agent) was evaluated and rejected for
this specific role — confirmed to have no native process-supervisor
primitive; its own ecosystem (via Paperclip's adapters) treats it as a
managed worker, not a process watcher. Hermes remains a candidate for
a later, unrelated role (Learning System / Asset Factory, see Phase 4
in ROADMAP.md) — that track is independent of this section and does
not reopen this decision.

## Future Design Considerations (not implemented — two-callers rule
## still applies; recorded so a future build doesn't reinvent these)

- **Event Bus, when it eventually has two real callers**: include
  event coalescing — repeated triggers for the same task/worker before
  it wakes should collapse into one wake, not fire redundantly.
- **Policy Engine, when a single global budget config becomes
  insufficient**: design toward scoped policies (e.g. task /
  opportunity / department / worker / provider / model) rather than
  a flat global ceiling. Do not build this shape until a second real
  business unit actually needs differentiated budgets.
```

### ROADMAP.md — update the "Next Prompt for the Agentic IDE" block's task instructions:

Replace the Hermes-migration task description with:

```
YOUR TASK NOW: systemd boundary test, per the finalized directive
(see systemd-boundary-test.md / ARCHITECTURE.md "External Process
Supervision"). Hermes migration is deferred — do not build any Hermes
integration as part of this task.

  AgencyOS running under systemd supervision
    -> kill -9 AgencyOS
    -> systemd detects the death and restarts it
    -> AgencyOS's own STARTUP_RECOVERY_STARTED/COMPLETED fire correctly
    -> the interrupted task resumes
    -> no duplicate worker/reviewer cost
    -> task reaches COMPLETED
    -> git diff is clean

Only after this passes does Phase 1 begin.
```
