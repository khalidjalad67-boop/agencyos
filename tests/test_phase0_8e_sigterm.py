"""
tests/test_phase0_8e_sigterm.py

0.8E Regression Suite -- SIGTERM / mid-heartbeat-write shutdown tests.

These tests prove that no SQLite/JSONL count drift can result from:
  1. A SIGTERM arriving between SQLite commit and JSONL append in log_event().
  2. A SIGTERM arriving between SQLite commit and JSONL append in execute_atomic_transition().
  3. Repeated SIGTERM-at-boundary kills over dozens of iterations.

Tests exercise already-fixed code.  If any test here fails against current
code, that is a real regression -- do NOT change the implementation to make
it pass; report the regression first.

Write order contract (db.py):
  log_event()                 -> conn.commit()  THEN  write_jsonl()
  execute_atomic_transition() -> conn.commit()  THEN  write_jsonl()

PRAGMA synchronous=FULL means a committed transaction is on disk before
commit() returns, so a kill immediately after commit() cannot silently
discard an SQLite row.  JSONL is protected by the SIGTERM handler giving
the process a clean drain window before exit.
"""

import os
import sys
import json
import time
import signal
import sqlite3
import subprocess
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["AGENTOS_TEST_MODE"] = "1"

from src.db import Database, resolve_db_path, resolve_log_path


