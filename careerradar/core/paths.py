"""Canonical locations for immutable application resources and mutable user data.

Source checkouts keep their historical repository-local layout. Installed wheels use the
operating system's standard config/data/state directories, so ``careerradar`` works from any
directory and never writes into site-packages.
"""

import os
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir, user_documents_dir, user_state_dir

# careerradar/core/paths.py -> careerradar/core -> careerradar -> project/package root
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGE_ROOT = os.path.join(REPO_ROOT, "careerradar")


def _expand(value: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(value)))


def _is_checkout() -> bool:
    """Whether this package is executing from the source tree, not a wheel install."""
    return os.path.exists(os.path.join(REPO_ROOT, "pyproject.toml"))


def _under_home(name: str) -> str | None:
    root = os.environ.get("CAREERRADAR_HOME")
    return os.path.join(_expand(root), name) if root else None


SOURCE_CHECKOUT = _is_checkout() and not os.environ.get("CAREERRADAR_HOME")
APP_NAME = "careerradar"

if SOURCE_CHECKOUT:
    CONFIG_DIR = REPO_ROOT
    DATA_DIR = os.path.join(REPO_ROOT, "data")
    STATE_DIR = REPO_ROOT
    _default_resumes_dir = os.path.join(REPO_ROOT, "resumes")
else:
    CONFIG_DIR = _under_home("config") or user_config_dir(APP_NAME)
    DATA_DIR = _under_home("data") or user_data_dir(APP_NAME)
    STATE_DIR = _under_home("state") or user_state_dir(APP_NAME)
    _default_resumes_dir = _under_home("resumes") or os.path.join(
        user_documents_dir(), "CareerRadar"
    )

CONFIG_DIR = _expand(os.environ.get("CAREERRADAR_CONFIG_DIR", CONFIG_DIR))
DATA_DIR = _expand(os.environ.get("CAREERRADAR_DATA_DIR", DATA_DIR))
STATE_DIR = _expand(os.environ.get("CAREERRADAR_STATE_DIR", STATE_DIR))

DB_PATH = _expand(os.environ.get("CAREERRADAR_DB_PATH", os.path.join(DATA_DIR, "jobs.db")))
GRAPH_DB_PATH = _expand(
    os.environ.get("CAREERRADAR_GRAPH_DB_PATH", os.path.join(DATA_DIR, "graphs.db"))
)
CONFIG_PATH = _expand(
    os.environ.get("CAREERRADAR_CONFIG_PATH", os.path.join(CONFIG_DIR, "config.yaml"))
)
CONFIG_LOCAL_PATH = _expand(
    os.environ.get("CAREERRADAR_CONFIG_LOCAL_PATH", os.path.join(CONFIG_DIR, "config.local.yaml"))
)
ENV_PATH = _expand(os.environ.get("CAREERRADAR_ENV_PATH", os.path.join(CONFIG_DIR, ".env")))
LOG_PATH = _expand(os.environ.get("CAREERRADAR_LOG_PATH", os.path.join(STATE_DIR, "app.log")))
STATUS_PATH = _expand(
    os.environ.get("CAREERRADAR_STATUS_PATH", os.path.join(STATE_DIR, "sync_status.json"))
)
PIPELINE_LOCK_PATH = _expand(
    os.environ.get("CAREERRADAR_PIPELINE_LOCK_PATH", os.path.join(STATE_DIR, ".pipeline.lock"))
)
RESUMES_DIR = _expand(os.environ.get("CAREERRADAR_RESUMES_DIR", _default_resumes_dir))

# Kept for callers that need a static default. Runtime resume generation uses
# ``generated_resumes_dir`` so `resumes.output_dir` can override it.
GENERATED_RESUMES_DIR = os.path.join(RESUMES_DIR, "generated") if SOURCE_CHECKOUT else RESUMES_DIR

# Packaged, immutable assets. Hatch includes the frontend build in wheels.
FRONTEND_DIR = os.path.join(PACKAGE_ROOT, "web", "frontend")
TEMPLATES_DIR = os.path.join(PACKAGE_ROOT, "templates")

# Logging and SQLite can be initialized before the CLI reaches `init`. Ensure their parent
# directories exist at import time, while leaving visible resume output creation lazy.
for _directory in (CONFIG_DIR, DATA_DIR, STATE_DIR):
    Path(_directory).mkdir(parents=True, exist_ok=True)


def generated_resumes_dir(config: dict[str, Any] | None = None) -> str:
    """Return the visible resume directory, respecting ``resumes.output_dir``.

    The config import is intentionally lazy: config itself imports this module to find its
    files. Relative overrides resolve beside the user config file, not the caller's CWD.
    """
    if config is None:
        from careerradar.core.config import load_config

        config = load_config()
    output = ((config.get("resumes") or {}).get("output_dir") or "").strip()
    if not output:
        return GENERATED_RESUMES_DIR
    expanded = os.path.expanduser(os.path.expandvars(output))
    if not os.path.isabs(expanded):
        expanded = os.path.join(CONFIG_DIR, expanded)
    return os.path.abspath(expanded)


def ensure_user_dirs() -> dict[str, str]:
    """Create writable parent directories and return the resolved application layout."""
    directories = {
        "config_dir": CONFIG_DIR,
        "data_dir": DATA_DIR,
        "state_dir": STATE_DIR,
        "resumes_dir": generated_resumes_dir(),
    }
    for directory in directories.values():
        Path(directory).mkdir(parents=True, exist_ok=True)
    file_paths = (DB_PATH, GRAPH_DB_PATH, LOG_PATH, STATUS_PATH, PIPELINE_LOCK_PATH)
    for file_path in file_paths:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
    return directories
