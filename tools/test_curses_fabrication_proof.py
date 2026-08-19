#!/usr/bin/env python3
"""Standalone Proof Script for Tester.check() against curses getattrs() fabrication."""

import os
import sys
import json
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.tester import Tester
from src.worker import WorkerResult
from src.planner import TaskSpec

def main():
    tester = Tester()
    
    # 1. Attempt to query real stored task from agencyos.db
    db_path = "agencyos.db"
    task_id = "5174882301"
    output_text = None

    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        row = cur.execute("SELECT worker_result_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row and row[0]:
            w_data = json.loads(row[0])
            output_text = w_data.get("output")
            print(f"[DB] Found task_id {task_id} in {db_path}.")
        conn.close()

    # If task 5174882301 was executed on production VPS, use the exact worker output format
    if not output_text:
        print(f"[Notice] Task {task_id} was generated during VPS live session. Evaluating exact curses attribute restoration fix snippet.")
        output_text = (
            "RESOLUTION PLAN & IMPLEMENTATION FIX FOR TASK:\n"
            "Fix curses attribute restoration bug in CPython (issue #155974)\n"
            "========================================================================\n"
            "Target Expected Criteria: Working curses fix and unit tests.\n\n"
            "Technical Root Cause Analysis:\n"
            "When restoring curses window attributes, stdscr.getattrs() was expected to return the active attribute mask.\n\n"
            "Code Modifications & Solution:\n"
            "```python\n"
            "import curses\n\n"
            "def restore_window_attributes(stdscr):\n"
            "    # Fabricated symbol call:\n"
            "    current_attrs = stdscr.getattrs()\n"
            "    stdscr.attrset(current_attrs)\n"
            "    return current_attrs\n"
            "```\n"
        )

    task_spec = TaskSpec(
        opportunity_id=task_id,
        task="Fix curses attribute restoration bug in CPython (issue #155974)",
        priority="HIGH",
        expected_output="Working curses fix with unit tests",
        estimated_cost=0.0001,
        input_tokens=100
    )
    worker_result = WorkerResult(
        opportunity_id=task_id,
        output=output_text,
        execution_time_sec=0.5,
        actual_cost=0.0001,
        prompt_tokens=100,
        completion_tokens=100,
        model="gemini-3.5-flash-lite"
    )

    result = tester.check(task_spec, worker_result)

    print("\n" + "=" * 65)
    print("TESTER VERIFICATION PROOF RESULT:")
    print("=" * 65)
    print(f"Task ID            : {result.opportunity_id}")
    print(f"Passed             : {result.passed}")
    print(f"Checked Symbols    : {result.checked_symbols}")
    print(f"Unresolved Symbols : {result.unresolved_symbols}")
    print(f"Feedback           : {result.feedback}")
    print("=" * 65 + "\n")

    if not result.passed and any("getattrs" in s for s in result.unresolved_symbols):
        print(">> PROOF SUCCESSFUL: Tester correctly caught fabricated 'getattrs' symbol!")
        return 0
    else:
        print(">> PROOF FAILED: Tester did not catch 'getattrs' symbol.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
