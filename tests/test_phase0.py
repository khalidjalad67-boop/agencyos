import os
import unittest
from src.opportunity import OpportunityFetcher, Opportunity
from src.planner import Planner, TaskSpec
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.logger import AuditLogger
from main import run_phase0_loop

TEST_LOG_FILE = "test_audit_log.jsonl"

class TestPhase0(unittest.TestCase):
    
    def setUp(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)

    def tearDown(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)

    def test_live_opportunity_fetcher(self):
        fetcher = OpportunityFetcher()
        opps = fetcher.fetch_opportunities(limit=5)
        self.assertEqual(len(opps), 5)
        self.assertIn("github_issues", opps[0].source)
        self.assertIsNotNone(opps[0].payload.get("issue_number"))

    def test_planner_shaping(self):
        planner = Planner()
        opp = Opportunity(
            id="4878017272",
            title="IPv6 addresses parsed incorrectly because of .partition(':')",
            description="Detailed description of IPv6 parsing defect",
            source="github_issues:pallets/flask",
            payload={"labels": ["bug"], "repo": "pallets/flask", "issue_number": 6093}
        )
        task_spec = planner.plan(opp)
        self.assertIsInstance(task_spec, TaskSpec)
        self.assertEqual(task_spec.opportunity_id, "4878017272")
        self.assertEqual(task_spec.priority, "HIGH")
        self.assertLess(task_spec.estimated_cost, 0.25)
        self.assertGreater(task_spec.input_tokens, 0)

    def test_worker_execution_dynamic_metrics(self):
        worker = Worker()
        task_spec = TaskSpec(
            opportunity_id="4878017272",
            task="Resolve issue #6093 (IPv6 addresses parsed incorrectly)",
            priority="HIGH",
            expected_output="Working code fix addressing IPv6 parsing",
            estimated_cost=0.00014,
            input_tokens=100
        )
        result = worker.execute(task_spec)
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.opportunity_id, "4878017272")
        self.assertIn("RESOLUTION PLAN", result.output)
        self.assertGreaterEqual(result.execution_time_sec, 0.5)
        self.assertGreater(result.prompt_tokens, 0)
        self.assertGreater(result.completion_tokens, 0)
        self.assertGreater(result.actual_cost, 0.0)
        self.assertEqual(result.http_status, 200)

    def test_reviewer_evaluation(self):
        reviewer = Reviewer()
        task_spec = TaskSpec(
            opportunity_id="4878017272",
            task="Resolve issue #6093",
            priority="HIGH",
            expected_output="Working code fix addressing IPv6 parsing",
            estimated_cost=0.00014,
            input_tokens=100
        )
        worker_result = WorkerResult(
            opportunity_id="4878017272",
            output="RESOLUTION PLAN FOR TASK: Resolve issue #6093\nVerified with test coverage.",
            execution_time_sec=0.65,
            actual_cost=0.00008,
            prompt_tokens=100,
            completion_tokens=200,
            model="gemini-1.5-flash",
            http_status=200
        )
        review_res = reviewer.review(task_spec, worker_result)
        self.assertIsInstance(review_res, ReviewResult)
        self.assertTrue(review_res.passed)
        self.assertGreater(review_res.score, 0.70)
        self.assertGreater(review_res.review_cost, 0.0)

    def test_approval_gate_decision(self):
        def approve_provider(opp, tspec, wres, rres):
            return ApprovalDecision(opportunity_id=opp.id, approved=True, comments="Passed")

        def reject_provider(opp, tspec, wres, rres):
            return ApprovalDecision(opportunity_id=opp.id, approved=False, comments="Rejected")

        gate_approve = ApprovalGate(decision_provider=approve_provider)
        gate_reject = ApprovalGate(decision_provider=reject_provider)
        
        opp = Opportunity("id1", "t", "d", "s", {})
        tspec = TaskSpec("id1", "t", "p", "e", 0.05, 100)
        wres = WorkerResult("id1", "out", 0.65, 0.00008, 100, 200, "gemini-1.5-flash", 200)
        rres = ReviewResult("id1", True, 0.9, "good", 0.00003, 50)

        self.assertTrue(gate_approve.request_approval(opp, tspec, wres, rres).approved)
        self.assertFalse(gate_reject.request_approval(opp, tspec, wres, rres).approved)

    def test_audit_logger(self):
        logger = AuditLogger(log_filepath=TEST_LOG_FILE)
        logger.log_event("TEST_EVENT", {"summary": "Testing logger", "key": "val"})
        logs = logger.read_all_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["event_type"], "TEST_EVENT")
        self.assertEqual(logs[0]["payload"]["key"], "val")

    def test_five_consecutive_successful_executions(self):
        summary = run_phase0_loop(num_opportunities=5, auto_approve=True, log_file=TEST_LOG_FILE)
        self.assertEqual(summary["total_tasks"], 5)
        self.assertEqual(summary["successful_executions"], 5)
        self.assertEqual(summary["blocked_executions"], 0)

    def test_average_cost_under_quarter(self):
        summary = run_phase0_loop(num_opportunities=5, auto_approve=True, log_file=TEST_LOG_FILE)
        self.assertLess(summary["average_cost_per_task"], 0.25)
        self.assertTrue(summary["cost_under_threshold"])

    def test_approval_gate_rejection_blocks_execution(self):
        summary = run_phase0_loop(num_opportunities=5, auto_approve=False, log_file=TEST_LOG_FILE)
        self.assertEqual(summary["total_tasks"], 5)
        self.assertEqual(summary["successful_executions"], 4)
        self.assertEqual(summary["blocked_executions"], 1)

if __name__ == "__main__":
    unittest.main()
