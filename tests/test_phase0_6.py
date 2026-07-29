import os
import unittest
from src.opportunity import Opportunity
from src.quality import OpportunityQualityScorer, QualityEvaluation
from main import run_phase0_loop

TEST_LOG_FILE = "test_phase0_6_audit_log.jsonl"
TEST_CONFIG_FILE = "config/test_settings.yaml"

class TestPhase06DataDrivenQuality(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)
        self.scorer = OpportunityQualityScorer()

    def tearDown(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)

    def test_quality_scorer_evaluates_raw_opportunity(self):
        opp = Opportunity(
            id="opp-raw",
            title="Valid Issue Title",
            description="Detailed problem context covering reproduction steps and expected behavior.",
            source="github_issues:pallets/flask",
            payload={"repo": "pallets/flask", "labels": ["bug"]}
        )
        passed, score, reason = self.scorer.evaluate(opp)
        self.assertTrue(passed)
        self.assertEqual(score, 1.00)
        self.assertIn("meets quality criteria", reason)

    def test_reject_empty_description(self):
        empty_opp = Opportunity(
            id="opp-empty",
            title="Vague Issue",
            description="Short",
            source="github_issues:fastapi/fastapi",
            payload={"repo": "fastapi/fastapi", "labels": []}
        )
        passed, score, reason = self.scorer.evaluate(empty_opp)
        self.assertFalse(passed)
        self.assertLess(score, 0.50)
        self.assertIn("below quality threshold", reason)

    def test_reject_duplicate_stale_issues(self):
        duplicate_opp = Opportunity(
            id="opp-dup",
            title="[DUPLICATE] Stale report #102",
            description="Detailed context text here for duplicate issue.",
            source="github_issues:psf/requests",
            payload={"repo": "psf/requests", "labels": ["duplicate"]}
        )
        passed, score, reason = self.scorer.evaluate(duplicate_opp)
        self.assertFalse(passed)
        self.assertLess(score, 0.50)
        self.assertIn("duplicate or stale", reason)

    def test_data_driven_pipeline_telemetry(self):
        report = run_phase0_loop(num_opportunities=10, auto_approve=True, log_file=TEST_LOG_FILE, quality_scorer=self.scorer)
        telemetry = report["telemetry"]
        self.assertEqual(telemetry["total_tasks"], 10)
        self.assertGreater(telemetry["quality_rejected_executions"], 0)
        self.assertIn("QUALITY_REJECTED", telemetry["failure_categories"])

if __name__ == "__main__":
    unittest.main()
