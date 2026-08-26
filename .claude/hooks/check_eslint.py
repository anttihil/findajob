#!/usr/bin/env python3
import json
import subprocess
import sys
from pathlib import Path

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, OSError):
    data = {}

tool_input = data.get("tool_input", {})
tool_response = data.get("tool_response", {})
file_path = (
    tool_input.get("file_path") or tool_input.get("path") or tool_response.get("filePath") or ""
)

exts = (".ts", ".tsx", ".js", ".jsx")
if not file_path or not any(file_path.endswith(ext) for ext in exts):
    sys.exit(0)

# Run eslint on the modified file
result = subprocess.run(
    ["npx", "eslint", file_path],
    capture_output=True,
    text=True,
)
output = (result.stdout + result.stderr).strip()

if result.returncode != 0 and output:
    reason = f"ESLint issues in {Path(file_path).name}:\n{output}"
    print(json.dumps({"decision": "block", "reason": reason}))
