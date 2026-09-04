"""careerradar -- one entry point for every stage of the pipeline.

The pipeline is four stages that talk to each other only through `jobs.pipeline_state`:

    search    scrape boards, write postings as state='new'
    score     drain 'new', write a verdict, mark 'scored'
    research  drain high-scoring 'scored' companies, write a dossier, mark 'researched'
    start     serve the dashboard and run the background scheduler

`start` runs the FastAPI dashboard and background asyncio scheduler, which runs
stages in isolated worker subprocesses and automatically chains search -> score -> research.
"""

import argparse
import sys
from typing import Any


def _cmd_score_retry(args: argparse.Namespace) -> Any:
    from careerradar.scoring.worker import run_retry

    return run_retry(args.job_id)


def _cmd_search(args: argparse.Namespace) -> int:
    from careerradar.search.runner import run_sync

    res = run_sync(
        dry_run=getattr(args, "dry_run", False),
        force=getattr(args, "force", False),
    )
    if isinstance(res, dict):
        if res.get("cells_planned") and not res.get("cells_succeeded"):
            return 1
        return 0
    return 0 if res is None else int(bool(res))


def _cmd_score(args: argparse.Namespace) -> Any:
    from careerradar.scoring.worker import run_scoring

    return run_scoring(
        limit=getattr(args, "limit", None),
    )


def _cmd_research(args: argparse.Namespace) -> Any:
    from careerradar.research.worker import run_research

    return run_research(
        company=getattr(args, "company", None),
        limit=getattr(args, "limit", None),
    )


def _cmd_profile(args: argparse.Namespace) -> Any:
    from careerradar.profile.cli import run_profile_command

    return run_profile_command(args)


def _cmd_import(args: argparse.Namespace) -> int:
    from careerradar.search.importer import import_and_process_job

    url = args.url
    score = not getattr(args, "no_score", False)
    generate_resume = getattr(args, "resume", False)
    model = getattr(args, "model", None)

    print(f"Importing job from {url}...")
    res = import_and_process_job(
        url=url,
        score=score,
        generate_resume=generate_resume,
        model=model,
    )
    job_id = res["job_id"]
    job = res.get("job") or {}
    print(f"Imported job {job_id}: {job.get('title')} @ {job.get('company')}")
    if res.get("verdict"):
        v = res["verdict"]
        fit_str = "FIT" if v.get("fit") else "NO FIT"
        reason = (
            f" ({v.get('reason_type')}: {v.get('reason_description')})"
            if v.get("reason_type")
            else ""
        )
        print(f"  Scoring Verdict: {fit_str}{reason}")
    if res.get("resume"):
        r = res["resume"]
        print("  Resume generated:")
        if r.get("typst_path"):
            print(f"    Typst: {r.get('typst_path')}")
        print(f"    PDF:   {r.get('pdf_path')}")
        if r.get("ats_score") is not None:
            print(f"    ATS Match Score: {r.get('ats_score')}/10 ({r.get('ats_verdict')})")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    sub = args.subcommand
    if sub == "generate":
        from careerradar.profile.builder import build_resume_for_job

        model = getattr(args, "model", None)
        res = build_resume_for_job(args.job_id, model=model)
        print(f"Generated resume for job {args.job_id}:")
        if res.get("typst_path"):
            print(f"  Typst: {res.get('typst_path')}")
        print(f"  PDF:   {res.get('pdf_path')}")
        if res.get("ats_score") is not None:
            print(f"  ATS Match Score: {res.get('ats_score')}/10 ({res.get('ats_verdict')})")
        return 0
    if sub == "list":
        from careerradar.profile.repository import list_tailored_resumes

        limit = getattr(args, "limit", 20) or 20
        resumes = list_tailored_resumes(limit=limit)
        if not resumes:
            print("No generated resumes found.")
            return 0
        for r in resumes:
            score_str = f"{r.get('ats_score')}/10" if r.get("ats_score") is not None else "N/A"
            source_file = r.get("typst_path")
            print(
                f"[{r.get('id')}] Job {r.get('job_id')} ({r.get('job_title')} @ "
                f"{r.get('job_company')}): ATS: {score_str} ({r.get('ats_verdict')}) - "
                f"{source_file}"
            )
        return 0
    if sub == "batch":
        from careerradar.core.database import Database
        from careerradar.profile.builder import build_resume_for_job

        status = getattr(args, "status", "saved")
        db = Database()
        try:
            jobs_res = db.query_jobs(status=status, limit=100)
            jobs = jobs_res.get("jobs") or []
            print(f"Found {len(jobs)} jobs with status='{status}'.")
            for j in jobs:
                jid = j["id"]
                print(f"Building resume for job {jid}: {j.get('title')} @ {j.get('company')}...")
                build_resume_for_job(
                    jid,
                    model=getattr(args, "model", None),
                    db=db,
                )
            return 0
        finally:
            db.close()
    return 0


