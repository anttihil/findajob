"""DeepSeek model construction and cost accounting, in one place.

Three constraints from the live API drove this module's shape. They are documented in
full in `docs/deepseek.md`; the short version:

  1. `response_format: json_schema` does not exist on DeepSeek yet, so schema-enforced
     output has to go through function calling.
  2. Function calling with a *forced* tool choice is rejected while thinking mode is on
     ("Thinking mode does not support this tool_choice"), and V4 thinks by default. So a
     model used for structured output must disable thinking; a model used for open-ended
     tool work must leave it on and let the model choose.
  3. Prompt prefixes are cached automatically at 1/50th the input price, which is what
     makes per-posting scoring cost a fraction of a cent -- but only if the prefix is
     byte-identical across calls.

`structured_model()` and `agentic_model()` exist so callers pick a *purpose* rather than
remembering which of those apply.
"""

import os

from careerradar.core.logger import get_logger

logger = get_logger()

# USD per million tokens, from https://deepseek.ai/pricing (checked 2026-08-11).
# `in_hit` is the automatic prefix-cache rate -- two orders of magnitude below `in_miss`,
# which is why prompt layout is a cost decision and not a style one.
PRICES = {
    "deepseek-v4-flash": {"in_miss": 0.14, "in_hit": 0.0028, "out": 0.28},
    "deepseek-v4-pro": {"in_miss": 0.435, "in_hit": 0.003625, "out": 0.87},
}

DEFAULT_SCORING_MODEL = "deepseek-v4-flash"
DEFAULT_AGENT_MODEL = "deepseek-v4-pro"


class MissingApiKey(RuntimeError):
    pass


def require_api_key():
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise MissingApiKey(
            "DEEPSEEK_API_KEY is not set. Add it to .env (it is gitignored) or export it."
        )
    return key


def structured_model(model=DEFAULT_SCORING_MODEL, **kwargs):
    """A model for schema-enforced output. Thinking is OFF.

    Required: `with_structured_output(..., method="function_calling", strict=True)`
    compiles to a forced `tool_choice`, which the API rejects outright while thinking is
    enabled. `reasoning_effort="none"` is the documented lever and, unlike the
    `extra_body` spelling, it is a first-class parameter so LangChain does not route it
    through `model_kwargs` with a warning.
    """
    from langchain_deepseek import ChatDeepSeek

    require_api_key()
    return ChatDeepSeek(model=model, reasoning_effort="none", **kwargs)


def agentic_model(model=DEFAULT_AGENT_MODEL, **kwargs):
    """A model for open-ended tool use. Thinking stays ON.

    Safe here because an agent binds tools with `tool_choice="auto"` -- the model decides
    whether to call one -- and auto choice *is* accepted in thinking mode. Do not call
    `with_structured_output(strict=True)` on this; use `structured_model()` for that.
    """
    from langchain_deepseek import ChatDeepSeek

    require_api_key()
    return ChatDeepSeek(model=model, **kwargs)


def token_usage(message):
    """Pull DeepSeek's cache-aware token counts off a LangChain response.

    The standard `usage_metadata` does not carry the cache split, so this reads the raw
    `token_usage` block. Missing keys mean an older API shape, not zero usage -- callers
    that treat `cache_hit` as authoritative should check `prompt` too.
    """
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


def usage_cost(model, usage):
    """USD for one call, charging cached and uncached input at their own rates."""
    price = PRICES.get(model)
    if price is None:
        return 0.0
    return (
        usage["cache_miss"] * price["in_miss"]
        + usage["cache_hit"] * price["in_hit"]
        + usage["completion"] * price["out"]
    ) / 1_000_000


def estimate_cost(model, prompt_tokens, completion_tokens, cached_tokens=0):
    """Pre-flight estimate, used to decide whether a run is allowed to start."""
    return usage_cost(
        model,
        {
            "cache_hit": cached_tokens,
            "cache_miss": max(prompt_tokens - cached_tokens, 0),
            "completion": completion_tokens,
            "prompt": prompt_tokens,
        },
    )


class Spend:
    """Running cost for one worker run, with a hard ceiling.

    The ceiling aborts rather than trimming the work queue. A budget that silently drops
    the tail of the queue makes coverage depend on an invisible calculation -- you get a
    partial pass that looks like a complete one. Failing loudly is recoverable; a quietly
    incomplete corpus is not.
    """

    def __init__(self, model, max_usd=None):
        self.model = model
        self.max_usd = max_usd
        self.usd = 0.0
        self.calls = 0
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0

    def record(self, message):
        usage = token_usage(message)
        cost = usage_cost(self.model, usage)
        self.usd += cost
        self.calls += 1
        self.tokens_in += usage["prompt"]
        self.tokens_cached += usage["cache_hit"]
        self.tokens_out += usage["completion"]
        return usage, cost

    def exhausted(self):
        return self.max_usd is not None and self.usd >= self.max_usd

    def summary(self):
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
