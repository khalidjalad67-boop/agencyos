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
from tools.verify_audit import verify_audit
from tools.replay_audit import verify_replay

class TestPhase08DVerifier(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "agencyos.db")
        self.log_path = os.path.join(self.temp_dir, "audit_log.jsonl")
        self._create_clean_seed_data()

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_clean_seed_data(self):
        """Creates a pristine, valid SQLite database and matching audit_log.jsonl file
        with 2 tasks progressing through legal state transitions, budget spend, and approvals.
        """
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

        db = Database(self.db_path)
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        base_ts = 1785938000.0

        events = []
        def add_event(ev_type, payload, ts):
            events.append({"event_type": ev_type, "payload": payload, "timestamp": ts})
            db.log_event(ev_type, payload)
            with db._connection() as conn:
                conn.cursor().execute("UPDATE audit_log SET timestamp = ? WHERE id = (SELECT MAX(id) FROM audit_log)", (ts,))

        # Task 1: DISCOVERED -> PLANNED -> READY -> EXECUTING -> REVIEW -> WAITING_APPROVAL -> COMPLETED
        t1_id = "proc-soak-1"
        t1_data = {
            "task_id": t1_id, "opportunity_id": t1_id, "state": "DISCOVERED",
            "source": "soak", "repo": "soak/repo", "title": "Soak Task 1", "description": "Task 1",
            "payload": {"repo": "soak/repo"}, "created_at": base_ts, "updated_at": base_ts
        }
        db.execute_atomic_transition(t1_data)
        add_event("TASK_DISCOVERED", {"task_id": t1_id, "opportunity_id": t1_id}, base_ts + 1.0)

        t1_data["state"] = "PLANNED"
        t1_data["task_spec"] = {"opportunity_id": t1_id, "title": "Soak 1", "task_type": "FEATURE", "priority": "HIGH", "estimated_cost": 0.05}
        db.execute_atomic_transition(t1_data)
        add_event("TASK_PLANNED", {"task_id": t1_id, "opportunity_id": t1_id}, base_ts + 2.0)

        t1_data["state"] = "READY"
        db.execute_atomic_transition(t1_data)
        add_event("TASK_READY", {"task_id": t1_id, "opportunity_id": t1_id}, base_ts + 3.0)

        t1_data["state"] = "EXECUTING"
        db.execute_atomic_transition(t1_data)
        add_event("TASK_EXECUTING", {"task_id": t1_id, "opportunity_id": t1_id}, base_ts + 4.0)

        t1_data["worker_result"] = {"status": "SUCCESS", "actual_cost": 0.05, "execution_time_sec": 1.0}
        db.execute_atomic_transition(t1_data)
        add_event("WORKER_EXECUTED", {"task_id": t1_id, "opportunity_id": t1_id, "worker_result": t1_data["worker_result"]}, base_ts + 5.0)

        t1_data["state"] = "REVIEW"
        t1_data["review_result"] = {"passed": True, "score": 0.95, "review_cost": 0.01}
        db.execute_atomic_transition(t1_data, spend_record=(0.06, today_str, "Spend Task 1"))
        add_event("REVIEW_COMPLETED", {"task_id": t1_id, "opportunity_id": t1_id, "review_result": t1_data["review_result"]}, base_ts + 6.0)

        t1_data["state"] = "WAITING_APPROVAL"
        db.execute_atomic_transition(t1_data)
        add_event("TASK_WAITING_APPROVAL", {"task_id": t1_id, "opportunity_id": t1_id}, base_ts + 7.0)

        t1_data["state"] = "COMPLETED"
        db.execute_atomic_transition(t1_data, approval_record=(f"appr-{t1_id}", "APPROVED", "Passed review", base_ts + 8.0))
        add_event("TASK_COMPLETED", {"task_id": t1_id, "opportunity_id": t1_id, "cost": 0.06}, base_ts + 8.0)

        # Heartbeat 1
        add_event("SCHEDULER_HEARTBEAT", {"queue_depth": 0}, base_ts + 30.0)

        # Task 2: DISCOVERED -> PLANNED -> BLOCKED
        t2_id = "proc-soak-2"
        t2_data = {
            "task_id": t2_id, "opportunity_id": t2_id, "state": "DISCOVERED",
            "source": "soak", "repo": "soak/repo", "title": "Soak Task 2", "description": "Task 2",
            "payload": {"repo": "soak/repo"}, "created_at": base_ts + 31.0, "updated_at": base_ts + 31.0
        }
        db.execute_atomic_transition(t2_data)
        add_event("TASK_DISCOVERED", {"task_id": t2_id, "opportunity_id": t2_id}, base_ts + 31.0)

        t2_data["state"] = "PLANNED"
        t2_data["task_spec"] = {"opportunity_id": t2_id, "title": "Soak 2", "task_type": "FEATURE", "priority": "HIGH", "estimated_cost": 2.50}
        db.execute_atomic_transition(t2_data)
        add_event("TASK_PLANNED", {"task_id": t2_id, "opportunity_id": t2_id}, base_ts + 32.0)

        t2_data["state"] = "BLOCKED"
        t2_data["error_reason"] = "BUDGET_BLOCKED: Exceeds daily limit"
        db.execute_atomic_transition(t2_data)
        add_event("BUDGET_BLOCKED", {"task_id": t2_id, "opportunity_id": t2_id, "reason": "Exceeds daily limit"}, base_ts + 33.0)

        # Telemetry Report
        tel_metrics = db.get_telemetry_metrics(today_str)
        add_event("TELEMETRY_REPORT", {"telemetry": tel_metrics}, base_ts + 35.0)

        # Write matching JSONL file
        with open(self.log_path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")

    def test_pass_on_clean_data(self):
        """Verifies both verify_audit and replay_audit pass with 0 errors on clean seed data."""
        v_passed, v_errors = verify_audit(self.db_path, self.log_path)
        self.assertTrue(v_passed, f"verify_audit failed on clean data: {v_errors}")

        r_passed, r_diffs = verify_replay(self.db_path, self.log_path)
        self.assertTrue(r_passed, f"replay_audit failed on clean data: {r_diffs}")

    def test_catch_db_log_count_mismatch(self):
        """Corrupts log by removing 1 line from audit_log.jsonl -> verify_audit MUST fail."""
        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(self.log_path, "w", encoding="utf-8") as f:
            f.writelines(lines[:-1])  # Delete last line

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("DB/log count mismatch" in err for err in errors), f"Expected 'DB/log count mismatch' error, got: {errors}")

    def test_catch_backwards_timestamp(self):
        """Corrupts SQLite audit_log table by setting timestamp smaller than preceding row -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE audit_log SET timestamp = 1000.0 WHERE id = (SELECT MAX(id) FROM audit_log)")
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Backwards timestamp" in err for err in errors), f"Expected 'Backwards timestamp' error, got: {errors}")

    def test_catch_duplicate_opportunity_id(self):
        """Corrupts tasks table by inserting duplicate opportunity_id without logged retries -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = OFF")
        cur.execute("CREATE TABLE tasks_temp AS SELECT * FROM tasks")
        cur.execute("DROP TABLE tasks")
        cur.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL, state TEXT NOT NULL,
                source TEXT, repo TEXT, title TEXT, description TEXT, payload_json TEXT,
                task_spec_json TEXT, worker_result_json TEXT, review_result_json TEXT,
                error_reason TEXT, created_at REAL, updated_at REAL
            )
        """)
        cur.execute("INSERT INTO tasks SELECT * FROM tasks_temp")
        cur.execute("DROP TABLE tasks_temp")
        cur.execute("""
            INSERT INTO tasks (task_id, opportunity_id, state, title, created_at, updated_at)
            VALUES ('proc-soak-1-dup', 'proc-soak-1', 'COMPLETED', 'Duplicate Task 1', 1785938000.0, 1785938000.0)
        """)
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Duplicate opportunity ID" in err for err in errors), f"Expected 'Duplicate opportunity ID' error, got: {errors}")

    def test_catch_impossible_state_transition(self):
        """Corrupts SQLite audit_log by injecting transition away from terminal state -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # Fetch last ts
        last_ts = cur.execute("SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
        ts = float(last_ts) + 1.0
        bad_payload = json.dumps({"task_id": "proc-soak-1", "opportunity_id": "proc-soak-1"})
        cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('TASK_READY', ?, ?)", (bad_payload, ts))
        conn.commit()
        conn.close()

        # Append to JSONL so line count matches
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "TASK_READY", "payload": {"task_id": "proc-soak-1", "opportunity_id": "proc-soak-1"}, "timestamp": ts}) + "\n")

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Impossible state transition" in err for err in errors), f"Expected 'Impossible state transition' error, got: {errors}")

    def test_catch_orphan_approval_row(self):
        """Corrupts approvals table by inserting orphan opportunity_id -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO approvals (id, opportunity_id, status, requested_at) VALUES ('appr-orphan', 'non-existent-opp', 'APPROVED', 1785938000.0)")
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Orphan approval row found" in err for err in errors), f"Expected 'Orphan approval row found' error, got: {errors}")

    def test_catch_orphan_budget_row(self):
        """Corrupts budget table by inserting orphan opportunity_id -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO budget (opportunity_id, amount, date_str, timestamp) VALUES ('non-existent-opp', 0.50, '2026-08-05', 1785938000.0)")
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Orphan budget row found" in err for err in errors), f"Expected 'Orphan budget row found' error, got: {errors}")

    def test_catch_budget_mismatch(self):
        """Corrupts budget table by modifying amount so SUM(budget.amount) != JSON task costs -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE budget SET amount = 99.99 WHERE opportunity_id = 'proc-soak-1'")
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Budget mismatch" in err for err in errors), f"Expected 'Budget mismatch' error, got: {errors}")

    def test_catch_telemetry_mismatch(self):
        """Corrupts TELEMETRY_REPORT payload in audit_log -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        bad_tel = json.dumps({"telemetry": {"total_tasks": 999, "successful_executions": 555}})
        cur.execute("UPDATE audit_log SET payload_json = ? WHERE event_type = 'TELEMETRY_REPORT'", (bad_tel,))
        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Telemetry mismatch" in err for err in errors), f"Expected 'Telemetry mismatch' error, got: {errors}")

    def test_catch_unexplained_heartbeat_gap(self):
        """Injects a 300s heartbeat gap into audit_log without STALL_DETECTED event -> verify_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        last_ts = cur.execute("SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
        gap_ts = float(last_ts) + 300.0  # 300s gap > 5x base_interval (150s)
        bad_payload = json.dumps({"queue_depth": 0})
        cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('SCHEDULER_HEARTBEAT', ?, ?)", (bad_payload, gap_ts))
        conn.commit()
        conn.close()

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "SCHEDULER_HEARTBEAT", "payload": {"queue_depth": 0}, "timestamp": gap_ts}) + "\n")

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Unexplained heartbeat gap" in err for err in errors), f"Expected 'Unexplained heartbeat gap' error, got: {errors}")

    def test_replay_audit_catches_state_diff(self):
        """Alters task state in SQLite database so replayed audit_log diffs against DB -> replay_audit MUST fail."""
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("UPDATE tasks SET state = 'WORKER_FAILED' WHERE task_id = 'proc-soak-1'")
        conn.commit()
        conn.close()

        passed, diffs = verify_replay(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Task state mismatch" in diff for diff in diffs), f"Expected 'Task state mismatch' diff, got: {diffs}")

    def test_catch_illegal_continuation_after_opportunity_rejected(self):
        """Historical Bypass Test 1: Seed task through OPPORTUNITY_REJECTED to QUALITY_REJECTED (terminal state),
        then inject an illegal continuation event (TASK_PLANNED) -> verify_audit MUST fail.
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        last_ts = cur.execute("SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
        ts1 = float(last_ts) + 1.0
        ts2 = float(last_ts) + 2.0

        # Seed DISCOVERED -> OPPORTUNITY_REJECTED (QUALITY_REJECTED)
        t_id = "proc-hist-qual"
        cur.execute("""
            INSERT INTO tasks (task_id, opportunity_id, state, error_reason, title, created_at, updated_at)
            VALUES (?, ?, 'QUALITY_REJECTED', 'QUALITY_REJECTED: Description too short', 'Hist Qual Task', ?, ?)
        """, (t_id, t_id, ts1, ts1))

        ev1 = json.dumps({"task_id": t_id, "opportunity_id": t_id})
        ev2 = json.dumps({"task_id": t_id, "opportunity_id": t_id, "reason": "Description too short"})
        cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('TASK_DISCOVERED', ?, ?)", (ev1, ts1))
        cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('OPPORTUNITY_REJECTED', ?, ?)", (ev2, ts1 + 0.1))

        # Inject illegal continuation event (attempting transition QUALITY_REJECTED -> PLANNED)
        ev_illegal = json.dumps({"task_id": t_id, "opportunity_id": t_id})
        cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('TASK_PLANNED', ?, ?)", (ev_illegal, ts2))
        conn.commit()
        conn.close()

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "TASK_DISCOVERED", "payload": {"task_id": t_id, "opportunity_id": t_id}, "timestamp": ts1}) + "\n")
            f.write(json.dumps({"event_type": "OPPORTUNITY_REJECTED", "payload": {"task_id": t_id, "opportunity_id": t_id, "reason": "Description too short"}, "timestamp": ts1 + 0.1}) + "\n")
            f.write(json.dumps({"event_type": "TASK_PLANNED", "payload": {"task_id": t_id, "opportunity_id": t_id}, "timestamp": ts2}) + "\n")

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Impossible state transition for task 'proc-hist-qual'" in err and "QUALITY_REJECTED" in err for err in errors), f"Expected 'Impossible state transition for QUALITY_REJECTED' error, got: {errors}")

    def test_catch_illegal_continuation_after_task_blocked(self):
        """Historical Bypass Test 2: Seed task through TASK_BLOCKED to BLOCKED (terminal state)
        via the WAITING_APPROVAL -> BLOCKED approval-rejection path, then inject an illegal
        continuation event (TASK_PLANNED) -> verify_audit MUST fail.
        """
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        last_ts = cur.execute("SELECT MAX(timestamp) FROM audit_log").fetchone()[0]
        ts_base = float(last_ts) + 1.0

        t_id = "proc-hist-block"
        cur.execute("""
            INSERT INTO tasks (task_id, opportunity_id, state, error_reason, title, created_at, updated_at)
            VALUES (?, ?, 'BLOCKED', 'APPROVAL_REJECTED: Below threshold', 'Hist Block Task', ?, ?)
        """, (t_id, t_id, ts_base, ts_base + 0.7))

        events = [
            ("SCHEDULER_TASK_DISCOVERED", {"task_id": t_id, "opportunity_id": t_id, "title": "Hist Block Task", "source": "github_issues:agencyos"}, ts_base),
            ("TASK_PLANNED", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.1),
            ("TASK_READY", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.2),
            ("TASK_EXECUTING", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.3),
            ("WORKER_EXECUTED", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.4),
            ("REVIEW_COMPLETED", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.5),
            ("TASK_WAITING_APPROVAL", {"task_id": t_id, "opportunity_id": t_id}, ts_base + 0.6),
            ("TASK_BLOCKED", {"task_id": t_id, "opportunity_id": t_id, "reason": "Approval rejected"}, ts_base + 0.7),
            ("TASK_PLANNED", {"task_id": t_id, "opportunity_id": t_id, "repo": "agencyos"}, ts_base + 0.8) # Illegal transition away from BLOCKED
        ]

        with open(self.log_path, "a", encoding="utf-8") as f:
            for ev_type, payload, ts in events:
                cur.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES (?, ?, ?)", (ev_type, json.dumps(payload), ts))
                f.write(json.dumps({"event_type": ev_type, "payload": payload, "timestamp": ts}) + "\n")

        conn.commit()
        conn.close()

        passed, errors = verify_audit(self.db_path, self.log_path)
        self.assertFalse(passed)
        self.assertTrue(any("Impossible state transition for task 'proc-hist-block'" in err and "BLOCKED" in err for err in errors), f"Expected 'Impossible state transition for BLOCKED' error, got: {errors}")

    def test_scheduler_idle_backoff_heartbeat_intervals_no_spurious_stall(self):
        """Item 4 Test: Simulates legitimate scheduler idle backoff across multiple ticks (30s -> 60s -> 120s -> 240s)
        and asserts NO spurious WATCHDOG_WARNING or STALL_DETECTED events fire for gaps matching backed-off intervals.
        """
        from unittest.mock import patch
        from src.scheduler import Scheduler
        from src.watchdog import OperationalWatchdog
        from src.logger import AuditLogger

        db_path = os.path.join(self.temp_dir, "backoff_test.db")
        log_path = os.path.join(self.temp_dir, "backoff_test.jsonl")
        db = Database(db_path)
        logger = AuditLogger(log_path, db=db)
        watchdog = OperationalWatchdog(db=db)

        class DummyFetcher:
            def fetch_opportunities(self, limit=10):
                return []

        scheduler = Scheduler(fetcher=DummyFetcher(), db=db, watchdog=watchdog, logger=logger, interval_sec=30.0, max_interval_sec=300.0)

        # Simulate 4 consecutive idle ticks with backing off gaps matching expected_interval
        simulated_time = 1785940000.0
        scheduler.last_tick_time = simulated_time

        with patch("time.time") as mock_time:
            # Tick 1: 30s gap (interval 30s)
            simulated_time += 30.0
            mock_time.return_value = simulated_time
            scheduler.tick()

            # Tick 2: 60s gap (backed-off interval 60s)
            simulated_time += 60.0
            mock_time.return_value = simulated_time
            scheduler.tick()

            # Tick 3: 120s gap (backed-off interval 120s)
            simulated_time += 120.0
            mock_time.return_value = simulated_time
            scheduler.tick()

            # Tick 4: 240s gap (backed-off interval 240s)
            simulated_time += 240.0
            mock_time.return_value = simulated_time
            scheduler.tick()

        # Check SQLite audit_log for WATCHDOG_WARNING or STALL_DETECTED events
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        warnings = cur.execute("SELECT COUNT(*) FROM audit_log WHERE event_type = 'WATCHDOG_WARNING'").fetchone()[0]
        stalls = cur.execute("SELECT COUNT(*) FROM audit_log WHERE event_type = 'STALL_DETECTED'").fetchone()[0]
        conn.close()

        self.assertEqual(warnings, 0, f"Expected 0 WATCHDOG_WARNING events during legitimate backoff, got {warnings}")
        self.assertEqual(stalls, 0, f"Expected 0 STALL_DETECTED events during legitimate backoff, got {stalls}")

        # Run verify_audit on resulting DB & log
        passed, errors = verify_audit(db_path, log_path)
        self.assertTrue(passed, f"verify_audit failed on legitimate backoff sequence: {errors}")

    def test_heartbeat_backoff_and_stall_verification(self):
        """Verifies that a legitimate backoff sequence (30->60->120->240) is NOT flagged by verify_audit,
        while a subsequent genuine stall gap without backoff justification IS caught.
        """
        db_path = os.path.join(self.temp_dir, "hb_stall_test.db")
        log_path = os.path.join(self.temp_dir, "hb_stall_test.jsonl")
        db = Database(db_path, log_filepath=log_path)
        
        base_ts = 1785950000.0
        # Legitimate backoff sequence
        heartbeats = [
            (base_ts, 30.0),
            (base_ts + 30.0, 60.0),
            (base_ts + 90.0, 120.0),
            (base_ts + 210.0, 240.0),
            (base_ts + 450.0, 240.0)
        ]
        
        for ts, expected in heartbeats:
            payload = {"queue_depth": 0, "expected_interval": expected}
            payload_json = json.dumps(payload)
            with db._connection() as conn:
                conn.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('SCHEDULER_HEARTBEAT', ?, ?)", (payload_json, ts))
                conn.commit()
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"event_type": "SCHEDULER_HEARTBEAT", "payload": payload, "timestamp": ts}) + "\n")

        # 1. Assert legitimate backoff sequence passes verify_audit cleanly
        passed, errors = verify_audit(db_path, log_path)
        self.assertTrue(passed, f"Legitimate backoff sequence was falsely flagged: {errors}")

        # 2. Inject a genuine stall gap: 1500s gap (> 5x expected_interval 240s = 1200s) without STALL_DETECTED
        stall_ts = base_ts + 450.0 + 1500.0
        payload_stall = {"queue_depth": 0, "expected_interval": 240.0}
        with db._connection() as conn:
            conn.execute("INSERT INTO audit_log (event_type, payload_json, timestamp) VALUES ('SCHEDULER_HEARTBEAT', ?, ?)", (json.dumps(payload_stall), stall_ts))
            conn.commit()
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": "SCHEDULER_HEARTBEAT", "payload": payload_stall, "timestamp": stall_ts}) + "\n")

        # 3. Assert genuine stall gap IS caught after fix
        passed_after_stall, errors_after_stall = verify_audit(db_path, log_path)
        self.assertFalse(passed_after_stall, "Genuine stall gap was not caught by verify_audit")
        self.assertTrue(any("Unexplained heartbeat gap" in err and "without STALL_DETECTED event" in err for err in errors_after_stall),
                        f"Expected unexplained heartbeat gap error, got: {errors_after_stall}")

if __name__ == "__main__":
    unittest.main()
