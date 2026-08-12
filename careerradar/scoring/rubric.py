"""The ordinal scales the scoring agent answers on, and the words that define them.

One module, because the anchors have to be *identical* everywhere they appear. The prompt
states them to the model, `scale.py` orders them, `audit.py` quotes them back in flags, and
`stats.py` labels its cross-tabs with them. Three copies of "adjacent" that drifted apart
would produce a model and a report that quietly disagree about what was measured.

Why ordinals instead of a 0-100 score. Measured across 5,511 stored verdicts, the model
emitted 53 distinct values but 99.84% of them agreed with the band the prompt's own table
assigned -- and inside `worth_applying`, the single value 62 was 45% of the band. It was
choosing a label and then decorating it with digits. Asking for the label directly costs
nothing and stops the digits from implying a precision that was never there.

The scales are ORDERED, best first. `scale.py` depends on that ordering; do not reorder a
tuple here without reading it.
"""

# Ordered best -> worst. Index is the rank, and `scale.py` uses it directly.
ELIGIBILITY = ("eligible", "conditional", "blocked")
ROLE_MATCH = ("same_role", "adjacent", "different_domain", "different_field")
CAPABILITY_MATCH = ("exceeds", "meets", "most_with_gaps", "major_gaps", "not_close")
SENIORITY_GAP = ("matched", "candidate_above", "candidate_below")
EVIDENCE_QUALITY = ("strong", "adequate", "thin")

# `seniority_gap` is ordered by how much it hurts, which is not the same as the natural
# reading order of the values. Being asked for more seniority than you have is the worse
# problem: it is the one that gets a resume filtered out before a human sees it. Being
# overqualified costs you the occasional "we worry you'd be bored", which is negotiable.

IMPORTANCE = ("must_have", "important", "nice_to_have")
REQUIREMENT_STATUS = ("met", "partial", "unmet")


ELIGIBILITY_ANCHORS = {
    "eligible": (
        "Nothing in the posting disqualifies this candidate. Judge against the profile's "
        "HARD CONSTRAINTS, not against what a generic applicant might lack."
    ),
    "conditional": (
        "Disqualifying today, but the candidate could remove it without changing who they "
        "are: a clearance they are eligible to be sponsored for, a relocation they said "
        "they would consider, a certification with a known path. Say which, and quote it."
    ),
    "blocked": (
        "A requirement the candidate cannot meet and cannot negotiate: work authorization "
        "they lack, a clearance they cannot obtain, a language they do not speak, an "
        "on-site requirement in a city they will not move to, or anything the profile "
        "lists as NON-NEGOTIABLE. Quote the phrase that makes it one."
    ),
}

ROLE_MATCH_ANCHORS = {
    "same_role": (
        "The same job on the same kind of product. Note that the product matters: QA for "
        "an autonomous robot and QA for a SaaS dashboard are NOT the same role, and neither "
        "are robot-UI frontend and marketing-site frontend."
    ),
    "adjacent": (
        "The same discipline on a different product or stack. The candidate would be "
        "productive within weeks, and their past work reads as directly relevant."
    ),
    "different_domain": (
        "The same broad field, a different specialism. Transferable in principle, but the "
        "candidate would be learning the domain and the job at once."
    ),
    "different_field": (
        "A different profession that happens to share vocabulary with the candidate's. "
        "Shared tool names are not shared work."
    ),
}

CAPABILITY_MATCH_ANCHORS = {
    "exceeds": (
        "Has done this job at a larger scale or a higher level than it is being asked for. "
        "Every must-have is evidenced and some are under-asks."
    ),
    "meets": "Could start on Monday. Every must-have is evidenced in the profile.",
    "most_with_gaps": (
        "Most must-haves are evidenced; one or two are genuinely learnable on the job. "
        "A missing framework is a gap here, not a failure."
    ),
    "major_gaps": (
        "Several must-haves have no evidence at all. The candidate would be learning the "
        "core of the job, not the edges of it."
    ),
    "not_close": "The candidate has not done this kind of work.",
}

SENIORITY_GAP_ANCHORS = {
    "matched": "The level asked for is the level the candidate is at.",
    "candidate_above": "The candidate is more senior than the posting is pitched at.",
    "candidate_below": "The posting asks for more seniority than the candidate has.",
}

EVIDENCE_QUALITY_ANCHORS = {
    "strong": (
        "The posting says enough about the actual work to judge fit with confidence."
    ),
    "adequate": "Enough to judge, with some inference about what the day-to-day is.",
    "thin": (
        "A boilerplate or near-empty posting. Say so rather than guessing -- a confident "
        "verdict on three sentences is worse than an admitted unknown."
    ),
}

IMPORTANCE_ANCHORS = {
    "must_have": "Stated as required. Without it the application does not get read.",
    "important": "Clearly weighted, but a strong candidate missing it would still be read.",
    "nice_to_have": "Listed. Job ads pad this section; most of what they list lives here.",
}

ANCHORS = {
    "eligibility": (ELIGIBILITY, ELIGIBILITY_ANCHORS),
    "role_match": (ROLE_MATCH, ROLE_MATCH_ANCHORS),
    "capability_match": (CAPABILITY_MATCH, CAPABILITY_MATCH_ANCHORS),
    "seniority_gap": (SENIORITY_GAP, SENIORITY_GAP_ANCHORS),
    "evidence_quality": (EVIDENCE_QUALITY, EVIDENCE_QUALITY_ANCHORS),
}

# The dimensions the model answers, in the order the prompt asks for them. `models.py`
# declares its fields in this order too, and a test pins the two together: function calling
# fills fields in declaration order, so this sequence *is* the reasoning order.
DIMENSIONS = ("eligibility", "role_match", "capability_match",
              "seniority_gap", "evidence_quality")


def render_scale(dimension: str, indent: str = "  ") -> str:
    """One dimension as prompt text: the values in order, each with its anchor."""
    values, anchors = ANCHORS[dimension]
    lines = [f"{dimension}:"]
    width = max(len(v) for v in values)
    for value in values:
        continuation = indent + " " * (width + 2)
        wrapped = _wrap(anchors[value], first_indent=continuation, indent=continuation)
        lines.append(f"{indent}{value:<{width}}  {wrapped.lstrip()}")
    return "\n".join(lines)


def render_all_scales() -> str:
    return "\n\n".join(render_scale(dimension) for dimension in DIMENSIONS)


def _wrap(text: str, *, first_indent: str, indent: str, width: int = 92) -> str:
    """Wrap without importing textwrap's surprises about existing whitespace."""
    words = text.split()
    lines, current = [], first_indent
    for word in words:
        candidate = f"{current} {word}" if current.strip() else f"{current}{word}"
        if len(candidate) > width and current.strip():
            lines.append(current)
            current = f"{indent}{word}"
        else:
            current = candidate
    if current.strip():
        lines.append(current)
    return "\n".join(lines)
