import os
import sys
from copy import deepcopy
from typing import Any

import yaml

from findajob.core.paths import CONFIG_LOCAL_PATH, CONFIG_PATH, ENV_PATH


def load_env() -> None:
    env_path = ENV_PATH
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")


load_env()


DEFAULT_CONFIG: dict[str, Any] = {
    "scraper": {
        "sources": {"indeed": True, "linkedin": True},
        # Board mechanics and safe operating limits belong to the application.  Users may
        # still override them locally if a board changes its behaviour.
        "page_size": {"indeed": 100, "linkedin": 10},
        "requests_per_description": {"indeed": 0, "linkedin": 1},
        "hours_old_floor": 72,
        "max_hours_old": 168,
        "backfill_hours_old": 168,
        "max_staleness_hours": 72,
        "linkedin_details_ttl_hours": 168,
        "linkedin_details_retry_hours": 1,
        "policies": {
            "indeed": {
                "requests_per_proxy": 100,
                "request_interval_seconds": 2.0,
                "results_wanted": 500,
            },
            "linkedin": {
                "requests_per_proxy": 100,
                "request_interval_seconds": 3.0,
                "results_wanted": 500,
            },
        },
        "proxies": {"enabled": True},
        "circuit_breaker": {
            "consecutive_errors_to_trip": 3,
            "http_429_trips_immediately": True,
            "source_backoff_minutes": [15, 60, 240, 1440],
            "cell_backoff_minutes": [60, 360, 1440],
        },
    },
    "analytics": {
        "windows": [30, 90],
        "min_window_days": 30,
        "min_postings_for_skill": 20,
        "min_companies_for_skill": 3,
        "max_company_share": 0.40,
        "min_postings_for_scope": 20,
        "min_coverage_fraction": 0.50,
        "min_observations_per_cell": 1,
        "min_salary_samples": 12,
        "exclude_agencies": True,
        "gap_weights": {
            "blocking_gap": 0.40,
            "demand": 0.30,
            "adjacency": 0.20,
            "salary_lift": 0.10,
        },
    },
    "llm": {
        "provider": "auto",
        "scoring_model": "",
        "agent_model": "",
        "timeout_seconds": 60,
    },
    "profile": {"max_interview_questions": 15},
    "scoring": {
        "concurrency": 16,
        "include_skill_hint": False,
        "max_usd_per_run": 8.00,
        "postpone_deepseek_peak_hours": True,
    },
    "scheduler": {
        "search": {
            # Effective source defaults are resolved in search_schedule.py.
            "sources": {},
            "jitter_minutes": 15,
            "persistent_catchup": True,
            "timeout_minutes": 90,
        },
        "score": {
            "interval_minutes": 30,
            "jitter_minutes": 3,
            "timeout_minutes": 90,
        },
        "chaining": {"search_triggers_score": True},
    },
}


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `incoming` over `base` without dropping keys absent from `incoming`.

    Used for two things that used to be separate and inconsistent: layering config.yaml
    over the built-in defaults, and layering a partial POST /api/config body over the
    config on disk.

    The old defaults merge only descended two levels, so a config.yaml that specified
    `scraper.budgets.linkedin.searches_per_run` and nothing else inherited *no* other
    linkedin budget key -- the third level was copied wholesale or not at all. That is
    exactly the shape the new `scoring:` block has.
    """
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        return deepcopy(DEFAULT_CONFIG)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001 - a malformed config.yaml must fall back to defaults, not crash startup
            print(f"Error loading config.yaml: {e}", file=sys.stderr)
            return deepcopy(DEFAULT_CONFIG)

    merged = deep_merge(deepcopy(DEFAULT_CONFIG), config)
    if not os.path.exists(CONFIG_LOCAL_PATH):
        return merged

    with open(CONFIG_LOCAL_PATH, encoding="utf-8") as f:
        try:
            local_config = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001 - a malformed local override must not stop startup
            print(f"Error loading config.local.yaml: {e}", file=sys.stderr)
            return merged
    return deep_merge(merged, local_config)


def save_config(config_data: dict[str, Any]) -> None:
    """Persist user overrides without modifying the version-controlled defaults.

    Keeping only the local delta means `git pull` can update config.yaml without
    overwriting settings made in the dashboard or creating merge conflicts.
    """
    existing: dict[str, Any] = {}
    if os.path.exists(CONFIG_LOCAL_PATH):
        with open(CONFIG_LOCAL_PATH, encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    with open(CONFIG_LOCAL_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            deep_merge(existing, config_data), f, default_flow_style=False, sort_keys=False
        )
