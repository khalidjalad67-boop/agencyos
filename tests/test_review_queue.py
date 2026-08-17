import os
import unittest
import tempfile
import json
import time
from src.db import Database
from src.engine import AutonomousEngine
from src.opportunity import Opportunity
from src.planner import Planner, TaskSpec
from src.quality import OpportunityQualityScorer
from src.budget_guard import BudgetGuard
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate
from src.logger import AuditLogger
from tools.review_queue import list_queue, approve_task, reject_task
from tools.verify_audit import verify_audit
from tools.replay_audit import verify_replay

class MockWorker:
    def execute(self, task_spec: TaskSpec) -> WorkerResult:
        return WorkerResult(
            opportunity_id=task_spec.opportunity_id,
            output="Proposed fix with sample code implementation.",
            execution_time_sec=0.05,
            actual_cost=0.0001,
            prompt_tokens=100,
            completion_tokens=200,
            model="gemini-3.5-flash-lite",
            http_status=200,
            status="SUCCESS",
            error_reason=""
        )

class MockLLMReviewer:
    def __init__(self, passed: bool = True, score: float = 0.95, review_method: str = "llm_judged"):
        self.passed = passed
        self.score = score
        self.review_method = review_method

    def review(self, task_spec: TaskSpec, worker_result: WorkerResult) -> ReviewResult:
        return ReviewResult(
            opportunity_id=task_spec.opportunity_id,
            passed=self.passed,
            score=self.score,
            feedback="LLM review: proposed patch appears plausible structurally.",
            review_cost=0.00005,
            review_tokens=150,
            review_method=self.review_method
        )

