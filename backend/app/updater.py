import json
import os
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException


STATE_FILE = Path(os.getenv("UPDATE_STATE_FILE", "/tmp/amazon-agent-update.json"))
REQUEST_FILE = Path(os.getenv("UPDATE_REQUEST_FILE", "/tmp/amazon-agent-update.request"))


def _git(*args: str) -> str | None:
    try:
        return subprocess.check_output(["git", *args], text=True, timeout=8, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def version_info() -> dict:
    return {
        "version": os.getenv("APP_VERSION", "development"),
        "commit": os.getenv("APP_COMMIT") or _git("rev-parse", "HEAD"),
        "branch": os.getenv("APP_BRANCH") or _git("branch", "--show-current") or "main",
        "update_enabled": os.getenv("UPDATE_ENABLED", "false").lower() == "true",
    }


def verify_admin_token(token: str | None) -> None:
    expected = os.getenv("UPDATE_ADMIN_TOKEN", "")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(403, "Administrator authorization required")


def read_state() -> dict:
    if not STATE_FILE.exists():
        return {"status": "idle", **version_info()}
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown", **version_info()}


def start_update() -> dict:
    if os.getenv("UPDATE_ENABLED", "false").lower() != "true":
        raise HTTPException(409, "System update is not configured on this deployment")
    current = read_state()
    if current.get("status") in {"checking", "backing_up", "updating", "restarting", "rolling_back"}:
        raise HTTPException(409, "An update is already running")
    state = {"status": "checking", "started_at": datetime.now(timezone.utc).isoformat(), **version_info()}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _write_json(STATE_FILE, state)
    try:
        REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        REQUEST_FILE.write_text(datetime.now(timezone.utc).isoformat(), "utf-8")
    except OSError as exc:
        state.update(status="failed", error=exc.__class__.__name__)
        _write_json(STATE_FILE, state)
        raise HTTPException(500, "Unable to notify update worker") from exc
    return state


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value), "utf-8")
    temporary.replace(path)

