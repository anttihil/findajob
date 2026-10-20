"""LangGraph Actor-Critic StateGraph for 1-Page Tailored Resume Generation."""

import json
import os
import re
import sqlite3
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from findajob.core.llm import (
    get_model_for_role,
    invoke_structured,
    structured_model,
)
from findajob.core.logger import get_logger
from findajob.core.paths import generated_resumes_dir
from findajob.profile.layout_validator import validate_resume_layout
from findajob.profile.models import (
    ATSScreeningVerdict,
    LayoutValidationResult,
    Profile,
    TailoredResumePayload,
)
from findajob.profile.prompts import (
    GENERATOR_SYSTEM_PROMPT,
    build_generator_system,
    render_generator_user,
)
from findajob.profile.renderer import (
    compile_typst_to_pdf,
    render_typst,
    verify_page_count,
)
from findajob.profile.repository import load_profile, save_tailored_resume
from findajob.profile.screener import screen_resume
from findajob.profile.workspace import check_context_budget, review_resume

logger = get_logger()

MAX_ATTEMPTS = 2


class ResumeState(TypedDict, total=False):
    """Execution state for tailored resume generator graph."""

    job: dict[str, Any]
    master_profile: Profile | None
    attempts: int
    max_attempts: int
    model_name: str | None
    feedback: str | None
    resume_payload: TailoredResumePayload | None
    layout_result: LayoutValidationResult | None
    ats_verdict: ATSScreeningVerdict | None
    typst_path: str | None
    pdf_path: str | None
    saved_id: int | None
    error: str | None
    evidence_context: str
    memo_revision_id: int | None
    run_id: str
    connection: sqlite3.Connection
    inputs: dict[str, Any]
    draft_id: int | None
    best: dict[str, Any] | None
    previous_payload: TailoredResumePayload | None
    evidence_review: dict[str, Any]
    candidate_artifacts: dict[str, Any]


def node_prepare_context(state: ResumeState) -> dict[str, Any]:
    """Prepare candidate and job context for tailoring."""
    profile = state.get("master_profile")
    if not profile or not profile.name:
        profile = load_profile()
    return {
        "master_profile": profile,
        "attempts": state.get("attempts", 0),
        "max_attempts": state.get("max_attempts", MAX_ATTEMPTS),
        "model_name": state.get("model_name") or get_model_for_role("agent"),
    }


def node_generate(state: ResumeState) -> dict[str, Any]:
    """Invoke LLM to generate tailored resume payload using cached prefix architecture."""
    profile = state.get("master_profile")
    job = state.get("job") or {}
    feedback = state.get("feedback")
    attempts = state.get("attempts", 0) + 1
    model_name = state.get("model_name") or get_model_for_role("agent")

    if profile is None:
        return {"error": "Missing master profile ground truth", "attempts": attempts}

    system_prompt = build_generator_system(profile)
    if state.get("evidence_context"):
        system_prompt = GENERATOR_SYSTEM_PROMPT + "\n\n" + state.get("evidence_context", "")
    user_prompt = render_generator_user(job, feedback=feedback)
    previous = state.get("resume_payload") or state.get("previous_payload")
    if previous:
        user_prompt += "\n\n=== PREVIOUS DRAFT TO REVISE ===\n" + previous.model_dump_json()

    try:
        check_context_budget(system_prompt, user_prompt)
        model = structured_model(model_name, role="agent")
        payload = invoke_structured(
            model,
            TailoredResumePayload,
            [("system", system_prompt), ("user", user_prompt)],
            label=f"Resume generation for job {job.get('id', '')}",
        )
        result: dict[str, Any] = {
            "resume_payload": payload,
            "attempts": attempts,
            "error": None,
            "candidate_artifacts": {},
        }
        conn = state.get("connection")
        if conn is not None:
            pending_review = {
                "evidence": {
                    "grounded": False,
                    "questions": [],
                    "citations": [],
                    "revision_advice": "Review pending or interrupted.",
                },
                "recruiter": None,
            }
            cur = conn.execute(
                "INSERT INTO resume_drafts(job_id, run_id, attempt, memo_revision_id, "
                "inputs_json, payload_json, review_json, layout_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job["id"],
                    state.get("run_id"),
                    attempts,
                    state.get("memo_revision_id"),
                    json.dumps(state.get("inputs", {})),
                    payload.model_dump_json(),
                    json.dumps(pending_review),
                    "{}",
                ),
            )
            conn.execute(
                "UPDATE resume_workspaces SET updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE job_id = ?",
                (job["id"],),
            )
            conn.commit()
            result["draft_id"] = cur.lastrowid
        return result
    except Exception as exc:
        logger.exception("Failed generating tailored resume on attempt %d: %s", attempts, exc)
        return {"resume_payload": None, "attempts": attempts, "error": str(exc)}


