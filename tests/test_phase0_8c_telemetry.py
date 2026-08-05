import os
import sys
import time
import json
import sqlite3
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import Database, resolve_db_path
from src.budget_guard import BudgetGuard
from src.health import HealthMonitor
from src.watchdog import OperationalWatchdog

TEST_DB_FILE = "test_phase0_8c_telemetry.db"

class TestPhase08CTelemetry(unittest.TestCase):

    def setUp(self):
        self.target_db = resolve_db_path(TEST_DB_FILE)
        if os.path.exists(self.target_db):
            try:
                os.remove(self.target_db)
            except PermissionError:
                pass
        self.db = Database(TEST_DB_FILE)
        self.today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def tearDown(self):
        # Close connection cleanly without deleting target_db so it remains available for inspection
        del self.db
        time.sleep(0.1)

    def _seed_deterministic_tasks(self):
        """Seeds 5 deterministic opportunities representing every non-overlapping state:
        1. proc-telemetry-1: COMPLETED, spend $0.06 ($0.05 worker + $0.01 review)
        2. proc-telemetry-2: WORKER_FAILED, error_reason='WORKER_FAILED: Syntax error'
        3. proc-telemetry-3: BLOCKED, error_reason='BUDGET_BLOCKED: Exceeds daily limit'
        4. proc-telemetry-4: QUALITY_REJECTED, error_reason='QUALITY_REJECTED: Description too short'
        5. proc-telemetry-5: BLOCKED, error_reason='APPROVAL_REJECTED: Below threshold', spend $0.04 ($0.03 worker + $0.01 review)
        """
        now = time.time()
        # Task 1: COMPLETED
        self.db.execute_atomic_transition(
            {
                "task_id": "proc-telemetry-1",
                "opportunity_id": "proc-telemetry-1",
                "state": "COMPLETED",
                "source": "proc_telemetry_source",
                "repo": "telemetry/repo",
                "title": "Completed Task 1",
                "description": "Deterministic telemetry task 1",
                "payload": {"repo": "telemetry/repo"},
                "worker_result": {"status": "SUCCESS", "actual_cost": 0.05, "execution_time_sec": 1.0},
                "review_result": {"passed": True, "score": 0.95, "review_cost": 0.01},
                "created_at": now,
                "updated_at": now
            },
            spend_record=(0.06, self.today_date_str, "Worker $0.05 + Review $0.01")
        )

        # Task 2: WORKER_FAILED
        self.db.execute_atomic_transition({
            "task_id": "proc-telemetry-2",
            "opportunity_id": "proc-telemetry-2",
            "state": "WORKER_FAILED",
            "error_reason": "WORKER_FAILED: Execution crash",
            "source": "proc_telemetry_source",
            "repo": "telemetry/repo",
            "title": "Worker Failed Task 2",
            "description": "Deterministic telemetry task 2",
            "payload": {"repo": "telemetry/repo"},
            "created_at": now,
            "updated_at": now
        })

        # Task 3: BLOCKED by Budget
        self.db.execute_atomic_transition({
            "task_id": "proc-telemetry-3",
            "opportunity_id": "proc-telemetry-3",
            "state": "BLOCKED",
            "error_reason": "BUDGET_BLOCKED: Estimated cost exceeds daily limit",
            "source": "proc_telemetry_source",
            "repo": "telemetry/repo",
            "title": "Budget Blocked Task 3",
            "description": "Deterministic telemetry task 3",
            "payload": {"repo": "telemetry/repo"},
            "created_at": now,
            "updated_at": now
        })

        # Task 4: QUALITY_REJECTED
        self.db.execute_atomic_transition({
            "task_id": "proc-telemetry-4",
            "opportunity_id": "proc-telemetry-4",
            "state": "QUALITY_REJECTED",
            "error_reason": "QUALITY_REJECTED: Description too short",
            "source": "proc_telemetry_source",
            "repo": "telemetry/repo",
            "title": "Quality Rejected Task 4",
            "description": "Deterministic telemetry task 4",
            "payload": {"repo": "telemetry/repo"},
            "created_at": now,
            "updated_at": now
        })

        # Task 5: BLOCKED by Approval Rejection
        self.db.execute_atomic_transition(
            {
                "task_id": "proc-telemetry-5",
                "opportunity_id": "proc-telemetry-5",
                "state": "BLOCKED",
                "error_reason": "APPROVAL_REJECTED: Score below threshold",
                "source": "proc_telemetry_source",
                "repo": "telemetry/repo",
                "title": "Approval Rejected Task 5",
                "description": "Deterministic telemetry task 5",
                "payload": {"repo": "telemetry/repo"},
                "worker_result": {"status": "SUCCESS", "actual_cost": 0.03, "execution_time_sec": 1.0},
                "review_result": {"passed": True, "score": 0.85, "review_cost": 0.01},
                "created_at": now,
                "updated_at": now
            },
            spend_record=(0.04, self.today_date_str, "Worker $0.03 + Review $0.01"),
            approval_record=("appr-proc-5", "REJECTED", "Score below threshold", now)
        )

    def test_live_sql_telemetry_metrics_reconciliation(self):
        """Verifies Database.get_telemetry_metrics():
        1. Computes every field via live SQL queries.
        2. Category counts strictly partition total_tasks (sum-of-categories == total_tasks with zero overlap).
        3. Independently cross-checks SUM(budget.amount) against JSON actual_cost + review_cost extracted from tasks.
        """
        self._seed_deterministic_tasks()

        # Compute via Database helper
        metrics = self.db.get_telemetry_metrics(self.today_date_str)

        # Raw SQLite Query Verification
        conn = sqlite3.connect(self.target_db)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        raw_total_tasks = cur.execute("SELECT COUNT(*) as cnt FROM tasks").fetchone()["cnt"]
        raw_successful = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'COMPLETED'").fetchone()["cnt"]
        raw_worker_failed = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'WORKER_FAILED'").fetchone()["cnt"]
        raw_quality_rejected = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'QUALITY_REJECTED'").fetchone()["cnt"]
        raw_budget_blocked = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'BLOCKED' AND error_reason LIKE 'BUDGET_BLOCKED:%'").fetchone()["cnt"]
        raw_approval_rejected = cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'BLOCKED' AND (error_reason NOT LIKE 'BUDGET_BLOCKED:%' OR error_reason IS NULL)").fetchone()["cnt"]

        raw_total_cost = cur.execute("SELECT SUM(amount) as total FROM budget").fetchone()["total"]
        raw_today_spend = cur.execute("SELECT SUM(amount) as total FROM budget WHERE date_str = ?", (self.today_date_str,)).fetchone()["total"]

        # Raw Genuine Cost Cross-Check Query
        raw_reconciled_cost = cur.execute("""
            SELECT SUM(
                COALESCE(json_extract(worker_result_json, '$.actual_cost'), 0.0) +
                COALESCE(json_extract(review_result_json, '$.review_cost'), 0.0)
            ) as total FROM tasks
        """).fetchone()["total"]
        conn.close()

        # Assert 1: Live SQL queries match exact raw SQL counts
        self.assertEqual(metrics["total_tasks"], raw_total_tasks)
        self.assertEqual(metrics["total_tasks"], 5)
        self.assertEqual(metrics["successful_executions"], raw_successful)
        self.assertEqual(metrics["successful_executions"], 1)
        self.assertEqual(metrics["worker_failed_executions"], raw_worker_failed)
        self.assertEqual(metrics["worker_failed_executions"], 1)
        self.assertEqual(metrics["budget_blocked_executions"], raw_budget_blocked)
        self.assertEqual(metrics["budget_blocked_executions"], 1)
        self.assertEqual(metrics["quality_rejected_executions"], raw_quality_rejected)
        self.assertEqual(metrics["quality_rejected_executions"], 1)
        self.assertEqual(metrics["approval_rejected_executions"], raw_approval_rejected)
        self.assertEqual(metrics["approval_rejected_executions"], 1)

        # Assert 2: Strict Category Partitioning (sum of categories == total_tasks with zero overlap)
        cat_sum = (
            metrics["successful_executions"] +
            metrics["worker_failed_executions"] +
            metrics["quality_rejected_executions"] +
            metrics["budget_blocked_executions"] +
            metrics["approval_rejected_executions"]
        )
        self.assertEqual(cat_sum, metrics["total_tasks"], f"Category sum ({cat_sum}) MUST equal total_tasks ({metrics['total_tasks']})!")
        self.assertTrue(metrics["categories_partition_total"])

        # Assert 3: Genuine Cost Cross-Check (budget table vs tasks execution JSON extractions)
        self.assertAlmostEqual(metrics["total_cost"], float(raw_total_cost), places=6)
        self.assertAlmostEqual(metrics["reconciled_task_cost"], float(raw_reconciled_cost), places=6)
        self.assertAlmostEqual(metrics["total_cost"], 0.10, places=6)
        self.assertAlmostEqual(metrics["reconciled_task_cost"], 0.10, places=6)
        self.assertEqual(metrics["cost_reconciliation_diff"], 0.0)
        self.assertTrue(metrics["cost_reconciled"])

        # Assert 4: Derived rates
        self.assertAlmostEqual(metrics["success_rate"], 0.20, places=4)
        self.assertAlmostEqual(metrics["failure_rate"], 0.80, places=4)
        self.assertAlmostEqual(metrics["average_cost_per_task"], 0.02, places=6)

        # Copy generated test DB to project root for user inspection
        root_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), TEST_DB_FILE)
        shutil.copyfile(self.target_db, root_db)

    def test_budget_guard_cumulative_today_spend_live_query(self):
        """Verifies BudgetGuard.cumulative_today_spend is a live query property that reconciles
        against SUM(amount) in SQLite budget table.
        """
        self._seed_deterministic_tasks()
        guard = BudgetGuard(db=self.db)

        # Initial live spend query check
        self.assertAlmostEqual(guard.cumulative_today_spend, 0.10, places=6)

        # Record extra spend directly into DB
        self.db.record_spend("proc-telemetry-1", 0.05, self.today_date_str, "Extra worker spend")

        # Access property again: MUST immediately reflect new spend via live query (0.10 + 0.05 = 0.15)
        self.assertAlmostEqual(guard.cumulative_today_spend, 0.15, places=6)

    def test_health_monitor_includes_live_telemetry(self):
        """Verifies HealthMonitor includes telemetry computed via live Database.get_telemetry_metrics()."""
        self._seed_deterministic_tasks()
        watchdog = OperationalWatchdog(db=self.db)
        monitor = HealthMonitor(db=self.db, watchdog=watchdog)

        metrics = monitor.get_metrics()
        self.assertIn("telemetry", metrics)
        tel = metrics["telemetry"]
        self.assertEqual(tel["total_tasks"], 5)
        self.assertEqual(tel["successful_executions"], 1)
        self.assertAlmostEqual(tel["today_cumulative_spend"], 0.10, places=6)
        self.assertTrue(tel["categories_partition_total"])
        self.assertTrue(tel["cost_reconciled"])

if __name__ == "__main__":
    unittest.main()
