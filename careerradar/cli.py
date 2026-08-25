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


def _cmd_score_stats(args: argparse.Namespace) -> Any:
    from careerradar.scoring.stats import run_stats

    return run_stats(args.profile_version, scale_version=args.scale_version)


def _cmd_score_retry(args: argparse.Namespace) -> Any:
    from careerradar.scoring.worker import run_retry

    return run_retry(args.job_id)


def _cmd_search(args: argparse.Namespace) -> Any:
    from careerradar.search.runner import run_sync

    return run_sync(
        dry_run=args.dry_run,
        backfill=args.backfill,
        limit=args.limit,
        sources=args.sources,
        rescore_only=args.rescore_only,
        force=args.force,
    )


def _cmd_search_cost(args: argparse.Namespace) -> Any:
    from careerradar.search.cost import run_cost

    return run_cost(run_id=args.run, limit=args.limit, as_json=args.json)


def _cmd_score(args: argparse.Namespace) -> Any:
    from careerradar.scoring.worker import run_scoring

    return run_scoring(
        limit=args.limit,
        rescore_all=args.rescore_all,
        dry_run=args.dry_run,
    )


def _cmd_research(args: argparse.Namespace) -> Any:
    from careerradar.research.worker import run_research

    return run_research(company=args.company, limit=args.limit, dry_run=args.dry_run)


def _cmd_profile(args: argparse.Namespace) -> Any:
    from careerradar.profile.cli import run_profile_command

    return run_profile_command(args)


def _cmd_seed_cells(args: argparse.Namespace) -> Any:
    from careerradar.search.seed import seed_cells

    return seed_cells(prune=args.prune)


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
    # Only the stages that write set `stage`; a read-only command (status, cost, stats)
    # must never queue behind a 20-minute scrape.
    parser.set_defaults(stage=None)
    sub = parser.add_subparsers(dest="command", required=True)

    # --- start / web -----------------------------------------------------------------
    st_parser = sub.add_parser("start", help="start the web dashboard and background scheduler")
    st_parser.add_argument("--host", default="127.0.0.1")
    st_parser.add_argument("--port", type=int, default=8010)
    st_parser.add_argument("--reload", action="store_true", help="development autoreload")
    st_parser.set_defaults(func=_cmd_start)

    w = sub.add_parser("web", help="alias for start")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8010)
    w.add_argument("--reload", action="store_true", help="development autoreload")
    w.set_defaults(func=_cmd_start)

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
    sr.add_argument(
        "--backfill", action="store_true", help="widen the recency window to seed a cold corpus"
    )
    sr.add_argument("--limit", type=int, help="cap the number of cells scraped")
    sr.add_argument(
        "--source",
        action="append",
        dest="sources",
        choices=["indeed", "linkedin"],
        help="restrict to one source (repeatable)",
    )
    sr.add_argument(
        "--rescore-only",
        action="store_true",
        help="recompute keyword scores over stored postings; no scraping",
    )
    sr.add_argument("--force", action="store_true", help="ignore the sync lock")
    sr.set_defaults(func=_cmd_search, stage="search")

    scost = ssub.add_parser("cost", help="measured time and requests per cell for one run")
    scost.add_argument("--run", type=int, default=None, help="sync_runs.id (default: latest)")
    scost.add_argument("--limit", type=int, default=10, help="how many slow cells to list")
    scost.add_argument("--json", action="store_true", help="machine-readable")
    scost.set_defaults(func=_cmd_search_cost)

    sc = ssub.add_parser("seed-cells", help="rebuild the scrape cell matrix from roles.yaml")
    sc.add_argument(
        "--prune", action="store_true", help="disable cells no longer implied by roles.yaml"
    )
    sc.set_defaults(func=_cmd_seed_cells)

    # --- score -----------------------------------------------------------------------
    sco = sub.add_parser("score", help="score postings against the profile")
    scosub = sco.add_subparsers(dest="subcommand", required=True)
    scr = scosub.add_parser("run", help="drain unscored postings")
    scr.add_argument("--limit", type=int, help="cap the number of postings scored")
    scr.add_argument(
        "--rescore-all", action="store_true", help="re-score every posting, not just unscored ones"
    )
    scr.add_argument(
        "--dry-run", action="store_true", help="estimate cost and exit without calling the model"
    )
    scr.set_defaults(func=_cmd_score, stage="score")

    scs = scosub.add_parser("stats", help="verdict distribution for a scored corpus")
    scs.add_argument("--profile-version", type=int, help="defaults to the active profile")
    scs.add_argument(
        "--scale-version",
        type=int,
        help="restrict to one scale; 0 is the pre-v6 model-emitted score",
    )
    scs.set_defaults(func=_cmd_score_stats)

    scy = scosub.add_parser(
        "retry", help="re-offer postings withdrawn after repeated scoring failures"
    )
    scy.add_argument(
        "--job-id", type=int, help="clear one posting; defaults to every quarantined posting"
    )
    scy.set_defaults(func=_cmd_score_retry)

    # --- research --------------------------------------------------------------------
    r = sub.add_parser("research", help="build company dossiers for strong matches")
    rsub = r.add_subparsers(dest="subcommand", required=True)
    rr = rsub.add_parser("run", help="research companies behind high-scoring postings")
    rr.add_argument("--company", help="research one named company, ignoring the queue")
    rr.add_argument("--limit", type=int, help="cap the number of companies researched")
    rr.add_argument("--dry-run", action="store_true", help="show what would be researched and exit")
    rr.set_defaults(func=_cmd_research, stage="research")

    # --- db / status -----------------------------------------------------------------
    d = sub.add_parser("migrate", help="apply pending schema migrations")
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
