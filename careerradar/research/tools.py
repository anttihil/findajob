"""What the research agent can reach.

Two sources, deliberately different in kind:

  web search   Tavily. Everything the agent learns about a company comes from here, with
               the URL kept so a claim can be traced back.

  local corpus The postings already scraped. "What else is this company hiring for?" and
               "who else hires this role in this location?" are questions the database
               answers for free, without a search call or a new scrape.

The local half works with no API key at all, so a missing Tavily key degrades the dossier
to its nearby-jobs section rather than failing the run.
"""

import os
from typing import Any

from careerradar.core.database import Database
from careerradar.core.logger import get_logger

logger = get_logger()

MAX_SEARCH_RESULTS = 6

# The verdict for the ACTIVE profile, and no other. Identical to `Database._FROM`, and
# spelled out once here for the same reason it is spelled out once there: the two queries
# below both rank by the verdict, and a join that differed between them would rank the
# two halves of the dossier's "other openings" list on different profiles.
#
# These queries used to read `jobs.fit_score` -- a denormalized copy that `scoring/worker`
# writes on whichever profile scored the posting last, carrying no version with it. That
# number reaches the synthesize prompt and then `company_dossiers.nearby_jobs_json`, so a
# stale one does not just render wrong, it is reasoned over and cached. The stage already
# refuses to run on a stale profile -- `worker._candidates` gates on this same join -- so
# reading around it here was inconsistency rather than a decision.
_ACTIVE_VERDICT_JOIN = """
          job_verdicts v
                 ON v.job_id = j.id
                AND v.profile_version = (
                    SELECT version FROM profiles WHERE is_active = 1
                )
"""


class SearchUnavailable(RuntimeError):
    pass


def search_available() -> bool:
    return bool(os.environ.get("TAVILY_API_KEY"))


def web_search(
    query: str, max_results: int = MAX_SEARCH_RESULTS, include_raw: bool = False
) -> list[dict[str, Any]]:
    """One web search. Returns [{title, url, content}]."""
    if not search_available():
        raise SearchUnavailable(
            "TAVILY_API_KEY is not set. Add it to .env to enable company intel; "
            "nearby-job discovery works without it."
        )
    from langchain_tavily import TavilySearch

    tool = TavilySearch(max_results=max_results, include_raw_content=include_raw)
    try:
        payload = tool.invoke({"query": query})
    except Exception:
        logger.warning("Tavily search failed for %r", query, exc_info=True)
        return []

    results = payload.get("results", []) if isinstance(payload, dict) else payload
    return [
        {
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": (r.get("content") or "")[:2000],
        }
        for r in (results or [])
    ]


def render_results(results: list[dict[str, Any]]) -> str:
    if not results:
        return "(no results)"
    return "\n\n".join(
        f'<result url="{r["url"]}">\n{r["title"]}\n{r["content"]}\n</result>' for r in results
    )


def same_company_openings(
    company_normalized: str,
    exclude_job_id: int | None = None,
    limit: int = 15,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Other postings from this company already in the corpus."""
    owned = db is None
    db = db or Database()
    try:
        # LEFT, unlike the sibling below: this answers "what else is open here", which is
        # an inventory question. A posting the active profile has not judged yet still
        # belongs on the list, it just has no score to rank on and sorts last.
        query = f"""
            SELECT j.id, j.title, j.company, j.location, j.url, v.fit_score, j.role_family
              FROM jobs j
              LEFT JOIN {_ACTIVE_VERDICT_JOIN}
             WHERE j.company_normalized = ? AND j.duplicate_of IS NULL
        """
        params: list[Any] = [company_normalized]
        if exclude_job_id:
            query += " AND j.id != ?"
            params.append(exclude_job_id)
        query += " ORDER BY v.fit_score DESC NULLS LAST, j.date_found DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in db.conn.execute(query, params)]
    finally:
        if owned:
            db.close()


def nearby_company_openings(
    role_family: str,
    location_id: str,
    exclude_company: str,
    limit: int = 12,
    db: Database | None = None,
) -> list[dict[str, Any]]:
    """Strong postings for the same kind of role in the same market, elsewhere.

    This is the "nearby companies" half. It is a query, not a scrape: the cell matrix has
    already swept this role family in this location, so the answer is sitting in the
    corpus. Ranked by the scoring agent's own verdict, so 'nearby' means 'nearby and
    actually a good fit', not merely adjacent.
    """
    owned = db is None
    db = db or Database()
    try:
        # Inner, unlike the sibling above: this one claims the postings it returns are a
        # good fit, and that claim needs a verdict from the profile in force. The old
        # `AND j.fit_score IS NOT NULL` guard is gone because the join now does that job --
        # `job_verdicts.fit_score` is NOT NULL.
        rows = db.conn.execute(
            f"""
            SELECT j.id, j.title, j.company, j.location, j.url, v.fit_score
              FROM jobs j
              JOIN scrape_cells c ON c.id = j.scrape_cell_id
              JOIN {_ACTIVE_VERDICT_JOIN}
             WHERE j.role_family = ?
               AND c.location_id = ?
               AND (j.company_normalized IS NULL OR j.company_normalized != ?)
               AND j.duplicate_of IS NULL
             ORDER BY v.fit_score DESC
             LIMIT ?
            """,
            (role_family, location_id, exclude_company, limit),
        )
        return [dict(r) for r in rows]
    finally:
        if owned:
            db.close()
