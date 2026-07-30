"""Deterministic job-fit scoring.

Stage 1 of a two-stage design. This runs on every posting, costs nothing, and is fully
explainable -- every score decomposes into named components the UI can show. Stage 2 (an
optional Claude reranker over the top N) lives in backend/llm_scorer.py and is off by
default.

The previous scorer summed +25 per resume skill appearing in the title and +5 per skill in
the body, capped at 100, and called the result a percent. Three problems: it compared raw
resume strings against raw description text (so "AWS (EC2, S3)" matched nothing), a posting
listing many technologies scored higher than one that actually fit, and the number had no
interpretation. Scores here are a weighted mean of four bounded components, so 0-100 means
something consistent.
"""

import math
import re
from collections import Counter

from backend.profile import LEVEL_CLAIMED, LEVEL_MENTIONED, LEVEL_STRONG

DEFAULT_WEIGHTS = {
    "skill_coverage": 0.45,
    "bm25": 0.25,
    "title_family": 0.20,
    "seniority_fit": 0.10,
}

# How much each profile evidence level contributes when a required skill is matched. A skill
# named once in prose should not count as fully as one used across current projects.
LEVEL_CREDIT = {
    LEVEL_STRONG: 1.0,
    LEVEL_CLAIMED: 0.85,
    LEVEL_MENTIONED: 0.55,
}

# A skill named in the TITLE is a hard requirement in a way a body mention is not, so both
# its contribution and its cost when missing are amplified.
TITLE_SKILL_MULTIPLIER = 2.5

# Shrinkage for skill coverage, in pseudo-skills. Without it, a posting with two recognized
# skills that the user happens to have scores the same as one with twenty, and a posting with
# no recognized skills at all scores a perfect 1.0.
COVERAGE_PRIOR_WEIGHT = 4.0
# What an arbitrary posting's coverage would be absent any evidence.
COVERAGE_PRIOR = 0.25

# The user has ~4 years of engineering experience plus prior teaching. mid/senior postings
# fit; staff/principal is a stretch; intern is a mismatch.
SENIORITY_FIT = {
    "mid": 1.00,
    "senior": 0.95,
    "unspecified": 0.80,
    "lead": 0.60,
    "junior": 0.55,
    "staff": 0.40,
    "intern": 0.05,
}

BM25_K1 = 1.5
BM25_B = 0.75
# Saturation point for the BM25 -> 0..1 mapping. BM25 is unbounded, so it needs a squash to
# sit in a weighted mean alongside three bounded components.
BM25_SATURATION = 12.0

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.\-]*")

_STOPWORDS = frozenset("""
a an and are as at be been but by for from has have if in into is it its of on or that the
to was were will with you your we our us they their this these those he she them i me my
will would can could should may might must shall about after all also am any because before
being below between both during each few further here how more most no nor not now once only
other over own same so some such than then there through too under until up very what when
where which while who whom why
role job work team company position candidate experience years apply applicant opportunity
""".split())


def tokenize(text):
    if not text:
        return []
    return [
        token for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS and len(token) > 1
    ]


class Bm25Index:
    """Minimal BM25 over a corpus of job descriptions.

    IDF needs document frequencies, so the index is built from whatever descriptions are
    already stored. With an empty corpus it degrades to a uniform IDF, which reduces BM25 to
    length-normalized term overlap -- weaker but still meaningful, and it means a cold
    database does not crash or silently score everything zero.
    """

    def __init__(self, documents=None):
        self.doc_count = 0
        self.doc_freq = Counter()
        self.avg_length = 1.0
        if documents:
            self.fit(documents)

    def fit(self, documents):
        lengths = []
        for document in documents:
            tokens = tokenize(document)
            if not tokens:
                continue
            lengths.append(len(tokens))
            self.doc_count += 1
            for token in set(tokens):
                self.doc_freq[token] += 1
        if lengths:
            self.avg_length = sum(lengths) / len(lengths)
        return self

    def idf(self, term):
        if self.doc_count == 0:
            return 1.0
        freq = self.doc_freq.get(term, 0)
        # Robertson-Sparck-Jones, floored so common terms cannot go negative.
        return max(
            0.05,
            math.log((self.doc_count - freq + 0.5) / (freq + 0.5) + 1.0),
        )

    def score(self, query_tokens, document_tokens):
        if not query_tokens or not document_tokens:
            return 0.0
        counts = Counter(document_tokens)
        length = len(document_tokens)
        total = 0.0
        for term in set(query_tokens):
            freq = counts.get(term, 0)
            if not freq:
                continue
            denominator = freq + BM25_K1 * (
                1 - BM25_B + BM25_B * length / max(self.avg_length, 1.0)
            )
            total += self.idf(term) * (freq * (BM25_K1 + 1)) / denominator
        return total


def squash(value, saturation):
    """Map an unbounded non-negative score into [0, 1)."""
    if value <= 0:
        return 0.0
    return value / (value + saturation)


