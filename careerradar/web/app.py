import json
import os
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)
from fastapi.responses import Response as FastAPIResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from starlette.types import Scope

from careerradar.core.config import deep_merge, load_config, save_config
from careerradar.core.database import Database
from careerradar.core.logger import get_logger
from careerradar.core.paths import FRONTEND_DIR, REPO_ROOT, TEMPLATE_DIR
from careerradar.core.status_manager import (
    clear_stale_lock,
    is_sync_running,
    load_sync_status,
    set_sync_progress,
)
from careerradar.market.analytics import MarketAnalytics
from careerradar.market.gap_analysis import GapAnalysis
from careerradar.profile.adapter import NoActiveProfile, load_profile
from careerradar.search.runner import run_sync
from careerradar.search.scheduler import select_cells, with_location_weights
from careerradar.search.sources.link_generator import LinkGenerator
from careerradar.taxonomy.roles import load_roles
from careerradar.taxonomy.skills import load_taxonomy
from careerradar.web import rendering

if TYPE_CHECKING:
    from careerradar.profile.adapter import ProfileAdapter
    from careerradar.taxonomy.roles import RoleTaxonomy
    from careerradar.taxonomy.skills import Taxonomy

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


BASE_DIR = REPO_ROOT


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
    # commutable | remote | relocation. candidates have local or remote preferences, so this is the
    # filter that matters most: anything neither remote nor within commuting distance
    # requires moving house.
    access: str | None = Query(None, pattern="^(commutable|remote|relocation)$"),
    include_duplicates: bool = False,
    min_score: int | None = None,
    # The scalar projection of the ordinals. Kept for a coarse cut, but the ordinals below
    # are what the dashboard should filter on -- they say WHY a posting qualifies, and a
    # threshold on a projected scale cannot.
    min_fit_score: int | None = None,
    verdict: str | None = Query(
        None, pattern="^(strong|worth_applying|stretch|poor_fit|mismatch)$"
    ),
    eligibility: str | None = Query(None, pattern="^(eligible|conditional|blocked)$"),
    role_match: str | None = Query(
        None, pattern="^(same_role|adjacent|different_domain|different_field)$"
    ),
    capability_match: str | None = Query(
        None, pattern="^(exceeds|meets|most_with_gaps|major_gaps|not_close)$"
    ),
    # Pareto tier: 1 dominates everything below it. Filtering `max_tier=4` asks for the top
    # four layers without asserting an exchange rate between the dimensions.
    max_tier: int | None = Query(None, ge=1, le=10),
    liveness: str | None = Query(None, pattern="^(live|stale|likely_closed|unknown)$"),
    pipeline_state: str | None = Query(None, pattern="^(new|scored|researched)$"),
    sort: str = Query("fit", pattern="^(fit|fit_score|match_score|date_found)$"),
    # Paginated from the start: the corpus reaches thousands of rows within days, and
    # renderJobCards builds DOM for every row it receives.
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
            min_fit_score=min_fit_score,
            verdict=verdict,
            eligibility=eligibility,
            role_match=role_match,
            capability_match=capability_match,
            max_tier=max_tier,
            liveness=liveness,
            pipeline_state=pipeline_state,
            sort=sort,
            limit=limit,
            offset=offset,
            # The full row, unlike the dashboard feed. This is a JSON API an external
            # script may already be consuming, and quietly dropping `description` and the
            # verdict detail out of its response would be a breaking change made for the
            # benefit of a caller that is not this one.
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
        return db.get_stats()
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


