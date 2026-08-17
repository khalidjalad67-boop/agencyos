import os
import time
import yaml
from typing import List, Optional, Tuple, Dict, Any
from src.db import Database
from src.opportunity import OpportunityFetcher, Opportunity
from src.logger import AuditLogger
from src.watchdog import OperationalWatchdog

class Scheduler:
    """Configurable scheduler (default: 30s) that creates execution events in SQLite.
    Never executes work directly; prevents duplicate scheduling.
    """

    def __init__(
        self,
        db: Database,
        interval_sec: Optional[float] = None,
        max_interval_sec: Optional[float] = None,
        fetcher: Optional[OpportunityFetcher] = None,
        watchdog: Optional[OperationalWatchdog] = None,
        logger: Optional[AuditLogger] = None,
        config_path: str = "config/settings.yaml"
    ):
        self.db = db
        self.config_path = config_path
        cfg = self._load_config()
        sched_cfg = cfg.get("scheduler", {})

        self.interval_sec = interval_sec if interval_sec is not None else float(sched_cfg.get("interval_sec", 30.0))
        self.max_interval_sec = max_interval_sec if max_interval_sec is not None else float(sched_cfg.get("max_idle_interval_sec", 240.0))
        self.backoff_multiplier = float(sched_cfg.get("idle_backoff_multiplier", 2.0))
        self._current_interval: float = self.interval_sec
        self.fetcher = fetcher or OpportunityFetcher()
        self.watchdog = watchdog
        self.logger = logger or AuditLogger(db=self.db)
        self.last_tick_time: float = 0.0

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def tick(self, opportunities_override: Optional[List[Opportunity]] = None, limit: int = 105) -> Tuple[List[str], float]:
        """Polls opportunity source and creates DISCOVERED task records in SQLite DB.
        Returns tuple of (newly scheduled task IDs list, recommended next interval sec).
        """
        now = time.time()
        prev_tick = self.last_tick_time
        self.last_tick_time = now

        # Check watchdog heartbeat for scheduler tick using current expected interval
        if prev_tick > 0:
            if self.watchdog:
                self.watchdog.check_heartbeat(now, prev_tick, self._current_interval)

        # Compute current queue depth (tasks in NEW, DISCOVERED, PLANNED, READY states)
        all_tasks = self.db.get_all_tasks()
        queue_depth = sum(1 for t in all_tasks if t["state"] in ("NEW", "DISCOVERED", "PLANNED", "READY"))

        # Log SCHEDULER_HEARTBEAT on every scheduler tick to audit_log.jsonl and SQLite audit_log table
        self.logger.log_event("SCHEDULER_HEARTBEAT", {
            "timestamp": now,
            "queue_depth": queue_depth,
            "expected_interval": self._current_interval
        })

        if opportunities_override is not None:
            opps = opportunities_override
        else:
            try:
                opps = self.fetcher.fetch_opportunities(limit=limit)
            except Exception as e:
                self.db.log_event("SCHEDULER_FETCH_ERROR", {"error": str(e)})
                if queue_depth == 0:
                    self._current_interval = min(self._current_interval * 2.0, self.max_interval_sec)
                else:
                    self._current_interval = self.interval_sec
                return [], self._current_interval

        scheduled_task_ids = []
        terminal_states = {"COMPLETED", "BLOCKED", "QUALITY_REJECTED", "WORKER_FAILED"}
        active_states = {"DISCOVERED", "PLANNED", "READY", "EXECUTING", "REVIEW", "WAITING_APPROVAL", "APPROVED", "DELIVERED"}

        for opp in opps:
            task_id = opp.id
            existing = self.db.get_task(task_id)
            if existing is not None:
                current_state = existing.get("state")
                if current_state in terminal_states or current_state in active_states:
                    # Explicitly skip scheduling tasks already in terminal or active states
                    continue

            task_data = {
                "task_id": task_id,
                "opportunity_id": opp.id,
                "state": "DISCOVERED",
                "source": opp.source,
                "repo": opp.payload.get("repo", "unknown"),
                "title": opp.title,
                "description": opp.description,
                "payload": opp.payload,
                "created_at": now,
                "updated_at": now
            }
            self.db.execute_atomic_transition(
                task_data,
                audit_event=("SCHEDULER_TASK_DISCOVERED", {
                    "task_id": task_id,
                    "opportunity_id": opp.id,
                    "title": opp.title,
                    "source": opp.source
                })
            )
            scheduled_task_ids.append(task_id)

        # Idle backoff logic: multiply interval if queue_depth == 0 and 0 scheduled tasks
        if queue_depth == 0 and len(scheduled_task_ids) == 0:
            self._current_interval = min(self._current_interval * self.backoff_multiplier, self.max_interval_sec)
        else:
            self._current_interval = self.interval_sec

        return scheduled_task_ids, self._current_interval
