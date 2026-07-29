from dataclasses import dataclass, asdict
from typing import Dict, Any
from src.opportunity import Opportunity

@dataclass
class TaskSpec:
    opportunity_id: str
    task: str
    priority: str
    expected_output: str
    estimated_cost: float
    input_tokens: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class Planner:
    """Planner shapes a live opportunity into a structured task spec with dynamic token estimation."""
    
    def plan(self, opportunity: Opportunity) -> TaskSpec:
        labels = [str(l).lower() for l in opportunity.payload.get("labels", [])]
        title_lower = opportunity.title.lower()
        
        if any(kw in title_lower or kw in labels for kw in ["security", "fail", "bug", "error", "leak", "vulnerability"]):
            priority = "HIGH"
        elif any(kw in title_lower or kw in labels for kw in ["perf", "optimize", "slow", "add", "feature"]):
            priority = "MEDIUM"
        else:
            priority = "LOW"
            
        task_instruction = (
            f"Resolve issue #{opportunity.payload.get('issue_number', 'N/A')} ({opportunity.title}) "
            f"in repository {opportunity.payload.get('repo', 'unknown')}. "
            f"Context: {opportunity.description[:250]}"
        )
        
        expected_output = (
            f"Working code fix addressing '{opportunity.title}'. "
            f"Must include root cause analysis, modified code implementation, test verification, and documentation."
        )
        
        # Estimate input token count (approx 4 chars per token)
        prompt_text = task_instruction + expected_output
        input_tokens = max(50, len(prompt_text) // 4)
        
        # Price per 1k tokens (e.g. $0.00015 input, $0.0006 output for lightweight model)
        estimated_cost = round((input_tokens / 1000.0) * 0.00015 + (200 / 1000.0) * 0.0006, 6)
        
        return TaskSpec(
            opportunity_id=opportunity.id,
            task=task_instruction,
            priority=priority,
            expected_output=expected_output,
            estimated_cost=estimated_cost,
            input_tokens=input_tokens
        )
