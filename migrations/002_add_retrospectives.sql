-- AgencyOS Migration 002: Add Retrospectives Table
-- Stores deterministic post-execution metrics (cost, outcome, rejection source, detector flags) per terminal task.

CREATE TABLE IF NOT EXISTS retrospectives (
    task_id TEXT PRIMARY KEY,
    repo TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('COMPLETED', 'BLOCKED', 'QUALITY_REJECTED', 'WORKER_FAILED')),
    review_method TEXT,
    cost REAL NOT NULL DEFAULT 0.0,
    rejection_source TEXT,
    hedge_flagged INTEGER NOT NULL DEFAULT 0,
    stub_flagged INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_retrospectives_outcome ON retrospectives(outcome);
CREATE INDEX IF NOT EXISTS idx_retrospectives_repo ON retrospectives(repo);
CREATE INDEX IF NOT EXISTS idx_retrospectives_created_at ON retrospectives(created_at);
