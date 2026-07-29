import os
import json
import time
import yaml
from datetime import datetime, timezone
from typing import Tuple, Dict, Any
from src.planner import TaskSpec

class BudgetGuard:
    """Pre-execution budget guard enforcing per-task ceilings and persistent daily spend limits."""
    
    def __init__(self, config_path: str = "config/settings.yaml", log_filepath: str = "audit_log.jsonl"):
        self.config_path = config_path
        self.log_filepath = log_filepath
        self.config = self._load_config()
        
        self.per_task_limit = float(self.config.get("budget", {}).get("per_task_limit", 0.25))
        self.daily_limit = float(self.config.get("budget", {}).get("daily_limit", 2.00))
        self.hard_stop = bool(self.config.get("budget", {}).get("hard_stop", True))
        
        # Initialize persistent today spend from audit_log.jsonl
        self.today_date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.cumulative_today_spend = self._calculate_today_spend_from_log()

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception as e:
                print(f"[Warning] Failed to load {self.config_path}: {e}")
        return {"budget": {"per_task_limit": 0.25, "daily_limit": 2.00, "hard_stop": True}}

    def _calculate_today_spend_from_log(self) -> float:
        """Persistently reads cumulative spend for current day across script executions."""
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
                    
                    if event_type in ("TASK_COMPLETED", "WORKER_EXECUTED") and entry_time:
                        entry_date = datetime.fromtimestamp(entry_time, timezone.utc).strftime("%Y-%m-%d")
                        if entry_date == self.today_date_str:
                            cost = float(entry.get("payload", {}).get("cost", entry.get("payload", {}).get("worker_result", {}).get("actual_cost", 0.0)))
                            if event_type == "TASK_COMPLETED":
                                total_spend += cost
        except Exception as e:
            print(f"[Warning] Failed to read cumulative spend from audit log: {e}")
            
        return round(total_spend, 6)

    def check_budget(self, task_spec: TaskSpec) -> Tuple[bool, str]:
        """Pre-execution check against TaskSpec estimated_cost before Worker invocation."""
        # 1. Per-task ceiling check
        if task_spec.estimated_cost > self.per_task_limit:
            return False, f"Estimated cost (${task_spec.estimated_cost:.4f}) exceeds per-task ceiling (${self.per_task_limit:.2f})"
            
        # 2. Persistent daily limit check
        if self.cumulative_today_spend + task_spec.estimated_cost > self.daily_limit:
            return False, f"Estimated cost (${task_spec.estimated_cost:.4f}) + Today Spend (${self.cumulative_today_spend:.4f}) exceeds daily limit (${self.daily_limit:.2f})"
            
        return True, "Budget approved"

    def record_spend(self, cost: float) -> None:
        """Updates in-memory today spend tracker when task completes."""
        self.cumulative_today_spend = round(self.cumulative_today_spend + cost, 6)
