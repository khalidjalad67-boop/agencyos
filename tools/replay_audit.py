#!/usr/bin/env python3
"""Standalone Replay Auditor for AgencyOS.
Usage: python tools/replay_audit.py agencyos.db audit_log.jsonl

Reconstructs task states, total spend, and approval statuses purely by replaying audit_log.jsonl
from scratch sequentially, then diffs reconstructed state against what is stored in agencyos.db.
Exits non-zero (1) if any difference is found.
"""

import os
import sys
import json
import sqlite3
from typing import Dict, Any, List, Tuple

def replay_audit_log(log_path: str) -> Tuple[Dict[str, Dict[str, Any]], float, Dict[str, str]]:
    """Reconstructs tasks dict, total spend float, and approvals dict by replaying audit_log.jsonl events."""
    reconstructed_tasks: Dict[str, Dict[str, Any]] = {}
    task_worker_costs: Dict[str, float] = {}
    reconstructed_spend = 0.0
    reconstructed_approvals: Dict[str, str] = {}

    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            ev_type = entry.get("event_type")
            payload = entry.get("payload", {})
            tid = payload.get("task_id") or payload.get("opportunity_id")

            if ev_type == "TASK_DISCOVERED" and tid:
                reconstructed_tasks[tid] = {"state": "DISCOVERED", "error_reason": None}
            elif ev_type == "OPPORTUNITY_REJECTED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "QUALITY_REJECTED"
                reconstructed_tasks[tid]["error_reason"] = f"QUALITY_REJECTED: {payload.get('reason')}"
            elif ev_type == "TASK_PLANNED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "PLANNED"
            elif ev_type == "BUDGET_BLOCKED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "BLOCKED"
                reconstructed_tasks[tid]["error_reason"] = f"BUDGET_BLOCKED: {payload.get('reason')}"
            elif ev_type == "TASK_READY" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "READY"
            elif ev_type == "TASK_EXECUTING" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "EXECUTING"
            elif ev_type == "WORKER_FAILED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "WORKER_FAILED"
                reconstructed_tasks[tid]["error_reason"] = payload.get("error")
            elif ev_type == "WORKER_EXECUTED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "EXECUTING"
                w_res = payload.get("worker_result", {})
                if isinstance(w_res, dict):
                    task_worker_costs[tid] = float(w_res.get("actual_cost", 0.0))
            elif ev_type == "REVIEW_COMPLETED" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "REVIEW"
                w_cost = task_worker_costs.get(tid, 0.0)
                r_res = payload.get("review_result", {})
                r_cost = float(r_res.get("review_cost", 0.0)) if isinstance(r_res, dict) else 0.0
                reconstructed_spend += (w_cost + r_cost)
            elif ev_type == "TASK_WAITING_APPROVAL" and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "WAITING_APPROVAL"
            elif ev_type in ("TASK_COMPLETED", "HUMAN_APPROVAL_GRANTED") and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "COMPLETED"
                reconstructed_approvals[tid] = "APPROVED"
            elif ev_type in ("TASK_BLOCKED", "HUMAN_APPROVAL_REJECTED") and tid:
                if tid not in reconstructed_tasks: reconstructed_tasks[tid] = {}
                reconstructed_tasks[tid]["state"] = "BLOCKED"
                reconstructed_tasks[tid]["error_reason"] = f"APPROVAL_REJECTED: {payload.get('reason')}"
                reconstructed_approvals[tid] = "REJECTED"

    return reconstructed_tasks, round(reconstructed_spend, 6), reconstructed_approvals

def verify_replay(db_path: str, log_path: str) -> Tuple[bool, List[str]]:
    diffs = []
    if not os.path.exists(db_path):
        return False, [f"Database file not found: {db_path}"]
    if not os.path.exists(log_path):
        return False, [f"Log file not found: {log_path}"]

    recon_tasks, recon_spend, recon_apprs = replay_audit_log(log_path)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    db_tasks = {r["task_id"]: dict(r) for r in cur.execute("SELECT task_id, state, error_reason FROM tasks").fetchall()}
    db_spend = round(cur.execute("SELECT SUM(amount) as total FROM budget").fetchone()["total"] or 0.0, 6)
    db_apprs = {r["opportunity_id"]: r["status"] for r in cur.execute("SELECT opportunity_id, status FROM approvals").fetchall()}
    conn.close()

    # Diff 1: Task count diff
    if len(recon_tasks) != len(db_tasks):
        diffs.append(f"Task count mismatch: Replayed audit log has {len(recon_tasks)} tasks, DB has {len(db_tasks)} tasks.")

    # Diff 2: Individual task state diff
    for tid, rtask in recon_tasks.items():
        if tid not in db_tasks:
            diffs.append(f"Task '{tid}' present in audit log replay but missing from SQLite database.")
        else:
            dtask = db_tasks[tid]
            if rtask["state"] != dtask["state"]:
                diffs.append(f"Task state mismatch for '{tid}': Replayed state '{rtask['state']}' != DB state '{dtask['state']}'.")

    # Diff 3: Total budget spend diff
    if recon_spend != db_spend:
        diffs.append(f"Total budget spend mismatch: Replayed spend (${recon_spend:.6f}) != DB budget table sum (${db_spend:.6f}).")

    # Diff 4: Approval status diff
    for opp_id, status in recon_apprs.items():
        if opp_id not in db_apprs:
            diffs.append(f"Approval for '{opp_id}' present in audit log replay but missing from SQLite approvals table.")
        elif db_apprs[opp_id] != status:
            diffs.append(f"Approval status mismatch for '{opp_id}': Replayed status '{status}' != DB status '{db_apprs[opp_id]}'.")

    return len(diffs) == 0, diffs

def main():
    if len(sys.argv) < 3:
        print("Usage: python tools/replay_audit.py <agencyos.db> <audit_log.jsonl>")
        sys.exit(1)

    db_path = sys.argv[1]
    log_path = sys.argv[2]

    passed, diffs = verify_replay(db_path, log_path)
    if passed:
        print(f"[REPLAY SUCCESS] Replayed audit log matches SQLite DB state 100% with zero diffs for {db_path} and {log_path}.")
        sys.exit(0)
    else:
        print(f"[REPLAY FAILED] Found {len(diffs)} state differences during audit replay:")
        for d in diffs:
            print(f"  - {d}")
        sys.exit(1)

if __name__ == "__main__":
    main()
