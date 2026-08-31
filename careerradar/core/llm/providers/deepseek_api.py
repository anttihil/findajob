"""DeepSeek API LLM provider."""

import os
from typing import Any, TypeVar

from pydantic import BaseModel

from careerradar.core.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    MissingApiKey,
    StructuredOutputError,
)
from careerradar.core.logger import get_logger

T = TypeVar("T", bound=BaseModel)
logger = get_logger()

# USD per million tokens from https://deepseek.ai/pricing (checked 2026-08-11).
PRICES = {
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.0028, "out": 0.28},
    "deepseek-v4-pro": {"in_miss": 0.435, "in_hit": 0.003625, "out": 0.87},
    "deepseek-chat": {"in_miss": 0.14, "in_hit": 0.0028, "out": 0.28},
}

DEFAULT_DEEPSEEK_SCORING_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_AGENT_MODEL = "deepseek-v4-pro"
STRUCTURED_RETRY_NUDGE = (
    "Your previous response did not call the tool. Call it now, with the complete "
    "structured object. Do not write prose."
)


def require_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise MissingApiKey(
            "DEEPSEEK_API_KEY is not set. Add it to .env (it is gitignored) or export it."
        )
    return key


def token_usage(message: Any) -> dict[str, int]:
    """Pull DeepSeek's cache-aware token counts off a LangChain response."""
    metadata = getattr(message, "response_metadata", None) or {}
    usage = metadata.get("token_usage") or {}
    prompt = usage.get("prompt_tokens") or 0
    cache_hit = usage.get("prompt_cache_hit_tokens") or 0
    return {
        "prompt": prompt,
        "cache_hit": cache_hit,
        "cache_miss": usage.get("prompt_cache_miss_tokens", max(prompt - cache_hit, 0)),
        "completion": usage.get("completion_tokens") or 0,
    }


def usage_cost(model: str, usage: dict[str, int]) -> float:
    """USD for one call, charging cached and uncached input at their own rates."""
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return (
        usage.get("cache_miss", 0) * price["in_miss"]
        + usage.get("cache_hit", 0) * price["in_hit"]
        + usage.get("completion", 0) * price["out"]
    ) / 1_000_000


def no_tool_call_reason(raw: Any) -> str:
    """Say why a schema-enforced call came back empty."""
    if raw is None:
        return "no response"
    metadata = getattr(raw, "response_metadata", None) or {}
    finish = metadata.get("finish_reason")
    if finish == "length":
        return "the response hit the output token limit before the tool call finished"
    invalid = getattr(raw, "invalid_tool_calls", None) or []
    if invalid:
        return f"malformed tool-call arguments ({invalid[0].get('error') or 'unparseable'})"
    text = " ".join(str(getattr(raw, "content", "") or "").split())
    if text:
        return f"prose instead of a tool call: {text[:200]}"
    return f"empty response (finish_reason={finish})"


class DeepSeekAPIProvider(BaseLLMProvider):
    """LLM provider using DeepSeek API via LangChain."""

    name = "deepseek"
    default_model = DEFAULT_DEEPSEEK_SCORING_MODEL
    default_scoring_model = DEFAULT_DEEPSEEK_SCORING_MODEL
    default_agent_model = DEFAULT_DEEPSEEK_AGENT_MODEL

    def is_available(self) -> tuple[bool, str]:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            return False, "DEEPSEEK_API_KEY environment variable is not set."
        return True, "DEEPSEEK_API_KEY configured."

    def _get_chat_model(self, model: str | None = None, temperature: float = 0.0) -> Any:
        from langchain_deepseek import ChatDeepSeek

        require_api_key()
        model_name = model or self.default_model
        return ChatDeepSeek(
            model=model_name,
            reasoning_effort="none",
            temperature=temperature,
        )

    def complete(
        self,
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        **kwargs: Any,  # noqa: ARG002
    ) -> LLMResponse:
        chat = self._get_chat_model(model=model, temperature=temperature)
        formatted: list[tuple[str, str]] = []
        if system:
            formatted.append(("system", system))

        if isinstance(messages, str):
            formatted.append(("user", messages))
        else:
            for m in messages:
                if isinstance(m, tuple):
                    formatted.append(m)
                elif isinstance(m, dict):
                    formatted.append((m.get("role", "user"), m.get("content", "")))
                else:
                    formatted.append(("user", str(m)))

        res = chat.invoke(formatted)
        usage = token_usage(res)
        model_name = model or self.default_model
        cost = usage_cost(model_name, usage)
        return LLMResponse(
            content=str(getattr(res, "content", "")),
            usage=usage,
            cost_usd=cost,
            model=model_name,
            raw=res,
        )

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
        **kwargs: Any,  # noqa: ARG002
    ) -> T:
        chat = self._get_chat_model(model=model, temperature=temperature)
        chain = chat.with_structured_output(
            schema, method="function_calling", strict=True, include_raw=True
        )

        formatted: list[tuple[str, str]] = []
        if system:
            formatted.append(("system", system))

        if isinstance(messages, str):
            formatted.append(("user", messages))
        else:
            for m in messages:
                if isinstance(m, tuple):
                    formatted.append(m)
                elif isinstance(m, dict):
                    formatted.append((m.get("role", "user"), m.get("content", "")))
                else:
                    formatted.append(("user", str(m)))

        reason = None
        for attempt in range(1, attempts + 1):
            turns = list(formatted)
            if attempt > 1:
                turns.append(("user", STRUCTURED_RETRY_NUDGE))
            result: dict[str, Any] = chain.invoke(turns)
            parsed = result.get("parsed")
            if parsed is not None and result.get("parsing_error") is None:
                return parsed
            reason = result.get("parsing_error") or no_tool_call_reason(result.get("raw"))
            logger.warning(
                "%s: no usable %s (attempt %d/%d): %s",
                label or self.name,
                schema.__name__,
                attempt,
                attempts,
                reason,
            )

        raise StructuredOutputError(
            f"{label or self.name}: the model did not return a valid {schema.__name__} in "
            f"{attempts} attempts. Last reason: {reason}"
        )

    def estimate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int = 0,
    ) -> float:
        return usage_cost(
            model,
            {
                "cache_hit": cached_tokens,
                "cache_miss": max(prompt_tokens - cached_tokens, 0),
                "completion": completion_tokens,
                "prompt": prompt_tokens,
            },
        )
