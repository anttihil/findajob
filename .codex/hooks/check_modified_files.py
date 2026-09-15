#!/usr/bin/env python3
"""Run static checks for source files modified by a Codex apply_patch call."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

PATCH_FILE_PATTERN = re.compile(r"^\*\*\* (?:Add|Update) File: (.+)$", re.MULTILINE)
PYTHON_SUFFIXES = {".py"}
FRONTEND_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}


def workspace_root() -> Path:
    """Return the repository root, even when Codex starts in a subdirectory."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return Path(result.stdout.strip())
    return Path.cwd()


def modified_source_files(event: dict[str, object], root: Path) -> list[Path]:
    """Extract existing Python and frontend paths from an apply_patch payload."""
    if event.get("tool_name") != "apply_patch":
        return []

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    patch = tool_input.get("command")
    if not isinstance(patch, str):
        return []

    paths: list[Path] = []
    for relative_name in PATCH_FILE_PATTERN.findall(patch):
        path = root / relative_name
        if path.suffix in PYTHON_SUFFIXES | FRONTEND_SUFFIXES and path.is_file():
            paths.append(path)
    return paths


def run_check(command: list[str], root: Path) -> str | None:
    """Return the check output when a command fails."""
    environment = os.environ.copy()
    environment.setdefault("UV_CACHE_DIR", "/tmp/careerradar-uv-cache")
    result = subprocess.run(
        command,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    return (result.stdout + result.stderr).strip() or "command failed without output"


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(event, dict):
        return

    root = workspace_root()
    files = modified_source_files(event, root)
    python_files = [file for file in files if file.suffix in PYTHON_SUFFIXES]
    frontend_files = [file for file in files if file.suffix in FRONTEND_SUFFIXES]

    failures: list[str] = []
    if python_files:
        paths = [str(file) for file in python_files]
        for label, command in (
            ("Ruff", ["uv", "run", "ruff", "check", *paths]),
            ("Pyright", ["uv", "run", "pyright", *paths]),
        ):
            if output := run_check(command, root):
                failures.append(f"{label} issues:\n{output}")
    if frontend_files:
        paths = [str(file) for file in frontend_files]
        if output := run_check(["npx", "eslint", *paths], root):
            failures.append(f"ESLint issues:\n{output}")

    if failures:
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": "Static checks failed after the edit:\n\n" + "\n\n".join(failures),
                }
            )
        )


if __name__ == "__main__":
    main()
