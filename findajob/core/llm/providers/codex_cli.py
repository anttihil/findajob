"""Codex CLI LLM provider."""

import os
import shutil
from typing import Any

from findajob.core.llm.base import LLMError, LLMResponse
from findajob.core.llm.providers.cli_base import BaseCLIProvider
from findajob.core.logger import get_logger

logger = get_logger()


class CodexCLIProvider(BaseCLIProvider):
    """LLM provider powered by the Codex CLI (`codex`)."""

    name = "codex"
    default_model = "gpt-5.6-terra"
    default_scoring_model = "gpt-5.6-terra"
    default_agent_model = "gpt-5.6-terra"

    def _resolve_model(self, model: str | None) -> str | None:
        if not model or model.startswith("deepseek-") or model == "default":
            return None
        return model

    def is_available(self) -> tuple[bool, str]:
        path = shutil.which("codex")
        if not path:
            return False, "Binary 'codex' not found in PATH."

        auth_path = os.path.expanduser("~/.codex/auth.json")
        if not os.path.exists(auth_path):
            return False, "Codex is installed but auth.json not found. Run 'codex login'."

        return True, f"Found authenticated Codex CLI at {path}"

    def complete(
        self,
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,  # noqa: ARG002
        **kwargs: Any,
    ) -> LLMResponse:
        resolved_model = self._resolve_model(model)
        sys_str, user_str = self._format_messages(messages, system=system)
        full_prompt = f"{sys_str}\n\n{user_str}" if sys_str else user_str

        cmd = [
            "codex",
            "exec",
            "--ephemeral",
            "-s",
            "read-only",
            "--color",
            "never",
            "-",
        ]
        if resolved_model:
            cmd.extend(["--model", resolved_model])

        retcode, stdout, stderr = self._run_subprocess(
            cmd,
            input_text=full_prompt,
            timeout=kwargs.get("timeout"),
        )

        if retcode != 0:
            raise LLMError(f"Codex execution failed (exit code {retcode}): {stderr or stdout}")

        usage = self._estimate_tokens(full_prompt, stdout)
        return LLMResponse(
            content=stdout.strip(),
            usage=usage,
            cost_usd=0.0,
            model=model or self.default_model,
        )
