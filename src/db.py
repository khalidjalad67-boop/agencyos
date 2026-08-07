import os
import sys
import sqlite3
import json
import time
import tempfile
from contextlib import contextmanager
from typing import Dict, Any, List, Optional, Tuple, Set

DEFAULT_DB_PATH = "agencyos.db"

def get_project_root() -> str:
    """Returns the absolute path to the AgentOS project root directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def is_in_test_mode() -> bool:
    """Returns True if code is executing under unittest or test harness."""
    return "unittest" in sys.modules or os.environ.get("AGENTOS_TEST_MODE") == "1"

def resolve_db_path(db_path: str = DEFAULT_DB_PATH) -> str:
    """Resolves database path deterministically. If in test mode and requesting a relative DB path,
    isolates to a temporary test DB directory to prevent test suite from touching production DB.
    """
    if os.path.isabs(db_path):
        return db_path
    
    if is_in_test_mode() and not os.environ.get("AGENTOS_ALLOW_PROD_DB"):
        test_temp_dir = os.path.join(get_project_root(), "tests", ".temp_test_dbs")
        os.makedirs(test_temp_dir, exist_ok=True)
        return os.path.join(test_temp_dir, db_path)
        
    return os.path.join(get_project_root(), db_path)

LEGAL_TRANSITIONS: Dict[str, Set[str]] = {
    # First write — no prior row
    "DISCOVERED":       {"DISCOVERED", "PLANNED", "QUALITY_REJECTED"},

    # Budget check
    "PLANNED":          {"READY", "BLOCKED"},

    # Worker execution block
    "READY":            {"EXECUTING"},
    "EXECUTING":        {
                            "EXECUTING",        # self — WORKER_EXECUTED (L194) and re-entry (L156)
                            "REVIEW",           # worker done, entering review (L212, L228, recovery L50)
                            "WAITING_APPROVAL", # recovery: both caches found (recovery L34)
                            "READY",            # recovery: no cache — reset (recovery L64)
                            "WORKER_FAILED",    # worker returned FAILED status (L180)
                        },

    # Review block — REVIEW→REVIEW covers the resume path (recovery L50 -> engine L228)
    "REVIEW":           {"REVIEW", "WAITING_APPROVAL"},

    # Approval gate
    "WAITING_APPROVAL": {"WAITING_APPROVAL", "COMPLETED", "BLOCKED"},

    # Terminal states — zero outgoing transitions
    "COMPLETED":        set(),
    "BLOCKED":          set(),
    "QUALITY_REJECTED": set(),
    "WORKER_FAILED":    set(),
}

TERMINAL_STATES = frozenset({"COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED"})

class InvalidStateTransitionError(Exception):
    """Raised when execute_atomic_transition would write an illegal state."""
    pass

class Database:
    """Persistent SQLite Database Manager handling tasks, approvals, idempotency_keys, audit_log, and budget tables."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH, log_filepath: str = "audit_log.jsonl"):
        self.db_path = resolve_db_path(db_path)
        self.log_filepath = log_filepath
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """Provides an atomic transaction context manager.
        Enforces foreign keys, acquires exclusive write lock, and commits all operations atomically on exit.
        Rolls back completely if an unhandled exception occurs.
        """
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
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

    def _init_db(self):
        """Initializes database schema by executing migration runner."""
        from src.migrations import run_migrations
        run_migrations(self.db_path)

    # --- ATOMIC LIFECYCLE TRANSITION METHOD ---
    def execute_atomic_transition(
        self,
        task_data: Dict[str, Any],
        audit_event: Optional[Tuple[str, Dict[str, Any]]] = None,
        spend_record: Optional[Tuple[float, str, str]] = None,
        approval_record: Optional[Tuple[str, str, str, Optional[float]]] = None
    ) -> None:
        """Executes a task state transition, audit event, spend record, and approval decision
        as one single atomic SQLite transaction (all-or-nothing).
        Enforces legal state transitions against persisted state.
        """
        new_state = task_data.get("state")
        existing = self.get_task(task_data["task_id"])
        if existing is not None and new_state is not None:
            current_state = existing["state"]
            legal = LEGAL_TRANSITIONS.get(current_state, set())
            if new_state not in legal:
                self.log_event("ILLEGAL_TRANSITION", {
                    "task_id": task_data["task_id"],
                    "from_state": current_state,
                    "to_state": new_state,
                    "reason": "Not in LEGAL_TRANSITIONS"
                })
                raise InvalidStateTransitionError(
                    f"Illegal state transition {current_state!r} -> {new_state!r} "
                    f"for task {task_data['task_id']!r}"
                )

        now = time.time()
        with self.transaction() as conn:
            cursor = conn.cursor()
            
            # 1. Save Task Record
            cursor.execute("""
                INSERT INTO tasks (
                    task_id, opportunity_id, state, source, repo, title, description,
                    payload_json, task_spec_json, worker_result_json, review_result_json,
                    error_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    opportunity_id=excluded.opportunity_id,
                    state=excluded.state,
                    source=excluded.source,
                    repo=excluded.repo,
                    title=excluded.title,
                    description=excluded.description,
                    payload_json=excluded.payload_json,
                    task_spec_json=excluded.task_spec_json,
                    worker_result_json=excluded.worker_result_json,
                    review_result_json=excluded.review_result_json,
                    error_reason=excluded.error_reason,
                    updated_at=excluded.updated_at
            """, (
                task_data["task_id"],
                task_data["opportunity_id"],
                task_data["state"],
                task_data.get("source", ""),
                task_data.get("repo", ""),
                task_data.get("title", ""),
                task_data.get("description", ""),
                json.dumps(task_data.get("payload", {})) if isinstance(task_data.get("payload"), dict) else task_data.get("payload_json", "{}"),
                json.dumps(task_data.get("task_spec", {})) if isinstance(task_data.get("task_spec"), dict) else task_data.get("task_spec_json", None),
                json.dumps(task_data.get("worker_result", {})) if isinstance(task_data.get("worker_result"), dict) else task_data.get("worker_result_json", None),
                json.dumps(task_data.get("review_result", {})) if isinstance(task_data.get("review_result"), dict) else task_data.get("review_result_json", None),
                task_data.get("error_reason", ""),
                task_data.get("created_at", now),
                now
            ))

            # 2. Spend Record (if provided)
            if spend_record:
                amount, date_str, description = spend_record
                cursor.execute("""
                    INSERT INTO budget (opportunity_id, amount, date_str, timestamp, description)
                    VALUES (?, ?, ?, ?, ?)
                """, (task_data["opportunity_id"], amount, date_str, now, description))

            # 3. Approval Record (if provided)
            if approval_record:
                appr_id, status, comments, decided_at = approval_record
                cursor.execute("""
                    INSERT INTO approvals (id, opportunity_id, status, requested_at, decided_at, comments)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        decided_at=excluded.decided_at,
                        comments=excluded.comments
                """, (appr_id, task_data["opportunity_id"], status, now, decided_at, comments))

            # 4. Audit Log Event (if provided)
            if audit_event:
                event_type, payload = audit_event
                cursor.execute("""
                    INSERT INTO audit_log (event_type, payload_json, timestamp)
                    VALUES (?, ?, ?)
                """, (event_type, json.dumps(payload), now))

        # 5. Append audit event to audit_log.jsonl ONLY AFTER transaction commits successfully
        if audit_event:
            event_type, payload = audit_event
            from src.logger import AuditLogger
            AuditLogger(log_filepath=self.log_filepath).write_jsonl(event_type, payload, timestamp=now)



    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            d = dict(row)
            d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
            d["task_spec"] = json.loads(d["task_spec_json"]) if d["task_spec_json"] else None
            d["worker_result"] = json.loads(d["worker_result_json"]) if d["worker_result_json"] else None
            d["review_result"] = json.loads(d["review_result_json"]) if d["review_result_json"] else None
            return d

    def get_tasks_by_state(self, state: str) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks WHERE state = ?", (state,))
            rows = cursor.fetchall()
            res = []
            for row in rows:
                d = dict(row)
                d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
                d["task_spec"] = json.loads(d["task_spec_json"]) if d["task_spec_json"] else None
                d["worker_result"] = json.loads(d["worker_result_json"]) if d["worker_result_json"] else None
                d["review_result"] = json.loads(d["review_result_json"]) if d["review_result_json"] else None
                res.append(d)
            return res

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tasks")
            rows = cursor.fetchall()
            res = []
            for row in rows:
                d = dict(row)
                d["payload"] = json.loads(d["payload_json"]) if d["payload_json"] else {}
                d["task_spec"] = json.loads(d["task_spec_json"]) if d["task_spec_json"] else None
                d["worker_result"] = json.loads(d["worker_result_json"]) if d["worker_result_json"] else None
                d["review_result"] = json.loads(d["review_result_json"]) if d["review_result_json"] else None
                res.append(d)
            return res

    # --- APPROVALS METHODS ---
    def save_approval(self, approval_id: str, opportunity_id: str, status: str, comments: str = "") -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO approvals (id, opportunity_id, status, requested_at, decided_at, comments)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status=excluded.status,
                    decided_at=excluded.decided_at,
                    comments=excluded.comments
            """, (approval_id, opportunity_id, status, now, now if status != "PENDING" else None, comments))
            conn.commit()

    def get_approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def get_pending_approvals(self) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM approvals WHERE status = 'PENDING'")
            return [dict(r) for r in cursor.fetchall()]

    # --- IDEMPOTENCY METHODS ---
    def get_idempotency_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT result_json FROM idempotency_keys WHERE key = ?", (key,))
            row = cursor.fetchone()
            if row:
                return json.loads(row["result_json"])
            return None

    def save_idempotency_key(self, key: str, result_data: Dict[str, Any]) -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO idempotency_keys (key, result_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET result_json=excluded.result_json
            """, (key, json.dumps(result_data), now))
            conn.commit()

    # --- AUDIT LOG METHODS ---
    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO audit_log (event_type, payload_json, timestamp)
                VALUES (?, ?, ?)
            """, (event_type, json.dumps(payload), now))
            conn.commit()
        from src.logger import AuditLogger
        AuditLogger(log_filepath=self.log_filepath).write_jsonl(event_type, payload, timestamp=now)

    def get_audit_logs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            res = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d["payload_json"])
                res.append(d)
            return res

    # --- BUDGET METHODS ---
    def record_spend(self, opportunity_id: str, amount: float, date_str: str, description: str = "") -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO budget (opportunity_id, amount, date_str, timestamp, description)
                VALUES (?, ?, ?, ?, ?)
            """, (opportunity_id, amount, date_str, now, description))
            conn.commit()

    def get_today_spend(self, date_str: str) -> float:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(amount) as total FROM budget WHERE date_str = ?", (date_str,))
            row = cursor.fetchone()
            if row and row["total"] is not None:
                return float(row["total"])
            return 0.0

    def is_budget_empty(self) -> bool:
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as cnt FROM budget")
            row = cursor.fetchone()
            return row["cnt"] == 0 if row else True

    def get_telemetry_metrics(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """Computes every telemetry report field directly from live SQL queries against SQLite tables,
        partitioning categories with zero overlap and cross-checking budget spend against task execution JSON.
        """
        if date_str is None:
            from datetime import datetime, timezone
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        with self._connection() as conn:
            cur = conn.cursor()

            # 1. total_tasks: count of all rows in tasks table
            cur.execute("SELECT COUNT(*) as cnt FROM tasks")
            total_tasks = cur.fetchone()["cnt"]

            # 2. successful_executions: count of tasks in COMPLETED state
            cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'COMPLETED'")
            successful_executions = cur.fetchone()["cnt"]

            # 3. worker_failed_executions: count of tasks in WORKER_FAILED state
            cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'WORKER_FAILED'")
            worker_failed_executions = cur.fetchone()["cnt"]

            # 4. quality_rejected_executions: count of tasks in QUALITY_REJECTED state
            cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'QUALITY_REJECTED'")
            quality_rejected_executions = cur.fetchone()["cnt"]

            # 5. budget_blocked_executions: count of tasks in BLOCKED state with BUDGET_BLOCKED error reason
            cur.execute("SELECT COUNT(*) as cnt FROM tasks WHERE state = 'BLOCKED' AND error_reason LIKE 'BUDGET_BLOCKED:%'")
            budget_blocked_executions = cur.fetchone()["cnt"]

            # 6. approval_rejected_executions: count of tasks in BLOCKED state with non-budget error reason (e.g. APPROVAL_REJECTED / REVIEW_REJECTED)
            cur.execute("""
                SELECT COUNT(*) as cnt FROM tasks 
                WHERE state = 'BLOCKED' AND (error_reason NOT LIKE 'BUDGET_BLOCKED:%' OR error_reason IS NULL)
            """)
            approval_rejected_executions = cur.fetchone()["cnt"]

            # 7. total_cost: sum of all amounts in budget table
            cur.execute("SELECT SUM(amount) as total FROM budget")
            row_cost = cur.fetchone()
            total_cost = round(float(row_cost["total"]), 6) if row_cost and row_cost["total"] is not None else 0.0

            # 8. today_cumulative_spend: sum of amounts in budget table for date_str
            cur.execute("SELECT SUM(amount) as total FROM budget WHERE date_str = ?", (date_str,))
            row_today = cur.fetchone()
            today_cumulative_spend = round(float(row_today["total"]), 6) if row_today and row_today["total"] is not None else 0.0

            # 9. Genuine Independent Cost Reconciliation: recompute cost directly by extracting actual_cost and review_cost from tasks JSON
            cur.execute("""
                SELECT SUM(
                    COALESCE(json_extract(worker_result_json, '$.actual_cost'), 0.0) +
                    COALESCE(json_extract(review_result_json, '$.review_cost'), 0.0)
                ) as total FROM tasks
            """)
            row_reconciled = cur.fetchone()
            reconciled_task_cost = round(float(row_reconciled["total"]), 6) if row_reconciled and row_reconciled["total"] is not None else 0.0
            cost_reconciliation_diff = round(abs(total_cost - reconciled_task_cost), 6)

            # Derived live metrics & rates
            category_sum = successful_executions + worker_failed_executions + quality_rejected_executions + budget_blocked_executions + approval_rejected_executions
            success_rate = round(successful_executions / total_tasks, 4) if total_tasks > 0 else 0.0
            total_failures = worker_failed_executions + budget_blocked_executions + quality_rejected_executions + approval_rejected_executions
            failure_rate = round(total_failures / total_tasks, 4) if total_tasks > 0 else 0.0
            approval_total = successful_executions + approval_rejected_executions
            approval_rate = round(successful_executions / approval_total, 4) if approval_total > 0 else 0.0
            avg_cost = round(total_cost / total_tasks, 6) if total_tasks > 0 else 0.0

            return {
                "total_tasks": total_tasks,
                "successful_executions": successful_executions,
                "worker_failed_executions": worker_failed_executions,
                "budget_blocked_executions": budget_blocked_executions,
                "quality_rejected_executions": quality_rejected_executions,
                "approval_rejected_executions": approval_rejected_executions,
                "category_sum": category_sum,
                "categories_partition_total": (category_sum == total_tasks),
                "success_rate": success_rate,
                "failure_rate": failure_rate,
                "approval_rate": approval_rate,
                "total_cost": total_cost,
                "reconciled_task_cost": reconciled_task_cost,
                "cost_reconciliation_diff": cost_reconciliation_diff,
                "cost_reconciled": (cost_reconciliation_diff == 0.0),
                "average_cost_per_task": avg_cost,
                "today_cumulative_spend": today_cumulative_spend
            }
