"""Exercise verification freshness and failure handling in isolated repositories."""

from __future__ import annotations

import importlib
import io
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts import verify


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("test-results/\nnode_modules/\n.venv/\n")
    (tmp_path / "source.py").write_text("value = 1\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def stop_hook(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("verify_stop")


def passing_checks() -> list[tuple[str, list[str]]]:
    # Write only ignored output, just as the real test suites and build do.
    code = "from pathlib import Path; p=Path('test-results/runs'); "
    code += "p.write_text(p.read_text()+'run\\n' if p.exists() else 'run\\n')"
    return [("check", [sys.executable, "-c", code])]


def test_explicit_verification_runs_even_with_a_fresh_receipt(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "checks", lambda _browser: passing_checks())
    assert verify.verify(repository) == 0
    assert verify.has_fresh_success(repository)
    assert verify.verify(repository) == 0
    assert (repository / "test-results/runs").read_text() == "run\nrun\n"


def test_python_bytecode_cache_creation_does_not_invalidate_verification(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = repository / ".venv"
    subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
    site_packages = next(environment.glob("lib/*/site-packages"))
    (site_packages / "_verification_cache_probe.py").write_text("value = 1\n")
    assert not (site_packages / "__pycache__").exists()
    # Import a known module so cache creation doesn't depend on uv's startup files.
    # Isolated mode ignores inherited bytecode-cache environment settings.
    command = [str(environment / "bin/python"), "-I", "-c", "import _verification_cache_probe"]
    monkeypatch.setattr(
        verify,
        "checks",
        lambda _browser: [("create Python bytecode cache", command)],
    )

    assert verify.verify(repository) == 0
    assert list((site_packages / "__pycache__").glob("_verification_cache_probe.*.pyc"))
    assert verify.has_fresh_success(repository)


@pytest.mark.parametrize("change", ["install", "update", "remove"])
def test_python_dependency_changes_invalidate_success(
    repository: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    site_packages = repository / ".venv/lib/python3.12/site-packages"
    metadata = site_packages / "example-1.0.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: example\nVersion: 1.0\n")
    monkeypatch.setattr(verify, "checks", lambda _browser: passing_checks())
    assert verify.verify(repository) == 0

    if change == "install":
        added = site_packages / "another-1.0.dist-info/METADATA"
        added.parent.mkdir()
        added.write_text("Name: another\nVersion: 1.0\n")
    elif change == "update":
        metadata.write_text("Name: example\nVersion: 2.0\n")
    else:
        metadata.unlink()
        metadata.parent.rmdir()

    assert not verify.has_fresh_success(repository)


@pytest.mark.parametrize("change", ["tracked", "untracked", "deleted", "staged", "dependency"])
def test_source_and_dependency_changes_invalidate_success(
    repository: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(verify, "checks", lambda _browser: passing_checks())
    assert verify.verify(repository) == 0
    if change == "untracked":
        (repository / "new.py").write_text("value = 2\n")
    elif change == "deleted":
        (repository / "source.py").unlink()
    elif change == "dependency":
        dependencies = repository / "node_modules"
        dependencies.mkdir()
        (dependencies / ".package-lock.json").write_text("{}")
    else:
        (repository / "source.py").write_text("value = 2\n")
        if change == "staged":
            subprocess.run(["git", "add", "source.py"], cwd=repository, check=True)
    assert not verify.has_fresh_success(repository)


def test_failure_stops_later_checks_and_removes_previous_receipt(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = repository / verify.RECEIPT
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"fingerprint": verify.fingerprint(repository)}))
    monkeypatch.setattr(
        verify,
        "checks",
        lambda _browser: [
            ("fails", [sys.executable, "-c", "raise SystemExit(3)"]),
            *passing_checks(),
        ],
    )
    assert verify.verify(repository) == 1
    assert not receipt.exists()
    assert not (repository / "test-results/runs").exists()


def test_changes_during_verification_cannot_receive_a_success_receipt(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        verify,
        "checks",
        lambda _browser: [
            ("edits source", [sys.executable, "-c", "open('source.py','w').write('value = 3\\n')"]),
        ],
    )
    assert verify.verify(repository) == 1
    assert not (repository / verify.RECEIPT).exists()


def test_hook_reuses_success_without_starting_a_process(
    repository: Path, stop_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "checks", lambda _browser: passing_checks())
    assert verify.verify(repository) == 0
    # No scripts/verify.py exists here: starting verification would fail.
    assert stop_hook.check(repository) is None
    assert not list((repository / verify.STATE_DIR).glob("stop-*.log"))


def test_hook_executes_verification_and_keeps_failure_output(
    repository: Path, stop_hook: ModuleType
) -> None:
    scripts = repository / "scripts"
    scripts.mkdir()
    (scripts / "verify.py").write_text("print('broken test')\nraise SystemExit(1)\n")
    failure = stop_hook.check(repository)
    assert failure is not None and "broken test" in failure
    assert len(list((repository / verify.STATE_DIR).glob("stop-*.log"))) == 1


@pytest.mark.parametrize("continued", [False, True])
def test_hook_rechecks_repairs_and_bounds_continuations(
    stop_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    continued: bool,
) -> None:
    calls: list[Path] = []

    def fail(root: Path) -> str:
        calls.append(root)
        return "test failure"

    monkeypatch.setattr(stop_hook, "check", fail)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": continued})))
    stop_hook.main()
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    if continued:
        assert "decision" not in result
        assert "unverified" in result["systemMessage"]
    else:
        assert result["decision"] == "block"
        assert "test failure" in result["reason"]
