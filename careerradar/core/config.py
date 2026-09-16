import os
import sys
from copy import deepcopy
from typing import Any

import yaml

from careerradar.core.paths import CONFIG_LOCAL_PATH, CONFIG_PATH, ENV_PATH


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
        "cadence_hours": 24,
        "hours_old_floor": 72,
        "max_hours_old": 168,
        "backfill_hours_old": 168,
        "max_staleness_hours": 72,
        "budgets": {
            "indeed": {
                "searches_per_run": 44,
                "request_units": 130,
                "results_wanted_default": 75,
                "max_results_wanted": 200,
                "min_seconds_between_searches": 2.0,
                "jitter_seconds": [0.5, 2.5],
                "fetch_descriptions": True,
                "desc_selection": "census",
            },
            "linkedin": {
                "searches_per_run": 15,
                "request_units": 60,
                "results_wanted_default": 60,
                "max_results_wanted": 100,
                "max_pages_per_run": 60,
                "min_seconds_between_searches": 6.0,
                "jitter_seconds": [2.0, 6.0],
                "fetch_descriptions": False,
                "desc_selection": "none",
            },
        },
        "proxies": {
            "enabled": True,
            "rotating": True,
            "with_proxies": {
                "linkedin": {
                    "searches_per_run": 30,
                    "request_units": 1700,
                    "results_wanted_default": 50,
                    "max_results_wanted": 75,
                    "max_pages_per_run": 180,
                    "min_seconds_between_searches": 2.0,
                    "jitter_seconds": [0.5, 2.0],
                    "fetch_descriptions": True,
                    "desc_selection": "census",
                }
            },
        },
        "circuit_breaker": {
            "consecutive_errors_to_trip": 3,
            "proxy_rotation_retries": 3,
            "http_429_trips_immediately": True,
            "transient_retries": 2,
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
        "fit_threshold": 70,
        "include_skill_hint": False,
        "max_usd_per_run": 8.00,
        "skip_seniority": ["lead", "staff"],
        "max_posting_age_days": 3,
    },
    "scheduler": {
        "enabled": False,
        "search": {
            "schedule": ["01:00", "07:00", "13:00", "19:00"],
            "jitter_minutes": 30,
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
