import os
import sys
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.analytics import MarketAnalytics
from backend.config import load_config, save_config
from backend.database import Database
from backend.gap_analysis import GapAnalysis
from backend.profile import build_profile
from backend.resume_parser import ResumeParser
from backend.roles import load_roles
from backend.scheduler import select_cells
from backend.sources.link_generator import LinkGenerator
from backend.status_manager import (
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from backend.taxonomy import load_taxonomy
from sync import run_sync

app = FastAPI(title="Job Search Automation Dashboard")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


def get_db():
    return Database()


def resumes_dir():
    config = load_config()
    if config.get("resumes_dir"):
        return os.path.abspath(os.path.join(BASE_DIR, config["resumes_dir"]))
    return os.path.join(BASE_DIR, "resumes")


def analytics_context(db):
    """Shared objects for the analytics endpoints."""
    config = load_config()
    taxonomy = load_taxonomy()
    roles = load_roles()
    profile = build_profile(taxonomy=taxonomy)
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


def deep_merge(base: dict, incoming: dict) -> dict:
    """Recursively merge `incoming` into `base` without dropping absent keys."""
    result = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# --- Jobs ------------------------------------------------------------------------------

@app.get("/api/jobs")
def get_jobs(
    status: Optional[str] = None,
    country: Optional[str] = None,
    resume_match: Optional[str] = None,
    role_family: Optional[str] = None,
    seniority: Optional[str] = None,
    source: Optional[str] = None,
    is_remote: Optional[bool] = None,
    has_salary: Optional[bool] = None,
    include_duplicates: bool = False,
    min_score: Optional[int] = None,
    # Paginated from the start: the corpus reaches thousands of rows within days, and
    # renderJobCards builds DOM for every row it receives.
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    db = get_db()
    try:
        return db.query_jobs(
            status=status, country=country, resume_match=resume_match,
            role_family=role_family, seniority=seniority, source=source,
            is_remote=is_remote, has_salary=has_salary,
            include_duplicates=include_duplicates, min_score=min_score,
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
    """The user's canonical skill profile, with graded evidence."""
    taxonomy = load_taxonomy()
    return build_profile(taxonomy=taxonomy).to_dict()


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


@app.get("/api/resumes")
def get_parsed_resumes():
    return ResumeParser(resumes_dir()).parse_all()


@app.get("/api/search-links")
def get_search_links(resume: str, country: str, query: str):
    resumes = ResumeParser(resumes_dir()).parse_all()
    if resume not in resumes:
        raise HTTPException(status_code=404, detail="Resume file not found")
    return LinkGenerator.generate_links(resumes[resume]["skills"], country, query)


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
        tasks = select_cells(cells, config.get("scraper", {}), roles, source)
        return {
            "source": source,
            "cells_total": len(cells),
            "cells_planned": len(tasks),
            "tasks": [task.to_dict() for task in tasks],
        }
    finally:
        db.close()


# --- Digests ----------------------------------------------------------------------------

from backend.digest import DigestGenerator  # noqa: E402


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

app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.get("/")
def serve_home():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return HTMLResponse("<h1>Frontend dashboard index.html not found yet.</h1>")
