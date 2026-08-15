"""The per-posting scoring graph.

    render -> score -> validate --ok--> done
                 ^         |
                 +- retry -+   (bounded)

Small on purpose. The retry edge is the reason it is a graph at all: DeepSeek V4 is
documented to occasionally fall back into reasoning mode and return prose where a tool
call was required (langchainjs#10954). `structured_model()` disables thinking, which
prevents the common case, but a bounded validate-and-retry is what keeps one bad response
from costing a posting its verdict.

Deliberately **not** checkpointed. Durability for scoring lives in `jobs.pipeline_state`:
a posting that fails stays `new` and the next run picks it up. Checkpointing single-call
graphs would add a write per posting to buy a resumability the queue already provides.
"""

from typing import Any, TypedDict

from careerradar.core.llm import DEFAULT_SCORING_MODEL, structured_model
from careerradar.core.logger import get_logger
from careerradar.profile.models import FitAssessment
from careerradar.scoring import scale
from careerradar.scoring.audit import audit
from careerradar.scoring.prompts import render_posting

logger = get_logger()

MAX_ATTEMPTS = 3

RETRY_NUDGE = (
    "Your previous response did not call the verdict tool. Call it now with the "
    "structured verdict. Do not write prose."
)


# A generic nudge against a semantic failure just buys the same failure again: the model
# did call the tool, and telling it otherwise is a description it cannot act on. When the
# schema or the auditor rejected a well-formed answer, say which rule it broke.
def semantic_nudge(reason: str) -> str:
    return (
        f"Your previous verdict was rejected: {reason}\n"
        "Return the verdict again, corrected. Every quote must be copied from the posting "
        "exactly as it appears there."
    )


class ScoreState(TypedDict, total=False):
    system: str
    posting: dict[str, Any]
    # The deterministic extractor's read on this posting, already rendered. Built by the
    # worker, which owns the DB and the scorer; the graph must not open a connection.
    skill_hint: str
    rendered: str
    verdict: dict[str, Any] | None
    usage: dict[str, int] | None
    attempts: int
    error: str | None
    # Whether the last failure was the model answering the schema and being rejected,
    # rather than not answering at all. Decides which nudge the retry carries. Must be
    # declared here: LangGraph drops keys a node returns that the state does not name.
    semantic: bool
    model: str
    # Set by the worker so the auditor can check blockers against what the candidate
    # actually has, and strengths against what the posting actually mentions.
    profile: Any
    taxonomy: Any


def node_render(state: ScoreState) -> dict[str, Any]:
    return {
        "rendered": render_posting(
            state.get("posting") or {}, skill_hint=state.get("skill_hint") or ""
        ),
        "attempts": 0,
    }


def node_score(state: ScoreState) -> dict[str, Any]:
    model_name = state.get("model", DEFAULT_SCORING_MODEL)
    model = structured_model(model_name)
    chain = model.with_structured_output(
        FitAssessment, method="function_calling", strict=True, include_raw=True
    )

    messages = [("system", state.get("system") or ""), ("user", state.get("rendered") or "")]
    if state.get("attempts", 0):
        previous = state.get("error") or ""
        messages.append(
            ("user", semantic_nudge(previous) if state.get("semantic") else RETRY_NUDGE)
        )

    attempts = state.get("attempts", 0) + 1
    try:
        # `include_raw=True` makes this a dict at runtime; the stub only sees the
        # non-`include_raw` overload's BaseModel return type.
        result: dict[str, Any] = chain.invoke(messages)  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001 - network, rate limit, 400
        logger.warning("Scoring call failed (attempt %d): %s", attempts, exc)
        return {"attempts": attempts, "error": str(exc), "verdict": None}

    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")

    from careerradar.core.llm import token_usage

    usage = token_usage(raw) if raw is not None else None

    if parse_error is not None or parsed is None:
        # A cross-field validator in FitAssessment raising surfaces here too, not just a
        # malformed tool call: LangChain catches it and reports it as a parsing error.
        # Those are the semantic rejections, and they deserve a nudge that names the rule.
        logger.warning("Scoring returned unusable output (attempt %d): %s", attempts, parse_error)
        return {
            "attempts": attempts,
            "error": _rule_from(parse_error),
            "verdict": None,
            "usage": usage,
            "semantic": _is_semantic(parse_error),
        }

    # The verbatim-quote check cannot be a Pydantic validator -- it needs the posting text,
    # and LangChain builds the parser itself with no way to pass context through. It runs
    # here instead, which is also where the taxonomy cross-check has to live.
    parsed, flags, fatal = audit(
        parsed,
        posting=state.get("posting") or {},
        profile=state.get("profile"),
        taxonomy=state.get("taxonomy"),
    )
    if fatal is not None:
        logger.warning("Verdict failed audit (attempt %d): %s", attempts, fatal)
        return {
            "attempts": attempts,
            "error": fatal,
            "verdict": None,
            "usage": usage,
            "semantic": True,
        }

    verdict = parsed.model_dump()
    verdict["audit_flags"] = flags
    # The number is computed here rather than in the worker so that anything driving the
    # graph -- the eval harness included -- gets it through the same path.
    verdict.update(scale.project(verdict))
    return {
        "attempts": attempts,
        "verdict": verdict,
        "usage": usage,
        "error": None,
        "semantic": False,
    }


def _is_semantic(parse_error: Any) -> bool:
    """Did the model answer the schema and get rejected, or fail to answer at all?"""
    return "validation error" in str(parse_error).lower()


def _rule_from(parse_error: Any) -> str:
    """The rule text out of a Pydantic error, without the stack of field paths."""
    text = str(parse_error)
    marker = "Value error, "
    return text.split(marker, 1)[1].split("\n")[0].strip() if marker in text else text


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
