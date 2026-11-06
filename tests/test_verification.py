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


def commit_all(root: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "base",
        ],
        cwd=root,
        check=True,
    )


@pytest.mark.parametrize("change", ["clean", "unstaged", "staged", "untracked", "deleted"])
def test_hook_skips_clean_and_prose_only_changes_without_a_receipt(
    repository: Path, stop_hook: ModuleType, change: str
) -> None:
    prose = repository / "README.md"
    prose.write_text("Documentation\n")
    commit_all(repository)
    if change == "untracked":
        (repository / "notes.md").write_text("Notes\n")
    elif change == "deleted":
        prose.unlink()
    elif change != "clean":
        prose.write_text("Updated documentation\n")
        if change == "staged":
            subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)

    assert not verify.changed_files(repository)
    # There is no runner in the fixture, so an attempted full run would fail.
    assert stop_hook.check(repository) is None
    assert not (repository / verify.STATE_DIR).exists()


@pytest.mark.parametrize("staged", [False, True])
def test_prose_only_hook_still_rejects_whitespace_errors(
    repository: Path, stop_hook: ModuleType, staged: bool
) -> None:
    prose = repository / "README.md"
    prose.write_text("Documentation\n")
    commit_all(repository)
    prose.write_text("Trailing whitespace  \n")
    if staged:
        subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)

    failure = stop_hook.check(repository)
    assert failure is not None and "trailing whitespace" in failure
    assert not (repository / verify.STATE_DIR).exists()


@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked", "deleted", "renamed"])
def test_source_changes_require_verification(repository: Path, change: str) -> None:
    commit_all(repository)
    source = repository / "source.py"
    if change == "deleted":
        source.unlink()
    elif change == "renamed":
        # Moving source to a prose path must still detect the deleted source.
        subprocess.run(["git", "mv", "source.py", "notes.md"], cwd=repository, check=True)
    elif change == "untracked":
        (repository / "new.py").write_text("value = 2\n")
    else:
        source.write_text("value = 2\n")
        if change == "staged":
            subprocess.run(["git", "add", "source.py"], cwd=repository, check=True)
    assert verify.changed_files(repository)


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("docs/operations.md", False),
        (".agents/skills/verification/SKILL.md", False),
        ("AGENTS.md", False),
        ("LICENSE", False),
        ("findajob/templates/prompt.md", True),
        ("config.yaml", True),
        ("uv.lock", True),
        ("package-lock.json", True),
        (".github/workflows/ci.yml", True),
        (".codex/hooks.json", True),
        ("scripts/verify.py", True),
        ("tests/test_new.py", True),
        ("new-tool.config", True),
    ],
)
def test_new_files_are_classified_by_effect(repository: Path, name: str, required: bool) -> None:
    commit_all(repository)
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new content\n")
    assert bool(verify.changed_files(repository)) is required


def test_prose_changes_preserve_success_for_unchanged_app_inputs(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prose = repository / "README.md"
    prose.write_text("Documentation\n")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    prose.write_text("Updated documentation\n")
    assert verify.has_fresh_success(repository)


def test_prose_whitespace_is_checked_even_with_a_fresh_app_receipt(
    repository: Path, stop_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    prose = repository / "README.md"
    prose.write_text("Documentation\n")
    commit_all(repository)
    (repository / "source.py").write_text("value = 2\n")
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    prose.write_text("Trailing whitespace  \n")
    assert verify.has_fresh_success(repository)
    failure = stop_hook.check(repository)
    assert failure is not None and "trailing whitespace" in failure


def test_explicit_verification_runs_even_with_a_fresh_receipt(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    assert verify.has_fresh_success(repository)
    assert verify.verify(repository) == 0
    assert (repository / "test-results/runs").read_text() == "run\nrun\n"


def test_ignored_installed_dependencies_do_not_invalidate_success(
    repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    dependencies = repository / "node_modules"
    dependencies.mkdir()
    (dependencies / ".package-lock.json").write_text("{}")
    assert verify.has_fresh_success(repository)


@pytest.mark.parametrize("change", ["tracked", "untracked", "deleted", "staged"])
def test_changed_files_invalidate_success(
    repository: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    if change == "untracked":
        (repository / "new.py").write_text("value = 2\n")
    elif change == "deleted":
        (repository / "source.py").unlink()
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
        lambda: [
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
        lambda: [
            ("edits source", [sys.executable, "-c", "open('source.py','w').write('value = 3\\n')"]),
        ],
    )
    assert verify.verify(repository) == 1
    assert not (repository / verify.RECEIPT).exists()


def test_hook_reuses_success_without_starting_a_process(
    repository: Path, stop_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    # No scripts/verify.py exists here: starting verification would fail.
    assert stop_hook.check(repository) is None
    assert not list((repository / verify.STATE_DIR).glob("stop-*.log"))


def test_committing_verified_files_leaves_the_hook_clean(
    repository: Path, stop_hook: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit_all(repository)
    (repository / "source.py").write_text("value = 2\n")
    monkeypatch.setattr(verify, "checks", passing_checks)
    assert verify.verify(repository) == 0
    commit_all(repository)
    assert verify.changed_files(repository) == []
    assert stop_hook.check(repository) is None


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
