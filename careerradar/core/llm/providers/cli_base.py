"""Base class for CLI-piped LLM providers."""

import json
import os
import re
import subprocess
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from careerradar.core.llm.base import (
    BaseLLMProvider,
    LLMError,
    StructuredOutputError,
)
from careerradar.core.logger import get_logger

# Ensure standard user bin is in PATH for non-interactive shells and background services
_user_bin = os.path.expanduser("~/.local/bin")
if os.path.isdir(_user_bin) and _user_bin not in os.environ.get("PATH", "").split(os.path.pathsep):
    os.environ["PATH"] = f"{os.environ.get('PATH', '')}{os.path.pathsep}{_user_bin}"

T = TypeVar("T", bound=BaseModel)
logger = get_logger()

# Regex to strip ANSI escape codes
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class BaseCLIProvider(BaseLLMProvider):
    """Base provider that pipes prompts into an OS CLI process and parses output."""

    timeout_seconds: float = 90.0

    def _strip_ansi(self, text: str) -> str:
        return ANSI_ESCAPE_RE.sub("", text)

    def _run_subprocess(
        self,
        cmd: list[str],
        input_text: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Execute a subprocess command, optionally streaming input_text to stdin."""
        t_out = timeout or self.timeout_seconds
        try:
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = p.communicate(input=input_text, timeout=t_out)
            clean_stdout = self._strip_ansi(stdout or "")
            clean_stderr = self._strip_ansi(stderr or "")
            return p.returncode, clean_stdout, clean_stderr
        except subprocess.TimeoutExpired:
            p.kill()
            raise LLMError(f"CLI command {cmd[0]} timed out after {t_out} seconds") from None
        except FileNotFoundError:
            raise LLMError(
                f"CLI binary '{cmd[0]}' not found in PATH. Make sure it is installed."
            ) from None
        except Exception as exc:
            raise LLMError(f"Failed to execute CLI command {cmd[0]}: {exc}") from exc

    def _format_messages(
        self,
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        system: str | None = None,
    ) -> tuple[str | None, str]:
        """Convert varied message formats into (system_prompt, combined_user_text)."""
        if isinstance(messages, str):
            return system, messages

        sys_parts: list[str] = [system] if system else []
        turn_parts: list[str] = []

        for msg in messages:
            if isinstance(msg, tuple):
                role, content = msg
            elif isinstance(msg, dict):
                role = msg.get("role", "user")
                content = msg.get("content", "")
            else:
                role = getattr(msg, "type", "user")
                content = str(getattr(msg, "content", msg))

            if role == "system":
                sys_parts.append(content)
            else:
                turn_parts.append(f"<{role}>\n{content}\n</{role}>")

        sys_str = "\n\n".join(filter(None, sys_parts)) if sys_parts else None
        return sys_str, "\n\n".join(turn_parts)

    def _extract_json_from_text(self, text: str) -> dict[str, Any] | list[Any]:
        """Extract a JSON object or array from LLM response text."""
        cleaned = text.strip()

        # 1. Direct JSON parse
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # 2. Markdown fenced code block
        code_block_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.DOTALL)
        if code_block_match:
            try:
                return json.loads(code_block_match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # 3. First '{' to last '}'
        start_obj = cleaned.find("{")
        end_obj = cleaned.rfind("}")
        if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
            try:
                return json.loads(cleaned[start_obj : end_obj + 1])
            except json.JSONDecodeError:
                pass

        # 4. First '[' to last ']'
        start_arr = cleaned.find("[")
        end_arr = cleaned.rfind("]")
        if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
            try:
                return json.loads(cleaned[start_arr : end_arr + 1])
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Could not parse valid JSON from model response: {cleaned[:300]}")

    def complete_structured(
        self,
        schema: type[T],
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        attempts: int = 3,
        label: str = "",
        **kwargs: Any,
    ) -> T:
        """Default structured completion using prompt-injected JSON Schema and retries."""
        schema_json = json.dumps(schema.model_json_schema(), indent=2)
        schema_instruction = (
            "You MUST respond ONLY with a valid JSON object strictly matching this schema:\n"
            f"```json\n{schema_json}\n```\n"
            "Do not write any commentary, explanation, or text outside of the JSON."
        )

        sys_str, user_str = self._format_messages(messages, system=system)
        full_system = f"{sys_str}\n\n{schema_instruction}" if sys_str else schema_instruction

        current_messages = [("system", full_system), ("user", user_str)]
        last_error = ""

        for attempt in range(1, attempts + 1):
            resp = self.complete(
                current_messages,
                model=model,
                temperature=temperature,
                **kwargs,
            )
            raw_text = resp.content
            try:
                data = self._extract_json_from_text(raw_text)
                if isinstance(data, dict):
                    return schema.model_validate(data)
                raise ValueError("Expected a JSON object, got a list")
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                logger.warning(
                    "%s: structured validation failed (attempt %d/%d): %s",
                    label or self.name,
                    attempt,
                    attempts,
                    last_error[:200],
                )
                if attempt < attempts:
                    current_messages.append(("assistant", raw_text))
                    nudge = (
                        f"Your previous response failed validation: {last_error}.\n"
                        "Fix the issue and return ONLY the corrected JSON object."
                    )
                    current_messages.append(("user", nudge))

        raise StructuredOutputError(
            f"{label or self.name}: failed to produce valid {schema.__name__} after "
            f"{attempts} attempts. Last error: {last_error}"
        )

    def _estimate_tokens(self, prompt: str, completion: str) -> dict[str, int]:
        """Estimate token usage using character length heuristic (~3.5 chars/token)."""
        prompt_tokens = max(len(prompt) // 4, 1)
        completion_tokens = max(len(completion) // 4, 1)
        return {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "cache_hit": 0,
            "cache_miss": prompt_tokens,
        }
