import json
import sqlite3
from pathlib import Path

import pytest

from findajob.labeler.app import _public_example
from findajob.labeler.database import LabelerDatabase, family_split
from findajob.labeler.exporter import export_dataset
from findajob.labeler.importer import import_examples


def _source_database(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            company TEXT,
            company_normalized TEXT,
            location TEXT,
            url TEXT,
            description TEXT,
            date_posted TEXT,
            date_found TEXT,
            last_seen_at TEXT,
            duplicate_of INTEGER,
            content_hash TEXT
        );
        CREATE TABLE job_verdicts (
            id INTEGER PRIMARY KEY,
            job_id INTEGER NOT NULL,
            profile_version INTEGER NOT NULL,
            model TEXT NOT NULL,
            fit INTEGER,
            reason_type TEXT,
            reason_description TEXT,
            prompt_hash TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE profile (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            summary_text TEXT NOT NULL,
            skills_json TEXT NOT NULL
        );
        INSERT INTO profile VALUES (1, 'Private Candidate', 'Python, APIs, robot UI', '["Python"]');
        INSERT INTO jobs VALUES
            (1, 'Perception Engineer', 'Robots Inc', 'robots inc', 'Seattle',
             'https://example.test/1', 'Build C++ ROS sensor-fusion systems.', '2026-09-18',
             '2026-09-18', '2026-09-18', NULL, 'hash-1'),
            (2, 'Platform Engineer', 'Cloud Co', 'cloud co', 'Remote',
             'https://example.test/2', 'Build Python platform APIs.', '2026-09-17',
             '2026-09-17', '2026-09-17', NULL, 'hash-2');
        INSERT INTO job_verdicts VALUES
            (1, 1, 1, 'model-a', 0, 'skills', 'No production C++ or ROS evidence.',
             'prompt-v1', '2026-09-18T10:00:00Z'),
            (2, 2, 1, 'model-a', 1, 'match', 'Direct platform and Python evidence.',
             'prompt-v1', '2026-09-18T10:00:00Z');
        PRAGMA user_version = 31;
        """
    )
    conn.commit()
    conn.close()


def test_import_snapshots_are_immutable_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "jobs.db"
    labeler = tmp_path / "labeler.db"
    _source_database(source)

    result = import_examples(
        source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[1, 2]
    )
    assert result["imported"] == 2
    assert result["buckets"] == {"selected": 2}

    source_conn = sqlite3.connect(source)
    source_conn.execute("UPDATE jobs SET description = 'Changed later' WHERE id = 1")
    source_conn.commit()
    source_conn.close()

    with LabelerDatabase(str(labeler)) as db:
        row = db.conn.execute("SELECT * FROM examples WHERE source_job_id = '1'").fetchone()
        assert row is not None
        assert row["description"] == "Build C++ ROS sensor-fusion systems."
        assert json.loads(row["job_snapshot_json"])["description"] == row["description"]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.conn.execute("UPDATE examples SET title = 'Mutated' WHERE id = ?", (row["id"],))

    second = import_examples(source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[2])
    assert second["imported"] == 0
    assert second["skipped_existing"] == 1


def test_api_blinds_verdict_until_label_and_tracks_progress(tmp_path: Path) -> None:
    source = tmp_path / "jobs.db"
    labeler = tmp_path / "labeler.db"
    _source_database(source)
    import_examples(source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[1, 2])
    with LabelerDatabase(str(labeler)) as db:
        row = db.next_example("initial")
        assert row is not None
        first = _public_example(row)
        assert first["model_verdict"] is None
        assert "queue_bucket" not in first
        assert "model_outcome" not in first

        annotation = db.annotate(example_id=first["id"], review_round=1, outcome="stretch")
        assert annotation["agreement"] == 0
        updated = db.update_annotation(
            annotation["id"],
            decisive_reason="specialization",
            confidence=5,
            notes="Adjacent only.",
        )
        assert updated["decisive_reason"] == "specialization"
        completed = db.get_queue_example("initial", first["id"])
        assert completed is not None
        revealed = _public_example(completed)
        assert revealed["model_verdict"]["outcome"] == "reject"
        assert db.queue_summaries() == [
            {
                "queue_name": "initial",
                "total": 2,
                "completed": 1,
                "pending": 1,
                "skipped": 0,
            }
        ]


def test_relabel_is_blind_and_export_is_versioned(tmp_path: Path) -> None:
    source = tmp_path / "jobs.db"
    labeler = tmp_path / "labeler.db"
    _source_database(source)
    import_examples(source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[1, 2])
    with LabelerDatabase(str(labeler)) as db:
        examples = db.conn.execute("SELECT id, model_outcome FROM examples ORDER BY id").fetchall()
        for row in examples:
            outcome = "pass" if row["model_outcome"] == "reject" else "apply"
            db.annotate(example_id=row["id"], review_round=1, outcome=outcome)
        assert db.create_relabel_queue("consistency", percent=50, seed=7) == 1

    with LabelerDatabase(str(labeler)) as db:
        relabel_row = db.next_example("consistency")
        assert relabel_row is not None
        relabel = _public_example(relabel_row)
        assert relabel["review_round"] == 2
        assert relabel["annotation"] is None
        assert relabel["model_verdict"] is None

    output = tmp_path / "exports"
    manifest = export_dataset(
        labeler_db_path=str(labeler), output_dir=str(output), version="baseline-v1"
    )
    assert manifest["example_count"] == 2
    records = [
        json.loads(line)
        for line in (output / "baseline-v1" / "examples.jsonl").read_text().splitlines()
    ]
    assert {record["human_outcome"] for record in records} == {"apply", "pass"}
    assert all(record["profile_text"] == "Python, APIs, robot UI" for record in records)
    assert all(record["provenance"]["snapshot_hash"] for record in records)
    with pytest.raises(FileExistsError):
        export_dataset(labeler_db_path=str(labeler), output_dir=str(output), version="baseline-v1")


def test_consistency_report_compares_only_completed_pairs(tmp_path: Path) -> None:
    source = tmp_path / "jobs.db"
    labeler = tmp_path / "labeler.db"
    _source_database(source)
    import_examples(source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[1, 2])
    with LabelerDatabase(str(labeler)) as db:
        examples = db.conn.execute("SELECT id FROM examples ORDER BY id").fetchall()
        first_id, second_id = [row["id"] for row in examples]
        db.annotate(example_id=first_id, review_round=1, outcome="apply")
        db.annotate(
            example_id=second_id,
            review_round=1,
            outcome="stretch",
            decisive_reason="specialization",
            confidence=3,
            notes="Adjacent experience.",
        )
        db.create_relabel_queue("consistency", percent=100)
        assert db.consistency_report() == {
            "total": 0,
            "agreed": 0,
            "disagreed": 0,
            "agreement_rate": None,
            "comparisons": [],
            "unresolved": 0,
        }

        db.annotate(example_id=first_id, review_round=2, outcome="apply")
        partial = db.consistency_report()
        assert partial["total"] == 1
        assert partial["agreement_rate"] == 1
        assert [item["id"] for item in partial["comparisons"]] == [first_id]

        db.annotate(
            example_id=second_id,
            review_round=2,
            outcome="pass",
            decisive_reason="seniority",
            confidence=5,
            notes="Scope too broad.",
        )
        report = db.consistency_report()
        assert report["total"] == 2
        assert report["agreed"] == report["disagreed"] == 1
        assert report["agreement_rate"] == 0.5
        assert report["unresolved"] == 1
        disagreement, agreement = report["comparisons"]
        assert disagreement["id"] == second_id
        assert disagreement["agrees"] is False
        assert disagreement["first_outcome"] == "stretch"
        assert disagreement["second_outcome"] == "pass"
        assert disagreement["first_reason"] == "specialization"
        assert disagreement["second_reason"] == "seniority"
        assert disagreement["first_confidence"] == 3
        assert disagreement["second_confidence"] == 5
        assert disagreement["first_notes"] == "Adjacent experience."
        assert disagreement["second_notes"] == "Scope too broad."
        assert disagreement["description"] == "Build Python platform APIs."
        assert disagreement["profile_text"] == "Python, APIs, robot UI"
        assert agreement["agrees"] is True
        # Viewing the report preserves both original annotations for export.
        exported = next(row for row in db.export_rows() if row["id"] == second_id)
        assert [a["outcome"] for a in exported["annotations"]] == ["stretch", "pass"]


@pytest.mark.parametrize("selected_round", [1, 2])
def test_resolution_persists_and_controls_export(tmp_path: Path, selected_round: int) -> None:
    source = tmp_path / "jobs.db"
    labeler = tmp_path / "labeler.db"
    _source_database(source)
    import_examples(source_db_path=str(source), labeler_db_path=str(labeler), job_ids=[1])
    with LabelerDatabase(str(labeler)) as db:
        example = db.next_example("initial")
        assert example is not None
        example_id = example["id"]
        db.annotate(example_id=example_id, review_round=1, outcome="stretch")
        db.create_relabel_queue("consistency", percent=100)
        with pytest.raises(ValueError, match="Both review rounds"):
            db.resolve_consistency(example_id, 2)
        db.annotate(
            example_id=example_id,
            review_round=2,
            outcome="pass",
            decisive_reason="specialization",
            confidence=5,
            notes="Requires direct ROS evidence.",
        )
        with pytest.raises(ValueError, match="first or second"):
            db.resolve_consistency(example_id, 3)
        # Exercise upgrading an existing v1 database without losing its reviews.
        db.conn.execute("DROP TABLE consistency_resolutions")
        db.conn.execute("PRAGMA user_version = 1")
        db.conn.commit()

    with LabelerDatabase(str(labeler)) as db:
        assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        db.resolve_consistency(example_id, 1, "Initial choice")
        db.resolve_consistency(example_id, selected_round, "  Specialization is role-defining.  ")

    with LabelerDatabase(str(labeler)) as db:
        report = db.consistency_report()
        assert report["unresolved"] == 0
        assert report["agreed"] == 0
        assert report["disagreed"] == 1
        resolution = report["comparisons"][0]["resolution"]
        assert resolution["selected_round"] == selected_round
        assert resolution["notes"] == "Specialization is role-defining."

    output = tmp_path / "exports"
    export_dataset(labeler_db_path=str(labeler), output_dir=str(output), version="resolved-v1")
    record = json.loads((output / "resolved-v1" / "examples.jsonl").read_text())
    primary = record["review_rounds"][selected_round - 1]
    assert record["human_outcome"] == primary["outcome"]
    assert record["human_reason"] == primary["decisive_reason"]
    assert record["human_confidence"] == primary["confidence"]
    assert record["human_notes"] == primary["notes"]
    assert record["agreement"] == primary["agreement"]
    assert record["consistency_resolution"]["selected_round"] == selected_round
    assert [a["outcome"] for a in record["review_rounds"]] == ["stretch", "pass"]


def test_family_split_is_deterministic() -> None:
    assert family_split("company-title:acme|engineer") == family_split(
        "company-title:acme|engineer"
    )
    assert family_split("company-title:acme|engineer") in {"optimization", "validation"}
