import os
import sqlite3
import json
import time
from contextlib import contextmanager
from typing import Dict, Any, List, Optional, Tuple

DEFAULT_DB_PATH = "agencyos.db"

class Database:
    """Persistent SQLite Database Manager handling tasks, approvals, idempotency_keys, audit_log, and budget tables."""

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self):
        """Initializes database schema with required tables if they do not exist."""
        with self._connection() as conn:
            cursor = conn.cursor()
            
            # 1. tasks table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source TEXT,
                    repo TEXT,
                    title TEXT,
                    description TEXT,
                    payload_json TEXT,
                    task_spec_json TEXT,
                    worker_result_json TEXT,
                    review_result_json TEXT,
                    error_reason TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)

            # 2. approvals table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    opportunity_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at REAL NOT NULL,
                    decided_at REAL,
                    comments TEXT
                )
            """)

            # 3. idempotency_keys table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    key TEXT PRIMARY KEY,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)

            # 4. audit_log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    timestamp REAL NOT NULL
                )
            """)

            # 5. budget table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS budget (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    opportunity_id TEXT,
                    amount REAL NOT NULL,
                    date_str TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    description TEXT
                )
            """)
            
            conn.commit()

    # --- TASKS METHODS ---
    def save_task(self, task_data: Dict[str, Any]) -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
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
            conn.commit()

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

    def update_task_state(self, task_id: str, new_state: str, error_reason: str = "") -> None:
        now = time.time()
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tasks SET state = ?, error_reason = ?, updated_at = ? WHERE task_id = ?
            """, (new_state, error_reason, now, task_id))
            conn.commit()

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
