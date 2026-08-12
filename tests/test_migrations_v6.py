"""Migration v6 against a real v5 database.

v6 is additive, which makes the interesting assertions the ones about what it must NOT do.
The 5,511 verdicts already in the live database cost real money and are the only
cross-version evidence in the system -- the profile v3-vs-v4 drift measurement came from
them -- so they have to survive, and they have to stay distinguishable from verdicts scored
under a computed scale. `scale_version = 0` is what carries that distinction, and a
default that silently marked old rows as current would destroy the one thing making the
comparison possible.

The liveness view gets the same scrutiny for the opposite reason: it must not call a
posting closed on the strength of nobody having looked.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.migrations import (  # noqa: E402
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_pragmas,
    current_version,
    migrate,
)


class MigrationV6Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.migrate_to(5)

    def tearDown(self):
        self.conn.close()
        os.unlink(self.tmp.name)

    def migrate_to(self, version):
        apply_pragmas(self.conn)
        cursor = self.conn.cursor()
        for target, _description, fn in MIGRATIONS:
            if target > version:
                break
            fn(cursor)
            cursor.execute(f"PRAGMA user_version = {int(target)}")
        self.conn.commit()

    def columns(self, table):
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def seed_verdict(self, job_id=1, fit_score=72):
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, sync_run_id) "
            "VALUES (?, ?, 'Platform Engineer', ?, 'desc', 1)",
            (job_id, f"k{job_id}", f"https://example.test/{job_id}"),
        )
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit_score, "
            "verdict, created_at) VALUES (?, 4, 'deepseek-v4-flash', ?, "
            "'worth_applying', '2026-08-05T00:00:00Z')",
            (job_id, fit_score),
        )
        self.conn.commit()

    # -- schema ------------------------------------------------------------------------

    def test_the_migration_reaches_the_declared_version(self):
        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 6)

    def test_the_ordinal_columns_exist(self):
        migrate(self.conn)
        expected = {"role_summary", "eligibility", "role_match", "capability_match",
                    "seniority_gap", "evidence_quality", "core_requirements",
                    "requirement_assessments", "audit_flags", "scale_version",
                    "verdict_schema_version", "pareto_tier", "prompt_hash"}
        self.assertTrue(expected <= self.columns("job_verdicts"))

    def test_the_coverage_and_liveness_columns_exist(self):
        migrate(self.conn)
        expected = {"matched_count", "required_count", "scorer_version",
                    "last_seen_at", "times_seen"}
        self.assertTrue(expected <= self.columns("jobs"))

    def test_applying_it_twice_is_safe(self):
        migrate(self.conn)
        self.assertEqual(migrate(self.conn), 0)

    # -- the old verdicts --------------------------------------------------------------

    def test_existing_verdicts_survive(self):
        self.seed_verdict()
        migrate(self.conn)
        row = self.conn.execute(
            "SELECT fit_score, verdict FROM job_verdicts WHERE job_id = 1").fetchone()
        self.assertEqual(row["fit_score"], 72)
        self.assertEqual(row["verdict"], "worth_applying")

    def test_existing_verdicts_are_marked_as_a_different_scale(self):
        """Scale 0 means the model emitted that number. Losing this loses the comparison."""
        self.seed_verdict()
        migrate(self.conn)
        row = self.conn.execute(
            "SELECT scale_version, verdict_schema_version FROM job_verdicts "
            "WHERE job_id = 1").fetchone()
        self.assertEqual(row["scale_version"], 0)
        self.assertEqual(row["verdict_schema_version"], 1)

    def test_old_verdicts_carry_no_ordinals(self):
        self.seed_verdict()
        migrate(self.conn)
        row = self.conn.execute(
            "SELECT eligibility, role_match, pareto_tier FROM job_verdicts "
            "WHERE job_id = 1").fetchone()
        self.assertIsNone(row["eligibility"])
        self.assertIsNone(row["role_match"])
        self.assertIsNone(row["pareto_tier"])

    def test_last_seen_at_is_backfilled_rather_than_left_null(self):
        self.conn.execute(
            "INSERT INTO sync_runs (id, started_at, status) "
            "VALUES (1, '2026-08-05T22:50:00Z', 'ok')")
        self.seed_verdict()
        migrate(self.conn)
        row = self.conn.execute("SELECT last_seen_at FROM jobs WHERE id = 1").fetchone()
        self.assertEqual(row["last_seen_at"], "2026-08-05T22:50:00Z")

    # -- liveness ----------------------------------------------------------------------

    def liveness_of(self, job_id):
        return self.conn.execute(
            "SELECT liveness FROM v_job_liveness WHERE job_id = ?", (job_id,)
        ).fetchone()["liveness"]

    def add_cell(self, cell_id, last_success_at):
        self.conn.execute(
            "INSERT INTO scrape_cells (id, source, role_family, location_id, query, "
            "tier, last_success_at, created_at) VALUES (?, 'indeed', 'sre', 'la', 'q', "
            "'core', ?, '2026-07-01T00:00:00Z')",
            (cell_id, last_success_at),
        )

    def place(self, job_id, cell_id, last_seen_at):
        self.conn.execute(
            "UPDATE jobs SET scrape_cell_id = ?, last_seen_at = ? WHERE id = ?",
            (cell_id, last_seen_at, job_id),
        )
        self.conn.commit()

    def test_a_posting_absent_from_a_later_scrape_is_likely_closed(self):
        self.seed_verdict()
        migrate(self.conn)
        self.add_cell(1, "2026-08-05T00:00:00Z")
        self.place(1, 1, "2026-07-30T00:00:00Z")
        self.assertEqual(self.liveness_of(1), "likely_closed")

    def test_a_posting_seen_in_the_latest_scrape_is_live(self):
        self.seed_verdict()
        migrate(self.conn)
        # `last_seen_at` records the run start and `last_success_at` the cell's completion,
        # so the two are minutes apart on a posting that WAS found. Without a grace window
        # every posting in the corpus reads as closed.
        self.add_cell(1, "2026-08-05T22:59:00Z")
        self.place(1, 1, "2026-08-05T22:50:00Z")
        self.assertEqual(self.liveness_of(1), "live")

    def test_a_posting_nobody_has_looked_for_is_not_called_closed(self):
        """Absence is only evidence if we looked. ~26 of 252 cells rotate per day."""
        self.seed_verdict()
        migrate(self.conn)
        self.add_cell(1, "2026-07-30T00:00:00Z")
        self.place(1, 1, "2026-07-30T00:00:00Z")
        self.assertEqual(self.liveness_of(1), "stale")

    def test_a_posting_with_no_cell_is_unknown(self):
        self.seed_verdict()
        migrate(self.conn)
        self.place(1, None, "2026-07-30T00:00:00Z")
        self.assertEqual(self.liveness_of(1), "unknown")

    def test_every_job_appears_in_the_view_exactly_once(self):
        self.seed_verdict(1)
        self.seed_verdict(2)
        migrate(self.conn)
        counts = self.conn.execute(
            "SELECT (SELECT COUNT(*) FROM jobs) AS jobs, "
            "(SELECT COUNT(*) FROM v_job_liveness) AS view").fetchone()
        self.assertEqual(counts["jobs"], counts["view"])


if __name__ == "__main__":
    unittest.main()