def _count_sqlite(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _count_jsonl(log_path):
    if not os.path.exists(log_path):
        return 0
    with open(log_path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _counts_match(db_path, log_path):
    sql = _count_sqlite(db_path)
    jsonl = _count_jsonl(log_path)
    return sql, jsonl, sql == jsonl


# ---------------------------------------------------------------------------
# 1. PRAGMA synchronous=FULL is set on every connection
# ---------------------------------------------------------------------------
class TestSynchronousFull(unittest.TestCase):
    """Verifies PRAGMA synchronous=FULL is active on _connection() and transaction()."""

    def _make_db(self, suffix=""):
        name = f"test_sync_{os.getpid()}{suffix}"
        return Database(f"{name}.db", f"{name}.jsonl")

    def _cleanup(self, db):
        for p in (db.db_path, db.log_filepath):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_connection_synchronous_is_full(self):
        """_connection() context must have synchronous=FULL (value 2)."""
        db = self._make_db("_conn")
        try:
            with db._connection() as conn:
                row = conn.execute("PRAGMA synchronous;").fetchone()
                self.assertEqual(
                    row[0], 2,
                    f"Expected synchronous=FULL (2), got {row[0]}. "
                    "PRAGMA synchronous=FULL must be set in _connection()."
                )
        finally:
            self._cleanup(db)

    def test_transaction_synchronous_is_full(self):
        """transaction() context must have synchronous=FULL (value 2)."""
        db = self._make_db("_txn")
        try:
            with db.transaction() as conn:
                row = conn.execute("PRAGMA synchronous;").fetchone()
                self.assertEqual(
                    row[0], 2,
                    f"Expected synchronous=FULL (2), got {row[0]}. "
                    "PRAGMA synchronous=FULL must be set in transaction()."
                )
        finally:
            self._cleanup(db)


# ---------------------------------------------------------------------------
# 2. No orphan JSONL when write_jsonl is interrupted after SQLite commit (log_event)
# ---------------------------------------------------------------------------
class TestLogEventWriteOrder(unittest.TestCase):
    """Interrupting write_jsonl() after commit must never leave JSONL > SQLite."""

    def setUp(self):
        name = f"test_log_order_{os.getpid()}"
        self.db = Database(f"{name}.db", f"{name}.jsonl")
        self.db_path = self.db.db_path
        self.log_path = self.db.log_filepath

    def tearDown(self):
        for p in (self.db_path, self.log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_write_jsonl_failure_leaves_no_orphan_jsonl(self):
        """SQLite commit fires before write_jsonl; a mid-write kill must not produce JSONL > SQLite."""
        from src.logger import AuditLogger
        original = AuditLogger.write_jsonl

        def exploding_write(self_inner, *args, **kwargs):
            raise OSError("Simulated kill between commit and JSONL append")

        AuditLogger.write_jsonl = exploding_write
        try:
            for i in range(5):
                try:
                    self.db.log_event("TEST_EVENT", {"i": i})
                except Exception:
                    pass
        finally:
            AuditLogger.write_jsonl = original

        sql, jsonl, _ = _counts_match(self.db_path, self.log_path)
        self.assertEqual(sql, 5, f"Expected 5 SQLite rows; got {sql}")
        self.assertLessEqual(jsonl, sql,
            f"JSONL ({jsonl}) > SQLite ({sql}): orphan JSONL line -- write order violated")

    def test_clean_log_event_no_drift(self):
        """20 successful log_event() calls must produce identical counts."""
        for i in range(20):
            self.db.log_event("TEST_HB", {"i": i})
        sql, jsonl, match = _counts_match(self.db_path, self.log_path)
        self.assertTrue(match, f"Drift: SQLite={sql}, JSONL={jsonl}")


# ---------------------------------------------------------------------------
# 3. No orphan JSONL when write_jsonl is interrupted in execute_atomic_transition
# ---------------------------------------------------------------------------
class TestAtomicTransitionWriteOrder(unittest.TestCase):

    def setUp(self):
        name = f"test_transition_order_{os.getpid()}"
        self.db = Database(f"{name}.db", f"{name}.jsonl")
        self.db_path = self.db.db_path
        self.log_path = self.db.log_filepath

    def tearDown(self):
        for p in (self.db_path, self.log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def _task(self, opp_id, state):
        return {
            "task_id": opp_id, "opportunity_id": opp_id,
            "state": state, "source": "github", "repo": "test/repo",
            "title": "T", "description": "d", "payload": {},
            "created_at": time.time(),
        }

    def test_write_jsonl_failure_in_atomic_transition_no_orphan(self):
        from src.logger import AuditLogger
        original = AuditLogger.write_jsonl

        # Seed 3 tasks cleanly
        for i in range(3):
            t = self._task(f"opp-{i}", "DISCOVERED")
            self.db.execute_atomic_transition(t,
                audit_event=("TASK_DISCOVERED", {"opportunity_id": t["opportunity_id"]}))

        sql_seed, jsonl_seed, _ = _counts_match(self.db_path, self.log_path)
        self.assertEqual(sql_seed, jsonl_seed, "Seed must produce matching counts")

        def exploding_write(self_inner, *args, **kwargs):
            raise OSError("Simulated kill")

        AuditLogger.write_jsonl = exploding_write
        try:
            for i in range(3):
                t = self._task(f"opp-{i}", "PLANNED")
                try:
                    self.db.execute_atomic_transition(t,
                        audit_event=("TASK_PLANNED", {"opportunity_id": t["opportunity_id"]}))
                except Exception:
                    pass
        finally:
            AuditLogger.write_jsonl = original

        sql, jsonl, _ = _counts_match(self.db_path, self.log_path)
        self.assertEqual(sql, sql_seed + 3,
            f"Expected {sql_seed + 3} SQLite rows; got {sql}")
        self.assertLessEqual(jsonl, sql,
            f"JSONL ({jsonl}) > SQLite ({sql}): orphan JSONL line -- write order violated")


# ---------------------------------------------------------------------------
# 4. Subprocess SIGTERM stress test (skipped on Windows)
# ---------------------------------------------------------------------------

_WORKER_SCRIPT_TEMPLATE = """
import os, sys, time, signal
sys.path.insert(0, {project_root!r})
os.environ["AGENTOS_ALLOW_PROD_DB"] = "1"
from src.db import Database
db = Database({db_path!r}, {log_path!r})
_done = False
def _handle(signum, frame):
    global _done
    _done = True
signal.signal(signal.SIGTERM, _handle)
sys.stdout.write("STARTED\\n")
sys.stdout.flush()
count = 0
while not _done and count < 200:
    db.log_event("SCHEDULER_HEARTBEAT", {{"count": count, "ts": time.time()}})
    time.sleep(0.01)
    count += 1
sys.exit(0)
"""


@unittest.skipIf(sys.platform == "win32", "SIGTERM subprocess test not supported on Windows")
class TestSigtermSubprocessNoDrift(unittest.TestCase):
    """
    Spawns a real subprocess running a tight heartbeat loop.
    Sends SIGTERM at varying delays targeting the commit/append boundary.
    Verifies JSONL never exceeds SQLite across 30 iterations.
    """

    ITERATIONS = 30

    def _paths(self, i):
        d = os.path.join(os.path.dirname(__file__), ".temp_test_dbs")
        os.makedirs(d, exist_ok=True)
        db_path = os.path.join(d, f"sigterm_{os.getpid()}_{i}.db")
        log_path = os.path.join(d, f"sigterm_{os.getpid()}_{i}.jsonl")
        for p in (db_path, log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        return db_path, log_path

    def _cleanup(self, db_path, log_path):
        for p in (db_path, log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_sigterm_at_heartbeat_boundary_no_jsonl_orphan(self):
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        violations = []
        results = []

        for i in range(self.ITERATIONS):
            db_path, log_path = self._paths(i)
            # Pre-initialize DB schema so worker process doesn't get killed mid-migration
            init_db = Database(db_path, log_path)
            del init_db

            script = _WORKER_SCRIPT_TEMPLATE.format(
                project_root=project_root,
                db_path=db_path,
                log_path=log_path,
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )

            # Wait for subprocess to finish initialization and start heartbeat loop
            started = False
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                if line.strip() == "STARTED":
                    started = True
                    break

            if not started:
                self.fail(f"Subprocess failed to start heartbeat loop")

            delay = 0.02 + (i % 10) * 0.03  # 0.02s to 0.29s of active heartbeat writes
            time.sleep(delay)
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

            sql, jsonl, _ = _counts_match(db_path, log_path)
            status = "PASS" if jsonl <= sql else "FAIL"
            print(f"[SIGTERM ITER {i:02d}] delay={delay:.2f}s | SQLite={sql} | JSONL={jsonl} | {status}", flush=True)
            results.append((i, delay, sql, jsonl, status))

            if jsonl > sql:
                violations.append(
                    f"iter {i}: JSONL={jsonl} > SQLite={sql} (delay={delay:.2f}s)"
                )
            self._cleanup(db_path, log_path)

        if violations:
            self.fail(
                f"{len(violations)}/{self.ITERATIONS} SIGTERM violations:\n"
                + "\n".join(violations)
            )


# ---------------------------------------------------------------------------
# 5. verify_audit.py correctly flags JSONL > SQLite (VPS soak regression)
# ---------------------------------------------------------------------------
class TestVerifyAuditFlagsMismatch(unittest.TestCase):

    def setUp(self):
        d = os.path.join(os.path.dirname(__file__), ".temp_test_dbs")
        os.makedirs(d, exist_ok=True)
        pid = os.getpid()
        self.db_path = os.path.join(d, f"verify_mismatch_{pid}.db")
        self.log_path = os.path.join(d, f"verify_mismatch_{pid}.jsonl")
        for p in (self.db_path, self.log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def tearDown(self):
        for p in (self.db_path, self.log_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

    def test_verify_audit_detects_jsonl_gt_sqlite(self):
        os.environ["AGENTOS_ALLOW_PROD_DB"] = "1"
        try:
            db = Database(self.db_path, self.log_path)
            for i in range(10):
                db.log_event("SCHEDULER_HEARTBEAT", {"i": i})
        finally:
            del os.environ["AGENTOS_ALLOW_PROD_DB"]

        orphan = {"timestamp": time.time(), "event_type": "SCHEDULER_HEARTBEAT",
                  "payload": {"orphan": True}}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(orphan) + "\n")

        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        verifier = os.path.join(project_root, "tools", "verify_audit.py")
        result = subprocess.run(
            [sys.executable, verifier, self.db_path, self.log_path],
            capture_output=True, text=True
        )
        self.assertNotEqual(result.returncode, 0,
            f"verify_audit.py should exit 1 on JSONL > SQLite; got {result.returncode}. "
            f"stdout: {result.stdout}")
        self.assertIn("mismatch", result.stdout.lower(),
            f"Expected 'mismatch' in output; got: {result.stdout}")

    def test_verify_audit_passes_on_matching_counts(self):
        os.environ["AGENTOS_ALLOW_PROD_DB"] = "1"
        try:
            db = Database(self.db_path, self.log_path)
            for i in range(5):
                db.log_event("SCHEDULER_HEARTBEAT", {"i": i})
        finally:
            del os.environ["AGENTOS_ALLOW_PROD_DB"]

        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        verifier = os.path.join(project_root, "tools", "verify_audit.py")
        result = subprocess.run(
            [sys.executable, verifier, self.db_path, self.log_path],
            capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0,
            f"verify_audit.py should pass on matching counts; got {result.returncode}. "
            f"stdout: {result.stdout}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
