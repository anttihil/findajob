import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from findajob.labeler.cli import main
from findajob.labeler.evaluation import (
    EvaluationRunner,
    digest,
    evaluation_defaults,
    load_dataset,
    metrics,
    score_prompt,
)
from findajob.profile.models import JobFitVerdict


@pytest.fixture
def dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(
        "findajob.core.llm.get_llm_provider",
        lambda _name=None: SimpleNamespace(name="test-provider"),
    )
    root = tmp_path / "export"
    root.mkdir()
    rows = []
    for index, (outcome, split) in enumerate(
        [
            ("apply", "validation"),
            ("pass", "validation"),
            ("stretch", "validation"),
            ("insufficient", "validation"),
            ("pass", "held_out"),
        ]
    ):
        rows.append(
            {
                "example_id": index,
                "family_key": f"family-{index}",
                "split": split,
                "job": {"title": f"Job {index}", "description": "Original posting"},
                "profile_text": "Frozen profile",
                "human_outcome": outcome,
                "human_reason": "specialization",
                "human_notes": "PRIVATE HUMAN NOTE",
                "model_reason_type": "skills",
                "provenance": {"rubric_version": "old"},
            }
        )
    text = "".join(json.dumps(row) + "\n" for row in rows)
    (root / "examples.jsonl").write_text(text, encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": "careerradar-verdict-labels-v1",
                "version": "baseline-v1",
                "example_count": len(rows),
                "files": {"examples.jsonl": {"sha256": digest(text)}},
            }
        ),
        encoding="utf-8",
    )
    return root


def test_paired_run_uses_frozen_inputs_and_survives_failures(dataset: Path) -> None:
    runner = EvaluationRunner(str(dataset.parent / "runs"))
    run = runner.create(
        dataset_path=str(dataset),
        model="test-model",
        candidate_rules="Candidate change",
        profile_boundary="Boundary",
    )
    seen = []

    def score(system: str, job: dict[str, Any], model: str) -> dict[str, Any]:
        seen.append((system, job, model))
        assert "PRIVATE HUMAN NOTE" not in system
        assert "Frozen profile" in system
        assert "human_outcome" not in job
        assert model == "test-model"
        if job["title"] == "Job 2" and "Candidate change" in system:
            raise RuntimeError("Provider unavailable")
        return {
            "outcome": "apply",
            "verdict": {"reason_description": "Evidence"},
            "attempts": 1,
            "usage": {"prompt": 10, "completion": 2},
            "cost_usd": 0.01,
        }

    finished = runner.execute(run["id"], scorer=score)
    assert finished["status"] == "completed_with_errors"
    assert finished["completed"] == finished["total"] == len(seen) == 6
    assert {item[1]["title"] for item in seen} == {"Job 0", "Job 1", "Job 2"}
    assert "Candidate change" not in seen[0][0]
    assert "Candidate change" in seen[1][0]
    assert "Candidate change" in seen[2][0]  # alternating order
    a, b = finished["summary"]["baseline"], finished["summary"]["candidate"]
    assert a["agreement"] == pytest.approx(1 / 3)
    assert a["weighted_agreement"] == 0.25
    assert a["false_positive_match_rate"] == pytest.approx(1 / 3)
    assert a["insufficient"] == 1
    assert b["failed"] == 1
    assert b["cost_usd"] == 0.02
    assert b["tokens"]["prompt"] == 20
    assert a["strata"]["human_reason"]["specialization"]["eligible"] == 3
    persisted = EvaluationRunner(str(runner.root)).get(run["id"])
    assert persisted == finished
    assert persisted["dataset_manifest"]["files"]["examples.jsonl"]["sha256"]
    assert persisted["examples"][0]["prompt_hashes"]["baseline"] == digest(seen[0][0])
    assert runner.list_runs()[0]["id"] == run["id"]
    with pytest.raises(ValueError, match="already started"):
        runner.execute(run["id"], scorer=score)


def test_dataset_verification_and_no_family_leakage(dataset: Path) -> None:
    manifest, records = load_dataset(str(dataset), "held_out")
    assert len(records) == 1
    with pytest.raises(ValueError, match="No evaluable"):
        load_dataset(str(dataset), "optimization")
    path = dataset / "examples.jsonl"
    text = path.read_text()
    path.write_text(text + " ")
    with pytest.raises(ValueError, match="checksum"):
        load_dataset(str(dataset), "all")
    rows = [json.loads(line) for line in text.splitlines()]
    rows[-1]["family_key"] = rows[0]["family_key"]
    changed = "".join(json.dumps(row) + "\n" for row in rows)
    path.write_text(changed)
    manifest["files"]["examples.jsonl"]["sha256"] = digest(changed)
    (dataset / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="multiple splits"):
        load_dataset(str(dataset), "all")


