"""Unit tests for LangGraph Resume Builder Actor-Critic Pipeline."""

import sqlite3
from unittest.mock import MagicMock, patch

from careerradar.core.migrations import migrate
from careerradar.profile.graph import (
    ResumeState,
    _route_after_layout,
    _route_after_screener,
    build_resume_graph,
)
from careerradar.profile.models import (
    ATSScreeningVerdict,
    LayoutValidationResult,
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    ResumeEducation,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)
from careerradar.profile.prompts import (
    GENERATOR_SYSTEM_PROMPT,
    build_generator_system,
    render_generator_user,
)


def test_generator_system_prefix_caching():
    profile = Profile(
        name="Alex River",
        email="alex@example.com",
        location="Austin, TX",
        executive_summary="Staff Infrastructure Engineer.",
        skills=[MasterSkillCategory(category="Cloud", skills=["AWS", "Kubernetes"])],
        experience=[
            MasterRole(
                title="Staff Engineer",
                company="CloudScale",
                dates="2022 - Present",
                projects=[
                    MasterProject(
                        heading="Scaled streaming platform",
                        bullets=["Handled 50k events/sec."],
                    )
                ],
            )
        ],
        education=[MasterEducation(institution="UT Austin", degree="BS CS")],
    )

    sys1 = build_generator_system(profile)
    sys2 = build_generator_system(profile)

    # 1. Byte-identical prefix
    assert sys1 == sys2

    # 2. Contains generator rules and profile ground truth
    assert GENERATOR_SYSTEM_PROMPT in sys1
    assert "Alex River" in sys1
    assert "Staff Infrastructure Engineer." in sys1
    assert "Scaled streaming platform" in sys1
    assert "Cloud: AWS, Kubernetes" in sys1

    # 3. Contains no posting leak
    assert "TARGET JOB" not in sys1
    assert "Anthropic" not in sys1

    # 4. User prompt carries volatile posting info
    job = {"id": 1, "title": "Platform Lead", "company": "Anthropic", "description": "LLMs."}
    user_prompt = render_generator_user(job, feedback="Shorten summary")
    assert "Platform Lead" in user_prompt
    assert "Anthropic" in user_prompt
    assert "Shorten summary" in user_prompt


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


@patch("careerradar.profile.graph.structured_model")
@patch("careerradar.profile.graph.render_docx")
@patch("careerradar.profile.graph.convert_to_pdf")
@patch("careerradar.profile.graph.screen_resume")
@patch("careerradar.profile.graph.invoke_structured")
@patch("careerradar.profile.graph.save_tailored_resume")
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
        contact_line_1="San Francisco, CA | jane@example.com",
        contact_line_2="github.com/janedoe",
        summary="Senior full-stack engineer.",
        experience=[
            ResumeRole(
                title="Lead Software Engineer",
                company="Acme Platform",
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
        education=[ResumeEducation(institution="State University", degree="BS")],
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
        "master_profile": Profile(name="Jane Doe"),
    }

    final_state = graph.invoke(initial_state)
    assert final_state["saved_id"] == 101
    assert final_state["docx_path"] is not None
    assert final_state["pdf_path"] == "/tmp/test_resume.pdf"
    assert final_state["ats_verdict"].passed is True
    conn.close()
