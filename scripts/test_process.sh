#!/usr/bin/env bash
# Uses the selected Compose stack; never run integration tests on its business DB.
set -euo pipefail
cd "$(dirname "$0")/.."
test_db="homa_workflow_test_$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
test_path="/tmp/$test_db"
cleanup() {
  status=$?
  docker compose exec -T api rm -rf "$test_path" >/dev/null 2>&1 || true
  docker compose exec -T db sh -c 'dropdb --if-exists -U "$POSTGRES_USER" "$1"' sh "$test_db" || true
  exit "$status"
}
trap cleanup EXIT
docker compose exec -T db sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$test_db"
for migration in db/*.sql; do
  docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"' sh "$test_db" < "$migration" >/dev/null
done
docker compose cp backend/tests "api:$test_path"
docker compose exec -T api python - "$test_db" "$test_path" <<'PY'
import os
import secrets
import shutil
import sys
import unittest
from urllib.parse import urlsplit, urlunsplit
test_db, test_path = sys.argv[1:]
assert test_db.startswith('homa_workflow_test_') and test_path == '/tmp/'+test_db
original = urlsplit(os.environ['DATABASE_URL'])
os.environ['DATABASE_URL'] = urlunsplit(original._replace(path='/'+test_db))
os.environ['WORKFLOW_INTEGRATION'] = '1'
os.environ['WORKFLOW_SETUP_TOKEN'] = secrets.token_urlsafe(32)
os.environ['WORKFLOW_UPLOAD_DIR'] = test_path+'/uploads'
try:
    suite = unittest.defaultTestLoader.discover(test_path)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
finally:
    shutil.rmtree(test_path+'/uploads', ignore_errors=True)
sys.exit(not result.wasSuccessful())
PY
