"""FastAPI application for fast, blinded local verdict labeling."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from findajob.labeler.database import LabelerDatabase, default_labeler_db_path

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
