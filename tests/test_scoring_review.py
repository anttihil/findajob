"""Single-stage completion, retry accounting, recovery, and legacy compatibility."""

import io
import sqlite3
from contextlib import redirect_stdout
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from findajob.core.migrations import migrate
from findajob.profile.models import PriorityVerdict
from findajob.scoring import repository, worker
from findajob.scoring.graph import MAX_ATTEMPTS, CallBudget, build_graph
from findajob.scoring.prompts import build_system, prompt_hash
from tests.test_scoring_module import PROFILE, FakeChain, bad_response, ok_response, posting


@pytest.fixture(autouse=True)
def off_peak() -> Any:
    with (
        mock.patch.object(worker, "_using_deepseek", return_value=False),
        mock.patch.object(worker, "get_llm_provider", return_value=SimpleNamespace(name="test")),
    ):
        yield


def response(fit: bool) -> dict[str, Any]:
    result = ok_response()
    result["parsed"] = PriorityVerdict(
        priority="first" if fit else "low", reason="Decisive posting and profile evidence."
    )
    result["raw"].findajob_cost_usd = 0.01
    result["raw"].response_metadata = {
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 40,
        }
    }
    return result


def models(first: FakeChain, review: FakeChain) -> Any:
    def resolve(model: str, **kwargs: Any) -> mock.Mock:
        return mock.Mock(
            with_structured_output=mock.Mock(return_value=first if model == "flash" else review)
        )

    return resolve


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


@pytest.mark.parametrize("priority", ["first", "consider", "low"])
def test_one_call_completes_without_reviewer(priority: str) -> None:
    answer = response(True)
    answer["parsed"] = PriorityVerdict.model_validate({"priority": priority, "reason": "Evidence"})
    chain = FakeChain([answer])
    with mock.patch(
        "findajob.scoring.graph.structured_model",
        return_value=mock.Mock(with_structured_output=mock.Mock(return_value=chain)),
    ) as model:
        state = build_graph().invoke(
            {"system": build_system(PROFILE), "posting": posting(), "model": "flash"}
        )
    assert state["verdict"] == {"priority": priority, "reason": "Evidence"}
    assert state["attempts"] == len(chain.calls) == 1
    assert state["cost_usd"] == pytest.approx(0.01)
    model.assert_called_once_with("flash", role="scoring", max_tokens=256, max_retries=0)


def test_failures_use_one_bounded_retry_limit() -> None:
    chain = FakeChain([bad_response()] * MAX_ATTEMPTS)
    reviewer = FakeChain([])
    with mock.patch("findajob.scoring.graph.structured_model", side_effect=models(chain, reviewer)):
        state = build_graph().invoke(
            {"posting": posting(), "model": "flash", "review_model": "pro"}
        )
    assert state["verdict"] is None and state["error"]
    assert len(chain.calls) == MAX_ATTEMPTS and not reviewer.calls


def test_failed_assessment_resumes_usage_and_only_charges_new_calls(db: Any) -> None:
    bad = response(False)
    bad["parsed"] = None
    bad["parsing_error"] = "no tool call"
    chain = FakeChain([bad] * MAX_ATTEMPTS)
    config = {"scoring": {"model": "flash", "max_usd_per_run": None}}
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(worker, "load_config", return_value=config),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch(
            "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
        ),
    ):
        with pytest.raises(RuntimeError, match="Scoring failed"):
            worker.score_job(1, db=db)
        assert repository.count_pending_scoring(db.conn) == 1
        chain.responses = [response(False)]
        with redirect_stdout(io.StringIO()) as out:
            assert worker.run_scoring() == 0
    final = db.conn.execute("SELECT * FROM job_verdicts").fetchone()
    assert final["fit"] is None and final["priority"] == "low"
    assert final["cost_usd"] == pytest.approx(0.04) and final["tokens_in"] == 400
    assert "cost:       $0.0100" in out.getvalue()
    assert repository.count_pending_scoring(db.conn) == 0


