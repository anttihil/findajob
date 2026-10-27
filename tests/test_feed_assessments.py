"""Feed, detail, navigation and counters read verdicts without legacy stage logic."""

from collections.abc import Iterator
from pathlib import Path
from typing import TypedDict

import pytest

from findajob.core.database import Database
from findajob.core.job_repository import count_review_first


class AssessmentFilters(TypedDict, total=False):
    priority: str


@pytest.fixture
def assessments(tmp_path: Path) -> Iterator[Database]:
    db = Database(str(tmp_path / "assessments.db"))
    # Missing assessments, historical/reviewed verdicts, and null verdict values.
    cases = [
        (False, None, None),
        (False, None, None),
        (False, None, None),
        (True, None, None),
        (True, None, "first"),
        (True, "reviewed", "consider"),
        (True, "assessed", None),
        (True, "reviewed", "low"),
        (True, "reviewed", "first"),
    ]
    for job_id, (has_verdict, stage, priority) in enumerate(cases, 1):
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, url, status, date_found, sync_run_id) "
            "VALUES (?, ?, 'Engineer', ?, 'unread', '2026-10-01', 1)",
            (job_id, f"job-{job_id}", f"https://example.test/{job_id}"),
        )
        # A second profile must neither leak into results nor multiply rows.
        for version in (1, 2):
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


def test_verdict_fields_match_in_feed_and_detail(assessments: Database) -> None:
    expected = {
        row["id"]: dict(row)
        for row in assessments.conn.execute(
            "SELECT j.id, v.fit, v.priority, v.reason_type, v.reason_description, v.profile_hash "
            "FROM jobs j LEFT JOIN job_verdicts v ON v.job_id=j.id AND v.profile_version=1"
        )
    }
    page = assessments.query_jobs()
    assert page["total"] == len(expected)
    for job in page["jobs"]:
        detail = assessments.query_jobs(job_id=job["id"])["jobs"][0]
        assert "stage" not in job and "stage" not in detail
        for name, value in expected[job["id"]].items():
            if name != "id":
                assert job[name] == detail[name] == value


@pytest.mark.parametrize(
    "filters",
    [
        {},
        {"priority": "first"},
        {"priority": "consider"},
        {"priority": "low"},
    ],
)
def test_priority_filters_preserve_order_navigation_and_counts(
    assessments: Database, filters: AssessmentFilters
) -> None:
    where = "".join(f" AND a.{name} = ?" for name in filters)
    expected = [
        row[0]
        for row in assessments.conn.execute(
            "SELECT j.id FROM jobs j LEFT JOIN job_verdicts a "
            "ON a.job_id = j.id AND a.profile_version = 1 "
            f"WHERE 1=1{where} ORDER BY CASE WHEN a.priority = 'first' THEN 0 "
            "WHEN a.priority = 'consider' THEN 1 WHEN a.priority = 'low' "
            "THEN 3 ELSE 2 END, "
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
        "SELECT COUNT(*) FROM job_verdicts WHERE profile_version = 1 AND priority = 'first'"
        + count_where,
        list(count_filters.values()),
    ).fetchone()[0]
    assert count_review_first(assessments.conn, **filters) == expected_count


def test_queries_do_not_require_view_and_detail_uses_indexed_assessments(
    assessments: Database,
) -> None:
    assert (
        assessments.conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'v_job_assessments'"
        ).fetchone()
        is None
    )
    statements: list[str] = []
    assessments.conn.set_trace_callback(statements.append)
    try:
        assert assessments.query_jobs(job_id=5)["jobs"][0]["priority"] == "first"
    finally:
        assessments.conn.set_trace_callback(None)
    query = next(sql for sql in statements if "COUNT(*) OVER ()" in sql)
    plan = [row[3] for row in assessments.conn.execute(f"EXPLAIN QUERY PLAN {query}")]
    assert not any("MATERIALIZE" in step and "page" not in step for step in plan)
    assert any("SEARCH v USING INDEX" in step for step in plan)
    assert len(assessments.job_ids_for()) == 9
    assert count_review_first(assessments.conn) == 2
