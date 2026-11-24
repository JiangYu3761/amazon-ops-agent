"""Run real Chromium or backend tests against a fresh local PostgreSQL database."""

import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen
from uuid import uuid4

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parents[1]


def serve():
    import uvicorn
    from starlette.staticfiles import StaticFiles

    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import app

    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True))
    listener = socket.socket(fileno=int(os.environ["WORKBENCH_LISTENER_FD"]))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[listener])


def run(backend=False):
    admin_dsn = os.environ["WORKBENCH_TEST_PG"]
    parts = urlsplit(admin_dsn)
    if parts.scheme not in ("postgres", "postgresql") or parts.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("WORKBENCH_TEST_PG must identify a local PostgreSQL test server.")
    name = "homa_workflow_test_" + uuid4().hex
    quoted = sql.Identifier(name)
    isolated_dsn = urlunsplit(parts._replace(path="/" + name))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(quoted))
        try:
            with psycopg.connect(isolated_dsn, autocommit=True) as db:
                for migration in sorted((ROOT / "db").glob("*.sql")):
                    db.execute(migration.read_text())
            with tempfile.TemporaryDirectory(prefix="homa-browser-") as scratch, socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen()
                url = "http://127.0.0.1:" + str(listener.getsockname()[1])
                env = dict(os.environ, DATABASE_URL=isolated_dsn, WORKBENCH_BROWSER_URL=url,
                           WORKBENCH_LISTENER_FD=str(listener.fileno()), WORKFLOW_SETUP_TOKEN=secrets.token_urlsafe(32),
                           WORKFLOW_UPLOAD_DIR=scratch, WORKFLOW_COOKIE_SECURE="false", SYNC_WORKER_ENABLED="false",
                           UPDATE_ENABLED="false", LLM_API_KEY="", CATALOG_FILE=str(ROOT / "frontend/catalog.json"),
                           WORKBENCH_DEFINITION_DIR=str(ROOT / "frontend"))
                for key in list(env):
                    if key.startswith("AMAZON_"):
                        del env[key]
                if backend:
                    env.update(WORKFLOW_INTEGRATION="1", PYTHONPATH=str(ROOT / "backend"))
                    subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "backend/tests", "-v"],
                                   check=True, cwd=ROOT, env=env)
                    return
                service = subprocess.Popen([sys.executable, __file__, "serve"], env=env, cwd=ROOT,
                                           pass_fds=[listener.fileno()])
                try:
                    for _ in range(100):
                        if service.poll() is not None:
                            raise RuntimeError("Test API exited before becoming ready.")
                        try:
                            with urlopen(url + "/health", timeout=1) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError("Isolated test API did not start.")
                    subprocess.run(["node", "scripts/check_workbench.cjs"], check=True, env=env, cwd=ROOT)
                finally:
                    service.terminate()
                    service.wait(timeout=30)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(quoted))


if __name__ == "__main__":
    if sys.argv[1:] == ["serve"]:
        serve()
    elif sys.argv[1:] in ([], ["--backend"]):
        run(backend="--backend" in sys.argv)
    else:
        raise SystemExit("usage: python scripts/check_workbench.py [--backend]")
