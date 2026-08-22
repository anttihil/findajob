#!/usr/bin/env python3
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path


def find_workspace_root(workspace_paths: list[str] | None = None) -> Path:
    if workspace_paths:
        for p in workspace_paths:
            path = Path(p).resolve()
            if path.exists():
                return path
    current = Path(os.getcwd()).resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() or (parent / ".git").exists():
            return parent
    return current


def compute_proposed_content(tool_name: str, tool_args: dict, target_path: Path) -> str | None:
    if tool_name == "write_to_file":
        return tool_args.get("CodeContent", "")

    if tool_name == "replace_file_content":
        if not target_path.exists():
            return None
        try:
            original_content = target_path.read_text(encoding="utf-8")
        except OSError:
            return None

        target_content = tool_args.get("TargetContent")
        replacement_content = tool_args.get("ReplacementContent")
        allow_multiple = tool_args.get("AllowMultiple", False)
        start_line = tool_args.get("StartLine")
        end_line = tool_args.get("EndLine")

        if target_content is None or replacement_content is None:
            return None

        lines = original_content.splitlines(keepends=True)
        if start_line is not None and end_line is not None and 1 <= start_line <= len(lines):
            start_idx = max(0, start_line - 1)
            end_idx = min(len(lines), end_line)
            chunk = "".join(lines[start_idx:end_idx])
            if target_content in chunk:
                count = -1 if allow_multiple else 1
                new_chunk = chunk.replace(target_content, replacement_content, count)
                return "".join(lines[:start_idx]) + new_chunk + "".join(lines[end_idx:])

        if target_content in original_content:
            count = -1 if allow_multiple else 1
            return original_content.replace(target_content, replacement_content, count)

        return None

    return None


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        data = {}

    tool_call = data.get("toolCall", {})
    tool_name = tool_call.get("name", "")
    tool_args = tool_call.get("args", {})

    target_file = tool_args.get("TargetFile")
    if not target_file or not str(target_file).endswith(".py"):
        print(json.dumps({"decision": "allow"}))
        sys.exit(0)

    target_path = Path(str(target_file)).resolve()
    workspace_root = find_workspace_root(data.get("workspacePaths"))

    proposed_content = compute_proposed_content(tool_name, tool_args, target_path)

    file_existed = target_path.exists()
    original_content = None
    if file_existed:
        with contextlib.suppress(OSError):
            original_content = target_path.read_text(encoding="utf-8")

    try:
        if proposed_content is not None:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(proposed_content, encoding="utf-8")

        result = subprocess.run(
            ["uv", "run", "pyright", str(target_path)],
            text=True,
            capture_output=True,
            cwd=str(workspace_root),
        )
    finally:
        if file_existed and original_content is not None:
            target_path.write_text(original_content, encoding="utf-8")
        elif not file_existed and target_path.exists():
            target_path.unlink()

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        reason = (
            f"Pyright type errors found in proposed change to {target_path.name}:\n"
            f"{output}\n"
            f"Please fix the type errors before applying this change."
        )
        print(json.dumps({"decision": "deny", "reason": reason}))
    else:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
