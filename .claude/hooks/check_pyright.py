#!/usr/bin/env python3
import json
import subprocess
import sys

data = json.load(sys.stdin)
tool_input = data.get("tool_input", {})
tool_response = data.get("tool_response", {})
file_path = tool_input.get("file_path") or tool_response.get("filePath")

if not file_path or not file_path.endswith(".py"):
    sys.exit(0)

result = subprocess.run(
    ["uv", "run", "pyright", file_path],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    output = result.stdout + result.stderr
    reason = f"Pyright type errors in {file_path}:\n{output}"
    print(json.dumps({"decision": "block", "reason": reason}))
