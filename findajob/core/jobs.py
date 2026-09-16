"""Application operations for inspecting stored postings."""

from __future__ import annotations

from typing import Any

from findajob.core.database import Database


def list_postings(args: Any) -> dict[str, Any]:
    """Query postings with the dashboard's stable, safe filter set."""
    db = Database()
    try:
        return db.query_jobs(
            country=args.country,
            location=args.location,
            source=args.source,
            is_remote=True if args.remote else None,
            pipeline_state=args.pipeline_state,
            fit=args.fit,
            reason_type=args.reason_type,
            date_posted=args.posted_within,
            q=args.text,
            limit=args.limit,
            offset=args.offset,
            detail=True,
        )
    finally:
        db.close()
