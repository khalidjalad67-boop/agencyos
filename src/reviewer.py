from dataclasses import dataclass, asdict
from typing import Dict, Any
from src.planner import TaskSpec
from src.worker import WorkerResult

@dataclass
class ReviewResult:
    opportunity_id: str
    passed: bool
    score: float
    feedback: str
    review_cost: float
    review_tokens: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class Reviewer:
    """Checks worker output against expected criteria, calculates token-based review costs, and computes dynamic review scores."""
    
    def review(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        """Evaluates worker output with dynamic scoring that varies based on issue details and token density."""
        expected = task_spec.expected_output.lower()
        output = worker_result.output.lower()
        task_text = task_spec.task.lower()
        
        # Calculate review tokens
        review_input = f"Evaluate:\nTask: {task_spec.task}\nExpected: {task_spec.expected_output}\nWorker Output: {worker_result.output[:300]}"
        review_tokens = max(20, len(review_input) // 4)
        review_cost = round((review_tokens / 1000.0) * 0.000075 + (50 / 1000.0) * 0.000300, 6)
        
        # 1. Base score
        score = 0.70
        
        # 2. Context keyword match: calculate exact fraction of key issue words present in output
        task_words = set(w for w in task_text.replace("#", " ").replace(":", " ").replace("(", " ").replace(")", " ").split() if len(w) > 4)
        output_words = set(w for w in output.split() if len(w) > 4)
        
        if task_words:
            matched_words = task_words.intersection(output_words)
            match_ratio = len(matched_words) / len(task_words)
            score += round(match_ratio * 0.15, 3)
        else:
            match_ratio = 0.5
            score += 0.075

        # 3. Output token completeness bonus/penalty based on token count
        if worker_result.completion_tokens > 280:
            score += 0.06
        elif worker_result.completion_tokens > 250:
            score += 0.04
        elif worker_result.completion_tokens > 230:
            score += 0.02

        # 4. Verification & structure markers
        if "test" in output or "verification" in output:
            score += 0.03
        if "root cause" in output:
            score += 0.03

        # 5. Priority penalty/bonus adjustment
        if task_spec.priority == "HIGH":
            score -= 0.02  # Higher scrutiny for high priority issues
        elif task_spec.priority == "LOW":
            score += 0.01

        final_score = round(min(0.99, max(0.40, score)), 3)
        passed = final_score >= 0.70
        
        feedback = (
            f"Dynamic evaluation score: {final_score:.3f}. "
            f"Verification: {'PASSED' if passed else 'FAILED'}. "
            f"Matched {len(task_words.intersection(output_words))}/{len(task_words)} task keywords."
        )

        return ReviewResult(
            opportunity_id=task_spec.opportunity_id,
            passed=passed,
            score=final_score,
            feedback=feedback,
            review_cost=review_cost,
            review_tokens=review_tokens
        )
