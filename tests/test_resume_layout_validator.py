"""Unit tests for the 1-page layout and point budget validator."""

from careerradar.profile.layout_validator import (
    calculate_resume_points,
    estimate_visual_lines,
    validate_resume_layout,
)
from careerradar.profile.models import (
    ResumeEducation,
    ResumeRole,
    ResumeSkillCategory,
    ResumeSubsection,
    TailoredResumePayload,
)


def test_estimate_visual_lines():
    assert estimate_visual_lines("", 80) == 0
    assert estimate_visual_lines("Hello world", 80) == 1
    assert estimate_visual_lines("A" * 90, 80) == 2
    assert estimate_visual_lines("A" * 165, 80) == 3


def test_valid_1page_resume():
    payload = TailoredResumePayload(
        name="Jane Doe",
        contact_line_1="San Francisco Bay Area | jane.doe@example.com | 555-019-2834",
        contact_line_2="github.com/janedoe | linkedin.com/in/janedoe",
        summary=(
            "Senior full-stack & systems engineer with deep expertise in Python and cloud "
            "architectures. Track record designing resilient AI workflows and developer tools."
        ),
        experience=[
            ResumeRole(
                title="Lead Software Engineer",
                company="Acme Platform Corp",
                dates="Jan 2020 - present",
                subsections=[
                    ResumeSubsection(
                        heading="Led modernization of core web systems and microservices:",
                        bullets=[
                            "Engineered scalable containerized services for 50k+ active users.",
                            "Implemented automated CI/CD pipelines reducing deployment friction.",
                        ],
                    )
                ],
            ),
            ResumeRole(
                title="Senior Software Engineer",
                company="Beta Robotics Inc",
                dates="Jan 2018 - Dec 2019",
                subsections=[
                    ResumeSubsection(
                        heading="Autonomous robotics fleet teleoperation UI:",
                        bullets=[
                            "Built high-throughput robot telemetry dashboard in React.",
                            "Optimized canvas rendering pipeline achieving steady 60 FPS.",
                        ],
                    )
                ],
            ),
        ],
        skills=[
            ResumeSkillCategory(
                category="Infrastructure",
                skills="AWS, Docker, Kubernetes, Terraform, Linux, CI/CD",
            ),
            ResumeSkillCategory(
                category="Languages & Frameworks",
                skills="Python, TypeScript, FastAPI, React, Node.js, PostgreSQL",
            ),
        ],
        education=[
            ResumeEducation(
                institution="State University",
                degree="BS in Computer Science",
            )
        ],
    )

    pts, violations = calculate_resume_points(payload)
    assert pts < 670.0
    assert len(violations) == 0

    res = validate_resume_layout(payload)
    assert res.is_valid is True
    assert res.estimated_points < res.max_points


def test_overflow_detection():
    # Construct a massive payload that overflows 1 page
    payload = TailoredResumePayload(
        name="Overflow Candidate",
        contact_line_1="Location | Email | Phone",
        contact_line_2="GitHub | LinkedIn | Website",
        summary="Extremely verbose summary " * 20,
        experience=[
            ResumeRole(
                title=f"Role {i}",
                company=f"Company {i}",
                dates="2020 - 2024",
                subsections=[
                    ResumeSubsection(
                        heading=f"Scope {i}",
                        bullets=[
                            f"Massive bullet point describing work {i} " * 4 for _ in range(4)
                        ],
                    )
                ],
            )
            for i in range(5)
        ],
        skills=[
            ResumeSkillCategory(category=f"Category {i}", skills="Skill A, Skill B, Skill C")
            for i in range(7)
        ],
        education=[ResumeEducation(institution=f"Uni {i}", degree="BS CS") for i in range(3)],
    )

    res = validate_resume_layout(payload)
    assert res.is_valid is False
    assert len(res.violations) > 0
    assert res.estimated_points > 670.0


def test_missing_bullets_detection():
    # Construct a payload with an empty experience section
    payload = TailoredResumePayload(
        name="No Bullets Candidate",
        contact_line_1="Location | Email | Phone",
        contact_line_2="GitHub | LinkedIn",
        summary="Concise summary for testing.",
        experience=[],
        skills=[ResumeSkillCategory(category="Languages", skills="Python")],
        education=[ResumeEducation(institution="University", degree="BS")],
    )

    res = validate_resume_layout(payload)
    assert res.is_valid is False
    assert any("Experience section is empty" in v for v in res.violations)


def test_resume_role_bullets_coercion():
    # Coerce flat bullets into subsections
    raw_data = {
        "title": "Software Engineer",
        "company": "Acme Corp",
        "dates": "2020 - Present",
        "bullets": ["Engineered core infrastructure.", "Optimized database queries."],
    }
    role = ResumeRole.model_validate(raw_data)
    assert len(role.subsections) == 1
    assert role.subsections[0].heading is None
    assert len(role.subsections[0].bullets) == 2

    # Coerce string subsections into ResumeSubsection
    raw_data_strings = {
        "title": "Software Engineer",
        "company": "Acme Corp",
        "dates": "2020 - Present",
        "subsections": ["First bullet point.", "Second bullet point."],
    }
    role2 = ResumeRole.model_validate(raw_data_strings)
    assert len(role2.subsections) == 1
    assert role2.subsections[0].bullets == ["First bullet point.", "Second bullet point."]


def test_resume_role_empty_subsections_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ResumeRole.model_validate(
            {
                "title": "Software Engineer",
                "company": "Acme Corp",
                "dates": "2020 - Present",
                "subsections": [],
            }
        )
