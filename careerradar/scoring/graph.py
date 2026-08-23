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
from careerradar.profile.models import JobFitVerdict
from careerradar.scoring.prompts import render_posting

logger = get_logger()

MAX_ATTEMPTS = 3

RETRY_NUDGE = (
    "Your previous response did not call the verdict tool. Call it now with the "
    "structured verdict. Do not write prose."
)


# A generic nudge against a semantic failure just buys the same failure again: the model
# did call the tool, and telling it otherwise is a description it cannot act on. When the
# schema rejected a well-formed answer, say which rule it broke.
def semantic_nudge(reason: str) -> str:
    return f"Your previous verdict was rejected: {reason}\nReturn the verdict again, corrected."


class ScoreState(TypedDict, total=False):
    system: str
    posting: dict[str, Any]
    # The deterministic extractor's read on this posting, already rendered. Built by the
    # worker, which owns the DB and the scorer; the graph must not open a connection.
    skill_hint: str
    rendered: str
    verdict: dict[str, Any] | None
    # Tokens summed over every attempt on this posting, not just the one that produced
    # the verdict. A retry is a billed call: keeping only the last attempt's counts made
    # the whole retry volume free in the run total and in `job_verdicts.cost_usd`.
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
        # `include_raw=True` makes this a dict at runtime; the stub only sees the
        # non-`include_raw` overload's BaseModel return type.
        result: dict[str, Any] = chain.invoke(messages)  # type: ignore[assignment]
    except Exception as exc:  # noqa: BLE001 - network, rate limit, 400
        logger.warning("Scoring call failed (attempt %d): %s", attempts, exc)
        return {"attempts": attempts, "error": str(exc), "verdict": None}

    raw = result.get("raw")
    parsed = result.get("parsed")
    parse_error = result.get("parsing_error")

    from careerradar.core.llm import no_tool_call_reason, token_usage

    usage = _add(state.get("usage"), token_usage(raw) if raw is not None else None)

    if parse_error is not None or parsed is None:
        # A cross-field validator in FitAssessment raising surfaces here too, not just a
        # malformed tool call: LangChain catches it and reports it as a parsing error.
        # Those are the semantic rejections, and they deserve a nudge that names the rule.
        #
        # When there is no parse error either, LangChain has nothing to say and this used
        # to log the bare string `None` -- 9 failures in one backlog run that named no
        # cause at all. `no_tool_call_reason` reads the response while it is still in
        # hand and separates prose-instead-of-a-tool-call from a truncation, which have
        # different fixes: retry versus a smaller ask.
        reason = parse_error if parse_error is not None else no_tool_call_reason(raw)

        # `no_tool_call_reason` only gets to speak when pydantic has nothing to say, so a
        # truncation that happened to leave valid JSON was reported as a pile of missing
        # required fields -- indistinguishable from a model that simply skipped them. That
        # was 36 of the 71 rejections in the last production run, and the two have
        # opposite fixes: a higher output ceiling, or a shorter ask. `finish_reason` is
        # the only thing that separates them, so it is read here regardless of who won the
        # race to explain the failure.
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
            "semantic": _is_semantic(parse_error),
        }

    verdict = parsed.model_dump()
    return {
        "attempts": attempts,
        "verdict": verdict,
        "usage": usage,
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


def _is_semantic(parse_error: Any) -> bool:
    """Did the model answer the schema and get rejected, or fail to answer at all?"""
    return "validation error" in str(parse_error).lower()


def _rule_from(parse_error: Any) -> str:
    """The rule a Pydantic error names, without the input dump around it.

    A model-level `ValueError` says the rule in one sentence, so that sentence is the
    whole answer. A field error does not: pydantic prints the path, the message, a repr
    of the input and a docs URL, and the previous version returned all four verbatim for
    anything that was not a `Value error, `. That went straight into the retry prompt,
    where 200 characters of `input_value={'role_summary': 'A Linux...` push the one line
    that matters out of the model's attention.

    Field errors keep `path: message`. The path is the load-bearing half -- knowing that
    `requirement_assessments.6.status` is wrong is what makes the message actionable.
    """
    text = str(parse_error)
    marker = "Value error, "
    if marker in text:
        # `.split(" [type=")` because the sentence drags the dump along on this path too:
        # `... is not actionable. [type=value_error, input_value={'role_summary': "Azure
        # c...research_worthy': False}, input_type=dict]`.
        return text.split(marker, 1)[1].split("\n")[0].split(" [type=")[0].strip()

    rules: list[str] = []
    field = ""
    for line in text.split("\n")[1:]:
        if line.startswith("    "):  # the "For further information visit ..." line
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
