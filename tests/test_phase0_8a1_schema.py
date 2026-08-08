import os
import sys
import sqlite3
import shutil
import tempfile
import unittest

from src.db import Database, resolve_db_path, get_project_root
from src.migrations import run_migrations

class TestPhase08A1Schema(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_db_path = os.path.join(self.temp_dir, "test_0_8a1.db")

    def tearDown(self):
        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_audited_db_migration_and_orphan_quarantine(self):
        """Runs migration directly against a copy of the actual pristine audited agencyos.db at project root.
        Verifies 23 orphan records quarantined, budget and approvals tables left completely empty,
        and PRAGMA foreign_key_check passes with 0 errors.

        This test is coupled to the specific VPS soak artifact agencyos.db that has 23 pre-migration
        legacy orphan rows. It is skipped when that file is not present (e.g. clean checkout, after
        smoke tests that delete it, or CI environments).
        """
        audited_db_orig = os.path.join(get_project_root(), "agencyos.db")
        if not os.path.exists(audited_db_orig):
            self.skipTest(
                "agencyos.db not present at project root — this test requires the specific VPS soak "
                "artifact with 23 pre-migration legacy orphan rows. Run after a soak before cleanup."
            )

        # Also skip if this DB has already been migrated (orphan table exists but is empty),
        # meaning it's a fresh post-migration file rather than the pre-migration VPS artifact.
        import sqlite3 as _sqlite3
        _conn = _sqlite3.connect(audited_db_orig)
        _has_orphan_table = _conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='_legacy_orphaned_rows'"
        ).fetchone()[0]
        _orphan_count = _conn.execute("SELECT COUNT(*) FROM _legacy_orphaned_rows").fetchone()[0] if _has_orphan_table else -1
        _conn.close()
        if _has_orphan_table and _orphan_count == 0:
            self.skipTest(
                "agencyos.db has already been migrated (0 orphan rows) — this test requires the "
                "pre-migration VPS soak artifact with 23 legacy orphan rows."
            )


        # Copy audited DB to temp directory
        audited_db_copy = os.path.join(self.temp_dir, "audited_agencyos_copy.db")
        shutil.copy2(audited_db_orig, audited_db_copy)
        print(f"\n[TEST DISCOVERY DB PATH] test_audited_db_migration_and_orphan_quarantine -> target: {audited_db_copy}")

        # Run migration on the copy
        run_migrations(audited_db_copy)

        conn = sqlite3.connect(audited_db_copy)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # 1. Verify exactly 23 orphaned rows quarantined
        orphans = cursor.execute("SELECT * FROM _legacy_orphaned_rows").fetchall()
        self.assertEqual(len(orphans), 23, f"Expected 23 quarantined orphan rows, found {len(orphans)}")


        budget_orphans = [r for r in orphans if r["source_table"] == "budget"]
        approval_orphans = [r for r in orphans if r["source_table"] == "approvals"]
        self.assertEqual(len(budget_orphans), 10)
        self.assertEqual(len(approval_orphans), 13)

        # 2. Verify budget and approvals tables are completely empty immediately post-migration
        remaining_budget = cursor.execute("SELECT COUNT(*) as cnt FROM budget").fetchone()["cnt"]
        remaining_approvals = cursor.execute("SELECT COUNT(*) as cnt FROM approvals").fetchone()["cnt"]
        self.assertEqual(remaining_budget, 0, f"Expected 0 remaining budget rows, found {remaining_budget}")
        self.assertEqual(remaining_approvals, 0, f"Expected 0 remaining approvals rows, found {remaining_approvals}")

        # 3. Verify foreign_key_check and integrity_check
        cursor.execute("PRAGMA foreign_keys = ON;")
        fk_errors = cursor.execute("PRAGMA foreign_key_check;").fetchall()
        self.assertEqual(len(fk_errors), 0, f"Expected 0 foreign key errors, found: {fk_errors}")

        integrity = cursor.execute("PRAGMA integrity_check;").fetchall()
        self.assertEqual(integrity[0][0], "ok")
        conn.close()

    def test_multi_cwd_deterministic_path_resolution(self):
        """Verifies that resolve_db_path returns the exact same absolute path regardless of CWD,
        and isolates test execution when in test mode.
        """
        orig_cwd = os.getcwd()
        try:
            path_root = resolve_db_path("agencyos.db")
            print(f"\n[TEST DISCOVERY DB PATH] test_multi_cwd_deterministic_path_resolution -> root path: {path_root}")

            # Ensure path is isolated in test mode (never project root agencyos.db)
            prod_path = os.path.join(get_project_root(), "agencyos.db")
            self.assertNotEqual(path_root, prod_path, "Test suite MUST NOT resolve default DB to production agencyos.db")

            # Change CWD to temp_dir
            os.chdir(self.temp_dir)
            path_temp = resolve_db_path("agencyos.db")

            # Change CWD to tests directory if exists
            tests_dir = os.path.join(get_project_root(), "tests")
            if os.path.exists(tests_dir):
                os.chdir(tests_dir)
                path_tests = resolve_db_path("agencyos.db")
                self.assertEqual(path_root, path_tests)

            self.assertEqual(path_root, path_temp)
            self.assertTrue(os.path.isabs(path_root))
        finally:
            os.chdir(orig_cwd)

    def test_schema_constraints_and_foreign_keys(self):
        """Verifies CHECK, UNIQUE, NOT NULL, FOREIGN KEY ON DELETE RESTRICT, and indexes."""
        db = Database(self.test_db_path)
        print(f"\n[TEST DISCOVERY DB PATH] test_schema_constraints_and_foreign_keys -> target: {db.db_path}")

        conn = sqlite3.connect(self.test_db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        cursor = conn.cursor()

        # 1. Test CHECK constraint on tasks.state
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("""
                INSERT INTO tasks (task_id, opportunity_id, state, created_at, updated_at)
                VALUES ('t-1', 'opp-1', 'INVALID_STATE', 1.0, 1.0)
            """)

        # Insert valid task
        cursor.execute("""
            INSERT INTO tasks (task_id, opportunity_id, state, created_at, updated_at)
            VALUES ('t-valid', 'opp-valid', 'DISCOVERED', 1.0, 1.0)
        """)
        conn.commit()

        # 2. Test UNIQUE constraint on opportunity_id
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("""
                INSERT INTO tasks (task_id, opportunity_id, state, created_at, updated_at)
                VALUES ('t-dup', 'opp-valid', 'PLANNED', 1.0, 1.0)
            """)

        # 3. Test Orphan FOREIGN KEY constraint on approvals and budget
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("""
                INSERT INTO approvals (id, opportunity_id, status, requested_at)
                VALUES ('appr-orphan', 'non-existent-opp', 'APPROVED', 1.0)
            """)

        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("""
                INSERT INTO budget (opportunity_id, amount, date_str, timestamp)
                VALUES ('non-existent-opp', 0.05, '2026-08-05', 1.0)
            """)

        # 4. Insert valid child approvals and budget
        cursor.execute("""
            INSERT INTO approvals (id, opportunity_id, status, requested_at)
            VALUES ('appr-valid', 'opp-valid', 'APPROVED', 1.0)
        """)
        cursor.execute("""
            INSERT INTO budget (opportunity_id, amount, date_str, timestamp)
            VALUES ('opp-valid', 0.05, '2026-08-05', 1.0)
        """)
        conn.commit()

        # 5. Test ON DELETE RESTRICT on tasks deletion
        with self.assertRaises(sqlite3.IntegrityError):
            cursor.execute("DELETE FROM tasks WHERE opportunity_id = 'opp-valid'")

        # 6. Verify Indexes Exist
        indexes = cursor.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()
        idx_names = [i[0] for i in indexes]
        self.assertIn("idx_tasks_opportunity_id", idx_names)
        self.assertIn("idx_tasks_state", idx_names)
        self.assertIn("idx_audit_log_timestamp", idx_names)

        conn.close()

if __name__ == "__main__":
    unittest.main()
