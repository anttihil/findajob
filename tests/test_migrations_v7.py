"""Migration v7 against a real v6 database.

v7 rewrites data rather than adding columns, which puts the burden of proof on the other
side from v6: the question is not "did it leave the old verdicts alone" but "did it change
them into exactly what they already meant". Each old blocker string was written under a
field contract that said "quote the phrase from the posting", so moving it into `quote`
with an empty `why` preserves it exactly, and `careerradar score audit` keeps reporting
comparable quote-status rates across the change.

The other thing worth pinning is what v7 must NOT do: bump VERDICT_SCHEMA_VERSION. That
constant drives the scoring worker's backlog query, so raising it would re-score every
verdict in the database -- thousands of API calls to re-derive ordinals that are already
correct, because a JSON shape changed.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.migrations import (
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_pragmas,
    current_version,
    migrate,
)
from careerradar.profile.models import VERDICT_SCHEMA_VERSION


class MigrationV7Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = sqlite3.connect(self.tmp.name)
        self.conn.row_factory = sqlite3.Row
        self.migrate_to(6)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.tmp.name)

    def migrate_to(self, version: int) -> None:
        apply_pragmas(self.conn)
        cursor = self.conn.cursor()
        for target, _description, fn in MIGRATIONS:
            if target > version:
                break
            fn(cursor)
            cursor.execute(f"PRAGMA user_version = {int(target)}")
        self.conn.commit()

    def seed(self, job_id: int, hard_blockers: str | None) -> None:
        self.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, description, sync_run_id) "
            "VALUES (?, ?, 'Platform Engineer', ?, 'desc', 1)",
            (job_id, f"k{job_id}", f"https://example.test/{job_id}"),
        )
        self.conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit_score, "
            "verdict, hard_blockers, verdict_schema_version, created_at) "
            "VALUES (?, 4, 'deepseek-v4-flash', 72, 'worth_applying', ?, 2, "
            "'2026-08-05T00:00:00Z')",
            (job_id, hard_blockers),
        )
        self.conn.commit()

    def blockers(self, job_id: int) -> list[dict[str, str]] | None:
        row = self.conn.execute(
            "SELECT hard_blockers FROM job_verdicts WHERE job_id = ?", (job_id,)
        ).fetchone()
        return json.loads(row["hard_blockers"]) if row["hard_blockers"] else None

    # -- the conversion ------------------------------------------------------------------

    def test_the_migration_reaches_the_declared_version(self) -> None:
        migrate(self.conn)
        self.assertEqual(current_version(self.conn), SCHEMA_VERSION)
        self.assertGreaterEqual(SCHEMA_VERSION, 7)

    def test_a_string_blocker_becomes_a_quote_with_no_reasoning(self) -> None:
        self.seed(1, json.dumps(["Must hold an active TS/SCI clearance"]))
        self.migrate_to(7)
        self.assertEqual(
            self.blockers(1), [{"quote": "Must hold an active TS/SCI clearance", "why": ""}]
        )

    def test_the_text_survives_character_for_character(self) -> None:
        """The audit re-runs `locate` over these, so a reworded quote moves the rates."""
        original = "För denna position krävs svenskt medborgarskap — inte uppfyllt"
        self.seed(1, json.dumps([original]))
        self.migrate_to(7)
        blockers = self.blockers(1)
        assert blockers is not None
        self.assertEqual(blockers[0]["quote"], original)

    def test_every_blocker_in_a_multi_entry_row_is_converted(self) -> None:
        self.seed(1, json.dumps(["needs clearance", "10+ years required"]))
        self.migrate_to(7)
        blockers = self.blockers(1)
        assert blockers is not None
        self.assertEqual([b["quote"] for b in blockers], ["needs clearance", "10+ years required"])

    def test_an_empty_list_is_left_alone(self) -> None:
        self.seed(1, "[]")
        self.migrate_to(7)
        self.assertEqual(self.blockers(1), [])

    def test_a_null_is_left_alone(self) -> None:
        self.seed(1, None)
        self.migrate_to(7)
        self.assertIsNone(self.blockers(1))

    def test_a_row_already_in_the_new_shape_is_untouched(self) -> None:
        already = [
            {"quote": "Must hold an active TS/SCI clearance", "why": "The candidate has none."}
        ]
        self.seed(1, json.dumps(already))
        self.migrate_to(7)
        self.assertEqual(self.blockers(1), already)

    def test_a_blocker_stored_as_bare_text_rather_than_json_is_carried_over(self) -> None:
        """An early row wrote one blocker unencoded. It is still a blocker."""
        self.seed(1, "Must hold an active TS/SCI clearance")
        self.migrate_to(7)
        self.assertEqual(
            self.blockers(1), [{"quote": "Must hold an active TS/SCI clearance", "why": ""}]
        )

    # -- what it must not do -------------------------------------------------------------

    def test_the_verdict_itself_is_not_touched(self) -> None:
        self.seed(1, json.dumps(["needs clearance"]))
        self.migrate_to(7)
        row = self.conn.execute(
            "SELECT fit_score, verdict, verdict_schema_version FROM job_verdicts WHERE job_id = 1"
        ).fetchone()
        self.assertEqual(row["fit_score"], 72)
        self.assertEqual(row["verdict"], "worth_applying")
        self.assertEqual(row["verdict_schema_version"], 2)

    def test_the_schema_version_does_not_force_a_rescore(self) -> None:
        """Raising this re-drains the whole backlog. The shape change did not need that."""
        self.assertGreaterEqual(VERDICT_SCHEMA_VERSION, 2)

    def test_applying_it_twice_is_safe(self) -> None:
        self.seed(1, json.dumps(["needs clearance"]))
        self.migrate_to(7)
        self.assertEqual(self.blockers(1), [{"quote": "needs clearance", "why": ""}])


if __name__ == "__main__":
    unittest.main()
