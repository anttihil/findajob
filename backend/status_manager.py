import os
import json
from datetime import datetime

STATUS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 
    "sync_status.json"
)

def load_sync_status():
    if not os.path.exists(STATUS_FILE):
        return {
            "sync_in_progress": False,
            "last_sync_time": None,
            "last_run_stats": {
                "total_fetched": 0,
                "total_evaluated": 0,
                "total_new": 0
            },
            "errors": []
        }
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "sync_in_progress": False,
            "last_sync_time": None,
            "last_run_stats": {
                "total_fetched": 0,
                "total_evaluated": 0,
                "total_new": 0
            },
            "errors": []
        }

def save_sync_status(status_data):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status_data, f, indent=2)
    except Exception as e:
        print(f"Error saving sync status: {e}")

def set_sync_progress(in_progress, total_fetched=0, total_evaluated=0, total_new=0):
    status = load_sync_status()
    status["sync_in_progress"] = in_progress
    if in_progress:
        # Clear old errors and set running stats to 0
        status["errors"] = []
        status["last_run_stats"] = {
            "total_fetched": 0,
            "total_evaluated": 0,
            "total_new": 0
        }
    else:
        status["last_sync_time"] = datetime.now().isoformat()
        status["last_run_stats"] = {
            "total_fetched": total_fetched,
            "total_evaluated": total_evaluated,
            "total_new": total_new
        }
    save_sync_status(status)

def add_sync_error(source, error_message):
    status = load_sync_status()
    # Avoid repeating the same error for a source within the same run
    if not any(e["source"] == source and e["error"] == error_message for e in status["errors"]):
        status["errors"].append({
            "source": source,
            "error": error_message,
            "timestamp": datetime.now().isoformat()
        })
        save_sync_status(status)
