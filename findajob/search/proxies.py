"""Rotating proxy pool support.

Credentials live in the environment (`SCRAPER_PROXIES`), never in config.yaml, because that
file is committed.

"""

import hashlib
import os
from typing import Any

from findajob.core.logger import get_logger

logger = get_logger()

ENV_VAR = "SCRAPER_PROXIES"


def load_proxies(config: dict[str, Any] | None = None) -> list[str]:
    """Proxy list from the environment, or [] when unset or disabled.

    Accepts a single endpoint or a comma-separated list. JobSpy wants
    'user:pass@host:port' entries.
    """
    scraper = (config or {}).get("scraper", {}) or {}
    settings = scraper.get("proxies") or {}
    if not settings.get("enabled", True):
        return []

    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return []

    proxies = list(dict.fromkeys(entry.strip() for entry in raw.split(",") if entry.strip()))
    if proxies:
        logger.info("Proxy pool: %d unique endpoint(s) from $%s", len(proxies), ENV_VAR)
    return proxies


def redact(proxy: str | None) -> str | None:
    """Hide credentials before a proxy string reaches a log line."""
    if not proxy or "@" not in proxy:
        return proxy
    return "***@" + proxy.rsplit("@", 1)[1]


def proxy_id(proxy: str | None) -> str:
    """Credential-safe persistent identifier for an endpoint (or 'direct')."""
    cleaned = (proxy or "").strip()
    if not cleaned:
        return "direct"
    host_port = cleaned.rsplit("@", 1)[-1]
    if "://" in host_port:
        host_port = host_port.split("://", 1)[1]
    host_port = host_port.rstrip("/")
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    return f"{host_port}#{digest}"
