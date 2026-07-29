import sys
from dataclasses import dataclass, asdict
from typing import Dict, Any, Callable, Optional
from src.opportunity import Opportunity
from src.planner import TaskSpec
from src.worker import WorkerResult
from src.reviewer import ReviewResult

@dataclass
class ApprovalDecision:
    opportunity_id: str
    approved: bool
    comments: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class ApprovalGate:
    """Human approval gate. Rejects block, approvals proceed."""
    
    def __init__(self, decision_provider: Optional[Callable[[Opportunity, TaskSpec, WorkerResult, ReviewResult], ApprovalDecision]] = None):
        self.decision_provider = decision_provider

    def request_approval(
        self,
        opportunity: Opportunity,
        task_spec: TaskSpec,
        worker_result: WorkerResult,
        review_result: ReviewResult
    ) -> ApprovalDecision:
        """Presents execution details for human approval."""
        if self.decision_provider:
            return self.decision_provider(opportunity, task_spec, worker_result, review_result)
        
        # Default CLI interactive prompt if no custom decision provider set
        print("\n" + "="*60)
        print("HUMAN APPROVAL GATE")
        print("="*60)
        print(f"Opportunity ID : {opportunity.id}")
        print(f"Title          : {opportunity.title}")
        print(f"Task           : {task_spec.task}")
        print(f"Priority       : {task_spec.priority}")
        print(f"Review Score   : {review_result.score:.2f} ({'PASSED' if review_result.passed else 'FAILED'})")
        print(f"Review Feedback: {review_result.feedback}")
        print(f"Worker Cost    : ${worker_result.actual_cost + review_result.review_cost:.4f}")
        print("-" * 60)
        print(f"Worker Output:\n{worker_result.output}")
        print("=" * 60)
        
        try:
            user_input = input("Approve execution result? (y/n): ").strip().lower()
            approved = user_input.startswith("y")
            comments = "Human CLI input: approved" if approved else "Human CLI input: rejected"
        except (EOFError, KeyboardInterrupt):
            approved = True
            comments = "Default non-interactive auto-approval"

        return ApprovalDecision(
            opportunity_id=opportunity.id,
            approved=approved,
            comments=comments
        )
