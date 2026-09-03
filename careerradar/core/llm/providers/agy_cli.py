"""Antigravity CLI (agy) LLM provider."""

import json
import shutil
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from careerradar.core.llm.base import LLMError, LLMResponse, StructuredOutputError
from careerradar.core.llm.providers.cli_base import BaseCLIProvider
from careerradar.core.logger import get_logger

T = TypeVar("T", bound=BaseModel)
logger = get_logger()


class AGYCLIProvider(BaseCLIProvider):
    """LLM provider powered by the Antigravity CLI (`agy`)."""

    name = "agy"
    default_model = "gemini-3.7-flash-high"
    default_scoring_model = "gemini-3.7-flash-high"
    default_agent_model = "gemini-3.7-flash-high"

    def _resolve_model(self, model: str | None) -> str:
        if not model or model.startswith("deepseek-") or model == "default":
            return self.default_model
        return model

    def is_available(self) -> tuple[bool, str]:
        path = shutil.which("agy")
        if not path:
            return False, "Binary 'agy' not found in PATH."
        return True, f"Found authenticated agy at {path}"

    def complete(
        self,
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,  # noqa: ARG002
        **kwargs: Any,
    ) -> LLMResponse:
        model_name = self._resolve_model(model)
        sys_str, user_str = self._format_messages(messages, system=system)
        full_prompt = f"{sys_str}\n\n{user_str}" if sys_str else user_str

        cmd = [
            "agy",
            "-p",
            full_prompt,
            "--output-format",
            "text",
            "--disable-slash-commands",
            "--model",
            model_name,
        ]

        retcode, stdout, stderr = self._run_subprocess(
            cmd,
            timeout=kwargs.get("timeout"),
        )

        if retcode != 0:
            raise LLMError(f"agy execution failed (exit code {retcode}): {stderr or stdout}")

        usage = self._estimate_tokens(full_prompt, stdout)
        return LLMResponse(content=stdout.strip(), usage=usage, cost_usd=0.0, model=model_name)

    def complete_structured(
        self,
        schema: type[T],
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,  # noqa: ARG002
        attempts: int = 3,
        label: str = "",
        **kwargs: Any,
    ) -> T:
        model_name = self._resolve_model(model)
        sys_str, user_str = self._format_messages(messages, system=system)
        full_prompt = f"{sys_str}\n\n{user_str}" if sys_str else user_str
        schema_json = json.dumps(schema.model_json_schema())

        cmd = [
            "agy",
            "-p",
            full_prompt,
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--disable-slash-commands",
            "--model",
            model_name,
        ]

        for attempt in range(1, attempts + 1):
            try:
                retcode, stdout, stderr = self._run_subprocess(
                    cmd,
                    timeout=kwargs.get("timeout"),
                )
                if retcode != 0:
                    raise LLMError(
                        f"agy execution failed (exit code {retcode}): {stderr or stdout}"
                    )

                data = json.loads(stdout.strip())
                struct_data = data.get("structured_output")
                if struct_data is not None:
                    return schema.model_validate(struct_data)

                # Fallback to response text extraction if structured_output key missing
                resp_text = data.get("response", "")
                parsed = self._extract_json_from_text(resp_text)
                if isinstance(parsed, dict):
                    return schema.model_validate(parsed)
            except (json.JSONDecodeError, ValidationError, LLMError, ValueError) as exc:
                logger.warning(
                    "%s: agy structured output attempt %d/%d failed: %s",
                    label or self.name,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt == attempts:
                    raise StructuredOutputError(
                        f"{label or self.name}: failed to produce valid {schema.__name__} "
                        f"after {attempts} attempts: {exc}"
                    ) from exc

        raise StructuredOutputError(
            f"{label or self.name}: failed to produce valid {schema.__name__}"
        )
