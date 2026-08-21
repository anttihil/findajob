"""The backlog counter and the backlog selector must agree.

They did not. `_select` skips duplicates, stub descriptions and postings a re-scrape could
not find, but the line printed at the end of a run counted `pipeline_state = 'new'` -- a
population that includes all three. On a real corpus that reported 421 postings waiting
while the queue actually held 43, and the number could never reach zero no matter how many
runs drained it.

So the invariant worth pinning is not either query's output on its own: it is that
`_pending` equals `len(_select(...))` on a database built to contain every kind of row
that used to separate them. The second test pins the other half -- that the ineligible
rows are reported rather than silently dropped, and that their reasons sum to the total
even though a posting is routinely two of the three at once.

The `closed` dimension is exercised only through the shared predicate, not seeded
directly: `v_job_liveness` derives from scrape cells and observations, and a fixture that
faked it would pin the fake rather than the view.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from typing import cast
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.database import Database
from careerradar.core.migrations import apply_pragmas, migrate
from careerradar.profile.models import VERDICT_SCHEMA_VERSION
from careerradar.scoring.worker import _ineligible, _pending, _select

PROFILE = 4
LONG = "x" * 400


class _Db:
    """Just enough of `Database` for the three module functions under test."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn


class _Fixture:
    """Fixture only. Not a TestCase -- inheriting one re-runs its tests in every subclass."""

    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        apply_pragmas(self.conn)
        migrate(self.conn)
        # `_Db` is intentionally not a `Database` -- it is a minimal duck-typed stand-in,
        # since building a real one would open a second connection to the same file.
        self.db = cast(Database, _Db(self.conn))

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    def job(
        self,
        job_id: int,
        *,
        description: str | None = LONG,
        duplicate_of: int | None = None,
        state: str = "new",
        seniority: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, duplicate_of, "
            "pipeline_state, seniority, sync_run_id) "
            "VALUES (?, ?, 'Platform Engineer', ?, ?, ?, ?, ?, 1)",
            (
                job_id,
                f"k{job_id}",
                f"https://example.test/{job_id}",
                description,
                duplicate_of,
                state,
                seniority,
            ),
        )
        self.conn.commit()

    def verdict(self, job_id: int, schema_version: int, profile_version: int = PROFILE) -> None:
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, reason_type, "
            "reason_description, verdict_schema_version, created_at) "
            "VALUES (?, ?, 'm', 1, 'match', 'desc', ?, '2026-01-01T00:00:00+00:00')",
            (job_id, profile_version, schema_version),
        )
        self.conn.commit()


class BacklogAccountingTests(_Fixture, unittest.TestCase):
    def test_pending_matches_what_select_returns(self) -> None:
        self.job(1)  # never scored -> queued
        self.job(2)
        self.verdict(2, VERDICT_SCHEMA_VERSION)  # current verdict -> done
        self.job(3, state="scored")
        self.verdict(3, VERDICT_SCHEMA_VERSION - 1)  # stale schema -> queued
        self.job(4, duplicate_of=1)  # duplicate -> never
        self.job(5, description="too short")  # stub -> never
        self.job(6, description=None)  # no description -> never

        self.assertEqual(_pending(self.db, PROFILE), len(_select(self.db, None, False, PROFILE)))
        self.assertEqual(_pending(self.db, PROFILE), 2)

    def test_pending_ignores_verdicts_from_another_profile(self) -> None:
        self.job(1, state="scored")
        self.verdict(1, VERDICT_SCHEMA_VERSION, profile_version=PROFILE - 1)
        self.assertEqual(_pending(self.db, PROFILE), len(_select(self.db, None, False, PROFILE)))
        self.assertEqual(_pending(self.db, PROFILE), 1)

    def test_pending_stays_incremental_after_rescore_all(self) -> None:
        """--rescore-all widens what a run does, not what is left to do afterwards."""
        self.job(1, state="scored")
        self.verdict(1, VERDICT_SCHEMA_VERSION)
        self.assertEqual(len(_select(self.db, None, True, PROFILE)), 1)
        self.assertEqual(_pending(self.db, PROFILE), 0)

    def test_ineligible_reasons_sum_to_the_total(self) -> None:
        self.job(1)  # eligible, not counted here
        self.job(2, duplicate_of=1)
        self.job(3, duplicate_of=1, description="short")  # two reasons at once
        self.job(4, description="short")
        self.job(5, description=None)
        self.job(6, state="scored", duplicate_of=1)  # not 'new' -> not counted

        row = _ineligible(self.db)
        self.assertEqual(row["total"], 4)
        self.assertEqual(row["duplicate"], 2)
        self.assertEqual(row["thin"], 2)
        self.assertEqual(row["duplicate"] + row["thin"] + row["closed"], row["total"])

    def test_ineligible_is_disjoint_from_pending(self) -> None:
        """The two numbers a run prints must never describe the same row."""
        self.job(1)
        self.job(2, duplicate_of=1)
        self.job(3, description="short")

        queued = {row["id"] for row in _select(self.db, None, False, PROFILE)}
        excluded = {
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM jobs WHERE pipeline_state = 'new' AND (duplicate_of IS NOT NULL "
                "OR description IS NULL OR length(description) <= 200)"
            )
        }
        self.assertEqual(queued & excluded, set())
        self.assertEqual(_ineligible(self.db)["total"], len(excluded))


