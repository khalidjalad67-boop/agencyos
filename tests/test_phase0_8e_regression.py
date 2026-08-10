"""
tests/test_phase0_8e_regression.py

Phase 0.8E -- Permanent Regression Suite for Phase 0.8 Historical Bugs.

This test file contains permanent regression tests for all historical bugs
diagnosed and fixed across Phase 0.8:

1. 0.8A1: Test isolation leak where test fixtures could write to project-root agencyos.db.
2. 0.8A2: Worker idempotency cache-hit path leaving worker_result_json NULL.
3. 0.8B: (a) REVIEW->REVIEW self-transition for crash recovery; (b) Watchdog _in_warning flag reset without full stall.
4. 0.8C: Disambiguation of BLOCKED tasks (BUDGET_BLOCKED vs APPROVAL_REJECTED) in telemetry.
5. 0.8D: Historical bypass detection in verify_audit.py (OPPORTUNITY_REJECTED and TASK_BLOCKED continuations).
6. Opportunity fetcher zero-item rate limit behavior (raises RuntimeError, no mock synthesis).

These tests exercise ALREADY-FIXED production code.
"""

import os
import sys
import json
import time
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["AGENTOS_TEST_MODE"] = "1"

from src.db import (
    Database,
    resolve_db_path,
    resolve_log_path,
    get_project_root,
    is_in_test_mode,
    InvalidStateTransitionError
)
from src.engine import AutonomousEngine, TaskSpec, WorkerResult, ReviewResult
from src.idempotency import IdempotencyGuard
from src.worker import Worker
from src.reviewer import Reviewer
from src.approval import ApprovalGate, ApprovalDecision
from src.watchdog import OperationalWatchdog
from src.opportunity import OpportunityFetcher, Opportunity
from src.recovery import run_startup_recovery
from tools.verify_audit import verify_audit
from tools.replay_audit import verify_replay


