"""AI Copilot and resume parsing engine for the unified Master Profile.

Supports:
1. Parsing uploaded resume files (.pdf, .docx, .txt, .md).
2. LLM-based extraction into structured ResumeMasterProfile.
3. Interactive Copilot chat to refine, question, rewrite, and update the Master Profile.
"""

import io

from pydantic import BaseModel, Field

from careerradar.core.llm import DEFAULT_AGENT_MODEL, invoke_structured, structured_model
from careerradar.core.logger import get_logger
from careerradar.resumes.models import ResumeMasterProfile

logger = get_logger()


def parse_resume_file(content: bytes, filename: str) -> str:
    """Extract raw text from PDF, DOCX, TXT, or MD bytes."""
    fname_lower = filename.lower()
    if fname_lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(content))
        pages_text = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages_text).strip()

    if fname_lower.endswith(".docx"):
        import docx

        doc = docx.Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                row_text = " | ".join(cell.text.strip() for cell in row.cells if cell.text.strip())
                if row_text:
                    paragraphs.append(row_text)
        return "\n\n".join(paragraphs).strip()

    # Plain text / Markdown
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("latin-1", errors="replace")


RESUME_EXTRACTION_SYSTEM = """\
You are an expert technical resume parser and career architect.
Your task is to extract a candidate's complete career history into a structured Master Profile.

Guidelines:
1. Extract personal and contact details (name, email, phone, location, links).
2. Write a clear 2-3 sentence executive positioning summary in summary_guidance.
3. Extract education history (institution, degree, graduation/details).
4. Categorize skills into logical groups (e.g. Infrastructure, AI Systems, Frontend, Languages).
5. In Experience, capture all work roles, dates, locations, project scopes, and impact bullets.
6. Identify work authorization/citizenship and location preferences if indicated in the text.
7. Extract core technical strengths and honest gaps/limitations for calibrated job matching.
"""


def extract_profile_from_resume_text(
    resume_text: str,
    model_name: str = DEFAULT_AGENT_MODEL,
    existing_profile: ResumeMasterProfile | None = None,
) -> ResumeMasterProfile:
    """Extract a ResumeMasterProfile from raw resume text."""
    model = structured_model(model=model_name)

    user_parts = [
        "=== UPLOADED RESUME DOCUMENT ===",
        resume_text,
    ]
    if existing_profile and existing_profile.name:
        user_parts.append(
            f"\n=== EXISTING PROFILE (PRESERVE/MERGE VALUABLE DETAILS) ===\n"
            f"{existing_profile.model_dump_json(indent=2)}"
        )

    user_parts.append(
        "\nExtract and construct the comprehensive Master Profile from this resume text."
    )

    messages = [
        ("system", RESUME_EXTRACTION_SYSTEM),
        ("user", "\n".join(user_parts)),
    ]

    extracted: ResumeMasterProfile = invoke_structured(
        model=model,
        schema=ResumeMasterProfile,
        messages=messages,
        label="resume_extractor",
    )
    return extracted


class CopilotChatResult(BaseModel):
    reply: str = Field(
        description="Conversational response in markdown (guidance, explanations, questions)."
    )
    updated_profile: ResumeMasterProfile = Field(
        description="The updated ResumeMasterProfile incorporating requested modifications."
    )
    changes_made: list[str] = Field(
        default_factory=list,
        description="Summary list of specific changes applied to the profile.",
    )


COPILOT_CHAT_SYSTEM = """\
You are CareerRadar's Master Profile Copilot — a technical career advisor and profile strategist.
You collaborate with the user to perfect their Master Profile for scoring and resume generation.

The Master Profile is the single source of truth for:
- 1-page tailored resume generation (contact info, work history, projects, skills).
- Job fit scoring (experience, skills, work eligibility, target roles, dealbreakers, gaps).

Your responsibilities:
1. Answer questions about the candidate's profile, positioning, and market readiness.
2. When asked to add, remove, or modify sections, apply changes directly to `updated_profile`.
3. If an uploaded resume is provided, analyze it and suggest impactful improvements.
4. Keep bullet points punchy, impact-focused, and quantified where possible.
5. Provide actionable, concise markdown responses.
"""


def chat_with_copilot(
    messages: list[dict[str, str]],
    current_profile: ResumeMasterProfile,
    resume_text: str | None = None,
    model_name: str = DEFAULT_AGENT_MODEL,
) -> CopilotChatResult:
    """Run an interactive turn with the Profile Copilot."""
    model = structured_model(model=model_name)

    context_blocks = [
        "=== CURRENT MASTER PROFILE ===",
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
    return result
