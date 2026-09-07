"""Tests for fuzzy search across job title, company, matched_skills, location,
and seniority.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database


class JobFuzzySearchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.path = tmp.name
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.conn.close()
        os.unlink(self.path)

    def add_job(
        self,
        job_id: int,
        title: str,
        company: str,
        *,
        matched_skills: list[str] | None = None,
        location: str | None = None,
        country: str | None = None,
        seniority: str | None = None,
        status: str = "unread",
        date_posted: str | None = "2026-08-15",
        date_found: str = "2026-08-15T10:00:00+00:00",
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, company, matched_skills, location, country, "
            "seniority, status, date_posted, date_found, sync_run_id, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
            (
                job_id,
                f"key_{job_id}",
                title,
                company,
                json.dumps(matched_skills or []),
                location,
                country,
                seniority,
                status,
                date_posted,
                date_found,
                f"https://example.test/job/{job_id}",
            ),
        )
        self.db.conn.commit()

    def test_empty_query_returns_all_unfiltered_jobs(self) -> None:
        self.add_job(1, "Python Developer", "Acme Inc")
        self.add_job(2, "Rust Engineer", "Tech Corp")

        res_none = self.db.query_jobs(q=None)
        self.assertEqual(len(res_none["jobs"]), 2)

        res_empty = self.db.query_jobs(q="")
        self.assertEqual(len(res_empty["jobs"]), 2)

        res_space = self.db.query_jobs(q="   ")
        self.assertEqual(len(res_space["jobs"]), 2)

    def test_exact_title_match(self) -> None:
        self.add_job(1, "Senior Python Backend Engineer", "Alpha Corp")
        self.add_job(2, "Frontend React Developer", "Beta Inc")

        res = self.db.query_jobs(q="Python")
        self.assertEqual([j["id"] for j in res["jobs"]], [1])

    def test_exact_company_match(self) -> None:
        self.add_job(1, "Staff Engineer", "Google")
        self.add_job(2, "Staff Engineer", "Microsoft")

        res = self.db.query_jobs(q="Google")
        self.assertEqual([j["id"] for j in res["jobs"]], [1])

    def test_matched_skills_search(self) -> None:
        self.add_job(1, "Software Developer", "Acme", matched_skills=["Kubernetes", "Docker", "Go"])
        self.add_job(2, "Software Developer", "Beta", matched_skills=["Java", "Spring"])

        res = self.db.query_jobs(q="Kubernetes")
        self.assertEqual([j["id"] for j in res["jobs"]], [1])

    def test_location_search(self) -> None:
        self.add_job(1, "Lead Architect", "Nordic Oy", location="Helsinki, Finland")
        self.add_job(2, "Lead Architect", "US Corp", location="San Francisco, USA")

        res = self.db.query_jobs(q="Helsinki")
        self.assertEqual([j["id"] for j in res["jobs"]], [1])

    def test_case_insensitive_matching(self) -> None:
        self.add_job(1, "TypeScript Engineer", "Acme")

        for term in ["typescript", "TYPESCRIPT", "TypeScript", "tYpEsCrIpT"]:
            res = self.db.query_jobs(q=term)
            self.assertEqual([j["id"] for j in res["jobs"]], [1], f"Failed for {term}")

    def test_fuzzy_typo_tolerance(self) -> None:
        self.add_job(1, "Python Developer", "Google", matched_skills=["Kubernetes", "FastAPI"])
        self.add_job(2, "Java Developer", "Oracle", matched_skills=["Spring Boot"])

        # Typo in title: 'pythn' -> 'Python'
        res1 = self.db.query_jobs(q="pythn")
        self.assertEqual([j["id"] for j in res1["jobs"]], [1])

        # Typo in company: 'gogle' -> 'Google'
        res2 = self.db.query_jobs(q="gogle")
        self.assertEqual([j["id"] for j in res2["jobs"]], [1])

        # Typo in skill: 'kuvernetes' -> 'Kubernetes'
        res3 = self.db.query_jobs(q="kuvernetes")
        self.assertEqual([j["id"] for j in res3["jobs"]], [1])

    def test_multi_token_search_requires_all_tokens(self) -> None:
        self.add_job(1, "Senior Python Engineer", "Acme", location="Remote")
        self.add_job(2, "Junior Python Engineer", "Acme", location="Remote")
        self.add_job(3, "Senior Java Engineer", "Acme", location="Remote")

        res = self.db.query_jobs(q="Senior Python")
        self.assertEqual([j["id"] for j in res["jobs"]], [1])

        # Multi-token with typo in one token
        res_fuzzy = self.db.query_jobs(q="Senior pythn")
        self.assertEqual([j["id"] for j in res_fuzzy["jobs"]], [1])

    def test_search_respects_status_and_filters(self) -> None:
        self.add_job(1, "Python Engineer", "Acme", status="unread", country="FI")
        self.add_job(2, "Python Engineer", "Acme", status="saved", country="FI")
        self.add_job(3, "Python Engineer", "Acme", status="unread", country="US")

        res_unread_fi = self.db.query_jobs(status="unread", country="FI", q="Python")
        self.assertEqual([j["id"] for j in res_unread_fi["jobs"]], [1])

        res_saved = self.db.query_jobs(status="saved", q="Python")
        self.assertEqual([j["id"] for j in res_saved["jobs"]], [2])

    def test_job_ids_for_matches_query_jobs_with_search(self) -> None:
        self.add_job(1, "Senior DevOps Engineer", "Cloud Inc", matched_skills=["Terraform", "AWS"])
        self.add_job(2, "DevOps Engineer", "Cloud Inc", matched_skills=["Terraform", "GCP"])
        self.add_job(3, "Frontend Developer", "Web Inc", matched_skills=["React"])

        q_jobs = self.db.query_jobs(q="Terraform")
        expected_ids = [j["id"] for j in q_jobs["jobs"]]

        feed_ids = self.db.job_ids_for(q="Terraform")
        self.assertEqual(feed_ids, expected_ids)
        self.assertEqual(set(feed_ids), {1, 2})


if __name__ == "__main__":
    unittest.main()