def skill_coverage(required, profile):
    """Fraction of a posting's required skills the user can evidence, shrunk toward a prior.

    Coverage, not count: a posting listing 30 technologies of which the user has 10 is a
    worse fit than one listing 6 of which they have 5. The old scorer rewarded the former.

    The shrinkage matters as much as the ratio. A raw ratio treats "2 skills detected, both
    matched" as identical to "20 detected, all 20 matched", and treats "no skills detected"
    as perfect coverage. A live dry run scored a *Stationary Engineer* boiler-operator
    posting at 83 for exactly that reason: zero recognized skills gave coverage 1.00.

    So the ratio is smoothed with COVERAGE_PRIOR_WEIGHT pseudo-skills at COVERAGE_PRIOR:
    an empty posting lands near the prior rather than at the top, and confident coverage
    requires a real denominator.
    """
    matched = []
    missing = []
    earned = 0.0
    possible = 0.0

    for skill, info in (required or {}).items():
        weight = TITLE_SKILL_MULTIPLIER if info.get("in_title") else 1.0
        possible += weight
        level = profile.level(skill)
        if level > 0:
            earned += weight * LEVEL_CREDIT.get(level, 0.5)
            matched.append(skill)
        else:
            missing.append(skill)

    coverage = (
        (earned + COVERAGE_PRIOR_WEIGHT * COVERAGE_PRIOR)
        / (possible + COVERAGE_PRIOR_WEIGHT)
    )
    return coverage, matched, missing


def title_family_fit(role_family, roles, profile):
    """Whether the posting's role family is one the user has a resume for."""
    if not role_family:
        return 0.0
    resume = roles.resume_for(role_family)
    if not resume:
        return 0.2
    if resume not in profile.variants:
        return 0.4
    # Core families are the user's stated targets; adjacent and breadth are pivots.
    return {"core": 1.0, "adjacent": 0.75, "breadth": 0.5}.get(
        roles.tier(role_family), 0.5
    )


def seniority_fit(seniority):
    return SENIORITY_FIT.get(seniority or "unspecified", 0.7)


class JobScorer:
    """Scores postings against the user profile. Deterministic and explainable."""

    def __init__(self, profile, roles, taxonomy, weights=None, bm25=None):
        self.profile = profile
        self.roles = roles
        self.taxonomy = taxonomy
        self.weights = dict(DEFAULT_WEIGHTS)
        if weights:
            self.weights.update(
                {k: v for k, v in weights.items() if k in DEFAULT_WEIGHTS}
            )
        self.bm25 = bm25 or Bm25Index()
        self._profile_tokens = None

    def profile_tokens(self):
        """The user's profile rendered as a BM25 query.

        Skill labels are repeated by evidence level, so a strongly-evidenced skill weighs
        more in the term overlap than one merely claimed.
        """
        if self._profile_tokens is None:
            parts = []
            for key in self.profile.keys():
                label = self.taxonomy.label(key)
                repeats = {LEVEL_STRONG: 3, LEVEL_CLAIMED: 2}.get(
                    self.profile.level(key), 1
                )
                parts.extend([label] * repeats)
            self._profile_tokens = tokenize(" ".join(parts))
        return self._profile_tokens

    def score(self, posting):
        """Score one posting. Returns a dict with the total and every component.

        `posting` needs title, description, role_family, seniority, and optionally a
        pre-extracted `skills` map; skills are extracted here if absent.
        """
        title = posting.get("title") or ""
        description = posting.get("description") or ""

        required = posting.get("skills")
        if required is None:
            required = self.taxonomy.extract(description, title=title)

        coverage, matched, missing = skill_coverage(required, self.profile)
        raw_bm25 = self.bm25.score(
            self.profile_tokens(), tokenize(f"{title} {description}")
        )
        components = {
            "skill_coverage": coverage,
            "bm25": squash(raw_bm25, BM25_SATURATION),
            "title_family": title_family_fit(
                posting.get("role_family"), self.roles, self.profile
            ),
            "seniority_fit": seniority_fit(posting.get("seniority")),
        }

        total = sum(components[name] * weight
                    for name, weight in self.weights.items())
        total = max(0.0, min(1.0, total))

        return {
            "score": int(round(total * 100)),
            "components": components,
            "weights": dict(self.weights),
            "matched_skills": sorted(matched),
            "missing_skills": sorted(missing),
            "required_count": len(required),
            "bm25_raw": round(raw_bm25, 3),
            "resume_match": self.roles.resume_for(posting.get("role_family")),
        }

    def score_many(self, postings):
        return [self.score(posting) for posting in postings]


def build_index_from_db(db, limit=2000):
    """Build a BM25 index from stored descriptions.

    Only full descriptions contribute: snippets would skew both the average document length
    and the document frequencies.
    """
    rows = db.conn.execute(
        "SELECT description FROM jobs "
        "WHERE description IS NOT NULL AND description_quality = 'full' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return Bm25Index([row[0] for row in rows])
