"""Provider billing outages stay visible and never quarantine otherwise valid jobs."""

import io
import sqlite3
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from findajob.core.migrations import MIGRATIONS, migrate
from findajob.core.status_repository import get_pipeline_status
from findajob.scoring import repository, worker
from findajob.scoring.graph import build_graph
from tests.test_scoring_module import PROFILE, FakeChain, posting
from tests.test_scoring_review import response


@pytest.fixture(autouse=True)
def off_peak() -> Any:
    with (
        mock.patch.object(worker, "_using_deepseek", return_value=False),
        mock.patch.object(worker, "get_llm_provider", return_value=SimpleNamespace(name="test")),
    ):
        yield


class BalanceError(RuntimeError):
    status_code = 402


@pytest.fixture
def database() -> Any:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    for job_id in range(1, 5):
        conn.execute(
            "INSERT INTO jobs (id, job_key, title, description, date_found) "
            "VALUES (?, ?, 'Engineer', ?, datetime('now'))",
            (job_id, str(job_id), "x" * 400),
        )
    conn.commit()
    db = mock.Mock(conn=conn)
    db.query_jobs.side_effect = lambda **kw: {
        "jobs": [dict(conn.execute("SELECT * FROM jobs WHERE id=?", (kw["job_id"],)).fetchone())]
    }
    yield db
    conn.close()


def failing_chain() -> mock.Mock:
    return mock.Mock(invoke=mock.Mock(side_effect=BalanceError("Insufficient Balance")))


def test_balance_failure_is_not_retried_within_graph() -> None:
    chain = failing_chain()
    model = mock.Mock()
    model.with_structured_output.return_value = chain
    with mock.patch("findajob.scoring.graph.structured_model", return_value=model):
        state = build_graph().invoke(
            {"posting": posting(), "model": "flash", "review_model": "pro"}
        )
    assert state["balance_exhausted"] is True
    assert state["verdict"] is None
    assert chain.invoke.call_count == 1


def test_worker_stops_after_batch_without_quarantining_and_recovers(database: Any) -> None:
    chain = failing_chain()
    model = mock.Mock()
    model.with_structured_output.return_value = chain
    config = {"scoring": {"model": "flash", "concurrency": 2, "review": {"model": "pro"}}}
    with (
        mock.patch.object(worker, "Database", return_value=database),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(worker, "load_config", return_value=config),
        mock.patch("findajob.scoring.graph.structured_model", return_value=model),
        redirect_stdout(io.StringIO()),
    ):
        for _ in range(3):
            assert worker.run_scoring() == 1
        assert chain.invoke.call_count == 6  # Two in-flight jobs per run; no next batch.
        assert database.conn.execute("SELECT SUM(scoring_failures) FROM jobs").fetchone()[0] == 0
        assert repository.count_pending_scoring(database.conn) == 4
        status = get_pipeline_status(database.conn, False)
        assert status["score"]["error"]["message"] == repository.BALANCE_ERROR_MESSAGE
        assert status["score"]["error"]["occurred_at"]
        # A successful subsequent run clears the persistent UI error and drains once.
        chain.invoke.side_effect = None
        chain.invoke.side_effect = (
            [response(True)] * 2
            + [response(True)] * 2
            + [response(True)] * 2
            + [response(True)] * 2
        )
        assert worker.run_scoring() == 0
        assert get_pipeline_status(database.conn, False)["score"]["error"] is None
        assert repository.count_pending_scoring(database.conn) == 0
        chain.invoke.reset_mock()
        assert worker.run_scoring() == 0
        chain.invoke.assert_not_called()


def test_single_job_balance_failure_recovers_without_quarantining(database: Any) -> None:
    chain = failing_chain()
    with (
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(worker, "load_config", return_value={"scoring": {"model": "flash"}}),
        mock.patch(
            "findajob.scoring.graph.structured_model",
            return_value=mock.Mock(with_structured_output=mock.Mock(return_value=chain)),
        ),
    ):
        with pytest.raises(worker.ScoringBalanceError):
            worker.score_job(1, db=database)
        assert repository.get_provider_failure(database.conn)
        assert (
            database.conn.execute("SELECT scoring_failures FROM jobs WHERE id=1").fetchone()[0] == 0
        )
        chain.invoke.side_effect = None
        chain.invoke.return_value = response(False)
        assert worker.score_job(1, db=database)["priority"] == "low"
        assert repository.get_provider_failure(database.conn) is None


