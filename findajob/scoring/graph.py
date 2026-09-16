"""The per-posting scoring graph.

render -> score -> validate --ok--> done
             ^         |
             +- retry -+   (bounded)
"""

from typing import Any, TypedDict

from findajob.core.llm import structured_model
from findajob.core.logger import get_logger
from findajob.profile.models import JobFitVerdict
from findajob.scoring.prompts import render_posting

logger = get_logger()

MAX_ATTEMPTS = 3

RETRY_NUDGE = (
    "Your previous response did not call the verdict tool. Call it now with the "
    "structured verdict. Do not write prose."
)


def semantic_nudge(reason: str) -> str:
    return f"Your previous verdict was rejected: {reason}\nReturn the verdict again, corrected."


class ScoreState(TypedDict, total=False):
    system: str
    posting: dict[str, Any]
    rendered: str
    verdict: dict[str, Any] | None
    usage: dict[str, int] | None
    cost_usd: float
    attempts: int
    error: str | None
    semantic: bool
    model: str
    profile: Any


def node_render(state: ScoreState) -> dict[str, Any]:
    return {
        "rendered": render_posting(state.get("posting") or {}),
        "attempts": 0,
    }


def node_score(state: ScoreState) -> dict[str, Any]:
    model_name = state.get("model") or None
    model = structured_model(model_name, role="scoring")
    chain = model.with_structured_output(
        JobFitVerdict, method="function_calling", strict=True, include_raw=True
    )

    messages = [("system", state.get("system") or ""), ("user", state.get("rendered") or "")]
    if state.get("attempts", 0):
        previous = state.get("error") or ""
        messages.append(
            ("user", semantic_nudge(previous) if state.get("semantic") else RETRY_NUDGE)
        )

    attempts = state.get("attempts", 0) + 1
    try:
        result: dict[str, Any] = chain.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - network, rate limit, 400
        logger.warning("Scoring call failed (attempt %d): %s", attempts, exc)
        return {"attempts": attempts, "error": str(exc), "verdict": None}

    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")

    from findajob.core.llm import no_tool_call_reason, token_usage

    usage = _add(state.get("usage"), token_usage(raw) if raw is not None else None)
    raw_cost = vars(raw).get("findajob_cost_usd") if raw is not None else None
    cost = _add_cost(state.get("cost_usd"), raw_cost)

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
            "semantic": _is_semantic(parse_error),
        }

    verdict = parsed.model_dump()
    return {
        "attempts": attempts,
        "verdict": verdict,
        "usage": usage,
        "cost_usd": cost,
        "error": None,
        "semantic": False,
    }


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
    if state.get("verdict") is not None:
        return "__end__"
    if state.get("attempts", 0) >= MAX_ATTEMPTS:
        return "__end__"
    return "score"


def build_graph() -> Any:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ScoreState)
    builder.add_node("render", node_render)
    builder.add_node("score", node_score)
    builder.add_edge(START, "render")
    builder.add_edge("render", "score")
    builder.add_conditional_edges("score", route_after_score, {"score": "score", "__end__": END})
    return builder.compile()
