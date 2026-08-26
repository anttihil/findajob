"""Unit tests for LangGraph Resume Builder Actor-Critic Pipeline."""

import sqlite3
from unittest.mock import MagicMock, patch

from careerradar.core.migrations import migrate
from careerradar.resumes.graph import (
    ResumeState,
    _route_after_layout,
    _route_after_screener,
    build_resume_graph,
)
from careerradar.resumes.models import (
    ATSScreeningVerdict,
    LayoutValidationResult,
    ResumeEducation,
    ResumeMasterProfile,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)


def test_graph_routing_helpers():
    # Layout valid -> screen_resume
    state_valid: ResumeState = {
        "layout_result": LayoutValidationResult(
            is_valid=True, estimated_points=600.0, violations=[]
        )
    }
    assert _route_after_layout(state_valid) == "screen_resume"

    # Layout invalid & attempts < max -> retry generate
    state_overflow: ResumeState = {
        "layout_result": LayoutValidationResult(
            is_valid=False, estimated_points=750.0, violations=["Too long"]
        ),
        "attempts": 1,
        "max_attempts": 3,
    }
    assert _route_after_layout(state_overflow) == "generate"

    # ATS passed -> render_artifacts
    state_ats_passed: ResumeState = {
        "ats_verdict": ATSScreeningVerdict(
            passed=True,
            score=9,
            strengths=[],
            missing_signals=[],
            actionable_feedback="",
        )
    }
    assert _route_after_screener(state_ats_passed) == "render_artifacts"

    # ATS failed & attempts < max -> retry generate
    state_ats_failed: ResumeState = {
        "ats_verdict": ATSScreeningVerdict(
            passed=False,
            score=7,
            strengths=[],
            missing_signals=["AWS"],
            actionable_feedback="Need AWS",
        ),
        "attempts": 1,
        "max_attempts": 3,
    }
    assert _route_after_screener(state_ats_failed) == "generate"


@patch("careerradar.resumes.graph.structured_model")
@patch("careerradar.resumes.graph.render_docx")
@patch("careerradar.resumes.graph.convert_to_pdf")
@patch("careerradar.resumes.graph.screen_resume")
@patch("careerradar.resumes.graph.invoke_structured")
@patch("careerradar.resumes.graph.save_generated_resume")
def test_full_graph_execution(
    mock_save: MagicMock,
    mock_invoke: MagicMock,
    mock_screen: MagicMock,
    mock_pdf: MagicMock,
    mock_docx: MagicMock,
    mock_model: MagicMock,
):
    mock_model.return_value = MagicMock()
    mock_docx.return_value = "/tmp/test_resume.docx"
    mock_pdf.return_value = "/tmp/test_resume.pdf"
    mock_save.return_value = 101

    payload = TailoredResumePayload(
        name="Jane Doe",
        contact_line_1="LA, CA | test@test.com",
        contact_line_2="github.com/test",
        summary="Senior full-stack engineer.",
        experience=[
            ResumeRole(
                title="Lead Software Engineer",
                company="UCLA",
                dates="2020 - Present",
                subsections=[
                    ResumeSubsection(
                        heading="Core Infrastructure:",
                        bullets=["Engineered scalable cloud services."],
                    )
                ],
            )
        ],
        skills=[ResumeSkillCategory(category="Cloud", skills="AWS, Docker")],
        education=[ResumeEducation(institution="UCLA", degree="PhD")],
    )

    verdict = ATSScreeningVerdict(
        passed=True,
        score=10,
        strengths=["Great fit"],
        missing_signals=[],
        actionable_feedback="",
    )

    mock_invoke.return_value = payload
    mock_screen.return_value = verdict

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    graph = build_resume_graph()
    initial_state: ResumeState = {
        "job": {
            "id": 42,
            "title": "Software Engineer",
            "company": "Anthropic",
            "description": "Build LLM applications.",
        },
        "master_profile": ResumeMasterProfile(name="Jane Doe"),
    }

    final_state = graph.invoke(initial_state)
    assert final_state["saved_id"] == 101
    assert final_state["docx_path"] is not None
    assert final_state["pdf_path"] == "/tmp/test_resume.pdf"
    assert final_state["ats_verdict"].passed is True
    conn.close()
