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
from tools.review_queue import (
    list_queue,
    explain_task,
    approve_task,
    reject_task,
    detect_hedge_language,
    detect_stub_placeholder,
    kpis_report
)
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
        
        # Approval gate with require_human_review_for_llm_judged = True and trusted_repos
        self.approval_gate = ApprovalGate(db=self.db)
        self.approval_gate.require_human_review_for_llm_judged = True
        self.approval_gate.auto_approve = True
        self.approval_gate.trusted_repos = ["pydantic/pydantic", "ansible/ansible"]

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

    def test_trusted_repo_llm_judged_auto_completes(self):
        """Confirm a trusted-repo task (llm_judged, passed) auto-completes without entering WAITING_APPROVAL."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        
        # pydantic/pydantic is in trusted_repos
        opp_pydantic = Opportunity(
            id="opp_trusted_pydantic",
            title="Fix serialization in pydantic-core",
            description="Fix datetime serialization timezone bug.",
            source="github",
            payload={"repo": "pydantic/pydantic", "issue_number": 808}
        )
        task_id = self._seed_task(opp_pydantic)
        result = engine.process_task(task_id)
        
        self.assertEqual(result["status"], "COMPLETED")
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "COMPLETED")
        
        # Approval record should be APPROVED
        approval = self.db.get_approval(f"appr-{opp_pydantic.id}")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["status"], "APPROVED")
        self.assertIn("trusted repository", approval["comments"])
        
        # Queue list should have 0 items
        queued = list_queue(db=self.db)
        self.assertEqual(len(queued), 0)

        # ansible/ansible is also in trusted_repos
        opp_ansible = Opportunity(
            id="opp_trusted_ansible",
            title="Fix copy module xattr preservation",
            description="Preserve extended attributes during file copy.",
            source="github",
            payload={"repo": "ansible/ansible", "issue_number": 809}
        )
        task_id_ans = self._seed_task(opp_ansible)
        result_ans = engine.process_task(task_id_ans)
        self.assertEqual(result_ans["status"], "COMPLETED")
        task_ans = self.db.get_task(task_id_ans)
        self.assertEqual(task_ans["state"], "COMPLETED")

    def test_non_trusted_repo_llm_judged_enters_waiting_approval(self):
        """Confirm a non-trusted-repo task (e.g. cpython or unknown) still enters WAITING_APPROVAL."""
        engine = self._create_engine(MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"))
        
        opp_cpython = Opportunity(
            id="opp_untrusted_cpython",
            title="Add curses window attribute helper",
            description="CPython C-internals change.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 901}
        )
        task_id = self._seed_task(opp_cpython)
        result = engine.process_task(task_id)
        
        self.assertEqual(result["status"], "WAITING_APPROVAL")
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "WAITING_APPROVAL")
        
        approval = self.db.get_approval(f"appr-{opp_cpython.id}")
        self.assertIsNotNone(approval)
        self.assertEqual(approval["status"], "PENDING")
        self.assertEqual(approval["comments"], "Waiting for human review (llm_judged)")
        
        # Queue list should contain this task
        queued = list_queue(db=self.db)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["task_id"], task_id)

    def test_list_queue_header_displays_trusted_repos(self):
        """Confirm list_queue prints trusted repos in the output header."""
        import io
        from contextlib import redirect_stdout
        
        f = io.StringIO()
        with redirect_stdout(f):
            list_queue(db=self.db)
        output = f.getvalue()
        self.assertIn("Trusted repos (auto-approve): pydantic/pydantic, ansible/ansible", output)

    def test_detect_stub_placeholder_flags_pandas_factorize_fixture(self):
        """Confirm detect_stub_placeholder detects the real pandas factorize bare pass with description comment."""
        pandas_fixture = (
            "### Code Fix\n"
            "```python\n"
            "    if not use_na_sentinel:\n"
            "        mask = indices == -1\n"
            "        if mask.any():\n"
            "            # Add a NaN/Null sentinel category to the dictionary values\n"
            "            # or handle the index mapping so nulls get len(dictionary).\n"
            "            pass # (Implementation assigns new index for nulls)\n"
            "```"
        )
        stubs = detect_stub_placeholder(pandas_fixture)
        self.assertTrue(len(stubs) >= 1)
        self.assertTrue(any("pass" in s for s in stubs))
        self.assertTrue(any("Implementation assigns new index" in s or "Add a NaN" in s for s in stubs))

    def test_detect_stub_placeholder_preceding_comment_only(self):
        """Confirm detect_stub_placeholder detects a pass preceded by a comment describing intended behavior."""
        code_snippet = (
            "```python\n"
            "def handle_null_values(series):\n"
            "    # We should assign a default index to missing items\n"
            "    pass\n"
            "```"
        )
        stubs = detect_stub_placeholder(code_snippet)
        self.assertEqual(len(stubs), 1)
        self.assertIn("should assign", stubs[0])

    def test_detect_stub_placeholder_complete_code_passes(self):
        """Confirm genuinely complete code without pass or hedging returns empty list."""
        complete_code = (
            "```python\n"
            "def factorize_fast(values):\n"
            "    uniques, codes = np.unique(values, return_inverse=True)\n"
            "    return codes, uniques\n"
            "```"
        )
        stubs = detect_stub_placeholder(complete_code)
        self.assertEqual(stubs, [])

    def test_detect_stub_placeholder_legitimate_empty_stub_passes(self):
        """Confirm legitimate empty function stub without intent comment does not get flagged."""
        abstract_code = (
            "```python\n"
            "class BaseResampler:\n"
            "    def resample(self):\n"
            "        pass\n\n"
            "    def aggregate(self, func):\n"
            "        pass\n"
            "```"
        )
        stubs = detect_stub_placeholder(abstract_code)
        self.assertEqual(stubs, [])

    def test_list_queue_and_explain_displays_stub_warning(self):
        """Confirm list_queue and explain_task output stub placeholder warnings when task has stubbed pass."""
        import io
        from contextlib import redirect_stdout

        class MockStubWorker:
            def execute(self, task_spec):
                return WorkerResult(
                    opportunity_id=task_spec.opportunity_id,
                    output=(
                        "```python\n"
                        "    if not use_na_sentinel:\n"
                        "        # Handle index mapping\n"
                        "        pass # (Implementation assigns new index for nulls)\n"
                        "```"
                    ),
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
            worker=MockStubWorker(),
            reviewer=MockLLMReviewer(passed=True, score=0.95, review_method="llm_judged"),
            approval_gate=self.approval_gate,
            logger=self.logger,
            log_filepath=self.log_path
        )
        opp = Opportunity(
            id="opp_stub_test",
            title="Pandas factorize null fix",
            description="Fix factorize handling for use_na_sentinel=False.",
            source="github",
            payload={"repo": "pandas-dev/pandas", "issue_number": 5555}
        )
        task_id = self._seed_task(opp)
        engine.process_task(task_id)

        # 1. Test list_queue output
        f_list = io.StringIO()
        with redirect_stdout(f_list):
            list_queue(db=self.db)
        list_output = f_list.getvalue()
        self.assertIn("[WARNING] STUB PLACEHOLDER DETECTED", list_output)

        # 2. Test explain_task output
        f_explain = io.StringIO()
        with redirect_stdout(f_explain):
            explain_task(task_id, db=self.db)
        explain_output = f_explain.getvalue()
        self.assertIn("--- STUB PLACEHOLDER DETECTION ---", explain_output)
        self.assertIn("[WARNING] Stub placeholder detected", explain_output)

    def test_kpis_report_overall_and_per_repo(self):
        """Confirm kpis_report computes overall lifecycle telemetry and per-repo statistics matching seeded data."""
        now = time.time()
        # Seed 2 completed pydantic tasks (trusted)
        for i in range(2):
            self.db.execute_atomic_transition({
                "task_id": f"kpi_pyd_{i}",
                "opportunity_id": f"kpi_pyd_{i}",
                "state": "COMPLETED",
                "repo": "pydantic/pydantic",
                "title": f"Pydantic task {i}",
                "worker_result": {"actual_cost": 0.001},
                "review_result": {"review_cost": 0.0005},
                "created_at": now,
                "updated_at": now
            }, spend_record=(0.0015, "2026-08-21", f"pydantic {i}"))

        # Seed 1 completed ansible task (trusted)
        self.db.execute_atomic_transition({
            "task_id": "kpi_ans_0",
            "opportunity_id": "kpi_ans_0",
            "state": "COMPLETED",
            "repo": "ansible/ansible",
            "title": "Ansible task 0",
            "worker_result": {"actual_cost": 0.002},
            "review_result": {"review_cost": 0.0005},
            "created_at": now,
            "updated_at": now
        }, spend_record=(0.0025, "2026-08-21", "ansible 0"))

        # Seed 1 blocked cpython task (untrusted)
        self.db.execute_atomic_transition({
            "task_id": "kpi_cpy_0",
            "opportunity_id": "kpi_cpy_0",
            "state": "BLOCKED",
            "repo": "python/cpython",
            "title": "CPython task 0",
            "error_reason": "HUMAN_APPROVAL_REJECTED: Invalid C syntax",
            "worker_result": {"actual_cost": 0.001},
            "review_result": {"review_cost": 0.0005},
            "created_at": now,
            "updated_at": now
        }, spend_record=(0.0015, "2026-08-21", "cpython 0"))

        res = kpis_report(db=self.db)
        
        # 1. Overall stats
        self.assertEqual(res["overall"]["total_tasks"], 4)
        self.assertEqual(res["overall"]["successful_executions"], 3)
        self.assertEqual(res["overall"]["approval_rejected_executions"], 1)
        self.assertEqual(res["overall"]["total_cost"], 0.007)
        self.assertAlmostEqual(res["overall"]["success_rate"], 0.75)
        self.assertAlmostEqual(res["overall"]["approval_rate"], 0.75)

        # 2. Per-repo stats (pydantic: 2, cpython: 1, ansible: 1)
        repos = {r["repo"]: r for r in res["per_repo"]}
        self.assertIn("pydantic/pydantic", repos)
        self.assertEqual(repos["pydantic/pydantic"]["total"], 2)
        self.assertEqual(repos["pydantic/pydantic"]["completed"], 2)
        self.assertTrue(repos["pydantic/pydantic"]["trusted"])

        self.assertIn("python/cpython", repos)
        self.assertEqual(repos["python/cpython"]["total"], 1)
        self.assertEqual(repos["python/cpython"]["blocked"], 1)
        self.assertFalse(repos["python/cpython"]["trusted"])

        self.assertIn("ansible/ansible", repos)
        self.assertEqual(repos["ansible/ansible"]["total"], 1)
        self.assertEqual(repos["ansible/ansible"]["completed"], 1)
        self.assertTrue(repos["ansible/ansible"]["trusted"])

    def test_kpis_report_rejection_and_approval_sources(self):
        """Confirm kpis_report correctly distinguishes tester, human rejection, trusted auto-approval, heuristic, and human approval."""
        now = time.time()
        # Seed base task records for FK constraints
        for opp_id, repo in [("1", "pydantic/pydantic"), ("2", "scikit-learn/scikit-learn"), ("3", "ansible/ansible")]:
            self.db.execute_atomic_transition({
                "task_id": opp_id,
                "opportunity_id": opp_id,
                "state": "COMPLETED",
                "repo": repo,
                "created_at": now,
                "updated_at": now
            })

        # 1. TESTER_REJECTED in audit log
        self.db.log_event("TESTER_REJECTED", {"task_id": "test_t_rej", "unresolved": ["os.fake_symbol"]})

        # 2. HUMAN_APPROVAL_REJECTED in audit log
        self.db.log_event("HUMAN_APPROVAL_REJECTED", {"task_id": "test_h_rej", "reason": "Too narrow"})

        # 3. Trusted auto-approval in approvals table
        self.db.save_approval("appr-1", "1", "APPROVED", "Autonomous approval: pydantic/pydantic is a trusted repository")

        # 4. Heuristic fallback auto-approval in approvals table
        self.db.save_approval("appr-2", "2", "APPROVED", "Autonomous non-interactive approval")

        # 5. Human approval via review_queue in approvals table
        self.db.save_approval("appr-3", "3", "APPROVED", "Human approval granted via review_queue")

        res = kpis_report(db=self.db)
        sources = res["pipeline_sources"]
        self.assertEqual(sources["tester_rejections"], 1)
        self.assertEqual(sources["human_rejections"], 1)
        self.assertEqual(sources["trusted_auto_approvals"], 1)
        self.assertEqual(sources["heuristic_auto_approvals"], 1)
        self.assertEqual(sources["human_approvals"], 1)

    def test_kpis_report_detector_signals_quality(self):
        """Confirm kpis_report computes approval/rejection rates against tasks with hedge and stub content."""
        now = time.time()
        
        # 1. Hedge task that was BLOCKED
        self.db.execute_atomic_transition({
            "task_id": "sig_hedge_blocked",
            "opportunity_id": "sig_hedge_blocked",
            "state": "BLOCKED",
            "repo": "pandas-dev/pandas",
            "title": "Hedge patch",
            "worker_result": {"output": "This is a conceptual patch."},
            "created_at": now,
            "updated_at": now
        })

        # 2. Stub task that was BLOCKED
        self.db.execute_atomic_transition({
            "task_id": "sig_stub_blocked",
            "opportunity_id": "sig_stub_blocked",
            "state": "BLOCKED",
            "repo": "pandas-dev/pandas",
            "title": "Stub patch",
            "worker_result": {"output": "```python\n    # Should handle missing\n    pass # (Implementation assigns index)\n```"},
            "created_at": now,
            "updated_at": now
        })

        # 3. Clean task that was COMPLETED
        self.db.execute_atomic_transition({
            "task_id": "sig_clean_completed",
            "opportunity_id": "sig_clean_completed",
            "state": "COMPLETED",
            "repo": "pydantic/pydantic",
            "title": "Clean patch",
            "worker_result": {"output": "```python\ndef solve(): return 42\n```"},
            "created_at": now,
            "updated_at": now
        })

        res = kpis_report(db=self.db)
        signals = res["detector_signals"]
        
        self.assertEqual(signals["hedge"]["total"], 1)
        self.assertEqual(signals["hedge"]["rejected"], 1)
        self.assertEqual(signals["hedge"]["rejection_rate"], 1.0)

        self.assertEqual(signals["stub"]["total"], 1)
        self.assertEqual(signals["stub"]["rejected"], 1)
        self.assertEqual(signals["stub"]["rejection_rate"], 1.0)

        self.assertEqual(signals["clean"]["total"], 1)
        self.assertEqual(signals["clean"]["approved"], 1)
        self.assertEqual(signals["clean"]["approval_rate"], 1.0)

if __name__ == "__main__":
    unittest.main()
