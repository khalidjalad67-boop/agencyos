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
import sqlite3
import re
import argparse
from typing import List, Dict, Any, Optional, Set, Tuple

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

STUB_ACTION_KEYWORDS: Set[str] = {
    "should", "would", "assigns", "assign", "handle", "handles",
    "implement", "implements", "implementation", "todo", "fixme",
    "logic", "calculate", "update", "set", "process", "add", "populate",
}

def detect_hedge_language(text: str) -> List[str]:
    """Scans text for known hedge / placeholder phrases (case-insensitive)."""
    if not text:
        return []
    text_lower = text.lower()
    return [phrase for phrase in HEDGE_PHRASES if phrase in text_lower]

def detect_stub_placeholder(text: str) -> List[str]:
    """Scans Python code blocks for bare 'pass' statements where implementation logic is described in comments instead of written."""
    if not text:
        return []

    matches: List[str] = []

    # Extract code blocks
    pattern = r"```(?:python|py)?\s*\n(.*?)```"
    blocks = re.findall(pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if not blocks:
        blocks = [text]

    for block in blocks:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Match bare pass with optional inline comment
            if re.match(r"^pass(?:\s+#.*)?$", stripped):
                inline_match = None
                # Check (b) inline comment starting with implementation/todo/etc.
                if "#" in stripped:
                    comment_part = stripped[stripped.index("#"):]
                    if re.search(r"#\s*(?:\(?[iI]mplementation|TODO|todo|implement|assigns|should|would|handle|logic)\b", comment_part, re.I):
                        inline_match = f"bare 'pass' with description comment: '{comment_part.strip()}'"

                # Check (a) immediately preceding comment lines
                prev_comments = []
                j = i - 1
                while j >= 0 and lines[j].strip().startswith("#"):
                    prev_comments.insert(0, lines[j].strip())
                    j -= 1

                prec_match = None
                if prev_comments:
                    combined_comment = " ".join(prev_comments)
                    words = set(re.findall(r"[a-zA-Z]+", combined_comment.lower()))
                    if words & STUB_ACTION_KEYWORDS:
                        snippet = prev_comments[0] if len(prev_comments) == 1 else f"{prev_comments[0]} ... {prev_comments[-1]}"
                        prec_match = f"bare 'pass' with description comment: '{snippet}'"

                if inline_match:
                    matches.append(inline_match)
                elif prec_match:
                    matches.append(prec_match)

    return matches

def get_trusted_repos(config_path: str = "config/settings.yaml") -> List[str]:
    """Reads trusted repos from config/settings.yaml."""
    if os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                appr = cfg.get("approval", {})
                return list(appr.get("trusted_repos") or [])
        except Exception:
            pass
    return []

def list_queue(db: Optional[Database] = None, db_path: Optional[str] = None, config_path: str = "config/settings.yaml") -> List[Dict[str, Any]]:
    """Queries DB for all tasks in WAITING_APPROVAL state with review_method == 'llm_judged'."""
    database = db or Database(db_path=db_path)
    waiting_tasks = database.get_tasks_by_state("WAITING_APPROVAL")
    
    llm_judged_tasks = []
    for task in waiting_tasks:
        rev_res = task.get("review_result") or {}
        if rev_res.get("review_method") == "llm_judged":
            llm_judged_tasks.append(task)
            
    trusted = get_trusted_repos(config_path=config_path)
    trusted_str = ", ".join(trusted) if trusted else "none"

    # Count Tester vs Reviewer rejections
    tester_rejections = 0
    reviewer_rejections = 0
    try:
        conn = sqlite3.connect(database.db_path)
        cur = conn.cursor()
        t_row = cur.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='TESTER_REJECTED'").fetchone()
        tester_rejections = t_row[0] if t_row else 0
        r_row = cur.execute("SELECT COUNT(*) FROM tasks WHERE state='BLOCKED'").fetchone()
        reviewer_rejections = r_row[0] if r_row else 0
        conn.close()
    except Exception:
        pass

    resolved_path = database.db_path
    print(f"=== AgencyOS Human Review Queue ({len(llm_judged_tasks)} pending) ===")
    print(f"Trusted repos (auto-approve): {trusted_str}")
    print(f"Rejections: Tester (TESTER_REJECTED): {tester_rejections} | Reviewer/Approval (BLOCKED): {reviewer_rejections}")
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
        stub_placeholders = detect_stub_placeholder(worker_output)

        print(f"\n[{idx}] Task ID : {task_id}")
        print(f"    Repo    : {repo}")
        print(f"    Title   : {title}")
        print(f"    Score   : {score}")
        print(f"    Feedback: {feedback_trunc}")
        if hedge_phrases:
            formatted_phrases = ", ".join(f"'{p}'" for p in hedge_phrases)
            print(f"    [WARNING] HEDGE LANGUAGE DETECTED: {formatted_phrases}")
        if stub_placeholders:
            formatted_stubs = "; ".join(stub_placeholders)
            print(f"    [WARNING] STUB PLACEHOLDER DETECTED: {formatted_stubs}")
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
    stub_placeholders = detect_stub_placeholder(worker_output)

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

    print("\n--- STUB PLACEHOLDER DETECTION ---")
    if stub_placeholders:
        formatted_stubs = "; ".join(stub_placeholders)
        print(f"[WARNING] Stub placeholder detected ({len(stub_placeholders)} matches): {formatted_stubs}")
        print("Note: The proposal contains bare 'pass' statements where implementation logic was described in comments instead of written.")
    else:
        print("None detected (No descriptive 'pass' placeholders found in worker output).")

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

def kpis_report(db: Optional[Database] = None, db_path: Optional[str] = None, config_path: str = "config/settings.yaml") -> Dict[str, Any]:
    """Generates and prints a comprehensive plain-text KPIs report covering lifecycle metrics,
    per-repository breakdown, rejection/approval sources, and detector signal quality.
    """
    database = db or Database(db_path=db_path)
    trusted_repos_set = set(get_trusted_repos(config_path=config_path))
    
    # 1. OVERALL LIFECYCLE & TELEMETRY
    telemetry = database.get_telemetry_metrics()
    total_tasks = telemetry["total_tasks"]
    completed_cnt = telemetry["successful_executions"]
    worker_failed_cnt = telemetry["worker_failed_executions"]
    quality_rejected_cnt = telemetry["quality_rejected_executions"]
    budget_blocked_cnt = telemetry["budget_blocked_executions"]
    approval_rejected_cnt = telemetry["approval_rejected_executions"]
    
    # State counts from tasks table for full visibility
    state_counts: Dict[str, int] = {}
    with database._connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT state, COUNT(*) as cnt FROM tasks GROUP BY state ORDER BY cnt DESC")
        for r in cur.fetchall():
            state_counts[r["state"]] = r["cnt"]

    success_rate = telemetry["success_rate"]
    approval_rate = telemetry["approval_rate"]
    total_cost = telemetry["total_cost"]
    avg_cost = telemetry["average_cost_per_task"]

    # 2. PER-REPO BREAKDOWN
    repo_stats: List[Dict[str, Any]] = []
    with database._connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                COALESCE(repo, 'unknown') as repo_name,
                COUNT(*) as total_cnt,
                SUM(CASE WHEN state = 'COMPLETED' THEN 1 ELSE 0 END) as completed_cnt,
                SUM(CASE WHEN state = 'BLOCKED' THEN 1 ELSE 0 END) as blocked_cnt,
                SUM(CASE WHEN state = 'QUALITY_REJECTED' THEN 1 ELSE 0 END) as quality_rejected_cnt,
                SUM(CASE WHEN state = 'WORKER_FAILED' THEN 1 ELSE 0 END) as worker_failed_cnt
            FROM tasks
            GROUP BY repo_name
            ORDER BY total_cnt DESC, repo_name ASC
        """)
        for r in cur.fetchall():
            r_name = r["repo_name"]
            r_tot = r["total_cnt"]
            r_comp = r["completed_cnt"]
            r_blk = r["blocked_cnt"]
            r_decided = r_comp + r_blk
            r_appr_rate = round(r_comp / r_decided, 4) if r_decided > 0 else (1.0 if r_comp > 0 else 0.0)
            is_trusted = r_name in trusted_repos_set
            repo_stats.append({
                "repo": r_name,
                "total": r_tot,
                "completed": r_comp,
                "blocked": r_blk,
                "quality_rejected": r["quality_rejected_cnt"],
                "worker_failed": r["worker_failed_cnt"],
                "approval_rate": r_appr_rate,
                "trusted": is_trusted
            })

    # 3. REJECTION & APPROVAL PIPELINE SOURCES
    tester_rejected_cnt = 0
    human_rejected_cnt = 0
    trusted_auto_approved_cnt = 0
    heuristic_auto_approved_cnt = 0
    human_approved_cnt = 0
    with database._connection() as conn:
        cur = conn.cursor()
        t_row = cur.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='TESTER_REJECTED'").fetchone()
        tester_rejected_cnt = t_row[0] if t_row else 0
        
        hr_row = cur.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='HUMAN_APPROVAL_REJECTED'").fetchone()
        human_rejected_cnt = hr_row[0] if hr_row else 0

        # Approvals table categorization
        tr_row = cur.execute("SELECT COUNT(*) FROM approvals WHERE status='APPROVED' AND comments LIKE '%trusted repository%'").fetchone()
        trusted_auto_approved_cnt = tr_row[0] if tr_row else 0

        hf_row = cur.execute("SELECT COUNT(*) FROM approvals WHERE status='APPROVED' AND comments LIKE '%Autonomous non-interactive approval%'").fetchone()
        heuristic_auto_approved_cnt = hf_row[0] if hf_row else 0

        ha_row = cur.execute("SELECT COUNT(*) FROM approvals WHERE status='APPROVED' AND (comments LIKE '%review_queue%' OR comments LIKE '%Human approval%')").fetchone()
        human_approved_cnt = ha_row[0] if ha_row else 0

    # 4. DETECTOR SIGNAL CHECK
    all_tasks = database.get_all_tasks()
    hedge_flagged: List[Tuple[Any, bool, bool]] = []
    stub_flagged: List[Tuple[Any, bool, bool]] = []
    any_flagged: List[Tuple[Any, bool, bool]] = []
    clean_tasks: List[Tuple[Any, bool, bool]] = []

    for t in all_tasks:
        wres = t.get("worker_result") or {}
        output = wres.get("output", "") if isinstance(wres, dict) else ""
        h_matches = detect_hedge_language(output)
        s_matches = detect_stub_placeholder(output)
        
        has_h = len(h_matches) > 0
        has_s = len(s_matches) > 0
        state = t.get("state")
        is_approved = (state == "COMPLETED")
        is_rejected = (state in ("BLOCKED", "QUALITY_REJECTED"))

        if has_h:
            hedge_flagged.append((t, is_approved, is_rejected))
        if has_s:
            stub_flagged.append((t, is_approved, is_rejected))
        if has_h or has_s:
            any_flagged.append((t, is_approved, is_rejected))
        else:
            clean_tasks.append((t, is_approved, is_rejected))

    def _calc_stats(items: List[Tuple[Any, bool, bool]]) -> Tuple[int, int, int, float, float]:
        tot = len(items)
        appr = sum(1 for _, a, _ in items if a)
        rej = sum(1 for _, _, r in items if r)
        decided = appr + rej
        rej_rate = round(rej / decided, 4) if decided > 0 else 0.0
        appr_rate = round(appr / decided, 4) if decided > 0 else 0.0
        return tot, appr, rej, rej_rate, appr_rate

    h_tot, h_appr, h_rej, h_rej_rate, _ = _calc_stats(hedge_flagged)
    s_tot, s_appr, s_rej, s_rej_rate, _ = _calc_stats(stub_flagged)
    any_tot, any_appr, any_rej, any_rej_rate, _ = _calc_stats(any_flagged)
    c_tot, c_appr, c_rej, _, c_appr_rate = _calc_stats(clean_tasks)

    # PRINT REPORT
    print("=" * 70)
    print("AGENCYOS KEY PERFORMANCE INDICATORS (KPIs) REPORT")
    print("=" * 70)

    print("\n--- 1. OVERALL LIFECYCLE & TELEMETRY ---")
    print(f"Total Tasks            : {total_tasks}")
    print("State Breakdown        :")
    for st in ["COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED", "WAITING_APPROVAL"]:
        cnt = state_counts.get(st, 0)
        print(f"  - {st:<20}: {cnt}")
    for st, cnt in state_counts.items():
        if st not in ["COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED", "WAITING_APPROVAL"]:
            print(f"  - {st:<20}: {cnt}")

    print(f"Success Rate           : {success_rate * 100:.1f}% ({completed_cnt} / {total_tasks})")
    approval_total = completed_cnt + approval_rejected_cnt
    print(f"Approval Rate          : {approval_rate * 100:.1f}% ({completed_cnt} / {approval_total if approval_total > 0 else total_tasks})")
    print(f"Total Spend            : ${total_cost:.6f}")
    print(f"Average Cost / Task    : ${avg_cost:.6f}")

    print("\n--- 2. PER-REPOSITORY BREAKDOWN ---")
    header_fmt = "{:<30} {:>7} {:>10} {:>8} {:>12} {:>14}"
    print(header_fmt.format("Repository", "Tasks", "Completed", "Blocked", "Appr. Rate", "Trust Status"))
    print("-" * 86)
    for r in repo_stats:
        trust_label = "[TRUSTED]" if r["trusted"] else "[UNTRUSTED]"
        rate_label = f"{r['approval_rate'] * 100:.1f}%" if (r["completed"] + r["blocked"]) > 0 else "N/A"
        print(header_fmt.format(
            r["repo"][:30],
            r["total"],
            r["completed"],
            r["blocked"],
            rate_label,
            trust_label
        ))

    print("\n--- 3. REJECTION & APPROVAL PIPELINE SOURCES ---")
    print(f"Tester Rejections (TESTER_REJECTED)               : {tester_rejected_cnt}")
    print(f"Human Approval Rejections (HUMAN_APPROVAL_REJECTED): {human_rejected_cnt}")
    print(f"Trusted Repo Auto-Approvals (trusted_repos)       : {trusted_auto_approved_cnt}")
    print(f"Heuristic Fallback Auto-Approvals                 : {heuristic_auto_approved_cnt}")
    print(f"Human Granted Approvals (via review_queue)        : {human_approved_cnt}")

    print("\n--- 4. DETECTOR SIGNAL QUALITY CHECK ---")
    h_rate_str = f"{h_rej_rate * 100:.1f}%" if (h_appr + h_rej) > 0 else "N/A"
    s_rate_str = f"{s_rej_rate * 100:.1f}%" if (s_appr + s_rej) > 0 else "N/A"
    any_rate_str = f"{any_rej_rate * 100:.1f}%" if (any_appr + any_rej) > 0 else "N/A"
    c_rate_str = f"{c_appr_rate * 100:.1f}%" if (c_appr + c_rej) > 0 else "N/A"

    print(f"Hedge Language Detected       : {h_tot} tasks ({h_appr} approved, {h_rej} rejected) -> Rejection Rate: {h_rate_str}")
    print(f"Stub Placeholders Detected    : {s_tot} tasks ({s_appr} approved, {s_rej} rejected) -> Rejection Rate: {s_rate_str}")
    print(f"Combined Any Warning Detected : {any_tot} tasks ({any_appr} approved, {any_rej} rejected) -> Rejection Rate: {any_rate_str}")
    print(f"Clean Tasks (No Warnings)     : {c_tot} tasks ({c_appr} approved, {c_rej} rejected) -> Approval Rate: {c_rate_str}")
    print("=" * 70)

    return {
        "overall": telemetry,
        "per_repo": repo_stats,
        "pipeline_sources": {
            "tester_rejections": tester_rejected_cnt,
            "human_rejections": human_rejected_cnt,
            "trusted_auto_approvals": trusted_auto_approved_cnt,
            "heuristic_auto_approvals": heuristic_auto_approved_cnt,
            "human_approvals": human_approved_cnt
        },
        "detector_signals": {
            "hedge": {"total": h_tot, "approved": h_appr, "rejected": h_rej, "rejection_rate": h_rej_rate},
            "stub": {"total": s_tot, "approved": s_appr, "rejected": s_rej, "rejection_rate": s_rej_rate},
            "combined": {"total": any_tot, "approved": any_appr, "rejected": any_rej, "rejection_rate": any_rej_rate},
            "clean": {"total": c_tot, "approved": c_appr, "rejected": c_rej, "approval_rate": c_appr_rate}
        }
    }

def main():
    parser = argparse.ArgumentParser(description="AgencyOS Human Review Queue Manager")
    parser.add_argument("--db", default=None, help="Path to database file (default: agencyos.db)")
    parser.add_argument("--config", default="config/settings.yaml", help="Path to settings.yaml (default: config/settings.yaml)")
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

    # kpis
    subparsers.add_parser("kpis", help="Print comprehensive KPIs report (lifecycle, per-repo, rejection sources, detector signal check)")

    args = parser.parse_args()

    if args.command == "list":
        list_queue(db_path=args.db, config_path=args.config)
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
    elif args.command == "kpis":
        kpis_report(db_path=args.db, config_path=args.config)

if __name__ == "__main__":
    main()

