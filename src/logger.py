import json
import logging
import os
import time
from typing import Dict, Any, List

class AuditLogger:
    """Structured audit log capturing every step, output, decision, and cost in the system."""
    
    def __init__(self, log_filepath: str = "audit_log.jsonl"):
        self.log_filepath = log_filepath
        self.logger = logging.getLogger("AuditLogger")
        self.logger.setLevel(logging.INFO)
        
        # Ensure log file exists or is clean
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            formatter = logging.Formatter('[%(asctime)s] %(levelname)s: %(message)s', '%Y-%m-%d %H:%M:%S')
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

    def log_event(self, event_type: str, payload: Dict[str, Any]) -> None:
        """Appends a structured event entry to audit_log.jsonl and outputs console log."""
        entry = {
            "timestamp": time.time(),
            "event_type": event_type,
            "payload": payload
        }
        
        # Write to JSONL
        with open(self.log_filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            
        # Write clean message to console logger
        self.logger.info(f"[{event_type}] {payload.get('summary', payload.get('opportunity_id', ''))}")

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
