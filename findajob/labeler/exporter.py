"""Versioned JSONL dataset exports for DSPy optimization and evaluation."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from findajob.labeler.database import LabelerDatabase, decode_snapshot_fields, utc_now


def _default_version() -> str:
    return datetime.now(timezone.utc).strftime("v%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _export_record(row: dict[str, Any]) -> dict[str, Any]:
    decoded = decode_snapshot_fields(row)
    annotations = decoded.pop("annotations")
    resolution = decoded.pop("resolution", None)
    selected_round = resolution["selected_round"] if resolution else 1
    primary = next(item for item in annotations if item["review_round"] == selected_round)
    return {
        "example_id": decoded["id"],
        "family_key": decoded["family_key"],
        "split": decoded["split"],
        "job": decoded["job_snapshot"],
        "profile_text": decoded["profile_text"],
        "profile": decoded["profile_snapshot"],
        "human_outcome": primary["outcome"],
        "human_reason": primary["decisive_reason"],
        "human_confidence": primary["confidence"],
        "human_notes": primary["notes"],
        "model_outcome": decoded["model_outcome"],
        "model_reason_type": decoded["model_reason_type"],
        "model_reason_description": decoded["model_reason_description"],
        "model_verdict": decoded["model_verdict"],
        "agreement": primary["agreement"],
        "review_rounds": annotations,
        "consistency_resolution": resolution,
        "provenance": {
            "snapshot_hash": decoded["snapshot_hash"],
            "source_job_id": decoded["source_job_id"],
            "source_db_identity": decoded["source_db_identity"],
            "source_profile_version": decoded["source_profile_version"],
            "rubric_version": decoded["rubric_version"],
            "model": decoded["model"],
            "imported_at": decoded["imported_at"],
            "source_metadata": decoded["source_metadata"],
        },
    }


def export_dataset(
    *,
    labeler_db_path: str | None,
    output_dir: str,
    version: str | None = None,
) -> dict[str, Any]:
    """Write a new immutable export directory and register it in the labeler database."""
    version = version or _default_version()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", version):
        raise ValueError("Version may contain only letters, digits, dots, underscores, and dashes.")
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / version
    if destination.exists():
        raise FileExistsError(f"Export version already exists: {destination}")

    with LabelerDatabase(labeler_db_path) as db:
        rows = db.export_rows()
        if not rows:
            raise ValueError("No completed first-round labels are available to export.")
        records = [_export_record(row) for row in rows]
        split_counts = Counter(record["split"] for record in records)
        outcome_counts = Counter(record["human_outcome"] for record in records)
        temp_path = Path(tempfile.mkdtemp(prefix=f".{version}-", dir=root))
        try:
            examples_path = temp_path / "examples.jsonl"
            with examples_path.open("x", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            manifest: dict[str, Any] = {
                "format": "careerradar-verdict-labels-v1",
                "version": version,
                "created_at": utc_now(),
                "example_count": len(records),
                "split_counts": dict(sorted(split_counts.items())),
                "outcome_counts": dict(sorted(outcome_counts.items())),
                "files": {
                    "examples.jsonl": {
                        "sha256": _sha256(examples_path),
                        "records": len(records),
                    }
                },
            }
            manifest_path = temp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temp_path.rename(destination)
        except Exception:
            shutil.rmtree(temp_path, ignore_errors=True)
            raise

        db.conn.execute(
            """
            INSERT INTO export_runs
                (version, output_path, created_at, example_count, manifest_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                version,
                str(destination),
                manifest["created_at"],
                len(records),
                json.dumps(manifest, sort_keys=True),
            ),
        )
        db.conn.commit()
        return {**manifest, "output_path": str(destination)}
