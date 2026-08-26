import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.responses import Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.types import Scope

from careerradar.core.config import deep_merge, load_config, save_config
from careerradar.core.database import Database
from careerradar.core.logger import get_logger
from careerradar.core.paths import FRONTEND_DIR
from careerradar.core.status_manager import (
    is_sync_running,
    load_sync_status,
)
from careerradar.market.analytics import MarketAnalytics
from careerradar.market.gap_analysis import GapAnalysis
from careerradar.profile.adapter import load_profile
from careerradar.search.scheduler import scrape_tasks
from careerradar.taxonomy.roles import load_roles
from careerradar.taxonomy.skills import load_taxonomy
from careerradar.web import rendering
from careerradar.web.live import live_hub

if TYPE_CHECKING:
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Auto-seed cells on startup to sync taxonomy changes without manual commands
    from careerradar.search.seed import seed_cells

    try:
        seed_cells(prune=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto cell seeding failed on startup: %s", e)

    live_task = asyncio.create_task(live_hub.start_monitor())
    from careerradar.core.scheduler import scheduler

    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        live_task.cancel()
        with suppress(asyncio.CancelledError):
            await live_task


app = FastAPI(title="Job Search Automation Dashboard", lifespan=lifespan)

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
async def restrict_to_owner(
    request: Request, call_next: Callable[[Request], Awaitable[FastAPIResponse]]
) -> FastAPIResponse:
    login = request.headers.get(IDENTITY_HEADER)
    if login is None:
        return await call_next(request)
    if login == OWNER_LOGIN:
        return await call_next(request)
    logger.warning("Refused dashboard request from tailnet user %s", login)
    return JSONResponse({"detail": "Not authorised for this dashboard."}, status_code=403)


class _PooledDatabase(Database):
    """A `Database` that outlives the request that borrowed it.

    Opening a SQLite connection is not free: the first statement on a fresh handle pays
    for the WAL pragmas and the file open, ~0.32ms measured, which was around a tenth of
    what a drawer request costs now that the drawer request is 3ms. Reusing the handle
    removes that from every request.

    `close()` is deliberately inert. The endpoints keep their `try/finally: db.close()`,
    which still reads as "this request is done with the database" -- it just no longer
    means "tear down the connection", because the next request on this thread wants it.
    Actually closing it is `dispose()`.
    """

    def close(self) -> None:
        """No-op. The connection is reused by the next request on this thread."""

    def dispose(self) -> None:
        super().close()


# One connection per worker thread. FastAPI runs these sync endpoints in a threadpool and
# a SQLite connection must not be shared across threads, so thread-local is the unit --
# not a module global, and not a pool that would have to hand connections back.
#
# The long-running writers do not come through here: `run_sync` builds its own `Database`
# (`search/runner.py`), as do the scoring and research workers, so a 45-minute scrape
# cannot pin a connection the dashboard is trying to read on.
_thread_state = threading.local()


def get_db() -> "_PooledDatabase":
    db = getattr(_thread_state, "db", None)
    if db is None:
        db = _PooledDatabase()
        _thread_state.db = db
    return db


def analytics_context(
    db: Database,  # noqa: ARG001 - endpoint dependency signature
) -> tuple[dict[str, Any], "Taxonomy", "RoleTaxonomy", "ProfileAdapter"]:
    """Shared objects for the analytics endpoints."""
    config = load_config()
    taxonomy = load_taxonomy()
    roles = load_roles()
    profile = load_profile(taxonomy=taxonomy)
    # `required` defaults to True, so load_profile raises NoActiveProfile rather than
    # returning None -- this is here only to narrow the type for callers.
    assert profile is not None
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
    status: str | None = None,
    country: str | None = None,
    role_family: str | None = None,
    seniority: str | None = None,
    source: str | None = None,
    is_remote: bool | None = None,
    has_salary: bool | None = None,
    # commutable | remote | relocation.
    access: str | None = Query(None, pattern="^(commutable|remote|relocation)$"),
    include_duplicates: bool = False,
    min_score: int | None = None,
    fit: bool | None = None,
    reason_type: str | None = None,
    liveness: str | None = Query(None, pattern="^(live|stale|likely_closed|unknown)?$"),
    pipeline_state: str | None = Query(None, pattern="^(new|scored|researched)?$"),
    date_posted: str | None = Query(None, pattern="^(24h|3d|7d|14d|30d)?$"),
    q: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = get_db()
    try:
        return db.query_jobs(
            status=status,
            country=country,
            role_family=role_family,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
            access=access,
            include_duplicates=include_duplicates,
            min_score=min_score,
            fit=fit,
            reason_type=reason_type,
            liveness=liveness,
            pipeline_state=pipeline_state,
            date_posted=date_posted,
            q=q,
            limit=limit,
            offset=offset,
            detail=True,
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
        return JSONResponse(content=db.get_stats(), headers=CACHE_HEADERS)
    finally:
        db.close()


# --- Model observability ----------------------------------------------------------------


@app.get("/api/observability/stats")
def get_observability_stats():
    db = get_db()
    try:
        return db.get_observability_stats()
    finally:
        db.close()


@app.get("/api/observability/verdicts")
def get_observability_verdicts(
    q: str | None = None,
    fit: str | None = None,
    reason_type: str | None = None,
    model: str | None = None,
    sort: str = Query(
        "tokens_out_desc",
        pattern="^(tokens_out_desc|tokens_out_asc|cost_desc|date_desc|date_asc)$",
    ),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    db = get_db()
    try:
        return db.query_observability_verdicts(
            q=q,
            fit=fit,
            reason_type=reason_type,
            model=model,
            sort=sort,
            limit=limit,
            offset=offset,
        )
    finally:
        db.close()


# --- Market analytics -------------------------------------------------------------------


@app.get("/api/market/supply")
def market_supply(
    window_days: int = Query(14, ge=1, le=365),
    location: str = Query(
        ..., description="Required: flow is only comparable within one location and source"
    ),
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
        return {"cells": MarketAnalytics(db, config, roles, taxonomy, profile).coverage_report()}
    finally:
        db.close()


# --- Skills ----------------------------------------------------------------------------


@app.get("/api/skills/gap")
def skills_gap(
    window_days: int = Query(90, ge=1, le=365),
    location: str | None = None,
    role_family: str | None = None,
    weighting: str | None = Query(None, pattern="^(interest|estimated_supply|observed)$"),
):
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        return GapAnalysis(db, config, roles, taxonomy, profile).analyse(
            window_days=window_days,
            location_id=location,
            role_family=role_family,
            weighting_mode=weighting,
        )
    finally:
        db.close()


@app.get("/api/skills/{skill}")
def skill_detail(
    skill: str, window_days: int = Query(90, ge=1, le=365), limit: int = Query(40, ge=1, le=200)
):
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
    from careerradar.profile.repository import load_active_row

    record = load_active_row()
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No active profile. Build one with: careerradar profile build",
        )
    return record


@app.get("/api/profile/versions")
def get_profile_versions():
    from careerradar.profile.repository import list_versions

    return list_versions()


# --- Resume Builder & Generator Endpoints -----------------------------------------------


@app.get("/api/resume-builder/profile")
def get_resume_builder_profile():
    from careerradar.resumes.repository import load_master_profile

    return load_master_profile().model_dump()


@app.put("/api/resume-builder/profile")
def update_resume_builder_profile(profile_data: dict[str, Any]):
    from careerradar.resumes.models import ResumeMasterProfile
    from careerradar.resumes.repository import save_master_profile

    try:
        profile = ResumeMasterProfile.model_validate(profile_data)
        save_master_profile(profile)
        return {"status": "ok", "profile": profile.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/resume/generate")
def generate_job_resume(job_id: int):
    from careerradar.resumes.builder import build_resume_for_job

    try:
        record = build_resume_for_job(job_id)
        return {"status": "ok", "record": record}
    except Exception as exc:
        logger.exception("Resume generation failed for job %d", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/resume")
def get_job_resume(job_id: int):
    from careerradar.resumes.repository import get_latest_resume_for_job

    db = get_db()
    try:
        record = get_latest_resume_for_job(job_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="No resume generated for this job yet")
        return record
    finally:
        db.close()


@app.get("/api/resumes")
def get_generated_resumes(limit: int = 50, offset: int = 0):
    from careerradar.resumes.repository import list_generated_resumes

    db = get_db()
    try:
        return list_generated_resumes(limit=limit, offset=offset, conn=db.conn)
    finally:
        db.close()


@app.get("/api/resumes/{resume_id}/download")
def download_resume(resume_id: int, format: str = Query("docx", pattern="^(docx|pdf)$")):
    from careerradar.resumes.repository import get_resume_by_id

    db = get_db()
    try:
        record = get_resume_by_id(resume_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="Resume record not found")

        file_path = record.get("pdf_path") if format == "pdf" else record.get("docx_path")
        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"{format.upper()} file not found on disk")

        media_type = (
            "application/pdf"
            if format == "pdf"
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        filename = os.path.basename(file_path)
        return FileResponse(file_path, media_type=media_type, filename=filename)
    finally:
        db.close()


# --- Company dossiers ------------------------------------------------------------------


def dossier_for(db: Database, company: str | None) -> dict[str, Any] | None:
    """The deep-research dossier for one company, or None.

    Factored out so the drawer renders the dossier from the same read the JSON endpoint
    serves. The drawer used to fetch this itself and swallow every failure in a bare
    `catch`, which meant "no dossier" and "the request broke" looked identical.
    """
    if not company:
        return None

    from careerradar.research import repository as research_repo

    return research_repo.get_dossier_by_company(db.conn, company)


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


# --- Sync ------------------------------------------------------------------------------


@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    from careerradar.core.scheduler import scheduler

    status = scheduler.get_status()
    if status.get("active_stage") == "search" or is_sync_running():
        return JSONResponse(status_code=409, content={"message": "Sync is already in progress"})
    background_tasks.add_task(scheduler.trigger, "search", manual=True)
    return {"message": "Sync triggered in background via scheduler"}


@app.get("/api/sync/status")
def get_sync_status():
    status = load_sync_status()
    status["sync_in_progress"] = is_sync_running()
    return status


@app.get("/api/scheduler/status")
def get_scheduler_status():
    from careerradar.core.scheduler import scheduler

    return scheduler.get_status()


@app.post("/api/scheduler/trigger/{stage}")
async def trigger_scheduler_stage(stage: str, background_tasks: BackgroundTasks):
    from careerradar.core.scheduler import scheduler

    if stage not in ["search", "score", "research"]:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {stage}")
    status = scheduler.get_status()
    if status.get("active_stage") == stage:
        return JSONResponse(
            status_code=409, content={"message": f"Stage '{stage}' is already in progress"}
        )
    background_tasks.add_task(scheduler.trigger, stage, manual=True)
    return {"message": f"Stage '{stage}' triggered in background"}


@app.get("/api/sync/plan")
def get_sync_plan(source: str = "indeed"):
    """What the next run would scrape, for transparency before a long scrape."""
    db = get_db()
    try:
        config = load_config()
        roles = load_roles()
        tasks = scrape_tasks(db, config, roles, source)
        return {
            "source": source,
            "cells_total": len(db.get_cells(source=source)),
            "cells_planned": len(tasks),
            "tasks": [task.to_dict() for task in tasks],
        }
    finally:
        db.close()


@app.get("/api/pipeline/status")
def pipeline_status():
    """Live progress for the two background stages: scraping and scoring.

    `careerradar status` (careerradar/core/status.py) already answers "is each stage
    stalled" from history alone -- last completed run, verdict backlog, verdict recency.
    This reuses that report and adds the two things it cannot show: how far the scrape
    *in progress right now* has gotten, and whether the scorer -- which has no lock file,
    only a timer -- has written anything in the last few minutes.
    """
    from careerradar.core import status_repository as status_repo

    db = get_db()
    try:
        return status_repo.get_pipeline_status(
            db.conn,
            sync_running=is_sync_running(),
            db_instance=db,
        )
    finally:
        db.close()


# --- Static frontend --------------------------------------------------------------------
#
# Vite's production build gives every asset a content hash in its filename, so a changed
# file is a changed URL and caching the response body forever is always safe -- no mtime
# stamping or revalidation trick needed, unlike the old hand-rolled scheme this replaced.
CACHE_HEADERS = {"Cache-Control": "no-cache"}
IMMUTABLE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


class ImmutableStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> FastAPIResponse:
        response = await super().get_response(path, scope)
        response.headers.update(IMMUTABLE_CACHE_HEADERS)
        return response


_STATIC_DIST = os.path.join(FRONTEND_DIR, "dist")
os.makedirs(_STATIC_DIST, exist_ok=True)

app.mount(
    "/static/dist",
    ImmutableStaticFiles(directory=_STATIC_DIST, check_dir=False),
    name="static",
)


def drawer_context(
    db: Database, query: rendering.FilterQuery, job_id: int | None
) -> dict[str, Any]:
    """Everything `GET /api/jobs/{id}/context` returns, for one posting or for none.

    `next_job_id` is the posting after this one *in the current feed order*, which is why
    the filter has to come in with the job id -- "next" is a property of the list you are
    reading, not of the posting. It is what the drawer's status buttons advance to.
    """
    if job_id is None:
        return {"job": None, "dossier": None, "requirement_rows": [], "next_job_id": None}

    # Fetched by id rather than searched for in the page above: the posting a link
    # points at need not be on the page the link was rendered from, and after a status
    # change it usually is not.
    match = db.query_jobs(job_id=job_id, status=None, limit=1)["jobs"]
    job = match[0] if match else None
    if job is None:
        return {"job": None, "dossier": None, "requirement_rows": [], "next_job_id": None}

    ids = db.job_ids_for(**query.as_db_kwargs())
    try:
        position = ids.index(job_id)
    except ValueError:
        # The posting is not on this page of this filter -- a deep link, or a status change
        # that just moved it out of the feed. There is no "next" to offer.
        next_job_id = None
    else:
        next_job_id = ids[position + 1] if position + 1 < len(ids) else None

    from careerradar.resumes.repository import get_latest_resume_for_job

    return {
        "job": job,
        "dossier": dossier_for(db, job.get("company")),
        "resume": get_latest_resume_for_job(job["id"], db.conn),
        "requirement_rows": [],
        "next_job_id": next_job_id,
    }


# The filter, as FastAPI parameters. Declared once and depended on by `/api/jobs/{id}/context`
# so a posting's "next" is always computed against the exact same filter the SPA is showing.
def filter_query(
    # These names are the query parameters of `Database.query_jobs`, deliberately. The
    # filter form posts them straight through, so a field whose name is wrong fails here
    # at validation instead of being silently dropped -- which is how the old dashboard
    # shipped a sort control that sent nothing and a resume filter that filtered nothing.
    status: str = Query("unread", pattern="^(unread|saved|applied|rejected)$"),
    access: str = Query("", pattern="^(commutable|remote|relocation)?$"),
    country: str = "",
    fit: bool | None = None,
    reason_type: str = Query("", pattern="^[a-z_]*$"),
    liveness: str = Query("", pattern="^(live|stale|likely_closed|unknown)?$"),
    date_posted: str = Query("", pattern="^(24h|3d|7d|14d|30d)?$"),
    q: str = "",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> rendering.FilterQuery:
    return rendering.FilterQuery(
        {
            "status": status,
            "access": access,
            "country": country,
            "fit": fit,
            "reason_type": reason_type if reason_type else None,
            "liveness": liveness,
            "date_posted": date_posted,
            "q": q,
            "limit": limit,
            "offset": offset,
        }
    )


# --- SPA JSON endpoints ------------------------------------------------------------------


@app.get("/api/meta")
def get_meta():
    return {
        "countries": rendering.country_choices(load_roles()),
        "reason_types": rendering.REASON_TYPE_CHOICES,
    }


@app.get("/api/jobs/{job_id}/context")
def get_job_context(job_id: int, query: rendering.FilterQuery = Depends(filter_query)):
    db = get_db()
    try:
        return drawer_context(db, query, job_id)
    finally:
        db.close()


# --- Real-Time Live Feed Stream ---------------------------------------------------------
@app.get("/api/live/events")
async def live_events(request: Request) -> StreamingResponse:
    """Server-Sent Events (SSE) stream for real-time dashboard updates.

    ARCHITECTURE & HEADERS HINT:
    - `media_type="text/event-stream"` tells the browser this is an EventSource stream.
    - `Cache-Control: no-cache, no-transform` prevents proxies and browser from caching.
    - `X-Accel-Buffering: no` instructs Nginx/reverse-proxies not to buffer the stream.
    - `request.is_disconnected()` allows terminating the generator on tab close.

    TODO for implementation:
    1. Import `StreamingResponse` from `fastapi.responses`.
    2. Import `live_hub` from `careerradar.web.live`.
    3. Define an async generator over `live_hub.subscribe()` checking `is_disconnected()`.
    4. Return `StreamingResponse(event_generator(), media_type="text/event-stream", headers=...)`.
    """

    async def event_generator():
        async for message in live_hub.subscribe():
            if await request.is_disconnected():
                break
            yield message

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Preact SPA shell -------------------------------------------------------------------
#
# `/api/...` and `/static/...` are excluded rather than left to fall through on no other
# route matching, because a typo'd or removed API path should 404 as itself, not silently
# return the SPA shell -- that failure mode reads as "this loaded", not "this is missing".
_SPA_INDEX = os.path.join(FRONTEND_DIR, "dist", "index.html")


@app.get("/{path:path}", response_class=HTMLResponse)
def spa_shell(path: str):
    if path.startswith(("api/", "static/")):
        raise HTTPException(status_code=404, detail="Not Found")
    if not os.path.exists(_SPA_INDEX):
        raise HTTPException(
            status_code=503, detail="Frontend not built yet -- run `npm run build`."
        )
    return FileResponse(_SPA_INDEX, headers=CACHE_HEADERS)
