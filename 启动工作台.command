#!/bin/bash
set -e
root=$(cd "$(dirname "$0")" && pwd -P)
if bash "$root/scripts/local_workspace.sh" start; then
  address=$(bash "$root/scripts/local_workspace.sh" url)
  /usr/bin/open "$address"
else
  echo "未能启动。请查看上方提示后重试。按回车关闭。"
  read -r _ || true
  exit 1
fi