def _cmd_target(args: argparse.Namespace) -> int:
    import sqlite3

    from careerradar.core.config import load_config
    from careerradar.core.paths import DB_PATH
    from careerradar.search.capacity import calculate_capacity
    from careerradar.taxonomy import repository as target_repo

    sub = args.subcommand
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if sub == "list":
            roles = target_repo.get_target_roles(conn)
            if not roles:
                print("No target roles configured.")
                return 0
            print(f"{'Key':<25} {'Label':<28} {'Status':<10} {'Queries'}")
            print("-" * 80)
            for r in roles:
                queries = [
                    q["query"] for q in target_repo.get_target_queries(conn, role_key=r["key"])
                ]
                status = "ACTIVE" if r["enabled"] else "PAUSED"
                print(f"{r['key']:<25} {r['label']:<28} {status:<10} {', '.join(queries)}")
            return 0

        if sub == "add":
            aliases = [a.strip() for a in (args.aliases or "").split(",") if a.strip()]
            queries = [q.strip() for q in (args.queries or "").split(",") if q.strip()]
            target_repo.save_target_role(
                conn,
                key=args.key,
                label=args.label or args.key.replace("_", " ").title(),
                aliases=aliases,
                enabled=True,
            )
            for q in queries:
                target_repo.add_target_query(conn, role_key=args.key, query_term=q)
            print(f"Added target role '{args.key}' with {len(queries)} queries.")
            return 0

        if sub == "toggle":
            enabled = not getattr(args, "disable", False)
            target_repo.toggle_target_role(conn, key=args.key, enabled=enabled)
            print(f"Target role '{args.key}' is now {'ACTIVE' if enabled else 'PAUSED'}.")
            return 0

        if sub == "delete":
            target_repo.delete_target_role(conn, key=args.key)
            print(f"Deleted target role '{args.key}'.")
            return 0

        if sub == "status":
            roles = target_repo.get_target_roles(conn, enabled_only=True)
            queries = target_repo.get_target_queries(conn, enabled_only=True)
            locs = target_repo.get_target_locations(conn, enabled_only=True)
            cfg = load_config()
            cap = calculate_capacity(len(queries), len(locs), cfg)
            print(f"Active Roles:     {len(roles)}")
            print(f"Active Queries:   {len(queries)}")
            print(f"Active Locations: {len(locs)}")
            print(f"Search Pairs:     {cap['search_pairs']} ({cap['total_cells']} total cells)")
            print(f"Cycle Duration:   ~{cap['cycle_hours']}h ({cap['cycle_days']} days)")
            print(f"Capacity Zone:    [{cap['zone'].upper()}]")
            print(f"Guidance:         {cap['message']}")
            return 0
    finally:
        conn.close()
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:  # noqa: ARG001 - argparse handler signature
    import sqlite3

    from careerradar.core.migrations import current_version, migrate
    from careerradar.core.paths import DB_PATH
    from careerradar.search.seed import seed_cells

    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
        print(f"schema version: {current_version(conn)}")
    finally:
        conn.close()

    seed_cells(prune=True)
    return 0


def _cmd_status(args: argparse.Namespace) -> Any:
    from careerradar.core.status import run_status

    return run_status(as_json=args.json)