def test_metrics_define_false_negatives_and_unknown_cost() -> None:
    rows = [
        {"human_outcome": "apply", "baseline": {"outcome": "pass"}},
        {"human_outcome": "stretch", "baseline": {"outcome": "pass"}},
        {"human_outcome": "pass", "baseline": {"outcome": "pass"}},
        {"human_outcome": "apply", "baseline": {"outcome": None, "error": "failed"}},
    ]
    report = metrics(rows, "baseline")
    assert report["false_negatives"] == 2
    assert report["false_negative_rate"] == 1
    assert report["false_positive_match_rate"] is None
    assert report["agreement"] == 0.25
    assert report["failed"] == 1
    assert report["cost_usd"] is None


def test_cli_persists_results_and_returns_failure_status(
    dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "findajob.labeler.evaluation.score_prompt",
        lambda *_args: {"outcome": "pass", "attempts": 1},
    )
    args = [
        "--label-db",
        str(dataset.parent / "labels.db"),
        "evaluate",
        "--dataset",
        str(dataset),
        "--model",
        "test-model",
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert Path(output["output_path"]).exists()
    assert output["summary"]["baseline"]["false_negatives"] == 2
    monkeypatch.setattr(
        "findajob.labeler.evaluation.score_prompt",
        lambda *_args: {"outcome": None, "error": "No verdict"},
    )
    assert main(args) == 1


def test_unfinished_run_is_reported_as_interrupted(dataset: Path) -> None:
    runner = EvaluationRunner(str(dataset.parent / "runs"))
    run = runner.create(dataset_path=str(dataset), model="test-model")
    assert runner.get(run["id"])["status"] == "interrupted"
    with pytest.raises(ValueError, match="Invalid run"):
        runner.get("../../etc/passwd")


def test_evaluation_defaults_come_from_dotenv(
    dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from findajob.core.config import load_env
    from findajob.core.llm.factory import get_llm_provider

    env = dataset.parent / ".env"
    env.write_text("LLM_PROVIDER=codex\nLLM_SCORING_MODEL=model-from-dotenv\n")
    monkeypatch.setenv("LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("LLM_SCORING_MODEL", "old-model")
    monkeypatch.setattr("findajob.core.config.ENV_PATH", str(env))
    monkeypatch.setattr("findajob.core.llm.get_llm_provider", get_llm_provider)
    load_env()
    runner = EvaluationRunner(str(dataset.parent / "runs"))
    run = runner.create(dataset_path=str(dataset))
    assert run["model"] == "model-from-dotenv"
    assert run["provider"] == "codex"
    assert evaluation_defaults() == {"provider": "codex", "model": "model-from-dotenv"}
    explicit = runner.create(dataset_path=str(dataset), provider="deepseek", model="override-model")
    assert explicit["provider"] == "deepseek"
    assert explicit["model"] == "override-model"


@pytest.mark.parametrize("cost", [None, 0.03])
def test_runner_adapter_reuses_retries_pins_provider_and_tracks_cost_coverage(
    monkeypatch: pytest.MonkeyPatch,
    cost: float | None,
) -> None:
    raw = SimpleNamespace(
        response_metadata={"token_usage": {"prompt_tokens": 10, "completion_tokens": 2}},
        findajob_cost_usd=cost,
    )
    model = Mock()
    model.with_structured_output.return_value.invoke.side_effect = [
        {"raw": raw, "parsed": None, "parsing_error": "Missing tool call"},
        {
            "raw": raw,
            "parsed": JobFitVerdict(
                fit=True, reason_type="match", reason_description="Direct platform evidence."
            ),
        },
    ]
    factory = Mock(return_value=model)
    monkeypatch.setattr("findajob.scoring.graph.structured_model", factory)
    result = score_prompt("Frozen prompt", {"title": "Platform"}, "test-model", "test-provider")
    assert result["outcome"] == "apply"
    assert result["attempts"] == 2
    assert result["usage"]["prompt"] == 20
    assert result["cost_usd"] == (None if cost is None else 0.06)
    factory.assert_called_with("test-model", role="scoring", provider="test-provider")
