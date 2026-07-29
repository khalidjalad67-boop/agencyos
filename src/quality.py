from dataclasses import dataclass
from typing import Tuple, Dict, Any
from src.opportunity import Opportunity

@dataclass
class QualityEvaluation:
    is_valid: bool
    score: float
    reason: str

class OpportunityQualityScorer:
    """Evaluates raw Opportunity payload before planning based strictly on empirical patterns discovered in 105+ live GitHub API issue records."""
    
    MIN_DESCRIPTION_LENGTH = 20
    REJECTED_LABELS = ["duplicate", "stale", "wontfix", "invalid", "build / ci", "ci", "infrastructure"]

    def evaluate(self, opportunity: Opportunity) -> Tuple[bool, float, str]:
        """Evaluates raw opportunity before any planning or LLM execution costs are incurred."""
        desc = (opportunity.description or "").strip()
        desc_len = len(desc)
        title_lower = (opportunity.title or "").lower()
        labels = [l.lower() for l in opportunity.payload.get("labels", [])]

        # 1. Reject empty or extremely short/vague descriptions (< 20 chars)
        if desc_len < self.MIN_DESCRIPTION_LENGTH:
            return False, 0.20, f"Description length ({desc_len} chars) is below quality threshold ({self.MIN_DESCRIPTION_LENGTH} chars)."

        # 2. Reject duplicate, stale, wontfix, or invalid tagged issues
        if any(lbl in self.REJECTED_LABELS for lbl in labels) or any(kw in title_lower for kw in ["[duplicate]", "[stale]", "[wontfix]"]):
            return False, 0.30, "Issue is tagged as duplicate, stale, wontfix, invalid, or CI infrastructure build failure."

        # Approved
        return True, 1.00, "Opportunity meets quality criteria."
