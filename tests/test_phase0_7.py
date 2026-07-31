import os
import sys
import time
import json
import unittest
import subprocess
import tempfile
from typing import List, Dict, Any

from src.db import Database
from src.opportunity import Opportunity
from src.scheduler import Scheduler
from src.engine import AutonomousEngine
from src.health import HealthMonitor
from src.recovery import run_startup_recovery
from src.idempotency import IdempotencyGuard
from src.watchdog import OperationalWatchdog
from src.budget_guard import BudgetGuard
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult

TEST_DB_FILE = "test_phase0_7.db"
TEST_LOG_FILE = "test_phase0_7_audit.jsonl"

class TestPhase07AutonomousOperations(unittest.TestCase):

    def setUp(self):
        if os.path.exists(TEST_DB_FILE):
            os.remove(TEST_DB_FILE)
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)
        self.db = Database(TEST_DB_FILE)

    def tearDown(self):
        if os.path.exists(TEST_DB_FILE):
            os.remove(TEST_DB_FILE)
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)

    def test_scheduler_creates_discovered_tasks_without_duplicates(self):
        scheduler = Scheduler(self.db, interval_sec=30.0)
        test_opps = [
            Opportunity(id="opp-101", title="Task 1", description="Description 1", source="test_source", payload={"repo": "repo/a"}),
            Opportunity(id="opp-102", title="Task 2", description="Description 2", source="test_source", payload={"repo": "repo/b"})
        ]
        
        first_tick = scheduler.tick(opportunities_override=test_opps)
        self.assertEqual(len(first_tick), 2)
        self.assertIn("opp-101", first_tick)
        self.assertIn("opp-102", first_tick)

        # Duplicate tick should be ignored
        second_tick = scheduler.tick(opportunities_override=test_opps)
        self.assertEqual(len(second_tick), 0)

        discovered = self.db.get_tasks_by_state("DISCOVERED")
        self.assertEqual(len(discovered), 2)

    def test_pre_planning_quality_check_prevents_planned_state(self):
        """User Requirement 2: Explicit unit test asserting that an opportunity with description_length == 0
        never reaches PLANNED state in the tasks table.
        """
        empty_body_opp = Opportunity(
            id="opp-empty-body",
            title="Empty Issue Body",
            description="",  # description_length == 0
            source="test_source",
            payload={"repo": "repo/a", "labels": []}
        )

        scheduler = Scheduler(self.db)
        scheduler.tick(opportunities_override=[empty_body_opp])

        engine = AutonomousEngine(self.db, log_filepath=TEST_LOG_FILE)
        res = engine.process_task("opp-empty-body")

        self.assertEqual(res["status"], "QUALITY_REJECTED")

        # Direct database assertion on tasks table
        task_in_db = self.db.get_task("opp-empty-body")
        self.assertIsNotNone(task_in_db)
        self.assertEqual(task_in_db["state"], "CANCELLED")
        self.assertIn("QUALITY_REJECTED", task_in_db["error_reason"])
        
        # Verify it NEVER reached PLANNED state
        planned_tasks = self.db.get_tasks_by_state("PLANNED")
        self.assertEqual(len(planned_tasks), 0)

    def test_idempotency_guard_prevents_duplicate_billing(self):
        task_data = {
            "task_id": "opp-billing-test",
            "opportunity_id": "opp-billing-test",
            "state": "DISCOVERED",
            "source": "test_source",
            "repo": "repo/a",
            "title": "Fix bug in parser",
            "description": "Detailed problem description covering bug fix.",
            "payload": {"repo": "repo/a", "labels": ["bug"]}
        }
        self.db.save_task(task_data)

        engine = AutonomousEngine(self.db, log_filepath=TEST_LOG_FILE)
        res1 = engine.process_task("opp-billing-test")
        self.assertEqual(res1["status"], "COMPLETED")

        initial_spend = self.db.get_today_spend(engine.budget_guard.today_date_str)
        self.assertGreater(initial_spend, 0.0)

        # Reset task state to EXECUTING to test idempotency guard re-run
        task = self.db.get_task("opp-billing-test")
        task["state"] = "EXECUTING"
        self.db.save_task(task)

        # Re-process task
        res2 = engine.process_task("opp-billing-test")
        self.assertEqual(res2["status"], "COMPLETED")

        # Spend MUST remain identical because Idempotency Guard returned cached outputs!
        second_spend = self.db.get_today_spend(engine.budget_guard.today_date_str)
        self.assertEqual(initial_spend, second_spend)

    def test_watchdog_backoff_and_cooldown_recovery(self):
        watchdog = OperationalWatchdog(self.db, failure_threshold=2, base_cooldown_sec=0.2)
        source = "unhealthy_repo"

        self.assertTrue(watchdog.is_source_enabled(source))

        # Record 2 consecutive failures -> threshold reached -> source disabled
        watchdog.record_failure(source, "Network Timeout")
        watchdog.record_failure(source, "500 Internal Error")

        self.assertFalse(watchdog.is_source_enabled(source))
        self.assertIn(source, watchdog.get_disabled_sources())

        # Wait for cooldown period (0.2s)
        time.sleep(0.3)

        # Source should automatically recover after cooldown
        self.assertTrue(watchdog.is_source_enabled(source))
        self.assertEqual(len(watchdog.get_disabled_sources()), 0)

    def test_health_monitor_json_output(self):
        watchdog = OperationalWatchdog(self.db)
        health = HealthMonitor(self.db, watchdog)

        metrics = health.get_metrics()
        self.assertIn("queue_depth", metrics)
        self.assertIn("running_tasks", metrics)
        self.assertIn("pending_approvals", metrics)
        self.assertIn("disabled_sources", metrics)
        self.assertIn("failure_rate", metrics)

        json_str = health.get_metrics_json()
        data = json.loads(json_str)
        self.assertEqual(data["queue_depth"], 0)

    def test_budget_single_source_of_truth_and_migration(self):
        """User Requirement 1: Verify SQLite budget table is single source of truth and legacy
        audit log file read is invoked strictly as a one-time initial fallback when budget table is empty.
        """
        # Create dummy legacy audit log file
        today_str = time.strftime("%Y-%m-%d", time.gmtime())
        legacy_entry = {
            "event_type": "TASK_COMPLETED",
            "timestamp": time.time(),
            "payload": {"cost": 0.1234}
        }
        with open(TEST_LOG_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps(legacy_entry) + "\n")

        # Initializing BudgetGuard on empty DB should trigger one-time migration
        bg = BudgetGuard(log_filepath=TEST_LOG_FILE, db=self.db)
        today_spend = self.db.get_today_spend(today_str)
        self.assertEqual(today_spend, 0.1234)

        # Subsequent spend writes directly to SQLite budget table
        bg.record_spend(0.0500, "opp-migrated")
        new_spend = self.db.get_today_spend(today_str)
        self.assertEqual(new_spend, 0.1734)

    def test_sigkill_process_crash_recovery_harness(self):
        """SIGKILL Crash-Recovery Test Harness:
        1. Spawns a worker script in a subprocess processing a task.
        2. Intentionally terminates the process (SIGKILL / proc.kill()) during EXECUTING state.
        3. Restarts execution with the same SQLite DB.
        4. Asserts correct recovery, zero duplicate billing, and valid task state.
        """
        # Step 1: Initialize task in DISCOVERED state in SQLite DB
        task_id = "crash-test-task-1"
        self.db.log_event("SIGKILL_CRASH_TEST_STARTED", {"task_id": task_id})
        self.db.save_task({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "DISCOVERED",
            "source": "crash_source",
            "repo": "crash/repo",
            "title": "Crash Recovery Verification Task",
            "description": "Comprehensive description for crash recovery testing.",
            "payload": {"repo": "crash/repo", "labels": []}
        })

        script_code = f"""
import sys, os, time
sys.path.insert(0, {json.dumps(os.getcwd())})
from src.db import Database
from src.engine import AutonomousEngine
from src.opportunity import Opportunity

db = Database({json.dumps(TEST_DB_FILE)})
engine = AutonomousEngine(db, log_filepath={json.dumps(TEST_LOG_FILE)})

task = db.get_task({json.dumps(task_id)})
opp = Opportunity(id=task["opportunity_id"], title=task["title"], description=task["description"], source=task["source"], payload=task["payload"])
task_spec = engine.planner.plan(opp)
task["task_spec"] = task_spec.to_dict()
task["state"] = "EXECUTING"
db.save_task(task)

print("STATE_IS_EXECUTING", flush=True)
time.sleep(30)
"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(script_code)
            script_path = f.name

        try:
            # Launch subprocess
            proc = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            
            # Wait for signal that process is in EXECUTING state
            while True:
                line = proc.stdout.readline()
                if "STATE_IS_EXECUTING" in line or proc.poll() is not None:
                    break

            # Verify task is currently EXECUTING in SQLite DB
            task_before_kill = self.db.get_task(task_id)
            self.assertEqual(task_before_kill["state"], "EXECUTING")

            # Step 3: FORCIBLY KILL THE PROCESS (SIGKILL / proc.kill())
            proc.kill()
            proc.wait()
            if proc.stdout: proc.stdout.close()
            if proc.stderr: proc.stderr.close()
            self.db.log_event("SIGKILL_PROCESS_TERMINATED", {"task_id": task_id, "signal": "SIGKILL"})

            # Step 4: Run Startup Recovery Routine on the exact same database
            recovery_summary = run_startup_recovery(self.db)
            self.assertEqual(recovery_summary["reset_tasks"], 1)

            # Task state should be reset to READY
            recovered_task = self.db.get_task(task_id)
            self.assertEqual(recovered_task["state"], "READY")

            # Step 5: Resume execution with AutonomousEngine
            engine = AutonomousEngine(self.db, log_filepath=TEST_LOG_FILE)
            # Mock worker execution for the crash recovery harness to isolate state-machine correctness
            mock_worker_res = WorkerResult(
                opportunity_id=task_id,
                output="Mocked worker output after crash recovery.",
                execution_time_sec=0.1,
                actual_cost=0.001,
                prompt_tokens=100,
                completion_tokens=50,
                model="mock-model"
            )
            mock_review_res = ReviewResult(
                opportunity_id=task_id,
                passed=True,
                score=0.95,
                feedback="Mocked review after crash recovery",
                review_cost=0.0005,
                review_tokens=30
            )
            engine.worker.execute = lambda tspec: mock_worker_res
            engine.reviewer.review = lambda tspec, wres: mock_review_res

            res = engine.process_task(task_id)
            self.assertEqual(res["status"], "COMPLETED")

            final_task = self.db.get_task(task_id)
            self.assertEqual(final_task["state"], "COMPLETED")
            self.db.log_event("SIGKILL_CRASH_TEST_RECOVERED", {"task_id": task_id, "final_state": "COMPLETED"})

            # Verify total spend in budget table is exactly 0.0015 (Worker + Reviewer billed EXACTLY ONCE)
            spend = self.db.get_today_spend(engine.budget_guard.today_date_str)
            self.assertEqual(spend, 0.0015)

        finally:
            if os.path.exists(script_path):
                os.remove(script_path)

    def test_scheduler_logs_heartbeat_event(self):
        """Verify SCHEDULER_HEARTBEAT is logged to SQLite audit_log table and JSONL on every tick."""
        from src.logger import AuditLogger
        logger = AuditLogger(log_filepath=TEST_LOG_FILE, db=self.db)
        scheduler = Scheduler(self.db, interval_sec=30.0, logger=logger)

        # 1. Tick when queue depth is 0
        scheduler.tick(opportunities_override=[])

        audit_logs = self.db.get_audit_logs(limit=10)
        heartbeat_logs = [l for l in audit_logs if l["event_type"] == "SCHEDULER_HEARTBEAT"]
        self.assertEqual(len(heartbeat_logs), 1)
        self.assertIn("timestamp", heartbeat_logs[0]["payload"])
        self.assertIn("queue_depth", heartbeat_logs[0]["payload"])
        self.assertEqual(heartbeat_logs[0]["payload"]["queue_depth"], 0)

        # Verify JSONL log file also contains the event
        all_jsonl = logger.read_all_logs()
        jsonl_heartbeats = [e for e in all_jsonl if e["event_type"] == "SCHEDULER_HEARTBEAT"]
        self.assertEqual(len(jsonl_heartbeats), 1)
        self.assertIn("queue_depth", jsonl_heartbeats[0]["payload"])

        # 2. Add an opportunity and tick again
        opp = Opportunity(id="heartbeat-opp-1", title="Test 1", description="Desc 1", source="src", payload={})
        scheduler.tick(opportunities_override=[opp])  # Tick 2: opp saved to DB

        # 3. Tick again (3rd tick) - queue depth is now 1 because heartbeat-opp-1 is in DISCOVERED state in DB
        opp2 = Opportunity(id="heartbeat-opp-2", title="Test 2", description="Desc 2", source="src", payload={})
        scheduler.tick(opportunities_override=[opp2])

        audit_logs = self.db.get_audit_logs(limit=10)
        heartbeat_logs = [l for l in audit_logs if l["event_type"] == "SCHEDULER_HEARTBEAT"]
        self.assertEqual(len(heartbeat_logs), 3)
        # Most recent heartbeat (3rd tick) found 1 item in queue
        self.assertEqual(heartbeat_logs[0]["payload"]["queue_depth"], 1)

if __name__ == "__main__":
    unittest.main()
