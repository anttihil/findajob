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

from findajob.core.database import Database
from findajob.core.migrations import apply_pragmas, migrate
from findajob.profile.models import VERDICT_SCHEMA_VERSION
from findajob.scoring.worker import _ineligible, _pending, _select

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

    def verdict(
        self,
        job_id: int,
        schema_version: int,
        profile_version: int = PROFILE,
        priority: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, reason_type, "
            "reason_description, verdict_schema_version, priority, created_at) "
            "VALUES (?, ?, 'm', ?, 'match', 'desc', ?, ?, '2026-01-01T00:00:00+00:00')",
            (job_id, profile_version, None if priority else 1, schema_version, priority),
        )
        self.conn.commit()


class BacklogAccountingTests(_Fixture, unittest.TestCase):
    def test_pending_matches_what_select_returns(self) -> None:
        self.job(1)  # never scored -> queued
        self.job(2)
        self.verdict(2, VERDICT_SCHEMA_VERSION, priority="consider")  # priority -> done
        self.job(3, state="scored")
        self.verdict(3, VERDICT_SCHEMA_VERSION - 1)  # binary verdict -> queued
        self.job(4, duplicate_of=1)  # duplicate -> never
        self.job(5, description="too short")  # stub -> never
        self.job(6, description=None)  # no description -> never

        self.assertEqual(_pending(self.db), len(_select(self.db, None)))
        self.assertEqual(_pending(self.db), 2)
        self.assertEqual({job["id"] for job in _select(self.db, None)}, {1, 3})

    def test_pending_excludes_priorities_from_another_profile(self) -> None:
        self.job(1, state="scored")
        self.verdict(1, VERDICT_SCHEMA_VERSION - 1, profile_version=PROFILE - 1, priority="low")
        self.assertEqual(_pending(self.db), len(_select(self.db, None)))
        self.assertEqual(_pending(self.db), 0)

    def test_binary_verdicts_remain_pending_regardless_of_schema_or_profile(self) -> None:
        self.job(1, state="scored")
        self.job(2, state="scored")
        self.verdict(1, VERDICT_SCHEMA_VERSION)
        self.verdict(2, VERDICT_SCHEMA_VERSION - 1, profile_version=PROFILE - 1)
        self.conn.execute("UPDATE job_verdicts SET fit=0 WHERE job_id=2")
        self.assertEqual({job["id"] for job in _select(self.db, None)}, {1, 2})
        self.assertEqual(_pending(self.db), 2)

    def test_scored_jobs_without_verdicts_remain_pending(self) -> None:
        self.job(1, state="scored")
        self.job(2, state="scored")
        self.assertEqual({job["id"] for job in _select(self.db, None)}, {1, 2})
        self.assertEqual(_pending(self.db), 2)

    def test_binary_verdicts_still_obey_eligibility_and_quarantine(self) -> None:
        from findajob.scoring.repository import MAX_SCORING_FAILURES

        self.job(1, state="scored")
        self.job(2, state="scored", duplicate_of=1)
        self.job(3, state="scored", description="short")
        self.job(4, state="scored")
        for job_id in range(1, 5):
            self.verdict(job_id, VERDICT_SCHEMA_VERSION - 1)
        self.conn.execute("UPDATE jobs SET scoring_failures=? WHERE id=4", (MAX_SCORING_FAILURES,))
        self.assertEqual([job["id"] for job in _select(self.db, None)], [1])
        self.assertEqual(_pending(self.db), 1)

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

        queued = {row["id"] for row in _select(self.db, None)}
        excluded = {
            row[0]
            for row in self.conn.execute(
                "SELECT id FROM jobs WHERE pipeline_state = 'new' AND (duplicate_of IS NOT NULL "
                "OR description IS NULL OR length(description) <= 200)"
            )
        }
        self.assertEqual(queued & excluded, set())
        self.assertEqual(_ineligible(self.db)["total"], len(excluded))


