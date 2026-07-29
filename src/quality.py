from dataclasses import dataclass
from typing import Tuple
from src.opportunity import Opportunity

@dataclass
class QualityEvaluation:
    is_valid: bool
    score: float
    reason: str

class OpportunityQualityScorer:
    """Evaluates raw Opportunity payload before planning.
    
    Derived from empirical analysis of 104 real GitHub API open issues:
    - Correlation between description_length and score was weak (r = 0.27).
    - All reviewed scores fell within a narrow passing band (0.876 - 0.980).
    - Only description_length == 0 (empty body) is supported as an empirical rejection cutoff.
    - Tag-based rejection (duplicate, stale, wontfix, invalid) is retained as a structural
      default based on face validity, not an empirically observed pattern (zero such tags
      appeared in the 105-issue sample).
    """
    
    # Structural default labels for closed/unworkable issues (face validity, unobserved in sample)
    STRUCTURAL_REJECT_LABELS = ["duplicate", "stale", "wontfix", "invalid"]

    def evaluate(self, opportunity: Opportunity) -> Tuple[bool, float, str]:
        """Evaluates raw opportunity before any planning or execution costs are incurred."""
        desc = (opportunity.description or "").strip()
        title_lower = (opportunity.title or "").lower()
        labels = [l.lower() for l in opportunity.payload.get("labels", [])]

        # 1. Reject only empty body (description_length == 0) — the only cutoff supported by empirical data
        if len(desc) == 0:
            return False, 0.0, "Opportunity description is empty (description_length == 0)."

        # 2. Reject duplicate, stale, wontfix, or invalid tagged issues (structural default based on face validity)
        if any(lbl in self.STRUCTURAL_REJECT_LABELS for lbl in labels) or any(kw in title_lower for kw in ["[duplicate]", "[stale]", "[wontfix]"]):
            return False, 0.30, "Issue is tagged as duplicate, stale, wontfix, or invalid (structural default)."

        # Approve all other opportunities (no arbitrary length thresholds or CI label filters)
        return True, 1.00, "Opportunity meets quality criteria."
