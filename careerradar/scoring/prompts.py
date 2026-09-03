"""Prompt construction for the scoring agent.

The split between `build_system` and `render_posting` is the cost model, not an
organizational preference.

DeepSeek caches prompt prefixes automatically and bills a cache hit at 1/50th the input
rate. `build_system` produces the part that is byte-identical on every call in a run --
the rules plus the frozen profile prefix -- so it is paid for once and read from cache
thereafter. `render_posting` produces the part that differs.
"""

import hashlib
from typing import Any

MAX_DESCRIPTION_CHARS = 6000

# How many entries the taxonomy hint may carry.
MAX_HINT_MATCHED = 12
MAX_HINT_MISSING = 15


def build_rules(fit_threshold: int = 70) -> str:
    return f"""\
You assess whether a specific candidate is a strong fit for a job posting.

Your task is to answer a single question:
Given the candidate's years of experience, seniority, skills, project descriptions,
and personal summary, does this candidate fit into this job at {fit_threshold}% or higher match?

Evaluation guidelines:
- Threshold ({fit_threshold}%): Candidate meets core technical capabilities and foundational stack;
  secondary, niche, or proprietary tools can be learned on the job.
- Experience & Seniority: Treat required experience proportionally (meeting ~{fit_threshold}% of
  stated years is acceptable; relevant degrees offset years). Qualified candidates can reasonably
  step up to the next seniority tier when core skills match.
- Preferred Qualifications: "Preferred", "bonus", or "nice-to-have" skills are not disqualifiers.
  Never reject solely for missing preferred items.
- Company Affinity: Current employment or prior degrees/roles with the hiring organization is
  a strong positive fit signal.
- Interests vs. Dealbreakers: Candidate interests are preferences, not hard exclusions.
  Reject only for explicit dealbreakers or irreconcilable stack/clearance barriers.

Return a structured output:
1. fit: boolean (true if candidate fits the job at >= {fit_threshold}% match, false otherwise).
2. reason_type: string (a single lowercase word classifying the verdict reason for
   observability/filtering). Common reason types include:
   - "match" (strong fit meeting the core criteria)
   - "skills" (lacks key required technical skills or technologies)
   - "experience" (insufficient total years of experience)
   - "seniority" (seniority mismatch, e.g. too junior or too senior)
   - "domain" (different product domain or industry specialism)
   - "clearance" (requires security clearance or citizenship not possessed)
   - "location" (onsite/location or work authorization constraint violation)
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


def render_skill_hint(matched: list[str] | None = None, missing: list[str] | None = None) -> str:
    """The deterministic extractor's read on this posting, as a hint."""
    matched = list(matched or [])[:MAX_HINT_MATCHED]
    missing = list(missing or [])[:MAX_HINT_MISSING]
    if not matched and not missing:
        return ""
    lines = [
        (
            '<taxonomy_signal note="Regex extraction over a fixed skill list. '
            'The posting text is authoritative.">'
        )
    ]
    if matched:
        lines.append("candidate profile has: " + ", ".join(matched))
    if missing:
        lines.append("posting asks, not on profile: " + ", ".join(missing))
    lines.append("</taxonomy_signal>")
    return "\n".join(lines)


def format_matched(matched_skills: list[Any]) -> list[str]:
    """Format matched skill labels for prompt rendering."""
    if not matched_skills:
        return []
    if isinstance(matched_skills[0], tuple):
        return [str(label) for label, _ in matched_skills]
    return [str(s) for s in matched_skills]


def _facts(posting: dict[str, Any]) -> list[str]:
    facts = []
    if posting.get("seniority"):
        facts.append(f"seniority-guess: {posting['seniority']}")
    if posting.get("is_remote"):
        facts.append("remote: yes")
    if posting.get("salary_annual_usd"):
        facts.append(f"salary: ${int(posting['salary_annual_usd']):,}/yr")
    if posting.get("access"):
        facts.append(f"access: {posting['access']}")
    return facts


def render_posting(posting: dict[str, Any], *, skill_hint: str = "") -> str:
    """The volatile half. One posting, delimited as untrusted data."""
    description = (posting.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    facts = _facts(posting)
    facts_line = f"<facts>{'; '.join(facts)}</facts>\n" if facts else ""

    rendered = (
        "<posting>\n"
        f"<title>{posting.get('title') or ''}</title>\n"
        f"<company>{posting.get('company') or ''}</company>\n"
        f"<location>{posting.get('location') or ''}</location>\n"
        f"{facts_line}"
        f"<description>\n{description}\n</description>\n"
        "</posting>"
    )
    return f"{rendered}\n{skill_hint}" if skill_hint else rendered
