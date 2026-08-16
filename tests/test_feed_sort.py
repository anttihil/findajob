"""The `date_posted` feed sort, and the rows the board gave no date for.

`date_posted` replaced `date_found`, which ranked postings by when the scraper caught them
rather than by anything about the posting: one scrape day returns postings spanning two
months, so the found date measures the search schedule.

The assertion that matters here is the fallback. About 2% of the corpus carries no
`date_posted` and no `posted_window_*` either -- the sources JobSpy does not supply dates
for -- and SQLite sorts NULL last under DESC, so a bare `date_posted DESC` would park those
rows permanently at the end of a 20k-row feed where nobody reaches them. The sort coalesces
to the found date, which is an honest upper bound: a posting existed at or before the moment
it was seen.

`job_ids_for` is exercised alongside `query_jobs` because it selects only `jobs.id`. A sort
written against the `liveness` SELECT alias rather than the inlined CASE would work in the
feed and fail there, and the drawer's "next posting" comes from that query.
"""

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

    def job(self, job_id: int, date_posted: str | None, date_found: str) -> None:
        self.db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, status, "
            "date_posted, date_found, sync_run_id) "
            "VALUES (?, ?, 'Platform Engineer', ?, 'desc', 'unread', ?, ?, 1)",
            (job_id, f"k{job_id}", f"https://example.test/{job_id}", date_posted, date_found),
        )
        self.db.conn.commit()

    def feed(self) -> list[int]:
        page = self.db.query_jobs(status="unread", sort="date_posted", limit=50)
        return [job["id"] for job in page["jobs"]]

    def test_the_feed_descends_by_posted_date_not_found_date(self) -> None:
        # 1 was posted first and found last; a `date_found` sort would invert these.
        self.job(1, "2026-08-01", "2026-08-14T09:00:00+00:00")
        self.job(2, "2026-08-10", "2026-08-11T09:00:00+00:00")
        self.assertEqual([2, 1], self.feed())

    def test_a_same_day_tie_breaks_on_the_found_date(self) -> None:
        self.job(1, "2026-08-10", "2026-08-10T09:00:00+00:00")
        self.job(2, "2026-08-10", "2026-08-12T09:00:00+00:00")
        self.assertEqual([2, 1], self.feed())

    def test_a_posting_with_no_posted_date_sorts_by_the_day_it_was_found(self) -> None:
        # The point of the COALESCE: 2 has no date, and must land between the two dated
        # rows rather than after both of them.
        self.job(1, "2026-08-12", "2026-08-12T09:00:00+00:00")
        self.job(2, None, "2026-08-10T09:00:00+00:00")
        self.job(3, "2026-08-05", "2026-08-05T09:00:00+00:00")
        self.assertEqual([1, 2, 3], self.feed())

    def test_the_found_timestamp_is_truncated_to_its_day_before_comparison(self) -> None:
        # Otherwise the full ISO-8601 string compares against a bare YYYY-MM-DD and every
        # dateless row outranks the dated row it shares a day with.
        self.job(1, "2026-08-10", "2026-08-10T23:59:00+00:00")
        self.job(2, None, "2026-08-10T00:01:00+00:00")
        self.assertEqual([1, 2], self.feed())

    def test_job_ids_for_accepts_the_sort_without_the_liveness_alias(self) -> None:
        self.job(1, "2026-08-01", "2026-08-01T09:00:00+00:00")
        self.job(2, "2026-08-10", "2026-08-10T09:00:00+00:00")
        self.assertEqual([2, 1], self.db.job_ids_for(status="unread", sort="date_posted", limit=50))

    def test_date_found_is_no_longer_a_sort(self) -> None:
        self.assertNotIn("date_found", self.db._SORTS)


if __name__ == "__main__":
    unittest.main()