def _cmd_llm(args: argparse.Namespace) -> int:
    import subprocess

    from careerradar.core.llm import get_llm_provider, list_available_providers

    sub = args.subcommand
    if sub == "status":
        providers = list_available_providers()
        print(f"{'Provider':<12} {'Type':<8} {'Available':<12} {'Status'}")
        print("-" * 80)
        for p in providers:
            avail_str = "YES" if p["available"] else "NO"
            print(f"{p['name']:<12} {p['type']:<8} {avail_str:<12} {p['status']}")
        print()
        try:
            active = get_llm_provider()
            print(f"Active Provider: {active.name.upper()}")
        except Exception as exc:  # noqa: BLE001
            print(f"Active Provider: NONE ({exc})")
        return 0

    if sub == "auth":
        target = getattr(args, "provider", None) or "claude"
        if target == "agy":
            return subprocess.run(["agy"], check=False).returncode
        if target == "claude":
            return subprocess.run(["claude", "auth", "login"], check=False).returncode
        if target == "codex":
            return subprocess.run(["codex", "login"], check=False).returncode
        if target == "opencode":
            return subprocess.run(["opencode", "providers", "login"], check=False).returncode
        print(f"Interactive login not supported for provider '{target}'.")
        return 1

    if sub == "test":
        from pydantic import BaseModel

        class TestOutput(BaseModel):
            message: str
            answer: int

        provider_name = getattr(args, "provider", None)
        try:
            prov = get_llm_provider(provider_name)
            print(f"Testing provider '{prov.name}'...")
            print("1. Text completion test...")
            resp = prov.complete("Respond with the single word SUCCESS.")
            print(f"   Output: {resp.content.strip()}")

            print("2. Structured output test...")
            structured = prov.complete_structured(
                TestOutput,
                "Return a JSON object with message='Hello CareerRadar' and answer=42.",
                label="cli_test",
            )
            print(f"   Structured Output: {structured.model_dump()}")
            print(f"Provider '{prov.name}' is operational!")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Test failed: {exc}")
            return 1

    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "careerradar.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        timeout_graceful_shutdown=5,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    doc = __doc__ or ""
    parser = argparse.ArgumentParser(prog="careerradar", description=doc.split("\n")[0])
    parser.set_defaults(stage=None)
    sub = parser.add_subparsers(dest="command", required=True)

    # --- start -----------------------------------------------------------------------
    st_parser = sub.add_parser("start", help="start the web dashboard and background scheduler")
    st_parser.add_argument("--host", default="127.0.0.1")
    st_parser.add_argument("--port", type=int, default=8010)
    st_parser.add_argument("--reload", action="store_true", help="development autoreload")
    st_parser.set_defaults(func=_cmd_start)

    # --- profile ---------------------------------------------------------------------
    p = sub.add_parser("profile", help="build or inspect the candidate profile")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pb = psub.add_parser("build", help="run the document ingest + interview wizard")
    pb.add_argument("file", nargs="?", help="optional path to resume document (pdf, md, txt)")
    pb.add_argument("--resume", action="store_true", help="continue an interview left unfinished")
    pb.add_argument(
        "--force", action="store_true", help="rebuild even if the corpus has not changed"
    )
    pb.add_argument(
        "--no-interview",
        action="store_true",
        help="build from the documents alone, skipping the interview",
    )
    psub.add_parser("show", help="print the active profile")
    psub.add_parser("history", help="list every profile version")
    p.set_defaults(func=_cmd_profile)

    # --- search ----------------------------------------------------------------------
    s = sub.add_parser("search", help="scrape job boards")
    ssub = s.add_subparsers(dest="subcommand", required=True)
    sr = ssub.add_parser("run", help="run one scrape pass")
    sr.add_argument("--dry-run", action="store_true", help="plan and fetch, but write nothing")
    sr.add_argument("--force", action="store_true", help="ignore the sync lock")
    sr.set_defaults(func=_cmd_search, stage="search")

    # --- score -----------------------------------------------------------------------
    sco = sub.add_parser("score", help="score postings against the profile")
    scosub = sco.add_subparsers(dest="subcommand", required=True)
    scr = scosub.add_parser("run", help="drain unscored postings")
    scr.add_argument("--limit", type=int, help="cap the number of postings scored")
    scr.set_defaults(func=_cmd_score, stage="score")

    scy = scosub.add_parser(
        "retry", help="re-offer postings withdrawn after repeated scoring failures"
    )
    scy.add_argument(
        "--job-id", type=int, help="clear one posting; defaults to every quarantined posting"
    )
    scy.set_defaults(func=_cmd_score_retry, stage="score")

    # --- research --------------------------------------------------------------------
    r = sub.add_parser("research", help="build company dossiers for strong matches")
    rsub = r.add_subparsers(dest="subcommand", required=True)
    rr = rsub.add_parser("run", help="research companies behind high-scoring postings")
    rr.add_argument("--company", help="research one named company, ignoring the queue")
    rr.add_argument("--limit", type=int, help="cap the number of companies researched")
    rr.set_defaults(func=_cmd_research, stage="research")

    # --- resume ----------------------------------------------------------------------
    res = sub.add_parser("resume", help="tailor, validate, and generate 1-page resumes")
    res_sub = res.add_subparsers(dest="subcommand", required=True)
    res_gen = res_sub.add_parser("generate", help="generate tailored resume for a specific job")
    res_gen.add_argument("job_id", type=int, help="job posting database id")
    res_gen.add_argument(
        "--model", help="Model to use for tailoring (or leave empty for provider default)"
    )
    res_gen.set_defaults(func=_cmd_resume)

    res_list = res_sub.add_parser("list", help="list generated tailored resumes")
    res_list.add_argument("--limit", type=int, default=20, help="max resumes to list")
    res_list.set_defaults(func=_cmd_resume)

    res_batch = res_sub.add_parser("batch", help="batch generate resumes for jobs by status")
    res_batch.add_argument("--status", default="saved", help="status to match (default: saved)")
    res_batch.add_argument(
        "--model", help="Model to use for tailoring (or leave empty for provider default)"
    )
    res_batch.set_defaults(func=_cmd_resume)

    # --- import ----------------------------------------------------------------------
    imp = sub.add_parser(
        "import", help="import a job from URL, score it, and optionally generate a resume"
    )
    imp.add_argument("url", help="URL of the job posting")
    imp.add_argument("--no-score", action="store_true", help="skip scoring stage")
    imp.add_argument("--resume", action="store_true", help="generate tailored resume after scoring")
    imp.add_argument("--model", help="Model to use for extraction/scoring/tailoring")
    imp.set_defaults(func=_cmd_import)

    # --- target ----------------------------------------------------------------------
    tar = sub.add_parser("target", help="manage target roles, search queries, and capacity")
    tar_sub = tar.add_subparsers(dest="subcommand", required=True)

    tar_sub.add_parser("list", help="list all configured target roles and queries")

    tar_add = tar_sub.add_parser("add", help="add a new target role with queries")
    tar_add.add_argument("key", help="unique role key (e.g. ai_engineer)")
    tar_add.add_argument("--label", help="display label (e.g. AI Engineer)")
    tar_add.add_argument("--aliases", help="comma-separated aliases")
    tar_add.add_argument("--queries", help="comma-separated search queries")

    tar_tog = tar_sub.add_parser("toggle", help="enable or pause a target role")
    tar_tog.add_argument("key", help="unique role key")
    tar_tog.add_argument("--disable", action="store_true", help="pause this target role")

    tar_del = tar_sub.add_parser("delete", help="delete a target role and its queries")
    tar_del.add_argument("key", help="unique role key")

    tar_sub.add_parser("status", help="show capacity status and matrix cycle guidance")
    tar.set_defaults(func=_cmd_target)

    # --- llm -------------------------------------------------------------------------
    llm_parser = sub.add_parser(
        "llm", help="inspect LLM providers, test connections, and authenticate"
    )
    llm_sub = llm_parser.add_subparsers(dest="subcommand", required=True)
    llm_sub.add_parser("status", help="list all LLM providers and availability status")

    llm_auth = llm_sub.add_parser("auth", help="launch interactive login for a CLI provider")
    llm_auth.add_argument(
        "provider",
        choices=["agy", "claude", "codex", "opencode"],
        help="CLI provider to authenticate",
    )

    llm_test = llm_sub.add_parser(
        "test", help="run a quick completion and structured test against a provider"
    )
    llm_test.add_argument(
        "--provider", help="provider to test (e.g. agy, claude, codex, opencode, deepseek)"
    )
    llm_parser.set_defaults(func=_cmd_llm)

    # --- db / status -----------------------------------------------------------------
    d = sub.add_parser("migrate", help="apply pending schema migrations and seed cells")
    d.set_defaults(func=_cmd_migrate, stage="migrate")

    st = sub.add_parser("status", help="one health report for every stage of the pipeline")
    st.add_argument("--json", action="store_true", help="machine-readable, for piping over ssh")
    st.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.stage:
        return args.func(args) or 0

    from careerradar.core import pipeline_lock

    with pipeline_lock.hold(args.stage):
        return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
