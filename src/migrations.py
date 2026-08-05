import os
import sys
import sqlite3
import argparse

def get_project_root() -> str:
    """Returns the absolute path to the AgentOS project root directory."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def resolve_db_path(db_path: str = "agencyos.db") -> str:
    """Resolves database path deterministically relative to project root if relative path given."""
    if os.path.isabs(db_path):
        return db_path
    return os.path.join(get_project_root(), db_path)

def run_migrations(db_path: str = "agencyos.db") -> str:
    """Applies migrations/001_initial_schema.sql to the target SQLite database deterministically."""
    resolved_path = resolve_db_path(db_path)
    migration_file = os.path.join(get_project_root(), "migrations", "001_initial_schema.sql")

    if not os.path.exists(migration_file):
        raise FileNotFoundError(f"Migration script not found at {migration_file}")

    with open(migration_file, "r", encoding="utf-8") as f:
        sql_script = f.read()

    conn = sqlite3.connect(resolved_path, timeout=30.0)
    try:
        cursor = conn.cursor()
        # Execute migration script in transaction
        cursor.executescript(sql_script)
        conn.commit()
        
        # Verify FK integrity after migration
        cursor.execute("PRAGMA foreign_keys = ON;")
        fk_errors = cursor.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key violations detected post-migration: {fk_errors}")
            
        print(f"[MIGRATION SUCCESS] Applied {migration_file} to {resolved_path}")
        return resolved_path
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgencyOS Database Migration Runner")
    parser.add_argument("--db-path", type=str, default="agencyos.db", help="Path to SQLite database")
    args = parser.parse_args()
    
    run_migrations(args.db_path)
