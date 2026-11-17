#!/usr/bin/env sh
set -eu

REQUEST_FILE=${UPDATE_REQUEST_FILE:-/var/run/amazon-agent/update.request}
STATE_FILE=${UPDATE_STATE_FILE:-/var/run/amazon-agent/update-state.json}

while true; do
  if [ -f "$REQUEST_FILE" ]; then
    rm -f "$REQUEST_FILE"
    UPDATE_STATE_FILE="$STATE_FILE" sh scripts/self_update.sh || true
  fi
  sleep 2
done

