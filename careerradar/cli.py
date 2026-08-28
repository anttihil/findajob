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


def _cmd_search(args: argparse.Namespace) -> Any:
    from careerradar.search.runner import run_sync

    return run_sync(
        dry_run=getattr(args, "dry_run", False),
        force=getattr(args, "force", False),
    )


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


def _cmd_resume(args: argparse.Namespace) -> int:
    sub = args.subcommand
    if sub == "generate":
        from careerradar.core.llm import DEFAULT_AGENT_MODEL
        from careerradar.profile.builder import build_resume_for_job

        model = getattr(args, "model", None) or DEFAULT_AGENT_MODEL
        res = build_resume_for_job(args.job_id, model=model)
        print(f"Generated resume for job {args.job_id}:")
        print(f"  DOCX: {res.get('docx_path')}")
        print(f"  PDF:  {res.get('pdf_path')}")
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
            print(
                f"[{r.get('id')}] Job {r.get('job_id')} ({r.get('job_title')} @ "
                f"{r.get('job_company')}): ATS: {score_str} ({r.get('ats_verdict')}) - "
                f"{r.get('docx_path')}"
            )
        return 0
    if sub == "batch":
        from careerradar.core.database import Database
        from careerradar.core.llm import DEFAULT_AGENT_MODEL
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
                    model=getattr(args, "model", None) or DEFAULT_AGENT_MODEL,
                    db=db,
                )
            return 0
        finally:
            db.close()
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


def _cmd_start(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("careerradar.web.app:app", host=args.host, port=args.port, reload=args.reload)
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
    res_gen.add_argument("--model", help="DeepSeek model to use for tailoring")
    res_gen.set_defaults(func=_cmd_resume)

    res_list = res_sub.add_parser("list", help="list generated tailored resumes")
    res_list.add_argument("--limit", type=int, default=20, help="max resumes to list")
    res_list.set_defaults(func=_cmd_resume)

    res_batch = res_sub.add_parser("batch", help="batch generate resumes for jobs by status")
    res_batch.add_argument("--status", default="saved", help="status to match (default: saved)")
    res_batch.add_argument("--model", help="DeepSeek model to use for tailoring")
    res_batch.set_defaults(func=_cmd_resume)

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
