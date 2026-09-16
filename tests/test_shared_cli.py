"""Shared CLI commands must have a stable machine-readable representation."""

import json
import tempfile
from unittest import mock

import pytest

from careerradar import cli
from careerradar.core import jobs
from careerradar.core.database import Database
from careerradar.scoring import worker
from careerradar.search import explicit
from careerradar.search.repository import upsert_posting


def _records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line]


def test_selected_score_emits_one_json_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli.build_parser().parse_args(["score", "jobs", "--job-id", "99", "--json"])
    result = {"status": "error", "error": "provider unavailable"}
    with mock.patch.object(worker, "score_selected", return_value=result):
        assert cli._cmd_shared(args) == 1

    records = _records(capsys.readouterr().out)
    assert records == [
        {
            "contract_version": 1,
            "stage": "score",
            "status": "error",
            "error": "provider unavailable",
        }
    ]


def test_explicit_query_uses_the_same_shared_dispatcher(
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = cli.build_parser().parse_args(
        ["search", "query", "--query", "Platform Engineer", "--location", "Helsinki", "--json"]
    )
    result = {"status": "ok", "job_ids": [41], "postings_fetched": 1}
    with mock.patch.object(explicit, "run_query", return_value=result) as search:
        assert cli._cmd_shared(args) == 0

    records = _records(capsys.readouterr().out)
    assert records == [cli._result("search", result)]
    search.assert_called_once_with(args)


def test_jobs_list_uses_detail_records_and_safe_filters() -> None:
    db = mock.MagicMock()
    db.query_jobs.return_value = {"jobs": [{"id": 7, "description": "full"}], "total": 1}
    args = cli.build_parser().parse_args(
        [
            "jobs",
            "list",
            "--source",
            "indeed",
            "--pipeline-state",
            "new",
            "--text",
            "platform",
        ]
    )
    with mock.patch.object(jobs, "Database", return_value=db):
        result = jobs.list_postings(args)

    assert result["jobs"] == [{"id": 7, "description": "full"}]
    db.query_jobs.assert_called_once_with(
        country=None,
        location=None,
        source="indeed",
        is_remote=None,
        pipeline_state="new",
        fit=None,
        reason_type=None,
        date_posted=None,
        q="platform",
        limit=25,
        offset=0,
        detail=True,
    )
    db.close.assert_called_once()


def test_explicit_query_honors_persisted_source_backoff() -> None:
    args = cli.build_parser().parse_args(
        ["search", "query", "--query", "Platform Engineer", "--location", "Helsinki"]
    )
    db = mock.MagicMock()
    circuit = mock.MagicMock()
    circuit.persisted_backoff_active.return_value = True
    circuit.backoff_until.return_value = "2030-01-01T00:00:00+00:00"
    with (
        mock.patch.object(explicit, "Database", return_value=db),
        mock.patch.object(explicit, "SourceCircuit", return_value=circuit),
        mock.patch.object(explicit, "load_config", return_value={}),
        mock.patch.object(explicit, "load_proxies", return_value=[]),
    ):
        with pytest.raises(ValueError, match="backoff until"):
            explicit.run_query(args)

    circuit.before_request.assert_not_called()


def test_explicit_query_does_not_erase_existing_cell_attribution() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        db = Database(db_path=tmp.name)
        try:
            job_id, is_new = upsert_posting(
                db.conn,
                {
                    "job_key": "attribution-test",
                    "title": "Platform Engineer",
                    "company": "Example",
                    "url": "https://example.test/jobs/1",
                    "scrape_cell_id": 42,
                },
            )
            assert job_id is not None and is_new

            _, is_new = upsert_posting(
                db.conn,
                {
                    "job_key": "attribution-test",
                    "title": "Platform Engineer (updated)",
                    "company": "Example",
                    "url": "https://example.test/jobs/1",
                    "scrape_cell_id": None,
                },
            )
            assert not is_new
            row = db.conn.execute(
                "SELECT scrape_cell_id FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            assert row["scrape_cell_id"] == 42
        finally:
            db.close()
