"""The liveness view: when absence from a scrape counts as evidence of closure.

v6 called a posting closed whenever its cell was scraped after the posting was last seen
and the posting did not come back. That is only half the test. A cell's search carries
`hours_old`, which filters the board on `date_posted`, so a posting older than the window
cannot come back whether it is open or closed -- the rule was measuring age, not absence.
On the production corpus it fired on 89.5% of postings outside the window against 8.6% of
those inside it, and it was holding 8,435 postings of unknown status out of the scoring
queue.

So the assertions that matter here are the ones about restraint: what the view must NOT
call closed. Both halves are pinned, because a rule that never says 'likely_closed' would
pass every restraint test and put the whole dead corpus back in the queue.

The view is exercised through `migrate()` rather than a hand-written CREATE VIEW, and the
inlined copy in `database._LIVENESS_CASE` is checked against it directly -- the two are
separate strings kept deliberately identical, which is exactly the pair that drifts.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.core.migrations import apply_pragmas, migrate


def ago(**delta: float) -> str:
    """A timestamp relative to now. The view measures staleness against `now`, so a fixed
    date turns the same scrape from live into stale as the calendar moves on."""
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


class LivenessWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        apply_pragmas(self.conn)
        migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    # -- fixture -----------------------------------------------------------------------

    def job(self, job_id: int = 1, date_posted: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, date_posted, "
            "sync_run_id) VALUES (?, ?, 'Platform Engineer', ?, 'desc', ?, 1)",
            (job_id, f"k{job_id}", f"https://example.test/{job_id}", date_posted),
        )

    def cell(self, cell_id: int, last_success_at: str, last_hours_old: int | None = 24) -> None:
        self.conn.execute(
            "INSERT INTO scrape_cells (id, source, location_id, query, search_label, "
            "country, last_success_at, last_hours_old, created_at) "
            "VALUES (?, 'indeed', 'los_angeles', ?, 'Los Angeles, CA', 'US', ?, ?, "
            "'2026-07-01T00:00:00Z')",
            (cell_id, f"q{cell_id}", last_success_at, last_hours_old),
        )

    def place(self, job_id: int, cell_id: int | None, last_seen_at: str) -> None:
        self.conn.execute(
            "UPDATE jobs SET scrape_cell_id = ?, last_seen_at = ? WHERE id = ?",
            (cell_id, last_seen_at, job_id),
        )
        self.conn.commit()

    def liveness_of(self, job_id: int = 1) -> str:
        return self.conn.execute(
            "SELECT liveness FROM v_job_liveness WHERE job_id = ?", (job_id,)
        ).fetchone()["liveness"]

    # -- absence that IS evidence ------------------------------------------------------

    def test_a_posting_inside_the_window_that_did_not_come_back_is_likely_closed(self) -> None:
        """Posted 6h before a scrape with a 24h window, and absent from it. Gone."""
        self.job(date_posted=ago(hours=30))
        self.cell(1, ago(hours=24), last_hours_old=24)
        self.place(1, 1, ago(days=3))
        self.assertEqual(self.liveness_of(), "likely_closed")

    # -- absence that is NOT evidence --------------------------------------------------

    def test_a_posting_older_than_the_window_is_unknown_not_closed(self) -> None:
        """The 24h search could not have returned a 10-day-old posting either way.

        This is the case that was holding 8,435 postings out of the scoring queue.
        """
        self.job(date_posted=ago(days=10))
        self.cell(1, ago(hours=2), last_hours_old=24)
        self.place(1, 1, ago(days=9))
        self.assertEqual(self.liveness_of(), "unknown")

    def test_a_posting_with_no_date_posted_is_unknown(self) -> None:
        """Without a posting date there is no way to tell whether the window reached it."""
        self.job(date_posted=None)
        self.cell(1, ago(hours=2), last_hours_old=24)
        self.place(1, 1, ago(days=9))
        self.assertEqual(self.liveness_of(), "unknown")

    def test_a_cell_that_never_recorded_a_window_proves_nothing(self) -> None:
        """NULL last_hours_old is a zero-width window, not an unbounded one."""
        self.job(date_posted=ago(hours=30))
        self.cell(1, ago(hours=24), last_hours_old=None)
        self.place(1, 1, ago(days=3))
        self.assertEqual(self.liveness_of(), "unknown")

    def test_a_posting_nobody_has_looked_for_is_not_called_closed(self) -> None:
        """Absence is only evidence if we looked. ~26 of 252 cells rotate per day."""
        self.job(date_posted=ago(days=8))
        self.cell(1, ago(days=8), last_hours_old=24)
        self.place(1, 1, ago(days=8))
        self.assertEqual(self.liveness_of(), "stale")

    def test_a_posting_seen_in_the_latest_scrape_is_live(self) -> None:
        # `last_seen_at` records the run start and `last_success_at` the cell's completion,
        # so the two are minutes apart on a posting that WAS found. Without a grace window
        # every posting in the corpus reads as closed.
        self.job(date_posted=ago(hours=5))
        self.cell(1, ago(minutes=1), last_hours_old=24)
        self.place(1, 1, ago(minutes=10))
        self.assertEqual(self.liveness_of(), "live")

    def test_a_posting_with_no_cell_is_unknown(self) -> None:
        self.job(date_posted=ago(hours=5))
        self.place(1, None, ago(days=9))
        self.assertEqual(self.liveness_of(), "unknown")

    def test_every_job_appears_in_the_view_exactly_once(self) -> None:
        self.job(1, date_posted=ago(hours=5))
        self.job(2, date_posted=None)
        self.conn.commit()
        counts = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM jobs) AS jobs, "
            "(SELECT COUNT(*) FROM v_job_liveness) AS view"
        ).fetchone()
        self.assertEqual(counts["jobs"], counts["view"])

    # -- the two copies of the rule ----------------------------------------------------

    def test_the_inlined_case_agrees_with_the_view_on_every_state(self) -> None:
        """`database._LIVENESS_CASE` restates the view for speed. Restatements drift.

        One row per liveness state, checked through both paths at once. A change to either
        string alone fails here rather than in whichever half of the app happens to read
        the other one.
        """
        from findajob.core.database import Database

        rows = [
            (1, ago(hours=30), ago(hours=24), 24, ago(days=3)),  # likely_closed
            (2, ago(days=10), ago(hours=2), 24, ago(days=9)),  # unknown, aged out
            (3, ago(days=8), ago(days=8), 24, ago(days=8)),  # stale
            (4, ago(hours=5), ago(minutes=1), 24, ago(minutes=10)),  # live
        ]
        for job_id, date_posted, success, hours_old, last_seen in rows:
            self.job(job_id, date_posted=date_posted)
            self.cell(job_id, success, last_hours_old=hours_old)
            self.place(job_id, job_id, last_seen)

        inlined = self.conn.execute(
            f"SELECT jobs.id, {Database._LIVENESS_CASE} AS liveness "
            "  FROM jobs LEFT JOIN scrape_cells cell ON cell.id = jobs.scrape_cell_id"
        ).fetchall()
        self.assertEqual(
            {row["id"]: row["liveness"] for row in inlined},
            {job_id: self.liveness_of(job_id) for job_id, *_ in rows},
        )
        # Not a tautology only if the four rows really do cover four states.
        self.assertEqual(
            sorted({row["liveness"] for row in inlined}),
            ["likely_closed", "live", "stale", "unknown"],
        )


if __name__ == "__main__":
    unittest.main()
