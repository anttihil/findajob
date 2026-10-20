"""Integration checks for durable evidence, interviews, retries, and export gates."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from findajob.core.database import Database
from findajob.profile.builder import build_resume_for_job
from findajob.profile.copilot import MemoChatResult
from findajob.profile.models import (
    ATSScreeningVerdict,
    LayoutValidationResult,
    Profile,
    TailoredResumePayload,
)
from findajob.profile.repository import save_profile
from findajob.profile.workspace import (
    EvidenceCitation,
    ResumeReview,
    WorkspaceConflict,
    check_context_budget,
    claim_workspace,
    evidence_context,
    get_workspace,
    memo_current,
    memo_save,
    review_resume,
    save_answer,
)
from findajob.web.app import app


@pytest.fixture
def db(tmp_path: Path):
    database = Database(str(tmp_path / "workspace.db"))
    database.conn.execute(
        "INSERT INTO jobs(id, title, company, description, job_key) "
        "VALUES (1, 'Engineer', 'Acme', 'Build tools', 'acme-engineer')"
    )
    save_profile(Profile(name="Alex"), database.conn)
    database.conn.commit()
    yield database
    database.close()


def payload(summary: str = "Built useful tools.") -> TailoredResumePayload:
    return TailoredResumePayload(
        name="Alex",
        contact_line_1="Remote",
        contact_line_2="",
        summary=summary,
    )


def review(**kwargs: object) -> ResumeReview:
    values = {
        "grounded": True,
        "citations": [EvidenceCitation(claim="Built useful tools", sources=["M1"])],
        **kwargs,
    }
    return ResumeReview.model_validate(values)


def seed_draft(db: Database, question: str = "How many users adopted the tool?") -> int:
    claim_workspace(db.conn, 1)
    memo = memo_save(db.conn, "Built tools; adoption unconfirmed.", "import", None)
    cur = db.conn.execute(
        "INSERT INTO resume_drafts(job_id, run_id, attempt, memo_revision_id, inputs_json, "
        "payload_json, review_json, layout_json) VALUES (1, 'test', 1, ?, ?, ?, ?, ?)",
        (
            memo["id"],
            json.dumps(
                {"profile": Profile(name="Alex").model_dump(), "memo": memo, "model": "test"}
            ),
            payload().model_dump_json(),
            json.dumps(
                {
                    "evidence": review(questions=[question]).model_dump(),
                    "recruiter": ATSScreeningVerdict(passed=False, score=8).model_dump(),
                }
            ),
            json.dumps({"is_valid": True}),
        ),
    )
    db.conn.execute("UPDATE resume_workspaces SET status = 'interview' WHERE job_id = 1")
    db.conn.commit()
    return int(cur.lastrowid or 0)


def test_memo_full_text_roundtrip_conflicts_and_sources(db: Database):
    markdown = "long achievement\n" * 3000 + "Final correction: scope is not completed work."
    first = memo_save(db.conn, markdown, "import", None)
    assert memo_current(db.conn)["markdown"] == markdown
    assert "Final correction" in evidence_context(Profile(name="Alex"), markdown)
    assert "[M3001]" in evidence_context(Profile(), markdown)
    with pytest.raises(WorkspaceConflict):
        memo_save(db.conn, "stale edit", "manual", None)
    second = memo_save(db.conn, "corrected", "manual", first["id"])
    assert second["id"] != first["id"]
    assert (
        db.conn.execute(
            "SELECT markdown FROM career_memo_revisions WHERE id = ?", (first["id"],)
        ).fetchone()[0]
        == markdown
    )
    with pytest.raises(ValueError, match="no source text has been truncated"):
        check_context_budget("a" * 90001)


def test_interview_transaction_idempotency_and_job_ownership(db: Database):
    draft_id = seed_draft(db)
    question = "How many users adopted the tool?"
    base = memo_current(db.conn)["id"]
    answer = save_answer(db.conn, 1, draft_id, question, "Approximately five people.", base)
    assert "Approximately five people." in answer["markdown"]
    assert "Question:" in answer["markdown"]
    assert answer["source"] == "resume interview"
    assert get_workspace(db.conn, 1)["answers"][0]["draft_id"] == draft_id
    assert save_answer(db.conn, 1, draft_id, question, "Approximately five people.", base) == answer
    with pytest.raises(WorkspaceConflict):
        save_answer(db.conn, 1, draft_id, question, "Ten people", answer["id"])
    with pytest.raises(ValueError, match="does not belong"):
        save_answer(db.conn, 2, draft_id, question, "Five", answer["id"])
    with pytest.raises(ValueError, match="does not belong"):
        save_answer(db.conn, 1, draft_id, "Invent a metric?", "Yes", answer["id"])


def test_concurrent_generation_is_rejected(db: Database):
    claim_workspace(db.conn, 1)
    with pytest.raises(WorkspaceConflict):
        claim_workspace(db.conn, 1)


def test_invalid_source_citations_cannot_pass():
    with (
        patch("findajob.profile.workspace.structured_model"),
        patch(
            "findajob.profile.workspace.invoke_structured",
            return_value=review(
                citations=[EvidenceCitation(claim="unsupported", sources=["M999"])]
            ),
        ),
    ):
        result = review_resume({}, payload(), "[M1] Built tools")
    assert not result.grounded
    assert result.next_action == "revise"


def test_best_grounded_draft_survives_regression(db: Database, tmp_path: Path):
    memo_save(db.conn, "Built tools", "import", None)
    db.conn.commit()
    reviews = [
        review(next_action="revise", revision_advice="Improve framing"),
        review(next_action="revise", revision_advice="Weaker framing"),
    ]
    verdicts = [
        ATSScreeningVerdict(passed=False, score=8),
        ATSScreeningVerdict(passed=False, score=6),
    ]
    with (
        patch("findajob.profile.graph.structured_model"),
        patch(
            "findajob.profile.graph.invoke_structured",
            side_effect=[payload("First"), payload("Second")],
        ) as generator,
        patch("findajob.profile.graph.review_resume", side_effect=reviews),
        patch("findajob.profile.graph.screen_resume", side_effect=verdicts),
        patch(
            "findajob.profile.graph.validate_resume_layout",
            return_value=LayoutValidationResult(
                is_valid=True,
                estimated_points=400,
            ),
        ),
        patch("findajob.profile.graph.generated_resumes_dir", return_value=str(tmp_path)),
        patch("findajob.profile.graph.render_typst"),
        patch(
            "findajob.profile.graph.compile_typst_to_pdf", return_value=str(tmp_path / "resume.pdf")
        ),
        patch("findajob.profile.graph.verify_page_count", return_value=1),
    ):
        record = build_resume_for_job(1, db=db)
    assert record["ats_score"] == 8
    assert record["resume"]["summary"] == "First"
    workspace = get_workspace(db.conn, 1)
    assert len(workspace["drafts"]) == 2
    assert workspace["selected_draft_id"] == workspace["drafts"][1]["id"]
    assert "First" in generator.call_args_list[1].args[2][1][1]
    assert workspace["drafts"][0]["generated_resume_id"] is None
    snapshot = json.loads(
        db.conn.execute("SELECT inputs_json FROM resume_drafts LIMIT 1").fetchone()[0]
    )
    assert snapshot["memo"]["markdown"] == "Built tools"


@pytest.mark.parametrize("grounded,pages", [(False, 1), (True, 2), (True, 0)])
def test_unsupported_or_overflow_drafts_never_export(db: Database, grounded: bool, pages: int):
    with (
        patch("findajob.profile.graph.structured_model"),
        patch("findajob.profile.graph.invoke_structured", return_value=payload()),
        patch("findajob.profile.graph.review_resume", return_value=review(grounded=grounded)),
        patch(
            "findajob.profile.graph.screen_resume",
            return_value=ATSScreeningVerdict(passed=True, score=10),
        ),
        patch(
            "findajob.profile.graph.validate_resume_layout",
            return_value=LayoutValidationResult(
                is_valid=True,
                estimated_points=400,
            ),
        ),
        patch("findajob.profile.graph.render_typst"),
        patch("findajob.profile.graph.compile_typst_to_pdf", return_value="/tmp/draft.pdf"),
        patch("findajob.profile.graph.verify_page_count", return_value=pages),
    ):
        result = build_resume_for_job(1, db=db)
    assert result["pdf_path"] is None
    assert db.conn.execute("SELECT COUNT(*) FROM generated_resumes").fetchone()[0] == 0
    workspace = get_workspace(db.conn, 1)
    assert workspace["status"] == "needs_attention"
    assert len(workspace["drafts"]) == 2


def test_generation_failure_keeps_draft_and_unlocks_workspace(db: Database):
    with (
        patch("findajob.profile.graph.structured_model"),
        patch("findajob.profile.graph.invoke_structured", return_value=payload()),
        patch(
            "findajob.profile.graph.screen_resume", side_effect=RuntimeError("provider unavailable")
        ),
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            build_resume_for_job(1, db=db)
    workspace = get_workspace(db.conn, 1)
    assert workspace["status"] == "needs_attention"
    assert len(workspace["drafts"]) == 1
    assert not workspace["drafts"][0]["review"]["evidence"]["grounded"]
    claim_workspace(db.conn, 1)


def test_memo_and_interview_api(db: Database):
    with patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)):
        client = TestClient(app)
        text = "memo\n" * 10000 + "Important final correction"
        response = client.put("/api/career-memo", json={"markdown": text})
        assert response.status_code == 200
        assert response.json()["markdown"] == text
        assert client.get("/api/career-memo/download").text == text
        assert client.put("/api/career-memo", json={"markdown": "stale"}).status_code == 409
        revision_id = response.json()["id"]
        assert client.get(f"/api/career-memo/revisions/{revision_id}").json()["markdown"] == text
        assert client.get("/api/jobs/999/resume/workspace").status_code == 404


def test_memo_copilot_proposes_without_saving_then_uses_revision_guard(db: Database):
    original = memo_save(db.conn, "Original evidence", "import", None)
    db.conn.commit()
    with (
        patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)),
        patch(
            "findajob.web.resume_workspace.chat_with_memo_copilot",
            return_value=MemoChatResult(reply="Compacted", updated_markdown="Concise evidence"),
        ) as chat,
    ):
        client = TestClient(app)
        response = client.post(
            "/api/career-memo/chat",
            json={
                "current_markdown": "Unsaved editor evidence",
                "messages": [{"role": "user", "content": "Compact"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["updated_markdown"] == "Concise evidence"
        assert chat.call_args.args[1] == "Unsaved editor evidence"
        assert client.get("/api/career-memo").json() == original
        assert len(client.get("/api/career-memo/revisions").json()) == 1

        # A clarification arriving while the proposal is reviewed must not be overwritten.
        latest = client.put(
            "/api/career-memo",
            json={
                "markdown": "New clarification",
                "base_revision_id": original["id"],
            },
        ).json()
        assert (
            client.put(
                "/api/career-memo",
                json={
                    "markdown": response.json()["updated_markdown"],
                    "base_revision_id": original["id"],
                },
            ).status_code
            == 409
        )
        assert client.get("/api/career-memo").json() == latest


def test_memo_copilot_api_errors_preserve_saved_evidence(db: Database):
    original = memo_save(db.conn, "Original evidence", "import", None)
    db.conn.commit()
    with (
        patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)),
        patch("findajob.web.resume_workspace.chat_with_memo_copilot") as chat,
    ):
        client = TestClient(app)
        body = {
            "current_markdown": "Original evidence",
            "messages": [
                {"role": "user", "content": "Compact"},
            ],
        }
        chat.side_effect = ValueError("Context exceeds budget")
        assert client.post("/api/career-memo/chat", json=body).status_code == 400
        chat.side_effect = RuntimeError("Provider unavailable")
        assert client.post("/api/career-memo/chat", json=body).status_code == 500
        invalid = client.post("/api/career-memo/chat", json={**body, "messages": []})
        assert invalid.status_code == 422
        assert client.get("/api/career-memo").json() == original


def test_interview_api_rolls_back_stale_memo(db: Database):
    draft_id = seed_draft(db)
    with patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)):
        client = TestClient(app)
        response = client.post(
            "/api/jobs/1/resume/answers",
            json={
                "draft_id": draft_id,
                "question": "How many users adopted the tool?",
                "answer": "Five people",
                "base_revision_id": None,
            },
        )
        assert response.status_code == 409
        assert get_workspace(db.conn, 1)["answers"] == []
        assert "Five people" not in memo_current(db.conn)["markdown"]


def test_select_export_and_historical_sources(db: Database, tmp_path: Path):
    draft_id = seed_draft(db)
    with (
        patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)),
        patch("findajob.web.resume_workspace.generated_resumes_dir", return_value=str(tmp_path)),
    ):
        client = TestClient(app)
        endpoint = f"/api/jobs/1/resume/drafts/{draft_id}"
        result = client.post(f"{endpoint}/select")
        assert result.status_code == 200
        data = result.json()
        assert data["selected_draft_id"] == draft_id
        record_id = data["drafts"][0]["generated_resume_id"]
        row = db.conn.execute(
            "SELECT * FROM generated_resumes WHERE id = ?", (record_id,)
        ).fetchone()
        from findajob.profile.renderer import verify_page_count

        assert verify_page_count(row["pdf_path"]) == 1
        assert Path(row["typst_path"]).exists()
        current = client.get("/api/career-memo").json()
        client.put(
            "/api/career-memo",
            json={
                "markdown": "Later correction",
                "base_revision_id": current["id"],
            },
        )
        assert client.post(f"{endpoint}/select").status_code == 409
        snapshot = client.get(f"{endpoint}/sources").json()
        assert "Built tools; adoption unconfirmed." in snapshot["evidence"]
        assert "Later correction" not in snapshot["evidence"]
        assert client.get(f"/api/jobs/999/resume/drafts/{draft_id}/sources").status_code == 404


def test_interview_api_persists_and_recovers_abandoned_run(db: Database):
    draft_id = seed_draft(db)
    base_revision_id = memo_current(db.conn)["id"]
    with patch("findajob.web.resume_workspace.Database", side_effect=lambda: Database(db.db_path)):
        client = TestClient(app)
        result = client.post(
            "/api/jobs/1/resume/answers",
            json={
                "draft_id": draft_id,
                "question": "How many users adopted the tool?",
                "answer": "Approximately five users.",
                "base_revision_id": base_revision_id,
            },
        )
        assert result.status_code == 200
        reloaded = client.get("/api/jobs/1/resume/workspace").json()
        assert reloaded["answers"][0]["answer"] == "Approximately five users."
        assert reloaded["memo_revision_id"] != base_revision_id
        assert "Approximately five users." in client.get("/api/career-memo").json()["markdown"]
        claim_workspace(db.conn, 1)
        db.conn.commit()
        assert client.post("/api/jobs/1/resume/recover").status_code == 409
        db.conn.execute(
            "UPDATE resume_workspaces SET updated_at = datetime('now', '-2 hours') WHERE job_id = 1"
        )
        db.conn.commit()
        recovered = client.post("/api/jobs/1/resume/recover")
        assert recovered.status_code == 200
        assert recovered.json()["status"] == "needs_attention"
        assert len(recovered.json()["drafts"]) == 1


def test_interview_resume_uses_saved_answer_previous_draft_and_critique(db: Database):
    memo_save(db.conn, "Built useful tools.", "import", None)
    db.conn.commit()
    question = "How many users adopted the tool?"
    with (
        patch("findajob.profile.graph.structured_model"),
        patch(
            "findajob.profile.graph.invoke_structured",
            side_effect=[payload("First draft"), payload("Revised draft")],
        ) as generator,
        patch(
            "findajob.profile.graph.review_resume",
            side_effect=[
                review(
                    next_action="interview",
                    questions=[question],
                    revision_advice="Clarify adoption",
                ),
                review(),
            ],
        ),
        patch(
            "findajob.profile.graph.screen_resume",
            return_value=ATSScreeningVerdict(passed=False, score=8),
        ),
        patch(
            "findajob.profile.graph.validate_resume_layout",
            return_value=LayoutValidationResult(
                is_valid=True,
                estimated_points=400,
            ),
        ),
        patch("findajob.profile.graph.render_typst"),
        patch("findajob.profile.graph.compile_typst_to_pdf", return_value="/tmp/draft.pdf"),
        patch("findajob.profile.graph.verify_page_count", return_value=1),
    ):
        first = build_resume_for_job(1, db=db)
        assert first["workspace"]["status"] == "interview"
        draft_id = first["workspace"]["selected_draft_id"]
        save_answer(
            db.conn, 1, draft_id, question, "Approximately five users.", memo_current(db.conn)["id"]
        )
        db.conn.commit()
        second = build_resume_for_job(1, db=db)
    assert second["workspace"]["status"] == "complete"
    assert len(second["workspace"]["drafts"]) == 2
    messages = generator.call_args_list[1].args[2]
    assert "Approximately five users." in messages[0][1]
    assert "First draft" in messages[1][1]
    assert "Clarify adoption" in messages[1][1]
    assert first["id"] != second["id"]