class SenioritySkipTests(_Fixture, unittest.TestCase):
    """`scoring.skip_seniority` must move rows from pending to excluded, never lose them.

    The levels are a yield decision (config.yaml records the measurement), so these tests
    patch the config rather than asserting today's list: what is pinned is the mechanism --
    the selector and the counter still agree, and a skipped posting is reported with a
    reason instead of vanishing from both numbers.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(
            "careerradar.scoring.worker.load_config",
            return_value={"scoring": {"skip_seniority": ["lead", "staff"]}},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_skipped_levels_leave_the_queue(self) -> None:
        self.job(1, seniority="mid")
        self.job(2, seniority="unspecified")
        self.job(3, seniority=None)  # NULL reads as unspecified, not as skipped
        self.job(4, seniority="lead")
        self.job(5, seniority="staff")

        queued = {row["id"] for row in _select(self.db, None, False, PROFILE)}
        self.assertEqual(queued, {1, 2, 3})
        self.assertEqual(_pending(self.db, PROFILE), len(queued))

    def test_skipped_rows_are_reported_not_dropped(self) -> None:
        self.job(1, seniority="mid")
        self.job(2, seniority="lead")
        self.job(3, seniority="staff", duplicate_of=1)  # duplicate wins on precedence

        row = _ineligible(self.db)
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["seniority"], 1)
        self.assertEqual(row["duplicate"], 1)
        self.assertEqual(
            row["duplicate"] + row["thin"] + row["closed"] + row["seniority"], row["total"]
        )

    def test_skipped_and_pending_stay_disjoint(self) -> None:
        for job_id, level in enumerate(("mid", "lead", "staff", "senior"), start=1):
            self.job(job_id, seniority=level)

        queued = {row["id"] for row in _select(self.db, None, False, PROFILE)}
        self.assertEqual(queued, {1, 4})
        self.assertEqual(_ineligible(self.db)["total"], 2)

    def test_rescore_all_still_honours_the_skip(self) -> None:
        """--rescore-all widens which verdicts are redone, not which levels are judged."""
        self.job(1, seniority="mid", state="scored")
        self.job(2, seniority="lead", state="scored")
        self.verdict(1, VERDICT_SCHEMA_VERSION)
        self.verdict(2, VERDICT_SCHEMA_VERSION)

        self.assertEqual({row["id"] for row in _select(self.db, None, True, PROFILE)}, {1})


class EmptySenioritySkipTests(_Fixture, unittest.TestCase):
    """An empty list must be a no-op, not a clause that excludes everything."""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch(
            "careerradar.scoring.worker.load_config",
            return_value={"scoring": {"skip_seniority": []}},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_level_is_skipped(self) -> None:
        self.job(1, seniority="lead")
        self.job(2, seniority="staff")

        self.assertEqual(len(_select(self.db, None, False, PROFILE)), 2)
        self.assertEqual(_pending(self.db, PROFILE), 2)
        self.assertEqual(_ineligible(self.db)["total"], 0)


if __name__ == "__main__":
    unittest.main()


class QuarantineTests(_Fixture, unittest.TestCase):
    """A posting that fails the same way every run stops being offered.

    The retry design assumes failure is a draw -- a rate limit, or the model answering in
    prose. Some failures are a property of the posting: a record that is not a job ad
    extracts no requirements, the validator rejects it, and that outcome is identical on
    every attempt of every run. Three calls a run, indefinitely, with nothing recorded.
    """

    def record_failures(
        self, job_id: int, times: int, error: str = "core_requirements is empty"
    ) -> None:
        from careerradar.scoring.worker import _record_failure

        for _ in range(times):
            _record_failure(self.db, job_id, error)
        self.conn.commit()

    def test_posting_is_offered_until_the_threshold(self) -> None:
        from careerradar.scoring.worker import MAX_SCORING_FAILURES

        self.job(1)
        for n in range(MAX_SCORING_FAILURES):
            self.assertEqual(_pending(self.db, PROFILE), 1, f"withdrawn after only {n} failures")
            self.record_failures(1, 1)
        self.assertEqual(_pending(self.db, PROFILE), 0)
        self.assertEqual(_select(self.db, None, False, PROFILE), [])

    def test_rescore_all_does_not_resurrect_a_quarantined_posting(self) -> None:
        """--rescore-all widens the verdict predicate, not the eligibility one."""
        from careerradar.scoring.worker import MAX_SCORING_FAILURES

        self.job(1)
        self.record_failures(1, MAX_SCORING_FAILURES)
        self.assertEqual(_select(self.db, None, True, PROFILE), [])

    def test_a_success_clears_the_counter(self) -> None:
        """Two failures then a verdict must not leave the posting one failure from exile."""
        from careerradar.scoring.worker import MAX_SCORING_FAILURES, _persist

        self.job(1)
        self.record_failures(1, MAX_SCORING_FAILURES - 1)
        _persist(self.db, {"id": 1}, _VERDICT, None, 0.0, "m", PROFILE, "hash")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT scoring_failures, last_scoring_error FROM jobs WHERE id = 1"
        ).fetchone()
        self.assertEqual(row["scoring_failures"], 0)
        self.assertIsNone(row["last_scoring_error"])

    def test_quarantined_groups_by_reason(self) -> None:
        from careerradar.scoring.worker import MAX_SCORING_FAILURES, _quarantined

        self.job(1)
        self.job(2)
        self.job(3)
        self.record_failures(1, MAX_SCORING_FAILURES, "core_requirements is empty")
        self.record_failures(2, MAX_SCORING_FAILURES, "core_requirements is empty")
        self.record_failures(3, MAX_SCORING_FAILURES, "429 rate limited")

        rows = _quarantined(self.db)
        self.assertEqual(sum(r["total"] for r in rows), 3)
        self.assertEqual(rows[0]["total"], 2)
        self.assertIn("core_requirements", rows[0]["reason"])

    def test_retry_clears_the_counter(self) -> None:
        from careerradar.scoring.worker import MAX_SCORING_FAILURES

        self.job(1)
        self.record_failures(1, MAX_SCORING_FAILURES)
        self.assertEqual(_pending(self.db, PROFILE), 0)
        self.conn.execute(
            "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL "
            "WHERE COALESCE(scoring_failures, 0) > 0"
        )
        self.conn.commit()
        self.assertEqual(_pending(self.db, PROFILE), 1)


_VERDICT = {
    "fit_score": 50,
    "verdict": "maybe",
    "seniority_gap": "match",
    "hard_blockers": [],
    "key_gaps": [],
    "strengths": [],
    "reasoning": "r",
    "research_worthy": False,
    "role_summary": "s",
    "eligibility": "eligible",
    "role_match": "same_role",
    "capability_match": "meets",
    "evidence_quality": "direct",
    "core_requirements": [],
    "requirement_assessments": [],
    "audit_flags": [],
    "scale_version": 1,
    "pareto_tier": 1,
}
