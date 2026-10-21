"""Command-line entry point for the standalone verdict labeler."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from findajob.core.paths import DATA_DIR, DB_PATH
from findajob.labeler.database import LabelerDatabase, default_labeler_db_path
from findajob.labeler.exporter import export_dataset
from findajob.labeler.importer import import_examples


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="findajob-labeler",
        description="Private, local workbench for blinded CareerRadar verdict labels.",
    )
    parser.add_argument("--label-db", default=default_labeler_db_path())
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="initialize the standalone labeler database")

    imp = commands.add_parser("import", help="snapshot a sampled audit queue from jobs.db")
    imp.add_argument("--source-db", default=DB_PATH)
    imp.add_argument("--count", type=int, default=75)
    imp.add_argument("--seed", type=int, default=0)
    imp.add_argument("--queue", default="initial")
    imp.add_argument("--job-id", type=int, action="append", dest="job_ids")

    serve = commands.add_parser("serve", help="run the local labeling web application")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8020)

    relabel = commands.add_parser("relabel", help="create a blinded consistency queue")
    relabel.add_argument("--percent", type=int, default=10)
    relabel.add_argument("--seed", type=int, default=0)
    relabel.add_argument("--queue")

    export = commands.add_parser("export", help="write a new versioned JSONL dataset")
    export.add_argument("--output-dir", default=os.path.join(DATA_DIR, "labeler_exports"))
    export.add_argument("--version")

    evaluate = commands.add_parser(
        "evaluate", help="compare baseline and candidate on frozen labels"
    )
    evaluate.add_argument("--dataset", required=True, help="export version directory")
    evaluate.add_argument(
        "--split", choices=["optimization", "validation", "held_out", "all"], default="validation"
    )
    evaluate.add_argument("--model", default="", help="override LLM_SCORING_MODEL from .env")
    evaluate.add_argument("--provider", default="", help="override LLM_PROVIDER from .env")
    evaluate.add_argument("--candidate-rules-file", help="UTF-8 instructions appended to candidate")
    evaluate.add_argument("--profile-boundary-file", help="UTF-8 candidate profile clarification")
    evaluate.add_argument("--fit-threshold", type=int, default=70, help=argparse.SUPPRESS)
    evaluate.add_argument(
        "--output-dir", help="default: labeler_evaluations beside labeler database"
    )

    commands.add_parser("status", help="show queue progress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        with LabelerDatabase(args.label_db):
            pass
        print(f"Initialized labeler database at {os.path.abspath(args.label_db)}")
        return 0
    if args.command == "import":
        result = import_examples(
            source_db_path=args.source_db,
            labeler_db_path=args.label_db,
            count=args.count,
            seed=args.seed,
            job_ids=args.job_ids,
            queue_name=args.queue,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "serve":
        import uvicorn

        from findajob.labeler.app import create_app

        uvicorn.run(create_app(args.label_db), host=args.host, port=args.port)
        return 0
    if args.command == "relabel":
        queue = args.queue or f"relabel-{args.percent}pct-seed{args.seed}"
        with LabelerDatabase(args.label_db) as db:
            count = db.create_relabel_queue(queue, percent=args.percent, seed=args.seed)
        print(json.dumps({"queue": queue, "examples": count}, indent=2))
        return 0
    if args.command == "export":
        result = export_dataset(
            labeler_db_path=args.label_db,
            output_dir=args.output_dir,
            version=args.version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "status":
        with LabelerDatabase(args.label_db) as db:
            result: dict[str, Any] = {"database": db.path, "queues": db.queue_summaries()}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.command == "evaluate":
        from findajob.labeler.evaluation import DEFAULT_CANDIDATE_RULES, EvaluationRunner

        runner = EvaluationRunner(
            args.output_dir or str(Path(args.label_db).resolve().parent / "labeler_evaluations")
        )
        try:
            run = runner.create(
                dataset_path=args.dataset,
                split=args.split,
                model=args.model,
                provider=args.provider,
                candidate_rules=Path(args.candidate_rules_file).read_text(encoding="utf-8")
                if args.candidate_rules_file
                else DEFAULT_CANDIDATE_RULES,
                profile_boundary=Path(args.profile_boundary_file).read_text(encoding="utf-8")
                if args.profile_boundary_file
                else "",
                fit_threshold=args.fit_threshold,
            )
            run = runner.execute(run["id"])
        except (ValueError, OSError) as exc:
            print(str(exc))
            return 1
        print(
            json.dumps(
                {
                    "id": run["id"],
                    "status": run["status"],
                    "summary": run["summary"],
                    "output_path": str(runner.root / f"{run['id']}.json"),
                },
                indent=2,
            )
        )
        return 0 if run["status"] == "completed" else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
