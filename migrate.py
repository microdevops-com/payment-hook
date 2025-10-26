#!/usr/bin/env python3
import glob
import logging
import os
import sys
from datetime import datetime

import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def get_connection():
    return psycopg2.connect(
        host=os.getenv("PG_HOST", "localhost"),
        port=os.getenv("PG_PORT", "5432"),
        dbname=os.getenv("PG_DB"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
    )


def ensure_migrations_table():
    """Create migrations tracking table if it doesn't exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    id SERIAL PRIMARY KEY,
                    version VARCHAR(255) NOT NULL UNIQUE,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """
            )
            conn.commit()
            logger.info("Migrations table ensured")


def get_applied_migrations():
    """Get list of applied migrations."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            return [row[0] for row in cur.fetchall()]


def mark_migration_applied(version):
    """Mark a migration as applied."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()


def get_migration_files():
    """Get sorted list of migration files."""
    migration_files = glob.glob("migrations/*.sql")
    return sorted([os.path.basename(f) for f in migration_files])


def run_migration(migration_file):
    """Run a single migration file."""
    migration_path = os.path.join("migrations", migration_file)

    if not os.path.exists(migration_path):
        logger.error(f"Migration file not found: {migration_path}")
        return False

    try:
        with open(migration_path, "r") as f:
            sql = f.read()

        if not sql.strip():
            logger.warning(f"Empty migration file: {migration_file}")
            return True

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Split by semicolons and execute each statement
                statements = [s.strip() for s in sql.split(";") if s.strip()]
                for statement in statements:
                    cur.execute(statement)
                conn.commit()

        # Extract version from filename (remove .sql extension)
        version = migration_file[:-4] if migration_file.endswith(".sql") else migration_file
        mark_migration_applied(version)
        logger.info(f"Applied migration: {migration_file}")
        return True

    except Exception as e:
        logger.error(f"Failed to apply migration {migration_file}: {e}")
        return False


def run_migrations():
    """Run all pending migrations."""
    ensure_migrations_table()

    applied_migrations = get_applied_migrations()
    migration_files = get_migration_files()

    if not migration_files:
        logger.info("No migration files found")
        return True

    pending_migrations = []
    for migration_file in migration_files:
        version = migration_file[:-4] if migration_file.endswith(".sql") else migration_file
        if version not in applied_migrations:
            pending_migrations.append(migration_file)

    if not pending_migrations:
        logger.info("No pending migrations")
        return True

    logger.info(f"Found {len(pending_migrations)} pending migrations")

    for migration_file in pending_migrations:
        if not run_migration(migration_file):
            logger.error(f"Migration failed, stopping: {migration_file}")
            return False

    logger.info("All migrations applied successfully")
    return True


def create_migration(name):
    """Create a new migration file."""
    if not name:
        logger.error("Migration name is required")
        return False

    # Create migrations directory if it doesn't exist
    os.makedirs("migrations", exist_ok=True)

    # Generate timestamp-based filename
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{name}.sql"
    filepath = os.path.join("migrations", filename)

    if os.path.exists(filepath):
        logger.error(f"Migration file already exists: {filepath}")
        return False

    template = f"""-- Migration: {name}
-- Created: {datetime.now().isoformat()}

-- Add your SQL statements here
-- Each statement should end with a semicolon

-- Example:
-- CREATE TABLE example (
--     id SERIAL PRIMARY KEY,
--     name VARCHAR(255) NOT NULL
-- );
"""

    with open(filepath, "w") as f:
        f.write(template)

    logger.info(f"Created migration: {filepath}")
    return True


def main():
    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "create":
            if len(sys.argv) < 3:
                logger.error("Usage: python migrate.py create <migration_name>")
                sys.exit(1)

            migration_name = "_".join(sys.argv[2:])
            if create_migration(migration_name):
                sys.exit(0)
            else:
                sys.exit(1)

        elif command == "status":
            ensure_migrations_table()
            applied = get_applied_migrations()
            available = get_migration_files()

            print(f"Applied migrations ({len(applied)}):")
            for migration in applied:
                print(f"  ✓ {migration}")

            pending = []
            for migration_file in available:
                version = migration_file[:-4] if migration_file.endswith(".sql") else migration_file
                if version not in applied:
                    pending.append(migration_file)

            if pending:
                print(f"\nPending migrations ({len(pending)}):")
                for migration in pending:
                    print(f"  ⏳ {migration}")
            else:
                print("\nNo pending migrations")

            sys.exit(0)

        else:
            logger.error(f"Unknown command: {command}")
            logger.info("Available commands: create <name>, status")
            sys.exit(1)

    # Default: run migrations
    if run_migrations():
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
