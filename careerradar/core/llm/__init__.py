"""Unified LLM subsystem and provider interface for Find a Job."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from careerradar.core.llm.base import (
    BaseLLMProvider,
    LLMError,
    LLMResponse,
    LLMUnavailableError,
    MissingApiKey,
    StructuredOutputError,
)
from careerradar.core.llm.factory import get_llm_provider, list_available_providers
from careerradar.core.llm.providers.deepseek_api import (
    DEFAULT_DEEPSEEK_AGENT_MODEL,
    DEFAULT_DEEPSEEK_SCORING_MODEL,
    BalanceMeasuredChat,
    no_tool_call_reason,
    require_api_key,
    token_usage,
)

T = TypeVar("T", bound=BaseModel)

DEFAULT_SCORING_MODEL = DEFAULT_DEEPSEEK_SCORING_MODEL
DEFAULT_AGENT_MODEL = DEFAULT_DEEPSEEK_AGENT_MODEL


class StructuredChainAdapter(Generic[T]):
    """Adapter implementing LangChain with_structured_output runnable interface for CLI tools."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        schema: type[T],
        model_name: str | None = None,
        include_raw: bool = False,
    ):
        self.provider = provider
        self.schema = schema
        self.model_name = model_name
        self.include_raw = include_raw

    def invoke(self, messages: list[Any]) -> Any:
        try:
            parsed = self.provider.complete_structured(
                schema=self.schema,
                messages=messages,
                model=self.model_name,
            )
            if self.include_raw:
                return {"parsed": parsed, "parsing_error": None, "raw": None}
            return parsed
        except Exception as exc:
            if self.include_raw:
                return {"parsed": None, "parsing_error": str(exc), "raw": None}
            raise


class ModelAdapter:
    """Adapter wrapping BaseLLMProvider to provide LangChain-compatible model interface."""

    def __init__(self, provider: BaseLLMProvider, model_name: str | None = None):
        self.provider = provider
        self.model_name = model_name

    def with_structured_output(self, schema: type[T], **kwargs: Any) -> StructuredChainAdapter[T]:
        include_raw = bool(kwargs.get("include_raw", False))
        return StructuredChainAdapter(
            self.provider, schema, model_name=self.model_name, include_raw=include_raw
        )

    def invoke(self, messages: list[Any]) -> Any:
        resp = self.provider.complete(messages, model=self.model_name)
        return resp.content


def get_model_for_role(
    role: str = "scoring",
    override: str | None = None,
    provider: BaseLLMProvider | str | None = None,
) -> str:
    """Resolve the appropriate model name for a pipeline role ('scoring' or 'agent')."""
    if override:
        return override
    from careerradar.core.config import load_config

    config = load_config()
    llm_cfg = config.get("llm") or {}
    key = "scoring_model" if role == "scoring" else "agent_model"
    custom = llm_cfg.get(key)
    if custom:
        return str(custom)
    try:
        if isinstance(provider, BaseLLMProvider):
            prov = provider
        elif isinstance(provider, str):
            prov = get_llm_provider(provider)
        else:
            prov = get_llm_provider()

        if role == "scoring":
            return getattr(prov, "default_scoring_model", prov.default_model)
        return getattr(prov, "default_agent_model", prov.default_model)
    except LLMUnavailableError:
        return "deepseek-chat"


def structured_model(
    model: str | None = None,
    *,
    provider: str | None = None,
    role: str = "scoring",
    temperature: float = 0.0,
    **kwargs: Any,
) -> Any:
    """Build a structured model adapter for schema-enforced output."""
    prov = get_llm_provider(provider)
    resolved_model = model or get_model_for_role(role=role, provider=prov)
    if prov.name == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        require_api_key()
        return BalanceMeasuredChat(
            ChatDeepSeek(
                model=resolved_model,
                reasoning_effort="none",
                temperature=temperature,
                **kwargs,
            )
        )
    return ModelAdapter(prov, model_name=resolved_model)


def invoke_structured(
    model: Any,
    schema: type[T],
    messages: list[Any],
    *,
    provider: str | None = None,
    label: str = "",
    attempts: int = 3,
) -> T:
    """Unified helper for schema-enforced structured model execution."""
    if model is not None and hasattr(model, "with_structured_output"):
        chain = model.with_structured_output(
            schema, method="function_calling", strict=True, include_raw=True
        )
        reason = None
        for attempt in range(1, attempts + 1):
            turns = list(messages)
            if attempt > 1:
                nudge = (
                    "Your previous response did not call the tool. "
                    "Call it now with the complete structured object."
                )
                turns.append(("user", nudge))
            result: dict[str, Any] = chain.invoke(turns)
            parsed = result.get("parsed")
            if parsed is not None and result.get("parsing_error") is None:
                return parsed
            reason = result.get("parsing_error") or no_tool_call_reason(result.get("raw"))
        raise StructuredOutputError(
            f"{label or 'structured_invoke'}: the model did not return a valid "
            f"{schema.__name__} in {attempts} attempts. Last reason: {reason}"
        )

    prov = get_llm_provider(provider)
    return prov.complete_structured(
        schema=schema,
        messages=messages,
        attempts=attempts,
        label=label,
    )


def estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
) -> float:
    """Pre-flight pricing is unavailable; DeepSeek cost is measured after a call."""
    del model, prompt_tokens, completion_tokens, cached_tokens
    return 0.0


class Spend:
    """Running cost for one worker run, with a hard ceiling."""

    def __init__(self, model: str, max_usd: float | None = None) -> None:
        self.model = model
        self.max_usd = max_usd
        self.usd = 0.0
        self.calls = 0
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0

    def record(self, message: Any) -> tuple[dict[str, int], float]:
        usage = (
            token_usage(message)
            if message is not None
            else {"prompt": 0, "cache_hit": 0, "cache_miss": 0, "completion": 0}
        )
        cost = vars(message).get("careerradar_cost_usd", 0.0) if message is not None else 0.0
        self.usd += cost
        self.calls += 1
        self.tokens_in += usage.get("prompt", 0)
        self.tokens_cached += usage.get("cache_hit", 0)
        self.tokens_out += usage.get("completion", 0)
        return usage, cost

    def exhausted(self) -> bool:
        return self.max_usd is not None and self.usd >= self.max_usd

    def summary(self) -> dict[str, Any]:
        cache_rate = (self.tokens_cached / self.tokens_in) if self.tokens_in else 0.0
        return {
            "model": self.model,
            "calls": self.calls,
            "usd": round(self.usd, 4),
            "tokens_in": self.tokens_in,
            "tokens_cached": self.tokens_cached,
            "tokens_out": self.tokens_out,
            "cache_rate": round(cache_rate, 3),
        }


__all__ = [
    "DEFAULT_AGENT_MODEL",
    "DEFAULT_SCORING_MODEL",
    "BaseLLMProvider",
    "LLMError",
    "LLMResponse",
    "LLMUnavailableError",
    "MissingApiKey",
    "ModelAdapter",
    "Spend",
    "StructuredChainAdapter",
    "StructuredOutputError",
    "estimate_cost",
    "get_llm_provider",
    "get_model_for_role",
    "invoke_structured",
    "list_available_providers",
    "no_tool_call_reason",
    "require_api_key",
    "structured_model",
    "token_usage",
]
