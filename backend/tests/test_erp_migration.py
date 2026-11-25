"""Upgrade a pre-ERP database twice without touching the main integration database."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4


@unittest.skipUnless(os.getenv("WORKFLOW_INTEGRATION") == "1", "requires isolated PostgreSQL")
class MigrationUpgradeTest(unittest.TestCase):
    def test_upgrade_restart_and_atomic_failure(self):
        import psycopg
        from psycopg import sql
        from app.migrate import migrate
        parts = urlsplit(os.environ["DATABASE_URL"])
        self.assertTrue(parts.path.startswith("/homa_workflow_test_"))
        name = "homa_workflow_test_migration_" + uuid4().hex
        dsn = urlunsplit(parts._replace(path="/" + name))
        directory = Path(__file__).resolve().parents[2] / "db"
        if not directory.is_dir():
            directory = Path("/app/db")
        self.assertTrue(directory.is_dir())
        with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    for f in sorted(directory.glob("*.sql")):
                        if int(f.name[:3]) <= 11:
                            conn.execute(f.read_text())
                    product = conn.execute("INSERT INTO products(model,name,marketplace) VALUES ('PRE-ERP','旧数据','US') RETURNING id").fetchone()[0]
                def instance(_):
                    with psycopg.connect(dsn, autocommit=True) as conn:
                        migrate(conn, directory)
                with ThreadPoolExecutor(max_workers=2) as instances:
                    list(instances.map(instance, range(2)))
                with psycopg.connect(dsn, autocommit=True) as conn, tempfile.TemporaryDirectory() as tmp:
                    migrate(conn, directory)
                    self.assertEqual(conn.execute("SELECT name FROM products WHERE id=%s", (product,)).fetchone()[0], "旧数据")
                    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
                    self.assertEqual(applied, {f.name for f in directory.glob("*.sql")})
                    Path(tmp, "900_probe.sql").write_text("CREATE TABLE migration_probe(id integer)")
                    Path(tmp, "901_broken.sql").write_text("SELECT 1/0")
                    with self.assertRaises(psycopg.errors.DivisionByZero):
                        migrate(conn, Path(tmp))
                    self.assertIsNone(conn.execute("SELECT to_regclass('migration_probe')").fetchone()[0])
                    self.assertFalse(conn.execute("SELECT EXISTS(SELECT 1 FROM schema_migrations WHERE version='900_probe.sql')").fetchone()[0])
            finally:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
