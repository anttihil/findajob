"""The `careerradar profile` wizard.

Terminal-first because the interview is a once-a-year task with a natural conversational
shape, and because LangGraph's interrupt/resume maps directly onto "print a question, read
a line, hand it back". The graph itself is headless -- a web adapter would drive the same
`stream`/`Command(resume=...)` loop, and nothing in `graph.py` knows about a terminal.
"""

import json
import sys
import textwrap

from careerradar.core.config import load_config
from careerradar.core.llm import DEFAULT_AGENT_MODEL, MissingApiKey
from careerradar.core.logger import get_logger

logger = get_logger()

THREAD_ID = "profile-build"

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


def _supports_colour():
    return sys.stdout.isatty()


def _style(text, code):
    return f"{code}{text}{RESET}" if _supports_colour() else text


def _wrap(text, indent="  "):
    return "\n".join(
        textwrap.fill(line, width=88, initial_indent=indent, subsequent_indent=indent)
        if line.strip() else ""
        for line in str(text).split("\n")
    )


def _render_profile(profile: dict) -> str:
    out = []
    out.append(_style("BIO", BOLD))
    out.append(_wrap(profile.get("bio", "")))
    facts = []
    if profile.get("years_experience") is not None:
        facts.append(f"{profile['years_experience']:g} years")
    if profile.get("seniority"):
        facts.append(profile["seniority"])
    if facts:
        out.append(_wrap(" · ".join(facts)))

    skills = sorted(profile.get("skills", []), key=lambda s: (-s["level"], s["key"]))
    for level, heading in ((3, "STRONG"), (2, "WORKING"), (1, "FAMILIAR")):
        group = [s["label"] for s in skills if s["level"] == level]
        if group:
            out.append("")
            out.append(_style(f"{heading} ({len(group)})", BOLD))
            out.append(_wrap(", ".join(group)))

    for key, heading in (
        ("strengths", "STRENGTHS"),
        ("weaknesses", "HONEST GAPS"),
        ("non_negotiables", "NON-NEGOTIABLE"),
        ("red_flags", "RED FLAGS"),
    ):
        values = profile.get(key) or []
        if values:
            out.append("")
            out.append(_style(heading, BOLD))
            out.extend(_wrap(f"- {v}") for v in values)

    constraints = profile.get("constraints") or {}
    if any(constraints.values()):
        out.append("")
        out.append(_style("CONSTRAINTS", BOLD))
        for field, value in constraints.items():
            if value in (None, [], ""):
                continue
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            out.append(_wrap(f"- {field.replace('_', ' ')}: {value}"))

    preferences = profile.get("preferences") or {}
    if any(preferences.values()):
        out.append("")
        out.append(_style("PREFERENCES", BOLD))
        for field, value in preferences.items():
            if value in (None, [], ""):
                continue
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value)
            out.append(_wrap(f"- {field.replace('_', ' ')}: {value}"))

    return "\n".join(out)


def _ask_multiline(prompt):
    """Read one answer. A blank line submits; the user can type '?' to skip."""
    print(prompt, end="", flush=True)
    try:
        return input()
    except EOFError:
        return ""


