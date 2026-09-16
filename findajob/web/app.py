import asyncio
import base64
import os
import secrets
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

from findajob.core.config import load_config, save_config
from findajob.core.database import Database
from findajob.core.logger import get_logger
from findajob.core.paths import FRONTEND_DIR
from findajob.core.scheduler_preferences import (
    get_scheduler_preferences,
    update_scheduler_preferences,
)
from findajob.core.status_manager import (
    is_sync_running,
    load_sync_status,
)
from findajob.market.analytics import MarketAnalytics
from findajob.market.gap_analysis import GapAnalysis
from findajob.profile.adapter import load_profile
from findajob.search.scheduler import scrape_tasks
from findajob.search.targets import load_targets
from findajob.web import rendering
from findajob.web.live import live_hub

if TYPE_CHECKING:
    from findajob.profile.adapter import ProfileAdapter
    from findajob.search.targets import SearchTargets


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Auto-seed cells on startup to sync target changes without manual commands
    from findajob.search.seed import seed_cells

    try:
        seed_cells(prune=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Auto cell seeding failed on startup: %s", e)

    live_task = asyncio.create_task(live_hub.start_monitor())
    from findajob.core.scheduler import scheduler

    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()
        live_task.cancel()
        with suppress(asyncio.CancelledError):
            await live_task
        live_hub.stop()


app = FastAPI(title="Find a Job", lifespan=lifespan)

logger = get_logger()

IDENTITY_HEADER = "tailscale-user-login"


def _is_request_authenticated(
    request: Request, expected_password: str, expected_token: str, expected_user: str
) -> bool:
    """Validate Bearer tokens, API keys, or HTTP Basic Auth credentials."""
    # Check X-API-Key header
    api_key = request.headers.get("x-api-key", "").strip()
    if api_key:
        if expected_token and secrets.compare_digest(api_key, expected_token):
            return True
        if expected_password and secrets.compare_digest(api_key, expected_password):
            return True

    # Check Authorization header (Basic or Bearer)
    auth_header = request.headers.get("authorization", "").strip()
    if auth_header.startswith("Bearer "):
        bearer = auth_header[7:].strip()
        if expected_token and secrets.compare_digest(bearer, expected_token):
            return True
        if expected_password and secrets.compare_digest(bearer, expected_password):
            return True

    if auth_header.startswith("Basic "):
        try:
            encoded_creds = auth_header[6:].strip()
            decoded = base64.b64decode(encoded_creds).decode("utf-8")
            if ":" in decoded:
                user, pwd = decoded.split(":", 1)
                user_ok = (not expected_user) or secrets.compare_digest(user, expected_user)
                pwd_ok = bool(expected_password) and secrets.compare_digest(pwd, expected_password)
                token_ok = bool(expected_token) and secrets.compare_digest(pwd, expected_token)
                if user_ok and (pwd_ok or token_ok):
                    return True
        except (ValueError, UnicodeDecodeError):
            return False

    return False


@app.middleware("http")
async def auth_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[FastAPIResponse]]
) -> FastAPIResponse:
    owner_login = os.environ.get("FIND_A_JOB_OWNER", "").strip()
    auth_password = os.environ.get("FIND_A_JOB_PASSWORD", "").strip()
    auth_token = os.environ.get("FIND_A_JOB_AUTH_TOKEN", "").strip()
    auth_user = os.environ.get("FIND_A_JOB_USER", "").strip()

    # 1. Tailscale Serve identity header check (if request arrived through Tailscale)
    login = request.headers.get(IDENTITY_HEADER)
    if login is not None:
        if owner_login and login == owner_login:
            return await call_next(request)
        logger.warning("Refused dashboard request from tailnet user %s", login)
        return JSONResponse({"detail": "Not authorised for this dashboard."}, status_code=403)

    # 2. Opt-in Basic Auth / Token gate (active only if password or token is configured)
    if (auth_password or auth_token) and not _is_request_authenticated(
        request, auth_password, auth_token, auth_user
    ):
        return JSONResponse(
            {"detail": "Authentication required."},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Find a Job"'},
        )

    return await call_next(request)


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
# (`search/runner.py`), as does the scoring worker, so a 45-minute scrape
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
) -> tuple[dict[str, Any], "SearchTargets", "ProfileAdapter"]:
    """Shared objects for the analytics endpoints."""
    config = load_config()
    targets = load_targets()
    profile = load_profile()
    # `required` defaults to True, so load_profile raises NoActiveProfile rather than
    # returning None -- this is here only to narrow the type for callers.
    assert profile is not None
    return config, targets, profile


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


