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
        live_hub.stop()


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


class TargetQueryCreate(BaseModel):
    role_key: str
    query: str
    enabled: bool = True


class TargetQueryUpdate(BaseModel):
    query: str | None = None
    role_key: str | None = None
    enabled: bool | None = None


class TargetToggle(BaseModel):
    enabled: bool


class TargetLocationPayload(BaseModel):
    id: str
    label: str
    search_label: str | None = None
    country: str = "US"
    indeed_country: str = "usa"
    is_remote: bool = False
    access: str = "relocation"
    weight: float = 1.0
    distance: int = 50
    enabled: bool = True


class TargetLocationUpdate(BaseModel):
    label: str | None = None
    search_label: str | None = None
    country: str | None = None
    indeed_country: str | None = None
    is_remote: bool | None = None
    access: str | None = None
    weight: float | None = None
    distance: int | None = None
    enabled: bool | None = None


class TargetRolePayload(BaseModel):
    key: str
    label: str
    aliases: list[str] | None = None
    resume: str | None = None
    enabled: bool = True


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


@app.get("/api/observability/prompt")
def get_observability_prompt():
    from careerradar.profile.render import render_profile
    from careerradar.profile.repository import load_active_row, load_profile
    from careerradar.scoring.prompts import build_rules, build_system, prompt_hash

    prof = load_profile()
    row = load_active_row()
    summary_text = (row.get("summary_text") if row else "") or render_profile(prof)
    system_prompt = build_system(summary_text)
    rules_text = build_rules()
    hash_val = prompt_hash(summary_text)

    return {
        "prompt_hash": hash_val,
        "rules": rules_text,
        "summary_text": summary_text,
        "system_prompt": system_prompt,
        "updated_at": row.get("updated_at") if row else None,
    }


# --- Market analytics -------------------------------------------------------------------


@app.get("/api/market/yield")
@app.get("/api/market/query-yield")
def market_query_yield(
    window_days: int | None = Query(None, ge=1, le=365),
    source: str | None = Query(None),
    location: str | None = Query(None),
    role_family: str | None = Query(None),
    min_postings: int = Query(0, ge=0),
):
    """Query yield analytics for (source, query, location) search tuples.

    Returns postings found and scoring agent strong fits for each search tuple,
    as well as rollups by query term, source, and location.
    """
    db = get_db()
    try:
        config, taxonomy, roles, profile = analytics_context(db)
        if location and location not in roles.locations:
            raise HTTPException(status_code=400, detail=f"Unknown location: {location}")
        return MarketAnalytics(db, config, roles, taxonomy, profile).query_yield(
            window_days=window_days,
            source=source,
            location_id=location,
            role_family=role_family,
            min_postings=min_postings,
        )
    finally:
        db.close()


@app.get("/api/market/locations")
def market_locations():
    roles = load_roles()
    return {
        "locations": [location.to_dict() for location in roles.locations.values()],
        "role_families": [
            {"key": f.key, "label": f.label, "active": f.active, "resume": f.resume}
            for f in roles.families.values()
        ],
    }


@app.get("/api/targets/capacity")
def get_target_capacity():
    from careerradar.search.capacity import calculate_capacity
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        config, _, _, _ = analytics_context(db)
        queries = target_repo.get_target_queries(db.conn, enabled_only=True)
        locs = target_repo.get_target_locations(db.conn, enabled_only=True)
        return calculate_capacity(len(queries), len(locs), config)
    finally:
        db.close()


