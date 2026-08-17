#!/usr/bin/env python3
"""AgencyOS Human Review Queue Tool.
CLI interface to inspect, explain, and manually approve or reject LLM-judged tasks in WAITING_APPROVAL.

Commands:
  python tools/review_queue.py list [--db agencyos.db]
  python tools/review_queue.py explain <task_id> [--db agencyos.db]
  python tools/review_queue.py approve <task_id> [--db agencyos.db]
  python tools/review_queue.py reject <task_id> --reason "<text>" [--db agencyos.db]
"""

import sys
import os
import time
import json
import argparse
from typing import List, Dict, Any, Optional

from src.db import Database, resolve_db_path
from src.worker import WorkerResult
from src.reviewer import ReviewResult

HEDGE_PHRASES: List[str] = [
    "conceptual patch",
    "conceptual implementation",
    "illustrative",
    "in principle",
    "placeholder",
    "for demonstration purposes",
    "the actual implementation",
    "simplified",
    "pseudo-code",
    "pseudocode",
    "not a full implementation",
    "approach would be",
]

def detect_hedge_language(text: str) -> List[str]:
    """Scans text for known hedge / placeholder phrases (case-insensitive)."""
    if not text:
        return []
    text_lower = text.lower()
    return [phrase for phrase in HEDGE_PHRASES if phrase in text_lower]

