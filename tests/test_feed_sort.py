"""The default feed sort: strong fit first, then date posted (with timestamps/dates).

Postings are sorted by strong fit (`v.fit DESC NULLS LAST`), then by when they
were posted (`COALESCE(jobs.date_posted, jobs.date_found) DESC`), with
`jobs.match_score DESC` as secondary sort for same-day items, followed by
`jobs.date_found DESC` and `jobs.id DESC` as the final tie-breakers.

The fallback ensures dateless rows (which carry no `date_posted`) are coalesced
to the date they were found rather than being parked at the end of the feed.
"""

import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database


class DatePostedSortTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.path = tmp.name
        self.db = Database(self.path)

    def tearDown(self) -> None:
        self.db.conn.close()
        os.unlink(self.path)

    def profile(self, version: int = 1, is_active: int = 1) -> None:
        self.db.conn.execute(
            "INSERT INTO profiles (version, is_active, profile_json, summary_text, created_at) "
            "VALUES (?, ?, '{}', '', '2026-01-01T00:00:00+00:00')",
            (version, is_active),
        )
        self.db.conn.commit()

    def verdict(self, job_id: int, fit: int | None, profile_version: int = 1) -> None:
        if fit is not None:
            self.db.conn.execute(
                "INSERT INTO job_verdicts (job_id, profile_version, model, fit, reason_type, "
                "reason_description, verdict_schema_version, created_at) "
                "VALUES (?, ?, 'm', ?, 'match', 'desc', 1, '2026-01-01T00:00:00+00:00')",
                (job_id, profile_version, fit),
            )
            self.db.conn.commit()

    def job(
        self,
        job_id: int,
        date_posted: str | None,
        date_found: str,
        match_score: int = 0,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, status, "
            "date_posted, date_found, match_score, sync_run_id) "
            "VALUES (?, ?, 'Platform Engineer', ?, 'desc', 'unread', ?, ?, ?, 1)",
            (
                job_id,
                f"k{job_id}",
                f"https://example.test/{job_id}",
                date_posted,
                date_found,
                match_score,
            ),
        )
        self.db.conn.commit()

    def feed(self) -> list[int]:
        page = self.db.query_jobs(status="unread", limit=50)
        return [job["id"] for job in page["jobs"]]

    def test_strong_fit_outranks_no_fit_and_unscored(self) -> None:
        self.profile(version=1, is_active=1)
        # 1 is older but a strong fit (fit=1)
        self.job(1, "2026-08-01T10:00:00", "2026-08-01T12:00:00+00:00")
        self.verdict(1, fit=1)

        # 2 is newer but not a fit (fit=0)
        self.job(2, "2026-08-10T10:00:00", "2026-08-10T12:00:00+00:00")
        self.verdict(2, fit=0)

        # 3 is newest but unscored (no verdict row -> fit=NULL)
        self.job(3, "2026-08-12T10:00:00", "2026-08-12T12:00:00+00:00")

        self.assertEqual([1, 2, 3], self.feed())

    def test_the_feed_descends_by_posted_date_not_found_date(self) -> None:
        # 1 was posted first and found last; a `date_found` sort would invert these.
        self.job(1, "2026-08-01T12:00:00Z", "2026-08-14T09:00:00+00:00")
        self.job(2, "2026-08-10T12:00:00Z", "2026-08-11T09:00:00+00:00")
        self.assertEqual([2, 1], self.feed())

    def test_the_feed_descends_by_posted_timestamp_granularity(self) -> None:
        # Both jobs posted on the same date, but job 2 has a later timestamp
        self.job(1, "2026-08-10T09:15:00", "2026-08-11T09:00:00+00:00")
        self.job(2, "2026-08-10T14:30:00", "2026-08-11T09:00:00+00:00")
        self.assertEqual([2, 1], self.feed())

    def test_a_same_day_tie_breaks_on_the_found_date(self) -> None:
        self.job(1, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00")
        self.job(2, "2026-08-10T12:00:00Z", "2026-08-12T09:00:00+00:00")
        self.assertEqual([2, 1], self.feed())

    def test_a_posting_with_no_posted_date_sorts_by_the_day_it_was_found(self) -> None:
        # The point of the COALESCE: 2 has no date, and must land between the two dated
        # rows rather than after both of them.
        self.job(1, "2026-08-12T12:00:00Z", "2026-08-12T09:00:00+00:00")
        self.job(2, None, "2026-08-10T09:00:00+00:00")
        self.job(3, "2026-08-05T12:00:00Z", "2026-08-05T09:00:00+00:00")
        self.assertEqual([1, 2, 3], self.feed())

    def test_a_posting_normalized_to_noon_ranks_consistently_with_found_timestamp(self) -> None:
        # A date-only posting normalized to noon UTC (12:00:00Z) ranks ahead of early-morning
        # found timestamps and behind late-night found timestamps.
        self.job(1, "2026-08-10T12:00:00Z", "2026-08-10T23:59:00+00:00")
        self.job(2, None, "2026-08-10T00:01:00+00:00")
        self.assertEqual([1, 2], self.feed())

    def test_job_ids_for_matches_query_jobs_order(self) -> None:
        self.job(1, "2026-08-01T12:00:00Z", "2026-08-01T09:00:00+00:00")
        self.job(2, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00")
        self.assertEqual([2, 1], self.db.job_ids_for(status="unread", limit=50))

    def test_same_day_sorts_by_match_score_desc(self) -> None:
        # Same day and found time, but job 2 has a higher match_score
        self.job(1, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00", match_score=50)
        self.job(2, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00", match_score=85)
        self.assertEqual([2, 1], self.feed())

    def test_same_day_and_match_score_tie_breaks_on_id_desc(self) -> None:
        # Same day, same found timestamp, same match score -> id DESC breaks tie
        self.job(1, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00", match_score=75)
        self.job(2, "2026-08-10T12:00:00Z", "2026-08-10T09:00:00+00:00", match_score=75)
        self.assertEqual([2, 1], self.feed())

    def test_sort_parameter_removed_from_database_methods(self) -> None:
        query_sig = inspect.signature(self.db.query_jobs)
        ids_sig = inspect.signature(self.db.job_ids_for)
        self.assertNotIn("sort", query_sig.parameters)
        self.assertNotIn("sort", ids_sig.parameters)


if __name__ == "__main__":
    unittest.main()
