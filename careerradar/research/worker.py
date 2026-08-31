"""Drain the research queue: high-scoring postings, grouped by company.

Runs after scoring, on a daily timer -- deep research is the expensive stage and the one
whose answers age slowest, so it does not belong on the same cadence as either scraping or
scoring.
"""

from datetime import datetime, timezone
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.llm import MissingApiKey, get_model_for_role
from careerradar.core.logger import get_logger
from careerradar.profile.repository import load_active
from careerradar.research import repository as research_repo
from careerradar.research.graph import build_graph
from careerradar.research.tools import search_available

logger = get_logger()


DEFAULT_RESEARCH_GATE = research_repo.DEFAULT_RESEARCH_GATE
_gate_sql = research_repo._gate_sql


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidates(
    db: Database,
    gate: dict[str, Any],
    refresh_days: int,
    limit: int | None,
    company: str | None = None,
) -> list[dict[str, Any]]:
    return research_repo.get_research_candidates(
        db.conn,
        gate=gate,
        refresh_days=refresh_days,
        limit=limit,
        company=company,
    )


def _persist(
    db: Database,
    row: dict[str, Any],
    dossier: dict[str, Any],
    model: str,
    profile_version: int,
    cost: float,
) -> None:
    research_repo.save_dossier(
        db.conn,
        row=row,
        dossier=dossier,
        model=model,
        profile_version=profile_version,
        cost=cost,
    )


def run_research(company: str | None = None, limit: int | None = None) -> int:
    config = load_config()
    research_config = config.get("research") or {}
    scoring_config = config.get("scoring") or {}
    model = research_config.get("model") or get_model_for_role("agent")
    gate = scoring_config.get("research_gate") or DEFAULT_RESEARCH_GATE
    refresh_days = research_config.get("refresh_days", 30)
    limit = limit or research_config.get("max_companies_per_run", 5)

    loaded = load_active()
    if loaded is None:
        logger.error("No active profile. Build one first: careerradar profile build")
        return 1
    profile_version, _profile, summary = loaded

    if not search_available():
        print("TAVILY_API_KEY is not set.")
        print("Company intel and contacts need it; nearby-job discovery does not.")
        print("Continuing with nearby jobs only.\n")

    db = Database()
    try:
        rows = _candidates(db, gate, refresh_days, limit, company=company)
        if not rows:
            if company:
                print(f"No postings found for {company!r}.")
            else:
                criteria = "; ".join(f"{k} in {v}" for k, v in gate.items())
                print(
                    "Nothing to research: no company has a live posting matching the "
                    f"research gate ({criteria}) without a dossier newer than "
                    f"{refresh_days} days."
                )
            return 0

        print(f"profile v{profile_version} · model {model}")
        print(f"companies to research: {len(rows)}")
        for row in rows:
            print(
                f"  {row['company'][:44]:<46} tier {row['best_tier']}  "
                f"({row['postings']} posting(s))"
            )

        run_id = research_repo.start_research_run(db.conn)

        graph = build_graph()
        done = 0
        for row in rows:
            print(f"\nresearching {row['company']}…")
            try:
                state = graph.invoke(
                    {
                        "company": row["company"],
                        "company_normalized": row["company_normalized"],
                        "role_family": row["role_family"],
                        "location_id": row["location_id"],
                        "job_id": row["job_id"],
                        "profile_summary": summary,
                        "model": model,
                    }
                )
            except Exception:
                logger.warning("Research failed for %s", row["company"], exc_info=True)
                continue

            dossier = state.get("dossier")
            if not dossier:
                logger.warning("No dossier produced for %s", row["company"])
                continue

            _persist(db, row, dossier, model, profile_version, cost=0.0)
            db.conn.commit()
            done += 1

            intel = dossier.get("intel") or {}
            print(f"  {intel.get('summary', '')[:150]}")
            print(
                f"  contacts: {len(dossier.get('contacts') or [])} · "
                f"other openings: {len(dossier.get('nearby_jobs') or [])} · "
                f"sources: {len(dossier.get('sources') or [])}"
            )
            if dossier.get("application_angle"):
                print(f"  angle: {dossier['application_angle'][:170]}")

        if run_id is not None:
            research_repo.finish_research_run(
                db.conn, run_id=run_id, companies_count=done, status="ok"
            )
        print(f"\n{done} dossier(s) written.")
        return 0
    except MissingApiKey as exc:
        logger.error(str(exc))
        return 1
    finally:
        db.close()
