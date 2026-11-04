"""Shared CLI commands must have a stable machine-readable representation."""

import json
import tempfile
from unittest import mock

import pytest

from findajob import cli
from findajob.core import jobs
from findajob.core.database import Database
from findajob.scoring import worker
from findajob.search import explicit
from findajob.search.repository import upsert_posting


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
            "--priority",
            "consider",
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
        priority="consider",
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


@pytest.mark.parametrize(
    "option,value",
    [("--fit", None), ("--no-fit", None), ("--reason-type", "match"), ("--stage", "reviewed")],
)
def test_jobs_list_rejects_retired_filters(option: str, value: str | None) -> None:
    args = ["jobs", "list", option]
    if value is not None:
        args.append(value)
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(args)
    assert exc.value.code == 2


@pytest.mark.parametrize("source", [None, "indeed", "linkedin"])
def test_search_run_passes_source_filter(source: str | None) -> None:
    from findajob.search import runner

    argv = ["search", "run", "--dry-run"]
    if source:
        argv.extend(["--source", source])
    args = cli.build_parser().parse_args(argv)
    with mock.patch.object(runner, "run_sync", return_value={}) as run:
        assert cli._cmd_search(args) == 0
    run.assert_called_once_with(
        dry_run=True,
        force=False,
        sources=[source] if source else None,
    )


def test_search_proxies_inspects_and_resets_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from findajob.core import config as config_mod
    from findajob.core import database as db_mod
    from findajob.search import proxies as proxies_mod

    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        seed_db = Database(db_path=tmp.name)
        try:
            p1 = "u:p@10.0.0.1:8000"
            p2 = "u:p@10.0.0.2:8000"
            seed_db.set_proxy_failure(
                proxies_mod.proxy_id(p1),
                ("linkedin", "indeed"),
                status="broken",
                backoff_until=None,
                consecutive_failures=3,
                last_error="407 Proxy Authentication Required",
            )
            seed_db.set_proxy_failure(
                proxies_mod.proxy_id(p2),
                ("linkedin",),
                status="backoff",
                backoff_until="2099-01-01T00:00:00+00:00",
                consecutive_failures=1,
                last_error="HTTP 429",
            )
        finally:
            seed_db.close()

        with (
            mock.patch.object(config_mod, "load_config", return_value={"scraper": {}}),
            mock.patch.object(proxies_mod, "load_proxies", return_value=[p1, p2]),
            mock.patch.object(db_mod, "Database", side_effect=lambda: Database(db_path=tmp.name)),
        ):
            inspect_args = cli.build_parser().parse_args(["search", "proxies"])
            assert cli._cmd_search_proxies(inspect_args) == 0
            out = capsys.readouterr().out
            assert "[linkedin] 2 configured: 0 usable, 1 backoff, 1 broken" in out
            assert "[indeed] 2 configured: 1 usable, 0 backoff, 1 broken" in out
            assert "u:p" not in out

            reset_broken = cli.build_parser().parse_args(
                ["search", "proxies", "--reset", "--broken-only", "--source", "linkedin"]
            )
            assert cli._cmd_search_proxies(reset_broken) == 0
            assert "Cleared 1 proxy state row(s)." in capsys.readouterr().out

            reset_all = cli.build_parser().parse_args(["search", "proxies", "--reset"])
            assert cli._cmd_search_proxies(reset_all) == 0
            assert "Cleared 2 proxy state row(s)." in capsys.readouterr().out