def node_validate_layout(state: ResumeState) -> dict[str, Any]:
    """Validate 1-page constraints against typography layout engine."""
    payload = state.get("resume_payload")
    if payload is None:
        return {"layout_result": None}

    layout_res = validate_resume_layout(payload)
    if not layout_res.is_valid:
        violations_str = "\n- ".join(layout_res.violations)
        feedback = (
            f"LAYOUT OVERFLOW: The resume is {layout_res.estimated_points:.1f} pt "
            f"(ceiling is {layout_res.max_points:.1f} pt).\nViolations:\n- {violations_str}\n"
            f"Please shorten or condense bullets and summary so it fits strictly on 1 page."
        )
        logger.warning("Resume layout validation failed: %s", feedback)
        return {"layout_result": layout_res, "feedback": feedback}

    logger.info("Resume layout validation passed (estimated %s pt)", layout_res.estimated_points)
    return {"layout_result": layout_res, "feedback": None}


def node_screen_resume(state: ResumeState) -> dict[str, Any]:
    """Simulate external ATS screening on the rendered resume text."""
    job = state.get("job") or {}
    payload = state.get("resume_payload")
    if payload is None:
        return {"ats_verdict": None}

    ats_verdict = screen_resume(job, payload, model_name=get_model_for_role("scoring"))
    if state.get("connection") is not None:
        return _review_and_checkpoint(state, ats_verdict)
    if not ats_verdict.passed and ats_verdict.score < 9:
        missing = ", ".join(ats_verdict.missing_signals)
        feedback = (
            f"ATS SCREENER FEEDBACK (Score: {ats_verdict.score}/10):\n"
            f"Actionable advice: {ats_verdict.actionable_feedback}\n"
            f"Missing or under-emphasized signals from JD: {missing}\n"
            f"If the candidate has relevant experience in the master profile, emphasize it."
        )
        return {"ats_verdict": ats_verdict, "feedback": feedback}

    return {"ats_verdict": ats_verdict, "feedback": None}


