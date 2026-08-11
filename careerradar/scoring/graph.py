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

from typing import Optional, TypedDict

from careerradar.core.llm import DEFAULT_SCORING_MODEL, structured_model
from careerradar.core.logger import get_logger
from careerradar.profile.models import FitVerdict
from careerradar.scoring.prompts import render_posting

logger = get_logger()

MAX_ATTEMPTS = 3

RETRY_NUDGE = (
    "Your previous response did not call the verdict tool. Call it now with the "
    "structured verdict. Do not write prose."
)


class ScoreState(TypedDict, total=False):
    system: str
    posting: dict
    rendered: str
    verdict: Optional[dict]
    usage: Optional[dict]
    attempts: int
    error: Optional[str]
    model: str


def node_render(state: ScoreState) -> dict:
    return {"rendered": render_posting(state["posting"]), "attempts": 0}


def node_score(state: ScoreState) -> dict:
    model_name = state.get("model", DEFAULT_SCORING_MODEL)
    model = structured_model(model_name)
    chain = model.with_structured_output(
        FitVerdict, method="function_calling", strict=True, include_raw=True
    )

    messages = [("system", state["system"]), ("user", state["rendered"])]
    if state.get("attempts", 0):
        messages.append(("user", RETRY_NUDGE))

    attempts = state.get("attempts", 0) + 1
    try:
        result = chain.invoke(messages)
    except Exception as exc:  # network, rate limit, 400
        logger.warning("Scoring call failed (attempt %d): %s", attempts, exc)
        return {"attempts": attempts, "error": str(exc), "verdict": None}

    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")

    from careerradar.core.llm import token_usage

    usage = token_usage(raw) if raw is not None else None

    if parse_error is not None or parsed is None:
        logger.warning("Scoring returned unparseable output (attempt %d): %s",
                       attempts, parse_error)
        return {"attempts": attempts, "error": str(parse_error), "verdict": None,
                "usage": usage}

    return {"attempts": attempts, "verdict": parsed.model_dump(), "usage": usage,
            "error": None}


def route_after_score(state: ScoreState) -> str:
    if state.get("verdict") is not None:
        return "__end__"
    if state.get("attempts", 0) >= MAX_ATTEMPTS:
        return "__end__"
    return "score"


def build_graph():
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ScoreState)
    builder.add_node("render", node_render)
    builder.add_node("score", node_score)
    builder.add_edge(START, "render")
    builder.add_edge("render", "score")
    builder.add_conditional_edges(
        "score", route_after_score, {"score": "score", "__end__": END}
    )
    return builder.compile()
