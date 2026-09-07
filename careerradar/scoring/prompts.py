"""Prompt construction for the smart scoring agent.

The split between `build_system` and `render_posting` is the cost model:
DeepSeek and other LLM providers cache prompt prefixes automatically.
`build_system` produces the part that is byte-identical on every call in a run --
the rules plus the frozen candidate profile prefix. `render_posting` produces the
per-job posting content without keyword/taxonomy diff anchoring.
"""

import hashlib
from typing import Any

MAX_DESCRIPTION_CHARS = 6000


def build_rules(fit_threshold: int = 70) -> str:
    return f"""\
You assess whether a specific candidate is a strong fit for a job posting.

Your task is to evaluate holistic candidate-job fit:
Given the candidate's years of experience, seniority, categorized skills, project accomplishments,
work history, eligibility constraints, and non-negotiable dealbreakers, does this candidate fit
this role at {fit_threshold}% or higher match?

Evaluation guidelines:
- Fit Threshold ({fit_threshold}%): Candidate meets core technical capabilities and stack;
  secondary, niche, or proprietary tools can be learned on the job.
- Experience & Seniority: Treat required experience proportionally (meeting ~{fit_threshold}% of
  stated years is acceptable; relevant advanced degrees offset years). Qualified candidates can
  reasonably step up to the next seniority tier when core skills match.
- Preferred Qualifications: "Preferred", "bonus", or "nice-to-have" skills are not disqualifiers.
  Never reject solely for missing preferred items.
- Company Affinity: Current employment or prior degrees/roles with the hiring organization is
  a strong positive fit signal.
- Dealbreakers & Eligibility: Candidate dealbreakers and work authorization are hard vetoes.
  If the posting explicitly mandates a condition that violates candidate dealbreakers
  (e.g. 24/7 on-call, security clearance candidate lacks, unworkable onsite location),
  reject with fit=false.

Return a structured output:
1. fit: boolean (true if candidate fits at >= {fit_threshold}% match with no dealbreaker broken).
2. reason_type: string (a single lowercase word classifying the verdict reason for
   observability/filtering). Common reason types include:
   - "match" (strong fit meeting the core criteria)
   - "skills" (lacks key required technical skills or technologies)
   - "experience" (insufficient total years of experience)
   - "seniority" (seniority mismatch, e.g. too junior or too senior)
   - "domain" (different product domain or industry specialism)
   - "clearance" (requires security clearance or citizenship not possessed)
   - "location" (onsite/location or work authorization constraint violation)
   - "dealbreaker" (triggers explicit candidate dealbreaker)
   - "compensation" (salary range below candidate comp floor)
   - "tech_stack" (stack fundamentally different from candidate's focus)
   - "overqualified" (candidate significantly overqualified for junior role)
3. reason_description: string (a concise 1-2 sentence explanation of why the candidate fits
   or does not fit, useful for the dashboard).
"""


RULES = build_rules(70)


def build_system(profile_summary: str, fit_threshold: int = 70) -> str:
    """The cached half. Identical for every posting scored against one profile."""
    return f"{build_rules(fit_threshold)}\n{profile_summary}"


def prompt_hash(profile_summary: str = "", fit_threshold: int = 70) -> str:
    """Identifies the rules a verdict was produced under."""
    return hashlib.sha256((build_rules(fit_threshold) + profile_summary).encode()).hexdigest()[:16]


def _facts(posting: dict[str, Any]) -> list[str]:
    facts = []
    if posting.get("is_remote"):
        facts.append("remote: yes")
    if posting.get("salary_annual_usd"):
        facts.append(f"salary: ${int(posting['salary_annual_usd']):,}/yr")
    return facts


def render_posting(posting: dict[str, Any], **_kwargs: Any) -> str:
    """The volatile half. One posting, delimited as untrusted data."""
    description = (posting.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    facts = _facts(posting)
    facts_line = f"<facts>{'; '.join(facts)}</facts>\n" if facts else ""

    return (
        "<posting>\n"
        f"<title>{posting.get('title') or ''}</title>\n"
        f"<company>{posting.get('company') or ''}</company>\n"
        f"<location>{posting.get('location') or ''}</location>\n"
        f"{facts_line}"
        f"<description>\n{description}\n</description>\n"
        "</posting>"
    )
