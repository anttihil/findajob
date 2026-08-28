"""Unit tests for Blind ATS Screener."""

from unittest.mock import MagicMock, patch

from careerradar.profile.models import (
    ATSScreeningVerdict,
    ResumeEducation,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)
from careerradar.profile.prompts import render_job_context, render_resume_plaintext
from careerradar.profile.screener import screen_resume


def test_render_resume_plaintext_and_job_context():
    job = {
        "title": "Staff Backend Engineer",
        "company": "Stripe",
        "location": "San Francisco, CA",
        "role_family": "backend",
        "seniority": "staff",
        "matched_skills": ["Python", "AWS", "FastAPI"],
        "description": "We are seeking a Staff Backend Engineer to lead high-throughput pipelines.",
    }
    job_ctx = render_job_context(job)
    assert "TARGET JOB: Staff Backend Engineer" in job_ctx
    assert "COMPANY: Stripe" in job_ctx
    assert "Python, AWS, FastAPI" in job_ctx

    payload = TailoredResumePayload(
        name="Jane Doe",
        contact_line_1="San Francisco, CA | jane@example.com",
        contact_line_2="github.com/janedoe",
        summary="Senior backend engineer.",
        experience=[
            ResumeRole(
                title="Lead Software Engineer",
                company="Acme Corp",
                dates="2020 - present",
                subsections=[
                    ResumeSubsection(
                        heading="Core payments service:",
                        bullets=["Engineered robust payment sync."],
                    )
                ],
            )
        ],
        skills=[ResumeSkillCategory(category="Backend", skills="Python, FastAPI")],
        education=[ResumeEducation(institution="State University", degree="BS")],
    )

    txt = render_resume_plaintext(payload)
    assert "JANE DOE" in txt
    assert "SUMMARY" in txt
    assert "EXPERIENCE" in txt
    assert "Lead Software Engineer, Acme Corp" in txt
    assert "Backend: Python, FastAPI" in txt
    assert "State University, BS" in txt


@patch("careerradar.profile.screener.invoke_structured")
@patch("careerradar.profile.screener.structured_model")
def test_screen_resume_mock(mock_model: MagicMock, mock_invoke: MagicMock):
    mock_invoke.return_value = ATSScreeningVerdict(
        passed=True,
        score=9,
        strengths=["Strong Python background", "Relevant cloud experience"],
        missing_signals=[],
        actionable_feedback="Great fit for role.",
    )

    job = {
        "title": "Backend Engineer",
        "company": "Acme",
        "description": "Python role",
    }
    payload = TailoredResumePayload(
        name="Jane Doe",
        contact_line_1="SF | jane@example.com",
        contact_line_2="github.com/janedoe",
        summary="Experienced Python developer.",
        experience=[],
        skills=[],
        education=[],
    )

    verdict = screen_resume(job, payload)
    assert verdict.passed is True
    assert verdict.score == 9
    assert "Strong Python background" in verdict.strengths

    # Verify context passed to model contains ONLY job and plaintext resume, zero master profile
    call_kwargs = mock_invoke.call_args.kwargs
    messages = call_kwargs["messages"]
    system_msg = messages[0][1]
    user_msg = messages[1][1]
    assert "Applicant Tracking System" in system_msg
    assert "=== TARGET JOB POSTING ===" in user_msg
    assert "=== SUBMITTED RESUME ===" in user_msg
