"""The profile-building graph: documents in, reviewed profile out.

    ingest -> extract -> gaps -> [ ask <-> record ] -> synthesize -> review -> persist
                                      ^                                 |
                                      +---------- revise ---------------+

Why a graph rather than a script: the interview is a human-in-the-loop conversation that
can span days. LangGraph's `interrupt()` plus a SQLite checkpointer means the wizard can be
closed at question 4 and resumed at question 4 a week later, with the extracted claims and
the answers so far intact -- no bespoke state file, no re-reading the corpus, no re-paying
for the extraction call.

One question per node invocation rather than a loop inside one node: `interrupt()` re-runs
its node from the top on resume, so a multi-interrupt node would re-execute its own body
once per answered question. A cursor in the state and a conditional edge keeps each resume
to exactly one turn of work.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from careerradar.core.llm import (
    DEFAULT_AGENT_MODEL,
    invoke_structured,
    structured_model,
)
from careerradar.core.logger import get_logger
from careerradar.core.paths import GRAPH_DB_PATH
from careerradar.profile.ingest import collect_documents, render_corpus
from careerradar.profile.models import ExtractedClaims, GapQuestions, Profile

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.graph.state import CompiledStateGraph

logger = get_logger()

EXTRACT_SYSTEM = """\
You are building a factual profile of a job candidate from their own career documents.

The documents are the candidate's own and are TRUSTED.

Read all of them together. They are several tailored versions of one career plus an
achievements log, so the same work appears more than once, described differently for
different audiences. Reconcile them into one picture rather than summing them.

Assign a skill level of 3 only where the documents show substantial shipped work -- an
achievements entry with real volume, a named project with the technology at its centre. A
skill that merely appears in a competencies list is 2. A skill mentioned once in passing is 1.
Recording an honest 2 is far more useful than an optimistic 3: every downstream score
depends on this being calibrated.

Record contradictions rather than resolving them silently -- if one resume implies a
seniority another does not support, say so. Those become interview questions."""

GAPS_SYSTEM = """\
You are preparing to interview a job candidate to finish their profile.

You have their documents and the claims extracted from them. Your task is to identify what
the documents genuinely CANNOT tell you, and turn that into questions worth a person's time.

Career documents are a sales artifact. They systematically omit: honest weaknesses, why
they left, compensation expectations, what they would refuse, which of the listed skills
they actually enjoy, and where a title overstates or understates the real work.

Ask about those. Do NOT ask anything the documents already answer -- a question whose
answer is in the corpus wastes the interview and signals you did not read it.

