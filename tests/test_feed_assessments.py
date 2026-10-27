"""Direct assessment joins preserve the view's semantics without materializing it."""

from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import pytest

from findajob.core.database import Database
from findajob.core.job_repository import count_review_first


class AssessmentFilters(TypedDict, total=False):
    stage: str
    priority: str
    fit: bool
    reason_type: str


@pytest.fixture
def assessments(tmp_path: Path) -> Iterator[Database]:
    db = Database(str(tmp_path / "assessments.db"))
    # Missing assessments, screening only, historical/reviewed legacy verdicts,
    # explicit verdict stage, conflicting screening, and null verdict values.
    cases = [
        (False, None, None, None),
        (False, None, None, "screening_excluded"),
        (False, None, None, "screening_advanced"),
        (True, None, None, "screening_excluded"),
        (True, None, "first", "screening_excluded"),
        (True, "reviewed", "consider", "screening_excluded"),
        (True, "screening_excluded", None, "screening_advanced"),
        (True, "reviewed", "low", None),
        (True, "reviewed", "first", "screening_advanced"),
    ]
    for job_id, (has_verdict, stage, priority, screening_stage) in enumerate(cases, 1):
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, status, date_found, sync_run_id) "
            "VALUES (?, ?, 'Engineer', ?, 'unread', '2026-10-01', 1)",
            (job_id, f"job-{job_id}", f"https://example.test/{job_id}"),
        )
        # A second profile must neither leak into results nor multiply rows.
        for version in (1, 2):
            if screening_stage is not None or version == 2:
                db.conn.execute(
                    "INSERT INTO job_screenings (job_id, profile_version, advance, reason, "
                    "stage, model, review_model, profile_hash, posting_hash, prompt_hash, "
                    "prompt_version, schema_version, state_json, created_at) "
                    "VALUES (?, ?, 0, 'screening reason', ?, 'm', 'm', 'screening hash', "
                    "'posting', 'prompt', '1', 1, '{}', '2026-10-01')",
                    (job_id, version, screening_stage or "screening_excluded"),
                )
            if has_verdict or version == 2:
                db.conn.execute(
                    "INSERT INTO job_verdicts (job_id, profile_version, model, stage, priority, "
                    "fit, reason_type, reason_description, profile_hash, created_at) "
                    "VALUES (?, ?, 'm', ?, ?, 1, 'match', ?, ?, '2026-10-01')",
                    (
                        job_id,
                        version,
                        stage,
                        priority if version == 1 else "first",
                        None if job_id == 9 else "verdict reason",
                        None if job_id == 9 else "verdict hash",
                    ),
                )
    db.conn.commit()
    yield db
    db.close()


def test_assessment_fields_match_view_in_feed_and_detail(assessments: Database) -> None:
    expected = {
        row["job_id"]: dict(row)
        for row in assessments.conn.execute("SELECT * FROM v_job_assessments")
    }
    page = assessments.query_jobs()
    assert page["total"] == len(expected)
    for job in page["jobs"]:
        detail = assessments.query_jobs(job_id=job["id"])["jobs"][0]
        for name, value in expected[job["id"]].items():
            if name not in {"job_id", "profile_version"}:
                assert job[name] == detail[name] == value


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"stage": "unreviewed"},
        {"stage": "historical"},
        {"stage": "reviewed"},
        {"stage": "screening_excluded"},
        {"stage": "screening_advanced"},
        {"priority": "first"},
        {"priority": "consider"},
        {"priority": "low"},
        {"fit": True},
        {"fit": False},
        {"reason_type": "match"},
    ],
)
def test_assessment_filters_order_navigation_and_counts_match_view(
    assessments: Database, filters: AssessmentFilters
) -> None:
    where = "".join(f" AND a.{name} = ?" for name in filters)
    expected = [
        row[0]
        for row in assessments.conn.execute(
            "SELECT j.id FROM jobs j LEFT JOIN v_job_assessments a ON a.job_id = j.id "
            f"WHERE 1=1{where} ORDER BY CASE WHEN a.priority = 'first' THEN 0 "
            "WHEN a.priority = 'consider' THEN 1 WHEN a.priority = 'low' "
            "OR a.stage = 'screening_excluded' THEN 3 ELSE 2 END, "
            "COALESCE(j.date_posted, j.date_found) DESC, j.id DESC",
            list(filters.values()),
        )
    ]
    page = assessments.query_jobs(**filters, limit=2)
    assert page["total"] == len(expected)
    assert [job["id"] for job in page["jobs"]] == expected[:2]
    assert assessments.job_ids_for(**filters, limit=2) == expected[:2]
    assert assessments.job_ids_for(**filters, limit=2, offset=2) == expected[2:4]
    # The counter deliberately overrides any supplied priority.
    count_filters = {key: value for key, value in filters.items() if key != "priority"}
    count_where = "".join(f" AND {name} = ?" for name in count_filters)
    expected_count = assessments.conn.execute(
        "SELECT COUNT(*) FROM v_job_assessments WHERE priority = 'first'" + count_where,
        list(count_filters.values()),
    ).fetchone()[0]
    assert count_review_first(assessments.conn, **filters) == expected_count


def test_queries_do_not_require_view_and_detail_uses_indexed_assessments(
    assessments: Database,
) -> None:
    assessments.conn.execute("DROP VIEW v_job_assessments")
    statements: list[str] = []
    assessments.conn.set_trace_callback(statements.append)
    try:
        assert assessments.query_jobs(job_id=5)["jobs"][0]["stage"] == "reviewed"
    finally:
        assessments.conn.set_trace_callback(None)
    query = next(sql for sql in statements if "COUNT(*) OVER ()" in sql)
    plan = [row[3] for row in assessments.conn.execute(f"EXPLAIN QUERY PLAN {query}")]
    assert not any("MATERIALIZE" in step for step in plan)
    assert any("SEARCH v USING INDEX" in step for step in plan)
    assert any("SEARCH s USING INDEX" in step for step in plan)
    assert len(assessments.job_ids_for()) == 9
    assert count_review_first(assessments.conn) == 2
