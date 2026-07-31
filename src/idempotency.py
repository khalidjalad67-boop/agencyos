from typing import Optional, Dict, Any
from src.db import Database
from src.worker import WorkerResult
from src.reviewer import ReviewResult

class IdempotencyGuard:
    """Idempotency guard scoped ONLY to Worker and Reviewer LLM calls.
    Prevents duplicate network calls and duplicate billing across restarts.
    """

    def __init__(self, db: Database):
        self.db = db

    def get_worker_result(self, task_id: str) -> Optional[WorkerResult]:
        key = f"WORKER:{task_id}:v1"
        data = self.db.get_idempotency_key(key)
        if data:
            return WorkerResult(
                opportunity_id=data["opportunity_id"],
                output=data["output"],
                execution_time_sec=data["execution_time_sec"],
                actual_cost=data.get("actual_cost", 0.0),
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                model=data.get("model", "gemini-1.5-flash"),
                http_status=data.get("http_status", 200),
                status=data.get("status", "SUCCESS"),
                error_reason=data.get("error_reason", "")
            )
        return None

    def save_worker_result(self, task_id: str, worker_result: WorkerResult) -> None:
        key = f"WORKER:{task_id}:v1"
        self.db.save_idempotency_key(key, worker_result.to_dict())

    def get_review_result(self, task_id: str) -> Optional[ReviewResult]:
        key = f"REVIEW:{task_id}:v1"
        data = self.db.get_idempotency_key(key)
        if data:
            return ReviewResult(
                opportunity_id=data["opportunity_id"],
                passed=data["passed"],
                score=data["score"],
                feedback=data["feedback"],
                review_cost=data.get("review_cost", 0.0),
                review_tokens=data.get("review_tokens", 0)
            )
        return None

    def save_review_result(self, task_id: str, review_result: ReviewResult) -> None:
        key = f"REVIEW:{task_id}:v1"
        self.db.save_idempotency_key(key, review_result.to_dict())
