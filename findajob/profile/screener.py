"""Blind ATS / Recruiter screening engine for tailored resumes."""

from typing import Any

from findajob.core.llm import invoke_structured, structured_model
from findajob.core.logger import get_logger
from findajob.profile.models import ATSScreeningVerdict, TailoredResumePayload
from findajob.profile.prompts import (
    SCREENER_SYSTEM_PROMPT,
    render_job_context,
    render_resume_plaintext,
)

logger = get_logger()


def screen_resume(
    job: dict[str, Any],
    payload: TailoredResumePayload,
    model_name: str | None = None,
) -> ATSScreeningVerdict:
    """Evaluate a rendered resume strictly against the target job posting.

    The screener is provided ONLY the Job Description and the Plaintext Resume text.
    """
    job_ctx = render_job_context(job)
    resume_txt = render_resume_plaintext(payload)

    user_message = f"""=== TARGET JOB POSTING ===
{job_ctx}

=== SUBMITTED RESUME ===
{resume_txt}

Evaluate whether this resume meets the requirements to advance in the ATS / recruiter screen."""

    messages = [
        ("system", SCREENER_SYSTEM_PROMPT),
        ("user", user_message),
    ]

    model = structured_model(model=model_name, role="scoring")
    verdict: ATSScreeningVerdict = invoke_structured(
        model=model,
        schema=ATSScreeningVerdict,
        messages=messages,
        label="ats_screener",
    )
    logger.info(
        "ATS Screener verdict: passed=%s score=%d feedback=%s",
        verdict.passed,
        verdict.score,
        verdict.actionable_feedback[:100] if verdict.actionable_feedback else "None",
    )
    return verdict