def dossier_for(db: Database, company: str | None) -> dict[str, Any] | None:
    """The deep-research dossier for one company, or None.

    Factored out so the drawer renders the dossier from the same read the JSON endpoint
    serves. The drawer used to fetch this itself and swallow every failure in a bare
    `catch`, which meant "no dossier" and "the request broke" looked identical.
    """
    from careerradar.search.normalizer import normalize_company

    if not company:
        return None
    # Two indexed probes rather than one `OR`. SQLite will not use an index for either arm
    # of a disjunction across two columns, so the single-statement version scanned
    # `company_dossiers` on every drawer open; both columns are indexed as of migration v9.
    row = db.conn.execute(
        "SELECT * FROM company_dossiers WHERE company_normalized = ?",
        (normalize_company(company),),
    ).fetchone()
    if row is None:
        row = db.conn.execute(
            "SELECT * FROM company_dossiers WHERE company_display = ?", (company,)
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
def get_search_links(
    country: str,
    query: str,
    role_family: str | None = None,  # noqa: ARG001 - declared query parameter, part of the HTTP contract
) -> dict[str, Any]:
    """Boolean search links, built from the profile's strongest skills.

    Previously keyed on a parsed resume variant. The profile is now one unified artifact,
    so the skills come from it directly -- and they are the *canonical* skills the scorer
    uses, rather than whatever the regex parser found in a bulleted list.
    """
    taxonomy = load_taxonomy()
    try:
        profile = load_profile(taxonomy=taxonomy)
    except NoActiveProfile as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # `required` defaults to True, so load_profile raises NoActiveProfile rather than
    # returning None -- this is here only to narrow the type for the calls below.
    assert profile is not None
    strongest = [taxonomy.label(key) for key in sorted(profile.keys(min_level=3))] or [
        taxonomy.label(k) for k in sorted(profile.keys(min_level=2))
    ]
    return LinkGenerator.generate_links(strongest, country, query)


# --- Sync ------------------------------------------------------------------------------


def bg_sync_task():
    # trigger_sync() already validated-and-set the lock synchronously, right before
    # scheduling this task, specifically to close the check-then-act race between two
    # rapid clicks. Without force=True, run_sync()'s own internal lock check sees that
    # same lock -- owner_pid is THIS process, since a FastAPI background task runs
    # in-process, not as a subprocess -- and refuses, mistaking itself for a concurrent
    # sync. The refusal returns before run_sync()'s try/finally, so the lock it never
    # actually held is never released either: every dashboard Sync click silently failed
    # and stuck sync_in_progress=true for up to 90 minutes.
    run_sync(force=True)


@app.post("/api/sync")
def trigger_sync(background_tasks: BackgroundTasks):
    # is_sync_running checks the owning PID rather than trusting the flag, so a crashed
    # sync no longer wedges this endpoint permanently.
    if is_sync_running():
        return JSONResponse(status_code=409, content={"message": "Sync is already in progress"})
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


# An asset requested at a versioned URL can be cached hard, because the version is the
# file's mtime: change the file and every page that references it names a different URL on
# the next render. That keeps the matched-set guarantee above -- the page is still
# `no-cache`, so it is always the current page that decides which asset URLs are current --
# while removing the revalidation round trip each asset used to cost on every navigation.
IMMUTABLE_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


class RevalidatedStaticFiles(StaticFiles):
    """StaticFiles that asks before reusing anything, unless the URL is versioned.

    See CACHE_HEADERS and IMMUTABLE_CACHE_HEADERS. An unversioned URL still revalidates,
    so a hand-written or third-party reference cannot accidentally pin a stale file.
    """

    async def get_response(self, path: str, scope: Scope) -> FastAPIResponse:
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers.update(IMMUTABLE_CACHE_HEADERS if versioned else CACHE_HEADERS)
        return response


def static_url(path: str) -> str:
    """URL for a bundled asset, stamped with its modification time.

    Stat on every render rather than once at import: editing a stylesheet and reloading
    has to show the edit, and the stat is immaterial next to rendering the page.

    Note this stamps only what a template names directly. The ES module graph that
    `main.js` pulls in is fetched by the browser at unversioned URLs and so keeps
    revalidating, which is the conservative half of the same rule.
    """
    try:
        stamp = int(os.path.getmtime(os.path.join(FRONTEND_DIR, path)))
    except OSError:
        # A missing asset is a 404 to be seen, not an exception during render.
        return f"/static/{path}"
    return f"/static/{path}?v={stamp}"


app.mount("/static", RevalidatedStaticFiles(directory=FRONTEND_DIR), name="static")


# --- Server-rendered dashboard -----------------------------------------------------------

templates = Jinja2Templates(directory=TEMPLATE_DIR)
templates.env.filters["short_date"] = rendering.short_date
templates.env.filters["hostname"] = rendering.hostname
templates.env.globals["static"] = static_url


def drawer_context(
    db: Database, query: rendering.FilterQuery, job_id: int | None
) -> dict[str, Any]:
    """Everything `partials/job_drawer.html` renders, for one posting or for none.

    Shared by `dashboard()` and `GET /drawer` so the two cannot drift. They render the
    same partial from the same data; the only difference is that one wraps it in a page.

    `next_job_id` is the posting after this one *in the current feed order*, which is why
    the filter has to come in with the job id -- "next" is a property of the list you are
    reading, not of the posting. It is what the drawer's status buttons advance to.
    """
    if job_id is None:
        return {
            "job": None,
            "dossier": None,
            "requirement_rows": [],
            "highlighted_description": "",
            "next_job_id": None,
        }

    # Fetched by id rather than searched for in the page above: the posting a link
    # points at need not be on the page the link was rendered from, and after a status
    # change it usually is not.
    match = db.query_jobs(job_id=job_id, status=None, limit=1)["jobs"]
    job = match[0] if match else None
    if job is None:
        return {
            "job": None,
            "dossier": None,
            "requirement_rows": [],
            "highlighted_description": "",
            "next_job_id": None,
        }

    ids = db.job_ids_for(**query.as_db_kwargs())
    try:
        position = ids.index(job_id)
    except ValueError:
        # The posting is not on this page of this filter -- a deep link, or a status change
        # that just moved it out of the feed. There is no "next" to offer.
        next_job_id = None
    else:
        next_job_id = ids[position + 1] if position + 1 < len(ids) else None

    return {
        "job": job,
        "dossier": dossier_for(db, job.get("company")),
        "requirement_rows": rendering.requirement_rows(job),
        "highlighted_description": rendering.highlight_terms(
            job.get("description"), job.get("matched_skills") or []
        ),
        "next_job_id": next_job_id,
    }


# The filter, as FastAPI parameters. Declared once and depended on by both `/` and
# `/drawer` so that the two routes cannot accept different filters -- the drawer is built
# from the feed it was opened out of, and a parameter only one of them honoured would make
# "the next posting" mean two different things.
def filter_query(
    # These names are the query parameters of `Database.query_jobs`, deliberately. The
    # filter form posts them straight through, so a field whose name is wrong fails here
    # at validation instead of being silently dropped -- which is how the old dashboard
    # shipped a sort control that sent nothing and a resume filter that filtered nothing.
    status: str = Query("unread", pattern="^(unread|saved|applied|rejected)$"),
    access: str = Query("", pattern="^(commutable|remote|relocation)?$"),
    country: str = "",
    min_score: int | None = Query(None, ge=0, le=100),
    max_tier: int | None = Query(None, ge=1, le=10),
    verdict: str = Query("", pattern="^(strong|worth_applying|stretch|poor_fit|mismatch)?$"),
    eligibility: str = Query("", pattern="^(eligible|conditional|blocked)?$"),
    sort: str = Query("fit", pattern="^(fit|fit_score|match_score|date_found)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> rendering.FilterQuery:
    return rendering.FilterQuery(
        {
            "status": status,
            "access": access,
            "country": country,
            "min_score": min_score,
            "max_tier": max_tier,
            "verdict": verdict,
            "eligibility": eligibility,
            "sort": sort,
            "limit": limit,
            "offset": offset,
        }
    )


@app.get("/drawer", response_class=HTMLResponse)
def drawer(
    request: Request,
    query: rendering.FilterQuery = Depends(filter_query),
    # Which posting to render. Absent means the closed drawer, which is how the close
    # affordances work: one endpoint, and `job_drawer.html` already renders nothing when
    # `job` is undefined.
    job: int | None = None,
):
    """The drawer partial on its own -- no feed query, no stats, no page.

    This is the whole point of the change: opening a posting used to re-render the entire
    dashboard because the drawer was page state, so a click cost a feed query, a stats
    rollup and a 93KB document the browser had to rebuild. Here it costs one indexed row.
    """
    db = get_db()
    try:
        return templates.TemplateResponse(
            request,
            "partials/job_drawer.html",
            {"query": query, **drawer_context(db, query, job)},
            headers=CACHE_HEADERS,
        )
    finally:
        db.close()


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    query: rendering.FilterQuery = Depends(filter_query),
    # Which posting the drawer is showing, if any. The drawer is page state rather than
    # client state: it is in the URL, so it survives a reload and the back button closes
    # it. Opening one used to depend on the card list still being in a JS array.
    #
    # htmx now swaps the drawer in without a navigation, but this path is unchanged and
    # still renders it server-side -- it is what a reload, a deep link and a browser with
    # no JavaScript get, and `/drawer` shares its context builder so the two agree.
    job: int | None = None,
):
    db = get_db()
    try:
        page = db.query_jobs(**query.as_db_kwargs())
        stats = db.get_stats()

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
                **drawer_context(db, query, job),
            },
            headers=CACHE_HEADERS,
        )
    finally:
        db.close()


