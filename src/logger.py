import json
import logging
import os
import time
from typing import Dict, Any, List, Optional
from src.db import Database, resolve_log_path, DEFAULT_LOG_PATH

class AuditLogger:
    """Structured audit log capturing every step, output, decision, and cost in the system.
    Writes structured events to both audit_log.jsonl and SQLite audit_log table.
    """
    
    def __init__(self, log_filepath: str = DEFAULT_LOG_PATH, db: Optional[Database] = None):
        self.db = db
        self.log_filepath = self.db.log_filepath if (self.db and hasattr(self.db, "log_filepath")) else resolve_log_path(log_filepath)
        self.logger = logging.getLogger("AuditLogger")
        self.logger.setLevel(logging.INFO)
        
        # Ensure log file handlers are initialized once
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S')
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

    def write_jsonl(self, event_type: str, payload: Dict[str, Any], timestamp: Optional[float] = None) -> None:
        """Appends a structured event entry to audit_log.jsonl without writing to SQLite DB or logging to console."""
        entry = {
            "timestamp": timestamp if timestamp is not None else time.time(),
            "event_type": event_type,
            "payload": payload
        }
        try:
            with open(self.log_filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[Warning] Failed to write to {self.log_filepath}: {e}")

    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Delegates audit event logging to self.db.log_event() for unified SQLite and JSONL writes."""
        if self.db:
            try:
                self.db.log_event(event_type, payload)
            except Exception as e:
                print(f"[Warning] Failed to write audit log event: {e}")
        else:
            now = time.time()
            self.write_jsonl(event_type, payload, timestamp=now)

        # Write clean message to console logger
        summary_msg = payload.get('summary') or payload.get('task_id') or payload.get('opportunity_id') or (f"queue_depth={payload.get('queue_depth')}" if 'queue_depth' in payload else '')
        self.logger.info(f"[{event_type}] {summary_msg}")

    def read_all_logs(self) -> List[Dict[str, Any]]:
        """Reads all entries from audit_log.jsonl."""
        if not os.path.exists(self.log_filepath):
            return []
        
        entries = []
        with open(self.log_filepath, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
        return entries
