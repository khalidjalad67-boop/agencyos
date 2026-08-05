import time
from typing import Dict, Any, List, Optional
from src.db import Database
from src.opportunity import Opportunity
from src.planner import Planner, TaskSpec
from src.quality import OpportunityQualityScorer
from src.budget_guard import BudgetGuard
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.logger import AuditLogger
from src.idempotency import IdempotencyGuard
from src.watchdog import OperationalWatchdog

class AutonomousEngine:
    """Restart-safe autonomous engine executing the task state machine.
    Enforces quality checks before planning, budget limits before execution,
    Worker/Reviewer idempotency, persistent approval queue, and watchdog monitoring.
    """

    def __init__(
        self,
        db: Database,
        quality_scorer: Optional[OpportunityQualityScorer] = None,
        planner: Optional[Planner] = None,
        budget_guard: Optional[BudgetGuard] = None,
        worker: Optional[Worker] = None,
        reviewer: Optional[Reviewer] = None,
        approval_gate: Optional[ApprovalGate] = None,
        watchdog: Optional[OperationalWatchdog] = None,
        logger: Optional[AuditLogger] = None,
        log_filepath: str = "audit_log.jsonl"
    ):
        self.db = db
        self.logger = logger or AuditLogger(log_filepath=log_filepath, db=self.db)
        self.quality_scorer = quality_scorer or OpportunityQualityScorer()
        self.planner = planner or Planner()
        self.budget_guard = budget_guard or BudgetGuard(db=self.db, log_filepath=log_filepath)
        self.worker = worker or Worker()
        self.reviewer = reviewer or Reviewer()
        self.approval_gate = approval_gate or ApprovalGate(db=self.db)
        self.watchdog = watchdog or OperationalWatchdog(db=self.db)
        self.idempotency = IdempotencyGuard(db=self.db)

    def process_task(self, task_id: str) -> Dict[str, Any]:
        """Processes a single task through the lifecycle state machine."""
        task = self.db.get_task(task_id)
        if not task:
            return {"status": "NOT_FOUND", "task_id": task_id}

        state = task["state"]
        source = task.get("source", "unknown")
        repo = task.get("repo", "unknown")

        # Terminal state guard: do not process tasks already in terminal states
        if state in ("COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED"):
            return {"status": state, "reason": "Task is in terminal state", "task_id": task_id}

        if not self.watchdog.is_source_enabled(source):
            self.logger.log_event("WATCHDOG_SKIPPED_TASK", {
                "task_id": task_id,
                "source": source,
                "reason": "Source temporarily disabled by watchdog"
            })
            return {"status": "SKIPPED", "reason": "Source disabled", "task_id": task_id}

        # Construct Opportunity object
        opp = Opportunity(
            id=task["opportunity_id"],
            title=task["title"],
            description=task["description"],
            source=source,
            payload=task["payload"]
        )

        # ---------------------------------------------------------------------
        # 1. QUALITY CHECK (Strictly BEFORE Planning! DISCOVERED -> PLANNED)
        # ---------------------------------------------------------------------
        if state == "DISCOVERED":
            is_valid, qual_score, qual_reason = self.quality_scorer.evaluate(opp)
            if not is_valid:
                error_msg = f"QUALITY_REJECTED: {qual_reason}"
                task["state"] = "QUALITY_REJECTED"
                task["error_reason"] = error_msg
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("OPPORTUNITY_REJECTED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "score": qual_score,
                        "reason": qual_reason,
                        "repo": repo
                    })
                )
                return {"status": "QUALITY_REJECTED", "reason": qual_reason, "task_id": task_id}

            # Passed quality check -> transition to PLANNED
            task_spec = self.planner.plan(opp)
            task["task_spec"] = task_spec.to_dict()
            task["state"] = "PLANNED"
            self.db.execute_atomic_transition(
                task,
                audit_event=("TASK_PLANNED", {
                    "task_id": task_id,
                    "opportunity_id": opp.id,
                    "task_spec": task_spec.to_dict(),
                    "repo": repo
                })
            )
            state = "PLANNED"

        # ---------------------------------------------------------------------
        # 2. BUDGET CHECK (PLANNED -> READY)
        # ---------------------------------------------------------------------
        if state == "PLANNED":
            task_spec_data = task["task_spec"]
            task_spec = TaskSpec(**task_spec_data)
            
            budget_passed, budget_reason = self.budget_guard.check_budget(task_spec)
            if not budget_passed:
                error_msg = f"BUDGET_BLOCKED: {budget_reason}"
                task["state"] = "BLOCKED"
                task["error_reason"] = error_msg
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("BUDGET_BLOCKED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "reason": budget_reason,
                        "estimated_cost": task_spec.estimated_cost,
                        "repo": repo
                    })
                )
                return {"status": "BUDGET_BLOCKED", "reason": budget_reason, "task_id": task_id}

            task["state"] = "READY"
            self.db.execute_atomic_transition(
                task,
                audit_event=("TASK_READY", {
                    "task_id": task_id,
                    "opportunity_id": opp.id,
                    "repo": repo
                })
            )
            state = "READY"

        # ---------------------------------------------------------------------
        # 3. WORKER EXECUTION with Idempotency Guard (READY / EXECUTING -> REVIEW)
        # ---------------------------------------------------------------------
        if state in ("READY", "EXECUTING") or (state == "REVIEW" and task.get("review_result") is None):
            task_spec_data = task["task_spec"]
            task_spec = TaskSpec(**task_spec_data)

            if state in ("READY", "EXECUTING"):
                task["state"] = "EXECUTING"
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("TASK_EXECUTING", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "repo": repo
                    })
                )

            # Check Idempotency for Worker
            cached_worker_res = self.idempotency.get_worker_result(task_id)
            if cached_worker_res:
                worker_result = cached_worker_res
                task["worker_result"] = worker_result.to_dict()  # mirror else-branch; prevents worker_result_json=NULL on recovered tasks
                self.logger.log_event("WORKER_IDEMPOTENCY_HIT", {
                    "task_id": task_id,
                    "opportunity_id": opp.id
                })
            else:
                worker_result = self.worker.execute(task_spec)
                if worker_result.status == "FAILED":
                    self.watchdog.record_failure(source, worker_result.error_reason)
                    task["state"] = "WORKER_FAILED"
                    task["error_reason"] = worker_result.error_reason
                    self.db.execute_atomic_transition(
                        task,
                        audit_event=("WORKER_FAILED", {
                            "task_id": task_id,
                            "opportunity_id": opp.id,
                            "error": worker_result.error_reason
                        })
                    )
                    return {"status": "WORKER_FAILED", "reason": worker_result.error_reason, "task_id": task_id}

                self.watchdog.record_success(source)
                self.idempotency.save_worker_result(task_id, worker_result)
                task["worker_result"] = worker_result.to_dict()
                task["state"] = "EXECUTING"
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("WORKER_EXECUTED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "worker_result": worker_result.to_dict(),
                        "repo": repo
                    })
                )

            # -----------------------------------------------------------------
            # 4. REVIEWER EVALUATION with Idempotency Guard
            # -----------------------------------------------------------------
            cached_review_res = self.idempotency.get_review_result(task_id)
            if cached_review_res:
                review_result = cached_review_res
                task["review_result"] = review_result.to_dict()
                task["state"] = "REVIEW"
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("REVIEWER_IDEMPOTENCY_HIT", {
                        "task_id": task_id,
                        "opportunity_id": opp.id
                    })
                )
            else:
                review_result = self.reviewer.review(task_spec, worker_result)
                self.idempotency.save_review_result(task_id, review_result)

                # Record spend and update review state atomically
                task_cost = round(worker_result.actual_cost + review_result.review_cost, 6)
                today_str = self.budget_guard.today_date_str
                task["review_result"] = review_result.to_dict()
                task["state"] = "REVIEW"
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("REVIEW_COMPLETED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "review_result": review_result.to_dict(),
                        "repo": repo
                    }),
                    spend_record=(task_cost, today_str, "Task spend")
                )
                self.budget_guard.cumulative_today_spend = self.db.get_today_spend(today_str)
            state = "REVIEW"

        # ---------------------------------------------------------------------
        # 5. APPROVAL GATE & COMPLETION (REVIEW / WAITING_APPROVAL -> COMPLETED)
        # ---------------------------------------------------------------------
        if state in ("REVIEW", "WAITING_APPROVAL"):
            task_spec = TaskSpec(**task["task_spec"])
            worker_result = WorkerResult(**task["worker_result"])
            review_result = ReviewResult(**task["review_result"])

            task["state"] = "WAITING_APPROVAL"
            self.db.execute_atomic_transition(
                task,
                audit_event=("TASK_WAITING_APPROVAL", {
                    "task_id": task_id,
                    "opportunity_id": opp.id
                })
            )

            approval_decision = self.approval_gate.request_approval(opp, task_spec, worker_result, review_result)
            now_time = time.time()

            if approval_decision.approved and review_result.passed:
                task["state"] = "COMPLETED"
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("TASK_COMPLETED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "cost": worker_result.actual_cost + review_result.review_cost
                    }),
                    approval_record=(
                        f"appr-{opp.id}",
                        "APPROVED",
                        approval_decision.comments,
                        now_time
                    )
                )
                return {"status": "COMPLETED", "task_id": task_id}
            else:
                reason = approval_decision.comments if review_result.passed else review_result.feedback
                task["state"] = "BLOCKED"
                task["error_reason"] = reason
                self.db.execute_atomic_transition(
                    task,
                    audit_event=("TASK_BLOCKED", {
                        "task_id": task_id,
                        "opportunity_id": opp.id,
                        "reason": reason
                    }),
                    approval_record=(
                        f"appr-{opp.id}",
                        "REJECTED",
                        reason,
                        now_time
                    )
                )
                return {"status": "BLOCKED", "reason": reason, "task_id": task_id}

        return {"status": task["state"], "task_id": task_id}
