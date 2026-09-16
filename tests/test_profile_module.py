"""Profile: rendering, adapter, models, and singleton persistence."""

import os
import sqlite3
import sys
import tempfile
import unittest
from typing import cast

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.core.migrations import migrate
from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import (
    MasterEducation,
    MasterProject,
    MasterRole,
    MasterSkillCategory,
    Profile,
    RoleTargeting,
    WorkEligibility,
)
from careerradar.profile.render import (
    render_profile,
    render_profile_for_resume,
    render_profile_for_scoring,
)


class RenderTests(unittest.TestCase):
    def sample_profile(self) -> Profile:
        return Profile(
            name="Jane Doe",
            email="jane@example.com",
            phone="555-0100",
            location="San Francisco, CA",
            summary_guidance=(
                "Staff software engineer specialized in AI systems and distributed backends."
            ),
            seniority="Mid / Senior",
            years_experience=4.5,
            eligibility=WorkEligibility(
                citizenship=["Authorized to work in US"],
                locations=["San Francisco, CA", "Remote"],
                willing_to_relocate=False,
                comp_floor_usd=150000,
            ),
            targeting=RoleTargeting(
                target_roles=["AI Infrastructure", "Platform Engineer"],
                work_modes=["Remote", "Hybrid"],
                dealbreakers=["No 24/7 on-call", "No on-site relocation"],
            ),
            skills=[
                MasterSkillCategory(
                    category="Languages",
                    skills=["Python", "TypeScript", "Go"],
                ),
                MasterSkillCategory(
                    category="Cloud & Infra",
                    skills=["AWS", "Docker", "Kubernetes"],
                ),
            ],
            experience=[
                MasterRole(
                    title="Lead Software Engineer",
                    company="TechCorp",
                    dates="2022 - Present",
                    location="San Francisco, CA",
                    projects=[
                        MasterProject(
                            heading="Built high throughput data engine",
                            bullets=["Processed 50k events/sec with sub-second latency."],
                        )
                    ],
                )
            ],
            education=[
                MasterEducation(
                    institution="State University",
                    degree="BS Computer Science",
                )
            ],
        )

    def test_render_is_byte_stable_across_calls(self) -> None:
        """The prefix cache matches bytes. Instability is a ~50x input-cost regression."""
        profile = self.sample_profile()
        self.assertEqual(render_profile(profile), render_profile(profile))
        self.assertEqual(render_profile_for_scoring(profile), render_profile_for_scoring(profile))
        self.assertEqual(render_profile_for_resume(profile), render_profile_for_resume(profile))

    def test_render_is_stable_under_skill_reordering(self) -> None:
        """Two profiles with the same skills in a different order must render stably."""
        a = self.sample_profile()
        b = a.model_copy(update={"skills": list(reversed(a.skills))})
        self.assertEqual(render_profile(a), render_profile(b))
        self.assertEqual(render_profile_for_resume(a), render_profile_for_resume(b))

    def test_render_carries_the_parts_the_scorer_needs(self) -> None:
        text = render_profile(self.sample_profile())
        self.assertIn("Python", text)
        self.assertIn("TechCorp", text)
        self.assertIn("Built high throughput data engine", text)
        self.assertIn("$150,000", text)
        self.assertIn("DEALBREAKERS", text)
        self.assertIn("No 24/7 on-call", text)

    def test_render_resume_context(self) -> None:
        text = render_profile_for_resume(self.sample_profile())
        self.assertIn("CANDIDATE NAME: Jane Doe", text)
        self.assertIn("San Francisco, CA", text)
        self.assertIn("ROLE: Lead Software Engineer at TechCorp", text)
        self.assertIn("Built high throughput data engine", text)
        self.assertIn("Languages: Python, TypeScript, Go", text)
        self.assertNotIn("DEALBREAKERS", text)
        self.assertNotIn("$150,000", text)

    def test_render_contains_no_clock(self) -> None:
        """A timestamp anywhere in the prefix invalidates the cache on every call."""
        text = render_profile(self.sample_profile())
        for marker in ("2026-", "2025-", "T00:", "GMT", "UTC"):
            self.assertNotIn(marker, text)

    def test_render_includes_all_project_bullets_without_truncation(self) -> None:
        """Ensure every bullet across multiple projects and roles is fully rendered."""
        b1 = (
            "Cut ~1,000 hours of manual migration by having coding agents "
            "mine millions of lines of site data into deterministic XML transformation rules"
        )
        b2 = (
            "Consolidated independently maintained sites into a multi-tenant "
            "platform with centralized CI/CD and IaC, onboarding first 15 clients"
        )
        b3 = (
            "Enabled decommissioning decisions with a content inventory "
            "tool surfacing media volume, page age, and content issues invisible to admin users"
        )
        profile = Profile(
            summary_guidance="Senior developer.",
            experience=[
                MasterRole(
                    title="Software Engineer",
                    company="University of California Los Angeles",
                    dates="Jan 2025 - Present",
                    projects=[
                        MasterProject(
                            heading="Designed and delivered the replacement for 20 sites:",
                            bullets=[b1, b2, b3],
                        ),
                    ],
                ),
                MasterRole(
                    title="Research Assistant",
                    company="UCLA",
                    dates="2020 - 2022",
                    projects=[],
                ),
            ],
            projects=[
                MasterProject(
                    heading="Multi-agent search workflow",
                    bullets=[
                        "Built async pipeline handling 50 requests/sec with low latency.",
                        "Integrated SQLite vector search for sub-second retrieval.",
                    ],
                )
            ],
        )
        text = render_profile(profile)
        self.assertIn(
            "Software Engineer at University of California Los Angeles: "
            "Designed and delivered the replacement for 20 sites:",
            text,
        )
        self.assertIn(b1, text)
        self.assertIn(b2, text)
        self.assertIn(b3, text)
        self.assertIn("Research Assistant at UCLA", text)
        self.assertIn("Project: Multi-agent search workflow", text)
        self.assertIn("Built async pipeline handling 50 requests/sec with low latency.", text)
        self.assertIn("Integrated SQLite vector search for sub-second retrieval.", text)


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Profile(
            summary_guidance="Staff software engineer.",
            seniority="Mid / Senior",
            years_experience=4.0,
            skills=[
                MasterSkillCategory(category="Languages", skills=["Python", "TypeScript", "Go"]),
                MasterSkillCategory(category="Infra", skills=["AWS", "Docker"]),
            ],
            experience=[
                MasterRole(
                    title="Software Engineer",
                    company="Acme Corp",
                    dates="2020 - Present",
                    projects=[
                        MasterProject(
                            heading="Built REST APIs",
                            bullets=["Used Python and AWS to build scalable microservices."],
                        )
                    ],
                )
            ],
        )
        self.adapter = ProfileAdapter(self.profile)

    def test_level_and_has_match_expected_semantics(self) -> None:
        self.assertTrue(self.adapter.has("python"))
        self.assertTrue(self.adapter.has("aws"))
        self.assertFalse(self.adapter.has("rust"))
        self.assertEqual(self.adapter.level("python"), 1)
        self.assertEqual(self.adapter.level("nonexistent"), 0)

    def test_keys_and_contains(self) -> None:
        keys = self.adapter.keys()
        self.assertIn("python", keys)
        self.assertIn("docker", keys)
        self.assertIn("python", self.adapter)
        self.assertNotIn("rust", self.adapter)

    def test_evidence_is_available(self) -> None:
        evidence = self.adapter.evidence("python")
        self.assertTrue(len(evidence) > 0)
        self.assertIn("Python", evidence[0])

    def test_by_category_preserves_profile_categories(self) -> None:
        grouped = self.adapter.by_category()
        self.assertIn("Languages", grouped)
        self.assertIn("Infra", grouped)
        lang_keys = [s["key"] for s in grouped["Languages"]]
        self.assertIn("python", lang_keys)
        self.assertIn("typescript", lang_keys)


