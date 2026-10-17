"""SQLite persistence for immutable labeling examples and mutable human annotations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from findajob.core.paths import DATA_DIR

SCHEMA_VERSION = 2
OUTCOMES = frozenset({"apply", "stretch", "pass", "insufficient"})
DECISIVE_REASONS = frozenset(
    {
        "eligibility",
        "specialization",
        "seniority",
        "role_domain",
        "location_compensation",
        "preference",
        "other",
    }
)
SPLITS = frozenset({"optimization", "validation", "held_out"})


def default_labeler_db_path() -> str:
    return os.path.abspath(
        os.path.expanduser(
            os.path.expandvars(
                os.environ.get("FIND_A_JOB_LABELER_DB_PATH", os.path.join(DATA_DIR, "labeler.db"))
            )
        )
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def family_split(family_key: str) -> str:
    """Assign related examples to one stable split.

    The hash is deliberately independent of import order, database row IDs, and runtime
    randomness. A family can be manually promoted to held-out later, and every member moves
    together because the assignment is stored once per family.
    """
    bucket = int(hashlib.sha256(family_key.encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "optimization"
    return "validation"


class LabelerDatabase:
    """A short-lived connection to the standalone labeler database."""

    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(path or default_labeler_db_path())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> LabelerDatabase:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"Labeler database schema {version} is newer than supported {SCHEMA_VERSION}."
            )
        if version == 0:
            self.conn.executescript(
                """
                CREATE TABLE examples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_hash TEXT NOT NULL UNIQUE,
                    source_job_id TEXT NOT NULL,
                    source_db_path TEXT NOT NULL,
                    source_db_identity TEXT NOT NULL,
                    source_profile_version INTEGER,
                    imported_at TEXT NOT NULL,
                    family_key TEXT NOT NULL,
                    queue_bucket TEXT NOT NULL,
                    title TEXT NOT NULL,
                    company TEXT,
                    location TEXT,
                    url TEXT,
                    description TEXT NOT NULL,
                    date_posted TEXT,
                    job_snapshot_json TEXT NOT NULL,
                    profile_text TEXT NOT NULL,
                    profile_snapshot_json TEXT NOT NULL,
                    rubric_version TEXT,
                    model TEXT,
                    model_outcome TEXT,
                    model_reason_type TEXT,
                    model_reason_description TEXT,
                    model_verdict_json TEXT NOT NULL,
                    source_metadata_json TEXT NOT NULL
                );

                CREATE INDEX idx_examples_family ON examples(family_key);
                CREATE INDEX idx_examples_source_job ON examples(source_job_id);

                CREATE TABLE dataset_assignments (
                    family_key TEXT PRIMARY KEY,
                    split TEXT NOT NULL CHECK(split IN ('optimization', 'validation', 'held_out')),
                    assigned_at TEXT NOT NULL,
                    assignment_method TEXT NOT NULL
                );

                CREATE TABLE annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    example_id INTEGER NOT NULL REFERENCES examples(id),
                    review_round INTEGER NOT NULL DEFAULT 1 CHECK(review_round > 0),
                    outcome TEXT NOT NULL
                        CHECK(outcome IN ('apply', 'stretch', 'pass', 'insufficient')),
                    decisive_reason TEXT CHECK(
                        decisive_reason IS NULL OR decisive_reason IN (
                            'eligibility', 'specialization', 'seniority', 'role_domain',
                            'location_compensation', 'preference', 'other'
                        )
                    ),
                    confidence INTEGER CHECK(confidence IS NULL OR confidence BETWEEN 1 AND 5),
                    notes TEXT NOT NULL DEFAULT '',
                    agreement INTEGER CHECK(agreement IS NULL OR agreement IN (0, 1)),
                    model_revealed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(example_id, review_round)
                );

                CREATE TABLE queue_items (
                    queue_name TEXT NOT NULL,
                    example_id INTEGER NOT NULL REFERENCES examples(id),
                    review_round INTEGER NOT NULL DEFAULT 1 CHECK(review_round > 0),
                    position INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'completed', 'skipped')),
                    added_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY(queue_name, example_id, review_round),
                    UNIQUE(queue_name, position)
                );

                CREATE INDEX idx_queue_pending
                    ON queue_items(queue_name, status, position);

                CREATE TABLE export_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version TEXT NOT NULL UNIQUE,
                    output_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    example_count INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL
                );

                -- Imported evidence is an immutable snapshot. Queue, split, and annotation
                -- state deliberately live in separate tables so normal review never needs to
                -- mutate it.
                CREATE TRIGGER examples_immutable_update
                    BEFORE UPDATE ON examples BEGIN
                    SELECT RAISE(ABORT, 'example snapshots are immutable');
                END;
                CREATE TRIGGER examples_immutable_delete
                    BEFORE DELETE ON examples BEGIN
                    SELECT RAISE(ABORT, 'example snapshots are immutable');
                END;

                PRAGMA user_version = 1;
                """
            )
            self.conn.commit()

        if version < 2:
            self.conn.executescript(
                """
                CREATE TABLE consistency_resolutions (
                    example_id INTEGER PRIMARY KEY REFERENCES examples(id),
                    selected_round INTEGER NOT NULL CHECK(selected_round IN (1, 2)),
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(example_id, selected_round)
                        REFERENCES annotations(example_id, review_round)
                );
                PRAGMA user_version = 2;
                """
            )
            self.conn.commit()

    def insert_example(self, example: dict[str, Any], queue_name: str = "initial") -> int | None:
        """Insert one snapshot and queue it, returning None when already imported."""
        now = utc_now()
        try:
            cursor = self.conn.execute(
                """
                INSERT INTO examples (
                    snapshot_hash, source_job_id, source_db_path, source_db_identity,
                    source_profile_version, imported_at, family_key, queue_bucket,
                    title, company, location, url, description, date_posted,
                    job_snapshot_json, profile_text, profile_snapshot_json, rubric_version,
                    model, model_outcome, model_reason_type, model_reason_description,
                    model_verdict_json, source_metadata_json
                ) VALUES (
                    :snapshot_hash, :source_job_id, :source_db_path, :source_db_identity,
                    :source_profile_version, :imported_at, :family_key, :queue_bucket,
                    :title, :company, :location, :url, :description, :date_posted,
                    :job_snapshot_json, :profile_text, :profile_snapshot_json, :rubric_version,
                    :model, :model_outcome, :model_reason_type, :model_reason_description,
                    :model_verdict_json, :source_metadata_json
                )
                """,
                example,
            )
        except sqlite3.IntegrityError as exc:
            if "snapshot_hash" in str(exc) or "UNIQUE constraint failed: examples" in str(exc):
                return None
            raise
        assert cursor.lastrowid is not None
        example_id = int(cursor.lastrowid)
        self.conn.execute(
            """
            INSERT OR IGNORE INTO dataset_assignments
                (family_key, split, assigned_at, assignment_method)
            VALUES (?, ?, ?, 'deterministic-family-hash-v1')
            """,
            (example["family_key"], family_split(example["family_key"]), now),
        )
        position = self.conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM queue_items WHERE queue_name = ?",
            (queue_name,),
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO queue_items
                (queue_name, example_id, review_round, position, status, added_at)
            VALUES (?, ?, 1, ?, 'pending', ?)
            """,
            (queue_name, example_id, position, now),
        )
        return example_id

    def queue_summaries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT queue_name,
                   COUNT(*) AS total,
                   SUM(status = 'completed') AS completed,
                   SUM(status = 'pending') AS pending,
                   SUM(status = 'skipped') AS skipped
              FROM queue_items
             GROUP BY queue_name
             ORDER BY MIN(added_at), queue_name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def next_example(self, queue_name: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT e.*, q.review_round, q.position, q.status, d.split,
                   a.id AS annotation_id, a.outcome, a.decisive_reason, a.confidence,
                   a.notes, a.agreement, a.model_revealed_at
              FROM queue_items q
              JOIN examples e ON e.id = q.example_id
              JOIN dataset_assignments d ON d.family_key = e.family_key
              LEFT JOIN annotations a
                ON a.example_id = e.id AND a.review_round = q.review_round
             WHERE q.queue_name = ? AND q.status = 'pending'
             ORDER BY q.position
             LIMIT 1
            """,
            (queue_name,),
        ).fetchone()
        return dict(row) if row else None

    def get_queue_example(self, queue_name: str, example_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT e.*, q.review_round, q.position, q.status, d.split,
                   a.id AS annotation_id, a.outcome, a.decisive_reason, a.confidence,
                   a.notes, a.agreement, a.model_revealed_at
              FROM queue_items q
              JOIN examples e ON e.id = q.example_id
              JOIN dataset_assignments d ON d.family_key = e.family_key
              LEFT JOIN annotations a
                ON a.example_id = e.id AND a.review_round = q.review_round
             WHERE q.queue_name = ? AND e.id = ?
            """,
            (queue_name, example_id),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _agreement(outcome: str, model_outcome: str | None) -> int | None:
        if outcome == "insufficient" or model_outcome not in {"match", "reject"}:
            return None
        human_positive = outcome in {"apply", "stretch"}
        return int(human_positive == (model_outcome == "match"))

    def annotate(
        self,
        *,
        example_id: int,
        review_round: int,
        outcome: str,
        decisive_reason: str | None = None,
        confidence: int | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"Unknown outcome: {outcome}")
        if decisive_reason not in DECISIVE_REASONS | {None}:
            raise ValueError(f"Unknown decisive reason: {decisive_reason}")
        if decisive_reason and outcome not in {"stretch", "pass"}:
            raise ValueError("A decisive reason is only valid for Stretch or Pass.")
        if confidence is not None and confidence not in range(1, 6):
            raise ValueError("Confidence must be between 1 and 5.")
        example = self.conn.execute(
            "SELECT model_outcome FROM examples WHERE id = ?", (example_id,)
        ).fetchone()
        if example is None:
            raise KeyError(example_id)
        queued = self.conn.execute(
            "SELECT 1 FROM queue_items WHERE example_id = ? AND review_round = ?",
            (example_id, review_round),
        ).fetchone()
        if queued is None:
            raise ValueError("Example is not assigned to this review round.")
        now = utc_now()
        agreement = self._agreement(outcome, example["model_outcome"])
        self.conn.execute(
            """
            INSERT INTO annotations (
                example_id, review_round, outcome, decisive_reason, confidence, notes,
                agreement, model_revealed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(example_id, review_round) DO UPDATE SET
                outcome = excluded.outcome,
                decisive_reason = excluded.decisive_reason,
                confidence = excluded.confidence,
                notes = excluded.notes,
                agreement = excluded.agreement,
                updated_at = excluded.updated_at
            """,
            (
                example_id,
                review_round,
                outcome,
                decisive_reason,
                confidence,
                notes.strip(),
                agreement,
                now,
                now,
                now,
            ),
        )
        self.conn.execute(
            """
            UPDATE queue_items
               SET status = 'completed', completed_at = ?
             WHERE example_id = ? AND review_round = ?
            """,
            (now, example_id, review_round),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM annotations WHERE example_id = ? AND review_round = ?",
            (example_id, review_round),
        ).fetchone()
        assert row is not None
        return dict(row)

    def update_annotation(
        self,
        annotation_id: int,
        *,
        decisive_reason: str | None,
        confidence: int | None,
        notes: str,
    ) -> dict[str, Any]:
        if decisive_reason not in DECISIVE_REASONS | {None}:
            raise ValueError(f"Unknown decisive reason: {decisive_reason}")
        if confidence is not None and confidence not in range(1, 6):
            raise ValueError("Confidence must be between 1 and 5.")
        current = self.conn.execute(
            "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        if current is None:
            raise KeyError(annotation_id)
        if decisive_reason and current["outcome"] not in {"stretch", "pass"}:
            raise ValueError("A decisive reason is only valid for Stretch or Pass.")
        self.conn.execute(
            """
            UPDATE annotations
               SET decisive_reason = ?, confidence = ?, notes = ?, updated_at = ?
             WHERE id = ?
            """,
            (decisive_reason, confidence, notes.strip(), utc_now(), annotation_id),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM annotations WHERE id = ?", (annotation_id,)
        ).fetchone()
        assert row is not None
        return dict(row)

    def set_split(self, family_key: str, split: str) -> None:
        if split not in SPLITS:
            raise ValueError(f"Unknown split: {split}")
        result = self.conn.execute(
            """
            UPDATE dataset_assignments
               SET split = ?, assigned_at = ?, assignment_method = 'manual'
             WHERE family_key = ?
            """,
            (split, utc_now(), family_key),
        )
        if result.rowcount == 0:
            raise KeyError(family_key)
        self.conn.commit()

    def create_relabel_queue(self, queue_name: str, percent: int = 10, seed: int = 0) -> int:
        """Queue a deterministic blinded sample of completed first-round examples."""
        if not 1 <= percent <= 100:
            raise ValueError("Percent must be between 1 and 100.")
        rows = self.conn.execute(
            """
            SELECT DISTINCT e.id
              FROM examples e
              JOIN annotations a ON a.example_id = e.id AND a.review_round = 1
             ORDER BY e.id
            """
        ).fetchall()
        ranked = sorted(
            (int(row["id"]) for row in rows),
            key=lambda value: hashlib.sha256(f"{seed}:{value}".encode()).hexdigest(),
        )
        count = max(1, round(len(ranked) * percent / 100)) if ranked else 0
        now = utc_now()
        for position, example_id in enumerate(ranked[:count], 1):
            self.conn.execute(
                """
                INSERT OR IGNORE INTO queue_items
                    (queue_name, example_id, review_round, position, status, added_at)
                VALUES (?, ?, 2, ?, 'pending', ?)
                """,
                (queue_name, example_id, position, now),
            )
        self.conn.commit()
        return count

    def consistency_report(self) -> dict[str, Any]:
        """Compare completed human review pairs without exposing pending relabels."""
        rows = self.conn.execute(
            """
            SELECT e.id, e.title, e.company, e.description, e.profile_text,
                   a.outcome AS first_outcome, a.decisive_reason AS first_reason,
                   a.confidence AS first_confidence, a.notes AS first_notes,
                   b.outcome AS second_outcome, b.decisive_reason AS second_reason,
                   b.confidence AS second_confidence, b.notes AS second_notes
              FROM examples e
              JOIN annotations a ON a.example_id = e.id AND a.review_round = 1
              JOIN annotations b ON b.example_id = e.id AND b.review_round = 2
             ORDER BY (a.outcome = b.outcome), e.id
            """
        ).fetchall()
        comparisons = []
        for row in rows:
            item = dict(row)
            item["agrees"] = item["first_outcome"] == item["second_outcome"]
            item["resolution"] = self.get_consistency_resolution(item["id"])
            comparisons.append(item)
        total = len(comparisons)
        agreed = sum(item["agrees"] for item in comparisons)
        return {
            "total": total,
            "agreed": agreed,
            "disagreed": total - agreed,
            "agreement_rate": agreed / total if total else None,
            "unresolved": sum(
                not item["agrees"] and item["resolution"] is None for item in comparisons
            ),
            "comparisons": comparisons,
        }

    def get_consistency_resolution(self, example_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM consistency_resolutions WHERE example_id = ?", (example_id,)
        ).fetchone()
        return dict(row) if row else None

    def resolve_consistency(
        self, example_id: int, selected_round: int, notes: str = ""
    ) -> dict[str, Any]:
        if selected_round not in (1, 2):
            raise ValueError("Select the first or second review round.")
        rows = self.conn.execute(
            "SELECT review_round FROM annotations WHERE example_id = ? AND review_round IN (1, 2)",
            (example_id,),
        ).fetchall()
        if len(rows) != 2:
            raise ValueError("Both review rounds must be completed before choosing a final label.")
        now = utc_now()
        self.conn.execute(
            """
            INSERT INTO consistency_resolutions
                (example_id, selected_round, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(example_id) DO UPDATE SET
                selected_round = excluded.selected_round,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (example_id, selected_round, notes.strip(), now, now),
        )
        self.conn.commit()
        resolution = self.get_consistency_resolution(example_id)
        assert resolution is not None
        return resolution

    def export_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.*, d.split
              FROM examples e
              JOIN dataset_assignments d ON d.family_key = e.family_key
             WHERE EXISTS (
                 SELECT 1 FROM annotations a
                  WHERE a.example_id = e.id AND a.review_round = 1
             )
             ORDER BY d.split, e.family_key, e.id
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            annotations = self.conn.execute(
                "SELECT * FROM annotations WHERE example_id = ? ORDER BY review_round",
                (row["id"],),
            ).fetchall()
            item["annotations"] = [dict(annotation) for annotation in annotations]
            item["resolution"] = self.get_consistency_resolution(row["id"])
            result.append(item)
        return result


def decode_snapshot_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Decode JSON blobs for API and export consumers."""
    result = dict(row)
    for key in (
        "job_snapshot_json",
        "profile_snapshot_json",
        "model_verdict_json",
        "source_metadata_json",
    ):
        raw = result.pop(key, "{}")
        try:
            result[key.removesuffix("_json")] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            result[key.removesuffix("_json")] = {}
    return result
