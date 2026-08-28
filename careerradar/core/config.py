import os
from typing import Any

import yaml

from careerradar.core.paths import CONFIG_PATH, ENV_PATH


# Simple .env loader
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


def deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `incoming` over `base` without dropping keys absent from `incoming`.

    Used for two things that used to be separate and inconsistent: layering config.yaml
    over the built-in defaults, and layering a partial POST /api/config body over the
    config on disk.

    The old defaults merge only descended two levels, so a config.yaml that specified
    `scraper.budgets.linkedin.searches_per_run` and nothing else inherited *no* other
    linkedin budget key -- the third level was copied wholesale or not at all. That is
    exactly the shape the new `scoring:` and `research:` blocks have.
    """
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict[str, Any]:
    # Fallback defaults only, for a missing config.yaml. The real configuration lives in
    # config.yaml; the search space itself now comes from data/roles.yaml (role families x
    # locations), not from a flat query list.
    defaults: dict[str, Any] = {
        "scraper": {
            "sources": {"indeed": True, "linkedin": True},
            "page_size": {"indeed": 100, "linkedin": 10},
            "requests_per_description": {"indeed": 0, "linkedin": 1},
            "cadence_hours": 24,
            "hours_old_floor": 72,
            "max_hours_old": 168,
            "backfill_hours_old": 168,
            "max_staleness_hours": 72,
            "starvation_multiple": 3,
            "budgets": {
                "indeed": {
                    "searches_per_run": 20,
                    "request_units": 60,
                    "results_wanted_default": 75,
                    "max_results_wanted": 200,
                    "fetch_descriptions": True,
                    "desc_selection": "census",
                },
                "linkedin": {
                    "searches_per_run": 6,
                    "request_units": 60,
                    "results_wanted_default": 50,
                    "max_results_wanted": 75,
                    "max_pages_per_run": 14,
                    "fetch_descriptions": False,
                    "desc_selection": "none",
                },
            },
        },
        "matching": {
            # No min_match_score. It defaulted to 15, which is the floor of the scale it
            # gated, so it never once changed an outcome -- and a config key that reads
            # like a working safety valve but is not is worse than no key. The `llm`
            # reranker block went with it: it pointed at backend/llm_scorer.py, which no
            # longer exists.
            "weights": {"skill_coverage": 0.62, "title_family": 0.24, "seniority_fit": 0.14},
        },
        "analytics": {
            "windows": [30, 90],
            "min_window_days": 30,
            "min_postings_for_skill": 20,
            "exclude_agencies": True,
            "weighting_mode": "interest",
        },
        "scheduler": {
            "enabled": True,
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
            "research": {
                "schedule": ["20:30"],
                "jitter_minutes": 20,
                "timeout_minutes": 45,
            },
            "chaining": {
                "search_triggers_score": True,
                "score_triggers_research": True,
            },
        },
        "scoring": {
            "max_posting_age_days": 3,
        },
    }

    if not os.path.exists(CONFIG_PATH):
        return defaults

    with open(CONFIG_PATH, encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f) or {}
        except Exception as e:  # noqa: BLE001 - a malformed config.yaml must fall back to defaults, not crash startup
            print(f"Error loading config.yaml: {e}")
            return defaults

    return deep_merge(defaults, config)


def save_config(config_data: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)
