"""LangGraph Actor-Critic StateGraph for 1-Page Tailored Resume Generation."""

import os
import re
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
    user_prompt = render_generator_user(job, feedback=feedback)

    try:
        model = structured_model(model_name, role="agent")
        payload = invoke_structured(
            model,
            TailoredResumePayload,
            [("system", system_prompt), ("user", user_prompt)],
            label=f"Resume generation for job {job.get('id', '')}",
        )
        return {"resume_payload": payload, "attempts": attempts, "error": None}
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


def node_render_artifacts(state: ResumeState) -> dict[str, Any]:
    """Render output Typst source and compile 1-page PDF."""
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
    )
    logger.info("Saved generated resume record id=%d for job id=%s", resume_id, job.get("id"))
    return {"saved_id": resume_id}


def _route_after_layout(state: ResumeState) -> str:
    layout_res = state.get("layout_result")
    if layout_res and not layout_res.is_valid:
        if state.get("attempts", 0) < state.get("max_attempts", MAX_ATTEMPTS):
            return "generate"
        # When layout retries are exhausted, proceed to screener so ATS score/feedback are captured
        return "screen_resume"
    return "screen_resume"


def _route_after_screener(state: ResumeState) -> str:
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
    builder.add_edge("generate", "validate_layout")

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
