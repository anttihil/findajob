"""Execution of a user-requested board query outside the scheduled cell rotation."""

from __future__ import annotations

from typing import Any, cast

from findajob.core.config import load_config
from findajob.core.database import Database
from findajob.search.guard import SourceCircuit
from findajob.search.proxies import load_proxies
from findajob.search.runner import run_cell
from findajob.search.scheduler import ScrapeTaskPayload, results_wanted_for
from findajob.search.sources.jobspy_source import JobSpySource
from findajob.search.traffic import SourceTraffic

SUPPORTED_SOURCES = frozenset({"indeed", "linkedin"})


def run_query(args: Any) -> dict[str, Any]:
    """Fetch and persist an explicit query without altering configured cells."""
    if args.source not in SUPPORTED_SOURCES:
        raise ValueError(f"unsupported source {args.source!r}; choose indeed or linkedin")
    if not args.query.strip() or not args.location.strip():
        raise ValueError("--query and --location must not be empty")

    config = load_config()
    proxies = load_proxies(config)
    scraper_config = config.get("scraper", {})
    wanted = (
        args.results_wanted
        if args.results_wanted is not None
        else results_wanted_for(scraper_config, args.source)
    )
    if wanted <= 0:
        raise ValueError("--results-wanted must be positive")
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
        "results_wanted": wanted,
        "hours_old": args.hours_old,
        "fetch_description": True,
        "desc_selection": "census",
        "active": True,
        "proxies": proxies,
    }
    db = Database()
    run_id = db.start_sync_run("explicit_query")
    assert run_id is not None
    circuit = SourceCircuit(args.source, scraper_config, db=db)
    traffic = SourceTraffic(args.source, scraper_config, proxies, circuit)
    try:
        if circuit.persisted_backoff_active():
            raise ValueError(
                f"{args.source} is in circuit-breaker backoff until {circuit.backoff_until()}"
            )
        result = run_cell(
            db,
            JobSpySource(),
            circuit,
            cast(ScrapeTaskPayload, task),
            traffic,
            run_id,
            scraper_config,
        )
        db.finish_sync_run(
            run_id,
            "partial"
            if result["status"] == "budget_limited"
            else "failed"
            if result["status"] == "error"
            else "ok",
            cells_planned=1,
            cells_succeeded=int(result["status"] != "error"),
            postings_fetched=result["postings_fetched"],
            postings_new=result["postings_new"],
            duplicates_merged=result["duplicates_merged"],
        )
        return {**result, "query": args.query, "location": args.location, "source": args.source}
    except Exception as exc:
        db.finish_sync_run(run_id, "failed", error_summary=str(exc))
        raise
    finally:
        traffic.close()
        db.close()
