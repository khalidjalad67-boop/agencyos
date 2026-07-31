import json
import logging
import os
import time
from typing import Dict, Any, List, Optional
from src.db import Database

class AuditLogger:
    """Structured audit log capturing every step, output, decision, and cost in the system.
    Writes structured events to both audit_log.jsonl and SQLite audit_log table.
    """
    
    def __init__(self, log_filepath: str = "audit_log.jsonl", db: Optional[Database] = None):
        self.log_filepath = log_filepath
        self.db = db
        self.logger = logging.getLogger("AuditLogger")
        self.logger.setLevel(logging.INFO)
        
        # Ensure log file handlers are initialized once
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S')
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Appends a structured event entry to audit_log.jsonl and SQLite audit_log table."""
        now = time.time()
        entry = {
            "timestamp": now,
            "event_type": event_type,
            "payload": payload
        }
        
        # 1. Write to JSONL
        try:
            with open(self.log_filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            print(f"[Warning] Failed to write to {self.log_filepath}: {e}")
            
        # 2. Write to SQLite audit_log table
        if self.db:
            try:
                self.db.log_event(event_type, payload)
            except Exception as e:
                print(f"[Warning] Failed to write audit log to SQLite: {e}")

        # 3. Write clean message to console logger
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
