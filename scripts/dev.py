#!/usr/bin/env python3
"""Start an isolated Vite frontend and reloading API for a worktree."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def serve(commands: list[list[str]], environment: dict[str, str]) -> int:
    children: list[subprocess.Popen[bytes]] = []

    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for command in commands:
            children.append(
                subprocess.Popen(command, cwd=ROOT, env=environment, start_new_session=True)
            )
        while True:
            for child in children:
                code = child.poll()
                if code is not None:
                    return code or 1
            time.sleep(0.1)
    except KeyboardInterrupt:
        return 0
    finally:
        # Uvicorn's reload process has a child; stop the entire process group.
        for child in children:
            with suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=port, default=5173, help="frontend port (default: 5173)")
    parser.add_argument("--api-port", type=port, default=8011, help="API port (default: 8011)")
    args = parser.parse_args()
    if args.port == args.api_port:
        parser.error("frontend and API ports must differ")

    home = Path(tempfile.mkdtemp(prefix="careerradar-dev-"))
    (home / "user").mkdir()
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home / "user"),
        "FIND_A_JOB_HOME": str(home),
        "FIND_A_JOB_API_PROXY": f"http://127.0.0.1:{args.api_port}",
        "PYTHONUNBUFFERED": "1",
    }
    python = str(ROOT / ".venv/bin/python")
    subprocess.run(
        [python, "-c", "from findajob.core.database import Database; Database().close()"],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    print(f"Preview home: {home}\nFrontend: http://127.0.0.1:{args.port}", flush=True)
    print(f"API: http://127.0.0.1:{args.api_port}", flush=True)
    return serve(
        [
            [
                python,
                "-m",
                "uvicorn",
                "findajob.web.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(args.api_port),
                "--lifespan",
                "off",
                "--reload",
                "--reload-dir",
                "findajob",
            ],
            [
                "node",
                str(ROOT / "node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                str(args.port),
                "--strictPort",
            ],
        ],
        environment,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Dev server could not start: {error}", file=sys.stderr)
        sys.exit(1)
