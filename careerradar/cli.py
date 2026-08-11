"""careerradar -- one entry point for every stage of the pipeline.

The pipeline is four stages that talk to each other only through `jobs.pipeline_state`:

    search    scrape boards, write postings as state='new'
    score     drain 'new', write a verdict, mark 'scored'
    research  drain high-scoring 'scored' companies, write a dossier, mark 'researched'
    web       serve the dashboard

Each runs on its own systemd timer. They are separate commands rather than one `sync`
because a failure in one stage should not cost the work of another -- a rate-limited board
must not stall scoring of the backlog, and a bad API key must not lose a scrape.

`profile build` is the odd one out: it runs once, interactively, and everything else reads
what it produces.
"""

import argparse
import sys


def _cmd_search(args):
    from careerradar.search.runner import run_sync

    return run_sync(
        dry_run=args.dry_run,
        backfill=args.backfill,
        limit=args.limit,
        sources=args.sources,
        rescore_only=args.rescore_only,
        force=args.force,
    )


def _cmd_score(args):
    from careerradar.scoring.worker import run_scoring

    return run_scoring(
        limit=args.limit,
        rescore_all=args.rescore_all,
        dry_run=args.dry_run,
    )


def _cmd_research(args):
    from careerradar.research.worker import run_research

    return run_research(company=args.company, limit=args.limit, dry_run=args.dry_run)


def _cmd_profile(args):
    from careerradar.profile.cli import run_profile_command

    return run_profile_command(args)


def _cmd_seed_cells(args):
    from careerradar.search.seed import seed_cells

    return seed_cells(prune=args.prune, queries_per_family=args.queries_per_family)


def _cmd_migrate(args):
    import sqlite3

    from careerradar.core.migrations import current_version, migrate
    from careerradar.core.paths import DB_PATH

    conn = sqlite3.connect(DB_PATH)
    try:
        migrate(conn)
        print(f"schema version: {current_version(conn)}")
    finally:
        conn.close()
    return 0


def _cmd_web(args):
    import uvicorn

    uvicorn.run(
        "careerradar.web.app:app", host=args.host, port=args.port, reload=args.reload
    )
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="careerradar", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    # --- profile ---------------------------------------------------------------------
    p = sub.add_parser("profile", help="build or inspect the candidate profile")
    psub = p.add_subparsers(dest="subcommand", required=True)
    pb = psub.add_parser("build", help="run the document ingest + interview wizard")
    pb.add_argument("--resume", action="store_true",
                    help="resume an interview left unfinished")
    pb.add_argument("--restart", action="store_true",
                    help="discard any in-progress interview and start over")
    pb.add_argument("--no-interview", action="store_true",
                    help="build from the documents alone, skipping the interview")
    psub.add_parser("show", help="print the active profile")
    psub.add_parser("history", help="list every profile version")
    p.set_defaults(func=_cmd_profile)

    # --- search ----------------------------------------------------------------------
    s = sub.add_parser("search", help="scrape job boards")
    ssub = s.add_subparsers(dest="subcommand", required=True)
    sr = ssub.add_parser("run", help="run one scrape pass")
    sr.add_argument("--dry-run", action="store_true",
                    help="plan and fetch, but write nothing")
    sr.add_argument("--backfill", action="store_true",
                    help="widen the recency window to seed a cold corpus")
    sr.add_argument("--limit", type=int, help="cap the number of cells scraped")
    sr.add_argument("--source", action="append", dest="sources",
                    choices=["indeed", "linkedin"], help="restrict to one source (repeatable)")
    sr.add_argument("--rescore-only", action="store_true",
                    help="recompute keyword scores over stored postings; no scraping")
    sr.add_argument("--force", action="store_true",
                    help="ignore the sync lock")
    sr.set_defaults(func=_cmd_search)
    sc = ssub.add_parser("seed-cells", help="rebuild the scrape cell matrix from roles.yaml")
    sc.add_argument("--prune", action="store_true",
                    help="disable cells no longer implied by roles.yaml")
    sc.add_argument("--queries-per-family", type=int, default=1)
    sc.set_defaults(func=_cmd_seed_cells)

    # --- score -----------------------------------------------------------------------
    sco = sub.add_parser("score", help="score postings against the profile")
    scosub = sco.add_subparsers(dest="subcommand", required=True)
    scr = scosub.add_parser("run", help="drain unscored postings")
    scr.add_argument("--limit", type=int, help="cap the number of postings scored")
    scr.add_argument("--rescore-all", action="store_true",
                     help="re-score every posting, not just unscored ones")
    scr.add_argument("--dry-run", action="store_true",
                     help="estimate cost and exit without calling the model")
    scr.set_defaults(func=_cmd_score)

    # --- research --------------------------------------------------------------------
    r = sub.add_parser("research", help="build company dossiers for strong matches")
    rsub = r.add_subparsers(dest="subcommand", required=True)
    rr = rsub.add_parser("run", help="research companies behind high-scoring postings")
    rr.add_argument("--company", help="research one named company, ignoring the queue")
    rr.add_argument("--limit", type=int, help="cap the number of companies researched")
    rr.add_argument("--dry-run", action="store_true",
                    help="show what would be researched and exit")
    rr.set_defaults(func=_cmd_research)

    # --- db / web --------------------------------------------------------------------
    d = sub.add_parser("migrate", help="apply pending schema migrations")
    d.set_defaults(func=_cmd_migrate)

    w = sub.add_parser("web", help="serve the dashboard")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8010)
    w.add_argument("--reload", action="store_true", help="development autoreload")
    w.set_defaults(func=_cmd_web)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
