"""Search orchestration for LinkedIn detail reuse and bounded enrichment."""

from datetime import datetime, timedelta, timezone
from typing import Any

from findajob.core.database import Database
from findajob.core.logger import get_logger
from findajob.search.guard import (
    ERROR_BLOCKED,
    ERROR_RATE_LIMIT,
    SourceCircuit,
    SourceTripped,
    classify_error,
)
from findajob.search.normalizer import description_quality
from findajob.search.repository import get_linkedin_details
from findajob.search.sources.linkedin_details import LinkedInDetailsAdapter

logger = get_logger()
DETAIL_FIELDS = ("description", "company_industry", "url_direct")
TRACKING_FIELDS = ("details_fetched_at", "details_attempted_at", "details_error")


def _recent(stamp: str | None, now: datetime, hours: float) -> bool:
    if not stamp:
        return False
    try:
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return timedelta(0) <= now - moment < timedelta(hours=hours)
    except ValueError:
        return False


def enrich_linkedin(
    db: Database,
    postings: list[dict[str, Any]],
    adapter: LinkedInDetailsAdapter,
    circuit: SourceCircuit,
    config: dict[str, Any],
    *,
    fetch: bool,
    now: datetime,
) -> tuple[dict[str, int], dict[str, int]]:
    """Merge persisted details before fetching misses; never discard discoveries.

    No selection uses fit scores. Counts distinguish reusable content from a new
    successful fetch. The current page finishes even when its request allowance has been spent.
    Failed details are retried on a later sighting, after the failure cooldown.
    """
    cached = get_linkedin_details(db.conn, [p["job_key"] for p in postings])
    counts: dict[str, int] = dict.fromkeys(
        ("fetched", "reused", "failed", "missing", "skipped", "stale"), 0
    )
    cost = {"duration_ms": 0, "requests_made": 0}
    ttl = float(config.get("linkedin_details_ttl_hours", 168))
    retry = float(config.get("linkedin_details_retry_hours", 1))
    blocked = False
    all_fresh = True
    for posting in postings:
        previous = cached.get(posting["job_key"])
        changed = previous is not None and any(
            posting.get(field) != previous.get(field) for field in ("title", "company", "location")
        )
        if previous:
            posting.update(
                {field: previous.get(field) for field in (*DETAIL_FIELDS, *TRACKING_FIELDS)}
            )
        if changed:
            # The cached description belongs to the old listing metadata. Keep it
            # available as fallback content, but do not let its timestamp mark it fresh.
            posting["details_fetched_at"] = None
        complete = description_quality(posting.get("description")) == "full"
        fresh = complete and not changed and _recent(posting.get("details_fetched_at"), now, ttl)
        needed = not fresh
        cooldown = bool(posting.get("details_error")) and _recent(
            posting.get("details_attempted_at"), now, retry
        )
        fetched = False
        if fetch and needed and not cooldown and not circuit.is_open and not blocked:
            try:
                details = adapter.fetch(str(posting["site_job_id"]))
                posting.update(
                    description=details.description,
                    company_industry=details.company_industry,
                    url_direct=details.url_direct,
                    details_fetched_at=now.isoformat(),
                    details_error=None,
                )
                counts["fetched"] += 1
                complete, fetched = True, True
                # A successful detail response breaks a consecutive failure streak.
                circuit.on_detail_success()
            except SourceTripped:
                counts["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - discovery survives enrichment failures
                posting["details_error"] = f"{type(exc).__name__}: {exc}"[:500]
                counts["failed"] += 1
                circuit.on_error(exc)
                blocked = classify_error(exc) in (ERROR_BLOCKED, ERROR_RATE_LIMIT)
                logger.warning("LinkedIn details failed for %s: %s", posting["site_job_id"], exc)
            finally:
                measured = adapter.last_fetch
                for key in cost:
                    cost[key] += measured.get(key, 0)
                adapter.last_fetch = {"requests_made": 0, "duration_ms": 0}
                posting["details_attempted_at"] = now.isoformat()
        elif fetch and needed:
            counts["skipped"] += 1
        if complete and not (fresh or fetched):
            counts["stale"] += 1
        if complete and previous and not fetched:
            counts["reused"] += 1
        if not complete:
            counts["missing"] += 1
        posting["description_quality"] = description_quality(posting.get("description"))
        all_fresh = all_fresh and complete and (fresh or fetched)
        # Incomplete cohorts must not become a selected subset in skill analytics.
        posting["desc_selection"] = "available" if complete else "none"
    if postings and all_fresh:
        for posting in postings:
            # Complete content coverage, including reuse; not a network-fetch census.
            posting["desc_selection"] = "complete"
    return counts, cost
