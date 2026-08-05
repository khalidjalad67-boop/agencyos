import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import json
import unittest
from typing import List

from src.opportunity import OpportunityFetcher, Opportunity
from src.planner import Planner, TaskSpec
from src.budget_guard import BudgetGuard
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.logger import AuditLogger
from main import run_phase0_loop

TEST_LOG_FILE = "test_phase0_5_audit_log.jsonl"
TEST_CONFIG_FILE = "config/test_settings.yaml"

_SKIP_NETWORK = os.environ.get("AGENTOS_SKIP_NETWORK_TESTS") == "1"
_SKIP_NETWORK_REASON = "Network tests skipped via AGENTOS_SKIP_NETWORK_TESTS=1"

class TestPhase05Hardening(unittest.TestCase):
    
    def setUp(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)
        if os.path.exists(TEST_CONFIG_FILE):
            os.remove(TEST_CONFIG_FILE)
            
        os.makedirs("config", exist_ok=True)
        with open(TEST_CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write("budget:\n  per_task_limit: 0.25\n  daily_limit: 2.00\n  hard_stop: true\n")

    def tearDown(self):
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)
        if os.path.exists(TEST_CONFIG_FILE):
            os.remove(TEST_CONFIG_FILE)

    def test_budget_guard_per_task_limit(self):
        guard = BudgetGuard(config_path=TEST_CONFIG_FILE, log_filepath=TEST_LOG_FILE)
        
        # Task with estimated cost exceeding ceiling ($0.30 > $0.25)
        overpriced_task = TaskSpec(
            opportunity_id="opp-overpriced",
            task="Overpriced task",
            priority="HIGH",
            expected_output="Output",
            estimated_cost=0.30,
            input_tokens=500
        )
        passed, reason = guard.check_budget(overpriced_task)
        self.assertFalse(passed)
        self.assertIn("exceeds per-task ceiling", reason)

    def test_budget_guard_daily_limit(self):
        guard = BudgetGuard(config_path=TEST_CONFIG_FILE, log_filepath=TEST_LOG_FILE)
        guard.cumulative_today_spend = 1.95  # Spent $1.95 of $2.00 daily limit
        
        task_spec = TaskSpec(
            opportunity_id="opp-test",
            task="Regular task",
            priority="MEDIUM",
            expected_output="Output",
            estimated_cost=0.10,  # $1.95 + $0.10 = $2.05 > $2.00 limit
            input_tokens=100
        )
        passed, reason = guard.check_budget(task_spec)
        self.assertFalse(passed)
        self.assertIn("exceeds daily limit", reason)

    def test_worker_crash_isolation(self):
        worker = Worker()
        crashing_task = TaskSpec(
            opportunity_id="opp-crash",
            task="SIMULATED_WORKER_CRASH in worker execution",
            priority="HIGH",
            expected_output="Output",
            estimated_cost=0.05,
            input_tokens=100
        )
        # Worker execute must NOT throw unhandled exception; must isolate crash!
        result = worker.execute(crashing_task)
        self.assertIsInstance(result, WorkerResult)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.http_status, 500)
        self.assertIn("crash isolated", result.error_reason.lower())

    @unittest.skipIf(_SKIP_NETWORK, _SKIP_NETWORK_REASON)
    def test_retry_and_timeout_resilience(self):
        fetcher = OpportunityFetcher(max_retries=2)
        opps = fetcher.fetch_opportunities(limit=3)
        self.assertEqual(len(opps), 3)
        self.assertGreaterEqual(fetcher.max_retries, 2)

    def test_partial_failure_mixed_workload(self):
        """Simulates a realistic mixed 5-task workload (2 successes, 1 worker crash, 1 budget block, 1 approval rejection)."""
        logger = AuditLogger(log_filepath=TEST_LOG_FILE)
        guard = BudgetGuard(config_path=TEST_CONFIG_FILE, log_filepath=TEST_LOG_FILE)
        planner = Planner()
        worker = Worker()
        reviewer = Reviewer()
        
        # 5 Mixed Opportunities
        opportunities = [
            Opportunity("1", "Success issue 1", "Fix bug in repo", "test", {"issue_number": 1, "repo": "test/repo"}),
            Opportunity("2", "Worker crash issue", "SIMULATED_WORKER_CRASH", "test", {"issue_number": 2, "repo": "test/repo"}),
            Opportunity("3", "Budget blocked issue", "Overpriced task description", "test", {"issue_number": 3, "repo": "test/repo"}),
            Opportunity("4", "Rejection issue", "Issue rejected by approval gate", "test", {"issue_number": 4, "repo": "test/repo"}),
            Opportunity("5", "Success issue 2", "Fix another bug", "test", {"issue_number": 5, "repo": "test/repo"}),
        ]
        
        def mock_approval(opp, tspec, wres, rres):
            if opp.id == "4":
                return ApprovalDecision(opp.id, False, "Rejected by approval gate")
            return ApprovalDecision(opp.id, True, "Approved")

        approval_gate = ApprovalGate(decision_provider=mock_approval)
        
        success_count = 0
        worker_failed_count = 0
        budget_blocked_count = 0
        approval_rejected_count = 0
        
        for opp in opportunities:
            approval_gate.db.execute_atomic_transition({"task_id": opp.id, "opportunity_id": opp.id, "state": "DISCOVERED", "created_at": 1.0, "updated_at": 1.0})
            tspec = planner.plan(opp)
            
            # Force high cost for opp 3 to trigger budget block
            if opp.id == "3":
                tspec.estimated_cost = 0.35
                
            # 1. Budget Guard
            passed, reason = guard.check_budget(tspec)
            if not passed:
                budget_blocked_count += 1
                logger.log_event("BUDGET_BLOCKED", {"opportunity_id": opp.id, "reason": reason})
                continue
                
            # 2. Worker
            wres = worker.execute(tspec)
            if wres.status == "FAILED":
                worker_failed_count += 1
                logger.log_event("WORKER_FAILED", {"opportunity_id": opp.id, "reason": wres.error_reason})
                continue
                
            # 3. Reviewer
            rres = reviewer.review(tspec, wres)
            
            # 4. Approval Gate
            adec = approval_gate.request_approval(opp, tspec, wres, rres)
            if adec.approved:
                success_count += 1
                logger.log_event("TASK_COMPLETED", {"opportunity_id": opp.id, "cost": wres.actual_cost + rres.review_cost})
            else:
                approval_rejected_count += 1
                logger.log_event("TASK_BLOCKED", {"opportunity_id": opp.id, "reason": adec.comments})

        # Loop must complete 100% cleanly without crashing!
        self.assertEqual(success_count, 2)
        self.assertEqual(worker_failed_count, 1)
        self.assertEqual(budget_blocked_count, 1)
        self.assertEqual(approval_rejected_count, 1)
        
        # Verify log file recorded all events
        logs = logger.read_all_logs()
        self.assertTrue(any(l["event_type"] == "BUDGET_BLOCKED" for l in logs))
        self.assertTrue(any(l["event_type"] == "WORKER_FAILED" for l in logs))
        self.assertTrue(any(l["event_type"] == "TASK_BLOCKED" for l in logs))
        self.assertTrue(any(l["event_type"] == "TASK_COMPLETED" for l in logs))

if __name__ == "__main__":
    unittest.main()