class SchedulerPreferencesUpdate(BaseModel):
    enabled: bool


class TargetQueryCreate(BaseModel):
    query: str
    enabled: bool = True


class TargetQueryUpdate(BaseModel):
    query: str | None = None
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
    distance: int = 50
    enabled: bool = True


class TargetLocationUpdate(BaseModel):
    label: str | None = None
    search_label: str | None = None
    country: str | None = None
    indeed_country: str | None = None
    is_remote: bool | None = None
    distance: int | None = None
    enabled: bool | None = None


# --- Jobs ------------------------------------------------------------------------------


@app.get("/api/jobs")
def get_jobs(
    status: str | None = None,
    country: str | None = None,
    location: str | None = None,
    seniority: str | None = None,
    source: str | None = None,
    is_remote: bool | None = None,
    has_salary: bool | None = None,
    include_duplicates: bool = False,
    min_score: int | None = None,
    fit: bool | None = None,
    reason_type: str | None = None,
    liveness: str | None = Query(None, pattern="^(live|stale|likely_closed|unknown)?$"),
    pipeline_state: str | None = Query(None, pattern="^(new|scored)?$"),
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
            location=location,
            seniority=seniority,
            source=source,
            is_remote=is_remote,
            has_salary=has_salary,
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
    from findajob.profile.render import render_profile
    from findajob.profile.repository import load_active_row, load_profile
    from findajob.scoring.prompts import build_rules, build_system, prompt_hash

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
    query: str | None = Query(None),
    min_postings: int = Query(0, ge=0),
):
    """Query yield analytics for (source, query, location) search tuples.

    Returns postings found and scoring agent strong fits for each search tuple,
    as well as rollups by query term, source, and location.
    """
    db = get_db()
    try:
        config, targets, profile = analytics_context(db)
        if location and location not in targets.locations:
            raise HTTPException(status_code=400, detail=f"Unknown location: {location}")
        return MarketAnalytics(db, config, targets, profile).query_yield(
            window_days=window_days,
            source=source,
            location_id=location,
            query=query,
            min_postings=min_postings,
        )
    finally:
        db.close()


@app.get("/api/market/locations")
def market_locations():
    targets = load_targets()
    return {
        "locations": [location.to_dict() for location in targets.locations.values()],
        "queries": sorted(targets.queries),
    }


@app.get("/api/targets/capacity")
def get_target_capacity():
    from findajob.search import targets as target_repo
    from findajob.search.capacity import calculate_capacity

    db = get_db()
    try:
        config, _, _ = analytics_context(db)
        queries = target_repo.get_queries(db.conn, enabled_only=True)
        locs = target_repo.get_locations(db.conn, enabled_only=True)
        return calculate_capacity(len(queries), len(locs), config)
    finally:
        db.close()


