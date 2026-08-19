import unittest
import os
import tempfile
import time
import json
import sqlite3
from dataclasses import dataclass
from typing import Optional

from src.tester import Tester, TesterResult
from src.planner import TaskSpec, Planner
from src.worker import WorkerResult, Worker
from src.reviewer import Reviewer, ReviewResult
from src.opportunity import Opportunity
from src.db import Database
from src.logger import AuditLogger
from src.quality import OpportunityQualityScorer
from src.budget_guard import BudgetGuard
from src.approval import ApprovalGate
from src.engine import AutonomousEngine
from tools.verify_audit import verify_audit
from tools.replay_audit import verify_replay

class TestTester(unittest.TestCase):
    def setUp(self):
        self.tester = Tester()

    def test_curses_getattrs_fabrication_fails(self):
        """Confirm curses getattrs() fabrication fails with getattrs marked unresolved."""
        task_spec = TaskSpec(
            opportunity_id="opp_curses_fab",
            task="Fix curses attribute restoration in CPython",
            priority="HIGH",
            expected_output="Working curses fix",
            estimated_cost=0.0001,
            input_tokens=100
        )
        worker_result = WorkerResult(
            opportunity_id="opp_curses_fab",
            output=(
                "```python\n"
                "import curses\n\n"
                "def fix_screen(stdscr):\n"
                "    active_attrs = stdscr.getattrs()\n"
                "    stdscr.attrset(active_attrs)\n"
                "```"
            ),
            execution_time_sec=0.1,
            actual_cost=0.0001,
            prompt_tokens=100,
            completion_tokens=50,
            model="gemini-3.5-flash-lite"
        )
        result = self.tester.check(task_spec, worker_result)
        self.assertFalse(result.passed)
        self.assertTrue(any("getattrs" in s for s in result.unresolved_symbols))
        self.assertIn("unresolved symbol", result.feedback.lower())

    def test_valid_symbol_passes(self):
        """Confirm genuine existing symbols (e.g. os.path.exists) pass."""
        task_spec = TaskSpec(
            opportunity_id="opp_valid_sym",
            task="Check path existence helper",
            priority="LOW",
            expected_output="Valid path check",
            estimated_cost=0.0001,
            input_tokens=50
        )
        worker_result = WorkerResult(
            opportunity_id="opp_valid_sym",
            output=(
                "```python\n"
                "import os\n\n"
                "def check_file(path_str):\n"
                "    if os.path.exists(path_str):\n"
                "        return os.path.abspath(path_str)\n"
                "    return None\n"
                "```"
            ),
            execution_time_sec=0.1,
            actual_cost=0.0001,
            prompt_tokens=50,
            completion_tokens=50,
            model="gemini-3.5-flash-lite"
        )
        result = self.tester.check(task_spec, worker_result)
        self.assertTrue(result.passed)
        self.assertEqual(result.unresolved_symbols, [])
        self.assertTrue(any("os.path.exists" in s for s in result.checked_symbols))

    def test_inconclusive_nothing_checkable_passes(self):
        """Confirm inconclusive snippet with no checkable standard library attributes passes."""
        task_spec = TaskSpec(
            opportunity_id="opp_inconclusive",
            task="Custom algorithmic routine",
            priority="MEDIUM",
            expected_output="Pure logic implementation",
            estimated_cost=0.0001,
            input_tokens=50
        )
        worker_result = WorkerResult(
            opportunity_id="opp_inconclusive",
            output=(
                "```python\n"
                "def calculate_fibonacci(n):\n"
                "    a, b = 0, 1\n"
                "    for _ in range(n):\n"
                "        a, b = b, a + b\n"
                "    return a\n"
                "```"
            ),
            execution_time_sec=0.1,
            actual_cost=0.0001,
            prompt_tokens=50,
            completion_tokens=50,
            model="gemini-3.5-flash-lite"
        )
        result = self.tester.check(task_spec, worker_result)
        self.assertTrue(result.passed)
        self.assertEqual(result.unresolved_symbols, [])
        self.assertIn("inconclusive", result.feedback.lower())

