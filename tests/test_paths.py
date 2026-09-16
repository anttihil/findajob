"""Installed-mode paths must not depend on the source checkout or current directory."""

import json
import os
import subprocess
import sys
from pathlib import Path

from careerradar.core import paths as checkout_paths


def test_source_checkout_keeps_its_repository_local_config() -> None:
    assert checkout_paths.SOURCE_CHECKOUT is True
    assert str(Path(checkout_paths.REPO_ROOT) / "config.yaml") == checkout_paths.CONFIG_PATH


def _installed_paths(home: Path) -> dict[str, object]:
    env = os.environ.copy()
    for key in tuple(env):
        if key.startswith("FIND_A_JOB_"):
            env.pop(key)
    env["FIND_A_JOB_HOME"] = str(home)
    script = """
import json
from careerradar.core.paths import (
    CONFIG_PATH, DB_PATH, GENERATED_RESUMES_DIR, GRAPH_DB_PATH, LOG_PATH,
    SOURCE_CHECKOUT, STATUS_PATH, generated_resumes_dir,
)
print(json.dumps({
    'source_checkout': SOURCE_CHECKOUT, 'config': CONFIG_PATH, 'db': DB_PATH,
    'graph': GRAPH_DB_PATH, 'log': LOG_PATH, 'status': STATUS_PATH,
    'resumes': GENERATED_RESUMES_DIR, 'configured_resumes': generated_resumes_dir(),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_careerradar_home_uses_a_self_contained_installed_layout(tmp_path: Path) -> None:
    paths = _installed_paths(tmp_path / "career-home")
    root = tmp_path / "career-home"
    assert paths["source_checkout"] is False
    assert paths["config"] == str(root / "config" / "config.yaml")
    assert paths["db"] == str(root / "data" / "jobs.db")
    assert paths["graph"] == str(root / "data" / "graphs.db")
    assert paths["log"] == str(root / "state" / "app.log")
    assert paths["status"] == str(root / "state" / "sync_status.json")
    assert paths["resumes"] == str(root / "resumes")
    assert paths["configured_resumes"] == str(root / "resumes")


def test_resume_output_dir_config_overrides_the_platform_default(tmp_path: Path) -> None:
    root = tmp_path / "career-home"
    config_dir = root / "config"
    config_dir.mkdir(parents=True)
    configured = tmp_path / "application-documents"
    (config_dir / "config.yaml").write_text(
        f"resumes:\n  output_dir: {configured}\n", encoding="utf-8"
    )
    paths = _installed_paths(root)
    assert paths["configured_resumes"] == str(configured)
