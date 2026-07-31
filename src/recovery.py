from typing import List, Dict, Any
from src.db import Database
from src.idempotency import IdempotencyGuard

def run_startup_recovery(db: Database) -> Dict[str, Any]:
    """Startup recovery routine that runs once at application launch.
    Loads unfinished tasks, inspects persisted state and idempotency keys,
    reloads pending approvals, and cleanly recovers task state without executing business logic.
    """
    idempotency_guard = IdempotencyGuard(db)
    all_tasks = db.get_all_tasks()
    db.log_event("STARTUP_RECOVERY_STARTED", {"total_tasks": len(all_tasks)})

    recovered_tasks = 0
    reset_tasks = 0
    ignored_tasks = 0

    for task in all_tasks:
        task_id = task["task_id"]
        state = task["state"]

        if state in ("COMPLETED", "CANCELLED", "EXPIRED", "FAILED"):
            ignored_tasks += 1
            continue

        if state == "EXECUTING":
            worker_result = idempotency_guard.get_worker_result(task_id)
            review_result = idempotency_guard.get_review_result(task_id)

            if worker_result and review_result:
                # Worker and Reviewer completed before process crash
                task["worker_result"] = worker_result.to_dict()
                task["review_result"] = review_result.to_dict()
                task["state"] = "WAITING_APPROVAL"
                db.save_task(task)
                db.save_approval(f"appr-{task_id}", task["opportunity_id"], "PENDING", "Recovered pending approval after restart")
                db.log_event("STARTUP_RECOVERY_BOTH_CACHE_RECOVERED", {
                    "task_id": task_id,
                    "interrupted_phase": "AFTER_WORKER_AND_REVIEWER_COMPLETION",
                    "worker_key_found": True,
                    "reviewer_key_found": True,
                    "recovered_state": "WAITING_APPROVAL"
                })
                recovered_tasks += 1
            elif worker_result:
                # Worker completed before crash, reviewer did not finish
                task["worker_result"] = worker_result.to_dict()
                task["state"] = "REVIEWED"
                db.save_task(task)
                db.log_event("STARTUP_RECOVERY_WORKER_CACHE_RECOVERED", {
                    "task_id": task_id,
                    "interrupted_phase": "AFTER_WORKER_COMPLETION_BEFORE_REVIEWER",
                    "worker_key_found": True,
                    "reviewer_key_found": False,
                    "recovered_state": "REVIEWED"
                })
                recovered_tasks += 1
            else:
                # Worker did not finish before crash, reset state to READY to resume cleanly
                db.update_task_state(task_id, "READY", "Reset to READY by startup recovery routine")
                db.log_event("STARTUP_RECOVERY_PRE_WORKER_KILL_RESET", {
                    "task_id": task_id,
                    "interrupted_phase": "BEFORE_WORKER_COMPLETION",
                    "worker_key_found": False,
                    "reset_state": "READY"
                })
                reset_tasks += 1
        elif state == "WAITING_APPROVAL":
            # Ensure pending approval exists in DB
            db.save_approval(f"appr-{task_id}", task["opportunity_id"], "PENDING", "Reloaded pending approval after restart")
            recovered_tasks += 1

    summary = {
        "recovered_tasks": recovered_tasks,
        "reset_tasks": reset_tasks,
        "ignored_tasks": ignored_tasks,
        "pending_approvals": len(db.get_pending_approvals())
    }

    db.log_event("STARTUP_RECOVERY_COMPLETED", summary)
    return summary