class SeniorityEligibilityTests(_Fixture, unittest.TestCase):
    def test_seniority_never_excludes_a_posting_from_scoring(self) -> None:
        for job_id, level in enumerate(("mid", "lead", "staff", "senior"), start=1):
            self.job(job_id, seniority=level)

        queued = {row["id"] for row in _select(self.db, None)}
        self.assertEqual(queued, {1, 2, 3, 4})
        self.assertEqual(_pending(self.db), len(queued))


class PostingAgeTests(_Fixture, unittest.TestCase):
    def test_newest_posting_first_with_discovery_fallback_and_stable_ties(self) -> None:
        dates = (
            (1, "2026-08-01T12:00:00Z", "2026-08-20T12:00:00Z"),
            (2, "2026-08-15T12:00:00Z", "2026-08-16T12:00:00Z"),
            (3, None, "2026-08-10T12:00:00Z"),
            (4, "2026-08-15T05:00:00-07:00", "2026-08-19T12:00:00Z"),
        )
        for job_id, posted, found in dates:
            self.job(job_id, state="scored")
            self.verdict(job_id, VERDICT_SCHEMA_VERSION - 1)
            self.conn.execute(
                "UPDATE jobs SET date_posted=?, date_found=? WHERE id=?", (posted, found, job_id)
            )
        self.assertEqual([row["id"] for row in _select(self.db, None)], [4, 2, 3, 1])
        self.assertEqual([row["id"] for row in _select(self.db, 2)], [4, 2])
        self.assertEqual(_pending(self.db), 4)

    def test_old_unscored_postings_are_selected(self) -> None:
        # Fresh posting (today)
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, "
            "pipeline_state, date_found, sync_run_id) "
            "VALUES (1, 'k1', 'Platform Engineer', 'https://example.test/1', ?, "
            "'new', datetime('now'), 1)",
            (LONG,),
        )
        # A long-running posting first discovered 60 days ago is still worth assessing.
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, "
            "pipeline_state, date_found, sync_run_id) "
            "VALUES (2, 'k2', 'Platform Engineer', 'https://example.test/2', ?, "
            "'new', datetime('now', '-60 days'), 1)",
            (LONG,),
        )
        self.conn.commit()

        queued = [r["id"] for r in _select(self.db, None, include_closed=False)]
        self.assertEqual(queued, [1, 2])
        self.assertEqual(_pending(self.db), 2)

    def test_newly_discovered_job_with_old_posting_date_is_selected(self) -> None:
        self.job(1)
        self.conn.execute(
            "UPDATE jobs SET date_found=datetime('now'), "
            "date_posted=datetime('now', '-90 days') WHERE id=1"
        )
        # Retired local overrides must not silently reinstate the age cutoff.
        with mock.patch(
            "findajob.scoring.worker.load_config",
            return_value={"scoring": {"max_posting_age_days": 3}},
        ):
            self.assertEqual([r["id"] for r in _select(self.db, None)], [1])
            self.assertEqual(_pending(self.db), 1)


class QuarantineTests(_Fixture, unittest.TestCase):
    """A posting that fails the same way every run stops being offered."""

    def record_failures(
        self, job_id: int, times: int, error: str = "core_requirements is empty"
    ) -> None:
        from findajob.scoring.worker import _record_failure

        for _ in range(times):
            _record_failure(self.db, job_id, error)
        self.conn.commit()

    def test_posting_is_offered_until_the_threshold(self) -> None:
        from findajob.scoring.worker import MAX_SCORING_FAILURES

        self.job(1)
        for n in range(MAX_SCORING_FAILURES):
            self.assertEqual(_pending(self.db), 1, f"withdrawn after only {n} failures")
            self.record_failures(1, 1)
        self.assertEqual(_pending(self.db), 0)
        self.assertEqual(_select(self.db, None), [])

    def test_a_success_clears_the_counter(self) -> None:
        """Two failures then a verdict must not leave the posting one failure from exile."""
        from findajob.scoring.worker import MAX_SCORING_FAILURES, _persist

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
        from findajob.scoring.worker import MAX_SCORING_FAILURES, _quarantined

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
        from findajob.scoring.worker import MAX_SCORING_FAILURES

        self.job(1)
        self.record_failures(1, MAX_SCORING_FAILURES)
        self.assertEqual(_pending(self.db), 0)
        self.conn.execute(
            "UPDATE jobs SET scoring_failures = 0, last_scoring_error = NULL "
            "WHERE COALESCE(scoring_failures, 0) > 0"
        )
        self.conn.commit()
        self.assertEqual(_pending(self.db), 1)