@app.post("/jobs/{job_id}/status")
def set_job_status_form(
    request: Request,
    job_id: int,
    status: str = Form(...),
    next: str = "/",
    # The posting to advance to, resolved when this drawer was rendered
    # (`drawer_context`). Empty when the acted-on posting was the last on the page.
    next_job_id: str = Form(""),
    query: rendering.FilterQuery = Depends(filter_query),
):
    """Status change from the drawer.

    Without JavaScript this is a plain form post that redirects back to the URL the form
    came from, so the drawer stays open on the posting you were reading. The JSON endpoint
    at PUT /api/jobs/{id}/status is unchanged and is what the CLI and any script should
    use.

    With htmx it advances: the reply is the *next* posting's drawer, plus out-of-band
    fragments that drop the acted-on card from the feed and correct the counters it moved
    between. Reading a posting and deciding on it is one action, and under the default
    `status=unread` filter the posting you just judged is no longer in the list you are
    working through -- re-rendering the drawer onto it was showing you the one thing you
    were finished with.
    """
    if status not in ("unread", "saved", "applied", "rejected"):
        raise HTTPException(status_code=400, detail="Invalid status value")
    db = get_db()
    try:
        if not db.update_job_status(job_id, status):
            raise HTTPException(status_code=404, detail="Job not found")

        if request.headers.get("hx-request") != "true":
            # 303 so the browser follows with GET; a 307 would repeat the POST on reload.
            return RedirectResponse(next or "/", status_code=303)

        target = int(next_job_id) if next_job_id.isdigit() else None
        context = {"query": query, **drawer_context(db, query, target)}
        # `drawer_context` returns job=None if the id no longer resolves, which renders the
        # closed drawer -- the right outcome for the last posting on a page.
        if context["job"] is None:
            target = None
        body = templates.get_template("partials/job_drawer.html").render(request=request, **context)

        # The card goes only if the posting has actually left this feed. Under the default
        # `status=unread` it always has; under `status=saved`, marking something applied
        # also removes it, while re-saving an already-saved posting does not.
        remaining = db.job_ids_for(**query.as_db_kwargs())
        oob = []
        if job_id not in remaining:
            oob.append(f'<a id="job-card-{job_id}" hx-swap-oob="delete"></a>')
        # Two of the four stat tiles count statuses, so a triage pass walks them out of
        # date. The other two (total crawled, strong matches) a status change cannot move.
        counts = db.status_counts()
        oob.append(f'<h3 id="stat-saved" hx-swap-oob="true">{counts.get("saved", 0)}</h3>')
        oob.append(f'<h3 id="stat-applied" hx-swap-oob="true">{counts.get("applied", 0)}</h3>')

        return HTMLResponse(
            body + "".join(oob),
            headers={
                **CACHE_HEADERS,
                # The address bar follows the drawer, so a reload lands on the posting on
                # screen rather than the one that was there before the click.
                "HX-Push-Url": (query.with_job(target) if target else query.without_job()),
            },
        )
    finally:
        db.close()
