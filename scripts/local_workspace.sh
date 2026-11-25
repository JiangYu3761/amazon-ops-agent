#!/bin/bash
# Docker Compose v2, Bash 3.2+ (including the macOS system Bash).
set -euo pipefail
umask 077
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
INSTANCE=${HOMA_LOCAL_INSTANCE:-main}
[[ $INSTANCE =~ ^[a-z0-9][a-z0-9-]{0,30}$ ]] || { echo '实例名称须为 1–31 位小写字母、数字或短横线。' >&2; exit 2; }
if [[ $(uname -s) == Darwin ]]; then base_dir="$HOME/Library/Application Support/HOMA/workspaces"
else base_dir="${XDG_STATE_HOME:-$HOME/.local/state}/homa/workspaces"; fi
STATE=${HOMA_LOCAL_STATE_DIR:-$base_dir/$INSTANCE}
ACTION=${1:-start}
PROJECT="homa-local-$INSTANCE"
LABEL=com.homa.local-workspace
fail(){ printf 'HOMA：%s\n' "$*" >&2; exit 1; }
hexkey(){ od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'; }
valid_port(){ [[ $1 =~ ^[1-9][0-9]{0,4}$ ]] && (( $1<=65535 )); }
private_file(){ [[ -f $1 && ! -L $1 && -O $1 ]] || fail "配置缺失或归属异常：$1"; chmod 600 "$1"; }
saved(){ IFS= read -r REPLY < "$1"; }
usage(){ echo "用法：bash scripts/local_workspace.sh {start|status|url|stop|backup|restore 备份目录}"; }

case $ACTION in start|status|url|stop|backup) [[ $# -le 1 ]] || { usage; exit 2; } ;;
  restore) [[ $# == 2 && -d $2 ]] || { usage; exit 2; }
    ARCHIVE=$(cd "$2" && pwd -P)
    [[ -s $ARCHIVE/db.dump && -s $ARCHIVE/uploads.tar && -f $ARCHIVE/SHA256 ]] || fail '备份缺少数据库、附件或校验文件' ;;
  *) usage; exit 2 ;;
esac
[[ $STATE == /* && ! -L $STATE && $STATE != / ]] || fail '配置目录须使用独立的绝对路径。'
[[ $STATE != *$'\n'* && $STATE != *$'\r'* ]] || fail '目录名称不能包含换行。'
# Keep credentials and backups out of the Docker build context, including symlinked parent directories.
ancestor=$STATE
while [[ ! -d $ancestor ]]; do ancestor=$(dirname "$ancestor"); done
ancestor=$(cd "$ancestor" && pwd -P)
case "$ancestor/" in "$ROOT/"*) fail '私有配置目录须放在项目源码目录以外。' ;; esac
if [[ $ACTION != url ]]; then
  command -v docker >/dev/null || fail '请先安装并启动 Docker Desktop，或带 Compose v2 的 Docker Engine。'
  docker compose version >/dev/null 2>&1 && docker info >/dev/null 2>&1 || fail 'Docker Engine 或 Compose v2 尚未就绪。'
  endpoint=${DOCKER_HOST:-$(docker context inspect "$(docker context show)" --format '{{.Endpoints.docker.Host}}')}
  [[ $endpoint == unix://* ]] || fail '正式本机工作区只接受本地 Docker Unix socket；请切换到本地 Docker context。'
fi

# Validate both the resource's conventional name and Compose project labels.
check_resource(){
  local kind=$1 name=$2 format='.Config.Labels' actual
  if [[ $kind == volume || $kind == network ]]; then format='.Labels'; fi
  if docker "$kind" inspect "$name" >/dev/null 2>&1; then
    actual=$(docker "$kind" inspect --format "{{index $format \"$LABEL\"}}" "$name")
    [[ -n ${KEY:-} && $actual == "$KEY" ]] || fail "名称 $name 已被其他 Docker 资源占用；换用新实例名称或找回原私有配置。"
  fi
}
check_names(){
  local item kind
  for item in db api web; do
    check_resource container "$PROJECT-$item-1"
    check_resource image "$PROJECT-$item:local"
  done
  check_resource volume "${PROJECT}_database"
  check_resource volume "${PROJECT}_uploads"
  check_resource network "${PROJECT}_default"
  for kind in container volume network; do
    if [[ $kind == container ]]; then item=$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")
    else item=$(docker "$kind" ls -q --filter "label=com.docker.compose.project=$PROJECT"); fi
    for item in $item; do check_resource "$kind" "$item"; done
  done
}
check_port(){
  local owned
  owned=''
  if [[ $(docker container inspect --format '{{.State.Running}}' "$PROJECT-web-1" 2>/dev/null || :) == true ]]; then
    owned=$(docker port "$PROJECT-web-1" 80/tcp 2>/dev/null || :)
  fi
  if [[ $owned != "127.0.0.1:$PORT" ]] && (exec 9<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
    fail "本机端口 $PORT 已被占用。初次安装可先设置 HOMA_LOCAL_PORT 为其他空闲端口。"
  fi
}
checksum(){
  if command -v sha256sum >/dev/null; then sha256sum "$@"
  else shasum -a 256 "$@"; fi
}
check_backup(){
  # Only verify the two fixed filenames; never follow untrusted paths from a manifest.
  local actual expected
  actual=$(cd "$ARCHIVE" && checksum db.dump uploads.tar)
  expected=$(cat "$ARCHIVE/SHA256")
  [[ $actual == "$expected" ]] || fail '备份校验失败，两个文件和校验单须来自同一次备份。'
}
if [[ $ACTION == restore ]]; then check_backup; fi

if [[ ! -e $STATE ]]; then
  [[ $ACTION == start || $ACTION == restore ]] || fail '此工作区尚未建立。'
  PORT=${HOMA_LOCAL_PORT:-14681}; valid_port "$PORT" || fail '端口须为 1–65535。'
  KEY=''; check_names; check_port
  # Publish the ready flag last. A concurrent installer never adopts partial settings.
  mkdir -p "$(dirname "$STATE")"
  mkdir -m 700 "$STATE" || fail '配置路径已出现，请重新运行。'
  printf '%s\n' "$(hexkey 24)" > "$STATE/instance-key"
  printf '%s\n' "$INSTANCE" > "$STATE/instance-name"
  printf '%s\n' "$PORT" > "$STATE/port"
  printf 'POSTGRES_PASSWORD=%s\n' "$(hexkey 32)" > "$STATE/database.env"
  printf '%s\n' "$(hexkey 24)" > "$STATE/admin-setup-code"
  printf 'homa\n' > "$STATE/db-name"
  if [[ $ACTION == restore ]]; then cp "$ARCHIVE/SHA256" "$STATE/restore.pending"; fi
  touch "$STATE/config-ready"
fi
[[ -d $STATE && -O $STATE && ! -L $STATE && -f $STATE/config-ready ]] || fail '该目录已存在但缺少本实例配置，不会采用或覆盖。请指定新的 HOMA_LOCAL_STATE_DIR。'
chmod 700 "$STATE"
if [[ ! -e $STATE/integrations.env && ! -L $STATE/integrations.env ]]; then
  (set -C; printf '%s\n' '# Optional API credentials. One KEY=value per line.' > "$STATE/integrations.env")
fi
for file in instance-key instance-name port database.env admin-setup-code db-name integrations.env; do private_file "$STATE/$file"; done
saved "$STATE/instance-key"; KEY=$REPLY
[[ $KEY =~ ^[0-9a-f]{48}$ ]] || fail '本机实例标记无效。'
saved "$STATE/instance-name"; [[ $REPLY == "$INSTANCE" ]] || fail '配置属于其他实例名称。'
saved "$STATE/port"; PORT=$REPLY; valid_port "$PORT" || fail '私有配置中的端口无效。'
[[ -z ${HOMA_LOCAL_PORT:-} || $HOMA_LOCAL_PORT == "$PORT" ]] || fail "此实例已登记端口 $PORT；请使用原端口，或使用独立实例。"
if [[ $ACTION == url ]]; then echo "http://127.0.0.1:$PORT"; exit 0; fi
saved "$STATE/admin-setup-code"; PAIRING=$REPLY
saved "$STATE/db-name"; ACTIVE_DB=$REPLY
[[ $ACTIVE_DB == homa || $ACTIVE_DB =~ ^restored_[0-9a-f]{16}$ ]] || fail '配置中的数据库名称不合法。'
export HOMA_PROJECT="$PROJECT" HOMA_PRIVATE_DIR="$STATE" HOMA_INSTANCE_KEY="$KEY" HOMA_PAIRING_KEY="$PAIRING" HOMA_WEB_PORT="$PORT" HOMA_ACTIVE_DB="$ACTIVE_DB"
DC=(docker compose --project-name "$PROJECT" --env-file /dev/null -f "$ROOT/deploy/local-workspace/compose.yml")
if [[ -n ${HOMA_LOCAL_PYTHON_BASE:-} ]]; then DC+=(-f "$ROOT/deploy/local-workspace/local-base.yml"); fi
check_names
if [[ $ACTION == status ]]; then "${DC[@]}" ps -a; echo "http://127.0.0.1:$PORT"; exit 0; fi
mkdir "$STATE/operation.lock" || fail '另一个管理操作正在进行。若电脑曾中断，请按说明核对 operation.lock。'
printf '%s\n' "$$" > "$STATE/operation.lock/pid"
RESUME=false
cleanup(){
  code=$?
  if [[ $RESUME == true ]]; then "${DC[@]}" start api web >/dev/null || code=1; fi
  rm -f "$STATE/operation.lock/pid"; rmdir "$STATE/operation.lock"
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
check_names
if [[ $ACTION == stop ]]; then "${DC[@]}" stop; exit 0; fi
for volume in database uploads; do
  name="${PROJECT}_$volume"
  if ! docker volume inspect "$name" >/dev/null 2>&1; then
    [[ ! -f $STATE/installed ]] || fail "原数据卷 $name 已缺失。请在新实例中从备份恢复。"
    docker volume create --label "$LABEL=$KEY" "$name" >/dev/null
  fi
  check_resource volume "$name"
done

if [[ $ACTION == backup ]]; then
  [[ -f $STATE/installed ]] || fail '请先完成首次启动。'
  for service in db api web; do
    [[ $(docker container inspect --format '{{.State.Running}}' "$PROJECT-$service-1" 2>/dev/null) == true ]] || fail '备份前请先启动工作区。'
  done
  mkdir -p "$STATE/backups"
  tmp=$(mktemp -d "$STATE/backups/.writing-XXXXXXXX")
  RESUME=true
  "${DC[@]}" stop web api >&2
  "${DC[@]}" exec -T db pg_dump -Fc --exclude-table-data=public.workflow_sessions -U homa -d "$ACTIVE_DB" > "$tmp/db.dump"
  docker run --rm -i --network none --read-only --mount "type=volume,src=${PROJECT}_uploads,dst=/data,readonly" \
    "$PROJECT-db:local" tar -C /data -cf - . > "$tmp/uploads.tar"
  (cd "$tmp" && checksum db.dump uploads.tar > SHA256)
  dest="$STATE/backups/$(date -u +%Y%m%dT%H%M%SZ)-${tmp##*-}"
  mv "$tmp" "$dest"
  "${DC[@]}" up -d --wait --no-build api web >&2
  RESUME=false
  printf '%s\n' "$dest"
  exit 0
fi
check_port
if [[ $ACTION == restore ]]; then
  [[ ! -f $STATE/installed && -f $STATE/restore.pending ]] || fail '仅能向专为该备份新建的空实例恢复。'
  recorded=$(cat "$STATE/restore.pending")
  [[ $recorded == "$(cat "$ARCHIVE/SHA256")" ]] || fail '未完成的恢复需要同一份备份。'
elif [[ -f $STATE/restore.pending && ! -f $STATE/installed ]]; then
  fail '此实例正在恢复，请使用原备份再次运行 restore。'
fi
"${DC[@]}" build
check_names
if [[ $ACTION == restore && $ACTIVE_DB == homa ]]; then
  "${DC[@]}" up -d --wait --no-build db
  restore_db="restored_$(hexkey 8)"
  "${DC[@]}" exec -T db createdb -U homa -T template0 "$restore_db"
  "${DC[@]}" exec -T db pg_restore -U homa --no-owner --no-privileges --exit-on-error --single-transaction -d "$restore_db" < "$ARCHIVE/db.dump"
  # The backup is trusted; extraction nevertheless permits only ordinary files and directories under this volume.
  docker run --rm -i --network none --read-only --mount "type=volume,src=${PROJECT}_uploads,dst=/data" \
    "$PROJECT-api:local" python -c '
import tarfile, sys
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    for member in archive:
        if not (member.isdir() or member.isfile()):
            raise ValueError("Unsupported attachment archive entry")
        archive.extract(member, "/data", filter="data")
' < "$ARCHIVE/uploads.tar"
  printf '%s\n' "$restore_db" > "$STATE/db-name.new"; mv "$STATE/db-name.new" "$STATE/db-name"
  export HOMA_ACTIVE_DB="$restore_db"
fi
"${DC[@]}" up -d --wait --wait-timeout 210 --no-build
touch "$STATE/installed"
rm -f "$STATE/restore.pending"
echo "工作区已就绪：http://127.0.0.1:$PORT"
echo "首次管理员设置码文件：$STATE/admin-setup-code"
