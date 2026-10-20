#!/usr/bin/env python3
"""Gate Codex and Claude completion on the shared verification command."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from verify import ROOT, STATE_DIR, has_fresh_success


def check(root: Path) -> str | None:
    if has_fresh_success(root):
        return None
    log_dir = root / STATE_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", prefix="stop-", suffix=".log", dir=log_dir, delete=False
    ) as log:
        log_path = Path(log.name)
        process = subprocess.Popen(
            [sys.executable, str(root / "scripts/verify.py")],
            cwd=root,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=540)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            return f"Verification timed out after 540 seconds. Full log: {log_path}"
    if returncode == 0 and has_fresh_success(root):
        return None
    tail = log_path.read_text(errors="replace")[-6000:]
    return f"Verification did not pass. Full log: {log_path}\n\n{tail}"


def main() -> None:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("expected a hook event object")
    except (ValueError, OSError) as error:
        print(
            json.dumps({"decision": "block", "reason": f"Invalid verification hook input: {error}"})
        )
        return
    try:
        failure = check(ROOT)
    except (OSError, subprocess.SubprocessError) as error:
        failure = f"Verification could not run: {error}"
    if failure is None:
        print("{}")
    elif event.get("stop_hook_active"):
        # Recheck repairs, but permit a blocked handoff rather than an endless loop.
        print(
            json.dumps(
                {"systemMessage": f"Task remains unverified. Report this blocker:\n{failure}"}
            )
        )
    else:
        print(
            json.dumps(
                {
                    "decision": "block",
                    "reason": f"{failure}\n\nRead the log and fix failures within scope. Run "
                    "`make verify` after the final edit. If blocked, report the failing checks and "
                    "limitations; do not claim completion or disable verification.",
                }
            )
        )


if __name__ == "__main__":
    main()
