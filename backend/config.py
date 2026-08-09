import os
import yaml

# Simple .env loader
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip().strip('"').strip("'")

load_env()

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.yaml")

def load_config():
    # Fallback defaults only, for a missing config.yaml. The real configuration lives in
    # config.yaml; the search space itself now comes from data/roles.yaml (role families x
    # locations), not from a flat query list.
    defaults = {
        "resumes_dir": "resumes",
        "scraper": {
            "sources": {"indeed": True, "linkedin": True},
            "page_size": {"indeed": 15, "linkedin": 25},
            "cadence_hours": {"core": 24, "adjacent": 72, "breadth": 168},
            "hours_old_floor": {"core": 72, "adjacent": 168, "breadth": 336},
            "max_hours_old": 336,
            "backfill_hours_old": 336,
            "max_staleness_hours": 72,
            "starvation_multiple": 4,
            "budgets": {
                "indeed": {"searches_per_run": 20, "request_units": 200,
                           "results_wanted_default": 75, "max_results_wanted": 200,
                           "fetch_descriptions": True, "desc_selection": "census"},
                "linkedin": {"searches_per_run": 6, "request_units": 60,
                             "results_wanted_default": 50, "max_results_wanted": 75,
                             "max_pages_per_run": 14,
                             "fetch_descriptions": "budgeted",
                             "desc_selection": "top_k"},
            },
        },
        "matching": {
            "min_match_score": 15,
            "llm": {"enabled": False, "model": "claude-opus-5", "top_n": 25,
                    "max_usd_per_run": 1.0},
        },
        "analytics": {
            "windows": [30, 90],
            "min_window_days": 30,
            "min_postings_for_skill": 20,
            "exclude_agencies": True,
            "weighting_mode": "interest",
        },
        "digest": {
            "enabled": True,
            "min_score_for_digest": 40,
            "include_skill_gap": True,
        },
        "min_match_score": 15
    }

    if not os.path.exists(CONFIG_PATH):
        return defaults
    
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        try:
            config = yaml.safe_load(f) or {}
            # Merge defaults for missing top-level keys
            for k, v in defaults.items():
                if k not in config:
                    config[k] = v
                elif isinstance(v, dict) and isinstance(config[k], dict):
                    # Merge nested dicts
                    for subk, subv in v.items():
                        if subk not in config[k]:
                            config[k][subk] = subv
            return config
        except Exception as e:
            print(f"Error loading config.yaml: {e}")
            return defaults

def save_config(config_data):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)

