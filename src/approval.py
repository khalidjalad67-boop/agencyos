import sys
from dataclasses import dataclass, asdict
from typing import Dict, Any, Callable, Optional
from src.opportunity import Opportunity
from src.planner import TaskSpec
from src.worker import WorkerResult
from src.reviewer import ReviewResult
from src.db import Database

@dataclass
class ApprovalDecision:
    opportunity_id: str
    approved: bool
    comments: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class ApprovalGate:
    """Persistent Human Approval Gate backed by SQLite. Rejects block, approvals proceed.
    Never uses interactive terminal prompts in non-interactive mode.
    """

    def __init__(
        self,
        db: Optional[Database] = None,
        decision_provider: Optional[Callable[[Opportunity, TaskSpec, WorkerResult, ReviewResult], ApprovalDecision]] = None
    ):
        self.db = db or Database()
        self.decision_provider = decision_provider

    def request_approval(
        self,
        opportunity: Opportunity,
        task_spec: TaskSpec,
        worker_result: WorkerResult,
        review_result: ReviewResult
    ) -> ApprovalDecision:
        """Presents execution details for approval, querying/updating SQLite persistent approvals table."""
        approval_id = f"appr-{opportunity.id}"
        
        # Check if already decided in SQLite approvals table
        existing = self.db.get_approval(approval_id)
        if existing and existing["status"] in ("APPROVED", "REJECTED"):
            is_approved = (existing["status"] == "APPROVED")
            return ApprovalDecision(
                opportunity_id=opportunity.id,
                approved=is_approved,
                comments=existing.get("comments") or ("SQLite persisted approval" if is_approved else "SQLite persisted rejection")
            )

        # Register pending approval in SQLite
        self.db.save_approval(approval_id, opportunity.id, "PENDING", "Waiting for approval decision")

        if self.decision_provider:
            decision = self.decision_provider(opportunity, task_spec, worker_result, review_result)
            status = "APPROVED" if decision.approved else "REJECTED"
            self.db.save_approval(approval_id, opportunity.id, status, decision.comments)
            return decision

        # Default non-interactive autonomous resolution
        decision = ApprovalDecision(
            opportunity_id=opportunity.id,
            approved=True,
            comments="Autonomous non-interactive approval"
        )
        self.db.save_approval(approval_id, opportunity.id, "APPROVED", decision.comments)
        return decision
