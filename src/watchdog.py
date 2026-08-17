import os
import time
import yaml
from typing import Dict, Any, List, Set, Optional
from src.db import Database

class OperationalWatchdog:
    """Monitors component health, tracks repeated failures, applies exponential backoff,
    temporarily disables unhealthy sources, and automatically re-enables them after cooldown.
    Never terminates the application.
    """

    def __init__(
        self,
        db: Database,
        failure_threshold: Optional[int] = None,
        base_cooldown_sec: Optional[float] = None,
        config_path: str = "config/settings.yaml"
    ):
        self.db = db
        self.config_path = config_path
        cfg = self._load_config()
        watchdog_cfg = cfg.get("watchdog", {})

        self.failure_threshold = failure_threshold if failure_threshold is not None else int(watchdog_cfg.get("consecutive_failure_threshold", 3))
        self.base_cooldown_sec = base_cooldown_sec if base_cooldown_sec is not None else float(watchdog_cfg.get("base_cooldown_sec", 30.0))
        self.heartbeat_warning_multiplier = float(watchdog_cfg.get("heartbeat_warning_multiplier", 2.0))
        self.stall_threshold_multiplier = float(watchdog_cfg.get("stall_threshold_multiplier", 5.0))
        self.consecutive_failures: Dict[str, int] = {}
        self.disabled_sources: Dict[str, float] = {}  # source -> re_enable_timestamp
        self._in_warning: bool = False
        self._stall_active: bool = False

    def _load_config(self) -> Dict[str, Any]:
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}

    def _get_task_counts(self) -> Dict[str, Any]:
        tasks = self.db.get_all_tasks()
        counts: Dict[str, int] = {}
        for t in tasks:
            st = t["state"]
            counts[st] = counts.get(st, 0) + 1
        return {"task_counts": counts, "total_tasks": len(tasks)}

    def check_heartbeat(self, now: float, last_heartbeat: float, interval_sec: float) -> None:
        """Monitors heartbeat interval gaps. Emits WATCHDOG_WARNING if gap > 2x interval,
        STALL_DETECTED if gap > 5x interval, and RECOVERY_STARTED / RECOVERY_COMPLETED
        when a stall recovers. Resets _in_warning when gap returns to normal.
        """
        if last_heartbeat == 0.0:
            return  # first tick, no gap to measure
        gap = now - last_heartbeat

        if gap > self.stall_threshold_multiplier * interval_sec and not self._stall_active:
            task_counts = self._get_task_counts()
            self.db.log_event("STALL_DETECTED", {"gap_sec": gap, **task_counts})
            self._stall_active = True
            self._in_warning = True

        elif gap > self.heartbeat_warning_multiplier * interval_sec and not self._in_warning:
            self.db.log_event("WATCHDOG_WARNING", {"gap_sec": gap, "interval_sec": interval_sec})
            self._in_warning = True

        # Recovery path: gap returned to normal
        if gap <= 1.5 * interval_sec:
            if self._stall_active:
                task_counts = self._get_task_counts()
                self.db.log_event("RECOVERY_STARTED", {"gap_sec": gap, **task_counts})
                self.db.log_event("RECOVERY_COMPLETED", {"gap_sec": gap, **task_counts})
                self._stall_active = False
            # Reset warning flag unconditionally when gap normalizes
            self._in_warning = False

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
