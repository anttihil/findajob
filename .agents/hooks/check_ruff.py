#!/usr/bin/env python3
import json
import os
import subprocess
import sys


def get_target_files(data: dict) -> list[str]:
    files: list[str] = []

    target = data.get("toolCall", {}).get("args", {}).get("TargetFile")
    if target:
        files.append(str(target))

    transcript_path = data.get("transcriptPath")
    if transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, encoding="utf-8") as f:
                for line in reversed(f.readlines()):
                    entry = json.loads(line)
                    tool_calls = entry.get("tool_calls", [])
                    if tool_calls:
                        for tc in tool_calls:
                            val = tc.get("args", {}).get("TargetFile")
                            if val:
                                files.append(str(val).strip('"'))
                        break
        except (json.JSONDecodeError, OSError):
            pass

    seen = set()
    valid_files: list[str] = []
    for file_path in files:
        if file_path.endswith(".py") and os.path.exists(file_path) and file_path not in seen:
            seen.add(file_path)
            valid_files.append(file_path)

    return valid_files


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        data = {}

    target_files = get_target_files(data)

    if not target_files:
        print(json.dumps({}))
        sys.exit(0)

    result = subprocess.run(
        ["uv", "run", "ruff", "check", "--quiet", *target_files],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()

    if output:
        file_list = ", ".join(target_files)
        reason = f"Ruff lint issues in {file_list}:\n{output}"
        print(json.dumps({"decision": "block", "reason": reason}))
    else:
        print(json.dumps({}))


if __name__ == "__main__":
    main()