def _review_and_checkpoint(state: ResumeState, verdict: ATSScreeningVerdict) -> dict[str, Any]:
    """Review, compile, and persist each candidate before choosing the best grounded draft."""
    payload = state.get("resume_payload")
    assert payload is not None
    review = review_resume(
        state.get("job", {}),
        payload,
        state.get("evidence_context", ""),
        recruiter=verdict,
    )
    layout = state.get("layout_result")
    assert layout is not None
    artifacts: dict[str, Any] = {}
    if review.grounded and layout.is_valid:
        artifacts = _compile_candidate(state)
        if not artifacts.get("pdf_path") or artifacts.get("pages") != 1:
            layout = layout.model_copy(
                update={
                    "is_valid": False,
                    "violations": [
                        *layout.violations,
                        "Rendered PDF must compile successfully and contain exactly one page.",
                    ],
                }
            )
    review_data = {"evidence": review.model_dump(), "recruiter": verdict.model_dump()}
    layout_data = {
        **layout.model_dump(),
        "page_count": artifacts.get("pages"),
        "pdf_verified": bool(artifacts.get("pdf_path") and artifacts.get("pages") == 1),
    }
    conn = state.get("connection")
    assert conn is not None
    conn.execute(
        "UPDATE resume_drafts SET review_json = ?, layout_json = ? WHERE id = ?",
        (json.dumps(review_data), json.dumps(layout_data), state.get("draft_id")),
    )
    conn.execute(
        "UPDATE resume_workspaces SET updated_at = "
        "strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE job_id = ?",
        (state.get("job", {}).get("id"),),
    )
    conn.commit()
    best = state.get("best")
    eligible = review.grounded and layout.is_valid and artifacts.get("pdf_path")
    improved = eligible and (not best or verdict.score > best["score"])
    if improved:
        best = {
            "draft_id": state.get("draft_id"),
            "payload": payload,
            "verdict": verdict,
            "score": verdict.score,
            "artifacts": artifacts,
            "review": review.model_dump(),
            "layout": layout,
        }
        conn.execute(
            "UPDATE resume_workspaces SET selected_draft_id = ? WHERE job_id = ?",
            (state.get("draft_id"), state.get("job", {}).get("id")),
        )
        conn.commit()
    feedback = "\n".join(
        filter(
            None,
            [
                review.revision_advice,
                "Unsupported: " + "; ".join(review.unsupported_claims)
                if review.unsupported_claims
                else "",
                "Conflicts: " + "; ".join(review.conflicts) if review.conflicts else "",
                "Layout: " + "; ".join(layout.violations) if not layout.is_valid else "",
                verdict.actionable_feedback,
            ],
        )
    )
    # A useful question pauses automatic revisions after a grounded, printable draft.
    needs_revision = not review.grounded or not layout.is_valid or review.next_action == "revise"
    can_improve = bool(improved or state.get("attempts", 0) == 1)
    retry = needs_revision
    if not can_improve and eligible:
        retry = False
    return {
        "ats_verdict": verdict,
        "evidence_review": {**review.model_dump(), "retry": retry},
        "layout_result": layout,
        "feedback": feedback,
        "best": best,
        "candidate_artifacts": artifacts,
    }


def _compile_candidate(state: ResumeState) -> dict[str, Any]:
    payload = state.get("resume_payload")
    assert payload is not None
    output_dir = generated_resumes_dir()
    filename = (
        f"resume_job_{state.get('job', {}).get('id')}_"
        f"{state.get('run_id')}_{state.get('attempts')}.typ"
    )
    typst_path = os.path.join(output_dir, filename)
    render_typst(payload, typst_path)
    pdf_path = compile_typst_to_pdf(typst_path, output_dir)
    pages = verify_page_count(pdf_path) if pdf_path else 0
    return {"typst_path": typst_path, "pdf_path": pdf_path, "pages": pages}


def node_render_artifacts(state: ResumeState) -> dict[str, Any]:
    """Render output Typst source and compile 1-page PDF."""
    if state.get("connection") is not None:
        best = state.get("best")
        if not best:
            return {
                "typst_path": None,
                "pdf_path": None,
                "error": "No grounded, one-page draft is ready to export. Review the workspace.",
            }
        return {
            "resume_payload": best["payload"],
            "ats_verdict": best["verdict"],
            "draft_id": best["draft_id"],
            "evidence_review": best["review"],
            "layout_result": best["layout"],
            "typst_path": best["artifacts"]["typst_path"],
            "pdf_path": best["artifacts"]["pdf_path"],
        }
    payload = state.get("resume_payload")
    job = state.get("job") or {}
    if payload is None:
        return {"typst_path": None, "pdf_path": None}

    job_id = job.get("id", 0)
    company = re.sub(r"[^a-zA-Z0-9_-]", "_", str(job.get("company", "company")).lower())[:20]
    title = re.sub(r"[^a-zA-Z0-9_-]", "_", str(job.get("title", "role")).lower())[:20]
    filename_base = f"resume_job_{job_id}_{company}_{title}"
    output_dir = generated_resumes_dir()
    typst_path = os.path.join(output_dir, f"{filename_base}.typ")

    # Render Typst source and compile directly to 1-page PDF
    render_typst(payload, typst_path)
    pdf_path = compile_typst_to_pdf(typst_path, output_dir)

    if pdf_path:
        pages = verify_page_count(pdf_path)
        logger.info("Rendered PDF page count: %d", pages)

    return {"typst_path": typst_path, "pdf_path": pdf_path}


