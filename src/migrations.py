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
    """Applies all numbered SQL migrations in migrations/ to the target SQLite database deterministically."""
    resolved_path = resolve_db_path(db_path)
    migrations_dir = os.path.join(get_project_root(), "migrations")

    if not os.path.exists(migrations_dir):
        raise FileNotFoundError(f"Migrations directory not found at {migrations_dir}")

    migration_files = sorted([
        os.path.join(migrations_dir, f)
        for f in os.listdir(migrations_dir)
        if f.endswith(".sql")
    ])

    if not migration_files:
        raise FileNotFoundError(f"No SQL migration scripts found in {migrations_dir}")

    conn = sqlite3.connect(resolved_path, timeout=30.0)
    try:
        cursor = conn.cursor()
        for migration_file in migration_files:
            with open(migration_file, "r", encoding="utf-8") as f:
                sql_script = f.read()
            cursor.executescript(sql_script)
            conn.commit()
            print(f"[MIGRATION SUCCESS] Applied {migration_file} to {resolved_path}")
        
        # Verify FK integrity after migration
        cursor.execute("PRAGMA foreign_keys = ON;")
        fk_errors = cursor.execute("PRAGMA foreign_key_check;").fetchall()
        if fk_errors:
            raise RuntimeError(f"Foreign key violations detected post-migration: {fk_errors}")
            
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
