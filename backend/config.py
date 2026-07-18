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
    defaults = {
        "resumes_dir": "../resumes",
        "countries": ["US", "SE", "NO", "DK", "FI"],
        "search_queries": [
            "Software Engineer", "Full Stack Developer", 
            "DevOps Engineer", "AI Engineer", "Product Manager"
        ],
        "sources": {
            "gmail_imap": True
        },
        "gmail_imap": {
            "enabled": True,
            "email": "",
            "password_env_var": "GMAIL_APP_PASSWORD",
            "imap_server": "imap.gmail.com",
            "imap_port": 993
        },
        "digest": {
            "enabled": True,
            "min_score_for_digest": 40
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

