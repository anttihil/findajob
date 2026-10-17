"""Exercise LangGraph review and durable recovery without live model calls."""

import io
import json
import sqlite3
from contextlib import redirect_stdout
from typing import Any
from unittest import mock

import pytest

from findajob.core.migrations import migrate
from findajob.scoring import repository, worker
from findajob.scoring.graph import MAX_ATTEMPTS, build_graph
from findajob.scoring.prompts import build_system, prompt_hash
from tests.test_scoring_module import PROFILE, FakeChain, bad_response, ok_response, posting


def response(fit: bool) -> dict[str, Any]:
    result = ok_response(fit=fit, reason_type="match" if fit else "skills")
    raw = result["raw"]
    raw.findajob_cost_usd = 0.01
    raw.response_metadata = {
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 40,
        }
    }
    return result


def models(first: FakeChain, review: FakeChain) -> Any:
    def resolve(model: str, **kwargs: Any) -> mock.Mock:
        result = mock.Mock()
        result.with_structured_output.return_value = first if model == "flash" else review
        return result

    return resolve


@pytest.mark.parametrize(
    ("initial", "final"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_review_all_verdicts(initial: bool, final: bool) -> None:
    first, review = FakeChain([response(initial)]), FakeChain([response(final)])
    with mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)):
        state = build_graph().invoke(
            {
                "system": build_system(PROFILE),
                "posting": posting(),
                "model": "flash",
                "review_model": "pro",
            }
        )
    assert state["first_line"]["fit"] is initial
    assert state["verdict"]["fit"] is final
    assert state["attempts"] == 2
    assert state["review_attempts"] == 1
    assert state["cost_usd"] == pytest.approx(0.02)
    assert state["usage"]["prompt"] == 200
    assert PROFILE in review.calls[0][0][1]
    assert json.dumps(state["first_line"]) in review.calls[0][1][1]


def test_each_stage_has_its_own_retry_limit() -> None:
    first = FakeChain([bad_response(), bad_response(), response(True)])
    review = FakeChain([bad_response()] * MAX_ATTEMPTS)
    with mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)):
        state = build_graph().invoke(
            {
                "system": "rules",
                "posting": posting(),
                "model": "flash",
                "review_model": "pro",
            }
        )
    assert len(first.calls) == MAX_ATTEMPTS
    assert len(review.calls) == MAX_ATTEMPTS
    assert state["first_line"]["fit"] is True
    assert state["verdict"] is None
    assert state["attempts"] == 2 * MAX_ATTEMPTS
    assert state["error"]


def test_first_line_failure_never_reaches_review() -> None:
    first, review = FakeChain([bad_response()] * MAX_ATTEMPTS), FakeChain([])
    with mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)):
        state = build_graph().invoke(
            {
                "system": "rules",
                "posting": posting(),
                "model": "flash",
                "review_model": "pro",
            }
        )
    assert state["verdict"] is None
    assert not state.get("first_line")
    assert not review.calls


@pytest.fixture
def db() -> Any:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    migrate(conn)
    conn.execute(
        "INSERT INTO jobs (id, job_key, title, description, date_found) "
        "VALUES (1, 'one', 'Platform Engineer', ?, datetime('now'))",
        ("x" * 400,),
    )
    conn.commit()
    database = mock.Mock(conn=conn)
    database.query_jobs.side_effect = lambda **_kwargs: {
        "jobs": [dict(conn.execute("SELECT * FROM jobs WHERE id=1").fetchone())],
    }
    yield database
    conn.close()


def test_failed_review_resumes_and_only_charges_new_calls(db: Any) -> None:
    first = FakeChain([response(True)])
    failed = response(False)
    failed["parsed"] = None
    failed["parsing_error"] = "no tool call"
    review = FakeChain([failed] * MAX_ATTEMPTS)
    config = {
        "scoring": {
            "model": "flash",
            "concurrency": 1,
            "max_usd_per_run": None,
            "review": {"model": "pro"},
        }
    }
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(worker, "load_config", return_value=config),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)),
    ):
        with pytest.raises(RuntimeError, match="Scoring failed"):
            worker.score_job(1, db=db)
        assert db.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0] == 0
        assert repository.count_pending_scoring(db.conn, 1) == 1
        row = db.conn.execute("SELECT * FROM verdict_reviews").fetchone()
        assert json.loads(row["first_line_state"])["first_line"]["fit"] is True
        assert json.loads(row["review_state"])["cost_usd"] == pytest.approx(0.04)
        review.responses = [response(False)]
        out = io.StringIO()
        with redirect_stdout(out):
            assert worker.run_scoring() == 0
    assert len(first.calls) == 1
    assert len(review.calls) == 4
    final = db.conn.execute("SELECT * FROM job_verdicts").fetchone()
    assert final["fit"] == 0
    assert final["model"] == "pro"
    assert final["cost_usd"] == pytest.approx(0.05)
    assert final["tokens_in"] == 500
    assert "cost:       $0.0100" in out.getvalue()
    assert repository.count_pending_scoring(db.conn, 1) == 0
    assert db.conn.execute("SELECT finalized FROM verdict_reviews").fetchone()[0] == 1


