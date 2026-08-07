import os
import json
import time
import yaml
from datetime import datetime, timezone
from typing import Tuple, Dict, Any, Optional
from src.planner import TaskSpec
from src.db import Database, is_in_test_mode, resolve_log_path, DEFAULT_LOG_PATH

class BudgetGuard:
    """Pre-execution budget guard enforcing per-task ceilings and persistent daily spend limits.
    SQLite budget table is the single source of truth going forward. Audit log file read is used
    strictly as a one-time migration path for pre-existing spend when SQLite budget table is empty.
    """

    def __init__(self, config_path: str = "config/settings.yaml", log_filepath: str = DEFAULT_LOG_PATH, db: Optional[Database] = None):
        self.config_path = config_path
        self.db = db or Database(log_filepath=log_filepath)
        self.log_filepath = self.db.log_filepath if hasattr(self.db, "log_filepath") else resolve_log_path(log_filepath)
        self.config = self._load_config()

        self.per_task_limit = float(self.config.get("budget", {}).get("per_task_limit", 0.25))
        self.daily_limit = float(self.config.get("budget", {}).get("daily_limit", 2.00))
        self.hard_stop = bool(self.config.get("budget", {}).get("hard_stop", True))

        self.today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Single source of truth initialization (only in production)
        if not is_in_test_mode() and self.db.is_budget_empty() and os.path.exists(self.log_filepath):
            # One-time migration path for legacy audit_log.jsonl
            legacy_spend = self._calculate_legacy_today_spend()
            if legacy_spend > 0:
                self.db.execute_atomic_transition(
                    {
                        "task_id": "migration",
                        "opportunity_id": "migration",
                        "state": "COMPLETED",
                        "title": "Legacy audit_log spend migration"
                    },
                    spend_record=(legacy_spend, self.today_date_str, "Legacy audit_log spend migration")
                )

        self._spend_override: Optional[float] = None

    @property
    def cumulative_today_spend(self) -> float:
        """Live SQL query property returning exact sum of spend records for today's date from budget table."""
        if self._spend_override is not None:
            return self._spend_override
        return self.db.get_today_spend(self.today_date_str)

    @cumulative_today_spend.setter
    def cumulative_today_spend(self, value: float) -> None:
        self._spend_override = value

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[Warning] Failed to load {self.config_path}: {e}")
        return {"budget": {"per_task_limit": 0.25, "daily_limit": 2.00, "hard_stop": True}}

    def _calculate_legacy_today_spend(self) -> float:
        """One-time migration helper to read pre-existing spend from legacy audit_log.jsonl file."""
        if not os.path.exists(self.log_filepath):
            return 0.0

        total_spend = 0.0
        try:
            with open(self.log_filepath, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    entry = json.loads(line)
                    event_type = entry.get("event_type")
                    entry_time = entry.get("timestamp")

                    if event_type == "TASK_COMPLETED" and entry_time:
                        entry_date = datetime.fromtimestamp(entry_time, timezone.utc).strftime("%Y-%m-%d")
                        if entry_date == self.today_date_str:
                            cost = float(entry.get("payload", {}).get("cost", 0.0))
                            total_spend += cost
        except Exception as e:
            print(f"[Warning] Failed to read legacy spend: {e}")

        return round(total_spend, 6)

    def check_budget(self, task_spec: TaskSpec) -> Tuple[bool, str]:
        """Pre-execution check against TaskSpec estimated_cost before Worker invocation."""
        current_spend = self.cumulative_today_spend

        # 1. Per-task ceiling check
        if task_spec.estimated_cost > self.per_task_limit:
            return False, f"Estimated cost (${task_spec.estimated_cost:.4f}) exceeds per-task ceiling (${self.per_task_limit:.2f})"

        # 2. Persistent daily limit check
        if current_spend + task_spec.estimated_cost > self.daily_limit:
            return False, f"Estimated cost (${task_spec.estimated_cost:.4f}) + Today Spend (${current_spend:.4f}) exceeds daily limit (${self.daily_limit:.2f})"

        return True, "Budget approved"

    def record_spend(self, cost: float, opportunity_id: str = "unknown") -> None:
        """Records spend directly into the single source of truth SQLite budget table."""
        self.db.record_spend(opportunity_id, cost, self.today_date_str, "Task spend")
