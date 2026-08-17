import os
import sys
import json
import time
import shutil
import tempfile
import unittest
import yaml

from src.db import Database, resolve_log_path
from src.opportunity import Opportunity
from src.planner import Planner, TaskSpec
from src.budget_guard import BudgetGuard
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.quality import OpportunityQualityScorer
from src.watchdog import OperationalWatchdog
from src.scheduler import Scheduler
from src.engine import AutonomousEngine
from src.health import HealthMonitor
from src.recovery import run_startup_recovery
from tools.verify_audit import verify_audit
from tools.replay_audit import verify_replay

class TestPhase1KernelFoundations(unittest.TestCase):
    """Phase 1 Kernel Foundations Test Suite.
    Validates:
    1. Unattended loop execution with approval gates.
    2. Cost-per-task visibility across Database queries, audit log, budget table, and telemetry.
    3. Flat Policy Engine configuration loading and enforcement across all components.
    4. Compliance with verify_audit and replay_audit verification tools.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="phase1_test_")
        self.db_path = os.path.join(self.temp_dir, "test_phase1.db")
        self.log_path = os.path.join(self.temp_dir, "test_phase1_audit.jsonl")
        self.config_path = os.path.join(self.temp_dir, "settings.yaml")

        # Create flat policy rules config
        self.default_config = {
            "budget": {
                "per_task_limit": 0.25,
                "daily_limit": 2.00,
                "hard_stop": True
            },
            "quality": {
                "min_description_length": 1,
                "structural_reject_tags": ["duplicate", "stale", "wontfix", "invalid"]
            },
            "approval": {
                "auto_approve": True
            },
            "watchdog": {
                "consecutive_failure_threshold": 3,
                "base_cooldown_sec": 30.0,
                "heartbeat_warning_multiplier": 2.0,
                "stall_threshold_multiplier": 5.0
            },
            "scheduler": {
                "interval_sec": 30.0,
                "idle_backoff_multiplier": 2.0,
                "max_idle_interval_sec": 240.0
            },
            "network": {
                "max_retries": 3,
                "timeout_sec": 10,
                "backoff_factor": 1.5
            }
        }
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.dump(self.default_config, f)

        self.db = Database(self.db_path, log_filepath=self.log_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_flat_policy_engine_config_loading(self):
        """Validates that all components load their operational rules from flat YAML config."""
        # 1. BudgetGuard
        bg = BudgetGuard(config_path=self.config_path, db=self.db, log_filepath=self.log_path)
        self.assertEqual(bg.per_task_limit, 0.25)
        self.assertEqual(bg.daily_limit, 2.00)
        self.assertTrue(bg.hard_stop)

        # 2. Quality Scorer
        qs = OpportunityQualityScorer(config_path=self.config_path)
        self.assertEqual(qs.min_description_length, 1)
        self.assertIn("duplicate", qs.structural_reject_labels)

        # 3. Watchdog
        wd = OperationalWatchdog(db=self.db, config_path=self.config_path)
        self.assertEqual(wd.failure_threshold, 3)
        self.assertEqual(wd.base_cooldown_sec, 30.0)
        self.assertEqual(wd.heartbeat_warning_multiplier, 2.0)
        self.assertEqual(wd.stall_threshold_multiplier, 5.0)

        # 4. Scheduler
        sched = Scheduler(db=self.db, config_path=self.config_path)
        self.assertEqual(sched.interval_sec, 30.0)
        self.assertEqual(sched.max_interval_sec, 240.0)
        self.assertEqual(sched.backoff_multiplier, 2.0)

        # 5. Approval Gate
        appr = ApprovalGate(db=self.db, config_path=self.config_path)
        self.assertTrue(appr.auto_approve)

    def test_cost_per_task_visibility_on_completed_tasks(self):
        """Validates that cost is visible on every completed task in SQLite, DB queries, and audit log."""
        engine = AutonomousEngine(db=self.db, log_filepath=self.log_path)

        opp = Opportunity(
            id="p1-cost-test-01",
            title="Implement task cost inspection helper",
            description="Add a first-class cost property to tasks and direct query helpers.",
            source="test_repo",
            payload={"repo": "test_repo", "issue_number": 101, "labels": ["enhancement"]}
        )

        # Discovered -> Process
        now = time.time()
        self.db.execute_atomic_transition({
            "task_id": opp.id,
            "opportunity_id": opp.id,
            "state": "DISCOVERED",
            "source": opp.source,
            "repo": "test_repo",
            "title": opp.title,
            "description": opp.description,
            "payload": opp.payload,
            "created_at": now,
            "updated_at": now
        })

        result = engine.process_task(opp.id)
        self.assertEqual(result["status"], "COMPLETED")

        # 1. Inspect task record returned by get_task
        task = self.db.get_task(opp.id)
        self.assertIsNotNone(task)
        self.assertEqual(task["state"], "COMPLETED")
        self.assertIn("cost", task)
        self.assertGreater(task["cost"], 0.0)

        # 2. Inspect cost returned by get_task_cost
        cost_direct = self.db.get_task_cost(opp.id)
        self.assertEqual(cost_direct, task["cost"])

        # 3. Inspect worker and review result costs match
        w_cost = task["worker_result"]["actual_cost"]
        r_cost = task["review_result"]["review_cost"]
        self.assertEqual(task["cost"], round(w_cost + r_cost, 6))

        # 4. Inspect budget table spend record
        with self.db._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM budget WHERE opportunity_id = ?", (opp.id,))
            budget_row = cur.fetchone()
            self.assertIsNotNone(budget_row)
            self.assertEqual(round(float(budget_row["amount"]), 6), task["cost"])

        # 5. Inspect TASK_COMPLETED audit event in DB
        with self.db._connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT payload_json FROM audit_log WHERE event_type = 'TASK_COMPLETED'")
            audit_row = cur.fetchone()
            self.assertIsNotNone(audit_row)
            payload = json.loads(audit_row["payload_json"])
            self.assertEqual(payload["task_id"], opp.id)
            self.assertEqual(round(payload["cost"], 6), task["cost"])

        # 6. Inspect telemetry metrics cost reconciliation
        metrics = self.db.get_telemetry_metrics()
        self.assertEqual(metrics["successful_executions"], 1)
        self.assertEqual(metrics["total_cost"], task["cost"])
        self.assertEqual(metrics["reconciled_task_cost"], task["cost"])
        self.assertTrue(metrics["cost_reconciled"])
        self.assertEqual(metrics["average_cost_per_task"], task["cost"])

    def test_unattended_loop_execution_and_approval_gates(self):
        """Validates that the loop executes unattended across diverse tasks, correctly
        respecting approval gates (approvals proceed to COMPLETED, rejections block to BLOCKED).
        """
        # Create custom decision provider to reject specific opportunity
        def decision_provider(opp, task_spec, worker_res, review_res):
            if opp.id == "p1-unattended-rejected":
                return ApprovalDecision(
                    opportunity_id=opp.id,
                    approved=False,
                    comments="Human Gate: Requires security review before execution."
                )
            return ApprovalDecision(
                opportunity_id=opp.id,
                approved=True,
                comments="Human Gate: Approved for execution."
            )

        appr_gate = ApprovalGate(db=self.db, decision_provider=decision_provider)
        engine = AutonomousEngine(db=self.db, approval_gate=appr_gate, log_filepath=self.log_path)
        scheduler = Scheduler(db=self.db, config_path=self.config_path, logger=engine.logger)
        health = HealthMonitor(db=self.db, watchdog=engine.watchdog)

        # 3 Opportunities:
        # Opp 1: Standard valid -> should reach COMPLETED with cost
        # Opp 2: Rejected by human approval gate -> should reach BLOCKED
        # Opp 3: Empty body -> rejected by quality policy -> should reach QUALITY_REJECTED
        opps = [
            Opportunity(
                id="p1-unattended-ok-1",
                title="Fix connection timeout in client",
                description="The HTTP client throws timeout exception on slow network responses.",
                source="test_repo",
                payload={"repo": "test_repo", "issue_number": 201, "labels": ["bug"]}
            ),
            Opportunity(
                id="p1-unattended-rejected",
                title="Add admin credentials endpoint",
                description="Expose administrator credentials via unauthenticated debug endpoint.",
                source="test_repo",
                payload={"repo": "test_repo", "issue_number": 202, "labels": ["security"]}
            ),
            Opportunity(
                id="p1-unattended-empty",
                title="Invalid empty issue",
                description="",  # Empty description
                source="test_repo",
                payload={"repo": "test_repo", "issue_number": 203, "labels": []}
            )
        ]

        # Tick scheduler
        scheduled_ids, next_interval = scheduler.tick(opportunities_override=opps)
        self.assertEqual(len(scheduled_ids), 3)

        # Process all discovered/pending tasks unattended
        unfinished_states = ("DISCOVERED", "PLANNED", "READY", "EXECUTING", "REVIEW", "WAITING_APPROVAL")
        tasks_to_process = []
        for st in unfinished_states:
            tasks_to_process.extend(self.db.get_tasks_by_state(st))

        for t in tasks_to_process:
            engine.process_task(t["task_id"])

        # Validate task states
        t1 = self.db.get_task("p1-unattended-ok-1")
        self.assertEqual(t1["state"], "COMPLETED")
        self.assertGreater(t1["cost"], 0.0)

        t2 = self.db.get_task("p1-unattended-rejected")
        self.assertEqual(t2["state"], "BLOCKED")
        self.assertIn("Human Gate: Requires security review", t2["error_reason"])

        t3 = self.db.get_task("p1-unattended-empty")
        self.assertEqual(t3["state"], "QUALITY_REJECTED")

        # Telemetry verification
        metrics = health.get_metrics()["telemetry"]
        self.assertEqual(metrics["total_tasks"], 3)
        self.assertEqual(metrics["successful_executions"], 1)
        self.assertEqual(metrics["approval_rejected_executions"], 1)
        self.assertEqual(metrics["quality_rejected_executions"], 1)
        self.assertTrue(metrics["categories_partition_total"])
        self.assertTrue(metrics["cost_reconciled"])
        self.assertEqual(metrics["total_cost"], round(t1["cost"] + t2["cost"], 6))

        # Run verify_audit and replay_audit against the produced DB and JSONL
        passed_v, errs_v = verify_audit(self.db_path, self.log_path)
        self.assertTrue(passed_v, f"verify_audit failed with errors: {errs_v}")

        passed_r, errs_r = verify_replay(self.db_path, self.log_path)
        self.assertTrue(passed_r, f"verify_replay failed with errors: {errs_r}")

    def test_cost_visible_for_every_completed_task_in_batch(self):
        """Validates that in a batch of 10 tasks, every single completed task has an exact, visible cost."""
        engine = AutonomousEngine(db=self.db, log_filepath=self.log_path)
        scheduler = Scheduler(db=self.db, config_path=self.config_path, logger=engine.logger)

        opps = [
            Opportunity(
                id=f"batch-task-{i:02d}",
                title=f"Batch task {i} description and requirements",
                description=f"Detailed description for batch workload task number {i} to be planned and executed.",
                source="test_repo",
                payload={"repo": "test_repo", "issue_number": 300 + i, "labels": ["feature"]}
            )
            for i in range(1, 11)
        ]

        scheduler.tick(opportunities_override=opps)

        for opp in opps:
            res = engine.process_task(opp.id)
            self.assertEqual(res["status"], "COMPLETED")

        completed_tasks = self.db.get_tasks_by_state("COMPLETED")
        self.assertEqual(len(completed_tasks), 10)

        total_calculated_cost = 0.0
        for task in completed_tasks:
            cost = task.get("cost")
            self.assertIsNotNone(cost, f"Task {task['task_id']} is missing cost property")
            self.assertGreater(cost, 0.0, f"Task {task['task_id']} cost must be positive")
            direct_cost = self.db.get_task_cost(task["task_id"])
            self.assertEqual(cost, direct_cost)
            total_calculated_cost += cost

        total_calculated_cost = round(total_calculated_cost, 6)
        metrics = self.db.get_telemetry_metrics()
        self.assertEqual(metrics["successful_executions"], 10)
        self.assertEqual(metrics["total_cost"], total_calculated_cost)
        self.assertEqual(metrics["reconciled_task_cost"], total_calculated_cost)
        self.assertTrue(metrics["cost_reconciled"])

        # Tool verification
        passed_v, errs_v = verify_audit(self.db_path, self.log_path)
        self.assertTrue(passed_v, f"verify_audit errors: {errs_v}")
        passed_r, errs_r = verify_replay(self.db_path, self.log_path)
        self.assertTrue(passed_r, f"verify_replay errors: {errs_r}")

if __name__ == "__main__":
    unittest.main()
