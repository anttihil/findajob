#!/usr/bin/env python3
"""Run the repository's completion checks, shared by developers, agents, and CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = Path("test-results/verification")
RECEIPT = STATE_DIR / "success.json"


def is_verification_input(name: str) -> bool:
    """Exclude prose and agent instructions, while keeping packaged app resources."""
    path = Path(name)
    if path.parts[0] == "findajob":
        return True
    if path.parts[0] in {"docs", ".agents"}:
        return False
    return path.suffix.lower() not in {".md", ".rst"} and path.name != "LICENSE"


def changed_files(root: Path) -> list[str]:
    """Relevant staged, unstaged, and untracked files, including deleted paths."""
    names: set[str] = set()
    commands = [
        ["git", "diff", "--name-only", "--no-renames", "-z", "--"],
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z", "--"],
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, capture_output=True, check=True)
        names.update(os.fsdecode(name) for name in result.stdout.split(b"\0") if name)
    return sorted(name for name in names if is_verification_input(name))


def fingerprint(root: Path) -> str:
    """Hash only changed files so the hook can reuse their successful verification."""
    digest = hashlib.sha256()
    for name in changed_files(root):
        path = root / name
        digest.update(os.fsencode(name) + b"\0")
        try:
            digest.update(str(path.lstat().st_mode).encode() + b"\0")
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        except FileNotFoundError:
            digest.update(b"deleted")
        digest.update(b"\0")
    return digest.hexdigest()


def has_fresh_success(root: Path) -> bool:
    """Receipts are a local optimization; CI and explicit verification always rerun."""
    try:
        receipt = json.loads((root / RECEIPT).read_text())
        return isinstance(receipt, dict) and receipt.get("fingerprint") == fingerprint(root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False


def checks() -> list[tuple[str, list[str]]]:
    python = ["uv", "run", "--locked"]
    return [
        ("Python lint", [*python, "ruff", "check", "."]),
        ("Frontend lint", ["npm", "run", "lint"]),
        ("Python formatting", [*python, "ruff", "format", "--check", "."]),
        ("Frontend formatting", ["npm", "run", "format:check"]),
        ("Python types", [*python, "pyright"]),
        ("Frontend types", ["npm", "run", "typecheck"]),
        ("Python tests", [*python, "pytest", "-q"]),
        ("Frontend tests", ["npm", "test"]),
        ("Frontend production build", ["npm", "run", "build"]),
        ("Packaging smoke test", [sys.executable, "scripts/package_smoke.py"]),
        ("Working-tree whitespace", ["git", "diff", "--check"]),
        ("Staged whitespace", ["git", "diff", "--cached", "--check"]),
    ]


def verify(root: Path) -> int:
    receipt = root / RECEIPT
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    before = fingerprint(root)
    for label, command in checks():
        print(f"\n>>> {label}: {' '.join(command)}", flush=True)
        try:
            result = subprocess.run(command, cwd=root, check=False)
        except OSError as error:
            print(f"Verification could not run: {error}", file=sys.stderr)
            return 1
        if result.returncode:
            print(f"\nVerification failed: {label}", file=sys.stderr)
            return 1
    if fingerprint(root) != before:
        print("Files changed during verification; rerun after the final edit.", file=sys.stderr)
        return 1
    receipt.write_text(json.dumps({"fingerprint": before}) + "\n")
    print("\nVerification passed.", flush=True)
    return 0


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        return verify(ROOT)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Verification could not run: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
