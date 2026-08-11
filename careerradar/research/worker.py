"""Drain the research queue: high-scoring postings, grouped by company.

Runs after scoring, on a daily timer -- deep research is the expensive stage and the one
whose answers age slowest, so it does not belong on the same cadence as either scraping or
scoring.
"""

import json
from datetime import datetime, timedelta, timezone

from careerradar.core.config import load_config
from careerradar.core.database import Database
from careerradar.core.llm import DEFAULT_AGENT_MODEL, MissingApiKey
from careerradar.core.logger import get_logger
from careerradar.profile.store import load_active
from careerradar.research.graph import build_graph
from careerradar.research.tools import search_available

logger = get_logger()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _candidates(db, threshold, refresh_days, limit, company=None):
    """Companies worth researching, best posting first.

    Grouped by `company_normalized` so a company with a dozen strong postings is one unit
    of work. `MAX(fit_score)` picks the posting that earned the research, and its
    role_family and location drive the nearby-jobs queries.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=refresh_days)).isoformat()

    if company:
        where = "AND (j.company_normalized = ? OR j.company = ?)"
        params = [company, company]
    else:
        where = "AND j.fit_score >= ?"
        params = [threshold]

    query = f"""
        SELECT j.company_normalized,
               MAX(j.fit_score)                AS best_score,
               COUNT(*)                        AS postings,
               MAX(j.company)                  AS company,
               MAX(j.id)                       AS job_id,
               MAX(j.role_family)              AS role_family,
               MAX(c.location_id)              AS location_id
          FROM jobs j
          LEFT JOIN scrape_cells c ON c.id = j.scrape_cell_id
          LEFT JOIN company_dossiers d ON d.company_normalized = j.company_normalized
         WHERE j.duplicate_of IS NULL
           AND j.company_normalized IS NOT NULL
           {where}
           AND (d.generated_at IS NULL OR d.generated_at < ?)
         GROUP BY j.company_normalized
         ORDER BY best_score DESC
    """
    params.append(cutoff)
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in db.conn.execute(query, params)]


def _persist(db, row, dossier, model, profile_version, cost):
    db.conn.execute(
        """
        INSERT INTO company_dossiers
            (company_normalized, company_display, generated_at, profile_version, model,
             intel_json, contacts_json, nearby_jobs_json, sources_json, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_normalized) DO UPDATE SET
            company_display  = excluded.company_display,
            generated_at     = excluded.generated_at,
            profile_version  = excluded.profile_version,
            model            = excluded.model,
            intel_json       = excluded.intel_json,
            contacts_json    = excluded.contacts_json,
            nearby_jobs_json = excluded.nearby_jobs_json,
            sources_json     = excluded.sources_json,
            cost_usd         = excluded.cost_usd
        """,
        (
            row["company_normalized"], dossier.get("company") or row["company"], _now(),
            profile_version, model,
            json.dumps({**(dossier.get("intel") or {}),
                        "application_angle": dossier.get("application_angle")}),
            json.dumps(dossier.get("contacts") or []),
            json.dumps(dossier.get("nearby_jobs") or []),
            json.dumps(dossier.get("sources") or []),
            cost,
        ),
    )
    dossier_id = db.conn.execute(
        "SELECT id FROM company_dossiers WHERE company_normalized = ?",
        (row["company_normalized"],),
    ).fetchone()["id"]
    db.conn.execute(
        "UPDATE jobs SET dossier_id = ?, pipeline_state = 'researched' "
        "WHERE company_normalized = ? AND duplicate_of IS NULL",
        (dossier_id, row["company_normalized"]),
    )


def run_research(company=None, limit=None, dry_run=False):
    config = load_config()
    research_config = config.get("research") or {}
    scoring_config = config.get("scoring") or {}
    model = research_config.get("model", DEFAULT_AGENT_MODEL)
    threshold = scoring_config.get("research_threshold", 70)
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
        rows = _candidates(db, threshold, refresh_days, limit, company=company)
        if not rows:
            if company:
                print(f"No postings found for {company!r}.")
            else:
                print(f"Nothing to research: no company has a posting scoring >= {threshold} "
                      f"without a dossier newer than {refresh_days} days.")
            return 0

        print(f"profile v{profile_version} · model {model}")
        print(f"companies to research: {len(rows)}")
        for row in rows:
            print(f"  {row['company'][:44]:<46} best fit {row['best_score']}  "
                  f"({row['postings']} posting(s))")

        if dry_run:
            print("\n(dry run -- nothing researched, nothing written)")
            return 0

        run_id = db.conn.execute(
            "INSERT INTO research_runs (started_at, status) VALUES (?, 'running')",
            (_now(),),
        ).lastrowid
        db.conn.commit()

        graph = build_graph()
        done = 0
        for row in rows:
            print(f"\nresearching {row['company']}…")
            try:
                state = graph.invoke({
                    "company": row["company"],
                    "company_normalized": row["company_normalized"],
                    "role_family": row["role_family"],
                    "location_id": row["location_id"],
                    "job_id": row["job_id"],
                    "profile_summary": summary,
                    "model": model,
                })
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
            print(f"  contacts: {len(dossier.get('contacts') or [])} · "
                  f"other openings: {len(dossier.get('nearby_jobs') or [])} · "
                  f"sources: {len(dossier.get('sources') or [])}")
            if dossier.get("application_angle"):
                print(f"  angle: {dossier['application_angle'][:170]}")

        db.conn.execute(
            "UPDATE research_runs SET finished_at = ?, companies = ?, status = 'ok' "
            "WHERE id = ?",
            (_now(), done, run_id),
        )
        db.conn.commit()
        print(f"\n{done} dossier(s) written.")
        return 0
    except MissingApiKey as exc:
        logger.error(str(exc))
        return 1
    finally:
        db.close()