class TestPhase08ERegressionSuite(unittest.TestCase):
    """Permanent regression test suite covering all historical bugs in Phase 0.8."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_0_8e_reg_")
        self.db_path = os.path.join(self.temp_dir, "test_0_8e_reg.db")
        self.log_path = os.path.join(self.temp_dir, "test_0_8e_reg.jsonl")
        self.db = Database(self.db_path, self.log_path)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_engine(self) -> AutonomousEngine:
        """Helper to create AutonomousEngine with deterministic mock worker/reviewer."""
        engine = AutonomousEngine(self.db, log_filepath=self.log_path)

        mock_worker = Worker()
        mock_worker.execute = lambda tspec: WorkerResult(
            opportunity_id=tspec.opportunity_id,
            output="Mock worker output for regression test.",
            execution_time_sec=0.1,
            actual_cost=0.001,
            prompt_tokens=100,
            completion_tokens=50,
            model="mock-worker-model"
        )

        mock_reviewer = Reviewer()
        mock_reviewer.review = lambda tspec, wres: ReviewResult(
            opportunity_id=tspec.opportunity_id,
            passed=True,
            score=0.95,
            feedback="Mock review feedback: PASSED.",
            review_cost=0.0005,
            review_tokens=30
        )

        mock_approval = ApprovalGate(
            decision_provider=lambda opp, spec, wres, rres: ApprovalDecision(
                opportunity_id=opp.id, approved=True, comments="Auto-approved."
            ),
            db=self.db
        )

        engine.worker = mock_worker
        engine.reviewer = mock_reviewer
        engine.approval_gate = mock_approval
        return engine

    # -------------------------------------------------------------------------
    # 1. 0.8A1: Test Isolation Leak Prevention
    # -------------------------------------------------------------------------
    def test_0_8a1_test_isolation_leak_prevented(self):
        """0.8A1: Confirm resolve_db_path()/is_in_test_mode() makes it structurally impossible
        for a test-mode Database() call to resolve to the project-root agencyos.db path.
        """
        self.assertTrue(is_in_test_mode(), "Test suite must run with is_in_test_mode() == True")

        prod_db_path = os.path.join(get_project_root(), "agencyos.db")
        resolved_db = resolve_db_path("agencyos.db")

        self.assertNotEqual(
            resolved_db, prod_db_path,
            f"Test mode resolve_db_path('agencyos.db') resolved to production DB path: {prod_db_path}"
        )
        self.assertIn(".temp_test_dbs", resolved_db)

        # Confirm default Database() instantiation in test mode does not touch prod DB
        default_db = Database()
        self.assertNotEqual(
            default_db.db_path, prod_db_path,
            f"Default Database() instance resolved to production path: {default_db.db_path}"
        )

        # Confirm log path isolation as well
        prod_log_path = os.path.join(get_project_root(), "audit_log.jsonl")
        resolved_log = resolve_log_path("audit_log.jsonl")

        self.assertNotEqual(
            resolved_log, prod_log_path,
            f"Test mode resolve_log_path('audit_log.jsonl') resolved to production log path: {prod_log_path}"
        )
        self.assertIn(".temp_test_dbs", resolved_log)

    # -------------------------------------------------------------------------
    # 2. 0.8A2: Worker-Idempotency Cache-Hit Path
    # -------------------------------------------------------------------------
    def test_0_8a2_worker_idempotency_cache_hit_populates_worker_result_json(self):
        """0.8A2: Seed a task with a cached idempotency result but no worker_result_json
        in tasks, run process_task(), assert worker_result_json is populated after completion.
        """
        task_id = f"test-0-8a2-idem-{int(time.time() * 1000)}"
        now = time.time()

        # Step 1: Seed task in EXECUTING state with worker_result_json = NULL
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "EXECUTING",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Idempotency Cache Hit Test Task",
            "description": "Regression test for worker_result_json population on cache hit",
            "payload": {"repo": "test/repo"},
            "task_spec": TaskSpec(
                task_id, "Spec description", "HIGH", "Expected output", 0.01, 100
            ).to_dict(),
            "created_at": now,
            "updated_at": now
        })

        # Step 2: Seed idempotency guard cache with worker result
        expected_output = "Pre-cached worker result output."
        expected_cost = 0.0015
        cached_result = WorkerResult(
            opportunity_id=task_id,
            output=expected_output,
            execution_time_sec=0.4,
            actual_cost=expected_cost,
            prompt_tokens=120,
            completion_tokens=60,
            model="mock-cached-model"
        )
        idempotency = IdempotencyGuard(db=self.db)
        idempotency.save_worker_result(task_id, cached_result)

        # Confirm initial state: worker_result_json is NULL
        task_before = self.db.get_task(task_id)
        self.assertIsNone(task_before["worker_result_json"], "Pre-condition: worker_result_json must be NULL")

        # Step 3: Execute process_task() via engine
        engine = self._create_mock_engine()
        res = engine.process_task(task_id)
        self.assertEqual(res["status"], "COMPLETED")

        # Step 4: Assert worker_result_json is populated in DB
        task_after = self.db.get_task(task_id)
        self.assertIsNotNone(
            task_after["worker_result_json"],
            "worker_result_json MUST be populated after process_task() on idempotency cache hit"
        )
        w_data = json.loads(task_after["worker_result_json"])
        self.assertEqual(w_data.get("output"), expected_output)
        self.assertAlmostEqual(w_data.get("actual_cost", 0.0), expected_cost, places=6)

    # -------------------------------------------------------------------------
    # 3. 0.8B: State Machine REVIEW->REVIEW & Watchdog Warning Flag Reset
    # -------------------------------------------------------------------------
    def test_0_8b_review_self_transition_for_crash_recovery(self):
        """0.8B (a): Exercise real crash recovery resume path: seed a task in EXECUTING state
        with worker completed in idempotency cache but review not yet done. Route through
        run_startup_recovery() (which sets DB state to REVIEW), then let engine.process_task()
        complete the review naturally (triggering REVIEW -> REVIEW transition) and assert
        no InvalidStateTransitionError is raised.
        """
        task_id = f"test-0-8b-review-resume-{int(time.time() * 1000)}"
        now = time.time()

        # Step 1: Seed task in EXECUTING state (simulating process crash during worker execution)
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "EXECUTING",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Review Self Transition Recovery Test Task",
            "description": "Verifying REVIEW->REVIEW self transition during real process recovery",
            "payload": {"repo": "test/repo"},
            "task_spec": TaskSpec(
                task_id, "Spec description", "HIGH", "Expected output", 0.01, 100
            ).to_dict(),
            "created_at": now,
            "updated_at": now
        })

        # Step 2: Save worker result into idempotency cache (worker completed, review NOT completed)
        worker_output = "Worker output from pre-crash run"
        cached_worker_result = WorkerResult(
            opportunity_id=task_id,
            output=worker_output,
            execution_time_sec=0.3,
            actual_cost=0.0012,
            prompt_tokens=110,
            completion_tokens=55,
            model="mock-worker-model"
        )
        idempotency = IdempotencyGuard(db=self.db)
        idempotency.save_worker_result(task_id, cached_worker_result)

        # Confirm initial state: task in EXECUTING state, no review result in cache
        self.assertEqual(self.db.get_task(task_id)["state"], "EXECUTING")
        self.assertIsNone(idempotency.get_review_result(task_id))

        # Step 3: Route through run_startup_recovery()'s real worker-cache-only resume logic
        recovery_summary = run_startup_recovery(self.db)
        self.assertEqual(recovery_summary["recovered_tasks"], 1)

        # Confirm recovery set task state in DB to REVIEW
        task_after_recovery = self.db.get_task(task_id)
        self.assertEqual(task_after_recovery["state"], "REVIEW")
        self.assertIsNotNone(task_after_recovery["worker_result_json"])

        # Step 4: Run process_task() via engine to complete the review naturally
        # When process_task() executes on a task in REVIEW state, reviewer evaluation
        # calls db.execute_atomic_transition with state="REVIEW", attempting REVIEW -> REVIEW.
        engine = self._create_mock_engine()
        res = engine.process_task(task_id)

        # Step 5: Assert process_task succeeded without InvalidStateTransitionError and completed task
        self.assertEqual(res["status"], "COMPLETED")
        task_final = self.db.get_task(task_id)
        self.assertEqual(task_final["state"], "COMPLETED")
        self.assertIsNotNone(task_final["review_result_json"])

    def test_0_8b_watchdog_warning_flag_resets_on_gap_normalization(self):
        """0.8B (b): Simulate a gap that enters the 2x-5x warning band and returns to normal
        WITHOUT crossing 5x, then confirm a later 2x gap fires a NEW WATCHDOG_WARNING.
        """
        watchdog = OperationalWatchdog(db=self.db)
        base_interval = 30.0

        # Step 1: Gap in warning band (70s > 2 * 30s = 60s, < 5 * 30s = 150s)
        watchdog.check_heartbeat(now=100.0, last_heartbeat=30.0, interval_sec=base_interval)
        self.assertTrue(watchdog._in_warning, "_in_warning must be True after 70s gap")

        events_1 = [e for e in self.db.get_audit_logs(limit=100) if e["event_type"] == "WATCHDOG_WARNING"]
        self.assertEqual(len(events_1), 1, "First warning gap MUST produce exactly 1 WATCHDOG_WARNING event")

        # Step 2: Gap normalizes (30s gap: now=130.0, last_heartbeat=100.0)
        watchdog.check_heartbeat(now=130.0, last_heartbeat=100.0, interval_sec=base_interval)
        self.assertFalse(watchdog._in_warning, "_in_warning MUST reset to False when gap normalizes")

        # Step 3: Second gap in warning band (70s gap: now=200.0, last_heartbeat=130.0)
        watchdog.check_heartbeat(now=200.0, last_heartbeat=130.0, interval_sec=base_interval)
        self.assertTrue(watchdog._in_warning, "_in_warning must be True on second warning gap")

        events_2 = [e for e in self.db.get_audit_logs(limit=100) if e["event_type"] == "WATCHDOG_WARNING"]
        self.assertEqual(len(events_2), 2, "Second warning gap MUST produce a NEW WATCHDOG_WARNING event")

    # -------------------------------------------------------------------------
    # 4. 0.8C: Telemetry Category Partitioning (BLOCKED Disambiguation)
    # -------------------------------------------------------------------------
    def test_0_8c_telemetry_blocked_cause_disambiguation(self):
        """0.8C: Seed one budget-blocked and one approval-rejected task (both state='BLOCKED',
        different error_reason prefixes), assert get_telemetry_metrics() categorizes them
        separately and categories_partition_total is True.
        """
        now = time.time()

        # Seed Task 1: Budget-blocked (error_reason starts with BUDGET_BLOCKED:)
        t1_id = "proc-reg-budget-blocked"
        self.db.execute_atomic_transition({
            "task_id": t1_id,
            "opportunity_id": t1_id,
            "state": "BLOCKED",
            "error_reason": "BUDGET_BLOCKED: Exceeded daily limit $2.00",
            "title": "Budget Blocked Task",
            "created_at": now,
            "updated_at": now
        })

        # Seed Task 2: Approval-rejected (error_reason starts with APPROVAL_REJECTED:)
        t2_id = "proc-reg-approval-rejected"
        self.db.execute_atomic_transition({
            "task_id": t2_id,
            "opportunity_id": t2_id,
            "state": "BLOCKED",
            "error_reason": "APPROVAL_REJECTED: Low review confidence score",
            "title": "Approval Rejected Task",
            "created_at": now,
            "updated_at": now
        })

        metrics = self.db.get_telemetry_metrics()

        self.assertEqual(metrics["total_tasks"], 2)
        self.assertEqual(metrics["budget_blocked_executions"], 1, "Must count exactly 1 budget-blocked execution")
        self.assertEqual(metrics["approval_rejected_executions"], 1, "Must count exactly 1 approval-rejected execution")
        self.assertTrue(
            metrics["categories_partition_total"],
            "categories_partition_total MUST be True when categories sum to total_tasks"
        )

    # Note on 0.8D: Historical bypass corruption tests (OPPORTUNITY_REJECTED and TASK_BLOCKED
    # illegal continuation detection) are already defined and maintained in test_phase0_8d_verifier.py
    # (test_catch_illegal_continuation_after_opportunity_rejected and
    # test_catch_illegal_continuation_after_task_blocked). They are not duplicated here to prevent test drift.

    # -------------------------------------------------------------------------
    # 6. Reverted opportunity.py Mock-Fallback Incident
    # -------------------------------------------------------------------------
    def test_opportunity_fetcher_raises_runtime_error_on_zero_items(self):
        """Reverted opportunity.py incident: assert fetch_opportunities() raises
        RuntimeError (not a mock fallback) when all repos return zero items.
        """
        fetcher = OpportunityFetcher(max_retries=1)

        # Mock _fetch_repo_issues_with_retry to return [] for all repos (simulating API rate limit / no items)
        with patch.object(fetcher, "_fetch_repo_issues_with_retry", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                fetcher.fetch_opportunities(limit=10)

            err_msg = str(ctx.exception)
            self.assertIn("Failed to fetch live GitHub issues", err_msg)
            self.assertIn("rate limit", err_msg.lower())


if __name__ == "__main__":
    unittest.main()
