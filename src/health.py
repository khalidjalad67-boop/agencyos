import json
from typing import Dict, Any
from src.db import Database
from src.watchdog import OperationalWatchdog

class HealthMonitor:
    """Exposes structured JSON metrics for system telemetry. No dashboard."""

    def __init__(self, db: Database, watchdog: OperationalWatchdog):
        self.db = db
        self.watchdog = watchdog

    def get_metrics(self) -> Dict[str, Any]:
        all_tasks = self.db.get_all_tasks()
        pending_approvals = self.db.get_pending_approvals()
        disabled_sources = self.watchdog.get_disabled_sources()

        queue_depth = sum(1 for t in all_tasks if t["state"] in ("NEW", "DISCOVERED", "PLANNED", "READY"))
        running_tasks = sum(1 for t in all_tasks if t["state"] == "EXECUTING")
        
        total_eval = sum(1 for t in all_tasks if t["state"] in ("COMPLETED", "FAILED", "CANCELLED", "RETRYING"))
        failed_count = sum(1 for t in all_tasks if t["state"] in ("FAILED", "CANCELLED"))
        failure_rate = round(failed_count / total_eval, 4) if total_eval > 0 else 0.0

        return {
            "queue_depth": queue_depth,
            "running_tasks": running_tasks,
            "pending_approvals": len(pending_approvals),
            "disabled_sources": disabled_sources,
            "failure_rate": failure_rate
        }

    def get_metrics_json(self) -> str:
        return json.dumps(self.get_metrics(), indent=2)
