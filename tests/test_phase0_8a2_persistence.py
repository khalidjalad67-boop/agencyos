import os
import sys
import time
import json
import sqlite3
import shutil
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import Database, resolve_db_path, get_project_root
from src.opportunity import Opportunity
from src.scheduler import Scheduler
from src.engine import AutonomousEngine
from src.planner import TaskSpec
from src.worker import Worker, WorkerResult
from src.reviewer import Reviewer, ReviewResult
from src.approval import ApprovalGate, ApprovalDecision
from src.idempotency import IdempotencyGuard
from src.budget_guard import BudgetGuard

class TestPhase08A2Persistence(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = os.path.join(self.temp_dir, "test_0_8a2.db")
        self.test_log_path = os.path.join(self.temp_dir, "test_0_8a2_audit.jsonl")
        self.db = Database(self.test_db_path, self.test_log_path)

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_mock_engine(self) -> AutonomousEngine:
        """Helper to create AutonomousEngine with deterministic mock worker/reviewer."""
        engine = AutonomousEngine(self.db, log_filepath=self.test_log_path)
        
        mock_worker = Worker()
        mock_worker.execute = lambda tspec: WorkerResult(
            opportunity_id=tspec.opportunity_id,
            output="Mock worker output for live persistence test.",
            execution_time_sec=0.1,
            actual_cost=0.0012,
            prompt_tokens=150,
            completion_tokens=75,
            model="mock-worker-model"
        )
        
        mock_reviewer = Reviewer()
        mock_reviewer.review = lambda tspec, wres: ReviewResult(
            opportunity_id=tspec.opportunity_id,
            passed=True,
            score=0.96,
            feedback="Mock review feedback: PASSED.",
            review_cost=0.0005,
            review_tokens=40
        )
        
        mock_approval = ApprovalGate(decision_provider=lambda opp, spec, wres, rres: ApprovalDecision(opportunity_id=opp.id, approved=True, comments="Auto-approved for persistence test."), db=self.db)
        
        engine.worker = mock_worker
        engine.reviewer = mock_reviewer
        engine.approval_gate = mock_approval
        return engine

    def test_live_engine_db_table_population(self):
        """Verifies that executing process_task populates tasks, audit_log, idempotency_keys, approvals, and budget tables in SQLite."""
        task_id = "live-opp-100"
        
        # 1. Seed DISCOVERED task record
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "DISCOVERED",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Live persistence verification task",
            "description": "Comprehensive description for testing SQLite table population.",
            "payload": {"repo": "test/repo", "labels": []}
        })
        
        engine = self._create_mock_engine()
        result = engine.process_task(task_id)
        
        self.assertEqual(result["status"], "COMPLETED")
        
        conn = sqlite3.connect(self.test_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Verify tasks table
        task_row = cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        self.assertIsNotNone(task_row)
        self.assertEqual(task_row["state"], "COMPLETED")
        self.assertIsNotNone(task_row["task_spec_json"])
        self.assertIsNotNone(task_row["worker_result_json"])
        self.assertIsNotNone(task_row["review_result_json"])
        
        # Verify idempotency_keys table
        worker_key = f"WORKER:{task_id}:v1"
        review_key = f"REVIEW:{task_id}:v1"
        w_row = cursor.execute("SELECT * FROM idempotency_keys WHERE key = ?", (worker_key,)).fetchone()
        r_row = cursor.execute("SELECT * FROM idempotency_keys WHERE key = ?", (review_key,)).fetchone()
        self.assertIsNotNone(w_row, "Worker result must be stored in idempotency_keys table")
        self.assertIsNotNone(r_row, "Review result must be stored in idempotency_keys table")
        
        # Verify budget table
        budget_rows = cursor.execute("SELECT * FROM budget WHERE opportunity_id = ?", (task_id,)).fetchall()
        self.assertEqual(len(budget_rows), 1)
        expected_cost = round(0.0012 + 0.0005, 6)
        self.assertAlmostEqual(budget_rows[0]["amount"], expected_cost, places=6)
        
        # Verify approvals table
        approval_rows = cursor.execute("SELECT * FROM approvals WHERE opportunity_id = ?", (task_id,)).fetchall()
        self.assertEqual(len(approval_rows), 1)
        self.assertEqual(approval_rows[0]["status"], "APPROVED")
        
        # Verify audit_log table
        audit_logs = cursor.execute("SELECT * FROM audit_log").fetchall()
        self.assertGreater(len(audit_logs), 0)
        event_types = [r["event_type"] for r in audit_logs]
        self.assertIn("TASK_PLANNED", event_types)
        self.assertIn("TASK_COMPLETED", event_types)
        
        conn.close()

    def test_live_engine_audit_log_jsonl_matches_sqlite(self):
        """Verifies that running a real engine.process_task() call end-to-end plus direct AuditLogger.log_event calls results in audit_log.jsonl line count matching SQLite audit_log row count."""
        task_id = "live-opp-jsonl-sync"
        
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "DISCOVERED",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Live JSONL audit sync verification task",
            "description": "Verification that JSONL lines match SQLite audit rows.",
            "payload": {"repo": "test/repo", "labels": []}
        })
        
        engine = self._create_mock_engine()
        engine.logger.log_event("SCHEDULER_HEARTBEAT", {"queue_depth": 1, "expected_interval": 30.0})
        result = engine.process_task(task_id)
        engine.logger.log_event("SCHEDULER_HEARTBEAT", {"queue_depth": 0, "expected_interval": 60.0})
        
        self.assertEqual(result["status"], "COMPLETED")
        
        conn = sqlite3.connect(self.test_db_path)
        cursor = conn.cursor()
        sqlite_count = cursor.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()
        
        self.assertTrue(os.path.exists(self.test_log_path))
        with open(self.test_log_path, "r", encoding="utf-8") as f:
            jsonl_lines = [line for line in f if line.strip()]
        
        self.assertGreater(sqlite_count, 0)
        self.assertEqual(len(jsonl_lines), sqlite_count, f"JSONL line count ({len(jsonl_lines)}) must match SQLite audit_log row count ({sqlite_count})")

    def test_audit_logger_direct_log_event_single_jsonl_write(self):
        """Verifies that calling AuditLogger.log_event directly writes exactly ONE line to audit_log.jsonl and ONE row to SQLite audit_log."""
        from src.logger import AuditLogger
        logger = AuditLogger(log_filepath=self.test_log_path, db=self.db)
        logger.log_event("SCHEDULER_HEARTBEAT", {"queue_depth": 0, "expected_interval": 30.0})

        conn = sqlite3.connect(self.test_db_path)
        sqlite_count = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
        conn.close()

        with open(self.test_log_path, "r", encoding="utf-8") as f:
            jsonl_lines = [line for line in f if line.strip()]

        self.assertEqual(sqlite_count, 1, f"Expected 1 SQLite audit row, got {sqlite_count}")
        self.assertEqual(len(jsonl_lines), 1, f"Expected 1 JSONL line for direct AuditLogger call, got {len(jsonl_lines)}")

    def test_log_filepath_resolution_in_test_mode(self):
        """Verifies that relative log_filepath is deterministically resolved to tests/.temp_test_dbs under test mode."""
        from src.db import resolve_log_path
        resolved = resolve_log_path("audit_log.jsonl")
        expected_suffix = os.path.join("tests", ".temp_test_dbs", "audit_log.jsonl")
        self.assertTrue(resolved.endswith(expected_suffix), f"Resolved path {resolved} must end with {expected_suffix}")

        db = Database("rel_test.db", log_filepath="rel_audit.jsonl")
        self.assertTrue(db.log_filepath.endswith(os.path.join("tests", ".temp_test_dbs", "rel_audit.jsonl")),
                        f"Database log_filepath {db.log_filepath} must be isolated in test mode temp directory")

    def test_restart_idempotency_prevents_reexecution_and_rebilling(self):
        """Verifies that re-running process_task on a completed task post-restart hits terminal state guard, avoiding re-execution & duplicate billing."""
        task_id = "restart-opp-200"
        
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "DISCOVERED",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Restart idempotency task",
            "description": "Verifying restart idempotency prevents re-execution.",
            "payload": {"repo": "test/repo", "labels": []}
        })
        
        engine1 = self._create_mock_engine()
        res1 = engine1.process_task(task_id)
        self.assertEqual(res1["status"], "COMPLETED")
        
        initial_spend = self.db.get_today_spend(date_str=time.strftime("%Y-%m-%d", time.gmtime()))
        self.assertGreater(initial_spend, 0)
        
        # Simulate engine restart by creating a new AutonomousEngine instance with fresh mock worker that raises if called
        engine2 = AutonomousEngine(self.db, log_filepath=self.test_log_path)
        
        def _failing_worker(spec):
            self.fail("Worker MUST NOT be called for a completed task!")
        
        engine2.worker.execute = _failing_worker
        
        # Process completed task again
        res2 = engine2.process_task(task_id)
        self.assertEqual(res2["status"], "COMPLETED")
        self.assertEqual(res2["reason"], "Task is in terminal state")
        
        # Verify today spend did not increase
        post_restart_spend = self.db.get_today_spend(date_str=time.strftime("%Y-%m-%d", time.gmtime()))
        self.assertEqual(initial_spend, post_restart_spend)

    def test_scheduler_tick_filters_terminal_and_active_states(self):
        """Verifies Scheduler.tick() explicitly skips opportunities whose tasks exist in terminal or active states."""
        scheduler = Scheduler(db=self.db)
        now = time.time()
        
        # Seed 4 tasks in terminal states and 1 in active EXECUTING state
        terminal_tasks = [
            ("opp-c", "opp-c", "COMPLETED"),
            ("opp-b", "opp-b", "BLOCKED"),
            ("opp-q", "opp-q", "QUALITY_REJECTED"),
            ("opp-w", "opp-w", "WORKER_FAILED"),
            ("opp-e", "opp-e", "EXECUTING")
        ]
        
        for task_id, opp_id, state in terminal_tasks:
            self.db.execute_atomic_transition({
                "task_id": task_id,
                "opportunity_id": opp_id,
                "state": state,
                "created_at": now,
                "updated_at": now
            })
            
        # Overrides containing the same opportunities
        opp_overrides = [
            Opportunity(id="opp-c", title="T Completed", description="Desc", source="src", payload={}),
            Opportunity(id="opp-b", title="T Blocked", description="Desc", source="src", payload={}),
            Opportunity(id="opp-q", title="T Qual Rejected", description="Desc", source="src", payload={}),
            Opportunity(id="opp-w", title="T Worker Failed", description="Desc", source="src", payload={}),
            Opportunity(id="opp-e", title="T Executing", description="Desc", source="src", payload={}),
            Opportunity(id="opp-new", title="T Brand New", description="Desc", source="src", payload={})
        ]
        
        scheduled, _ = scheduler.tick(opportunities_override=opp_overrides)
        
        # Only the brand new opportunity should be scheduled
        self.assertEqual(scheduled, ["opp-new"])
        
        # Confirm terminal and executing task states were untouched
        for task_id, opp_id, orig_state in terminal_tasks:
            t = self.db.get_task(task_id)
            self.assertEqual(t["state"], orig_state, f"State for {task_id} should remain {orig_state}")

    def test_save_approval_and_record_spend_reject_orphan_insertion(self):
        """Verifies save_approval and record_spend raise IntegrityError when opportunity_id does not exist in tasks table."""
        # 1. Attempt save_approval without parent task
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.save_approval("appr-orphan-99", "non-existent-opp-99", "APPROVED", "Orphan test")
            
        # 2. Attempt record_spend without parent task
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.record_spend("non-existent-opp-99", 0.05, "2026-08-05", "Orphan spend test")

    def test_opportunity_4335223464_cannot_reexecute_after_quality_rejection(self):
        """Replays historical failure for opportunity 4335223464 (quality-rejected at audit line 356).
        Confirms Scheduler.tick and AutonomousEngine.process_task bypass re-execution and leave QUALITY_REJECTED state untouched.
        """
        opp_id = "4335223464"
        now = time.time()
        
        # Seed task in QUALITY_REJECTED state
        self.db.execute_atomic_transition({
            "task_id": opp_id,
            "opportunity_id": opp_id,
            "state": "QUALITY_REJECTED",
            "source": "github_issues",
            "repo": "psf/requests",
            "title": "documentation for requests.session.request(verify=...) duplicated",
            "description": "Historical quality rejected issue",
            "payload": {"repo": "psf/requests"},
            "error_reason": "QUALITY_REJECTED: Quality score 0.35 below threshold 0.70",
            "created_at": now,
            "updated_at": now
        })
        
        # 1. Scheduler tick with override containing 4335223464
        scheduler = Scheduler(db=self.db)
        opp_override = Opportunity(id=opp_id, title="Documentation issue", description="Desc", source="github_issues", payload={"repo": "psf/requests"})
        scheduled, _ = scheduler.tick(opportunities_override=[opp_override])
        
        self.assertEqual(scheduled, [], "Scheduler MUST NOT schedule a QUALITY_REJECTED task!")
        
        # 2. Engine process_task on 4335223464
        engine = AutonomousEngine(self.db, log_filepath=self.test_log_path)
        
        # Mock planner to fail if called
        def _failing_planner(opp):
            self.fail("Planner MUST NOT be called for a quality-rejected task!")
        engine.planner.plan = _failing_planner
        
        res = engine.process_task(opp_id)
        
        self.assertEqual(res["status"], "QUALITY_REJECTED")
        self.assertEqual(res["reason"], "Task is in terminal state")
        
        # Verify task state in SQLite remains QUALITY_REJECTED
        t = self.db.get_task(opp_id)
        self.assertEqual(t["state"], "QUALITY_REJECTED")

    def test_opportunity_4844615862_cannot_duplicate_after_completion(self):
        """Replays historical failure for opportunity 4844615862 (completed, then executed 2x more post-blackout).
        Confirms Scheduler.tick and AutonomousEngine.process_task skip completion, avoiding duplicate worker executions and rebilling.
        """
        opp_id = "4844615862"
        now = time.time()
        
        # Seed task in COMPLETED state
        self.db.execute_atomic_transition({
            "task_id": opp_id,
            "opportunity_id": opp_id,
            "state": "COMPLETED",
            "source": "github_issues",
            "repo": "psf/requests",
            "title": "Support for HTTP Query Method",
            "description": "Historical completed issue",
            "payload": {"repo": "psf/requests"},
            "created_at": now,
            "updated_at": now
        })
        
        # 1. Scheduler tick with override containing 4844615862
        scheduler = Scheduler(db=self.db)
        opp_override = Opportunity(id=opp_id, title="HTTP Query", description="Desc", source="github_issues", payload={"repo": "psf/requests"})
        scheduled, _ = scheduler.tick(opportunities_override=[opp_override])
        
        self.assertEqual(scheduled, [], "Scheduler MUST NOT schedule a COMPLETED task!")
        
        # 2. Engine process_task on 4844615862
        engine = AutonomousEngine(self.db, log_filepath=self.test_log_path)
        
        # Mock worker to fail if called
        def _failing_worker(tspec):
            self.fail("Worker MUST NOT execute for an already COMPLETED task!")
        engine.worker.execute = _failing_worker
        
        res = engine.process_task(opp_id)
        
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(res["reason"], "Task is in terminal state")
        
        # Verify SQLite task state remains COMPLETED
        t = self.db.get_task(opp_id)
        self.assertEqual(t["state"], "COMPLETED")

    def test_atomic_transaction_rollback_on_failure(self):
        """Verifies that if an error occurs mid-transition, Database.execute_atomic_transition rolls back 100% of writes (task, audit_log, budget, approvals)."""
        task_id = "rollback-opp-300"
        now = time.time()
        
        task_data = {
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "DISCOVERED",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Rollback test task",
            "description": "Verifying transaction rollback",
            "payload": {}
        }
        
        # 1. Save valid primary task and a secondary task
        self.db.execute_atomic_transition(task_data)
        
        other_task = dict(task_data)
        other_task["task_id"] = "existing-opp-400"
        other_task["opportunity_id"] = "existing-opp-400"
        self.db.execute_atomic_transition(other_task)

        # 2. Attempt atomic transition to PLANNED for primary task, but with opportunity_id colliding with existing-opp-400 (violating UNIQUE constraint)
        task_data["state"] = "PLANNED"
        task_data["opportunity_id"] = "existing-opp-400"
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute_atomic_transition(
                task_data,
                audit_event=("REVIEW_COMPLETED", {"task_id": task_id}),
                spend_record=(0.05, "2026-08-05", "Spend"),
                approval_record=("appr-rollback-opp-300", "APPROVED", "Approved", now)
            )
            
        # 3. Verify task state remained DISCOVERED in SQLite, zero audit logs added, zero spend recorded
        t = self.db.get_task(task_id)
        self.assertEqual(t["state"], "DISCOVERED", "Task state MUST NOT update if transaction fails!")
        
        conn = sqlite3.connect(self.test_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        audit_rows = cursor.execute("SELECT * FROM audit_log WHERE payload_json LIKE ?", (f"%{task_id}%",)).fetchall()
        self.assertEqual(len(audit_rows), 0, "No audit events MUST be saved if transaction fails!")
        
        conn.close()

    def test_scheduler_task_discovery_atomic_rollback(self):
        """Proves SQLite rolls back a real mid-transaction write in the scheduler discovery path.

        Mechanism: the real transaction() context manager runs (BEGIN IMMEDIATE).
        The task INSERT executes as call #1 on the cursor (real SQL, real write inside
        the open transaction). The audit_log INSERT (call #2) raises RuntimeError before
        it reaches SQLite. transaction()'s except-block calls conn.rollback(), undoing
        the task INSERT. We assert the task row does NOT exist afterward -- proving SQLite
        undid a write it had already issued, not that a stub never touched the DB.
        """
        import sqlite3 as _sqlite3
        from contextlib import contextmanager

        scheduler = Scheduler(db=self.db)
        opp_id = "disc-rollback-400"
        opp = Opportunity(id=opp_id, title="Disc Rollback", description="Desc", source="src", payload={})

        # Build a cursor subclass that counts execute() calls and raises on call >= raise_on
        crash_on_call = 2  # task INSERT is call 1, audit_log INSERT is call 2

        class CrashOnNthCursor(_sqlite3.Cursor):
            def __init__(self, connection):
                super().__init__(connection)
                self._call_count = 0

            def execute(self, sql, params=()):
                self._call_count += 1
                if self._call_count >= crash_on_call:
                    raise RuntimeError(
                        f"Simulated crash on cursor.execute() call #{self._call_count}: "
                        "task INSERT already issued, audit_log INSERT intercepted"
                    )
                return super().execute(sql, params)

        class CrashingConnection(_sqlite3.Connection):
            def cursor(self, factory=_sqlite3.Cursor):
                return CrashOnNthCursor(self)

        orig_transaction = self.db.transaction

        @contextmanager
        def _patched_transaction():
            conn = CrashingConnection(self.db.db_path, timeout=30.0)
            conn.row_factory = _sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        self.db.transaction = _patched_transaction

        with self.assertRaises(RuntimeError):
            scheduler.tick(opportunities_override=[opp])

        self.db.transaction = orig_transaction

        # task INSERT executed inside BEGIN IMMEDIATE but transaction() rolled it back.
        # The row must not exist, proving SQLite undid a real already-issued write.
        self.assertIsNone(
            self.db.get_task(opp_id),
            "task INSERT was issued inside BEGIN IMMEDIATE but MUST be rolled back "
            "when the audit_log INSERT raises -- proving real SQLite rollback!"
        )

        conn = _sqlite3.connect(self.test_db_path)
        conn.row_factory = _sqlite3.Row
        cursor = conn.cursor()
        audit_rows = cursor.execute(
            "SELECT * FROM audit_log WHERE payload_json LIKE ?", (f"%{opp_id}%",)
        ).fetchall()
        self.assertEqual(
            len(audit_rows), 0,
            "Audit event MUST NOT exist after rollback of the scheduler discovery transaction!"
        )
        conn.close()

    def test_worker_execution_success_atomic_rollback(self):
        """Proves SQLite rolls back a real mid-transaction write in the worker execution path.

        Mechanism: task is pre-seeded in READY state. When process_task() reaches the
        WORKER_EXECUTED transition, the real transaction() runs (BEGIN IMMEDIATE).
        The tasks UPSERT (writing worker_result + updated state) executes as call #1 on
        the cursor (real SQL, real write inside the open transaction). The audit_log INSERT
        (call #2) raises RuntimeError before it reaches SQLite. transaction()'s
        except-block calls conn.rollback(), undoing the UPSERT. We assert
        worker_result_json is still NULL -- proving SQLite undid a real already-issued write.
        """
        import sqlite3 as _sqlite3
        from contextlib import contextmanager

        task_id = "worker-rollback-500"
        now = time.time()

        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "READY",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Worker Rollback Task",
            "description": "Verifying worker execution atomic rollback",
            "payload": {"repo": "test/repo"},
            "task_spec": TaskSpec(task_id, "Worker rollback task spec", "HIGH", "Expected output", 0.01, 100).to_dict(),
            "created_at": now,
            "updated_at": now
        })

        engine = self._create_mock_engine()

        # The WORKER_EXECUTED transition calls execute_atomic_transition with audit_event set.
        # Inside that call, execute_atomic_transition does:
        #   cursor.execute() call #1 -> tasks UPSERT  (real write)
        #   cursor.execute() call #2 -> audit_log INSERT  (we raise here)
        # We count per-transaction, so transitions without audit_event (1 execute call each)
        # complete normally; only the WORKER_EXECUTED transition (2 calls) hits the raise.
        crash_on_call = 2
        worker_upsert_was_seen = [False]

        class CrashOnNthCursor(_sqlite3.Cursor):
            def __init__(self, connection):
                super().__init__(connection)
                self._call_count = 0

            def execute(self, sql, params=()):
                self._call_count += 1
                if self._call_count == 1 and "tasks" in sql:
                    worker_upsert_was_seen[0] = True
                if self._call_count >= crash_on_call and "audit_log" in sql:
                    raise RuntimeError(
                        f"Simulated crash on cursor.execute() call #{self._call_count}: "
                        "tasks UPSERT already issued, audit_log INSERT intercepted"
                    )
                return super().execute(sql, params)

        class CrashingConnection(_sqlite3.Connection):
            def cursor(self, factory=_sqlite3.Cursor):
                return CrashOnNthCursor(self)

        orig_transaction = self.db.transaction

        @contextmanager
        def _patched_transaction():
            conn = CrashingConnection(self.db.db_path, timeout=30.0)
            conn.row_factory = _sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            try:
                conn.execute("BEGIN IMMEDIATE;")
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

        self.db.transaction = _patched_transaction

        with self.assertRaises(RuntimeError):
            engine.process_task(task_id)

        self.db.transaction = orig_transaction

        # Confirm our crash cursor intercepted the WORKER_EXECUTED audit_log INSERT
        self.assertTrue(
            worker_upsert_was_seen[0],
            "CrashOnNthCursor must have seen the tasks UPSERT (call #1) before crashing"
        )

        # tasks UPSERT executed inside BEGIN IMMEDIATE but the transaction was rolled back.
        # worker_result_json must still be NULL (READY state preserved).
        t = self.db.get_task(task_id)
        self.assertIsNone(
            t["worker_result_json"],
            "worker_result UPSERT was issued inside BEGIN IMMEDIATE but MUST be rolled back "
            "when the audit_log INSERT raises -- proving real SQLite rollback!"
        )

        conn = _sqlite3.connect(self.test_db_path)
        conn.row_factory = _sqlite3.Row
        cursor = conn.cursor()
        audit_rows = cursor.execute(
            "SELECT * FROM audit_log WHERE event_type = 'WORKER_EXECUTED' AND payload_json LIKE ?",
            (f"%{task_id}%",)
        ).fetchall()
        self.assertEqual(
            len(audit_rows), 0,
            "WORKER_EXECUTED audit row MUST NOT exist after rollback!"
        )
        conn.close()

    def test_worker_idempotency_cache_hit_populates_worker_result_json(self):
        """Regression test for the crash-window bug: worker_result_json must NOT be NULL
        when a task recovers via the idempotency cache-hit path.

        Scenario (exact crash window this covers):
          1. Worker executes successfully.
          2. idempotency.save_worker_result() commits to idempotency_keys.
          3. Process crashes BEFORE execute_atomic_transition("WORKER_EXECUTED") commits,
             so the tasks row still has worker_result_json = NULL and state = EXECUTING.
          4. On restart, process_task() finds the cached worker result in idempotency_keys
             (cache-hit branch) and must write task["worker_result"] before the subsequent
             execute_atomic_transition() calls — otherwise the task reaches COMPLETED with
             worker_result_json = NULL permanently.

        This test seeds that exact state and asserts the final COMPLETED row has a
        non-NULL worker_result_json containing the correct output.
        """
        task_id = "idem-crash-window-600"
        now = time.time()

        # Step 1: Seed task in EXECUTING state with worker_result_json = NULL
        # (simulates crash after save_worker_result, before WORKER_EXECUTED commit)
        self.db.execute_atomic_transition({
            "task_id": task_id,
            "opportunity_id": task_id,
            "state": "EXECUTING",
            "source": "github_issues",
            "repo": "test/repo",
            "title": "Idempotency Cache Hit Worker Result Test",
            "description": "Verifying worker_result_json is populated on cache-hit recovery",
            "payload": {"repo": "test/repo"},
            "task_spec": TaskSpec(
                task_id, "Cache-hit recovery task spec", "HIGH", "Expected output", 0.01, 100
            ).to_dict(),
            # worker_result intentionally absent — simulates crash before WORKER_EXECUTED committed
            "created_at": now,
            "updated_at": now
        })

        # Step 2: Pre-seed idempotency_keys with the worker result that "completed before the crash"
        expected_output = "Cached worker output from pre-crash execution."
        expected_cost = 0.0014
        cached_result = WorkerResult(
            opportunity_id=task_id,
            output=expected_output,
            execution_time_sec=0.3,
            actual_cost=expected_cost,
            prompt_tokens=120,
            completion_tokens=60,
            model="mock-model"
        )
        idempotency = IdempotencyGuard(db=self.db)
        idempotency.save_worker_result(task_id, cached_result)

        # Confirm the task row starts with worker_result_json = NULL
        t_before = self.db.get_task(task_id)
        self.assertIsNone(
            t_before["worker_result_json"],
            "Pre-condition: worker_result_json must be NULL before recovery run"
        )

        # Step 3: Run process_task() — this should hit the idempotency cache, set
        # task["worker_result"], and proceed to COMPLETED with a non-NULL tasks row
        engine = self._create_mock_engine()
        result = engine.process_task(task_id)

        self.assertEqual(
            result["status"], "COMPLETED",
            f"process_task() must reach COMPLETED on cache-hit recovery, got: {result}"
        )

        # Step 4: Assert the final tasks row has worker_result_json populated
        conn = sqlite3.connect(self.test_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        row = cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        self.assertIsNotNone(row, "Task row must exist after process_task()")
        self.assertEqual(row["state"], "COMPLETED")

        self.assertIsNotNone(
            row["worker_result_json"],
            "worker_result_json MUST NOT be NULL on the COMPLETED row when recovering "
            "via the idempotency cache-hit path!"
        )

        worker_result_data = json.loads(row["worker_result_json"])
        self.assertEqual(
            worker_result_data.get("output"), expected_output,
            "worker_result_json must contain the correct cached output"
        )
        self.assertAlmostEqual(
            worker_result_data.get("actual_cost", 0.0), expected_cost, places=6,
            msg="worker_result_json must contain the correct cached cost"
        )
        conn.close()

if __name__ == "__main__":
    unittest.main()