def node_save_resume(state: ResumeState) -> dict[str, Any]:
    """Persist generated resume record in database."""
    payload = state.get("resume_payload")
    job = state.get("job") or {}
    typst_path = state.get("typst_path")
    if payload is None or not typst_path:
        return {"saved_id": None}

    ats_v = state.get("ats_verdict")
    resume_id = save_tailored_resume(
        job_id=int(job.get("id") or 0),
        model=str(state.get("model_name") or get_model_for_role("agent")),
        docx_path="",
        pdf_path=state.get("pdf_path"),
        resume=payload,
        summary=payload.summary,
        ats_score=ats_v.score if ats_v else None,
        ats_verdict="passed" if (ats_v and ats_v.passed) else "flagged",
        ats_feedback=ats_v.actionable_feedback if ats_v else None,
        typst_path=typst_path,
        conn=state.get("connection"),
    )
    if state.get("connection") is not None:
        conn = state.get("connection")
        assert conn is not None
        conn.execute(
            "UPDATE resume_drafts SET generated_resume_id = ? WHERE id = ?",
            (resume_id, state.get("draft_id")),
        )
        conn.execute(
            "UPDATE resume_workspaces SET selected_draft_id = ? WHERE job_id = ?",
            (state.get("draft_id"), job["id"]),
        )
        conn.commit()
    logger.info("Saved generated resume record id=%d for job id=%s", resume_id, job.get("id"))
    return {"saved_id": resume_id}


def _route_after_layout(state: ResumeState) -> str:
    if state.get("connection") is not None:
        return "screen_resume"
    layout_res = state.get("layout_result")
    if layout_res and not layout_res.is_valid:
        if state.get("attempts", 0) < state.get("max_attempts", MAX_ATTEMPTS):
            return "generate"
        # When layout retries are exhausted, proceed to screener so ATS score/feedback are captured
        return "screen_resume"
    return "screen_resume"


def _route_after_screener(state: ResumeState) -> str:
    if state.get("connection") is not None:
        if state.get("evidence_review", {}).get("retry") and state.get("attempts", 0) < state.get(
            "max_attempts", MAX_ATTEMPTS
        ):
            return "generate"
        return "render_artifacts"
    ats_v = state.get("ats_verdict")
    failed_screen = bool(ats_v and not ats_v.passed and ats_v.score < 9)
    can_retry = state.get("attempts", 0) < state.get("max_attempts", MAX_ATTEMPTS)
    if failed_screen and can_retry:
        return "generate"
    return "render_artifacts"


def build_resume_graph() -> CompiledStateGraph[Any, Any, Any, Any]:
    """Construct and compile the Resume Builder Actor-Critic LangGraph."""
    builder = StateGraph(ResumeState)

    builder.add_node("prepare_context", node_prepare_context)
    builder.add_node("generate", node_generate)
    builder.add_node("validate_layout", node_validate_layout)
    builder.add_node("screen_resume", node_screen_resume)
    builder.add_node("render_artifacts", node_render_artifacts)
    builder.add_node("save_resume", node_save_resume)

    builder.add_edge(START, "prepare_context")
    builder.add_edge("prepare_context", "generate")
    builder.add_conditional_edges(
        "generate",
        lambda state: "render_artifacts" if state.get("error") else "validate_layout",
        {"render_artifacts": "render_artifacts", "validate_layout": "validate_layout"},
    )

    builder.add_conditional_edges(
        "validate_layout",
        _route_after_layout,
        {
            "generate": "generate",
            "screen_resume": "screen_resume",
        },
    )

    builder.add_conditional_edges(
        "screen_resume",
        _route_after_screener,
        {"generate": "generate", "render_artifacts": "render_artifacts"},
    )

    builder.add_edge("render_artifacts", "save_resume")
    builder.add_edge("save_resume", END)

    return builder.compile()
