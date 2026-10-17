"""Offline paired prompt evaluations against checksum-verified, frozen human labels."""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from findajob.labeler.database import utc_now
from findajob.scoring.prompts import build_system, render_posting

DEFAULT_CANDIDATE_RULES = """Apply the fit threshold to the role's core work, not a count of
matching keywords. Use the following checks before choosing the verdict:
- Identify the primary deliverables and required depth from responsibilities and required
  qualifications, not just the title. Separate these from preferred and secondary tools.
- Map each role-defining requirement to explicit profile evidence at the requested depth.
  A listed skill supports familiarity; a project supports only the work it describes.
  Do not invent capabilities, production maturity, or years in a specialization. Total
  engineering tenure is not tenure in every listed skill. A pilot is not evidence of
  years operating production systems; adjacent terminology is not direct experience.
- Treat a required language/framework as core when the posting calls for strong production
  proficiency in it to deliver the main system. Experience with another backend stack or
  a matching frontend does not make that requirement secondary. Secondary tools remain
  learnable when the candidate already demonstrates the core work at comparable depth.
- Keep distinct specializations distinct: LLM API/agent application work does not establish
  statistical modeling or production ML expertise; container/cloud work does not establish
  deep cluster orchestration expertise. Credit the demonstrated work without expanding it.
- Scope alternatives precisely. If adjacent experience is accepted for one requirement,
  that allowance does not waive separate required expertise elsewhere in the posting.
- A positive verdict must be supported on the core work without assuming a new specialization
  or substantial core-stack retraining. If a role-defining requirement lacks that support,
  return fit=false and name the decisive gap. Peripheral strengths, company affinity, and
  preferred skills cannot compensate for that gap. Do not reject for a missing preferred
  item, an incidental tool, or a modest seniority step when the core work is supported.
Keep the final explanation concise and consistent with the verdict, identifying the
decisive core evidence or gap rather than a list of shared keywords."""

SPLITS = {"optimization", "validation", "held_out"}
OUTCOMES = ("apply", "stretch", "pass")


