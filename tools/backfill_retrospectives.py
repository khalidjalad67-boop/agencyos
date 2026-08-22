#!/usr/bin/env python3
"""AgencyOS Retrospectives Backfill Script.

Backfills existing historical tasks in terminal states (COMPLETED, BLOCKED,
QUALITY_REJECTED, WORKER_FAILED) into the new retrospectives table.

Usage (on VPS):
    python3 -m tools.backfill_retrospectives --db agencyos.db
"""

import sys
import argparse
from typing import Optional
from src.db import Database
from src.migrations import run_migrations
from src.retrospective import generate_retrospective_for_task

def run_backfill(db_path: str = "agencyos.db") -> None:
    """Applies migration 002 if needed, scans all terminal tasks, and populates retrospectives."""
    print(f"[BACKFILL] Ensuring migrations are up to date on {db_path}...")
    run_migrations(db_path=db_path)

    db = Database(db_path=db_path)
    with db._connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT task_id, state, repo
            FROM tasks
            WHERE state IN ('COMPLETED', 'BLOCKED', 'QUALITY_REJECTED', 'WORKER_FAILED')
            ORDER BY created_at ASC
        """)
        rows = cursor.fetchall()

    total_tasks = len(rows)
    print(f"[BACKFILL] Found {total_tasks} terminal tasks to process.")

    backfilled_count = 0
    outcome_counts = {}

    for row in rows:
        task_id = row["task_id"]
        retro = generate_retrospective_for_task(task_id, db)
        if retro:
            backfilled_count += 1
            outcome = retro["outcome"]
            outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    print("=" * 60)
    print(f"[BACKFILL COMPLETE] Processed {backfilled_count}/{total_tasks} retrospective records.")
    for outcome, cnt in sorted(outcome_counts.items()):
        print(f"  - {outcome:<18}: {cnt}")
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="AgencyOS Retrospectives Backfill Tool")
    parser.add_argument("--db", default="agencyos.db", help="Path to target SQLite database (default: agencyos.db)")
    args = parser.parse_args()

    run_backfill(db_path=args.db)

if __name__ == "__main__":
    main()
