"""What a scrape actually cost: wall time and HTTP requests, per cell and per source.

The numbers come from `cell_observations.duration_ms` and `.requests_made`, measured around
the JobSpy call (sources/jobspy_source.py), and are read through the `cell_cost` view so
ad-hoc SQL and this report cannot disagree.

The comparison worth making is `requests_made` against what `scheduler.estimate_units`
charged the cell against the run's request budget. Until 2026-08-15 that estimate counted
search pages only, so a LinkedIn description census was charged 5 requests and spent 56, and
`budgets.linkedin.request_units` bought a tenth of the cells it claimed. The estimate is now
description-aware; this report is how it stays honest, because a model checked only against
itself cannot be wrong.

Rows written before schema v11 have NULL for both columns and are reported as unmeasured
rather than as free.
"""

import argparse
import json
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.search import repository as search_repo
from careerradar.search.scheduler import estimate_units


def collect(db: Database, run_id: int | None = None, limit: int = 10) -> dict[str, Any]:
    run_id, run, cells = search_repo.get_sync_run_cost_data(db.conn, run_id=run_id)
    if run_id is None:
        return {"run_id": None, "run": None, "sources": [], "slowest": []}

    scraper = load_config().get("scraper", {}) or {}
    for cell in cells:
        # `desc_selection` is what the cell actually did, so the estimate is rebuilt on the
        # same terms the scheduler would have used for it. Charging a titles-only cell for
        # descriptions (or the reverse) would make the comparison meaningless.
        cell["est_requests"] = estimate_units(
            cell["source"],
            cell["requested"],
            scraper,
            fetch_descriptions=cell["desc_selection"] == "census",
        )

    sources: dict[str, dict[str, Any]] = {}
    for cell in cells:
        summary = sources.setdefault(
            cell["source"],
            {
                "source": cell["source"],
                "cells": 0,
                "unmeasured": 0,
                "returned": 0,
                "new_unique": 0,
                "requests": 0,
                "est_requests": 0.0,
                "seconds": 0.0,
            },
        )
        summary["cells"] += 1
        summary["returned"] += cell["returned"]
        summary["new_unique"] += cell["new_unique"]
        summary["est_requests"] += cell["est_requests"]
        if cell["duration_ms"] is None:
            summary["unmeasured"] += 1
            continue
        summary["requests"] += cell["requests_made"] or 0
        summary["seconds"] += cell["seconds"]

    for summary in sources.values():
        measured = summary["cells"] - summary["unmeasured"]
        summary["seconds_per_cell"] = summary["seconds"] / measured if measured else None
        summary["ms_per_request"] = (
            summary["seconds"] * 1000 / summary["requests"] if summary["requests"] else None
        )

    return {
        "run_id": run_id,
        "run": dict(run) if run else None,
        "sources": sorted(sources.values(), key=lambda s: -s["seconds"]),
        "slowest": [cell for cell in cells if cell["duration_ms"] is not None][:limit],
    }


def _num(value: float | None, fmt: str = "d") -> str:
    return "-" if value is None else format(value, fmt)


def _clock(seconds: float | None) -> str:
    if not seconds:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def render(report: dict[str, Any]) -> str:
    if report["run_id"] is None:
        return "No measured scrape yet -- cost is recorded from schema v11 onward."

    run = report["run"] or {}
    header = (
        f"  {'source':<10} {'cells':>7} {'time':>8} {'s/cell':>8} {'requests':>9} "
        f"{'planned':>8} {'ms/req':>8} {'returned':>9} {'new':>6}"
    )
    out = [
        (
            f"SCRAPE COST  run {report['run_id']}  {run.get('started_at', '?')}  "
            f"({run.get('mode')}, {run.get('status')})"
        ),
        "",
        header,
    ]
    for row in report["sources"]:
        cells = f"{row['cells']}" + (f" ({row['unmeasured']}?)" if row["unmeasured"] else "")
        # A source with nothing measured prints dashes rather than zeros: "0 requests in
        # 0.0s" is the shape of a free scrape, which is the one thing it was not.
        measured = row["cells"] - row["unmeasured"]
        requests = row["requests"] if measured else None
        out.append(
            f"  {row['source'][:10]:<10} {cells:>7} {_clock(row['seconds']):>8} "
            f"{_num(row['seconds_per_cell'], '.1f'):>8} {_num(requests):>9} "
            f"{row['est_requests']:>8.0f} {_num(row['ms_per_request'], '.0f'):>8} "
            f"{row['returned']:>9} {row['new_unique']:>6}"
        )

    unmeasured = sum(row["unmeasured"] for row in report["sources"])
    if unmeasured:
        out.append(f"  ({unmeasured} cell(s) marked ? were scraped before cost was measured)")

    # Measured against planned, per source: the two boards have different cost shapes, and
    # an average over both hides whichever one is wrong.
    for row in report["sources"]:
        est, made = row["est_requests"], row["requests"]
        if est and made > est * 1.2:
            out.append(
                f"  ! {row['source']} spent {made} requests where the scheduler planned "
                f"{est:.0f} ({made / est:.1f}x) -- budgets.{row['source']}.request_units "
                f"buys {est / made:.0%} of the cells it claims"
            )

    out += ["", "SLOWEST CELLS"]
    for row in report["slowest"]:
        out.append(
            f"  {_clock(row['seconds']):>7}  {row['requests_made'] or 0:>4} req "
            f"(planned {row['est_requests']:>3.0f})  {row['ms_per_request'] or 0:>5.0f} ms/req  "
            f"{row['source'][:8]:<8} {row['query'][:24]:<24} {row['location_id'][:12]:<12} "
            f"{row['returned']:>3} returned, {row['new_unique']:>3} new  {row['status']}"
        )
    return "\n".join(out)


def run_cost(run_id: int | None = None, limit: int = 10, as_json: bool = False) -> int:
    db = Database()
    try:
        report = collect(db, run_id=run_id, limit=limit)
    finally:
        db.close()
    print(json.dumps(report, indent=2, default=str) if as_json else render(report))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--run", type=int, default=None, help="sync_runs.id (default: latest)")
    parser.add_argument("--limit", type=int, default=10, help="how many slow cells to list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_cost(run_id=args.run, limit=args.limit, as_json=args.json))