def cmd_build(args):
    from langgraph.types import Command

    from careerradar.profile.graph import build_graph, open_checkpointer
    from careerradar.profile.ingest import CorpusError, collect_documents
    from careerradar.profile.models import Profile
    from careerradar.profile.store import corpus_changed, save_profile

    config = load_config()
    profile_config = config.get("profile") or {}
    model = profile_config.get("model", DEFAULT_AGENT_MODEL)

    try:
        documents = collect_documents()
    except CorpusError as exc:
        print(f"{exc}")
        return 1
    if not documents:
        print("The corpus is empty. List your documents under `profile.corpus` in "
              "config.yaml.")
        return 1

    changed = corpus_changed(documents)
    if changed is False and not (args.force or args.resume):
        print("An active profile already exists and the corpus has not changed since it "
              "was built.")
        print("Rebuild anyway with:  careerradar profile build --force")
        return 0

    checkpointer = open_checkpointer()
    graph = build_graph(checkpointer=checkpointer)
    thread = {"configurable": {"thread_id": THREAD_ID}}

    if not args.resume:
        # Start clean. Without this, a *finished* build leaves a checkpoint whose graph has
        # already reached END, so the next `build` resumes a completed run, produces no
        # work, and re-saves the previous draft -- looking like it rebuilt when it did not.
        # `--resume` is the only way to continue an interrupted interview.
        checkpointer.delete_thread(THREAD_ID)

    print()
    print(_style("Building your profile", BOLD))
    # List the corpus, do not just count it. The corpus is "whatever is in resumes/", so a
    # stale or unwanted document is otherwise invisible until it has already shaped the
    # profile -- and every score downstream.
    for document in documents:
        print(_wrap(f"{document.kind:<16} {document.name}", indent="    "))
    print(_wrap(f"{len(documents)} document(s) · model {model}", indent="  "))
    print()

    max_questions = 0 if getattr(args, "no_interview", False) else profile_config.get(
        "max_interview_questions", 15
    )
    if max_questions == 0:
        print(_wrap("Skipping the interview: building from the documents alone. "
                    "The documents cannot state compensation, work authorization, or what "
                    "you would refuse, so those constraints will be empty until you run "
                    "the interview.", indent="  "))
        print()

    payload = {"model": model, "max_questions": max_questions}

    resume_value = None
    try:
        while True:
            stream_input = (
                Command(resume=resume_value) if resume_value is not None else payload
            )
            interrupted = None
            for chunk in graph.stream(stream_input, thread, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    interrupted = chunk["__interrupt__"][0].value
            payload = None
            resume_value = None

            if interrupted is None:
                break

            if interrupted["kind"] == "question":
                index = interrupted["index"] + 1
                total = interrupted["total"]
                print(_style(f"[{index}/{total}] {interrupted['topic']}", BOLD))
                print(_wrap(interrupted["question"]))
                print(_style(_wrap(f"why: {interrupted['why']}"), DIM))
                answer = _ask_multiline("\n  > ")
                print()
                resume_value = answer

            elif interrupted["kind"] == "review":
                print()
                print(_style("=" * 88, DIM))
                print(_render_profile(interrupted["profile"]))
                print(_style("=" * 88, DIM))
                print()
                print(_wrap("Press enter to approve, or describe what to change.", indent="  "))
                reply = _ask_multiline("\n  > ").strip()
                print()
                if not reply:
                    resume_value = {"approve": True}
                else:
                    print(_wrap("Revising…", indent="  "))
                    print()
                    resume_value = {"approve": False, "revision": reply}
    except KeyboardInterrupt:
        print()
        print(_wrap("Stopped. Your answers are checkpointed -- resume with:", indent="  "))
        print(_wrap("careerradar profile build --resume", indent="    "))
        return 130
    except MissingApiKey as exc:
        print(f"\n{exc}")
        return 1

    final = graph.get_state(thread).values
    if not final.get("approved"):
        print(_wrap("Profile was not approved; nothing saved.", indent="  "))
        return 1

    profile = Profile.model_validate(final["draft"])
    version = save_profile(
        profile, model=model, documents=documents, turns=final.get("turns", [])
    )
    print(_style(f"Saved profile v{version} (active).", BOLD))
    print(_wrap(f"{len(profile.skills)} skills · {len(final.get('turns', []))} "
                f"interview answers", indent="  "))
    print()
    print(_wrap("Next:  careerradar score run --dry-run", indent="  "))
    return 0


def cmd_show(args):
    from careerradar.profile.store import load_active_row

    record = load_active_row()
    if record is None:
        print("No active profile. Build one with:  careerradar profile build")
        return 1
    print(_style(f"Profile v{record['version']}  ({record['created_at']}, {record['model']})", BOLD))
    print()
    print(_render_profile(record["profile"]))
    print()
    print(_style("PROMPT PREFIX (cached on every scoring call)", BOLD))
    print(_style(_wrap(f"{len(record['summary_text'])} chars", indent="  "), DIM))
    return 0


def cmd_history(args):
    from careerradar.profile.store import list_versions

    versions = list_versions()
    if not versions:
        print("No profiles yet.")
        return 1
    print(f"{'ver':>4}  {'active':^6}  {'created':<26} {'model':<18} {'docs':>4} {'turns':>5}")
    for row in versions:
        print(f"{row['version']:>4}  {'  *   ' if row['is_active'] else '      '}  "
              f"{row['created_at'][:25]:<26} {(row['model'] or '-'):<18} "
              f"{row['documents']:>4} {row['turns']:>5}")
    return 0


def run_profile_command(args):
    return {"build": cmd_build, "show": cmd_show, "history": cmd_history}[
        args.subcommand
    ](args)