class TestTesterPipelineIntegration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_engine_tester.db")
        self.log_path = os.path.join(self.temp_dir.name, "test_engine_tester_audit.jsonl")
        
        self.db = Database(db_path=self.db_path)
        self.db.log_filepath = self.log_path
        self.logger = AuditLogger(log_filepath=self.log_path, db=self.db)
        
        self.approval_gate = ApprovalGate(db=self.db)
        self.approval_gate.trusted_repos = ["pydantic/pydantic", "ansible/ansible"]

    def tearDown(self):
        self.temp_dir.cleanup()

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

    def test_tester_rejected_task_never_reaches_reviewer(self):
        """Confirm a task failing Tester check transitions to QUALITY_REJECTED and never calls Reviewer."""
        class MockFabricatingWorker:
            def execute(self, task_spec):
                return WorkerResult(
                    opportunity_id=task_spec.opportunity_id,
                    output=(
                        "```python\n"
                        "import curses\n"
                        "def fix(stdscr):\n"
                        "    return stdscr.getattrs()\n"
                        "```"
                    ),
                    execution_time_sec=0.1,
                    actual_cost=0.0001,
                    prompt_tokens=100,
                    completion_tokens=50,
                    model="gemini-3.5-flash-lite"
                )

        class FailingReviewer:
            def review(self, task_spec, worker_result):
                raise AssertionError("Reviewer should NEVER be called on a TESTER_REJECTED task!")

        engine = AutonomousEngine(
            db=self.db,
            quality_scorer=OpportunityQualityScorer(),
            planner=Planner(),
            budget_guard=BudgetGuard(db=self.db, log_filepath=self.log_path),
            worker=MockFabricatingWorker(),
            tester=Tester(),
            reviewer=FailingReviewer(),
            approval_gate=self.approval_gate,
            logger=self.logger,
            log_filepath=self.log_path
        )

        opp = Opportunity(
            id="opp_tester_block_1",
            title="Curses window getattrs bug",
            description="Fabricated getattrs method in curses patch.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 999}
        )
        task_id = self._seed_task(opp)

        # Process task -> should be rejected by Tester before Reviewer
        res = engine.process_task(task_id)
        self.assertEqual(res["status"], "QUALITY_REJECTED")
        
        task_row = self.db.get_task(task_id)
        self.assertEqual(task_row["state"], "QUALITY_REJECTED")
        self.assertIn("TESTER_REJECTED", task_row["error_reason"])
        
        # Verify review_result_json was NEVER populated
        self.assertIsNone(task_row["review_result"])

        # Verify audit log & replay validation
        is_valid, errs = verify_audit(self.db_path, self.log_path)
        self.assertTrue(is_valid, f"verify_audit failed: {errs}")

        replay_passed, diffs = verify_replay(self.db_path, self.log_path)
        self.assertTrue(replay_passed, f"replay_audit failed: {diffs}")

    def test_manager_domain_tagging_in_task_planned_event(self):
        """Confirm Manager tags domain_trusted=True for trusted repos and False for untrusted repos in TASK_PLANNED."""
        engine = AutonomousEngine(
            db=self.db,
            quality_scorer=OpportunityQualityScorer(),
            planner=Planner(),
            budget_guard=BudgetGuard(db=self.db, log_filepath=self.log_path),
            worker=Worker(),
            tester=Tester(),
            reviewer=Reviewer(),
            approval_gate=self.approval_gate,
            logger=self.logger,
            log_filepath=self.log_path
        )

        # 1. Trusted repo: pydantic
        opp_trusted = Opportunity(
            id="opp_mgr_pydantic",
            title="Valid pydantic issue",
            description="Pydantic core performance enhancement.",
            source="github",
            payload={"repo": "pydantic/pydantic", "issue_number": 111}
        )
        self._seed_task(opp_trusted)
        engine.process_task(opp_trusted.id)

        # 2. Untrusted repo: cpython
        opp_untrusted = Opportunity(
            id="opp_mgr_cpython",
            title="CPython issue",
            description="CPython internal issue.",
            source="github",
            payload={"repo": "python/cpython", "issue_number": 222}
        )
        self._seed_task(opp_untrusted)
        engine.process_task(opp_untrusted.id)

        # Inspect audit_log for domain_trusted field
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        rows = cur.execute("SELECT payload_json FROM audit_log WHERE event_type='TASK_PLANNED' ORDER BY id ASC").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)

        payload_trusted = json.loads(rows[0][0])
        self.assertEqual(payload_trusted["repo"], "pydantic/pydantic")
        self.assertTrue(payload_trusted.get("domain_trusted"))

        payload_untrusted = json.loads(rows[1][0])
        self.assertEqual(payload_untrusted["repo"], "python/cpython")
        self.assertFalse(payload_untrusted.get("domain_trusted"))

if __name__ == "__main__":
    unittest.main()
