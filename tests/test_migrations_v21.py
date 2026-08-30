"""Unit tests for schema migration v21 (Profile Executive Summary, Directives, Projects)."""

import sqlite3

from careerradar.core.migrations import current_version, migrate
from careerradar.profile.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    WorkEligibility,
)
from careerradar.profile.render import render_profile
from careerradar.profile.repository import load_profile, save_profile


def test_migration_v21_columns_and_defaults():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    assert current_version(conn) >= 21

    # Verify columns exist on single `profile` table
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(profile)")}
    expected_cols = {
        "id",
        "updated_at",
        "name",
        "email",
        "phone",
        "location",
        "github",
        "linkedin",
        "website",
        "executive_summary",
        "model_guidance",
        "dealbreakers_json",
        "projects_json",
        "seniority",
        "years_experience",
        "eligibility_json",
        "education_json",
        "skills_json",
        "experience_json",
        "summary_text",
    }
    for col in expected_cols:
        assert col in columns, f"Expected column {col} in profile table"


def test_profile_v21_roundtrip_with_projects_and_guidance():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    exec_pitch = "Staff Engineer architecting high-scale distributed systems."
    guidance = "Focus on backend infrastructure. Disregard frontend requirements."

    profile = Profile(
        name="Alex River",
        email="alex@example.com",
        phone="555-1234",
        location="Austin, TX",
        github="https://github.com/alexriver",
        linkedin="https://linkedin.com/in/alexriver",
        website="https://alexriver.dev",
        seniority="Senior / Staff",
        years_experience=6.0,
        executive_summary=exec_pitch,
        model_guidance=guidance,
        dealbreakers=["24/7 on-call", "DoD security clearance required"],
        eligibility=WorkEligibility(
            citizenship=["US Citizen"],
            locations=["Austin, TX", "Remote"],
            willing_to_relocate=False,
            comp_floor_usd=160000,
        ),
        experience=[
            MasterRole(
                title="Lead Infrastructure Engineer",
                company="CloudScale",
                dates="2022 - Present",
                location="Austin, TX",
                projects=[
                    MasterProject(
                        heading="Distributed Kafka streaming platform",
                        bullets=["Scaled Kafka pipeline to 100k events/sec."],
                    )
                ],
            )
        ],
        projects=[
            MasterProject(
                heading="CareerRadar - Autonomous AI job search engine",
                url="https://github.com/alexriver/careerradar",
                bullets=["Engineered LangGraph multi-agent pipeline."],
            )
        ],
        skills=[
            MasterSkillCategory(category="Languages", skills=["Go", "Python", "Rust"]),
            MasterSkillCategory(category="Infrastructure", skills=["Kubernetes", "Kafka", "AWS"]),
        ],
        education=[
            MasterEducation(
                institution="UT Austin",
                degree="BS Computer Science",
                details="Honors, Magna Cum Laude",
            )
        ],
    )

    save_profile(profile, conn=conn)

    loaded = load_profile(conn=conn)
    assert loaded.name == "Alex River"
    assert loaded.executive_summary == exec_pitch
    assert loaded.model_guidance == guidance
    assert loaded.dealbreakers == ["24/7 on-call", "DoD security clearance required"]
    assert len(loaded.projects) == 1
    assert loaded.projects[0].heading == "CareerRadar - Autonomous AI job search engine"
    assert loaded.projects[0].url == "https://github.com/alexriver/careerradar"
    assert loaded.projects[0].bullets == ["Engineered LangGraph multi-agent pipeline."]
    assert len(loaded.experience) == 1
    assert loaded.experience[0].company == "CloudScale"

    # Verify rendered summary prefix includes projects, directives, and dealbreakers
    summary = render_profile(loaded)
    assert "Alex River" not in summary  # Header text
    assert "Staff Engineer architecting high-scale" in summary
    assert "Project: CareerRadar - Autonomous AI job search engine" in summary
    assert "Engineered LangGraph multi-agent pipeline." in summary
    assert "POSITIONING & STRATEGIC DIRECTIVES" in summary
    assert "Focus on backend infrastructure" in summary
    assert "NON-NEGOTIABLE DEALBREAKERS" in summary
    assert "24/7 on-call" in summary
