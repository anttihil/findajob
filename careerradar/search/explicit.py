"""Execution of a user-requested board query outside the scheduled cell rotation."""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timezone
from typing import Any, cast

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.search.guard import SourceCircuit, SourceTripped
from careerradar.search.normalizer import normalize_rows
from careerradar.search.proxies import apply_proxy_budgets, is_rotating, load_proxies, pin_for
from careerradar.search.runner import store_postings
from careerradar.search.scheduler import ScrapeTaskPayload
from careerradar.search.sources.jobspy_source import JobSpySource

SUPPORTED_SOURCES = frozenset({"indeed", "linkedin"})


def run_query(args: Any) -> dict[str, Any]:
    """Fetch and persist an explicit query without altering configured cells."""
    if args.source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported source {args.source!r}; choose indeed or linkedin")
    if not args.query.strip() or not args.location.strip():
        raise ValueError("--query and --location must not be empty")

    config = load_config()
    proxies = load_proxies(config)
    scraper_config = apply_proxy_budgets(config.get("scraper", {}), proxies)
    budget = (scraper_config.get("budgets") or {}).get(args.source) or {}
    fetch_descriptions = budget.get("fetch_descriptions", False) is True
    task: dict[str, Any] = {
        "cell_id": None,
        "source": args.source,
        "query": args.query,
        "location_id": "explicit_query",
        "location_label": args.location,
        "country": args.country,
        "indeed_country": args.indeed_country,
        "is_remote": args.remote,
        "distance": args.distance,
        "results_wanted": args.results_wanted,
        "hours_old": args.hours_old,
        "fetch_description": fetch_descriptions,
        "desc_selection": "census" if fetch_descriptions else "none",
        "active": True,
        "est_request_units": 0.0,
        "proxies": pin_for(proxies, 0, 0),
    }
    db = Database()
    run_id = db.start_sync_run("explicit_query")
    circuit = SourceCircuit(
        args.source,
        scraper_config,
        db=db,
        rotating_proxies=bool(proxies) and is_rotating(config),
    )
    client = JobSpySource()
    observed_at = datetime.now(timezone.utc)
    cost = {"duration_ms": 0, "requests_made": 0}
    try:
        if circuit.persisted_backoff_active():
            raise ValueError(
                f"{args.source} is in circuit-breaker backoff until {circuit.backoff_until()}"
            )
        circuit.before_request()
        with contextlib.redirect_stdout(sys.stderr):
            rows = client.fetch_for_task(cast(ScrapeTaskPayload, task))
        cost.update(client.last_fetch or {})
        postings, stats = normalize_rows(rows, task, observed_at=observed_at, config=scraper_config)
        job_ids, new_count, duplicates = store_postings(db, postings, run_id)
        circuit.on_success(stats["returned"]) if stats["returned"] else circuit.on_empty()
        db.record_observation(
            run_id,
            task,
            observed_at.isoformat(),
            returned=stats["returned"],
            new_unique=new_count,
            saturated=int(stats["returned"] >= args.results_wanted),
            descriptions_full=stats["with_full_description"],
            status="ok" if stats["returned"] else "empty",
            duration_ms=cost["duration_ms"],
            requests_made=cost["requests_made"],
        )
        assert run_id is not None
        db.finish_sync_run(
            run_id,
            "ok",
            cells_planned=1,
            cells_succeeded=int(bool(stats["returned"])),
            postings_fetched=stats["returned"],
            postings_new=new_count,
            duplicates_merged=duplicates,
        )
        return {
            "status": "ok" if stats["returned"] else "empty",
            "query": args.query,
            "location": args.location,
            "source": args.source,
            "job_ids": job_ids,
            "postings_fetched": stats["returned"],
            "postings_new": new_count,
            "duplicates_merged": duplicates,
            "descriptions_full": stats["with_full_description"],
            **cost,
        }
    except SourceTripped as exc:
        if run_id is not None:
            db.finish_sync_run(run_id, "failed", error_summary=str(exc))
        raise ValueError(f"{args.source} is in circuit-breaker backoff: {exc}") from exc
    except Exception as exc:
        if run_id is not None:
            db.finish_sync_run(run_id, "failed", error_summary=str(exc))
        raise
    finally:
        db.close()
