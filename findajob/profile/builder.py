"""Facade runner for generating tailored resumes."""

import json
from typing import Any
from uuid import uuid4

from findajob.core.database import Database
from findajob.core.llm import get_model_for_role
from findajob.core.logger import get_logger
from findajob.profile.graph import ResumeState, build_resume_graph
from findajob.profile.models import Profile, TailoredResumePayload
from findajob.profile.prompts import render_resume_plaintext
from findajob.profile.repository import get_resume_by_id, load_profile
from findajob.profile.workspace import (
    check_context_budget,
    claim_workspace,
    evidence_context,
    get_workspace,
    memo_current,
)

logger = get_logger()

__all__ = ["build_resume_for_job", "render_resume_plaintext"]


def build_resume_for_job(
    job_id: int,
    model: str | None = None,
    master_profile: Profile | None = None,
    db: Database | None = None,
) -> dict[str, Any]:
    """Run the Actor-Critic Resume Builder graph for a single job."""
    owned = db is None
    database = db or Database()
    claimed = False
    try:
        job_res = database.query_jobs(job_id=job_id, detail=True)
        jobs = job_res.get("jobs") or []
        if not jobs:
            raise ValueError(f"Job with id {job_id} not found.")
        job = jobs[0]

        profile = master_profile or load_profile(database.conn)
        memo = memo_current(database.conn)
        evidence = evidence_context(profile, memo["markdown"])
        check_context_budget(evidence, str(job.get("description", "")))
        workspace = get_workspace(database.conn, job_id)
        previous = next(
            (d for d in workspace["drafts"] if d["id"] == workspace["selected_draft_id"]), None
        )
        if previous is None and workspace["drafts"]:
            previous = workspace["drafts"][0]
        claim_workspace(database.conn, job_id)
        database.conn.commit()
        claimed = True
        graph = build_resume_graph()
        resolved_model = model or get_model_for_role("agent")

        initial_state: ResumeState = {
            "job": job,
            "master_profile": profile,
            "model_name": resolved_model,
            "connection": database.conn,
            "run_id": uuid4().hex,
            "evidence_context": evidence,
            "memo_revision_id": memo["id"],
            "inputs": {
                "profile": profile.model_dump(),
                "memo": memo,
                "job": job,
                "model": resolved_model,
            },
            "previous_payload": (
                TailoredResumePayload.model_validate(previous["payload"]) if previous else None
            ),
            "feedback": (
                json.dumps({"review": previous["review"], "layout": previous["layout"]})
                if previous
                else None
            ),
        }

        logger.info(
            "Starting resume build for job %d (%s at %s)",
            job_id,
            job.get("title"),
            job.get("company"),
        )
        final_state: dict[str, Any] = graph.invoke(initial_state)
        review = final_state.get("evidence_review", {})
        status = (
            "needs_attention"
            if final_state.get("error")
            else ("interview" if review.get("questions") else "complete")
        )
        database.conn.execute(
            "UPDATE resume_workspaces SET status = ?, error = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE job_id = ?",
            (status, final_state.get("error"), job_id),
        )
        database.conn.commit()

        saved_id = final_state.get("saved_id")
        if saved_id:
            record = get_resume_by_id(saved_id, database.conn)
            if record:
                record["workspace"] = get_workspace(database.conn, job_id)
                return record

        return {
            "job_id": job_id,
            "typst_path": final_state.get("typst_path"),
            "pdf_path": final_state.get("pdf_path"),
            "resume": final_state.get("resume_payload"),
            "ats_verdict": final_state.get("ats_verdict"),
            "workspace": get_workspace(database.conn, job_id),
        }
    except Exception as exc:
        database.conn.rollback()
        if claimed:
            database.conn.execute(
                "UPDATE resume_workspaces SET status = 'needs_attention', error = ? "
                "WHERE job_id = ?",
                (str(exc), job_id),
            )
            database.conn.commit()
        raise
    finally:
        if owned:
            database.close()
