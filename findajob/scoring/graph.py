"""The per-posting scoring graph.

render -> score -> done, with bounded retries and no automatic reviewer.
"""

from functools import partial
from threading import Lock
from typing import Any, TypedDict

from pydantic import BaseModel

from findajob.core.llm import structured_model
from findajob.core.logger import get_logger
from findajob.profile.models import PriorityVerdict
from findajob.scoring.prompts import RULES, render_posting

logger = get_logger()

MAX_ATTEMPTS = 3

RETRY_NUDGE = (
    "Your previous response did not call the verdict tool. Call it now with the "
    "structured verdict. Do not write prose."
)


def semantic_nudge(reason: str) -> str:
    return f"Your previous verdict was rejected: {reason}\nReturn the verdict again, corrected."


class CallBudget:
    """Serialize metered calls under a cap; stop before any call after exhaustion.

    Provider balance costs are known only after a call, so the final call can cross
    the ceiling. No later stage, retry, or posting may start after that boundary.
    """

    def __init__(self, max_usd: float | None = None):
        self.max_usd = max_usd
        self.usd = 0.0
        self.tracking_failed = False
        self.lock = Lock()

    def exhausted(self) -> bool:
        return self.max_usd is not None and (self.tracking_failed or self.usd >= self.max_usd)


class ScoreState(TypedDict, total=False):
    system: str
    posting: dict[str, Any]
    rendered: str
    verdict: dict[str, Any] | None
    usage: dict[str, int] | None
    cost_usd: float
    cost_recorded: int
    attempts: int
    error: str | None
    semantic: bool
    model: str
    provider: str
    profile: Any
    budget: Any
    budget_exhausted: bool
    profile_hash: str
    balance_exhausted: bool


def node_render(state: ScoreState) -> dict[str, Any]:
    return {
        "rendered": render_posting(state.get("posting") or {}),
        "attempts": 0,
        "verdict": None,
        "error": None,
        "semantic": False,
        "balance_exhausted": False,
        "budget_exhausted": False,
    }


