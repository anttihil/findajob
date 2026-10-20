"""Keep the configured uv cache consistent when packaging leaves the checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import package_smoke


@pytest.mark.parametrize("override", [None, "relative-cache", "absolute-cache"])
def test_smoke_test_keeps_configured_cache_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str | None
) -> None:
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.uv]\ncache-dir = ".cache/uv"\n')
    monkeypatch.delenv("UV_CONFIG_FILE", raising=False)
    monkeypatch.delenv("UV_NO_CONFIG", raising=False)
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    expected_cache = root / ".cache/uv"
    if override is not None:
        expected_cache = root / override
        monkeypatch.setenv(
            "UV_CACHE_DIR", str(expected_cache) if override == "absolute-cache" else override
        )

    real_run = subprocess.run
    outside_commands: list[list[str]] = []

    def run(
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        check: bool,
        capture_output: bool = False,
        text: bool = False,
        stdout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if command == ["uv", "cache", "dir"]:
            assert cwd == root
            return real_run(command, cwd=cwd, env=env, check=True, capture_output=True, text=True)
        assert env["UV_CACHE_DIR"] == str(expected_cache)
        if command[:2] == ["uv", "build"]:
            wheel_dir = Path(command[-1])
            wheel_dir.mkdir()
            (wheel_dir / "findajob.whl").touch()
        if command[0] == "uv" and cwd != root:
            # Exercise real uv configuration discovery from outside the project.
            result = real_run(
                ["uv", "cache", "dir"], cwd=cwd, env=env, check=True, capture_output=True, text=True
            )
            assert result.stdout.strip() == str(expected_cache)
            outside_commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(package_smoke.subprocess, "run", run)
    package_smoke.smoke_test(root)
    assert [command[1:3] for command in outside_commands] == [["pip", "install"], ["pip", "check"]]
