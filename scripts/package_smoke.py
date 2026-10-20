#!/usr/bin/env python3
"""Build and smoke-test the wheel outside the checkout in a temporary environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# -I prevents the checkout or PYTHONPATH from satisfying imports in this subprocess.
INSTALLED_CHECK = r"""
import os
import re
import sys
from pathlib import Path

import findajob
from findajob.core import paths
from findajob.web.app import app

package = Path(findajob.__file__).resolve().parent
assert package.is_relative_to(Path(sys.prefix).resolve()), package
assert not paths.SOURCE_CHECKOUT, "Imported a source checkout instead of the installed wheel"
assert Path(paths.DB_PATH).is_relative_to(Path(os.environ["FIND_A_JOB_HOME"]))
assert app.routes, "Packaged web app has no routes"

dist = package / "web/frontend/dist"
index = dist / "index.html"
assert index.is_file(), "Wheel is missing the frontend index.html"
assets = re.findall(r'(?:src|href)="(/static/dist/[^\"]+)"', index.read_text())
assert any(asset.endswith(".js") for asset in assets), "Frontend JavaScript is missing"
assert any(asset.endswith(".css") for asset in assets), "Frontend CSS is missing"
for asset in assets:
    bundled = dist / asset.removeprefix("/static/dist/")
    assert bundled.is_file() and bundled.stat().st_size > 0, f"Missing bundled asset: {asset}"
print("Installed wheel imports and bundled frontend assets passed.")
"""


def smoke_test(root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="careerradar-package-") as directory:
        work = Path(directory)
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("FIND_A_JOB_")
            and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        }
        venv = work / ".venv"
        environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
        environment["FIND_A_JOB_HOME"] = str(work / "app-home")
        environment["PYTHONNOUSERSITE"] = "1"
        wheel_dir = work / "dist"
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
            cwd=root,
            env=environment,
            check=True,
        )
        wheels = list(wheel_dir.glob("*.whl"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one wheel, found {len(wheels)}")
        # Install locked runtime dependencies without an editable checkout or dev tools.
        subprocess.run(
            ["uv", "sync", "--locked", "--no-dev", "--no-install-project"],
            cwd=root,
            env=environment,
            check=True,
        )
        bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "--no-deps", str(wheels[0])],
            cwd=work,
            env=environment,
            check=True,
        )
        subprocess.run(
            ["uv", "pip", "check", "--python", str(python)],
            cwd=work,
            env=environment,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", "-c", INSTALLED_CHECK],
            cwd=work,
            env=environment,
            check=True,
        )
        for command in ("findajob", "findajob-labeler"):
            executable = bin_dir / (f"{command}.exe" if os.name == "nt" else command)
            subprocess.run(
                [str(executable), "--help"],
                cwd=work,
                env=environment,
                stdout=subprocess.DEVNULL,
                check=True,
            )
        print("Wheel installation and both CLI entry points passed.", flush=True)


if __name__ == "__main__":
    try:
        smoke_test(ROOT)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Packaging smoke test failed: {error}", file=sys.stderr)
        sys.exit(1)
