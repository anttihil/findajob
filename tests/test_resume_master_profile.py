"""Unit tests for Resume Master Profile persistence and models."""

import sqlite3

from careerradar.core.migrations import migrate
from careerradar.resumes.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    ResumeMasterProfile,
)
from careerradar.resumes.repository import load_master_profile, save_master_profile


def test_master_profile_crud():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    # Initial profile seeded by migration v17
    p0 = load_master_profile(conn)
    assert p0.name != ""

    # Update profile
    p1 = ResumeMasterProfile(
        name="Jane Test",
        email="user@test.com",
        phone="555-0199",
        location="San Francisco, CA",
        github="https://github.com/usertest",
        linkedin="https://linkedin.com/in/usertest",
        website="https://usertest.dev",
        summary_guidance="Staff software engineer specialized in distributed systems and AI.",
        education=[MasterEducation(institution="MIT", degree="MS Computer Science", details="")],
        skills=[MasterSkillCategory(category="Languages", skills=["Python", "TypeScript", "Go"])],
        experience=[
            MasterRole(
                title="Lead Architect",
                company="TechCorp",
                dates="2022 - Present",
                projects=[
                    MasterProject(
                        name="AI Engine",
                        heading="Engineered real-time streaming engine",
                        bullets=["Reduced p99 latency by 40% using async Rust pipeline."],
                    )
                ],
            )
        ],
        raw_achievements_md="## Key Achievements\n- Built scalable microservices.",
    )

    save_master_profile(p1, conn)
    loaded = load_master_profile(conn)

    assert loaded.name == "Jane Test"
    assert loaded.email == "user@test.com"
    assert len(loaded.education) == 1
    assert loaded.education[0].institution == "MIT"
    assert len(loaded.skills) == 1
    assert loaded.skills[0].skills == ["Python", "TypeScript", "Go"]
    assert len(loaded.experience) == 1
    assert loaded.experience[0].company == "TechCorp"
    assert len(loaded.experience[0].projects[0].bullets) == 1
    assert "p99 latency" in loaded.experience[0].projects[0].bullets[0]
    assert "Key Achievements" in loaded.raw_achievements_md
    conn.close()
