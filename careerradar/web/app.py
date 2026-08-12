import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict

from careerradar.market.analytics import MarketAnalytics
from careerradar.core.config import deep_merge, load_config, save_config
from careerradar.core.database import Database
from careerradar.core.logger import get_logger
from careerradar.market.gap_analysis import GapAnalysis
from careerradar.profile.adapter import NoActiveProfile, load_profile
from careerradar.taxonomy.roles import load_roles
from careerradar.search.scheduler import select_cells, with_location_weights
from careerradar.search.sources.link_generator import LinkGenerator
from careerradar.core.status_manager import (
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from careerradar.taxonomy.skills import load_taxonomy
from careerradar.search.runner import run_sync

app = FastAPI(title="Job Search Automation Dashboard")

logger = get_logger()

# Nothing else in this app authenticates anybody. PUT /api/jobs/{id}/status, POST /api/sync
# (a 15-45 minute scrape) and POST /api/config (an arbitrary deep-merge rewrite of
# config.yaml on disk) are all wide open, and the only thing that has ever protected them is
# the 127.0.0.1 bind. Putting the dashboard on a tailnet removes that protection for every
# device on the tailnet -- including ones belonging to other users the tailnet is shared
# with -- so the bind stays, `tailscale serve` fronts it, and this gate checks who the proxy
# says is calling.
#
# Tailscale Serve sets Tailscale-User-Login on each proxied request and strips any copy the
# client tried to supply, so the header is trustworthy *provided* nothing but Serve can
# reach the port. That is exactly what the loopback bind guarantees.
#
# Fail closed: with no owner configured, proxied requests are refused rather than waved
# through. A request with no identity header at all did not come through Serve, so it is a
# genuinely local caller -- the CLI, a health check, or a browser on the machine itself.
#
# Known gap: Serve does not populate identity headers for traffic from *tagged* devices, so
# a tagged node would arrive here looking like a local caller and be let through. There are
# no tagged devices on this tailnet today, and tags only exist when someone creates them
# deliberately. Closing it properly means giving up the loopback TCP port and having Serve
# proxy to a Unix socket instead (`tailscale serve unix:...` + `uvicorn --uds`), after which
# nothing but Serve can reach the app and a missing header can be refused outright.
OWNER_LOGIN = os.environ.get("CAREERRADAR_OWNER", "").strip()
IDENTITY_HEADER = "tailscale-user-login"

if not OWNER_LOGIN:
    logger.warning(
        "CAREERRADAR_OWNER is unset: every request arriving through `tailscale serve` will "
        "be refused. Set it in .env to the tailnet login allowed to use the dashboard."
    )


@app.middleware("http")
async def restrict_to_owner(request: Request, call_next):
    login = request.headers.get(IDENTITY_HEADER)
    if login is None:
        return await call_next(request)
    if login == OWNER_LOGIN:
        return await call_next(request)
    logger.warning("Refused dashboard request from tailnet user %s", login)
    return JSONResponse(
        {"detail": "Not authorised for this dashboard."}, status_code=403
    )


from careerradar.core.paths import FRONTEND_DIR, REPO_ROOT, TEMPLATE_DIR
from careerradar.web import rendering

BASE_DIR = REPO_ROOT


def get_db():
    return Database()


def analytics_context(db):
    """Shared objects for the analytics endpoints."""
    config = load_config()
    taxonomy = load_taxonomy()
    roles = load_roles()
    profile = load_profile(taxonomy=taxonomy)
    return config, taxonomy, roles, profile


# --- Request/Response Models -----------------------------------------------------------

class StatusUpdate(BaseModel):
    status: str


class ConfigUpdate(BaseModel):
    """Permissive on purpose.

    The previous model declared five fixed fields, and app.js posted only those plus
    gmail_imap. Pydantic silently dropped anything else, so once config.yaml grew a
    `scraper:` block, any save from the Settings tab rewrote the omitted sections from
    whatever the frontend last knew about. Accepting arbitrary keys and merging server-side
    means a partial update stays partial instead of becoming a partial overwrite.
    """
    model_config = ConfigDict(extra="allow")


# --- Jobs ------------------------------------------------------------------------------

@app.get("/api/jobs")
def get_jobs(
    status: Optional[str] = None,
    country: Optional[str] = None,
    role_family: Optional[str] = None,
    seniority: Optional[str] = None,
    source: Optional[str] = None,
    is_remote: Optional[bool] = None,
    has_salary: Optional[bool] = None,
    # commutable | remote | relocation. candidates have local or remote preferences, so this is the
    # filter that matters most: anything neither remote nor within commuting distance
    # requires moving house.
    access: Optional[str] = Query(None, pattern="^(commutable|remote|relocation)$"),
    include_duplicates: bool = False,
    min_score: Optional[int] = None,
    # The scalar projection of the ordinals. Kept for a coarse cut, but the ordinals below
    # are what the dashboard should filter on -- they say WHY a posting qualifies, and a
    # threshold on a projected scale cannot.
    min_fit_score: Optional[int] = None,
    verdict: Optional[str] = Query(
        None, pattern="^(strong|worth_applying|stretch|poor_fit|mismatch)$"
    ),
    eligibility: Optional[str] = Query(
        None, pattern="^(eligible|conditional|blocked)$"
    ),
    role_match: Optional[str] = Query(
        None, pattern="^(same_role|adjacent|different_domain|different_field)$"
    ),
    capability_match: Optional[str] = Query(
        None, pattern="^(exceeds|meets|most_with_gaps|major_gaps|not_close)$"
    ),
    # Pareto tier: 1 dominates everything below it. Filtering `max_tier=4` asks for the top
    # four layers without asserting an exchange rate between the dimensions.
    max_tier: Optional[int] = Query(None, ge=1, le=10),
    liveness: Optional[str] = Query(
        None, pattern="^(live|stale|likely_closed|unknown)$"
    ),
    pipeline_state: Optional[str] = Query(None, pattern="^(new|scored|researched)$"),
    sort: str = Query("fit", pattern="^(fit|fit_score|match_score|date_found)$"),
    # Paginated from the start: the corpus reaches thousands of rows within days, and
    # renderJobCards builds DOM for every row it receives.
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = get_db()
    try:
        return db.query_jobs(
            status=status, country=country,
            role_family=role_family, seniority=seniority, source=source,
            is_remote=is_remote, has_salary=has_salary, access=access,
            include_duplicates=include_duplicates, min_score=min_score,
            min_fit_score=min_fit_score, verdict=verdict,
            eligibility=eligibility, role_match=role_match,
            capability_match=capability_match, max_tier=max_tier, liveness=liveness,
            pipeline_state=pipeline_state, sort=sort,
            limit=limit, offset=offset,
        )
    finally:
        db.close()


@app.put("/api/jobs/{job_id}/status")
def update_job_status(job_id: int, payload: StatusUpdate):
    if payload.status not in ["unread", "saved", "applied", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status value")

    db = get_db()
    try:
        if not db.update_job_status(job_id, payload.status):
            raise HTTPException(status_code=404, detail="Job not found")
        return {"success": True, "status": payload.status}
    finally:
        db.close()


@app.get("/api/stats")
def get_stats():
    db = get_db()
    try:
        return db.get_stats()
    finally:
        db.close()


# --- Market analytics -------------------------------------------------------------------

@app.get("/api/market/supply")
def market_supply(
    window_days: int = Query(14, ge=1, le=365),
    location: str = Query(..., description="Required: flow is only comparable within one "
                                          "location and source"),
    source: str = Query("indeed"),
):
    """Role-family supply for one location and source.

    `location` is required rather than optional by design. Flow is comparable only within a
    comparability class of (location, source, hours_old bucket, results bucket), so a pooled
    cross-location ranking would be the one comparison that is not valid. The dashboard
    renders small multiples -- one panel per location -- instead.
    """
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        if location not in roles.locations:
            raise HTTPException(status_code=400, detail=f"Unknown location: {location}")
        return MarketAnalytics(db, config, roles, taxonomy, profile).role_supply(
            window_days=window_days, location_id=location, source=source
        )
    finally:
        db.close()


@app.get("/api/market/locations")
def market_locations():
    roles = load_roles()
    return {
        "locations": [location.to_dict() for location in roles.locations.values()],
        "role_families": [
            {"key": f.key, "label": f.label, "tier": f.tier, "resume": f.resume}
            for f in roles.families.values()
        ],
    }


@app.get("/api/market/coverage")
def market_coverage():
    """Per-cell scrape health. This is where a silently degrading scraper becomes visible."""
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        return {
            "cells": MarketAnalytics(db, config, roles, taxonomy, profile).coverage_report()
        }
    finally:
        db.close()


# --- Skills ----------------------------------------------------------------------------

@app.get("/api/skills/gap")
def skills_gap(
    window_days: int = Query(90, ge=1, le=365),
    location: Optional[str] = None,
    role_family: Optional[str] = None,
    weighting: Optional[str] = Query(None, pattern="^(interest|estimated_supply|observed)$"),
):
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        return GapAnalysis(db, config, roles, taxonomy, profile).analyse(
            window_days=window_days, location_id=location,
            role_family=role_family, weighting_mode=weighting,
        )
    finally:
        db.close()


@app.get("/api/skills/{skill}")
def skill_detail(skill: str, window_days: int = Query(90, ge=1, le=365),
                 limit: int = Query(40, ge=1, le=200)):
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        if skill not in taxonomy:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill}")
        return GapAnalysis(db, config, roles, taxonomy, profile).skill_detail(
            skill, window_days=window_days, limit=limit
        )
    finally:
        db.close()


@app.get("/api/profile")
def get_profile():
    """The active, LLM-built profile.

    A 404 rather than an empty profile: nothing downstream is meaningful without one, and
    a blank profile rendered as a real one is how the previous design let a missing corpus
    quietly shift every score.
    """
    from careerradar.profile.store import load_active_row

    record = load_active_row()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No active profile. Build one with: careerradar profile build",
        )
    return record


@app.get("/api/profile/versions")
def get_profile_versions():
    from careerradar.profile.store import list_versions

    return list_versions()


# --- Company dossiers ------------------------------------------------------------------

def dossier_for(db, company: Optional[str]) -> Optional[Dict[str, Any]]:
    """The deep-research dossier for one company, or None.

    Factored out so the drawer renders the dossier from the same read the JSON endpoint
    serves. The drawer used to fetch this itself and swallow every failure in a bare
    `catch`, which meant "no dossier" and "the request broke" looked identical.
    """
    from careerradar.search.normalizer import normalize_company

    if not company:
        return None
    row = db.conn.execute(
        "SELECT * FROM company_dossiers WHERE company_normalized = ? "
        "OR company_display = ?",
        (normalize_company(company), company),
    ).fetchone()
    if row is None:
        return None
    record = dict(row)
    for field in ("intel_json", "contacts_json", "nearby_jobs_json", "sources_json"):
        record[field.removesuffix("_json")] = json.loads(record.pop(field) or "null")
    return record


@app.get("/api/companies/{company}/dossier")
def get_company_dossier(company: str):
    """The deep-research dossier for one company, keyed on its normalized name."""
    db = get_db()
    try:
        record = dossier_for(db, company)
        if record is None:
            raise HTTPException(status_code=404, detail="No dossier for this company")
        return record
    finally:
        db.close()


# --- Config ----------------------------------------------------------------------------

@app.get("/api/config")
def get_current_config():
    return load_config()


@app.post("/api/config")
def update_current_config(payload: ConfigUpdate):
    """Merge the posted keys into the stored config.

    Merging rather than assigning is what makes a partial update safe -- see ConfigUpdate.
    """
    incoming = payload.model_dump(exclude_unset=True)
    merged = deep_merge(load_config(), incoming)
    save_config(merged)
    return {"success": True, "config": merged}


@app.get("/api/search-links")
def get_search_links(country: str, query: str, role_family: Optional[str] = None):
    """Boolean search links, built from the profile's strongest skills.

    Previously keyed on a parsed resume variant. The profile is now one unified artifact,
    so the skills come from it directly -- and they are the *canonical* skills the scorer
    uses, rather than whatever the regex parser found in a bulleted list.
    """
    taxonomy = load_taxonomy()
    try:
        profile = load_profile(taxonomy=taxonomy)
    except NoActiveProfile as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    strongest = [
        taxonomy.label(key)
        for key in sorted(profile.keys(min_level=3))
    ] or [taxonomy.label(k) for k in sorted(profile.keys(min_level=2))]
    return LinkGenerator.generate_links(strongest, country, query)


# --- Sync ------------------------------------------------------------------------------

def bg_sync_task():
    run_sync()


@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    # is_sync_running checks the owning PID rather than trusting the flag, so a crashed
    # sync no longer wedges this endpoint permanently.
    if is_sync_running():
        return JSONResponse(
            status_code=409, content={"message": "Sync is already in progress"}
        )
    if clear_stale_lock():
        pass  # a previous run died; the lock has been released
    set_sync_progress(True)
    background_tasks.add_task(bg_sync_task)
    return {"message": "Sync triggered in background"}


@app.get("/api/sync/status")
def get_sync_status():
    status = load_sync_status()
    status["sync_in_progress"] = is_sync_running()
    return status


@app.get("/api/sync/plan")
def get_sync_plan(source: str = "indeed"):
    """What the next run would scrape, for transparency before a long scrape."""
    db = get_db()
    try:
        config = load_config()
        roles = load_roles()
        cells = db.get_cells(source=source)
        scraper_config = with_location_weights(config.get("scraper", {}), roles)
        tasks = select_cells(cells, scraper_config, roles, source)
        return {
            "source": source,
            "cells_total": len(cells),
            "cells_planned": len(tasks),
            "tasks": [task.to_dict() for task in tasks],
        }
    finally:
        db.close()


# --- Digests ----------------------------------------------------------------------------

from careerradar.market.digest import DigestGenerator  # noqa: E402


@app.get("/api/digests")
def list_digests():
    return DigestGenerator().list_digests()


@app.get("/api/digests/{filename}")
def get_digest_content(filename: str):
    content = DigestGenerator().get_digest_content(filename)
    if not content:
        raise HTTPException(status_code=404, detail="Digest not found")
    return {"content": content}


# --- Static frontend --------------------------------------------------------------------

os.makedirs(os.path.join(FRONTEND_DIR, "css"), exist_ok=True)
os.makedirs(os.path.join(FRONTEND_DIR, "js"), exist_ok=True)

# `no-cache` means "store it, but revalidate before every use" -- not "do not store".
# Both responses already carry an ETag, so revalidation costs a 304 with an empty body on
# a loopback connection.
#
# Without it the assets under /static send no Cache-Control at all, which leaves a browser
# free to apply *heuristic* freshness and serve them without asking. The page and its
# modules then drift apart independently, and a cached module paired with freshly rendered
# markup is a real, observed breakage: the script reaches for an element the HTML does not
# contain and the exception aborts everything after it. Files that are only correct as a
# matched set must be revalidated as one.
#
# The page itself is rendered per request now rather than served from disk, so it carries
# the header for the same reason: it names the module URLs that must not be stale.
CACHE_HEADERS = {"Cache-Control": "no-cache"}


class RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that asks before reusing anything. See CACHE_HEADERS."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers.update(CACHE_HEADERS)
        return response


app.mount("/static", RevalidatedStaticFiles(directory=FRONTEND_DIR), name="static")


# --- Server-rendered dashboard -----------------------------------------------------------

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters["short_date"] = rendering.short_date
templates.env.filters["hostname"] = rendering.hostname


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    # These names are the query parameters of `Database.query_jobs`, deliberately. The
    # filter form posts them straight through, so a field whose name is wrong fails here
    # at validation instead of being silently dropped -- which is how the old dashboard
    # shipped a sort control that sent nothing and a resume filter that filtered nothing.
    status: str = Query("unread", pattern="^(unread|saved|applied|rejected)$"),
    access: str = Query("", pattern="^(commutable|remote|relocation)?$"),
    country: str = "",
    min_score: Optional[int] = Query(None, ge=0, le=100),
    max_tier: Optional[int] = Query(None, ge=1, le=10),
    verdict: str = Query("", pattern="^(strong|worth_applying|stretch|poor_fit|mismatch)?$"),
    eligibility: str = Query("", pattern="^(eligible|conditional|blocked)?$"),
    sort: str = Query("fit", pattern="^(fit|fit_score|match_score|date_found)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    # Which posting the drawer is showing, if any. The drawer is page state rather than
    # client state: it is in the URL, so it survives a reload and the back button closes
    # it. Opening one used to depend on the card list still being in a JS array.
    job: Optional[int] = None,
):
    query = rendering.FilterQuery({
        "status": status, "access": access, "country": country,
        "min_score": min_score, "max_tier": max_tier, "verdict": verdict,
        "eligibility": eligibility, "sort": sort, "limit": limit, "offset": offset,
    })

    db = get_db()
    try:
        page = db.query_jobs(**query.as_db_kwargs())
        stats = db.get_stats()

        # Fetched by id rather than searched for in the page above: the posting a link
        # points at need not be on the page the link was rendered from, and after a status
        # change it usually is not.
        drawer_job = None
        dossier = None
        if job is not None:
            match = db.query_jobs(job_id=job, status=None, limit=1)["jobs"]
            drawer_job = match[0] if match else None
            if drawer_job is not None:
                dossier = dossier_for(db, drawer_job.get("company"))

        return templates.TemplateResponse(
            request,
            "base.html",
            {
                "query": query,
                "jobs": page["jobs"],
                "total": page["total"],
                "has_more": page["has_more"],
                "stats": stats,
                "countries": rendering.country_choices(load_roles()),
                "today": datetime.now().strftime("%B %-d, %Y"),
                "job": drawer_job,
                "dossier": dossier,
                "requirement_rows": (
                    rendering.requirement_rows(drawer_job) if drawer_job else []
                ),
                "highlighted_description": (
                    rendering.highlight_terms(
                        drawer_job.get("description"),
                        drawer_job.get("matched_skills") or [],
                    ) if drawer_job else ""
                ),
            },
            headers=CACHE_HEADERS,
        )
    finally:
        db.close()


@app.post("/jobs/{job_id}/status")
def set_job_status_form(job_id: int, status: str = Form(...), next: str = "/"):
    """Status change from the drawer, as a plain form post.

    Redirects back to the URL the form came from so the drawer stays open on the posting
    you were reading. The JSON endpoint at PUT /api/jobs/{id}/status is unchanged and is
    what the CLI and any script should use; this exists so the drawer needs no JS.
    """
    if status not in ("unread", "saved", "applied", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status value")
    db = get_db()
    try:
        if not db.update_job_status(job_id, status):
            raise HTTPException(status_code=404, detail="Job not found")
    finally:
        db.close()
    # 303 so the browser follows with GET; a 307 would repeat the POST on reload.
    return RedirectResponse(next or "/", status_code=303)
