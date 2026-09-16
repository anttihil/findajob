"""Rotating proxy pool support.

Credentials live in the environment (`SCRAPER_PROXIES`), never in config.yaml, because that
file is committed.

A rotating gateway changes what a 429 means. Without proxies, a 429 says the account's single
IP is flagged and retrying deepens the flag -- so the circuit trips the source for the rest of
the run. With a rotating pool, it says one exit IP got flagged, and the next request arrives
from a different one, so retrying is the correct response. That difference is also what lifts
the LinkedIn budget: it was throttled to 6 searches a run on the assumption of a ~10-pages-
per-IP wall, which a direct probe (scripts/probe_linkedin_page_wall.py, 2026-08-14) found no
evidence of through 99 consecutive pages on one proxied IP -- see
experiments/linkedin_page_wall/. The budget stays conservative for now regardless, since
lifting it is a separate decision from correcting the comment that used to justify it.
"""

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

    proxies = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if proxies:
        logger.info(
            f"Proxy pool: {len(proxies)} endpoint(s) from ${ENV_VAR}"
            f"{' (rotating)' if settings.get('rotating', True) else ''}"
        )
    return proxies


def pin_for(proxies: list[str], key: int | str, attempt: int = 0) -> list[str]:
    """Pick ONE endpoint from the pool, as a single-element list.

    JobSpy reassigns `session.proxies` from its cycle on every request (util.py), so handing
    it the whole pool means a different exit IP per request -- and therefore a fresh TCP
    connect plus TLS handshake per request, because a changed proxy is a different
    connection-pool key. Measured against the pool, TLS setup alone runs ~0.32-0.47s versus
    ~0.03s direct, and LinkedIn spends one request per description, so that handshake is
    paid 50+ times per cell.

    Pinning one endpoint for the whole cell restores keep-alive without giving up rotation:
    the pool still rotates, just at cell granularity rather than request granularity. That
    was also thought to fit the per-IP page wall better -- see the module docstring for why
    that wall's ~10-page figure didn't hold up under direct measurement.

    `attempt` shifts the choice, so a retry after a rate-limit lands on a different exit IP
    rather than hammering the one that was just flagged.
    """
    if not proxies:
        return []
    return [proxies[(int(key) + int(attempt)) % len(proxies)]]


def is_rotating(config: dict[str, Any] | None = None) -> bool:
    settings = ((config or {}).get("scraper", {}) or {}).get("proxies") or {}
    return bool(settings.get("rotating", True))


def apply_proxy_budgets(scraper_config: dict[str, Any], proxies: list[str]) -> dict[str, Any]:
    """Return a scraper config with the with-proxies budget overrides applied.

    Returns the input unchanged when no proxies are available, so the conservative
    single-IP budgets remain the default.
    """
    if not proxies:
        return scraper_config

    overrides = ((scraper_config.get("proxies") or {}).get("with_proxies")) or {}
    if not overrides:
        return scraper_config

    merged = dict(scraper_config)
    budgets = {name: dict(values) for name, values in (scraper_config.get("budgets") or {}).items()}
    for source, changes in overrides.items():
        budgets.setdefault(source, {}).update(changes)
        logger.info(f"Proxy budgets applied to {source}: {changes}")
    merged["budgets"] = budgets
    return merged


def redact(proxy: str | None) -> str | None:
    """Hide credentials before a proxy string reaches a log line."""
    if not proxy or "@" not in proxy:
        return proxy
    return "***@" + proxy.rsplit("@", 1)[1]