class RecentVerdictsTests(_Fixture, unittest.TestCase):
    def test_count_recent_verdicts_handles_iso_timestamps(self) -> None:
        from datetime import datetime, timedelta, timezone

        from findajob.scoring.repository import count_recent_verdicts

        now = datetime.now(timezone.utc)
        self.job(1)
        self.job(2)

        # Verdict created 3 hours ago (ISO 8601 with 'T' on same day)
        three_hours_ago = (now - timedelta(hours=3)).isoformat()
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, reason_type, "
            "reason_description, verdict_schema_version, created_at) "
            "VALUES (1, ?, 'm', 1, 'match', 'desc', ?, ?)",
            (PROFILE, VERDICT_SCHEMA_VERSION, three_hours_ago),
        )

        # Verdict created 2 minutes ago
        two_mins_ago = (now - timedelta(minutes=2)).isoformat()
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, reason_type, "
            "reason_description, verdict_schema_version, created_at) "
            "VALUES (2, ?, 'm', 1, 'match', 'desc', ?, ?)",
            (PROFILE, VERDICT_SCHEMA_VERSION, two_mins_ago),
        )
        self.conn.commit()

        self.assertEqual(count_recent_verdicts(self.conn, minutes=5), 1)
        self.assertEqual(count_recent_verdicts(self.conn, minutes=240), 2)


class StatusReportBacklogTests(_Fixture, unittest.TestCase):
    def test_collect_status_report_counts_only_eligible_pending(self) -> None:
        from findajob.core.status_repository import collect_status_report

        # 2 eligible pending jobs
        self.job(1)
        self.job(2)

        # Ineligible jobs in state 'new'
        self.job(3, duplicate_of=1)
        self.job(4, description="short")
        self.job(5, description=None)

        report = collect_status_report(self.conn)
        self.assertEqual(report["score"]["backlog"], 2)
        self.assertEqual(report["score"]["backlog_share"], 1.0)

    def test_status_counts_jobs_scored_under_any_profile_once(self) -> None:
        from findajob.core.status_repository import collect_status_report, get_pipeline_status

        self.job(1)
        self.job(2)
        self.verdict(
            2, VERDICT_SCHEMA_VERSION - 1, profile_version=PROFILE - 1, priority="consider"
        )
        self.verdict(2, VERDICT_SCHEMA_VERSION, profile_version=PROFILE, priority="first")

        for score in (
            collect_status_report(self.conn)["score"],
            get_pipeline_status(self.conn, False)["score"],
        ):
            self.assertEqual(score["backlog"], 1)
            self.assertEqual(score["backlog_share"], 0.5)

    def test_status_does_not_count_pending_binary_verdicts_as_completed_priorities(self) -> None:
        from findajob.core.status_repository import collect_status_report, get_pipeline_status

        self.job(1)
        self.job(2, state="scored")
        self.verdict(2, VERDICT_SCHEMA_VERSION - 1)
        self.job(3, state="scored")
        self.verdict(3, VERDICT_SCHEMA_VERSION, priority="first")
        for score in (
            collect_status_report(self.conn)["score"],
            get_pipeline_status(self.conn, False)["score"],
        ):
            self.assertEqual(score["backlog"], 2)
            self.assertAlmostEqual(score["backlog_share"], 2 / 3)


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


if __name__ == "__main__":
    unittest.main()
