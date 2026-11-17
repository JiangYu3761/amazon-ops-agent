#!/usr/bin/env sh
set -eu

APP_DIR=${APP_DIR:-/opt/amazon-ops-agent}
BRANCH=${APP_BRANCH:-main}
STATE_FILE=${UPDATE_STATE_FILE:-/tmp/amazon-agent-update.json}
BACKUP_DIR=${BACKUP_DIR:-$APP_DIR/backups}
HEALTH_URL=${APP_HEALTH_URL:-http://127.0.0.1:8000/health}
LOCK_DIR=${UPDATE_LOCK_DIR:-/tmp/amazon-agent-update.lock}

write_state() {
  status=$1
  detail=${2:-}
  python3 - "$STATE_FILE" "$status" "$detail" <<'PY'
import json, pathlib, sys
from datetime import datetime, timezone
p=pathlib.Path(sys.argv[1]); p.parent.mkdir(parents=True,exist_ok=True)
data={"status":sys.argv[2],"detail":sys.argv[3],"updated_at":datetime.now(timezone.utc).isoformat()}
tmp=p.with_suffix(p.suffix+".tmp")
tmp.write_text(json.dumps(data),encoding="utf-8")
tmp.replace(p)
PY
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  write_state failed "another update is running"
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

cd "$APP_DIR"
current=$(git rev-parse HEAD)
target=
switched=false
finished=false
failure_detail="update command failed"

finish_or_rollback() {
  result=$?
  if [ "$finished" = true ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
    return
  fi
  if [ "$switched" = true ]; then
    write_state rolling_back "$current"
    git reset --hard "$current" || true
    export APP_COMMIT="$current"
    docker compose up -d --build api web || true
  fi
  write_state failed "$failure_detail"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  exit "$result"
}
trap finish_or_rollback EXIT

write_state checking "$current"
failure_detail="could not fetch origin/$BRANCH"
git fetch --prune origin "$BRANCH"
target=$(git rev-parse "origin/$BRANCH")
if [ "$current" = "$target" ]; then
  write_state up_to_date "$current"
  finished=true
  exit 0
fi
failure_detail="non-fast-forward update rejected"
git merge-base --is-ancestor "$current" "$target"
failure_detail="deployment checkout has local changes"
test -z "$(git status --porcelain)"

mkdir -p "$BACKUP_DIR"
backup="$BACKUP_DIR/postgres-$(date -u +%Y%m%dT%H%M%SZ)-$current.sql.gz"
write_state backing_up "$backup"
failure_detail="database backup failed"
raw_backup="${backup%.gz}"
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-amazon_agent}" "${POSTGRES_DB:-amazon_agent}" > "$raw_backup"
gzip "$raw_backup"

write_state updating "$target"
failure_detail="build or migration failed; code rolled back, database backup retained at $backup"
git reset --hard "$target"
switched=true
export APP_COMMIT="$target"
export APP_BRANCH="$BRANCH"
docker compose build api
sh scripts/migrate.sh
docker compose up -d --build api web
write_state restarting "$target"

healthy=false
i=0
while [ "$i" -lt 30 ]; do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then healthy=true; break; fi
  i=$((i+1)); sleep 2
done
if [ "$healthy" != true ]; then
  failure_detail="health check failed; code rolled back, database backup retained at $backup"
  exit 1
fi
write_state succeeded "$target"
finished=true