def _sync_target_cells() -> None:
    from findajob.search.seed import seed_cells

    try:
        seed_cells(prune=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cell re-seeding failed after target update: %s", e)


@app.get("/api/targets")
def list_targets():
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        return {
            "queries": target_repo.get_queries(db.conn),
            "locations": target_repo.get_locations(db.conn),
        }
    finally:
        db.close()


@app.post("/api/targets/queries")
def create_target_query(payload: TargetQueryCreate):
    from findajob.search import targets as target_repo

    term = payload.query.strip()
    if not term:
        raise HTTPException(status_code=400, detail="Query term cannot be empty")

    db = get_db()
    try:
        query_id = target_repo.add_query(db.conn, query_term=term, enabled=payload.enabled)
        _sync_target_cells()
        return {"success": True, "id": query_id}
    finally:
        db.close()


@app.put("/api/targets/queries/{query_id}")
def update_target_query_endpoint(query_id: int, payload: TargetQueryUpdate):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        target_repo.update_query(
            db.conn,
            query_id=query_id,
            query_term=payload.query.strip() if payload.query is not None else None,
            enabled=payload.enabled,
        )
        _sync_target_cells()
        return {"success": True}
    finally:
        db.close()


@app.put("/api/targets/queries/{query_id}/toggle")
def toggle_target_query_endpoint(query_id: int, payload: TargetToggle):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        target_repo.toggle_query(db.conn, query_id=query_id, enabled=payload.enabled)
        _sync_target_cells()
        return {"success": True, "enabled": payload.enabled}
    finally:
        db.close()


@app.delete("/api/targets/queries/{query_id}")
def delete_target_query_endpoint(query_id: int):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        target_repo.delete_query(db.conn, query_id=query_id)
        _sync_target_cells()
        return {"success": True}
    finally:
        db.close()


@app.post("/api/targets/locations")
def create_target_location(payload: TargetLocationPayload):
    from findajob.search import targets as target_repo

    loc_id = payload.id.strip().lower().replace(" ", "_")
    if not loc_id:
        raise HTTPException(status_code=400, detail="Location ID cannot be empty")
    label = payload.label.strip()
    if not label:
        raise HTTPException(status_code=400, detail="Location label cannot be empty")
    search_label = payload.search_label.strip() if payload.search_label else label

    db = get_db()
    try:
        target_repo.save_location(
            db.conn,
            loc_id=loc_id,
            label=label,
            search_label=search_label,
            country=payload.country.strip().upper(),
            indeed_country=payload.indeed_country.strip().lower(),
            is_remote=payload.is_remote,
            distance=payload.distance,
            enabled=payload.enabled,
        )
        _sync_target_cells()
        return {"success": True, "id": loc_id}
    finally:
        db.close()


@app.put("/api/targets/locations/{loc_id}")
def update_target_location_endpoint(loc_id: str, payload: TargetLocationUpdate):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        existing = [loc for loc in target_repo.get_locations(db.conn) if loc["id"] == loc_id]
        if not existing:
            raise HTTPException(status_code=404, detail="Location not found")
        loc = existing[0]

        target_repo.save_location(
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
            distance=payload.distance if payload.distance is not None else loc["distance"],
            enabled=payload.enabled if payload.enabled is not None else bool(loc["enabled"]),
        )
        _sync_target_cells()
        return {"success": True}
    finally:
        db.close()


@app.put("/api/targets/locations/{loc_id}/toggle")
def toggle_target_location_endpoint(loc_id: str, payload: TargetToggle):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        target_repo.toggle_location(db.conn, loc_id=loc_id, enabled=payload.enabled)
        _sync_target_cells()
        return {"success": True, "enabled": payload.enabled}
    finally:
        db.close()


@app.delete("/api/targets/locations/{loc_id}")
def delete_target_location_endpoint(loc_id: str):
    from findajob.search import targets as target_repo

    db = get_db()
    try:
        target_repo.delete_location(db.conn, loc_id=loc_id)
        _sync_target_cells()
        return {"success": True}
    finally:
        db.close()


@app.post("/api/targets/sync-cells")
def sync_targets_cells_endpoint():
    from findajob.search import targets as target_repo
    from findajob.search.capacity import calculate_capacity

    _sync_target_cells()
    db = get_db()
    try:
        config, _, _ = analytics_context(db)
        queries = target_repo.get_queries(db.conn, enabled_only=True)
        locs = target_repo.get_locations(db.conn, enabled_only=True)
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
        config, targets, profile = analytics_context(db)
        return {"cells": MarketAnalytics(db, config, targets, profile).coverage_report()}
    finally:
        db.close()


# --- Skills ----------------------------------------------------------------------------


@app.get("/api/skills/gap")
def skills_gap(
    window_days: int = Query(90, ge=1, le=365),
    location: str | None = None,
    query: str | None = None,
):
    db = get_db()
    try:
        config, targets, profile = analytics_context(db)
        return GapAnalysis(db, config, targets, profile).analyse(
            window_days=window_days,
            location_id=location,
            query=query,
        )
    finally:
        db.close()


@app.get("/api/skills/{skill}")
def skill_detail(
    skill: str, window_days: int = Query(90, ge=1, le=365), limit: int = Query(40, ge=1, le=200)
):
    from findajob.market import repository as market_repo

    db = get_db()
    try:
        config, targets, profile = analytics_context(db)
        is_known = profile.has(skill) or market_repo.skill_exists(db.conn, skill)
        if not is_known:
            raise HTTPException(status_code=404, detail=f"Unknown skill: {skill}")
        return GapAnalysis(db, config, targets, profile).skill_detail(
            skill, window_days=window_days, limit=limit
        )
    finally:
        db.close()


@app.get("/api/profile")
@app.get("/api/resume-builder/profile")
def get_profile_endpoint():
    from findajob.profile.repository import load_profile

    return load_profile().model_dump()


@app.put("/api/profile")
@app.put("/api/resume-builder/profile")
def update_profile_endpoint(profile_data: dict[str, Any]):
    from findajob.profile.models import Profile
    from findajob.profile.repository import save_profile

    try:
        profile = Profile.model_validate(profile_data)
        save_profile(profile)
        return {"status": "ok", "profile": profile.model_dump()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/profile/versions")
def get_profile_versions():
    from findajob.profile.repository import list_versions

    return list_versions()


@app.get("/api/profile/vector")
def get_profile_vector_endpoint():
    from findajob.profile.adapter import load_profile as load_adapter_profile
    from findajob.profile.models import DEFAULT_PROFILE_VERSION
    from findajob.profile.render import render_profile
    from findajob.profile.repository import load_active_row, load_profile

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
    active_model = "deepseek-chat"
    try:
        from findajob.core.llm import get_llm_provider

        active_model = get_llm_provider().name
    except Exception:  # noqa: BLE001
        pass

    return {
        "version": DEFAULT_PROFILE_VERSION,
        "updated_at": row.get("updated_at") if row else None,
        "model": active_model,
        "profile": prof.model_dump(),
        "skills_vector": skills_vector,
        "summary_text": summary_text,
    }


@app.get("/api/llm/status")
def get_llm_status():
    from findajob.core.llm import get_llm_provider, list_available_providers

    providers = list_available_providers()
    try:
        active = get_llm_provider()
        active_name = active.name
    except Exception as exc:  # noqa: BLE001
        active_name = f"error: {exc}"
    return {
        "active_provider": active_name,
        "providers": providers,
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

    from findajob.profile.copilot import (
        extract_profile_from_resume_text,
        parse_resume_file,
    )
    from findajob.profile.repository import load_profile

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
    from findajob.profile.copilot import chat_with_copilot
    from findajob.profile.models import Profile

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


class ImportJobRequest(BaseModel):
    url: str
    score: bool = True
    generate_resume: bool = False
    model: str | None = None


@app.post("/api/jobs/import")
def api_import_job(req: ImportJobRequest):
    from findajob.search.importer import import_and_process_job

    if not req.url or not req.url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="A valid http or https URL is required.")

    db = get_db()
    try:
        res = import_and_process_job(
            url=req.url.strip(),
            score=req.score,
            generate_resume=req.generate_resume,
            model=req.model,
            db=db,
        )
        return {"status": "ok", **res}
    except ValueError as exc:
        logger.warning("Failed to import job from URL %s: %s", req.url, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to import job from URL %s", req.url)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        db.close()


@app.post("/api/jobs/{job_id}/resume/generate")
def generate_job_resume(job_id: int):
    from findajob.profile.builder import build_resume_for_job

    try:
        record = build_resume_for_job(job_id)
        return {"status": "ok", "record": record}
    except Exception as exc:
        logger.exception("Resume generation failed for job %d", job_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}/resume")
def get_job_resume(job_id: int):
    from findajob.profile.repository import get_latest_tailored_resume

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
    from findajob.profile.repository import list_tailored_resumes

    db = get_db()
    try:
        _ = offset
        return list_tailored_resumes(limit=limit, job_id=job_id, conn=db.conn)
    finally:
        db.close()


class ResumeSourceUpdate(BaseModel):
    typst_source: str


@app.get("/api/resumes/{resume_id}/download")
def download_resume(resume_id: int, format: str = Query("pdf", pattern="^(pdf|typst)$")):
    from pathlib import Path

    from findajob.core.paths import generated_resumes_dir
    from findajob.profile.models import TailoredResumePayload
    from findajob.profile.renderer import compile_typst_to_pdf, render_typst
    from findajob.profile.repository import get_resume_by_id

    db = get_db()
    try:
        record = get_resume_by_id(resume_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="Resume record not found")

        raw_source_path = record.get("typst_path")
        stem = Path(raw_source_path or f"resume_{resume_id}").stem

        if format == "pdf":
            file_path = record.get("pdf_path")
            if (not file_path or not os.path.exists(file_path)) and record.get("typst_path"):
                file_path = compile_typst_to_pdf(record["typst_path"], generated_resumes_dir())
            media_type = "application/pdf"
        else:
            file_path = record.get("typst_path")
            if (not file_path or not os.path.exists(file_path)) and record.get("resume"):
                payload = TailoredResumePayload.model_validate(record["resume"])
                file_path = os.path.join(generated_resumes_dir(), f"{stem}.typ")
                render_typst(payload, file_path)
            media_type = "text/plain; charset=utf-8"

        if not file_path or not os.path.exists(file_path):
            raise HTTPException(status_code=404, detail=f"{format.upper()} file not found on disk")

        filename = os.path.basename(file_path)
        return FileResponse(file_path, media_type=media_type, filename=filename)
    finally:
        db.close()


@app.get("/api/resumes/{resume_id}/source")
def get_resume_source(resume_id: int):
    from pathlib import Path

    from findajob.core.paths import generated_resumes_dir
    from findajob.profile.models import TailoredResumePayload
    from findajob.profile.renderer import generate_typst_source, render_typst, verify_page_count
    from findajob.profile.repository import get_resume_by_id

    db = get_db()
    try:
        record = get_resume_by_id(resume_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="Resume record not found")

        typst_path = record.get("typst_path")
        if typst_path and os.path.exists(typst_path):
            source = Path(typst_path).read_text(encoding="utf-8")
        elif record.get("resume"):
            payload = TailoredResumePayload.model_validate(record["resume"])
            source = generate_typst_source(payload)
            stem = f"resume_{resume_id}"
            typst_path = os.path.join(generated_resumes_dir(), f"{stem}.typ")
            render_typst(payload, typst_path)
        else:
            raise HTTPException(status_code=404, detail="No source data found for resume")

        pdf_path = record.get("pdf_path")
        pages = verify_page_count(pdf_path) if (pdf_path and os.path.exists(pdf_path)) else 1
        return {
            "resume_id": resume_id,
            "typst_source": source,
            "typst_path": typst_path,
            "page_count": pages,
        }
    finally:
        db.close()


@app.put("/api/resumes/{resume_id}/source")
def update_resume_source(resume_id: int, req: ResumeSourceUpdate):
    from pathlib import Path

    from findajob.core.paths import generated_resumes_dir
    from findajob.profile.renderer import compile_typst_to_pdf, verify_page_count
    from findajob.profile.repository import get_resume_by_id, update_resume_artifacts

    db = get_db()
    try:
        record = get_resume_by_id(resume_id, db.conn)
        if not record:
            raise HTTPException(status_code=404, detail="Resume record not found")

        typst_path = record.get("typst_path")
        if not typst_path:
            stem = f"resume_{resume_id}"
            typst_path = os.path.join(generated_resumes_dir(), f"{stem}.typ")

        Path(typst_path).parent.mkdir(parents=True, exist_ok=True)
        Path(typst_path).write_text(req.typst_source, encoding="utf-8")

        pdf_path = compile_typst_to_pdf(typst_path)
        pages = verify_page_count(pdf_path) if pdf_path else 0

        update_resume_artifacts(resume_id, typst_path, pdf_path, conn=db.conn)

        return {
            "status": "ok",
            "resume_id": resume_id,
            "typst_path": typst_path,
            "pdf_path": pdf_path,
            "page_count": pages,
        }
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
    save_config(incoming)
    return {"success": True, "config": load_config()}


# --- Sync ------------------------------------------------------------------------------


@app.post("/api/sync")
async def trigger_sync(background_tasks: BackgroundTasks):
    from findajob.core.scheduler import scheduler

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
    from findajob.core.scheduler import scheduler

    return scheduler.get_status()


@app.get("/api/scheduler/preferences")
def get_scheduler_preferences_endpoint():
    """The user-owned automatic-run preference and current in-process status."""
    from findajob.core.scheduler import scheduler

    # This synchronous handler calls get_db itself, so the thread-local connection is
    # created or reused in the same worker thread that uses it.
    db = get_db()
    preferences = get_scheduler_preferences(db.conn)
    return {**preferences, "status": scheduler.get_status()}


@app.put("/api/scheduler/preferences")
async def update_scheduler_preferences_endpoint(payload: SchedulerPreferencesUpdate):
    """Persist and apply automatic-run consent without requiring a server restart."""
    from findajob.core.scheduler import scheduler

    # FastAPI resolves sync dependencies in a worker thread but executes this async
    # endpoint on the event loop. Use a short-lived connection here rather than carrying
    # the thread-local dashboard connection across that boundary.
    db = Database()
    try:
        preferences = update_scheduler_preferences(db.conn, enabled=payload.enabled)
    finally:
        db.conn.close()
    if payload.enabled:
        await scheduler.start()
    else:
        await scheduler.stop()
    return {**preferences, "status": scheduler.get_status()}


@app.post("/api/scheduler/trigger/{stage}")
async def trigger_scheduler_stage(stage: str, background_tasks: BackgroundTasks):
    from findajob.core.scheduler import scheduler

    if stage not in ["search", "score"]:
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
        tasks = scrape_tasks(db, config, source)
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

    `findajob status` (findajob/core/status.py) already answers "is each stage
    stalled" from history alone -- last completed run, verdict backlog, verdict recency.
    This reuses that report and adds the two things it cannot show: how far the scrape
    *in progress right now* has gotten, and whether the scorer -- which has no lock file,
    only a timer -- has written anything in the last few minutes.
    """
    from findajob.core import status_repository as status_repo

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
# API responses include database-backed counters that can change outside the web process.
# `no-cache` still allows an intermediary to retain and reuse a response after a
# revalidation, which made the dashboard badges appear stuck. Do not store API snapshots.
CACHE_HEADERS = {"Cache-Control": "no-store"}
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
        return {"job": None, "requirement_rows": [], "next_job_id": None}

    # Fetched by id rather than searched for in the page above: the posting a link
    # points at need not be on the page the link was rendered from, and after a status
    # change it usually is not.
    match = db.query_jobs(job_id=job_id, status=None, limit=1)["jobs"]
    job = match[0] if match else None
    if job is None:
        return {"job": None, "requirement_rows": [], "next_job_id": None}

    ids = db.job_ids_for(**query.as_db_kwargs())
    try:
        position = ids.index(job_id)
    except ValueError:
        # The posting is not on this page of this filter -- a deep link, or a status change
        # that just moved it out of the feed. There is no "next" to offer.
        next_job_id = None
    else:
        next_job_id = ids[position + 1] if position + 1 < len(ids) else None

    from findajob.profile.repository import get_latest_tailored_resume

    return {
        "job": job,
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
    country: str = "",
    location: str = "",
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
            "country": country,
            "location": location,
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
    scoring = load_config().get("scoring") or {}
    return {
        "countries": rendering.country_choices(load_targets()),
        "reason_types": rendering.REASON_TYPE_CHOICES,
        "fit_threshold": int(scoring.get("fit_threshold", 70)),
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
    2. Import `live_hub` from `findajob.web.live`.
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
