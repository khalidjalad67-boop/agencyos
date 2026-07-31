import time
from typing import List, Optional
from src.db import Database
from src.opportunity import OpportunityFetcher, Opportunity

from src.logger import AuditLogger

class Scheduler:
    """Configurable scheduler (default: 30s) that creates execution events in SQLite.
    Never executes work directly; prevents duplicate scheduling.
    """

    def __init__(self, db: Database, interval_sec: float = 30.0, fetcher: Optional[OpportunityFetcher] = None, logger: Optional[AuditLogger] = None):
        self.db = db
        self.interval_sec = interval_sec
        self.fetcher = fetcher or OpportunityFetcher()
        self.logger = logger or AuditLogger(db=self.db)
        self.last_tick_time: float = 0.0

    def tick(self, opportunities_override: Optional[List[Opportunity]] = None, limit: int = 105) -> List[str]:
        """Polls opportunity source and creates DISCOVERED task records in SQLite DB.
        Returns list of newly scheduled task IDs.
        """
        now = time.time()
        self.last_tick_time = now

        # Compute current queue depth (tasks in NEW, DISCOVERED, PLANNED, READY states)
        all_tasks = self.db.get_all_tasks()
        queue_depth = sum(1 for t in all_tasks if t["state"] in ("NEW", "DISCOVERED", "PLANNED", "READY"))

        # Log SCHEDULER_HEARTBEAT on every scheduler tick to audit_log.jsonl and SQLite audit_log table
        self.logger.log_event("SCHEDULER_HEARTBEAT", {
            "timestamp": now,
            "queue_depth": queue_depth
        })

        if opportunities_override is not None:
            opps = opportunities_override
        else:
            try:
                opps = self.fetcher.fetch_opportunities(limit=limit)
            except Exception as e:
                self.db.log_event("SCHEDULER_FETCH_ERROR", {"error": str(e)})
                return []

        scheduled_task_ids = []
        for opp in opps:
            task_id = opp.id
            existing = self.db.get_task(task_id)
            if existing is None:
                task_data = {
                    "task_id": task_id,
                    "opportunity_id": opp.id,
                    "state": "DISCOVERED",
                    "source": opp.source,
                    "repo": opp.payload.get("repo", "unknown"),
                    "title": opp.title,
                    "description": opp.description,
                    "payload": opp.payload,
                    "created_at": now
                }
                self.db.save_task(task_data)
                self.db.log_event("SCHEDULER_TASK_DISCOVERED", {
                    "task_id": task_id,
                    "opportunity_id": opp.id,
                    "title": opp.title,
                    "source": opp.source
                })
                scheduled_task_ids.append(task_id)

        return scheduled_task_ids