class StoreTests(unittest.TestCase):
    """Round-trip against a real migrated database with single profile table."""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    class _Db:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self.conn = conn

        def close(self) -> None:
            pass

    def _db(self) -> Database:
        return cast(Database, self._Db(self.conn))

    def test_saving_and_loading_single_profile(self) -> None:
        from careerradar.profile.repository import load_active, load_profile, save_profile

        profile = Profile(
            name="Jane Doe",
            email="jane@example.com",
            summary_guidance="Architect",
            skills=[MasterSkillCategory(category="Languages", skills=["Python", "Go"])],
        )

        save_profile(profile, conn=self.conn)

        loaded = load_profile(conn=self.conn)
        self.assertEqual(loaded.name, "Jane Doe")
        self.assertEqual(loaded.skills[0].skills, ["Python", "Go"])

        count = self.conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
        self.assertEqual(count, 1)

        active = load_active(db=self._db())
        assert active is not None
        version, prof, summary = active
        self.assertEqual(version, 1)
        self.assertEqual(prof.name, "Jane Doe")
        self.assertIn("Architect", summary)

    def test_summary_text_persisted(self) -> None:
        from careerradar.profile.repository import load_active, save_profile

        profile = Profile(
            name="Jane Doe",
            email="jane@example.com",
            summary_guidance="Staff Engineer",
            skills=[MasterSkillCategory(category="Languages", skills=["Python"])],
        )
        save_profile(profile, conn=self.conn, render_prompt=True)
        active = load_active(db=self._db())
        assert active is not None
        _, loaded, summary = active
        self.assertEqual(summary, render_profile(loaded))


if __name__ == "__main__":
    unittest.main()
