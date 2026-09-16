"""Unit tests for unified Profile persistence and models."""

import sqlite3

from findajob.core.migrations import migrate
from findajob.profile.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
)
from findajob.profile.repository import load_profile, save_profile


def test_master_profile_crud():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate(conn)

    p0 = load_profile(conn)
    assert p0 is not None

    p1 = Profile(
        name="Jane Doe",
        email="jane@example.com",
        phone="555-0199",
        location="San Francisco, CA",
        github="https://github.com/janedoe",
        linkedin="https://linkedin.com/in/janedoe",
        website="https://janedoe.dev",
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
                        heading="Engineered real-time streaming engine",
                        bullets=["Reduced p99 latency by 40% using async Rust pipeline."],
                    )
                ],
            )
        ],
    )

    save_profile(p1, conn)
    loaded = load_profile(conn)

    assert loaded.name == "Jane Doe"
    assert loaded.email == "jane@example.com"
    assert len(loaded.education) == 1
    assert loaded.education[0].institution == "MIT"
    assert len(loaded.skills) == 1
    assert loaded.skills[0].skills == ["Python", "TypeScript", "Go"]
    assert len(loaded.experience) == 1
    assert loaded.experience[0].company == "TechCorp"
    assert len(loaded.experience[0].projects[0].bullets) == 1
    assert "p99 latency" in loaded.experience[0].projects[0].bullets[0]
    conn.close()
