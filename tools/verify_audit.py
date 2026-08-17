#!/usr/bin/env python3
"""Standalone Audit & DB Verifier for AgencyOS.
Usage: python tools/verify_audit.py agencyos.db audit_log.jsonl

Checks for:
1. DB count vs JSONL count mismatch
2. Backwards timestamps in audit log
3. Illegal states or impossible state transitions (reusing LEGAL_TRANSITIONS from src.db)
4. Duplicate opportunity IDs beyond logged retries
5. Orphan approvals or budget rows
6. Budget mismatch (SUM(budget.amount) vs tasks execution JSON cost sum)
7. Telemetry mismatch vs raw DB counts
8. Unexplained heartbeat gaps (gap > 2x interval without WATCHDOG_WARNING, or > 5x without STALL_DETECTED)
"""

import os
import sys
import json
import sqlite3
from typing import Dict, Any, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.db import LEGAL_TRANSITIONS, TERMINAL_STATES

def verify_audit(db_path: str, log_path: str) -> Tuple[bool, List[str]]:
    errors = []

    if not os.path.exists(db_path):
        return False, [f"Database file not found: {db_path}"]
    if not os.path.exists(log_path):
        return False, [f"Log file not found: {log_path}"]

    # Read JSONL file
    jsonl_events = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                jsonl_events.append(json.loads(line))
            except json.JSONDecodeError as e:
                errors.append(f"JSONL parse error on line {line_no}: {e}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    db_audit_rows = cur.execute("SELECT id, event_type, payload_json, timestamp FROM audit_log ORDER BY id ASC").fetchall()

    # 1. DB count != JSONL line count
    if len(db_audit_rows) != len(jsonl_events):
        errors.append(f"DB/log count mismatch: SQLite audit_log has {len(db_audit_rows)} rows but JSONL file has {len(jsonl_events)} lines.")

    # 2. Backwards timestamps check in audit logs
    prev_ts = 0.0
    for row in db_audit_rows:
        ts = float(row["timestamp"])
        if ts < prev_ts:
            errors.append(f"Backwards timestamp detected in audit_log at row ID {row['id']} (ts {ts:.6f} < prev {prev_ts:.6f}).")
        prev_ts = ts

    # 3. Tasks table verification
    tasks_rows = cur.execute("SELECT task_id, opportunity_id, state, error_reason, worker_result_json, review_result_json FROM tasks").fetchall()

    task_retry_count: Dict[str, int] = {}
    for row in db_audit_rows:
        if row["event_type"] == "TASK_RETRY":
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            tid = payload.get("task_id") or payload.get("opportunity_id")
            if tid:
                task_retry_count[tid] = task_retry_count.get(tid, 0) + 1

    opp_counts: Dict[str, int] = {}
    for r in tasks_rows:
        opp_id = r["opportunity_id"]
        opp_counts[opp_id] = opp_counts.get(opp_id, 0) + 1
        st = r["state"]
        if st not in LEGAL_TRANSITIONS and st not in TERMINAL_STATES:
            errors.append(f"Task '{r['task_id']}' is in an illegal state '{st}'.")
        if st == "COMPLETED":
            w_res = json.loads(r["worker_result_json"]) if r["worker_result_json"] else None
            r_res = json.loads(r["review_result_json"]) if r["review_result_json"] else None
            if not w_res or "actual_cost" not in w_res:
                errors.append(f"Completed task '{r['task_id']}' missing worker actual_cost in worker_result_json.")
            if not r_res or "review_cost" not in r_res:
                errors.append(f"Completed task '{r['task_id']}' missing review_cost in review_result_json.")

    # Duplicate opportunity IDs beyond logged retries check
    for opp_id, count in opp_counts.items():
        allowed_retries = task_retry_count.get(opp_id, 0)
        if count > 1 + allowed_retries:
            errors.append(f"Duplicate opportunity ID '{opp_id}' found ({count} rows) exceeding logged retries ({allowed_retries}).")

    # 4. Check for impossible state transitions by walking audit_log
    task_state_history: Dict[str, List[str]] = {}
    for row in db_audit_rows:
        ev_type = row["event_type"]
        payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        tid = payload.get("task_id") or payload.get("opportunity_id")
        if not tid:
            continue

        implied_from_to = None
        if ev_type == "TASK_DISCOVERED": implied_from_to = ("DISCOVERED", "DISCOVERED")
        elif ev_type == "OPPORTUNITY_REJECTED": implied_from_to = ("DISCOVERED", "QUALITY_REJECTED")
        elif ev_type == "TASK_PLANNED": implied_from_to = ("DISCOVERED", "PLANNED")
        elif ev_type == "TASK_READY": implied_from_to = ("PLANNED", "READY")
        elif ev_type == "TASK_EXECUTING": implied_from_to = ("READY", "EXECUTING")
        elif ev_type == "WORKER_EXECUTED": implied_from_to = ("EXECUTING", "EXECUTING")
        elif ev_type == "REVIEW_COMPLETED": implied_from_to = ("EXECUTING", "REVIEW")
        elif ev_type == "TASK_WAITING_APPROVAL": implied_from_to = ("REVIEW", "WAITING_APPROVAL")
        elif ev_type == "TASK_COMPLETED":
            implied_from_to = ("WAITING_APPROVAL", "COMPLETED")
            if "cost" not in payload:
                errors.append(f"TASK_COMPLETED event for task '{tid}' missing 'cost' in payload.")
        elif ev_type == "BUDGET_BLOCKED": implied_from_to = ("PLANNED", "BLOCKED")
        elif ev_type == "WORKER_FAILED": implied_from_to = ("EXECUTING", "WORKER_FAILED")
        elif ev_type == "TASK_BLOCKED": implied_from_to = ("WAITING_APPROVAL", "BLOCKED")

        if implied_from_to:
            from_st, to_st = implied_from_to
            if tid not in task_state_history:
                task_state_history[tid] = [from_st]
            current_st = task_state_history[tid][-1]
            if current_st != from_st and current_st in TERMINAL_STATES:
                errors.append(f"Impossible state transition for task '{tid}': attempting transition '{current_st}' -> '{to_st}' from terminal state.")
            elif to_st not in LEGAL_TRANSITIONS.get(current_st, set()) and current_st != to_st:
                errors.append(f"Impossible state transition for task '{tid}': '{current_st}' -> '{to_st}' is not legal from '{current_st}'.")
            task_state_history[tid].append(to_st)

    # 5. Orphan approvals and budget rows check
    orphan_approvals = cur.execute("""
        SELECT a.id, a.opportunity_id FROM approvals a
        LEFT JOIN tasks t ON a.opportunity_id = t.opportunity_id
        WHERE t.opportunity_id IS NULL
    """).fetchall()
    for o in orphan_approvals:
        errors.append(f"Orphan approval row found: ID '{o['id']}' with unresolvable opportunity_id '{o['opportunity_id']}'.")

    orphan_budget = cur.execute("""
        SELECT b.id, b.opportunity_id FROM budget b
        LEFT JOIN tasks t ON b.opportunity_id = t.opportunity_id
        WHERE t.opportunity_id IS NULL AND b.opportunity_id != 'migration'
    """).fetchall()
    for b in orphan_budget:
        errors.append(f"Orphan budget row found: ID '{b['id']}' with unresolvable opportunity_id '{b['opportunity_id']}'.")

    # 6. Budget mismatch check: SUM(budget.amount) vs tasks JSON cost sum
    total_budget = cur.execute("SELECT SUM(amount) as total FROM budget").fetchone()["total"] or 0.0
    json_cost_sum = cur.execute("""
        SELECT SUM(
            COALESCE(json_extract(worker_result_json, '$.actual_cost'), 0.0) +
            COALESCE(json_extract(review_result_json, '$.review_cost'), 0.0)
        ) as total FROM tasks
    """).fetchone()["total"] or 0.0

    if round(abs(float(total_budget) - float(json_cost_sum)), 6) != 0.0:
        errors.append(f"Budget mismatch: SUM(budget.amount) (${total_budget:.6f}) does not match JSON extracted task costs (${json_cost_sum:.6f}).")

    # 7. Extended Telemetry report vs raw DB counts check
    telemetry_events = [r for r in db_audit_rows if r["event_type"] == "TELEMETRY_REPORT"]
    if telemetry_events:
        last_tel = json.loads(telemetry_events[-1]["payload_json"])
        tel_data = last_tel.get("telemetry", {})

        raw_tasks = len(tasks_rows)
        raw_completed = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'COMPLETED'").fetchone()["cnt"]
        raw_worker_failed = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'WORKER_FAILED'").fetchone()["cnt"]
        raw_quality_rejected = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'QUALITY_REJECTED'").fetchone()["cnt"]
        raw_budget_blocked = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'BLOCKED' AND error_reason LIKE 'BUDGET_BLOCKED:%'").fetchone()["cnt"]
        raw_approval_rejected = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'BLOCKED' AND (error_reason NOT LIKE 'BUDGET_BLOCKED:%' OR error_reason IS NULL)").fetchone()["cnt"]

        raw_total_cost = round(float(cur.execute("SELECT SUM(amount) as total FROM budget").fetchone()["total"] or 0.0), 6)

        from datetime import datetime, timezone
        today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        raw_today_spend = round(float(cur.execute("SELECT SUM(amount) as total FROM budget WHERE date_str = ?", (today_date_str,)).fetchone()["total"] or 0.0), 6)

        if tel_data.get("total_tasks") is not None and tel_data["total_tasks"] != raw_tasks:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT total_tasks ({tel_data['total_tasks']}) != raw DB tasks count ({raw_tasks}).")
        if tel_data.get("successful_executions") is not None and tel_data["successful_executions"] != raw_completed:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT successful_executions ({tel_data['successful_executions']}) != raw DB COMPLETED count ({raw_completed}).")
        if tel_data.get("worker_failed_executions") is not None and tel_data["worker_failed_executions"] != raw_worker_failed:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT worker_failed_executions ({tel_data['worker_failed_executions']}) != raw DB WORKER_FAILED count ({raw_worker_failed}).")
        if tel_data.get("quality_rejected_executions") is not None and tel_data["quality_rejected_executions"] != raw_quality_rejected:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT quality_rejected_executions ({tel_data['quality_rejected_executions']}) != raw DB QUALITY_REJECTED count ({raw_quality_rejected}).")
        if tel_data.get("budget_blocked_executions") is not None and tel_data["budget_blocked_executions"] != raw_budget_blocked:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT budget_blocked_executions ({tel_data['budget_blocked_executions']}) != raw DB BUDGET_BLOCKED count ({raw_budget_blocked}).")
        if tel_data.get("approval_rejected_executions") is not None and tel_data["approval_rejected_executions"] != raw_approval_rejected:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT approval_rejected_executions ({tel_data['approval_rejected_executions']}) != raw DB APPROVAL_REJECTED count ({raw_approval_rejected}).")
        if tel_data.get("total_cost") is not None and round(abs(float(tel_data["total_cost"]) - raw_total_cost), 6) != 0.0:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT total_cost (${tel_data['total_cost']:.6f}) != raw DB budget total (${raw_total_cost:.6f}).")
        if tel_data.get("today_cumulative_spend") is not None and round(abs(float(tel_data["today_cumulative_spend"]) - raw_today_spend), 6) != 0.0:
            errors.append(f"Telemetry mismatch: TELEMETRY_REPORT today_cumulative_spend (${tel_data['today_cumulative_spend']:.6f}) != raw DB today spend (${raw_today_spend:.6f}).")
        if tel_data.get("categories_partition_total") is False:
            errors.append("Telemetry mismatch: TELEMETRY_REPORT categories_partition_total is False.")
        if tel_data.get("cost_reconciled") is False:
            errors.append("Telemetry mismatch: TELEMETRY_REPORT cost_reconciled is False.")

    # 8. Heartbeat gap and missing watchdog warning/stall checks using backed-off expected_interval
    last_hb_ts = 0.0
    last_expected_interval = 30.0
    for row in db_audit_rows:
        if row["event_type"] == "SCHEDULER_HEARTBEAT":
            ts = float(row["timestamp"])
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
            expected_interval = float(payload.get("expected_interval") or payload.get("interval_sec") or last_expected_interval)

            if last_hb_ts > 0.0:
                gap = ts - last_hb_ts
                if gap > 5.0 * expected_interval:
                    has_stall = any(r["event_type"] == "STALL_DETECTED" and last_hb_ts <= float(r["timestamp"]) <= ts for r in db_audit_rows)
                    if not has_stall:
                        errors.append(f"Unexplained heartbeat gap ({gap:.1f}s > 5x interval {expected_interval:.1f}s) between timestamps {last_hb_ts:.1f} and {ts:.1f} without STALL_DETECTED event.")
                elif gap > 2.0 * expected_interval:
                    has_warn = any(r["event_type"] == "WATCHDOG_WARNING" and last_hb_ts <= float(r["timestamp"]) <= ts for r in db_audit_rows)
                    if not has_warn:
                        errors.append(f"Unexplained heartbeat gap ({gap:.1f}s > 2x interval {expected_interval:.1f}s) between timestamps {last_hb_ts:.1f} and {ts:.1f} without WATCHDOG_WARNING event.")
            last_hb_ts = ts
            last_expected_interval = expected_interval

    conn.close()
    return len(errors) == 0, errors

def main():
    if len(sys.argv) < 3:
        print("Usage: python tools/verify_audit.py <agencyos.db> <audit_log.jsonl>")
        sys.exit(1)

    db_path = sys.argv[1]
    log_path = sys.argv[2]

    passed, errors = verify_audit(db_path, log_path)
    if passed:
        print(f"[VERIFY SUCCESS] All audit and DB verification checks passed cleanly for {db_path} and {log_path}.")
        sys.exit(0)
    else:
        print(f"[VERIFY FAILED] Found {len(errors)} verification errors:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

if __name__ == "__main__":
    main()
