"""Feed-only grouping of geographic/reposted variants."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.core.database import Database


class FeedGroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.path = tmp.name
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.close()
        os.unlink(self.path)

    def add_job(
        self,
        job_id: int,
        company: str | None,
        title: str,
        location: str,
        found_at: str,
        *,
        source: str = "indeed",
    ) -> None:
        self.db.conn.execute(
            """
            INSERT INTO jobs
                (id, job_key, company, title, location, url, source, status,
                 date_found, sync_run_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'unread', ?, 1)
            """,
            (
                job_id,
                f"job-{job_id}",
                company,
                title,
                location,
                f"https://example.test/{job_id}",
                source,
                found_at,
            ),
        )
        self.db.conn.commit()

    def test_groups_normalized_company_and_title_and_chooses_newest_representative(self) -> None:
        self.add_job(
            1, "Acme, Inc.", "Senior Platform Engineer (Remote)", "CA, US", "2026-09-01T00:00:00Z"
        )
        self.add_job(2, "ACME", "Senior Platform Engineer", "NY, US", "2026-09-03T00:00:00Z")
        self.add_job(3, "Acme LLC", "Senior Platform Engineer", "TX, US", "2026-09-02T00:00:00Z")

        page = self.db.query_jobs(status="unread")

        self.assertEqual(page["total"], 1)
        self.assertEqual([job["id"] for job in page["jobs"]], [2])
        self.assertEqual(page["jobs"][0]["listing_count"], 3)
        self.assertEqual(self.db.job_ids_for(status="unread"), [2])

    def test_does_not_group_different_companies_or_blank_company(self) -> None:
        self.add_job(1, "Acme", "Platform Engineer", "CA, US", "2026-09-01T00:00:00Z")
        self.add_job(2, "Beta", "Platform Engineer", "CA, US", "2026-09-02T00:00:00Z")
        self.add_job(3, None, "Platform Engineer", "CA, US", "2026-09-03T00:00:00Z")
        self.add_job(4, None, "Platform Engineer", "NY, US", "2026-09-04T00:00:00Z")

        page = self.db.query_jobs(status="unread")

        self.assertEqual(page["total"], 4)
        self.assertEqual([job["listing_count"] for job in page["jobs"]], [1, 1, 1, 1])

    def test_filters_apply_before_grouping(self) -> None:
        self.add_job(
            1, "Acme", "Platform Engineer", "CA, US", "2026-09-01T00:00:00Z", source="indeed"
        )
        self.add_job(
            2, "Acme", "Platform Engineer", "NY, US", "2026-09-03T00:00:00Z", source="linkedin"
        )

        page = self.db.query_jobs(status="unread", source="indeed")

        self.assertEqual(page["total"], 1)
        self.assertEqual(page["jobs"][0]["id"], 1)
        self.assertEqual(page["jobs"][0]["listing_count"], 1)


if __name__ == "__main__":
    unittest.main()