Ask about one skill or one topic per question. A question naming three skills gets one
thin sentence covering all three, and a yes/no framing ("is that just curiosity, or is
there work I'm missing?") gets a yes -- neither carries enough to correct a skill level.
Where the documents list a skill without evidence, ask what they built with it, where,
and how much of it was theirs.

Order the questions by how much the answer would change how a job posting gets scored.
Ask about hard constraints (work authorization, location, compensation floor) before
preferences. Each question must be answerable in a sentence or two."""

SYNTHESIZE_SYSTEM = """\
You are writing the final profile for a job candidate.

You have their documents, the claims extracted from those documents, and their own answers
to an interview. The interview answers are the candidate speaking about themselves: where
they contradict the documents, the answers win.

Skill levels are re-derived here, not inherited. The claims were levelled from the
documents alone, so their levels record what the candidate had room to write down -- not
what they have done. Read every answer for skill evidence and re-level against it:

  A skill they describe using on real work is at least 2, whatever the documents showed.
  If they describe building or owning something substantial with it, it is 3.

  Personal projects are real work. "I used it in several personal projects, including X"
  is a 2, not a 1.

  A level stays at 1 only when the candidate puts it there themselves -- studied it,
  tried it, never shipped with it.

An answer that names a skill and leaves its level unchanged is the specific failure this
step exists to prevent. The interview is the only place a skill the resume had no room to
justify can be corrected; if the levels come back matching the extracted claims, the
interview was wasted. When an answer moves a level, replace that skill's evidence with
what the candidate said, so the level and its justification agree.

This profile is the sole basis on which thousands of job postings will be scored. Two
failure modes to avoid, in order of cost:

  Flattery. A profile listing only strengths produces a scorer that calls everything a
  strong match, which is the same as having no scorer. Record the weaknesses the candidate
  admitted, in their terms.

  Vagueness. "Strong engineering skills" cannot discriminate between postings. "Ships
  production Python services and owns their deployment" can. Be specific enough that the
  difference between two similar postings is visible.

Carry every hard constraint through exactly as stated -- work authorization, location,
compensation floor. Those turn into hard blockers, and a softened constraint means the
candidate reads postings they cannot accept."""


def _merge_turns(existing: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return (existing or []) + (new or [])


class ProfileState(TypedDict, total=False):
    corpus: str
    documents: list[dict[str, Any]]
    claims: dict[str, Any] | None
    questions: list[dict[str, Any]]
    cursor: int
    turns: Annotated[list[dict[str, Any]], _merge_turns]
    draft: dict[str, Any] | None
    revision: str | None
    approved: bool
    model: str
    max_questions: int


# --- nodes ---------------------------------------------------------------------------


def node_ingest(state: ProfileState) -> dict[str, Any]:  # noqa: ARG001 - langgraph node signature
    documents = collect_documents()
    if not documents:
        raise RuntimeError(
            "No corpus documents found. Populate resumes/ (scripts/sync_corpus.sh) first."
        )
    logger.info("Profile: ingested %d documents", len(documents))
    return {
        "corpus": render_corpus(documents),
        "documents": [
            {"path": d.path, "kind": d.kind, "sha256": d.sha256, "chars": len(d.text)}
            for d in documents
        ],
    }


def node_extract(state: ProfileState) -> dict[str, Any]:
    model = structured_model(state.get("model", DEFAULT_AGENT_MODEL))
    claims = invoke_structured(
        model,
        ExtractedClaims,
        [("system", EXTRACT_SYSTEM), ("user", state.get("corpus", ""))],
        label="Profile extract",
    )
    logger.info(
        "Profile: extracted %d skills, %d contradictions",
        len(claims.skills),
        len(claims.contradictions),
    )
    return {"claims": claims.model_dump()}


def node_gaps(state: ProfileState) -> dict[str, Any]:
    model = structured_model(state.get("model", DEFAULT_AGENT_MODEL))
    claims = ExtractedClaims.model_validate(state.get("claims"))
    limit = state.get("max_questions", 15)
    result = invoke_structured(
        model,
        GapQuestions,
        [
            ("system", GAPS_SYSTEM),
            (
                "user",
                (
                    f"{state.get('corpus', '')}\n\n"
                    f"<extracted_claims>\n{claims.model_dump_json(indent=2)}\n</extracted_claims>\n\n"
                    f"Produce at most {limit} questions."
                ),
            ),
        ],
        label="Profile gaps",
    )
    questions = [q.model_dump() for q in result.questions][:limit]
    logger.info("Profile: %d interview questions", len(questions))
    return {"questions": questions, "cursor": 0}


def node_ask(state: ProfileState) -> dict[str, Any]:
    """Surface one question to the human and suspend until it is answered."""
    cursor = state.get("cursor", 0)
    questions = state.get("questions", [])
    question = questions[cursor]

    answer = interrupt(
        {
            "kind": "question",
            "index": cursor,
            "total": len(questions),
            "topic": question["topic"],
            "question": question["question"],
            "why": question["why"],
        }
    )

    return {
        "cursor": cursor + 1,
        "turns": [
            {
                "topic": question["topic"],
                "question": question["question"],
                "answer": (answer or "").strip(),
            }
        ],
    }


def node_synthesize(state: ProfileState) -> dict[str, Any]:
    model = structured_model(state.get("model", DEFAULT_AGENT_MODEL))

    transcript = (
        "\n\n".join(
            f"Q ({t['topic']}): {t['question']}\nA: {t['answer']}"
            for t in state.get("turns", [])
            if t.get("answer")
        )
        or "(no interview answers)"
    )

    claims = ExtractedClaims.model_validate(state.get("claims"))
    instruction = ""
    revision = state.get("revision")
    if revision:
        # A revision re-runs synthesis with the human's correction appended, rather than
        # patching the draft. Patching a structured object from free text is a second
        # extraction problem; regenerating with the correction in context is one.
        instruction = (
            f"\n\n<requested_changes>\n{revision}\n</requested_changes>\n"
            "Apply these changes. Keep everything else as it was."
        )

    profile = invoke_structured(
        model,
        Profile,
        [
            ("system", SYNTHESIZE_SYSTEM),
            (
                "user",
                (
                    f"{state.get('corpus', '')}\n\n"
                    f"<extracted_claims>\n{claims.model_dump_json(indent=2)}\n</extracted_claims>\n\n"
                    f"<interview>\n{transcript}\n</interview>{instruction}"
                ),
            ),
        ],
        label="Profile synthesize",
    )
    logger.info("Profile: synthesized draft with %d skills", len(profile.skills))
    return {"draft": profile.model_dump(), "revision": None}


def level_changes(claims: dict[str, Any] | None, draft: dict[str, Any]) -> dict[str, Any]:
    """What the interview did to the skill levels the documents alone produced.

    Computed here rather than in the terminal wizard because it is the reviewer's main
    question -- did answering fifteen questions change anything? -- and every adapter has
    to answer it. A silent no is the failure mode worth surfacing: a synthesis that echoes
    the extracted levels back looks like a finished profile, and the reviewer approves an
    interview that was never applied.
    """
    before = {s["key"]: s for s in ((claims or {}).get("skills") or [])}
    raised: list[dict[str, Any]] = []
    lowered: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    for skill in draft.get("skills") or []:
        previous = before.get(skill["key"])
        if previous is None:
            added.append({"key": skill["key"], "label": skill["label"], "to": skill["level"]})
        elif skill["level"] > previous["level"]:
            raised.append(
                {
                    "key": skill["key"],
                    "label": skill["label"],
                    "from": previous["level"],
                    "to": skill["level"],
                }
            )
        elif skill["level"] < previous["level"]:
            lowered.append(
                {
                    "key": skill["key"],
                    "label": skill["label"],
                    "from": previous["level"],
                    "to": skill["level"],
                }
            )
    return {
        "raised": raised,
        "lowered": lowered,
        "added": added,
        "total": len(draft.get("skills") or []),
    }


def node_review(state: ProfileState) -> dict[str, Any]:
    """Show the draft and wait. Only a human decision leaves this node."""
    draft = state.get("draft") or {}
    decision = interrupt(
        {
            "kind": "review",
            "profile": draft,
            "level_changes": level_changes(state.get("claims"), draft),
        }
    )
    if isinstance(decision, dict):
        if decision.get("approve"):
            return {"approved": True}
        return {"approved": False, "revision": decision.get("revision")}
    if isinstance(decision, str) and decision.strip().lower() in ("approve", "y", "yes"):
        return {"approved": True}
    return {"approved": False, "revision": decision}


# --- edges ---------------------------------------------------------------------------


def route_after_ask(state: ProfileState) -> str:
    if state.get("cursor", 0) >= len(state.get("questions", [])):
        return "synthesize"
    return "ask"


def route_after_gaps(state: ProfileState) -> str:
    return "synthesize" if not state.get("questions") else "ask"


def route_after_review(state: ProfileState) -> str:
    return "__end__" if state.get("approved") else "synthesize"


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(ProfileState)
    builder.add_node("ingest", node_ingest)
    builder.add_node("extract", node_extract)
    builder.add_node("gaps", node_gaps)
    builder.add_node("ask", node_ask)
    builder.add_node("synthesize", node_synthesize)
    builder.add_node("review", node_review)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "extract")
    builder.add_edge("extract", "gaps")
    builder.add_conditional_edges(
        "gaps", route_after_gaps, {"ask": "ask", "synthesize": "synthesize"}
    )
    builder.add_conditional_edges(
        "ask", route_after_ask, {"ask": "ask", "synthesize": "synthesize"}
    )
    builder.add_edge("synthesize", "review")
    builder.add_conditional_edges(
        "review", route_after_review, {"synthesize": "synthesize", "__end__": END}
    )
    return builder.compile(checkpointer=checkpointer)


def open_checkpointer():
    """A SqliteSaver on its own database file.

    Separate from jobs.db on purpose: the interview writes a checkpoint per turn, and that
    write pattern has no business sharing a WAL with a 20-minute scrape.
    """
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(GRAPH_DB_PATH, check_same_thread=False)
    return SqliteSaver(conn)


# Imported late so `interrupt` resolves against the installed langgraph without making this
# module unimportable when it is absent (the test suite imports the models beside it).
from langgraph.types import interrupt  # noqa: E402
