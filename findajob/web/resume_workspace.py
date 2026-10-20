"""HTTP interface for the career memo and durable per-job resume workspace."""

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from findajob.core.database import Database
from findajob.core.paths import generated_resumes_dir
from findajob.profile.models import TailoredResumePayload
from findajob.profile.repository import load_profile, save_tailored_resume
from findajob.profile.workspace import (
    WorkspaceConflict,
    get_workspace,
    memo_current,
    memo_save,
    save_answer,
)

router = APIRouter()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    db = Database()
    try:
        yield db.conn
        db.conn.commit()
    except WorkspaceConflict as exc:
        db.conn.rollback()
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        db.conn.rollback()
        raise HTTPException(400, str(exc)) from exc
    finally:
        db.close()


class MemoUpdate(BaseModel):
    markdown: str
    base_revision_id: int | None = None
    source: str = Field(default="manual edit", max_length=200)


@router.get("/api/career-memo")
def get_memo() -> dict[str, Any]:
    with connection() as conn:
        return memo_current(conn)


@router.put("/api/career-memo")
def update_memo(payload: MemoUpdate) -> dict[str, Any]:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return memo_save(conn, payload.markdown, payload.source, payload.base_revision_id)


@router.get("/api/career-memo/revisions")
def list_memo_revisions() -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT id, source, created_at FROM career_memo_revisions ORDER BY id DESC"
            ).fetchall()
        ]


@router.get("/api/career-memo/revisions/{revision_id}")
def get_memo_revision(revision_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            "SELECT * FROM career_memo_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Memo revision not found")
        return dict(row)


@router.get("/api/career-memo/download")
def download_memo() -> Response:
    with connection() as conn:
        return Response(
            memo_current(conn)["markdown"],
            media_type="text/markdown",
            headers={"Content-Disposition": 'attachment; filename="career-memo.md"'},
        )


@router.get("/api/resume-workspaces")
def list_workspaces() -> list[dict[str, Any]]:
    with connection() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT w.*, j.title, j.company FROM resume_workspaces w "
                "JOIN jobs j ON j.id = w.job_id ORDER BY w.updated_at DESC"
            ).fetchall()
        ]


@router.get("/api/jobs/{job_id}/resume/workspace")
def read_workspace(job_id: int) -> dict[str, Any]:
    with connection() as conn:
        if not conn.execute("SELECT id FROM jobs WHERE id = ?", (job_id,)).fetchone():
            raise HTTPException(404, "Job not found")
        result = get_workspace(conn, job_id)
        result["memo_revision_id"] = memo_current(conn)["id"]
        return result


class InterviewAnswer(BaseModel):
    draft_id: int
    question: str
    answer: str = Field(min_length=1)
    base_revision_id: int | None = None


@router.post("/api/jobs/{job_id}/resume/answers")
def answer_interview(job_id: int, payload: InterviewAnswer) -> dict[str, Any]:
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        memo = save_answer(
            conn,
            job_id,
            payload.draft_id,
            payload.question,
            payload.answer,
            payload.base_revision_id,
        )
        return {"memo_revision_id": memo["id"], "workspace": get_workspace(conn, job_id)}


@router.get("/api/jobs/{job_id}/resume/drafts/{draft_id}/sources")
def draft_sources(job_id: int, draft_id: int) -> dict[str, Any]:
    from findajob.profile.models import Profile
    from findajob.profile.workspace import evidence_context

    with connection() as conn:
        row = conn.execute(
            "SELECT inputs_json FROM resume_drafts WHERE job_id = ? AND id = ?",
            (job_id, draft_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Draft not found")
        inputs = json.loads(row["inputs_json"])
        return {
            "inputs": inputs,
            "evidence": evidence_context(
                Profile.model_validate(inputs["profile"]), inputs["memo"]["markdown"]
            ),
        }


@router.post("/api/jobs/{job_id}/resume/recover")
def recover_workspace(job_id: int) -> dict[str, Any]:
    """Release an abandoned run after its one-hour lease has expired."""
    with connection() as conn:
        result = conn.execute(
            "UPDATE resume_workspaces SET status = 'needs_attention', "
            "error = 'Previous generation was interrupted; saved drafts are available.' "
            "WHERE job_id = ? AND status = 'running' "
            "AND unixepoch(updated_at) < unixepoch('now', '-1 hour')",
            (job_id,),
        )
        if not result.rowcount:
            raise WorkspaceConflict(
                "No expired generation to recover. Active runs have a one-hour lease."
            )
        return get_workspace(conn, job_id)


@router.post("/api/jobs/{job_id}/resume/drafts/{draft_id}/select")
def select_draft(job_id: int, draft_id: int) -> dict[str, Any]:
    from findajob.profile.renderer import compile_typst_to_pdf, render_typst, verify_page_count

    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        workspace = get_workspace(conn, job_id)
        if workspace["status"] == "running":
            raise WorkspaceConflict("Wait for generation to finish before selecting a draft.")
        draft = next((d for d in workspace["drafts"] if d["id"] == draft_id), None)
        if not draft:
            raise HTTPException(404, "Draft not found")
        if not draft["review"]["evidence"]["grounded"] or not draft["layout"].get("is_valid"):
            raise ValueError("Only grounded, one-page drafts can be selected for export.")
        inputs = json.loads(
            conn.execute(
                "SELECT inputs_json FROM resume_drafts WHERE id = ?", (draft_id,)
            ).fetchone()["inputs_json"]
        )
        if (
            draft["memo_revision_id"] != memo_current(conn)["id"]
            or inputs["profile"] != load_profile(conn).model_dump()
        ):
            raise WorkspaceConflict(
                "The source evidence changed. Generate a reviewed revision first."
            )
        payload = TailoredResumePayload.model_validate(draft["payload"])
        output_dir = generated_resumes_dir()
        path = os.path.join(output_dir, f"resume_draft_{draft_id}_{uuid4().hex}.typ")
        render_typst(payload, path)
        pdf = compile_typst_to_pdf(path, output_dir)
        if not pdf or verify_page_count(pdf) != 1:
            raise ValueError("Draft did not compile to exactly one page.")
        recruiter = draft["review"]["recruiter"]
        record_id = save_tailored_resume(
            job_id,
            inputs["model"],
            resume=payload,
            summary=payload.summary,
            pdf_path=pdf,
            typst_path=path,
            ats_score=recruiter["score"],
            ats_verdict="passed" if recruiter["passed"] else "flagged",
            ats_feedback=recruiter["actionable_feedback"],
            conn=conn,
        )
        conn.execute(
            "UPDATE resume_drafts SET generated_resume_id = ? WHERE id = ?", (record_id, draft_id)
        )
        conn.execute(
            "UPDATE resume_workspaces SET selected_draft_id = ? WHERE job_id = ?",
            (draft_id, job_id),
        )
        return get_workspace(conn, job_id)
