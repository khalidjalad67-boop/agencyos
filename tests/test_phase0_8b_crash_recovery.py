import os
import sys
import time
import json
import sqlite3
import subprocess
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import Database, resolve_db_path
from src.opportunity import Opportunity

TEST_DB_FILE = "test_phase0_8b_crash.db"
TEST_LOG_FILE = "test_phase0_8b_crash_audit.jsonl"

@unittest.skipIf(os.environ.get("AGENTOS_SKIP_PROCESS_TESTS") == "1", "Process tests skipped via AGENTOS_SKIP_PROCESS_TESTS=1")
class TestPhase08BCrashRecovery(unittest.TestCase):

    def setUp(self):
        self.target_db = resolve_db_path(TEST_DB_FILE)
        if os.path.exists(self.target_db):
            try:
                os.remove(self.target_db)
            except PermissionError:
                pass
        if os.path.exists(TEST_LOG_FILE):
            try:
                os.remove(TEST_LOG_FILE)
            except PermissionError:
                pass
        self.db = Database(TEST_DB_FILE)

    def tearDown(self):
        # Close DB connection cleanly without deleting target_db so it remains available for inspection
        del self.db
        time.sleep(0.1)
        if os.path.exists(TEST_LOG_FILE):
            try:
                os.remove(TEST_LOG_FILE)
            except PermissionError:
                pass

    def test_subprocess_sigkill_recovery_lifecycle(self):
        """Crash Recovery Integration Test (DoD item 4):
        1. Seeds 3 opportunities in SQLite DB.
        2. Spawns autonomous engine subprocess running main.py.
        3. Forcibly kills subprocess (SIGKILL / proc.kill()) mid-execution.
        4. Verifies persisted DB state contains partially processed tasks.
        5. Restarts autonomous engine subprocess on exact same SQLite DB.
        6. Asserts:
           - No task row duplicated (UNIQUE opportunity_id)
           - RECOVERY_STARTED and RECOVERY_COMPLETED events logged in audit_log
           - Budget opportunity_id foreign keys resolved
           - No terminal state corrupted or re-executed
        """
        # Step 1: Seed 3 opportunities in DB
        opps = [
            {"id": "proc-crash-1", "title": "Crash Task 1", "desc": "Body for task 1"},
            {"id": "proc-crash-2", "title": "Crash Task 2", "desc": "Body for task 2"},
            {"id": "proc-crash-3", "title": "Crash Task 3", "desc": "Body for task 3"}
        ]
        now = time.time()
        for o in opps:
            self.db.execute_atomic_transition({
                "task_id": o["id"],
                "opportunity_id": o["id"],
                "state": "DISCOVERED",
                "source": "proc_crash_source",
                "repo": "crash/repo",
                "title": o["title"],
                "description": o["desc"],
                "payload": {"repo": "crash/repo", "issue_number": 999},
                "created_at": now,
                "updated_at": now
            })

        # Runner script code
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        runner_code = f"""
import sys, os
sys.path.insert(0, {json.dumps(project_root)})
from main import run_phase0_7_autonomous_loop
from src.opportunity import Opportunity

test_opps = [
    Opportunity(id="proc-crash-1", title="Crash Task 1", description="Body for task 1", source="proc_crash_source", payload={{"repo": "crash/repo", "issue_number": 999}}),
    Opportunity(id="proc-crash-2", title="Crash Task 2", description="Body for task 2", source="proc_crash_source", payload={{"repo": "crash/repo", "issue_number": 999}}),
    Opportunity(id="proc-crash-3", title="Crash Task 3", description="Body for task 3", source="proc_crash_source", payload={{"repo": "crash/repo", "issue_number": 999}})
]

run_phase0_7_autonomous_loop(
    db_path={json.dumps(TEST_DB_FILE)},
    num_opportunities=3,
    interval_sec=1.0,
    max_ticks=None,
    opportunities_override=test_opps
)
"""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(runner_code)
            script_path = f.name

        try:
            env = dict(os.environ)
            env["AGENTOS_TEST_MODE"] = "1"
            env["PYTHONPATH"] = project_root

            # Step 2: Spawn first subprocess
            print("\n[CRASH TEST] Spawning Subprocess Run 1...")
            proc1 = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

            # Let it run for 3 seconds (executing tick & engine loops)
            time.sleep(3.0)

            # Step 3: Hard kill (SIGKILL / proc.kill())
            print("[CRASH TEST] Executing SIGKILL (proc.kill()) on Subprocess Run 1...")
            proc1.kill()
            stdout1, stderr1 = proc1.communicate()

            print(f"[CRASH TEST Run 1 STDOUT]:\n{stdout1}")
            print(f"[CRASH TEST Run 1 STDERR]:\n{stderr1}")

            # Step 4: Verify DB state after crash
            conn = sqlite3.connect(self.target_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            active_tasks = cur.execute("SELECT task_id, state FROM tasks").fetchall()
            conn.close()

            print(f"[CRASH TEST Post-Kill Tasks]: {[(r['task_id'], r['state']) for r in active_tasks]}")
            self.assertEqual(len(active_tasks), 3, f"Tasks table MUST contain strictly the 3 seeded opportunities, got: {active_tasks}")

            # Step 5: Restart engine subprocess on exact same SQLite DB
            print("[CRASH TEST] Restarting Subprocess Run 2 on exact same DB...")
            proc2 = subprocess.Popen([sys.executable, script_path], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

            # Let recovery routine run and complete tasks (5 seconds)
            time.sleep(5.0)

            print("[CRASH TEST] Terminating Subprocess Run 2...")
            proc2.kill()
            stdout2, stderr2 = proc2.communicate()

            print(f"[CRASH TEST Run 2 STDOUT]:\n{stdout2}")
            print(f"[CRASH TEST Run 2 STDERR]:\n{stderr2}")

            # Step 6: Database integrity assertions
            conn = sqlite3.connect(self.target_db)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            # Assert A: Zero duplicate task rows
            dups = cur.execute("SELECT opportunity_id, COUNT(*) as cnt FROM tasks GROUP BY opportunity_id HAVING cnt > 1").fetchall()
            self.assertEqual(len(dups), 0, f"Duplicate opportunity rows found in tasks table: {dups}")

            # Assert B: Zero duplicate WORKER_EXECUTED audit log events per opportunity_id
            dup_worker_execs = cur.execute("""
                SELECT json_extract(payload_json, '$.opportunity_id') as opp_id, COUNT(*) as cnt FROM audit_log
                WHERE event_type = 'WORKER_EXECUTED'
                GROUP BY json_extract(payload_json, '$.opportunity_id') HAVING cnt > 1
            """).fetchall()
            self.assertEqual(len(dup_worker_execs), 0, f"Duplicate WORKER_EXECUTED audit log events found: {dup_worker_execs}")

            # Assert C: Zero duplicate budget rows per opportunity_id
            dup_budget_rows = cur.execute("""
                SELECT opportunity_id, COUNT(*) as cnt FROM budget
                GROUP BY opportunity_id HAVING cnt > 1
            """).fetchall()
            self.assertEqual(len(dup_budget_rows), 0, f"Duplicate budget rows found: {dup_budget_rows}")

            # Assert D: STARTUP_RECOVERY_STARTED and STARTUP_RECOVERY_COMPLETED in audit_log
            audit_events = [r["event_type"] for r in cur.execute("SELECT event_type FROM audit_log").fetchall()]
            self.assertIn("STARTUP_RECOVERY_STARTED", audit_events, f"STARTUP_RECOVERY_STARTED event missing in audit_log! Events: {audit_events}")
            self.assertIn("STARTUP_RECOVERY_COMPLETED", audit_events, f"STARTUP_RECOVERY_COMPLETED event missing in audit_log! Events: {audit_events}")

            # Assert E: All budget opportunity_ids exist in tasks table
            orphan_budget = cur.execute("""
                SELECT b.opportunity_id FROM budget b
                LEFT JOIN tasks t ON b.opportunity_id = t.opportunity_id
                WHERE t.opportunity_id IS NULL
            """).fetchall()
            self.assertEqual(len(orphan_budget), 0, f"Orphan budget rows found with unresolvable opportunity_id: {orphan_budget}")

            # Assert F: Every task reached a valid terminal state or valid active state
            final_rows = cur.execute("SELECT task_id, state FROM tasks").fetchall()
            print(f"[CRASH TEST Final DB Task States]: {[(r['task_id'], r['state']) for r in final_rows]}")
            self.assertEqual(len(final_rows), 3, f"Final tasks table MUST contain strictly the 3 seeded opportunities, got: {final_rows}")
            for r in final_rows:
                self.assertIn(r["state"], ("COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED", "WAITING_APPROVAL", "REVIEW", "EXECUTING", "READY", "PLANNED", "DISCOVERED"))

            conn.close()
            import shutil
            root_db = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), TEST_DB_FILE)
            shutil.copyfile(self.target_db, root_db)

        finally:
            if os.path.exists(script_path):
                os.remove(script_path)

if __name__ == "__main__":
    unittest.main()
