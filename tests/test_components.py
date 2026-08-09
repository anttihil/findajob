"""Smoke tests for the resume parser and the legacy matcher shim.

The previous `test_matcher_scoring` asserted `score >= 75` for a fixture, a number derived
from the old scorer's "+25 per title skill, +5 per body skill" arithmetic. That pinned an
implementation detail rather than a requirement: any reweighting broke it, and re-tuning the
constant would have taught nothing. Directional properties live in tests/test_scoring.py;
what remains here is that the legacy interface still works and returns sane shapes.
"""

import os
import sys
import unittest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.matcher import JobMatcher
from backend.resume_parser import ResumeParser


class TestResumeParsing(unittest.TestCase):
    def setUp(self):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.resumes_dir = os.path.join(project_dir, "resumes")

    def test_resume_parser_execution(self):
        """The parser finds every resume file and produces the expected shape."""
        if not os.path.exists(self.resumes_dir):
            self.skipTest("Resumes directory not found at " + self.resumes_dir)

        resumes = ResumeParser(self.resumes_dir).parse_all()
        self.assertGreater(len(resumes), 0, "Parser did not find any resume files.")

        for filename, data in resumes.items():
            self.assertIn("title", data)
            self.assertIn("skills", data)
            self.assertIsInstance(data["skills"], list)
            self.assertGreater(
                len(data["skills"]), 0, f"No skills extracted from {filename}"
            )
            # Every file must match a skills heading; four heading spellings are in use and
            # the previous parser recognised only one of them.
            self.assertTrue(
                data["has_skills_section"],
                f"No skills section matched in {filename}",
            )


class TestLegacyMatcherShim(unittest.TestCase):
    """The old (resume, score, skills) interface still works over the new scorer."""

    @classmethod
    def setUpClass(cls):
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if not os.path.isdir(os.path.join(project_dir, "resumes")):
            raise unittest.SkipTest("resumes/ not present")
        cls.matcher = JobMatcher()

    def test_returns_the_legacy_triple(self):
        resume, score, matched = self.matcher.evaluate_job(
            "Senior Platform Engineer",
            "You will own AWS infrastructure using Terraform, Ansible and Docker.",
        )
        self.assertIsInstance(score, int)
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 100)
        self.assertIsInstance(matched, list)
        self.assertIn("platform", (resume or "").lower())

    def test_matched_skills_are_human_readable_labels(self):
        _, _, matched = self.matcher.evaluate_job(
            "Platform Engineer",
            "Terraform, Docker and AWS experience required.",
        )
        self.assertIn("Terraform", matched)
        self.assertIn("Docker", matched)
        self.assertIn("AWS", matched)

    def test_role_family_selects_the_matching_resume(self):
        resume, _, _ = self.matcher.evaluate_job(
            "GenAI Application Engineer",
            "Build LLM applications with vLLM and the Claude API.",
        )
        self.assertEqual(resume, "genai_application_engineer.md")

    def test_empty_input_is_safe(self):
        resume, score, matched = self.matcher.evaluate_job("", "")
        self.assertGreaterEqual(score, 0)
        self.assertIsInstance(matched, list)

    def test_detailed_result_exposes_components(self):
        result = self.matcher.evaluate_detailed({
            "title": "Senior Platform Engineer",
            "description": "Terraform, Kubernetes, AWS.",
            "role_family": "platform_engineer",
            "seniority": "senior",
        })
        self.assertIn("components", result)
        self.assertIn("missing_skills", result)


if __name__ == "__main__":
    unittest.main()
