"""AI Copilot and resume parsing engine for the unified Profile.

Supports:
1. Parsing uploaded resume files (.pdf, .txt, .md).
2. LLM-based extraction into structured Profile.
3. Interactive Copilot chat to refine, question, rewrite, and update the Profile.
"""

import io

from pydantic import BaseModel, Field

from findajob.core.llm import invoke_structured, structured_model
from findajob.core.logger import get_logger
from findajob.profile.models import Profile

logger = get_logger()


def parse_resume_file(content: bytes, filename: str) -> str:
    """Extract raw text from PDF, TXT, or MD bytes."""
    fname_lower = filename.lower()
    if fname_lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages_text).strip()

    if fname_lower.endswith(".docx"):
        raise ValueError(
            "DOCX format is no longer supported. "
            "Please upload your resume as a PDF, TXT, or Markdown file."
        )

    # Plain text / Markdown
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


RESUME_EXTRACTION_SYSTEM = """\
You are an expert technical resume parser and career architect.
Your task is to extract a candidate's complete career history into a structured Profile.

Guidelines:
1. Extract personal and contact details (name, email, phone, location, links).
2. Write a clear 2-3 sentence elevator pitch / sales summary in executive_summary.
3. If specific positioning guidance or directives are evident, capture in model_guidance.
4. Extract education history (institution, degree, details).
5. Categorize skills into logical groups (e.g. Infrastructure, AI Systems, Frontend, Languages).
6. In Experience, capture roles, dates, locations, optional punchy subheadings (only when
   grouping distinct project scopes; omit for single-focus roles), and impact bullets.
7. In Projects, capture standalone open-source or personal projects with optional heading and URLs.
8. Identify work authorization/citizenship and location preferences in eligibility.
9. Identify any explicit non-negotiable dealbreakers in dealbreakers.
"""


def extract_profile_from_resume_text(
    resume_text: str,
    model_name: str | None = None,
    existing_profile: Profile | None = None,
) -> Profile:
    """Extract a Profile from raw resume text."""
    model = structured_model(model=model_name, role="agent")

    user_parts = [
        "Please parse and extract the candidate profile from the following resume text:",
        f"```text\n{resume_text[:25000]}\n```",
    ]
    if existing_profile and existing_profile.name:
        user_parts.append(
            f"\nExisting Master Profile for context:\n{existing_profile.model_dump_json(indent=2)}"
        )

    messages = [
        ("system", RESUME_EXTRACTION_SYSTEM),
        ("user", "\n\n".join(user_parts)),
    ]

    profile: Profile = invoke_structured(
        model=model,
        schema=Profile,
        messages=messages,
        label="resume_extraction_copilot",
    )
    logger.info(
        "Extracted profile for '%s': %d skill categories, %d roles, %d projects",
        profile.name,
        len(profile.skills),
        len(profile.experience),
        len(profile.projects),
    )
    return profile


class CopilotChatResult(BaseModel):
    """Result of an interactive copilot consultation turn."""

    reply: str = Field(
        default="",
        description="Clear, helpful markdown response answering user questions or suggestions.",
    )
    response_markdown: str = Field(
        default="",
        description="Clear, helpful markdown response answering user questions or suggestions.",
    )
    updated_profile: Profile = Field(
        description="The candidate's updated profile incorporating any approved changes."
    )
    changes_made: list[str] = Field(
        default_factory=list,
        description="Summary list of specific changes or additions made to the profile.",
    )


COPILOT_CHAT_SYSTEM = """\
You are an expert Career Copilot and executive resume coach.
You are helping the user refine, polish, and structure their Master Profile.

You have access to:
1. The candidate's current Profile schema.
2. The raw resume context if provided.

Instructions:
1. When asked to add or edit information, update the Profile schema accordingly.
2. If asked for feedback or suggestions, analyze the candidate's achievements and positioning.
3. In `model_guidance`, store high-level directives that future resume runs should honor.
4. In `executive_summary`, refine the short elevator pitch (2-3 sentences max).
5. In `skills`, organize into crisp categories.
6. In `dealbreakers`, capture hard non-negotiable rejection criteria.
7. Keep bullet points punchy, impact-focused, and quantified where possible.
8. Provide actionable, concise markdown responses.
"""


def chat_with_copilot(
    messages: list[dict[str, str]],
    current_profile: Profile,
    resume_text: str | None = None,
    model_name: str | None = None,
) -> CopilotChatResult:
    """Run an interactive turn with the Profile Copilot."""
    model = structured_model(model=model_name, role="agent")

    context_blocks = [
        "=== CURRENT CANDIDATE PROFILE ===",
        current_profile.model_dump_json(indent=2),
    ]
    if resume_text:
        context_blocks.append(f"\n=== UPLOADED RESUME TEXT CONTEXT ===\n{resume_text[:10000]}")

    formatted_messages: list[tuple[str, str]] = [
        ("system", COPILOT_CHAT_SYSTEM),
        ("user", "\n".join(context_blocks)),
    ]

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        role_tag = role if role in ("system", "user", "assistant") else "user"
        formatted_messages.append((role_tag, content))

    result: CopilotChatResult = invoke_structured(
        model=model,
        schema=CopilotChatResult,
        messages=formatted_messages,
        label="profile_copilot_chat",
    )
    if not result.reply and result.response_markdown:
        result.reply = result.response_markdown
    elif not result.response_markdown and result.reply:
        result.response_markdown = result.reply
    return result
