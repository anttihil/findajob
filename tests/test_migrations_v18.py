"""Unit tests for schema migration v18 (Unified Master Profile & Downstream Sync)."""

import sqlite3

from careerradar.core.migrations import current_version, migrate
from careerradar.resumes.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    ResumeMasterProfile,
)
from careerradar.resumes.repository import (
    load_master_profile,
    master_profile_to_scoring_profile,
    save_master_profile,
)


def test_migration_v18_columns_and_defaults():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    assert current_version(conn) == 18

    # Verify columns exist on resume_master_profile
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(resume_master_profile)")}
    expected_cols = {
        "seniority",
        "years_experience",
        "citizenship_json",
        "locations_json",
        "willing_to_relocate",
        "comp_floor_usd",
        "target_roles_json",
        "work_modes_json",
        "target_industries_json",
        "dealbreakers_json",
        "strengths_json",
        "weaknesses_json",
        "skill_ratings_json",
    }
    for col in expected_cols:
        assert col in columns, f"Column {col} missing in resume_master_profile table"


def test_master_profile_load_save_and_conversion():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    profile = ResumeMasterProfile(
        name="Jane Doe",
        email="jane@example.com",
        location="San Francisco, CA",
        seniority="Mid / Senior",
        years_experience=4.0,
        citizenship=["Authorized to work in US"],
        locations=["San Francisco, CA", "Remote"],
        willing_to_relocate=True,
        comp_floor_usd=95000,
        target_roles=["AI Infrastructure", "Platform Engineer"],
        dealbreakers=["24/7 on-call rotation"],
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

    save_master_profile(profile, conn=conn, sync_scoring=False)

    loaded = load_master_profile(conn=conn)
    assert loaded.name == "Jane Doe"
    assert loaded.seniority == "Mid / Senior"
    assert loaded.years_experience == 4.0
    assert "Authorized to work in US" in loaded.citizenship
    assert loaded.comp_floor_usd == 95000
    assert len(loaded.skills) == 1
    assert loaded.skills[0].category == "Infrastructure"
    assert "AWS" in loaded.skills[0].skills

    # Test conversion to scoring Profile
    scoring_prof = master_profile_to_scoring_profile(loaded)
    assert scoring_prof.years_experience == 4.0
    assert scoring_prof.seniority == "Mid / Senior"
    assert len(scoring_prof.skills) >= 3
    assert any(s.key == "aws" for s in scoring_prof.skills)
    assert scoring_prof.constraints.comp_floor_usd == 95000
    assert "Authorized to work in US" in scoring_prof.constraints.work_authorization
    assert "24/7 on-call rotation" in scoring_prof.non_negotiables
