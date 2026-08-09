"""User-profile tests.

The profile is what "does the user have this skill?" resolves to, so the graded levels
matter: the gap analysis should be able to distinguish a skill named once in a competencies
list from one used across current projects with real commit volume.
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.profile import (  # noqa: E402
    LEVEL_ABSENT,
    LEVEL_CLAIMED,
    LEVEL_MENTIONED,
    LEVEL_STRONG,
    build_profile,
)
from backend.taxonomy import load_taxonomy  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RealProfileTests(unittest.TestCase):
    """Against the actual corpus -- these files define the expected behaviour."""

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(os.path.join(REPO_ROOT, "resumes")):
            raise unittest.SkipTest("resumes/ not present")
        cls.profile = build_profile()

    def test_reads_all_eight_sources(self):
        """Six variants plus current_resume.md plus achievements.md."""
        self.assertEqual(len(self.profile.sources), 8)
        self.assertIn("current_resume.md", self.profile.sources)
        self.assertIn("achievements.md", self.profile.sources)

    def test_profile_is_substantial(self):
        self.assertGreater(len(self.profile), 60)

    def test_core_stack_is_present(self):
        for key in ["python", "typescript", "terraform", "docker", "aws", "postgresql",
                    "react", "ansible", "vllm", "claude_api"]:
            self.assertTrue(self.profile.has(key), f"expected {key} in profile")

    def test_aws_children_survive_parenthetical_expansion(self):
        """These were dropped entirely by the old parser."""
        for key in ["ec2", "s3", "iam", "vpc", "ssm"]:
            self.assertTrue(self.profile.has(key), f"expected {key} in profile")

    def test_go_is_present_from_current_resume_only(self):
        """Go appears in no file under resumes/ -- only current_resume.md."""
        self.assertTrue(self.profile.has("golang"))

    def test_market_skills_user_lacks_are_absent(self):
        """The gap analysis is meaningless unless these read as absent."""
        for key in ["kubernetes", "kafka", "snowflake", "dbt", "azure", "gcp",
                    "pytorch", "spark", "databricks", "airflow"]:
            self.assertFalse(
                self.profile.has(key), f"{key} should not be in the profile"
            )

    def test_project_evidence_produces_strong_levels(self):
        strong = [k for k, v in self.profile.skills.items()
                  if v["level"] == LEVEL_STRONG]
        self.assertGreater(len(strong), 10)
        # These carry real commit volume in achievements.md.
        for key in ["python", "typescript", "wordpress", "php"]:
            self.assertEqual(self.profile.level(key), LEVEL_STRONG, key)

    def test_commit_volume_is_recorded_for_strong_skills(self):
        self.assertGreater(self.profile.skills["php"]["commits"], 100)
        self.assertTrue(self.profile.skills["php"]["current"])

    def test_evidence_is_human_readable(self):
        evidence = self.profile.evidence("python")
        self.assertTrue(evidence)
        self.assertTrue(any("commits" in e or ".md" in e for e in evidence))

    def test_nordic_languages_come_from_taxonomy_not_corpus(self):
        """No resume states language proficiency, so these must be declared.

        This is load-bearing: with SE/NO/DK/FI in scope, a missing local language is a
        real blocking gap, and it can only be computed if the profile knows the user does
        not have Swedish.
        """
        self.assertEqual(self.profile.level("finnish"), LEVEL_STRONG)
        self.assertEqual(self.profile.level("english"), LEVEL_STRONG)
        self.assertEqual(self.profile.level("swedish"), LEVEL_ABSENT)
        self.assertEqual(self.profile.level("norwegian"), LEVEL_ABSENT)
        self.assertFalse(self.profile.has("swedish"))

    def test_each_resume_variant_is_tracked(self):
        self.assertGreaterEqual(len(self.profile.variants), 6)
        for name, info in self.profile.variants.items():
            self.assertTrue(info["title"], name)
            # The PM/EdTech variants legitimately carry fewer *technical* skills than the
            # engineering ones -- most of their competencies sections are prose about
            # stakeholders and communication, which correctly does not canonicalize.
            self.assertGreater(len(info["skills"]), 5, name)

    def test_engineering_variants_are_richly_covered(self):
        for name in ["fullstack_ai_engineer.md", "platform_devops_engineer.md",
                     "genai_application_engineer.md"]:
            info = self.profile.variants.get(name)
            self.assertIsNotNone(info, name)
            self.assertGreater(len(info["skills"]), 20, name)

    def test_pm_variant_covers_product_vocabulary(self):
        """EdTech PM is a target role family, so its vocabulary must canonicalize."""
        info = self.profile.variants["edtech_product_program_manager.md"]
        for key in ["product_mgmt", "agile", "edtech", "ab_testing", "accessibility"]:
            self.assertIn(key, info["skills"], key)

    def test_to_dict_is_serializable(self):
        import json

        payload = self.profile.to_dict()
        json.dumps(payload)  # must not raise
        self.assertIn("skills", payload)
        self.assertIn("taxonomy_hash", payload)
        # Absent skills are omitted from the serialized form.
        self.assertNotIn("swedish", payload["skills"])


class LevelDerivationTests(unittest.TestCase):
    """Level rules, against synthetic corpora so the thresholds are unambiguous."""

    def setUp(self):
        self.tax = load_taxonomy()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "resumes"))

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, relative, content):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return path

    def _build(self):
        return build_profile(
            resumes_dir=os.path.join(self.root, "resumes"),
            achievements_path=os.path.join(self.root, "achievements.md"),
            current_resume_path=os.path.join(self.root, "current_resume.md"),
            taxonomy=self.tax,
        )

    def test_skills_section_gives_claimed(self):
        self._write("resumes/a.md", "# X\n\n## Skills\n* **Backend:** Kubernetes\n")
        self.assertEqual(self._build().level("kubernetes"), LEVEL_CLAIMED)

    def test_prose_only_gives_mentioned(self):
        self._write(
            "resumes/a.md",
            "# X\n\n## Skills\n* **Backend:** Python\n\n## Experience\n"
            "* Ran **Kubernetes** in production.\n",
        )
        profile = self._build()
        self.assertEqual(profile.level("kubernetes"), LEVEL_MENTIONED)
        self.assertEqual(profile.level("python"), LEVEL_CLAIMED)

    def test_high_volume_project_gives_strong(self):
        self._write("resumes/a.md", "# X\n\n## Skills\n* **A:** Python\n")
        self._write(
            "achievements.md",
            "# A\n\n## Project Achievements\n\n### 1. Thing (`repo`)\n"
            "*Jan 2025 – present | Kubernetes, Python | 300 commits, 40 merged PRs*\n",
        )
        profile = self._build()
        self.assertEqual(profile.level("kubernetes"), LEVEL_STRONG)
        self.assertTrue(profile.skills["kubernetes"]["current"])

    def test_low_volume_project_stays_claimed(self):
        """A 10-commit shared repo is not strong evidence on its own."""
        self._write("resumes/a.md", "# X\n\n## Skills\n* **A:** Python\n")
        self._write(
            "achievements.md",
            "# A\n\n## Project Achievements\n\n### 1. Thing (`repo`)\n"
            "*Feb – Mar 2026 | Kubernetes | 10 commits*\n",
        )
        self.assertEqual(self._build().level("kubernetes"), LEVEL_CLAIMED)

    def test_levels_only_ratchet_upward(self):
        """Prose in one file must not demote a skills-section claim in another."""
        self._write("resumes/a.md", "# X\n\n## Skills\n* **A:** Kubernetes\n")
        self._write(
            "resumes/b.md",
            "# Y\n\n## Skills\n* **A:** Python\n\n## Experience\n* Used **Kubernetes**.\n",
        )
        self.assertEqual(self._build().level("kubernetes"), LEVEL_CLAIMED)

    def test_unknown_skill_is_absent(self):
        self._write("resumes/a.md", "# X\n\n## Skills\n* **A:** Python\n")
        profile = self._build()
        self.assertEqual(profile.level("kafka"), LEVEL_ABSENT)
        self.assertFalse(profile.has("kafka"))

    def test_keys_respects_min_level(self):
        self._write(
            "resumes/a.md",
            "# X\n\n## Skills\n* **A:** Kubernetes\n\n## Experience\n* Used **Kafka**.\n",
        )
        profile = self._build()
        self.assertIn("kafka", profile.keys(min_level=LEVEL_MENTIONED))
        self.assertNotIn("kafka", profile.keys(min_level=LEVEL_CLAIMED))
        self.assertIn("kubernetes", profile.keys(min_level=LEVEL_CLAIMED))

    def test_missing_optional_sources_are_tolerated(self):
        self._write("resumes/a.md", "# X\n\n## Skills\n* **A:** Python\n")
        profile = self._build()  # no achievements.md, no current_resume.md
        self.assertTrue(profile.has("python"))


if __name__ == "__main__":
    unittest.main()
