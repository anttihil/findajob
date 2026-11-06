"""Check preview ports, isolation, and cleanup without opening sockets."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from scripts import dev


@pytest.mark.parametrize("ports", [None, (5174, 8012)])
@pytest.mark.parametrize("failing_api", [False, True])
def test_dev_ports_isolation_and_server_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ports: tuple[int, int] | None,
    failing_api: bool,
) -> None:
    preview = tmp_path / "preview"
    preview.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    # One server exits after both start; the launcher must stop its companion.
    stub = (
        f"#!{sys.executable}\n"
        f"failing_api = {failing_api!r}\n"
        """import json, os, sys, time
from pathlib import Path
home = Path(os.environ["FIND_A_JOB_HOME"])
if sys.argv[1] == "-c":
    (home / "environment.json").write_text(json.dumps(dict(os.environ)))
    raise SystemExit(0)
api = sys.argv[1] == "-m"
(home / ("api.json" if api else "frontend.json")).write_text(
    json.dumps({"pid": os.getpid(), "args": sys.argv, "proxy": os.environ["FIND_A_JOB_API_PROXY"]}))
if api == failing_api:
    for _ in range(100):
        if (home / ("frontend.json" if api else "api.json")).exists():
            raise SystemExit(7)
        time.sleep(0.05)
time.sleep(30)
"""
    )
    for executable, source in (
        (python, stub),
        (binaries / "node", stub),
    ):
        executable.write_text(source)
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}:{os.environ['PATH']}")
    monkeypatch.setenv("FIND_A_JOB_DB_PATH", "/personal/jobs.db")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(dev.tempfile, "mkdtemp", lambda **_kwargs: str(preview))
    monkeypatch.setattr(
        sys,
        "argv",
        ["dev.py", *(["--port", str(ports[0]), "--api-port", str(ports[1])] if ports else [])],
    )
    assert dev.main() == 7
    isolated = json.loads((preview / "environment.json").read_text())
    assert isolated["HOME"] == str(preview / "user")
    assert isolated["FIND_A_JOB_HOME"] == str(preview)
    assert "FIND_A_JOB_DB_PATH" not in isolated
    assert "DEEPSEEK_API_KEY" not in isolated
    frontend_port, api_port = ports or (5173, 8011)
    for name, expected_port in (("api", api_port), ("frontend", frontend_port)):
        server = json.loads((preview / f"{name}.json").read_text())
        assert server["proxy"] == f"http://127.0.0.1:{api_port}"
        assert server["args"][server["args"].index("--port") + 1] == str(expected_port)
        with pytest.raises(ProcessLookupError):
            os.kill(server["pid"], 0)


@pytest.mark.parametrize(
    "arguments",
    [["--port", "0"], ["--api-port", "65536"], ["--port", "8011"], ["--port", "abc"]],
)
def test_invalid_ports_fail_before_creating_a_preview(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["dev.py", *arguments])
    with pytest.raises(SystemExit) as error:
        dev.main()
    assert error.value.code == 2
