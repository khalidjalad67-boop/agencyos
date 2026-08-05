import os
import sys
import time
import json
import ast
import sqlite3
import unittest
from typing import Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import Database, resolve_db_path, LEGAL_TRANSITIONS, TERMINAL_STATES, InvalidStateTransitionError
from src.opportunity import Opportunity
from src.scheduler import Scheduler
from src.watchdog import OperationalWatchdog
from src.engine import AutonomousEngine
from src.planner import TaskSpec
from src.worker import WorkerResult
from src.reviewer import ReviewResult

TEST_DB_FILE = "test_phase0_8b_sm.db"
TEST_LOG_FILE = "test_phase0_8b_sm_audit.jsonl"

class TestPhase08BStateMachine(unittest.TestCase):

    def setUp(self):
        target_db = resolve_db_path(TEST_DB_FILE)
        if os.path.exists(target_db):
            os.remove(target_db)
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)
        self.db = Database(TEST_DB_FILE)

    def tearDown(self):
        target_db = resolve_db_path(TEST_DB_FILE)
        if os.path.exists(target_db):
            os.remove(target_db)
        if os.path.exists(TEST_LOG_FILE):
            os.remove(TEST_LOG_FILE)

    # -------------------------------------------------------------------------
    # 1. AST SOURCE CONFORMANCE TEST
    # -------------------------------------------------------------------------
    def test_legal_transitions_covers_all_engine_calls(self):
        """Asserts every task["state"] literal written in engine.py and recovery.py
        is a registered value (appears as a target) in LEGAL_TRANSITIONS.

        LIMITATION: this checks that every to_state is *somewhere* in LEGAL_TRANSITIONS,
        not that the specific (from_state -> to_state) pair is legal. REVIEW -> REVIEW
        is a live example of a bug this test would NOT have caught: 'REVIEW' already
        appears as a target of EXECUTING, so the simplified check passes even if
        REVIEW -> REVIEW were missing. This class of bug (correct target state, wrong
        source state) requires manual transition-table review -- this test is a complement,
        not a substitute for that.
        """
        all_target_states: Set[str] = set()
        for legal_set in LEGAL_TRANSITIONS.values():
            all_target_states.update(legal_set)

        for filename in ("engine.py", "recovery.py"):
            filepath = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", filename)
            with open(filepath, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=filename)

            written_states = set()
            for node in ast.walk(tree):
                # Look for Subscript assignment: task["state"] = "LITERAL"
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Subscript):
                            if isinstance(target.slice, ast.Constant) and target.slice.value == "state":
                                if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                                    written_states.add(node.value.value)

            for state in written_states:
                self.assertIn(
                    state, all_target_states,
                    f"State '{state}' assigned in src/{filename} is not registered in any LEGAL_TRANSITIONS target set!"
                )

    # -------------------------------------------------------------------------
    # 2. STATE MACHINE BEHAVIOURAL TESTS
    # -------------------------------------------------------------------------
    def test_terminal_state_blocks_further_transition(self):
        """COMPLETED state cannot move to any further state."""
        task_id = "term-test-1"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "READY"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "WAITING_APPROVAL"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "COMPLETED"
        })

        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({
                "task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"
            })

    def test_terminal_state_logs_illegal_transition_event(self):
        """Illegal transition attempt logs an ILLEGAL_TRANSITION audit event."""
        task_id = "term-test-log"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "QUALITY_REJECTED"
        })

        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({
                "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
            })

        logs = self.db.get_audit_logs(limit=10)
        illegal_logs = [l for l in logs if l["event_type"] == "ILLEGAL_TRANSITION"]
        self.assertEqual(len(illegal_logs), 1)
        self.assertEqual(illegal_logs[0]["payload"]["task_id"], task_id)
        self.assertEqual(illegal_logs[0]["payload"]["from_state"], "QUALITY_REJECTED")
        self.assertEqual(illegal_logs[0]["payload"]["to_state"], "PLANNED")

    def test_illegal_non_terminal_skipping_transition(self):
        """PLANNED state cannot skip READY/EXECUTING and jump directly to REVIEW."""
        task_id = "skip-test-1"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
        })

        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({
                "task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"
            })

    def test_legal_full_happy_path(self):
        """Full lifecycle DISCOVERED -> PLANNED -> READY -> EXECUTING -> REVIEW -> WAITING_APPROVAL -> COMPLETED."""
        task_id = "happy-path-1"
        states = ["DISCOVERED", "PLANNED", "READY", "EXECUTING", "REVIEW", "WAITING_APPROVAL", "COMPLETED"]
        for st in states:
            self.db.execute_atomic_transition({
                "task_id": task_id, "opportunity_id": task_id, "state": st
            })
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "COMPLETED")

    def test_planned_to_blocked_is_legal(self):
        """Budget check failure moves task from PLANNED to BLOCKED."""
        task_id = "budget-blocked-1"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "BLOCKED"
        })
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "BLOCKED")

    def test_executing_self_transition_is_legal(self):
        """EXECUTING -> EXECUTING (e.g. WORKER_EXECUTED event) is legal."""
        task_id = "exec-self-1"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "READY"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"
        })
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "EXECUTING")

    def test_review_self_transition_is_legal(self):
        """REVIEW -> REVIEW (crash-recovery resume path: recovery L50 -> engine L228) is legal."""
        task_id = "rev-self-1"
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "READY"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"
        })
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"
        })
        # Fresh review after crash recovery
        self.db.execute_atomic_transition({
            "task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"
        })
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "REVIEW")

    def test_waiting_approval_self_transition_is_legal(self):
        """WAITING_APPROVAL -> WAITING_APPROVAL (restart re-entry path L250) is legal."""
        task_id = "wa-self-1"
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "READY"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "WAITING_APPROVAL"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "WAITING_APPROVAL"})
        task = self.db.get_task(task_id)
        self.assertEqual(task["state"], "WAITING_APPROVAL")

    def test_quality_rejected_is_terminal(self):
        task_id = "qr-term-1"
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "QUALITY_REJECTED"})
        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "READY"})

    def test_blocked_is_terminal(self):
        task_id = "bl-term-1"
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "BLOCKED"})
        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "COMPLETED"})

    def test_worker_failed_is_terminal(self):
        task_id = "wf-term-1"
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "DISCOVERED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "PLANNED"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "READY"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "EXECUTING"})
        self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "WORKER_FAILED"})
        with self.assertRaises(InvalidStateTransitionError):
            self.db.execute_atomic_transition({"task_id": task_id, "opportunity_id": task_id, "state": "REVIEW"})

    # -------------------------------------------------------------------------
    # 3. WATCHDOG HEARTBEAT & STALL RECOVERY TESTS
    # -------------------------------------------------------------------------
    def test_watchdog_warning_fires_at_2x_interval(self):
        watchdog = OperationalWatchdog(self.db)
        now = 1000.0
        last_hb = now - 70.0  # gap 70s > 2 * 30s = 60s
        watchdog.check_heartbeat(now, last_hb, interval_sec=30.0)

        logs = self.db.get_audit_logs(limit=10)
        warn_logs = [l for l in logs if l["event_type"] == "WATCHDOG_WARNING"]
        self.assertEqual(len(warn_logs), 1)
        self.assertEqual(warn_logs[0]["payload"]["gap_sec"], 70.0)

    def test_watchdog_warning_does_not_duplicate_within_same_gap(self):
        watchdog = OperationalWatchdog(self.db)
        now = 1000.0
        last_hb = now - 70.0
        watchdog.check_heartbeat(now, last_hb, interval_sec=30.0)
        # Second tick while still in warning gap
        watchdog.check_heartbeat(now + 10.0, last_hb, interval_sec=30.0)

        logs = self.db.get_audit_logs(limit=10)
        warn_logs = [l for l in logs if l["event_type"] == "WATCHDOG_WARNING"]
        self.assertEqual(len(warn_logs), 1)

    def test_stall_detected_fires_at_5x_interval(self):
        watchdog = OperationalWatchdog(self.db)
        now = 1000.0
        last_hb = now - 160.0  # gap 160s > 5 * 30s = 150s
        watchdog.check_heartbeat(now, last_hb, interval_sec=30.0)

        logs = self.db.get_audit_logs(limit=10)
        stall_logs = [l for l in logs if l["event_type"] == "STALL_DETECTED"]
        self.assertEqual(len(stall_logs), 1)
        self.assertEqual(stall_logs[0]["payload"]["gap_sec"], 160.0)

    def test_recovery_events_fire_after_stall_clears(self):
        watchdog = OperationalWatchdog(self.db)
        # Trigger stall
        watchdog.check_heartbeat(1000.0, 840.0, interval_sec=30.0)  # gap 160s
        self.assertTrue(watchdog._stall_active)

        # Gap normalizes to 30s
        watchdog.check_heartbeat(1030.0, 1000.0, interval_sec=30.0)
        self.assertFalse(watchdog._stall_active)

        logs = self.db.get_audit_logs(limit=10)
        start_logs = [l for l in logs if l["event_type"] == "RECOVERY_STARTED"]
        comp_logs = [l for l in logs if l["event_type"] == "RECOVERY_COMPLETED"]
        self.assertEqual(len(start_logs), 1)
        self.assertEqual(len(comp_logs), 1)

    def test_warning_flag_resets_without_full_stall(self):
        """Proves that a warning gap (2x-5x) that normalizes clears _in_warning so a subsequent
        warning gap in the same session fires a new WATCHDOG_WARNING event.
        """
        watchdog = OperationalWatchdog(self.db)
        # 1. Trigger warning (gap 70s)
        watchdog.check_heartbeat(1000.0, 930.0, interval_sec=30.0)
        self.assertTrue(watchdog._in_warning)
        self.assertFalse(watchdog._stall_active)

        # 2. Gap normalizes (30s) -> should reset _in_warning to False
        watchdog.check_heartbeat(1030.0, 1000.0, interval_sec=30.0)
        self.assertFalse(watchdog._in_warning)

        # 3. Trigger warning again (gap 70s) -> MUST fire a second WATCHDOG_WARNING
        watchdog.check_heartbeat(1100.0, 1030.0, interval_sec=30.0)

        logs = self.db.get_audit_logs(limit=10)
        warn_logs = [l for l in logs if l["event_type"] == "WATCHDOG_WARNING"]
        self.assertEqual(len(warn_logs), 2, "Second warning MUST fire after gap normalizes in between!")

    def test_no_recovery_events_when_only_warning_clears(self):
        """Proves RECOVERY_STARTED and RECOVERY_COMPLETED ONLY fire if _stall_active was True,
        not when a simple warning clears.
        """
        watchdog = OperationalWatchdog(self.db)
        # Trigger warning (gap 70s)
        watchdog.check_heartbeat(1000.0, 930.0, interval_sec=30.0)
        # Normalize
        watchdog.check_heartbeat(1030.0, 1000.0, interval_sec=30.0)

        logs = self.db.get_audit_logs(limit=10)
        rec_logs = [l for l in logs if l["event_type"] in ("RECOVERY_STARTED", "RECOVERY_COMPLETED")]
        self.assertEqual(len(rec_logs), 0, "Recovery events MUST NOT fire when clearing a warning-only gap!")

    # -------------------------------------------------------------------------
    # 4. SCHEDULER IDLE BACKOFF TESTS
    # -------------------------------------------------------------------------
    def test_idle_backoff_doubles_interval(self):
        scheduler = Scheduler(self.db, interval_sec=30.0, max_interval_sec=300.0)
        # Tick 1: idle -> interval 60s
        ids1, interval1 = scheduler.tick(opportunities_override=[])
        self.assertEqual(ids1, [])
        self.assertEqual(interval1, 60.0)

        # Tick 2: idle -> interval 120s
        ids2, interval2 = scheduler.tick(opportunities_override=[])
        self.assertEqual(ids2, [])
        self.assertEqual(interval2, 120.0)

    def test_active_tick_resets_to_base(self):
        scheduler = Scheduler(self.db, interval_sec=30.0, max_interval_sec=300.0)
        # Backoff on idle ticks
        scheduler.tick(opportunities_override=[])
        scheduler.tick(opportunities_override=[])
        self.assertEqual(scheduler._current_interval, 120.0)

        # Active tick with a new opportunity
        opp = Opportunity(id="active-opp-1", title="New Task", description="Desc", source="src", payload={})
        scheduled, next_interval = scheduler.tick(opportunities_override=[opp])
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(next_interval, 30.0)

    def test_backoff_ceiling_respected(self):
        scheduler = Scheduler(self.db, interval_sec=30.0, max_interval_sec=100.0)
        for _ in range(10):
            scheduler.tick(opportunities_override=[])
        self.assertEqual(scheduler._current_interval, 100.0)

    def test_tick_return_is_tuple(self):
        scheduler = Scheduler(self.db, interval_sec=30.0)
        res = scheduler.tick(opportunities_override=[])
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2)
        self.assertIsInstance(res[0], list)
        self.assertIsInstance(res[1], float)

if __name__ == "__main__":
    unittest.main()