def test_capture_is_durable_before_review_and_only_final_updates_cell(db: Any) -> None:
    first, review = FakeChain([response(True)]), FakeChain([response(False)])
    job = db.query_jobs()["jobs"][0]
    job["scrape_cell_id"] = 5
    original_invoke = review.invoke

    def check_boundary(messages: list[Any]) -> dict[str, Any]:
        row = db.conn.execute("SELECT first_line_state FROM verdict_reviews").fetchone()
        assert json.loads(row[0])["first_line"]["fit"] is True
        assert db.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0] == 0
        return original_invoke(messages)

    review.invoke = check_boundary
    with (
        mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)),
        mock.patch("findajob.search.repository.update_cell_quality") as update,
    ):
        states = list(worker._score_states(db, [job], "rules", "flash", "pro", 1, "hash", 1))
        state = states[0][1]
        update.assert_not_called()
        repository.save_verdict(
            db.conn, job, state["verdict"], state["usage"], state["cost_usd"], "pro", 1, "hash"
        )
        update.assert_called_once_with(db.conn, 5, False)


def test_budget_stops_after_current_batch(db: Any) -> None:
    for job_id in (2, 3, 4):
        db.conn.execute(
            "INSERT INTO jobs (id, job_key, title, description, date_found) "
            "VALUES (?, ?, 'Engineer', ?, datetime('now'))",
            (job_id, str(job_id), "x" * 400),
        )
    db.conn.commit()
    first = FakeChain([response(True)] * 4)
    review = FakeChain([response(False)] * 4)
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(
            worker,
            "load_config",
            return_value={
                "scoring": {
                    "model": "flash",
                    "concurrency": 2,
                    "max_usd_per_run": 0.001,
                    "review": {"model": "pro"},
                }
            },
        ),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)),
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring() == 0
    assert len(first.calls) == 2
    assert len(review.calls) == 2
    assert db.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0] == 2


def test_review_prompt_is_versioned() -> None:
    assert prompt_hash(PROFILE) != prompt_hash(PROFILE, review_model="pro")


def test_completed_review_can_be_published_after_interruption_without_calls(db: Any) -> None:
    first, review = FakeChain([response(True)]), FakeChain([response(False)])
    job = db.query_jobs()["jobs"][0]
    with mock.patch("findajob.scoring.graph.structured_model", side_effect=models(first, review)):
        original = list(worker._score_states(db, [job], "rules", "flash", "pro", 1, "hash", 1))
    # Simulate exit after review was saved but before the final verdict was published.
    with mock.patch("findajob.scoring.graph.structured_model") as model:
        recovered = list(worker._score_states(db, [job], "rules", "flash", "pro", 1, "hash", 1))
        model.assert_not_called()
    assert recovered[0][1]["verdict"] == original[0][1]["verdict"]
    assert recovered[0][1]["run_cost_usd"] == 0
    assert recovered[0][1]["run_calls"] == 0
    assert recovered[0][1]["run_usage"]["prompt"] == 0


def test_recovery_invalidated_by_changed_inputs(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    repository.save_review_stage(
        db.conn,
        job,
        {"first_line": {"fit": True}, "verdict": None},
        1,
        "hash",
        "flash",
        "pro",
        first_line=True,
    )
    assert repository.load_pending_review(db.conn, job, 1, "hash", "flash", "pro")
    assert repository.load_pending_review(db.conn, job, 2, "hash", "flash", "pro") is None
    assert repository.load_pending_review(db.conn, job, 1, "new", "flash", "pro") is None
    assert repository.load_pending_review(db.conn, job, 1, "hash", "other", "pro") is None
    assert repository.load_pending_review(db.conn, job, 1, "hash", "flash", "other") is None
    job["description"] = "changed description"
    assert repository.load_pending_review(db.conn, job, 1, "hash", "flash", "pro") is None


def test_zero_budget_makes_no_calls(db: Any) -> None:
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(
            worker,
            "load_config",
            return_value={
                "scoring": {
                    "model": "flash",
                    "max_usd_per_run": 0,
                }
            },
        ),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch("findajob.scoring.graph.structured_model") as model,
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring() == 0
        model.assert_not_called()


def test_pending_review_is_queued_even_when_an_older_final_verdict_exists(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    verdict = {"fit": True, "reason_type": "match", "reason_description": "Earlier assessment"}
    repository.save_verdict(db.conn, job, verdict, None, 0, "flash", 1, "old")
    assert repository.count_pending_scoring(db.conn, 1) == 0
    repository.save_review_stage(
        db.conn,
        job,
        {"first_line": verdict, "verdict": None},
        1,
        "new",
        "flash",
        "pro",
        first_line=True,
    )
    assert repository.count_pending_scoring(db.conn, 1) == 1
    assert len(repository.select_scoring_backlog(db.conn, 1)) == 1
    assert repository.get_pending_scoring_stats(db.conn, 1)[0] == 1
    repository.save_verdict(db.conn, job, verdict, None, 0, "pro", 1, "new")
    assert repository.count_pending_scoring(db.conn, 1) == 0
