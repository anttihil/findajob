import json
import os
from datetime import datetime, timedelta
from typing import Any

from careerradar.core.paths import STATUS_PATH

STATUS_FILE = STATUS_PATH


def load_sync_status() -> dict[str, Any]:
    if not os.path.exists(STATUS_FILE):
        return {
            "sync_in_progress": False,
            "last_sync_time": None,
            "last_run_stats": {"total_fetched": 0, "total_evaluated": 0, "total_new": 0},
            "errors": [],
        }
    try:
        with open(STATUS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - any unreadable status file falls back to a fresh status
        return {
            "sync_in_progress": False,
            "last_sync_time": None,
            "last_run_stats": {"total_fetched": 0, "total_evaluated": 0, "total_new": 0},
            "errors": [],
        }


def save_sync_status(status_data: dict[str, Any]) -> None:
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=2)
    except Exception as e:  # noqa: BLE001 - status is best-effort telemetry and must never fail a sync
        print(f"Error saving sync status: {e}")


# A sync that crashes leaves sync_in_progress=True and wedges the Sync button forever, so
# the lock records its owning PID and is treated as stale after this long.
STALE_LOCK_MINUTES = 90


def set_sync_progress(
    in_progress: bool,
    total_fetched: int = 0,
    total_evaluated: int = 0,
    total_new: int = 0,
) -> None:
    status = load_sync_status()
    status["sync_in_progress"] = in_progress
    if in_progress:
        status["errors"] = []
        status["last_run_stats"] = {"total_fetched": 0, "total_evaluated": 0, "total_new": 0}
        status["owner_pid"] = os.getpid()
        status["started_at"] = datetime.now().isoformat()
    else:
        status["last_sync_time"] = datetime.now().isoformat()
        status["last_run_stats"] = {
            "total_fetched": total_fetched,
            "total_evaluated": total_evaluated,
            "total_new": total_new,
        }
        status["owner_pid"] = None
        status["started_at"] = None
    save_sync_status(status)


def is_sync_running() -> bool:
    """Whether a sync genuinely holds the lock.

    Checks liveness rather than trusting the flag: a crashed or killed sync would otherwise
    leave sync_in_progress=True permanently, and the only recovery would be editing the
    status file by hand.
    """
    status = load_sync_status()
    if not status.get("sync_in_progress"):
        return False

    pid = status.get("owner_pid")
    if pid:
        try:
            os.kill(int(pid), 0)  # signal 0 only tests for existence
        except (OSError, ValueError, TypeError):
            return False  # owner is gone; the lock is stale

    started_at = status.get("started_at")
    if started_at:
        try:
            age = datetime.now() - datetime.fromisoformat(started_at)
            if age > timedelta(minutes=STALE_LOCK_MINUTES):
                return False
        except (ValueError, TypeError):
            pass

    return True


def clear_stale_lock() -> bool:
    """Release a lock whose owner is gone. Returns True if one was cleared."""
    status = load_sync_status()
    if status.get("sync_in_progress") and not is_sync_running():
        status["sync_in_progress"] = False
        status["owner_pid"] = None
        status["started_at"] = None
        save_sync_status(status)
        return True
    return False


def add_sync_error(source: str, error_message: str, severity: str = "error") -> None:
    """Record a run problem for the dashboard banner.

    `severity` distinguishes hard failures from coverage warnings and informational notices,
    so a rate-limit trip and "this family's supply is suppressed this window" do not render
    identically. The dedupe key stays (source, error), so one 429 produces one banner line.
    """
    status = load_sync_status()
    if not any(e["source"] == source and e["error"] == error_message for e in status["errors"]):
        status["errors"].append(
            {
                "source": source,
                "error": error_message,
                "severity": severity,
                "timestamp": datetime.now().isoformat(),
            }
        )
        save_sync_status(status)