def list_queue(db: Optional[Database] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Queries DB for all tasks in WAITING_APPROVAL state with review_method == 'llm_judged'."""
    database = db or Database(db_path=db_path)
    waiting_tasks = database.get_tasks_by_state("WAITING_APPROVAL")
    
    llm_judged_tasks = []
    for task in waiting_tasks:
        rev_res = task.get("review_result") or {}
        if rev_res.get("review_method") == "llm_judged":
            llm_judged_tasks.append(task)
            
    resolved_path = database.db_path
    print(f"=== AgencyOS Human Review Queue ({len(llm_judged_tasks)} pending) ===")
    if not llm_judged_tasks:
        print("No LLM-judged tasks pending in WAITING_APPROVAL queue.")
        return llm_judged_tasks

    for idx, t in enumerate(llm_judged_tasks, 1):
        task_id = t["task_id"]
        repo = t.get("repo", "unknown")
        title = t.get("title", "No Title")
        rev_res = t.get("review_result", {})
        score = rev_res.get("score", 0.0)
        feedback = rev_res.get("feedback", "")
        feedback_trunc = (feedback[:200] + "...") if len(feedback) > 200 else feedback
        
        wres = t.get("worker_result") or {}
        worker_output = wres.get("output", "") if isinstance(wres, dict) else ""
        hedge_phrases = detect_hedge_language(worker_output)

        print(f"\n[{idx}] Task ID : {task_id}")
        print(f"    Repo    : {repo}")
        print(f"    Title   : {title}")
        print(f"    Score   : {score}")
        print(f"    Feedback: {feedback_trunc}")
        if hedge_phrases:
            formatted_phrases = ", ".join(f"'{p}'" for p in hedge_phrases)
            print(f"    [WARNING] HEDGE LANGUAGE DETECTED: {formatted_phrases}")
        print(f"    Inspect : sqlite3 {resolved_path} \"SELECT worker_result_json FROM tasks WHERE task_id='{task_id}';\"")
    
    print("\n" + "=" * 55)
    return llm_judged_tasks

def explain_task(task_id: str, db: Optional[Database] = None, db_path: Optional[str] = None) -> bool:
    """Prints a clean, untruncated inspection block formatted for human review and external AI second opinions."""
    database = db or Database(db_path=db_path)
    task = database.get_task(task_id)
    if not task:
        print(f"[ERROR] Task '{task_id}' not found in database.")
        return False

    repo = task.get("repo", "unknown")
    title = task.get("title", "No Title")
    state = task.get("state", "unknown")
    task_spec = task.get("task_spec") or {}
    task_instruction = task_spec.get("task", "N/A")
    expected_output = task_spec.get("expected_output", "N/A")

    worker_result = task.get("worker_result") or {}
    worker_output = worker_result.get("output", "N/A")
    worker_model = worker_result.get("model", "unknown")

    review_result = task.get("review_result") or {}
    review_score = review_result.get("score", "N/A")
    review_passed = review_result.get("passed", "N/A")
    review_feedback = review_result.get("feedback", "N/A")
    review_method = review_result.get("review_method", "heuristic_fallback" if "score" in review_result else "unknown")

    hedge_phrases = detect_hedge_language(worker_output)

    print("=" * 70)
    print(f"AGENCYOS TASK REVIEW EXPLANATION: {task_id}")
    print("=" * 70)
    print(f"\nSTATUS: {state}")
    print(f"REPOSITORY: {repo}")
    print(f"ISSUE TITLE: {title}")
    
    print("\n--- TASK SPECIFICATION & INSTRUCTION ---")
    print(f"Instruction:\n{task_instruction}")
    if expected_output and expected_output != "N/A":
        print(f"\nExpected Criteria:\n{expected_output}")

    print("\n--- WORKER IMPLEMENTATION OUTPUT ---")
    print(f"Model: {worker_model}")
    print(f"Output:\n{worker_output}")

    print("\n--- REVIEWER EVALUATION ---")
    print(f"Method : {review_method}")
    print(f"Passed : {review_passed}")
    print(f"Score  : {review_score}")
    print(f"Feedback:\n{review_feedback}")

    print("\n--- HEDGE LANGUAGE DETECTION ---")
    if hedge_phrases:
        formatted_phrases = ", ".join(f"'{p}'" for p in hedge_phrases)
        print(f"[WARNING] Hedge language detected ({len(hedge_phrases)} phrases): {formatted_phrases}")
        print("Note: The proposal may contain conceptual placeholders or non-executable pseudocode.")
    else:
        print("None detected (No hedge / placeholder phrases found in worker output).")

    print("\n" + "=" * 70)
    return True

def approve_task(task_id: str, db: Optional[Database] = None, db_path: Optional[str] = None) -> bool:
    """Transitions a task from WAITING_APPROVAL to COMPLETED via execute_atomic_transition."""
    database = db or Database(db_path=db_path)
    task = database.get_task(task_id)
    if not task:
        print(f"[ERROR] Task '{task_id}' not found in database.")
        return False
        
    if task["state"] != "WAITING_APPROVAL":
        print(f"[ERROR] Task '{task_id}' is in state '{task['state']}', not 'WAITING_APPROVAL'. Cannot approve.")
        return False

    wres_data = task.get("worker_result") or {}
    rres_data = task.get("review_result") or {}
    worker_cost = float(wres_data.get("actual_cost", 0.0))
    review_cost = float(rres_data.get("review_cost", 0.0))
    total_cost = round(worker_cost + review_cost, 6)

    now_time = time.time()
    task["state"] = "COMPLETED"
    
    database.execute_atomic_transition(
        task,
        audit_event=("HUMAN_APPROVAL_GRANTED", {
            "task_id": task_id,
            "opportunity_id": task["opportunity_id"],
            "cost": total_cost,
            "timestamp": now_time
        }),
        approval_record=(
            f"appr-{task['opportunity_id']}",
            "APPROVED",
            "Human approval granted via review_queue",
            now_time
        )
    )
    print(f"[APPROVAL SUCCESS] Task {task_id} transitioned from WAITING_APPROVAL -> COMPLETED (Cost: ${total_cost:.6f}).")
    return True

def reject_task(task_id: str, reason: str, db: Optional[Database] = None, db_path: Optional[str] = None) -> bool:
    """Transitions a task from WAITING_APPROVAL to BLOCKED via execute_atomic_transition."""
    if not reason or not reason.strip():
        print("[ERROR] Rejection reason is required. Use --reason \"text\".")
        return False

    database = db or Database(db_path=db_path)
    task = database.get_task(task_id)
    if not task:
        print(f"[ERROR] Task '{task_id}' not found in database.")
        return False

    if task["state"] != "WAITING_APPROVAL":
        print(f"[ERROR] Task '{task_id}' is in state '{task['state']}', not 'WAITING_APPROVAL'. Cannot reject.")
        return False

    now_time = time.time()
    rejection_msg = f"HUMAN_APPROVAL_REJECTED: {reason.strip()}"
    task["state"] = "BLOCKED"
    task["error_reason"] = rejection_msg

    database.execute_atomic_transition(
        task,
        audit_event=("HUMAN_APPROVAL_REJECTED", {
            "task_id": task_id,
            "opportunity_id": task["opportunity_id"],
            "reason": reason.strip(),
            "timestamp": now_time
        }),
        approval_record=(
            f"appr-{task['opportunity_id']}",
            "REJECTED",
            reason.strip(),
            now_time
        )
    )
    print(f"[REJECTION SUCCESS] Task {task_id} transitioned from WAITING_APPROVAL -> BLOCKED (Reason: {reason.strip()}).")
    return True

def main():
    parser = argparse.ArgumentParser(description="AgencyOS Human Review Queue Manager")
    parser.add_argument("--db", default=None, help="Path to database file (default: agencyos.db)")
    subparsers = parser.add_subparsers(dest="command", required=True, help="Subcommands")

    # list
    subparsers.add_parser("list", help="List all pending LLM-judged tasks in WAITING_APPROVAL")

    # explain
    explain_parser = subparsers.add_parser("explain", help="Print a clean, untruncated explanation block for human or second-opinion review")
    explain_parser.add_argument("task_id", help="Task ID to explain")

    # approve
    approve_parser = subparsers.add_parser("approve", help="Approve a pending task and mark it COMPLETED")
    approve_parser.add_argument("task_id", help="Task ID to approve")

    # reject
    reject_parser = subparsers.add_parser("reject", help="Reject a pending task and mark it BLOCKED")
    reject_parser.add_argument("task_id", help="Task ID to reject")
    reject_parser.add_argument("--reason", required=True, help="Reason for rejection")

    args = parser.parse_args()

    if args.command == "list":
        list_queue(db_path=args.db)
    elif args.command == "explain":
        success = explain_task(args.task_id, db_path=args.db)
        if not success:
            sys.exit(1)
    elif args.command == "approve":
        success = approve_task(args.task_id, db_path=args.db)
        if not success:
            sys.exit(1)
    elif args.command == "reject":
        success = reject_task(args.task_id, args.reason, db_path=args.db)
        if not success:
            sys.exit(1)

if __name__ == "__main__":
    main()

