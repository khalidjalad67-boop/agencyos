-- AgencyOS Migration 001: Initial Schema & Constraints
-- Enforces explicit state machine CHECK constraint, UNIQUE(opportunity_id),
-- FOREIGN KEY ON DELETE RESTRICT on budget and approvals, and indexes.
-- Quarantines legacy orphaned records before applying constraints.

PRAGMA foreign_keys = OFF;

-- 1. Create _legacy_orphaned_rows quarantine table
CREATE TABLE IF NOT EXISTS _legacy_orphaned_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table TEXT NOT NULL,
    original_row_id TEXT,
    raw_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    quarantined_at REAL NOT NULL
);

-- 2. Ensure legacy tasks table exists with full schema before migrating
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    state TEXT NOT NULL,
    source TEXT,
    repo TEXT,
    title TEXT,
    description TEXT,
    payload_json TEXT,
    task_spec_json TEXT,
    worker_result_json TEXT,
    review_result_json TEXT,
    error_reason TEXT,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

-- 3. Ensure legacy budget table exists
CREATE TABLE IF NOT EXISTS budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT,
    amount REAL NOT NULL,
    date_str TEXT NOT NULL,
    timestamp REAL NOT NULL,
    description TEXT
);

-- 4. Ensure legacy approvals table exists
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    decided_at REAL,
    comments TEXT
);

-- 5. Quarantine all orphan budget records (opportunity_id NOT IN tasks or NULL)
INSERT INTO _legacy_orphaned_rows (source_table, original_row_id, raw_json, reason, quarantined_at)
SELECT 
    'budget', 
    CAST(id AS TEXT), 
    json_object('id', id, 'opportunity_id', opportunity_id, 'amount', amount, 'date_str', date_str, 'timestamp', timestamp, 'description', description), 
    'Orphan budget record referencing missing task', 
    strftime('%s', 'now')
FROM budget 
WHERE opportunity_id IS NULL 
   OR opportunity_id NOT IN (SELECT opportunity_id FROM tasks);

-- 6. Quarantine all orphan approval records (opportunity_id NOT IN tasks or NULL)
INSERT INTO _legacy_orphaned_rows (source_table, original_row_id, raw_json, reason, quarantined_at)
SELECT 
    'approvals', 
    id, 
    json_object('id', id, 'opportunity_id', opportunity_id, 'status', status, 'requested_at', requested_at, 'decided_at', decided_at, 'comments', comments), 
    'Orphan approval record referencing missing task', 
    strftime('%s', 'now')
FROM approvals 
WHERE opportunity_id IS NULL 
   OR opportunity_id NOT IN (SELECT opportunity_id FROM tasks);

-- 7. Re-create tasks table with strict DDL constraints
CREATE TABLE tasks_new (
    task_id TEXT PRIMARY KEY,
    opportunity_id TEXT UNIQUE NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('DISCOVERED','PLANNED','READY','EXECUTING','REVIEW','WAITING_APPROVAL','APPROVED','DELIVERED','COMPLETED','BLOCKED','QUALITY_REJECTED','WORKER_FAILED')),
    source TEXT,
    repo TEXT,
    title TEXT,
    description TEXT,
    payload_json TEXT,
    task_spec_json TEXT,
    worker_result_json TEXT,
    review_result_json TEXT,
    error_reason TEXT,
    created_at REAL NOT NULL DEFAULT 0.0,
    updated_at REAL NOT NULL DEFAULT 0.0
);

INSERT INTO tasks_new (task_id, opportunity_id, state, source, repo, title, description, payload_json, task_spec_json, worker_result_json, review_result_json, error_reason, created_at, updated_at)
SELECT task_id, opportunity_id, state, source, repo, title, description, payload_json, task_spec_json, worker_result_json, review_result_json, error_reason, created_at, updated_at 
FROM tasks 
WHERE state IN ('DISCOVERED','PLANNED','READY','EXECUTING','REVIEW','WAITING_APPROVAL','APPROVED','DELIVERED','COMPLETED','BLOCKED','QUALITY_REJECTED','WORKER_FAILED');

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

-- 8. Re-create approvals table with Foreign Key ON DELETE RESTRICT
CREATE TABLE approvals_new (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_at REAL NOT NULL,
    decided_at REAL,
    comments TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES tasks(opportunity_id) ON DELETE RESTRICT
);

INSERT INTO approvals_new (id, opportunity_id, status, requested_at, decided_at, comments)
SELECT id, opportunity_id, status, requested_at, decided_at, comments 
FROM approvals 
WHERE opportunity_id IS NOT NULL 
  AND opportunity_id IN (SELECT opportunity_id FROM tasks);

DROP TABLE approvals;
ALTER TABLE approvals_new RENAME TO approvals;

-- 9. Re-create budget table with Foreign Key ON DELETE RESTRICT
CREATE TABLE budget_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    amount REAL NOT NULL,
    date_str TEXT NOT NULL,
    timestamp REAL NOT NULL,
    description TEXT,
    FOREIGN KEY (opportunity_id) REFERENCES tasks(opportunity_id) ON DELETE RESTRICT
);

INSERT INTO budget_new (id, opportunity_id, amount, date_str, timestamp, description)
SELECT id, opportunity_id, amount, date_str, timestamp, description 
FROM budget 
WHERE opportunity_id IS NOT NULL 
  AND opportunity_id IN (SELECT opportunity_id FROM tasks);

DROP TABLE budget;
ALTER TABLE budget_new RENAME TO budget;

-- 10. Ensure idempotency_keys table exists
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- 11. Ensure audit_log table exists
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    timestamp REAL NOT NULL
);

-- 12. Create Indexes
CREATE INDEX IF NOT EXISTS idx_tasks_opportunity_id ON tasks(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp);

PRAGMA foreign_keys = ON;
