"""FastAPI application for fast, blinded local verdict labeling."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from findajob.labeler.database import LabelerDatabase, default_labeler_db_path
from findajob.labeler.evaluation import (
    DEFAULT_CANDIDATE_RULES,
    EvaluationRunner,
    evaluation_defaults,
)
from findajob.labeler.exporter import export_dataset

STATIC_DIR = Path(__file__).with_name("static")


class LabelPayload(BaseModel):
    outcome: str
    decisive_reason: str | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    notes: str = Field(default="", max_length=5000)
    review_round: int = Field(default=1, ge=1)


class AnnotationPatch(BaseModel):
    decisive_reason: str | None = None
    confidence: int | None = Field(default=None, ge=1, le=5)
    notes: str = Field(default="", max_length=5000)


class SplitPayload(BaseModel):
    split: str


class ConsistencyResolutionPayload(BaseModel):
    selected_round: int = Field(ge=1, le=2)
    notes: str = Field(default="", max_length=5000)


class ExportPayload(BaseModel):
    version: str = Field(min_length=1, max_length=80)


class EvaluationPayload(BaseModel):
    version: str
    split: str = "validation"
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=100)
    candidate_rules: str = Field(default=DEFAULT_CANDIDATE_RULES, max_length=20000)
    profile_boundary: str = Field(default="", max_length=10000)


def _public_example(row: dict[str, Any]) -> dict[str, Any]:
    """Return review evidence, withholding the prediction until this round is labeled."""
    result: dict[str, Any] = {
        "id": row["id"],
        "review_round": row["review_round"],
        "position": row["position"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "url": row["url"],
        "date_posted": row["date_posted"],
        "description": row["description"],
        "profile_text": row["profile_text"],
        "split": row["split"],
        "annotation": None,
        "model_verdict": None,
    }
    if row.get("annotation_id") is not None:
        result["annotation"] = {
            "id": row["annotation_id"],
            "outcome": row["outcome"],
            "decisive_reason": row["decisive_reason"],
            "confidence": row["confidence"],
            "notes": row["notes"],
            "agreement": row["agreement"],
            "model_revealed_at": row["model_revealed_at"],
        }
        result["model_verdict"] = {
            "outcome": row["model_outcome"],
            "reason_type": row["model_reason_type"],
            "reason_description": row["model_reason_description"],
            "model": row["model"],
        }
    return result


def create_app(labeler_db_path: str | None = None) -> FastAPI:
    db_path = labeler_db_path or default_labeler_db_path()
    with LabelerDatabase(db_path):
        pass

    app = FastAPI(title="CareerRadar Verdict Labeler", docs_url=None, redoc_url=None)
    app.state.labeler_db_path = db_path
    export_root = Path(db_path).resolve().parent / "labeler_exports"
    runner = EvaluationRunner(str(Path(db_path).resolve().parent / "labeler_evaluations"))
    app.state.evaluation_runner = runner
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="labeler-assets")

    @app.middleware("http")
    async def private_data_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/api/queues")
    async def queues() -> dict[str, Any]:
        with LabelerDatabase(db_path) as db:
            return {"queues": db.queue_summaries()}

    @app.get("/api/evaluations")
    def evaluations() -> dict[str, Any]:
        with LabelerDatabase(db_path) as db:
            exports = [
                dict(row)
                for row in db.conn.execute(
                    "SELECT version, output_path, created_at, example_count, manifest_json "
                    "FROM export_runs ORDER BY created_at DESC"
                ).fetchall()
            ]

        for item in exports:
            item["manifest"] = json.loads(item.pop("manifest_json"))
        defaults = evaluation_defaults()
        return {
            "exports": exports,
            "runs": runner.list_runs(),
            "default_candidate_rules": DEFAULT_CANDIDATE_RULES,
            "default_model": defaults["model"],
            "default_provider": defaults["provider"],
        }

    @app.post("/api/exports", status_code=201)
    def freeze_export(payload: ExportPayload) -> dict[str, Any]:
        try:
            with LabelerDatabase(db_path) as db:
                if db.consistency_report()["unresolved"]:
                    raise ValueError("Resolve label disagreements before freezing a dataset.")
            return export_dataset(
                labeler_db_path=db_path, output_dir=str(export_root), version=payload.version
            )
        except (ValueError, FileExistsError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/evaluations", status_code=202)
    def start_evaluation(payload: EvaluationPayload) -> dict[str, Any]:
        with LabelerDatabase(db_path) as db:
            export = db.conn.execute(
                "SELECT output_path FROM export_runs WHERE version = ?", (payload.version,)
            ).fetchone()
        if export is None:
            raise HTTPException(status_code=404, detail="Export version not found.")
        try:
            values = payload.model_dump(exclude={"version"})
            run = runner.start(dataset_path=export["output_path"], **values)
            return {"id": run["id"]}
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/evaluations/{run_id}")
    def evaluation(run_id: str) -> dict[str, Any]:
        try:
            return runner.get(run_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="Evaluation run not found.") from exc

    @app.get("/api/consistency")
    async def consistency() -> dict[str, Any]:
        with LabelerDatabase(db_path) as db:
            return db.consistency_report()

    @app.get("/api/next")
    async def next_example(
        queue: str = Query(default="initial", min_length=1, max_length=100),
    ) -> Any:
        with LabelerDatabase(db_path) as db:
            row = db.next_example(queue)
            return {"example": _public_example(row) if row else None}

    @app.put("/api/examples/{example_id}/resolution")
    async def resolve_consistency(
        example_id: int, payload: ConsistencyResolutionPayload
    ) -> dict[str, Any]:
        try:
            with LabelerDatabase(db_path) as db:
                resolution = db.resolve_consistency(
                    example_id, payload.selected_round, payload.notes
                )
                return {"resolution": resolution}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/examples/{example_id}")
    async def example(
        example_id: int,
        queue: str = Query(default="initial", min_length=1, max_length=100),
    ) -> Any:
        with LabelerDatabase(db_path) as db:
            row = db.get_queue_example(queue, example_id)
            if row is None:
                raise HTTPException(status_code=404, detail="Example not found in this queue.")
            return {"example": _public_example(row)}

    @app.post("/api/examples/{example_id}/label")
    async def label(example_id: int, payload: LabelPayload) -> Any:
        try:
            with LabelerDatabase(db_path) as db:
                annotation = db.annotate(
                    example_id=example_id,
                    review_round=payload.review_round,
                    outcome=payload.outcome,
                    decisive_reason=payload.decisive_reason,
                    confidence=payload.confidence,
                    notes=payload.notes,
                )
                model = db.conn.execute(
                    """
                    SELECT model_outcome AS outcome, model_reason_type AS reason_type,
                           model_reason_description AS reason_description, model
                      FROM examples WHERE id = ?
                    """,
                    (example_id,),
                ).fetchone()
                assert model is not None
                return {"annotation": annotation, "model_verdict": dict(model)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Example not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.patch("/api/annotations/{annotation_id}")
    async def patch_annotation(annotation_id: int, payload: AnnotationPatch) -> Any:
        try:
            with LabelerDatabase(db_path) as db:
                annotation = db.update_annotation(
                    annotation_id,
                    decisive_reason=payload.decisive_reason,
                    confidence=payload.confidence,
                    notes=payload.notes,
                )
                return {"annotation": annotation}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Annotation not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/examples/{example_id}/split")
    async def set_split(example_id: int, payload: SplitPayload) -> Any:
        try:
            with LabelerDatabase(db_path) as db:
                row = db.conn.execute(
                    "SELECT family_key FROM examples WHERE id = ?", (example_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(example_id)
                db.set_split(str(row["family_key"]), payload.split)
                affected = db.conn.execute(
                    "SELECT COUNT(*) FROM examples WHERE family_key = ?", (row["family_key"],)
                ).fetchone()[0]
                return {"split": payload.split, "family_examples": affected}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Example not found.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app