def evaluation_defaults() -> dict[str, str]:
    """Resolve .env-backed defaults through the application's shared LLM settings."""
    from findajob.core.llm import get_llm_provider, get_model_for_role

    provider = get_llm_provider()
    return {"provider": provider.name, "model": get_model_for_role("scoring", provider=provider)}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_dataset(path: str, split: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if split not in SPLITS | {"all"}:
        raise ValueError("Unknown dataset split.")
    root = Path(path).expanduser().resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    data = (root / "examples.jsonl").read_bytes()
    if manifest.get("format") != "careerradar-verdict-labels-v1":
        raise ValueError("Unsupported dataset format.")
    if hashlib.sha256(data).hexdigest() != manifest["files"]["examples.jsonl"]["sha256"]:
        raise ValueError("Dataset checksum mismatch; create a new export.")
    text = data.decode("utf-8")
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    if len(records) != manifest["example_count"]:
        raise ValueError("Dataset record count does not match manifest.")
    families: dict[str, str] = {}
    ids: set[int] = set()
    for record in records:
        if record["example_id"] in ids:
            raise ValueError("Duplicate example ID in dataset.")
        ids.add(record["example_id"])
        if record["split"] not in SPLITS or record["human_outcome"] not in (
            *OUTCOMES,
            "insufficient",
        ):
            raise ValueError("Invalid split or human outcome in dataset.")
        family = record["family_key"]
        if families.setdefault(family, record["split"]) != record["split"]:
            raise ValueError("A posting family appears in multiple splits.")
    selected = [r for r in records if split == "all" or r["split"] == split]
    if not any(r["human_outcome"] != "insufficient" for r in selected):
        raise ValueError("No evaluable labels in the selected split.")
    return manifest, selected


def metrics(rows: list[dict[str, Any]], variant: str) -> dict[str, Any]:
    eligible = [r for r in rows if r["human_outcome"] in OUTCOMES]
    scored = [r for r in eligible if r.get(variant, {}).get("outcome") in OUTCOMES]
    matrix = {human: dict.fromkeys(OUTCOMES, 0) for human in OUTCOMES}
    correct = fp = fn = matches = passes = opportunities = 0
    weighted_correct = 0
    for row in scored:
        human, prediction = row["human_outcome"], row[variant]["outcome"]
        matrix[human][prediction] += 1
        correct += human == prediction
        weighted_correct += (2 if human == "pass" else 1) * (human == prediction)
        fp += human == "pass" and prediction == "apply"
        fn += human in ("apply", "stretch") and prediction == "pass"
        matches += prediction == "apply"
        passes += human == "pass"
        opportunities += human in ("apply", "stretch")
    weight = sum(2 if r["human_outcome"] == "pass" else 1 for r in eligible)
    outputs = [r[variant] for r in rows if variant in r]
    usage = [o["usage"] for o in outputs if o.get("usage") is not None]
    costs = [o["cost_usd"] for o in outputs if o.get("cost_usd") is not None]
    return {
        "eligible": len(eligible),
        "scored": len(scored),
        "failed": len(eligible) - len(scored),
        "insufficient": len(rows) - len(eligible),
        "agreement": correct / len(eligible) if eligible else None,
        "weighted_agreement": weighted_correct / weight if weight else None,
        "false_positive_matches": fp,
        "false_positive_match_rate": fp / matches if matches else None,
        "false_positive_rate": fp / passes if passes else None,
        "false_negatives": fn,
        "false_negative_rate": fn / opportunities if opportunities else None,
        "confusion_matrix": matrix,
        "calls": sum(o.get("attempts", 0) for o in outputs),
        "usage_recorded": len(usage),
        "cost_recorded": len(costs),
        "tokens": {
            key: sum(u.get(key, 0) for u in usage)
            for key in ("prompt", "completion", "cache_hit", "cache_miss")
        },
        "cost_usd": sum(costs) if costs else None,
        "cost_per_evaluation_usd": sum(costs) / len(costs) if costs else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {}
    for variant in ("baseline", "candidate"):
        result[variant] = metrics(rows, variant)
        result[variant]["strata"] = {
            field: {
                str(value): metrics([r for r in rows if r.get(field) == value], variant)
                for value in sorted({r.get(field) or "unspecified" for r in rows})
            }
            for field in ("split", "model_reason_type", "human_reason")
        }
    return result


def score_prompt(
    system: str,
    job: dict[str, Any],
    model: str,
    provider: str = "",
) -> dict[str, Any]:
    """Reuse the production graph's schema, rendering and bounded retry policy only."""
    from findajob.scoring.graph import build_graph

    state = build_graph().invoke(
        {"system": system, "posting": job, "model": model, "provider": provider, "review_model": ""}
    )
    verdict = state.get("verdict")
    return {
        "outcome": ("apply" if verdict["fit"] else "pass") if verdict else None,
        "verdict": verdict,
        "error": state.get("error"),
        "attempts": state.get("attempts", 0),
        "usage": state.get("usage"),
        "cost_usd": state.get("cost_usd")
        if state.get("cost_recorded", 0) == state.get("attempts")
        else None,
    }


class EvaluationRunner:
    """Persist progress atomically; a UI run executes sequential calls in a worker thread."""

    def __init__(self, output_dir: str):
        self.root = Path(output_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._active: set[str] = set()

    def _path(self, run_id: str) -> Path:
        if len(run_id) != 32 or any(c not in "0123456789abcdef" for c in run_id):
            raise ValueError("Invalid run ID.")
        return self.root / f"{run_id}.json"

    def _save(self, run: dict[str, Any]) -> None:
        path = self._path(run["id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(run, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)

    def get(self, run_id: str) -> dict[str, Any]:
        run = json.loads(self._path(run_id).read_text(encoding="utf-8"))
        if run["status"] in ("queued", "running") and run_id not in self._active:
            run["status"] = "interrupted"
            run["error"] = "Server stopped before the run finished. Start a new run."
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        runs = [self.get(p.stem) for p in self.root.glob("*.json")]
        return [
            {
                k: r[k]
                for k in (
                    "id",
                    "created_at",
                    "status",
                    "dataset_version",
                    "split",
                    "completed",
                    "total",
                    "model",
                )
            }
            for r in sorted(runs, key=lambda r: r["created_at"], reverse=True)
        ]

    def create(
        self,
        *,
        dataset_path: str,
        split: str = "validation",
        model: str = "",
        provider: str = "",
        candidate_rules: str = DEFAULT_CANDIDATE_RULES,
        profile_boundary: str = "",
        fit_threshold: int = 70,
    ) -> dict[str, Any]:
        from findajob.core.llm import LLMUnavailableError, get_llm_provider, get_model_for_role
        from findajob.profile.models import JobFitVerdict

        if not candidate_rules.strip() and not profile_boundary.strip():
            raise ValueError("Provide candidate instructions or a profile boundary.")
        if not 1 <= fit_threshold <= 100:
            raise ValueError("Fit threshold must be between 1 and 100.")
        manifest, records = load_dataset(dataset_path, split)
        try:
            selected_provider = get_llm_provider(provider.strip() or None)
        except LLMUnavailableError as exc:
            raise ValueError(str(exc)) from exc
        resolved = get_model_for_role(
            "scoring", override=model.strip() or None, provider=selected_provider
        )
        rows = []
        for record in records:
            baseline = build_system(record["profile_text"], fit_threshold)
            candidate = baseline
            if candidate_rules.strip():
                candidate += "\nAdditional evaluation guidelines:\n" + candidate_rules.strip()
            if profile_boundary.strip():
                candidate += "\nCandidate profile scope clarification:\n" + profile_boundary.strip()
            rows.append(
                {
                    **record,
                    "human_reason": record.get("human_reason") or "unspecified",
                    "model_reason_type": record.get("model_reason_type") or "unspecified",
                    "prompts": {"baseline": baseline, "candidate": candidate},
                    "prompt_hashes": {"baseline": digest(baseline), "candidate": digest(candidate)},
                    "rendered_posting": render_posting(record["job"]),
                }
            )
        run = {
            "id": uuid.uuid4().hex,
            "format": "careerradar-verdict-evaluation-v1",
            "created_at": utc_now(),
            "status": "queued",
            "error": None,
            "dataset_path": str(Path(dataset_path).resolve()),
            "dataset_version": manifest["version"],
            "dataset_manifest": manifest,
            "split": split,
            "model": resolved,
            "provider": selected_provider.name,
            "fit_threshold": fit_threshold,
            "candidate_rules": candidate_rules,
            "profile_boundary": profile_boundary,
            "completed": 0,
            "output_schema": JobFitVerdict.model_json_schema(),
            "source_checksums": {
                str(p.relative_to(Path(__file__).parents[1])): hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
                for p in (
                    Path(__file__),
                    Path(__file__).parents[1] / "scoring" / "graph.py",
                    Path(__file__).parents[1] / "scoring" / "prompts.py",
                )
            },
            "total": 2 * sum(r["human_outcome"] != "insufficient" for r in rows),
            "metric_policy": "Exact three-class agreement; Pass labels weight 2, others 1. "
            "Failures count as incorrect; rates use successful predictions. "
            "Match maps to Apply, Reject to Pass. Insufficient is excluded.",
            "examples": rows,
            "summary": {},
        }
        self._save(run)
        return run

    def execute(
        self,
        run_id: str,
        scorer: Callable[[str, dict[str, Any], str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            run = self.get(run_id)
            if run["status"] not in ("queued", "interrupted") or run["completed"]:
                raise ValueError("Run has already started; create a new run.")
            self._active.add(run_id)
        try:
            run["status"] = "running"
            self._save(run)
            for index, row in enumerate(run["examples"]):
                if row["human_outcome"] == "insufficient":
                    continue
                # Alternate order to reduce systematic cache/order effects.
                variants = (
                    ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                )
                for variant in variants:
                    try:
                        if scorer is None:
                            row[variant] = score_prompt(
                                row["prompts"][variant], row["job"], run["model"], run["provider"]
                            )
                        else:
                            row[variant] = scorer(row["prompts"][variant], row["job"], run["model"])
                    except Exception as exc:  # noqa: BLE001 - persist per-example provider failures
                        row[variant] = {"outcome": None, "error": str(exc), "attempts": 0}
                    run["completed"] += 1
                    run["summary"] = summarize(run["examples"])
                    self._save(run)
            failed = any(s["failed"] for s in run["summary"].values())
            run["status"] = "completed_with_errors" if failed else "completed"
        except Exception as exc:  # noqa: BLE001 - retain partial progress on runner failure
            run["status"], run["error"] = "failed", str(exc)
        finally:
            run["finished_at"] = utc_now()
            self._save(run)
            self._active.discard(run_id)
        return run

    def start(self, **kwargs: Any) -> dict[str, Any]:
        run = self.create(**kwargs)
        self._active.add(run["id"])
        thread = threading.Thread(target=self.execute, args=(run["id"],), daemon=True)
        thread.start()
        return run