def _sync_taxonomy_cells() -> None:
    from careerradar.taxonomy import roles as roles_mod

    roles_mod._CACHE.clear()
    from careerradar.search.seed import seed_cells

    try:
        seed_cells(prune=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cell re-seeding failed after taxonomy update: %s", e)


@app.get("/api/targets")
def list_targets():
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        roles = target_repo.get_target_roles(db.conn)
        queries = target_repo.get_target_queries(db.conn)
        locs = target_repo.get_target_locations(db.conn)
        return {
            "roles": roles,
            "queries": queries,
            "locations": locs,
        }
    finally:
        db.close()


@app.post("/api/targets/queries")
def create_target_query(payload: TargetQueryCreate):
    from careerradar.taxonomy import repository as target_repo

    term = payload.query.strip()
    if not term:
        raise HTTPException(status_code=400, detail="Query term cannot be empty")
    role_key = payload.role_key.strip().lower()
    if not role_key:
        raise HTTPException(status_code=400, detail="Role key cannot be empty")

    db = get_db()
    try:
        # Ensure role exists in target_roles if missing
        roles = {r["key"] for r in target_repo.get_target_roles(db.conn)}
        if role_key not in roles:
            target_repo.save_target_role(
                db.conn,
                key=role_key,
                label=role_key.replace("_", " ").title(),
                enabled=True,
            )

        query_id = target_repo.add_target_query(
            db.conn,
            role_key=role_key,
            query_term=term,
            enabled=payload.enabled,
        )
        _sync_taxonomy_cells()
        return {"success": True, "id": query_id}
    finally:
        db.close()


@app.put("/api/targets/queries/{query_id}")
def update_target_query_endpoint(query_id: int, payload: TargetQueryUpdate):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.update_target_query(
            db.conn,
            query_id=query_id,
            query_term=payload.query.strip() if payload.query is not None else None,
            role_key=payload.role_key.strip().lower() if payload.role_key is not None else None,
            enabled=payload.enabled,
        )
        _sync_taxonomy_cells()
        return {"success": True}
    finally:
        db.close()


@app.put("/api/targets/queries/{query_id}/toggle")
def toggle_target_query_endpoint(query_id: int, payload: TargetToggle):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.toggle_target_query(db.conn, query_id=query_id, enabled=payload.enabled)
        _sync_taxonomy_cells()
        return {"success": True, "enabled": payload.enabled}
    finally:
        db.close()


@app.delete("/api/targets/queries/{query_id}")
def delete_target_query_endpoint(query_id: int):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.delete_target_query(db.conn, query_id=query_id)
        _sync_taxonomy_cells()
        return {"success": True}
    finally:
        db.close()


@app.post("/api/targets/locations")
def create_target_location(payload: TargetLocationPayload):
    from careerradar.taxonomy import repository as target_repo

    loc_id = payload.id.strip().lower().replace(" ", "_")
    if not loc_id:
        raise HTTPException(status_code=400, detail="Location ID cannot be empty")
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Location label cannot be empty")
    search_label = payload.search_label.strip() if payload.search_label else label

    db = get_db()
    try:
        target_repo.save_target_location(
            db.conn,
            loc_id=loc_id,
            label=label,
            search_label=search_label,
            country=payload.country.strip().upper(),
            indeed_country=payload.indeed_country.strip().lower(),
            is_remote=payload.is_remote,
            access=payload.access,
            weight=payload.weight,
            distance=payload.distance,
            enabled=payload.enabled,
        )
        _sync_taxonomy_cells()
        return {"success": True, "id": loc_id}
    finally:
        db.close()


@app.put("/api/targets/locations/{loc_id}")
def update_target_location_endpoint(loc_id: str, payload: TargetLocationUpdate):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        existing = [loc for loc in target_repo.get_target_locations(db.conn) if loc["id"] == loc_id]
        if not existing:
            raise HTTPException(status_code=404, detail="Location not found")
        loc = existing[0]

        target_repo.save_target_location(
            db.conn,
            loc_id=loc_id,
            label=payload.label.strip() if payload.label is not None else loc["label"],
            search_label=(
                payload.search_label.strip()
                if payload.search_label is not None
                else loc.get("search_label", loc["label"])
            ),
            country=(
                payload.country.strip().upper() if payload.country is not None else loc["country"]
            ),
            indeed_country=(
                payload.indeed_country.strip().lower()
                if payload.indeed_country is not None
                else loc.get("indeed_country", "usa")
            ),
            is_remote=(
                payload.is_remote if payload.is_remote is not None else bool(loc["is_remote"])
            ),
            access=payload.access if payload.access is not None else loc["access"],
            weight=payload.weight if payload.weight is not None else loc["weight"],
            distance=payload.distance if payload.distance is not None else loc["distance"],
            enabled=payload.enabled if payload.enabled is not None else bool(loc["enabled"]),
        )
        _sync_taxonomy_cells()
        return {"success": True}
    finally:
        db.close()


@app.put("/api/targets/locations/{loc_id}/toggle")
def toggle_target_location_endpoint(loc_id: str, payload: TargetToggle):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.toggle_target_location(db.conn, loc_id=loc_id, enabled=payload.enabled)
        _sync_taxonomy_cells()
        return {"success": True, "enabled": payload.enabled}
    finally:
        db.close()


@app.delete("/api/targets/locations/{loc_id}")
def delete_target_location_endpoint(loc_id: str):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.delete_target_location(db.conn, loc_id=loc_id)
        _sync_taxonomy_cells()
        return {"success": True}
    finally:
        db.close()


@app.post("/api/targets/roles")
def create_target_role_endpoint(payload: TargetRolePayload):
    from careerradar.taxonomy import repository as target_repo

    role_key = payload.key.strip().lower().replace(" ", "_")
    if not role_key:
        raise HTTPException(status_code=400, detail="Role key cannot be empty")
    label = payload.label.strip() or role_key.replace("_", " ").title()

    db = get_db()
    try:
        target_repo.save_target_role(
            db.conn,
            key=role_key,
            label=label,
            aliases=payload.aliases,
            resume=payload.resume,
            enabled=payload.enabled,
        )
        _sync_taxonomy_cells()
        return {"success": True, "key": role_key}
    finally:
        db.close()


@app.put("/api/targets/roles/{role_key}/toggle")
def toggle_target_role_endpoint(role_key: str, payload: TargetToggle):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.toggle_target_role(db.conn, key=role_key, enabled=payload.enabled)
        _sync_taxonomy_cells()
        return {"success": True, "enabled": payload.enabled}
    finally:
        db.close()


@app.delete("/api/targets/roles/{role_key}")
def delete_target_role_endpoint(role_key: str):
    from careerradar.taxonomy import repository as target_repo

    db = get_db()
    try:
        target_repo.delete_target_role(db.conn, key=role_key)
        _sync_taxonomy_cells()
        return {"success": True}
    finally:
        db.close()


@app.post("/api/targets/sync-cells")
def sync_targets_cells_endpoint():
    from careerradar.search.capacity import calculate_capacity
    from careerradar.taxonomy import repository as target_repo

    _sync_taxonomy_cells()
    db = get_db()
    try:
        config, _, _, _ = analytics_context(db)
        queries = target_repo.get_target_queries(db.conn, enabled_only=True)
        locs = target_repo.get_target_locations(db.conn, enabled_only=True)
        capacity = calculate_capacity(len(queries), len(locs), config)
        return {
            "success": True,
            "capacity": capacity,
            "total_cells": len(db.get_cells()),
        }
    finally:
        db.close()


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
@app.get("/api/resume-builder/profile")
def get_profile_endpoint():
    from careerradar.profile.repository import load_profile

    return load_profile().model_dump()


@app.put("/api/profile")
@app.put("/api/resume-builder/profile")
def update_profile_endpoint(profile_data: dict[str, Any]):
    from careerradar.profile.models import Profile
    from careerradar.profile.repository import save_profile

    try:
        profile = Profile.model_validate(profile_data)
        save_profile(profile)
        return {"status": "ok", "profile": profile.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/profile/versions")
def get_profile_versions():
    from careerradar.profile.repository import list_versions

    return list_versions()


@app.get("/api/profile/vector")
def get_profile_vector_endpoint():
    from careerradar.profile.adapter import load_profile as load_adapter_profile
    from careerradar.profile.render import render_profile
    from careerradar.profile.repository import load_active_row, load_profile

    prof = load_profile()
    row = load_active_row()
    summary_text = (row.get("summary_text") if row else "") or render_profile(prof)
    adapter = load_adapter_profile(required=False)
    skills_vector = []
    if adapter:
        for key, rec in adapter.skills.items():
            skills_vector.append(
                {
                    "key": key,
                    "label": rec["label"],
                    "level": rec["level"],
                    "evidence": rec.get("evidence", []),
                }
            )
    return {
        "version": 1,
        "updated_at": row.get("updated_at") if row else None,
        "model": "deepseek-chat",
        "profile": prof.model_dump(),
        "skills_vector": skills_vector,
        "summary_text": summary_text,
    }


# --- Master Profile & Resume Builder Endpoints ------------------------------------------


class ProfileChatPayload(BaseModel):
    messages: list[dict[str, str]]
    current_profile: dict[str, Any]
    resume_text: str | None = None


@app.post("/api/profile/upload")
@app.post("/api/profile/upload-resume")
async def upload_resume(request: Request):
    from fastapi import UploadFile
    from starlette.datastructures import UploadFile as StarletteUploadFile

    from careerradar.profile.copilot import (
        extract_profile_from_resume_text,
        parse_resume_file,
    )
    from careerradar.profile.repository import load_profile

    try:
        form = await request.form()
        uploaded_file = form.get("file")
        if (
            not isinstance(uploaded_file, (UploadFile, StarletteUploadFile))
            or not uploaded_file.filename
        ):
            raise HTTPException(status_code=400, detail="Missing 'file' in upload form payload.")

        content = await uploaded_file.read()
        filename = uploaded_file.filename or "resume.pdf"
        text = parse_resume_file(content, filename)
        if not text.strip():
            raise HTTPException(status_code=400, detail="Uploaded file contained no readable text.")

        existing = load_profile()
        extracted = extract_profile_from_resume_text(text, existing_profile=existing)
        return {
            "status": "ok",
            "filename": filename,
            "text_snippet": text[:500],
            "raw_text": text,
            "profile": extracted.model_dump(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Failed to parse and extract uploaded resume")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/profile/chat")
def profile_copilot_chat(payload: ProfileChatPayload):
    from careerradar.profile.copilot import chat_with_copilot
    from careerradar.profile.models import Profile

    try:
        current_prof = Profile.model_validate(payload.current_profile)
        result = chat_with_copilot(
            messages=payload.messages,
            current_profile=current_prof,
            resume_text=payload.resume_text,
        )
        return {
            "status": "ok",
            "reply": result.reply,
            "updated_profile": result.updated_profile.model_dump(),
            "changes_made": result.changes_made,
        }
    except Exception as exc:
        logger.exception("Profile copilot chat turn failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/resume/generate")
def generate_job_resume(job_id: int):
    from careerradar.profile.builder import build_resume_for_job

    try:
        record = build_resume_for_job(job_id)
        return {"status": "ok", "record": record}
    except Exception as exc:
        logger.exception("Resume generation failed for job %d", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/resume")
def get_job_resume(job_id: int):
    from careerradar.profile.repository import get_latest_tailored_resume

    db = get_db()
    try:
        record = get_latest_tailored_resume(job_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="No resume generated for this job yet")
        return record
    finally:
        db.close()


@app.get("/api/resumes")
def get_generated_resumes(limit: int = 50, offset: int = 0, job_id: int | None = None):
    from careerradar.profile.repository import list_tailored_resumes

    db = get_db()
    try:
        _ = offset
        return list_tailored_resumes(limit=limit, job_id=job_id, conn=db.conn)
    finally:
        db.close()


@app.get("/api/resumes/{resume_id}/download")
def download_resume(resume_id: int, format: str = Query("docx", pattern="^(docx|pdf)$")):
    from careerradar.profile.repository import get_resume_by_id

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

    from careerradar.profile.repository import get_latest_tailored_resume

    return {
        "job": job,
        "dossier": dossier_for(db, job.get("company")),
        "resume": get_latest_tailored_resume(job["id"], db.conn),
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


@app.exception_handler(Exception)
async def global_exception_handler(req: Request, exc: Exception):
    logger.error("Unhandled exception: %s %s %s", req.method, req.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "Unhandled server exception"})
