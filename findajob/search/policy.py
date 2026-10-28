"""One explicit source policy, independent of proxy connectivity."""

import math
from typing import Any

from findajob.core.config import DEFAULT_CONFIG


def source_policy(config: dict[str, Any], source: str) -> dict[str, Any]:
    policy = dict(DEFAULT_CONFIG["scraper"]["policies"][source])
    policy.update(config.get("policies", {}).get(source, {}))
    for name in ("requests_per_proxy", "results_wanted"):
        value = policy[name]
        if (
            isinstance(value, bool)
            or int(value) != value
            or int(value) < (name == "results_wanted")
        ):
            raise ValueError(f"{source} {name} must be a nonnegative integer (results_wanted > 0)")
    interval = float(policy["request_interval_seconds"])
    if not math.isfinite(interval) or interval < 0:
        raise ValueError(f"{source} request_interval_seconds must be nonnegative")
    return policy
