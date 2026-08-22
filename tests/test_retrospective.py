import os
import unittest
import tempfile
import time
from src.db import Database
from src.retrospective import determine_rejection_source, generate_retrospective_for_task
from tools.review_queue import retrospectives_report
from tools.backfill_retrospectives import run_backfill

class TestRetrospectives(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_retro.db")
        self.log_path = os.path.join(self.temp_dir.name, "test_retro.jsonl")
        self.db = Database(db_path=self.db_path, log_filepath=self.log_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_base_task(self, task_id: str, opp_id: str, state: str, repo: str = "pydantic/pydantic",
                         error_reason: str = "", worker_output: str = "Working solution implementation",
                         review_method: str = "llm_judged", worker_cost: float = 0.001, review_cost: float = 0.0005):
        task_data = {
            "task_id": task_id,
            "opportunity_id": opp_id,
            "state": state,
            "source": "github",
            "repo": repo,
            "title": "Fix issue",
            "description": "Bug fix description",
            "payload": {"issue_number": 101},
            "task_spec": {
                "opportunity_id": opp_id,
                "task": "Fix bug",
                "expected_output": "Working code",
                "estimated_cost": 0.005,
                "complexity": "LOW"
            },
            "worker_result": {
                "opportunity_id": opp_id,
                "output": worker_output,
                "execution_time_sec": 0.5,
                "actual_cost": worker_cost,
                "prompt_tokens": 150,
                "completion_tokens": 250,
                "model": "gemini-3.5-flash-lite",
                "http_status": 200,
                "status": "SUCCESS" if state != "WORKER_FAILED" else "FAILED",
                "error_reason": error_reason if state == "WORKER_FAILED" else ""
            },
            "review_result": {
                "opportunity_id": opp_id,
                "passed": state == "COMPLETED",
                "score": 0.95 if state == "COMPLETED" else 0.4,
                "feedback": "Review feedback",
                "review_cost": review_cost,
                "review_tokens": 100,
                "review_method": review_method
            } if state in ("COMPLETED", "BLOCKED") and not error_reason.startswith("BUDGET_BLOCKED") else None,
            "error_reason": error_reason
        }
        self.db.execute_atomic_transition(task_data)
        return task_data

    def test_retrospective_completed_clean(self):
        """Test completed task with clean output produces correct retrospective row."""
        self._seed_base_task(
            task_id="task_clean_1",
            opp_id="opp_clean_1",
            state="COMPLETED",
            repo="pydantic/pydantic",
            worker_output="def add(a, b):\n    return a + b\n"
        )
        retro = generate_retrospective_for_task("task_clean_1", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["task_id"], "task_clean_1")
        self.assertEqual(retro["repo"], "pydantic/pydantic")
        self.assertEqual(retro["outcome"], "COMPLETED")
        self.assertEqual(retro["review_method"], "llm_judged")
        self.assertEqual(retro["rejection_source"], None)
        self.assertFalse(retro["hedge_flagged"])
        self.assertFalse(retro["stub_flagged"])
        self.assertAlmostEqual(retro["cost"], 0.0015, places=5)

        # Verify persisted row in DB
        db_row = self.db.get_retrospective("task_clean_1")
        self.assertIsNotNone(db_row)
        self.assertEqual(db_row["outcome"], "COMPLETED")
        self.assertFalse(db_row["hedge_flagged"])
        self.assertFalse(db_row["stub_flagged"])

    def test_retrospective_completed_with_hedge_flag(self):
        """Test completed task with hedge language sets hedge_flagged=True."""
        self._seed_base_task(
            task_id="task_hedge_1",
            opp_id="opp_hedge_1",
            state="COMPLETED",
            repo="ansible/ansible",
            worker_output="This is a conceptual patch and theoretically should resolve the issue."
        )
        retro = generate_retrospective_for_task("task_hedge_1", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "COMPLETED")
        self.assertTrue(retro["hedge_flagged"])
        self.assertFalse(retro["stub_flagged"])

    def test_retrospective_completed_with_stub_flag(self):
        """Test completed task with stub placeholder sets stub_flagged=True."""
        self._seed_base_task(
            task_id="task_stub_1",
            opp_id="opp_stub_1",
            state="COMPLETED",
            repo="fastapi/fastapi",
            worker_output="def handle_route():\n    # TODO: implement handler logic\n    pass\n"
        )
        retro = generate_retrospective_for_task("task_stub_1", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "COMPLETED")
        self.assertFalse(retro["hedge_flagged"])
        self.assertTrue(retro["stub_flagged"])

    def test_retrospective_tester_rejected(self):
        """Test quality rejection via deterministic tester categorizes rejection_source=TESTER_REJECTED."""
        self._seed_base_task(
            task_id="task_tester_rej",
            opp_id="opp_tester_rej",
            state="QUALITY_REJECTED",
            repo="psf/requests",
            error_reason="TESTER_REJECTED: Unresolved symbols ['curses.window.getattrs']",
            worker_output="import curses\ncurses.window.getattrs()"
        )
        retro = generate_retrospective_for_task("task_tester_rej", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "QUALITY_REJECTED")
        self.assertEqual(retro["rejection_source"], "TESTER_REJECTED")

    def test_retrospective_human_rejection(self):
        """Test task blocked via human approval rejection categorizes rejection_source=HUMAN_APPROVAL_REJECTED."""
        self._seed_base_task(
            task_id="task_human_rej",
            opp_id="opp_human_rej",
            state="BLOCKED",
            repo="pandas-dev/pandas",
            error_reason="HUMAN_APPROVAL_REJECTED: Code lacks required unit test fixtures."
        )
        retro = generate_retrospective_for_task("task_human_rej", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "BLOCKED")
        self.assertEqual(retro["rejection_source"], "HUMAN_APPROVAL_REJECTED")

    def test_retrospective_budget_blocked(self):
        """Test task blocked via budget guard categorizes rejection_source=BUDGET_BLOCKED."""
        self._seed_base_task(
            task_id="task_budget_blk",
            opp_id="opp_budget_blk",
            state="BLOCKED",
            repo="scikit-learn/scikit-learn",
            error_reason="BUDGET_BLOCKED: Estimated cost 0.30 exceeds task limit 0.25"
        )
        retro = generate_retrospective_for_task("task_budget_blk", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "BLOCKED")
        self.assertEqual(retro["rejection_source"], "BUDGET_BLOCKED")

    def test_retrospective_worker_failed(self):
        """Test task failing during worker execution categorizes rejection_source=WORKER_FAILED."""
        self._seed_base_task(
            task_id="task_wfail",
            opp_id="opp_wfail",
            state="WORKER_FAILED",
            repo="pallets/flask",
            error_reason="Network timeout connecting to LLM endpoint"
        )
        retro = generate_retrospective_for_task("task_wfail", self.db)
        self.assertIsNotNone(retro)
        self.assertEqual(retro["outcome"], "WORKER_FAILED")
        self.assertEqual(retro["rejection_source"], "WORKER_FAILED")

    def test_retrospectives_report_and_backfill(self):
        """Test that backfill populates all terminal tasks and retrospectives_report outputs summary."""
        self._seed_base_task("task_b1", "opp_b1", "COMPLETED", "pydantic/pydantic")
        self._seed_base_task("task_b2", "opp_b2", "BLOCKED", "ansible/ansible", error_reason="BUDGET_BLOCKED: Limit exceeded")
        self._seed_base_task("task_b3", "opp_b3", "QUALITY_REJECTED", "psf/requests", error_reason="TESTER_REJECTED: Symbol error")

        # Run backfill on this test DB
        run_backfill(self.db_path)

        retros = self.db.get_all_retrospectives()
        self.assertEqual(len(retros), 3)

        # Run report
        report_res = retrospectives_report(self.db)
        self.assertEqual(len(report_res), 3)

if __name__ == "__main__":
    unittest.main()