class TestReviewQueue(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_queue.db")
        self.log_path = os.path.join(self.temp_dir.name, "test_queue_audit.jsonl")
        
        self.db = Database(db_path=self.db_path)
        self.db.log_filepath = self.log_path
        self.logger = AuditLogger(log_filepath=self.log_path, db=self.db)
        
        # Approval gate with require_human_review_for_llm_judged = True
        self.approval_gate = ApprovalGate(db=self.db)
        self.approval_gate.require_human_review_for_llm_judged = True
        self.approval_gate.auto_approve = True

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_engine(self, reviewer):
        return AutonomousEngine(
            db=self.db,
            quality_scorer=OpportunityQualityScorer(),
            planner=Planner(),
            budget_guard=BudgetGuard(db=self.db, log_filepath=self.log_path),
            worker=MockWorker(),
            reviewer=reviewer,
            approval_gate=self.approval_gate,
            logger=self.logger,
            log_filepath=self.log_path
        )

    def _seed_task(self, opp: Opportunity) -> str:
        now = time.time()
        self.db.execute_atomic_transition({
            "task_id": opp.id,
            "opportunity_id": opp.id,
            "state": "DISCOVERED",
            "source": opp.source,
            "repo": opp.payload.get("repo", "unknown"),
            "title": opp.title,
            "description": opp.description,
            "payload": opp.payload,
            "created_at": now,
            "updated_at": now
        })
        return opp.id

    def test_llm_judged_passing_task_lands_in_waiting_approval(self):
        """Confirm an LLM-judged passing task lands in WAITING_APPROVAL instead of auto-completing."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        
        opp = Opportunity(
            id="opp_llm_1",
            title="Fix CPython segfault in parser",
            description="Buffer overflow underflow in token parser.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 101}
        )
        task_id = self._seed_task(opp)
        
        result = engine.process_task(task_id)
        self.assertEqual(result["status"], "WAITING_APPROVAL")
        
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "WAITING_APPROVAL")
        self.assertEqual(task["review_result"]["review_method"], "llm_judged")
        
        approval = self.db.get_approval(f"appr-{opp.id}")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["status"], "PENDING")
        
        # Check review_queue list finds this task
        queued = list_queue(db=self.db)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["task_id"], task_id)

    def test_review_queue_approve_transitions_to_completed(self):
        """Confirm review_queue approve transitions task to COMPLETED with HUMAN_APPROVAL_GRANTED audit event."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        
        opp = Opportunity(
            id="opp_llm_approve",
            title="Fix requests header parsing",
            description="Malformed headers cause unhandled ValueError.",
            source="github",
            payload={"repo": "psf/requests", "issue_number": 202}
        )
        task_id = self._seed_task(opp)
        engine.process_task(task_id)
        
        # Verify it is waiting approval
        task_before = self.db.get_task(task_id)
        self.assertEqual(task_before["state"], "WAITING_APPROVAL")
        
        # Approve via review queue tool
        success = approve_task(task_id, db=self.db)
        self.assertTrue(success)
        
        task_after = self.db.get_task(task_id)
        self.assertEqual(task_after["state"], "COMPLETED")
        self.assertAlmostEqual(task_after["cost"], 0.00015, places=5)
        
        approval = self.db.get_approval(f"appr-{opp.id}")
        self.assertEqual(approval["status"], "APPROVED")
        
        # Verify audit log and replay integrity
        is_valid, errs = verify_audit(self.db_path, self.log_path)
        self.assertTrue(is_valid, f"verify_audit failed: {errs}")
        
        replay_passed, diffs = verify_replay(self.db_path, self.log_path)
        self.assertTrue(replay_passed, f"replay_audit failed: {diffs}")

    def test_review_queue_reject_transitions_to_blocked(self):
        """Confirm review_queue reject transitions task to BLOCKED with HUMAN_APPROVAL_REJECTED audit event."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        
        opp = Opportunity(
            id="opp_llm_reject",
            title="Invalid C syntax patch in CPython",
            description="Broken patch passes LLM plausibility check.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 303}
        )
        task_id = self._seed_task(opp)
        engine.process_task(task_id)
        
        task_before = self.db.get_task(task_id)
        self.assertEqual(task_before["state"], "WAITING_APPROVAL")
        
        # Reject with specific reason via review queue tool
        reject_reason = "Invalid C syntax, missing variable declaration in patch."
        success = reject_task(task_id, reason=reject_reason, db=self.db)
        self.assertTrue(success)
        
        task_after = self.db.get_task(task_id)
        self.assertEqual(task_after["state"], "BLOCKED")
        self.assertIn(reject_reason, task_after["error_reason"])
        
        approval = self.db.get_approval(f"appr-{opp.id}")
        self.assertEqual(approval["status"], "REJECTED")
        
        # Verify audit log and replay integrity
        is_valid, errs = verify_audit(self.db_path, self.log_path)
        self.assertTrue(is_valid, f"verify_audit failed: {errs}")
        
        replay_passed, diffs = verify_replay(self.db_path, self.log_path)
        self.assertTrue(replay_passed, f"replay_audit failed: {diffs}")

    def test_heuristic_fallback_still_auto_completes(self):
        """Confirm heuristic fallback reviews continue to auto-complete when auto_approve is True."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.85, review_method="heuristic_fallback"))
        
        opp = Opportunity(
            id="opp_heuristic_1",
            title="Heuristic evaluated task",
            description="Valid issue description.",
            source="github",
            payload={"repo": "psf/requests", "issue_number": 404}
        )
        task_id = self._seed_task(opp)
        result = engine.process_task(task_id)
        
        self.assertEqual(result["status"], "COMPLETED")
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "COMPLETED")

    def test_llm_judged_pending_persists_across_multiple_ticks_and_does_not_auto_approve(self):
        """Simulate multiple ticks on an llm_judged passing task: confirm request_approval() returns None on every tick without auto-approving."""
        opp = Opportunity(
            id="opp_multi_tick_llm",
            title="Complex bug in CPython parser",
            description="Patch requiring human verification.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 505}
        )
        self._seed_task(opp)
        task_spec = TaskSpec(
            opportunity_id=opp.id,
            task="Fix parser crash",
            priority="HIGH",
            expected_output="Working C code",
            estimated_cost=0.0001,
            input_tokens=100
        )
        worker_res = WorkerResult(
            opportunity_id=opp.id,
            output="Proposed C fix",
            execution_time_sec=0.1,
            actual_cost=0.0001,
            prompt_tokens=100,
            completion_tokens=50,
            model="gemini-3.5-flash-lite"
        )
        review_res = ReviewResult(
            opportunity_id=opp.id,
            passed=True,
            score=0.95,
            feedback="LLM review passed plausibility check.",
            review_cost=0.00005,
            review_tokens=100,
            review_method="llm_judged"
        )

        # Tick 1: first call to request_approval()
        decision_tick1 = self.approval_gate.request_approval(opp, task_spec, worker_res, review_res)
        self.assertIsNone(decision_tick1, "First call must return None (hold for human review)")
        
        # Verify approval row is PENDING with human-review comment
        appr_row = self.db.get_approval(f"appr-{opp.id}")
        self.assertIsNotNone(appr_row)
        self.assertEqual(appr_row["status"], "PENDING")
        self.assertEqual(appr_row["comments"], "Waiting for human review (llm_judged)")

        # Tick 2: second call simulating next scheduler tick on the same WAITING_APPROVAL task
        decision_tick2 = self.approval_gate.request_approval(opp, task_spec, worker_res, review_res)
        self.assertIsNone(decision_tick2, "Second tick must STILL return None and NOT auto-approve")

        # Tick 3: third call simulating yet another scheduler tick
        decision_tick3 = self.approval_gate.request_approval(opp, task_spec, worker_res, review_res)
        self.assertIsNone(decision_tick3, "Subsequent ticks must continue returning None until human action")

    def test_detect_hedge_language(self):
        """Confirm hedge language detection identifies all specified phrases case-insensitively."""
        from tools.review_queue import detect_hedge_language
        
        sample_hedge_text = (
            "Here is a Conceptual Patch for the issue. Note: the actual implementation "
            "would require further refactoring. In principle, this illustrative simplified pseudo-code works."
        )
        detected = detect_hedge_language(sample_hedge_text)
        self.assertIn("conceptual patch", detected)
        self.assertIn("the actual implementation", detected)
        self.assertIn("in principle", detected)
        self.assertIn("illustrative", detected)
        self.assertIn("simplified", detected)
        self.assertIn("pseudo-code", detected)

        clean_text = "def fix_parser(): return parse_tokens(strict=True)"
        self.assertEqual(detect_hedge_language(clean_text), [])

    def test_explain_task(self):
        """Confirm explain_task produces clean formatted output for both hedge and non-hedge tasks."""
        from tools.review_queue import explain_task
        
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        opp = Opportunity(
            id="opp_explain_test",
            title="Fix SSL handshake deadlock",
            description="Race condition in SSL connection wrap.",
            source="github",
            payload={"repo": "psf/requests", "issue_number": 606}
        )
        task_id = self._seed_task(opp)
        engine.process_task(task_id)

        # Call explain_task and verify it returns True
        success = explain_task(task_id, db=self.db)
        self.assertTrue(success)

        # Non-existent task returns False
        self.assertFalse(explain_task("non_existent_id", db=self.db))

    def test_list_queue_displays_hedge_warning(self):
        """Confirm list_queue detects hedge language in pending task output."""
        class MockHedgeWorker:
            def execute(self, task_spec):
                return WorkerResult(
                    opportunity_id=task_spec.opportunity_id,
                    output="This is an illustrative conceptual patch with placeholder logic.",
                    execution_time_sec=0.05,
                    actual_cost=0.0001,
                    prompt_tokens=100,
                    completion_tokens=200,
                    model="gemini-3.5-flash-lite"
                )

        engine = AutonomousEngine(
            db=self.db,
            quality_scorer=OpportunityQualityScorer(),
            planner=Planner(),
            budget_guard=BudgetGuard(db=self.db, log_filepath=self.log_path),
            worker=MockHedgeWorker(),
            reviewer=MockLLMReviewer(passed=True, score=0.90, review_method="llm_judged"),
            approval_gate=self.approval_gate,
            logger=self.logger,
            log_filepath=self.log_path
        )
        opp = Opportunity(
            id="opp_hedge_list",
            title="Broken C patch",
            description="Patch with conceptual language.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 707}
        )
        task_id = self._seed_task(opp)
        engine.process_task(task_id)

        queued = list_queue(db=self.db)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["task_id"], task_id)

if __name__ == "__main__":
    unittest.main()
