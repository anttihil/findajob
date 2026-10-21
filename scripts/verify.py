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


def fingerprint(root: Path) -> str:
    """Hash tracked and non-ignored untracked contents, including deletions and modes."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    digest = hashlib.sha256()
    for name in sorted(set(result.stdout.split(b"\0")) - {b""}):
        path = root / os.fsdecode(name)
        # Also exclude receipts when exercising the runner in a temporary repository.
        if path.is_relative_to(root / STATE_DIR):
            continue
        digest.update(name + b"\0")
        try:
            digest.update(str(path.lstat().st_mode).encode() + b"\0")
            if path.is_symlink():
                digest.update(os.fsencode(os.readlink(path)))
            elif path.is_file():
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        except FileNotFoundError:
            digest.update(b"deleted")
        digest.update(b"\0")
    # Track installation metadata, not site-packages directory timestamps: Python can
    # create __pycache__ there without changing installed dependencies.
    for pattern in (
        ".venv/pyvenv.cfg",
        ".venv/lib/*/site-packages/*.dist-info/**/*",
        "node_modules/.package-lock.json",
    ):
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            stat = path.stat()
            digest.update(f"{path.relative_to(root)}:{stat.st_mtime_ns}:{stat.st_size}".encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def has_fresh_success(root: Path) -> bool:
    """Receipts are a local optimization; CI and explicit verification always rerun."""
    try:
        receipt = json.loads((root / RECEIPT).read_text())
        return isinstance(receipt, dict) and receipt.get("fingerprint") == fingerprint(root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return False


def checks(browser: bool = False) -> list[tuple[str, list[str]]]:
    python = ["uv", "run", "--locked"]
    steps = [
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
    if browser:
        steps.append(("Browser tests", ["npm", "run", "test:browser"]))
    return steps


def verify(root: Path, browser: bool = False) -> int:
    receipt = root / RECEIPT
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.unlink(missing_ok=True)
    before = fingerprint(root)
    for label, command in checks(browser):
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
    receipt.write_text(json.dumps({"fingerprint": before, "browser": browser}) + "\n")
    print("\nVerification passed.", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="also run Playwright browser tests")
    args = parser.parse_args()
    try:
        return verify(ROOT, browser=args.browser)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Verification could not run: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
