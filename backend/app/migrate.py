"""Apply pending release SQL before an API process starts against an existing volume."""
import os
from pathlib import Path

import psycopg


MIGRATIONS = Path("/app/db")
LOCK_ID = 487021012  # Also used by scripts/migrate.sh when upgrading an existing deployment.


def migrate(conn, directory):
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    if not files:
        raise RuntimeError("No release migrations were packaged")
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
        conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                     version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
        # The first two releases predate the migration ledger.
        conn.execute("""INSERT INTO schema_migrations(version)
            SELECT version FROM (VALUES ('001_schema.sql'),('002_seed.sql')) AS baseline(version)
            WHERE to_regclass('public.products') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM schema_migrations)
            ON CONFLICT DO NOTHING""")
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for file in files:
            if file.name not in applied:
                conn.execute(file.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING", (file.name,))


if __name__ == "__main__":
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as database:
        migrate(database, MIGRATIONS)