def node_score(
    state: ScoreState, *, schema: type[BaseModel] = PriorityVerdict, rules: str = RULES
) -> dict[str, Any]:
    instructions = state.get("system") or ""
    stage_rules = rules
    if stage_rules not in instructions:
        instructions += "\n" + stage_rules
    messages = [("system", instructions), ("user", state.get("rendered") or "")]
    if state.get("attempts", 0):
        previous = state.get("error") or ""
        messages.append(
            ("user", semantic_nudge(previous) if state.get("semantic") else RETRY_NUDGE)
        )

    attempts = state.get("attempts", 0) + 1
    budget = state.get("budget")
    if budget is not None and budget.max_usd is not None:
        budget.lock.acquire()
        if budget.exhausted():
            budget.lock.release()
            return {
                "budget_exhausted": True,
                "error": "Cost tracking unavailable"
                if budget.tracking_failed
                else "Run spending limit reached",
                "verdict": None,
            }
    try:
        model_name = state.get("model") or None
        provider = state.get("provider")
        model = (
            structured_model(
                model_name, role="scoring", provider=provider, max_tokens=256, max_retries=0
            )
            if provider
            else structured_model(model_name, role="scoring", max_tokens=256, max_retries=0)
        )
        chain = model.with_structured_output(
            schema,
            method="function_calling",
            strict=True,
            include_raw=True,
        )
        result: dict[str, Any] = chain.invoke(messages)
        # Validate even when a provider adapter returns an unexpected model type.
        if result.get("parsed") is not None:
            try:
                payload = result["parsed"]
                if isinstance(payload, BaseModel):
                    payload = payload.model_dump()
                result["parsed"] = schema.model_validate(payload)
            except ValueError as exc:
                result["parsed"] = None
                result["parsing_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - network, rate limit, 400
        logger.warning("Scoring call failed (attempt %d): %s", attempts, exc)
        return {
            "attempts": attempts,
            "error": str(exc),
            "verdict": None,
            "balance_exhausted": (
                getattr(exc, "status_code", None) == 402
                or "insufficient balance" in str(exc).lower()
            ),
        }

    finally:
        if budget is not None and budget.max_usd is not None:
            raw_result = locals().get("result", {}).get("raw")
            if "result" in locals():
                charge = _raw_cost(raw_result)
                budget.tracking_failed |= charge is None
                budget.usd += charge or 0.0
            budget.lock.release()

    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")

    from findajob.core.llm import no_tool_call_reason, token_usage

    usage = _add(state.get("usage"), token_usage(raw) if raw is not None else None)
    raw_cost = _raw_cost(raw)
    cost = _add_cost(state.get("cost_usd"), raw_cost)
    cost_recorded = state.get("cost_recorded", 0) + (raw_cost is not None)

    if parse_error is not None or parsed is None:
        reason = parse_error if parse_error is not None else no_tool_call_reason(raw)

        metadata = getattr(raw, "response_metadata", None) or {}
        truncated = ""
        if metadata.get("finish_reason") == "length":
            truncated = " -- and the response hit the output token limit"

        logger.warning(
            "Scoring returned unusable output (attempt %d): %s%s", attempts, reason, truncated
        )
        return {
            "attempts": attempts,
            "error": _rule_from(reason) + truncated,
            "verdict": None,
            "usage": usage,
            "cost_usd": cost,
            "cost_recorded": cost_recorded,
            "semantic": _is_semantic(parse_error),
        }

    verdict = parsed.model_dump()
    return {
        "attempts": attempts,
        "verdict": verdict,
        "usage": usage,
        "cost_usd": cost,
        "cost_recorded": cost_recorded,
        "error": None,
        "semantic": False,
    }


def _raw_cost(raw: Any) -> float | None:
    # AIMessage stores provider extras outside __dict__ (Pydantic v2).
    value = getattr(raw, "findajob_cost_usd", None) if raw is not None else None
    return float(value) if isinstance(value, (float, int)) else None


def _add(total: dict[str, int] | None, usage: dict[str, int] | None) -> dict[str, int] | None:
    """Add this attempt's tokens to what the posting has already spent."""
    if usage is None:
        return total
    if total is None:
        return usage
    return {key: total[key] + value for key, value in usage.items()}


def _add_cost(total: float | None, cost: float | None) -> float:
    return (total or 0.0) + (cost or 0.0)


def _is_semantic(parse_error: Any) -> bool:
    """Did the model answer the schema and get rejected, or fail to answer at all?"""
    return "validation error" in str(parse_error).lower()


def _rule_from(parse_error: Any) -> str:
    """The rule a Pydantic error names, without the input dump around it."""
    text = str(parse_error)
    marker = "Value error, "
    if marker in text:
        return text.split(marker, 1)[1].split("\n")[0].split(" [type=")[0].strip()

    rules: list[str] = []
    field = ""
    for line in text.split("\n")[1:]:
        if line.startswith("    "):
            continue
        if not line.startswith(" "):
            field = line.strip()
            continue
        message = line.strip().split(" [type=")[0].strip()
        rules.append(f"{field}: {message}" if field else message)
    return "; ".join(rules) or text


def route_after_score(state: ScoreState) -> str:
    if (
        state.get("balance_exhausted")
        or state.get("budget_exhausted")
        or state.get("verdict") is not None
        or state.get("attempts", 0) >= MAX_ATTEMPTS
    ):
        return "__end__"
    return "score"


def build_graph(*, schema: type[BaseModel] = PriorityVerdict, rules: str = RULES) -> Any:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ScoreState)
    builder.add_node("render", node_render)
    builder.add_node("score", partial(node_score, schema=schema, rules=rules))
    builder.add_edge(START, "render")
    builder.add_edge("render", "score")
    builder.add_conditional_edges("score", route_after_score, {"score": "score", "__end__": END})
    return builder.compile()