def test_completed_assessment_publishes_after_interruption_without_calls(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    chain = FakeChain([response(True)])
    with mock.patch(
        "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
    ):
        original = list(worker._score_states(db, [job], "rules", "flash", "", 1, "hash", 1))
    assert db.conn.execute("SELECT COUNT(*) FROM job_verdicts").fetchone()[0] == 0
    with mock.patch("findajob.scoring.graph.structured_model") as provider:
        recovered = list(worker._score_states(db, [job], "rules", "flash", "", 1, "hash", 1))
    provider.assert_not_called()
    assert recovered[0][1]["verdict"] == original[0][1]["verdict"]
    assert recovered[0][1]["run_cost_usd"] == recovered[0][1]["run_calls"] == 0


@pytest.mark.parametrize("change", ["description", "location", "profile", "model", "schema"])
def test_input_changes_invalidate_cached_assessment(db: Any, change: str) -> None:
    job = db.query_jobs()["jobs"][0]
    repository.save_assessment(db.conn, job, 1, "hash", "flash", {"verdict": {"priority": "first"}})
    assert repository.load_assessment(db.conn, job, 1, "hash", "flash")
    phash, model = "hash", "flash"
    if change in ("description", "location"):
        job[change] = "Changed"
    elif change in ("profile", "schema"):
        phash = "changed"
    else:
        model = "other"
    assert not repository.load_assessment(db.conn, job, 1, phash, model)


def test_zero_budget_makes_no_calls(db: Any) -> None:
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(worker, "load_config", return_value={"scoring": {"max_usd_per_run": 0}}),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch("findajob.scoring.graph.structured_model") as provider,
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring() == 0
    provider.assert_not_called()


def test_budget_counts_malformed_attempts() -> None:
    bad = response(False)
    bad["parsed"] = None
    bad["parsing_error"] = "no tool call"
    chain = FakeChain([bad] * 3)
    with mock.patch(
        "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
    ):
        state = build_graph().invoke({"posting": {}, "model": "flash", "budget": CallBudget(0.015)})
    assert state["budget_exhausted"] and len(chain.calls) == 2
    assert state["cost_usd"] == pytest.approx(0.02)


def test_explicit_reassessment_preserves_history_and_ignores_old_reviewer(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    repository.save_verdict(
        db.conn,
        job,
        {"fit": True, "reason_type": "match", "reason_description": "Historical"},
        None,
        0,
        "old",
        1,
        "old",
    )
    assert [job["id"] for job in repository.select_scoring_backlog(db.conn)] == [1]
    chain, reviewer = FakeChain([response(False), response(True)]), FakeChain([])
    with (
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(
            worker,
            "load_config",
            return_value={
                "scoring": {"model": "flash", "review": {"enabled": True, "model": "pro"}}
            },
        ),
        mock.patch("findajob.scoring.graph.structured_model", side_effect=models(chain, reviewer)),
    ):
        assert worker.score_job(1, db=db, review_only=True)["priority"] == "low"
        assert worker.score_job(1, db=db)["priority"] == "first"
    assert len(chain.calls) == 2 and not reviewer.calls
    historical = db.conn.execute(
        "SELECT * FROM job_verdict_history ORDER BY archived_at"
    ).fetchall()
    assert len(historical) == 2 and historical[0]["fit"] == 1
    assert historical[0]["reason_description"] == "Historical"
    assert historical[1]["priority"] == "low"
    assert db.conn.execute("SELECT model FROM job_verdicts").fetchone()[0] == "flash"


@pytest.mark.parametrize("old_fit", [False, True])
def test_batch_backfills_binary_verdict_once_and_preserves_history(db: Any, old_fit: bool) -> None:
    from findajob.search.repository import upsert_posting

    job = db.query_jobs()["jobs"][0]
    repository.save_verdict(
        db.conn,
        job,
        {"fit": old_fit, "reason_type": "match", "reason_description": "Historical"},
        None,
        0,
        "old",
        1,
        "old",
    )
    db.conn.commit()
    chain = FakeChain([response(True)])
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(worker, "load_config", return_value={"scoring": {"model": "flash"}}),
        mock.patch(
            "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
        ),
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring(limit=1) == 0
        final = db.conn.execute("SELECT * FROM job_verdicts WHERE job_id=1").fetchone()
        assert final["priority"] == "first" and final["fit"] is None
        assert repository.count_pending_scoring(db.conn) == 0
        saved = db.conn.execute("SELECT status, pipeline_state FROM jobs WHERE id=1").fetchone()
        assert saved["status"] == job["status"] and saved["pipeline_state"] == "scored"

        upsert_posting(
            db.conn,
            {
                "job_key": "one",
                "description": "Refreshed posting content. " * 20,
                "pipeline_state": "new",
            },
        )
        assert worker.run_scoring(limit=1) == 0

    assert len(chain.calls) == 1
    assert repository.count_pending_scoring(db.conn) == 0
    history = db.conn.execute("SELECT * FROM job_verdict_history WHERE job_id=1").fetchall()
    assert len(history) == 1 and history[0]["fit"] == int(old_fit)
    assert history[0]["model"] == "old" and history[0]["reason_description"] == "Historical"


def test_failed_backfill_keeps_binary_verdict_active_and_pending(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    repository.save_verdict(
        db.conn,
        job,
        {"fit": True, "reason_type": "match", "reason_description": "Historical"},
        None,
        0,
        "old",
        1,
        "old",
    )
    db.conn.commit()
    chain = FakeChain([bad_response()] * MAX_ATTEMPTS)
    with (
        mock.patch.object(worker, "Database", return_value=db),
        mock.patch.object(worker, "load_active", return_value=(1, {}, PROFILE)),
        mock.patch.object(worker, "load_config", return_value={"scoring": {"model": "flash"}}),
        mock.patch(
            "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
        ),
        redirect_stdout(io.StringIO()),
    ):
        assert worker.run_scoring(limit=1) == 0
    final = db.conn.execute("SELECT * FROM job_verdicts WHERE job_id=1").fetchone()
    assert final["fit"] == 1 and final["priority"] is None and final["model"] == "old"
    assert db.conn.execute("SELECT COUNT(*) FROM job_verdict_history").fetchone()[0] == 0
    assert repository.count_pending_scoring(db.conn) == 1


def test_old_threshold_and_reviewer_settings_do_not_affect_prompt() -> None:
    assert prompt_hash(PROFILE, 70) == prompt_hash(PROFILE, 90, review_model="pro")
    assert prompt_hash(PROFILE) != prompt_hash(PROFILE + "Changed guidance")


def test_provider_change_invalidates_reusable_work(db: Any) -> None:
    job = db.query_jobs()["jobs"][0]
    chain = FakeChain([response(True), response(False)])
    with (
        mock.patch(
            "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
        ),
        mock.patch.object(
            worker, "get_llm_provider", return_value=SimpleNamespace(name="provider-a")
        ),
    ):
        first = list(worker._score_states(db, [job], "rules", "flash", "", 1, "hash", 1))
        reused = list(worker._score_states(db, [job], "rules", "flash", "", 1, "hash", 1))
    assert first[0][1]["verdict"] == reused[0][1]["verdict"]
    assert len(chain.calls) == 1
    with (
        mock.patch(
            "findajob.scoring.graph.structured_model", side_effect=models(chain, FakeChain([]))
        ),
        mock.patch.object(
            worker, "get_llm_provider", return_value=SimpleNamespace(name="provider-b")
        ),
    ):
        changed = list(worker._score_states(db, [job], "rules", "flash", "", 1, "hash", 1))
    assert len(chain.calls) == 2 and changed[0][1]["verdict"]["priority"] == "low"
