import time
from typing import Dict, Any, Optional
from tools.review_queue import detect_hedge_language, detect_stub_placeholder

def determine_rejection_source(task: Dict[str, Any]) -> Optional[str]:
    """Determines rejection category from task state and error_reason."""
    state = task.get("state")
    if state == "COMPLETED":
        return None
    error_reason = str(task.get("error_reason") or "")
    if state == "QUALITY_REJECTED":
        if "TESTER_REJECTED" in error_reason:
            return "TESTER_REJECTED"
        return "QUALITY_REJECTED"
    if state == "BLOCKED":
        if error_reason.startswith("BUDGET_BLOCKED"):
            return "BUDGET_BLOCKED"
        return "HUMAN_APPROVAL_REJECTED"
    if state == "WORKER_FAILED":
        return "WORKER_FAILED"
    return None

def generate_retrospective_for_task(task_id: str, db: Any) -> Optional[Dict[str, Any]]:
    """Generates and persists a deterministic retrospective record for a given task_id."""
    task = db.get_task(task_id)
    if not task:
        return None
    state = task.get("state")
    if state not in ("COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED"):
        return None

    repo = task.get("repo", "unknown")
    worker_res = task.get("worker_result") or {}
    worker_output = worker_res.get("output", "") if isinstance(worker_res, dict) else ""
    review_res = task.get("review_result") or {}
    review_method = review_res.get("review_method") if isinstance(review_res, dict) else None

    cost = float(task.get("cost") if task.get("cost") is not None else db.get_task_cost(task_id))
    rejection_source = determine_rejection_source(task)
    hedge_flagged = bool(detect_hedge_language(worker_output))
    stub_flagged = bool(detect_stub_placeholder(worker_output))

    retro_data = {
        "task_id": task_id,
        "repo": repo,
        "outcome": state,
        "review_method": review_method,
        "cost": round(cost, 6),
        "rejection_source": rejection_source,
        "hedge_flagged": hedge_flagged,
        "stub_flagged": stub_flagged,
        "created_at": time.time()
    }
    db.save_retrospective(retro_data)
    return retro_data