def test_empty_queue_does_not_hide_unresolved_provider_failure(database: Any) -> None:
    repository.record_balance_failure(database.conn)
    database.conn.execute("UPDATE jobs SET scoring_failures=3")
    with (
        mock.patch.object(worker, "Database", return_value=database),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch("findajob.scoring.graph.structured_model") as model,
        redirect_stdout(io.StringIO()) as output,
    ):
        assert worker.run_scoring() == 1
    model.assert_not_called()
    assert "backlog is drained" not in output.getvalue()
    assert get_pipeline_status(database.conn, False)["score"]["error"] is not None


def test_selected_jobs_stop_on_balance_failure() -> None:
    with mock.patch.object(
        worker,
        "score_job",
        side_effect=worker.ScoringBalanceError(repository.BALANCE_ERROR_MESSAGE),
    ) as score:
        result = worker.score_selected([1, 2, 3])
    assert result["failed"] == 3
    assert result["status"] == "error"
    score.assert_called_once_with(1, model=None, review_only=False, budget=mock.ANY)


@pytest.mark.parametrize("recovered", [False, True])
def test_upgrade_surfaces_only_unresolved_historical_balance_failure(recovered: bool) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    for version, _, migration in MIGRATIONS:
        if version < 34:
            migration(conn.cursor())
    conn.execute(
        "INSERT INTO jobs (id, title, scoring_failures, "
        "last_scoring_error, last_scoring_failure_at) "
        "VALUES (1, 'Engineer', 3, 'Error code: 402 - Insufficient Balance', "
        "'2026-10-20T20:00:00Z')"
    )
    if recovered:
        conn.execute(
            "INSERT INTO job_verdicts (job_id, profile_version, model, fit, created_at) "
            "VALUES (1, 1, 'flash', 1, '2026-10-20T21:00:00Z')"
        )
    conn.execute("PRAGMA user_version=33")
    conn.commit()
    assert migrate(conn) == 3
    assert (repository.get_provider_failure(conn) is None) is recovered
    assert conn.execute("SELECT scoring_failures FROM jobs").fetchone()[0] == 3
    assert migrate(conn) == 0
    failure = repository.get_provider_failure(conn)
    migration = next(fn for version, _, fn in MIGRATIONS if version == 34)
    migration(conn.cursor())
    assert repository.get_provider_failure(conn) == failure
    conn.close()


def test_automatic_scoring_never_reopens_finalized_jobs(database: Any) -> None:
    database.conn.execute("UPDATE jobs SET date_posted=datetime('now', '-60 days')")
    verdict = {"fit": True, "reason_type": "match", "reason_description": "Relevant experience"}
    for job_id, profile in [(1, 1), (2, 4), (3, 1)]:
        repository.save_verdict(
            database.conn, {"id": job_id}, verdict, None, 0, "flash", profile, "h"
        )
    database.conn.execute("UPDATE job_verdicts SET verdict_schema_version=2 WHERE job_id=3")
    database.conn.commit()
    assert [job["id"] for job in worker._select(database, None)] == [4]
    # A refresh and a new profile/schema must not reopen jobs already finalized.
    database.conn.execute("UPDATE jobs SET pipeline_state='new'")
    database.conn.execute("UPDATE job_verdicts SET verdict_schema_version=NULL WHERE job_id=2")
    assert [job["id"] for job in worker._select(database, 1)] == [4]
    repository.save_verdict(database.conn, {"id": 4}, verdict, None, 0, "flash", 1, "h")
    assert repository.count_pending_scoring(database.conn) == 0
    assert worker._select(database, None) == []


def test_partial_batch_success_does_not_hide_balance_failure(database: Any) -> None:
    failed = failing_chain()
    success = FakeChain([response(True), response(True)])
    config = {"scoring": {"model": "flash", "concurrency": 2, "review": {"model": "pro"}}}
    outcomes = [
        mock.Mock(with_structured_output=mock.Mock(return_value=failed)),
        mock.Mock(with_structured_output=mock.Mock(return_value=success)),
        mock.Mock(with_structured_output=mock.Mock(return_value=success)),
    ]
    with (
        mock.patch.object(worker, "Database", return_value=database),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(worker, "load_config", return_value=config),
        mock.patch("findajob.scoring.graph.structured_model", side_effect=outcomes),
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring() == 1
    assert repository.get_provider_failure(database.conn) is not None
    assert database.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0] == 1
    assert database.conn.execute("SELECT SUM(scoring_failures) FROM jobs").fetchone()[0] == 0
