"""Unit tests for schema migration v19 (Single Unified Profile Table)."""

import sqlite3

from careerradar.core.migrations import current_version, migrate
from careerradar.profile.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    RoleTargeting,
    WorkEligibility,
)
from careerradar.profile.repository import load_profile, save_profile


def test_migration_v19_columns_and_defaults():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    assert current_version(conn) >= 19

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
        "summary_guidance",
        "seniority",
        "years_experience",
        "eligibility_json",
        "targeting_json",
        "education_json",
        "skills_json",
        "experience_json",
        "summary_text",
    }
    for col in expected_cols:
        assert col in columns, f"Column {col} missing in profile table"


def test_single_profile_load_and_save():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    profile = Profile(
        name="Jane Doe",
        email="jane@example.com",
        location="San Francisco, CA",
        summary_guidance="Senior platform architect.",
        seniority="Mid / Senior",
        years_experience=4.0,
        eligibility=WorkEligibility(
            citizenship=["Authorized to work in US"],
            locations=["San Francisco, CA", "Remote"],
            willing_to_relocate=True,
            comp_floor_usd=95000,
        ),
        targeting=RoleTargeting(
            target_roles=["AI Infrastructure", "Platform Engineer"],
            dealbreakers=["24/7 on-call rotation"],
        ),
        education=[
            MasterEducation(
                institution="State University",
                degree="BS in Computer Science",
            )
        ],
        skills=[
            MasterSkillCategory(
                category="Infrastructure",
                skills=["AWS", "Terraform", "Docker"],
            )
        ],
        experience=[
            MasterRole(
                title="Software Engineer",
                company="Acme Corp",
                dates="2024 - Present",
                projects=[
                    MasterProject(
                        name="Simulator",
                        heading="Robot simulator:",
                        bullets=["Built frontend robot simulator in TypeScript."],
                    )
                ],
            )
        ],
    )

    save_profile(profile, conn=conn, render_prompt=True)

    loaded = load_profile(conn=conn)
    assert loaded.name == "Jane Doe"
    assert loaded.seniority == "Mid / Senior"
    assert loaded.years_experience == 4.0
    assert "Authorized to work in US" in loaded.eligibility.citizenship
    assert loaded.eligibility.comp_floor_usd == 95000
    assert len(loaded.skills) == 1
    assert loaded.skills[0].category == "Infrastructure"
    assert "AWS" in loaded.skills[0].skills
    assert "24/7 on-call rotation" in loaded.targeting.dealbreakers

    # Verify row in profile table has summary_text
    row = conn.execute("SELECT summary_text FROM profile WHERE id = 1").fetchone()
    assert row is not None
    assert "Senior platform architect." in row["summary_text"]
    assert "AWS, Terraform, Docker" in row["summary_text"]
    assert "Mid / Senior" in row["summary_text"]
