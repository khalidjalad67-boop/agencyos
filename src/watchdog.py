import time
from typing import Dict, Any, List, Set
from src.db import Database

class OperationalWatchdog:
    """Monitors component health, tracks repeated failures, applies exponential backoff,
    temporarily disables unhealthy sources, and automatically re-enables them after cooldown.
    Never terminates the application.
    """

    def __init__(self, db: Database, failure_threshold: int = 3, base_cooldown_sec: float = 30.0):
        self.db = db
        self.failure_threshold = failure_threshold
        self.base_cooldown_sec = base_cooldown_sec
        self.consecutive_failures: Dict[str, int] = {}
        self.disabled_sources: Dict[str, float] = {}  # source -> re_enable_timestamp

    def record_success(self, source: str) -> None:
        """Resets failure count on success."""
        if source in self.consecutive_failures:
            self.consecutive_failures[source] = 0

    def record_failure(self, source: str, error_reason: str = "") -> None:
        """Increments consecutive failures and applies exponential backoff if threshold exceeded."""
        current_fails = self.consecutive_failures.get(source, 0) + 1
        self.consecutive_failures[source] = current_fails

        if current_fails >= self.failure_threshold:
            multiplier = 2 ** (current_fails - self.failure_threshold)
            cooldown = self.base_cooldown_sec * multiplier
            re_enable_at = time.time() + cooldown
            self.disabled_sources[source] = re_enable_at

            self.db.log_event("WATCHDOG_SOURCE_DISABLED", {
                "source": source,
                "consecutive_failures": current_fails,
                "cooldown_sec": cooldown,
                "re_enable_at": re_enable_at,
                "error_reason": error_reason
            })

    def is_source_enabled(self, source: str) -> bool:
        """Checks if a source is currently enabled or if its cooldown has expired."""
        now = time.time()
        if source in self.disabled_sources:
            re_enable_at = self.disabled_sources[source]
            if now >= re_enable_at:
                # Cooldown expired, re-enable
                del self.disabled_sources[source]
                self.consecutive_failures[source] = 0
                self.db.log_event("WATCHDOG_SOURCE_RE_ENABLED", {
                    "source": source,
                    "re_enabled_at": now
                })
                return True
            else:
                return False
        return True

    def get_disabled_sources(self) -> List[str]:
        """Returns list of currently disabled sources whose cooldowns have not expired."""
        now = time.time()
        active_disabled = []
        for src, re_enable_at in list(self.disabled_sources.items()):
            if now < re_enable_at:
                active_disabled.append(src)
            else:
                del self.disabled_sources[src]
                self.consecutive_failures[src] = 0
                self.db.log_event("WATCHDOG_SOURCE_RE_ENABLED", {"source": src, "re_enabled_at": now})
        return active_disabled
