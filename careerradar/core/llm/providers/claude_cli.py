"""Claude Code CLI LLM provider."""

import json
import shutil
from typing import Any

from careerradar.core.llm.base import LLMError, LLMResponse
from careerradar.core.llm.providers.cli_base import BaseCLIProvider
from careerradar.core.logger import get_logger

logger = get_logger()


class ClaudeCLIProvider(BaseCLIProvider):
    """LLM provider powered by the Claude Code CLI (`claude`)."""

    name = "claude"
    default_model = "claude-3-5-sonnet-20241022"
    default_scoring_model = "claude-3-5-sonnet-20241022"
    default_agent_model = "claude-3-5-sonnet-20241022"

    def _resolve_model(self, model: str | None) -> str | None:
        if not model or model.startswith("deepseek-") or model == "default":
            return None
        return model

    def is_available(self) -> tuple[bool, str]:
        path = shutil.which("claude")
        if not path:
            return False, "Binary 'claude' not found in PATH."

        # Check authentication status
        try:
            retcode, stdout, _ = self._run_subprocess(["claude", "auth", "status"], timeout=10.0)
            if retcode == 0:
                data = json.loads(stdout.strip())
                if data.get("loggedIn"):
                    email = data.get("email", "")
                    return True, f"Found authenticated Claude CLI at {path} ({email})"
            return False, "Claude CLI is installed but not authenticated. Run 'claude auth login'."
        except Exception:  # noqa: BLE001
            return True, f"Found Claude CLI at {path} (auth status check skipped)"

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
            "claude",
            "-p",
            "--tools",
            "",
            "--no-session-persistence",
            "--output-format",
            "text",
        ]
        if resolved_model:
            cmd.extend(["--model", resolved_model])

        retcode, stdout, stderr = self._run_subprocess(
            cmd,
            input_text=full_prompt,
            timeout=kwargs.get("timeout"),
        )

        if retcode != 0:
            if "OAuth session expired" in stdout or "OAuth session expired" in stderr:
                raise LLMError("Claude CLI session expired. Please run 'claude auth login'.")
            raise LLMError(f"Claude execution failed (exit code {retcode}): {stderr or stdout}")

        usage = self._estimate_tokens(full_prompt, stdout)
        return LLMResponse(
            content=stdout.strip(),
            usage=usage,
            cost_usd=0.0,
            model=model or self.default_model,
        )
