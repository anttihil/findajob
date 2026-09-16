"""DeepSeek API LLM provider."""

import json
import os
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any, TypeVar
from urllib.request import Request, urlopen

from pydantic import BaseModel

from findajob.core.llm.base import (
    BaseLLMProvider,
    LLMResponse,
    MissingApiKey,
    StructuredOutputError,
)
from findajob.core.logger import get_logger

T = TypeVar("T", bound=BaseModel)
logger = get_logger()

BALANCE_URL = "https://api.deepseek.com/user/balance"
_balance_measurement_lock = Lock()

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


def usd_balance() -> float | None:
    """Return the USD balance reported by DeepSeek, or ``None`` when unavailable.

    DeepSeek does not expose a per-request price field.  Its balance endpoint is the
    provider's source of truth for what an API call was actually charged.
    """
    request = Request(BALANCE_URL, headers={"Authorization": f"Bearer {require_api_key()}"})
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
        for info in payload.get("balance_infos", []):
            if info.get("currency") == "USD":
                return float(Decimal(str(info["total_balance"])))
    except (OSError, ValueError, KeyError, InvalidOperation, json.JSONDecodeError) as exc:
        logger.warning("Could not read DeepSeek API balance for cost tracking: %s", exc)
        return None
    logger.warning("DeepSeek API did not return a USD balance; cost was not recorded.")
    return None


def balance_delta_usd(before: float | None, after: float | None) -> float | None:
    """Actual charge between two DeepSeek balance snapshots, never a token estimate."""
    if before is None or after is None:
        return None
    # A concurrent top-up must not turn into a negative "cost".
    return max(before - after, 0.0)


class BalanceMeasuredChain:
    """Attach DeepSeek's balance-derived charge to a structured-output raw response."""

    def __init__(self, chain: Any) -> None:
        self.chain = chain

    def invoke(self, *args: Any, **kwargs: Any) -> Any:
        # A balance is account-wide. Keep the API call between its own snapshots so
        # concurrent calls from this process cannot be charged to the wrong result.
        with _balance_measurement_lock:
            before = usd_balance()
            result = self.chain.invoke(*args, **kwargs)
            after = usd_balance()
        raw = result.get("raw") if isinstance(result, dict) else None
        if raw is not None:
            raw.findajob_cost_usd = balance_delta_usd(before, after)
        return result


class BalanceMeasuredChat:
    """LangChain model wrapper that measures only DeepSeek API calls."""

    def __init__(self, chat: Any) -> None:
        self.chat = chat

    def invoke(self, *args: Any, **kwargs: Any) -> tuple[Any, float | None]:
        with _balance_measurement_lock:
            before = usd_balance()
            result = self.chat.invoke(*args, **kwargs)
            return result, balance_delta_usd(before, usd_balance())

    def with_structured_output(self, *args: Any, **kwargs: Any) -> BalanceMeasuredChain:
        return BalanceMeasuredChain(self.chat.with_structured_output(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.chat, name)


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

        res, cost = BalanceMeasuredChat(chat).invoke(formatted)
        usage = token_usage(res)
        model_name = model or self.default_model
        return LLMResponse(
            content=str(getattr(res, "content", "")),
            usage=usage,
            cost_usd=cost or 0.0,
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
            result: dict[str, Any] = BalanceMeasuredChain(chain).invoke(turns)
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
        # DeepSeek exposes no pricing endpoint. Costs are recorded from its balance
        # endpoint after the request instead of being guessed before it.
        del model, prompt_tokens, completion_tokens, cached_tokens
        return 0.0
