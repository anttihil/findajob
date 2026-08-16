"""Checking a verdict against evidence that does not require a human.

This is the validation layer that works while the app is too young to have a golden set.
It rests on one observation: the two most expensive mistakes the scoring agent can make
are both objectively detectable from text already on hand.

A quote either is or is not in the posting. There is no judgement in that. Measured over
1,332 stored blockers, 88% were verifiable and 12% could not be found at all -- a real
error rate that nothing in the pipeline previously reported.

A hard blocker either does or does not demand something the profile says the candidate
already has. That is the worst error the agent can make: a fabricated blocker deletes an
opportunity silently, and the candidate never learns the posting existed. A wasted
application costs an afternoon; this costs a job you never saw.

Only one failure is fatal. A blocker that decided a non-`eligible` verdict and cannot be
located in the posting has nothing behind it, so the verdict is refused and retried.
Everything else is a flag, persisted to `job_verdicts.audit_flags` and counted in three
places: at the end of each `score run`, by `careerradar score stats`, which aggregates the
stored column, and -- for the two checks it re-derives -- by `careerradar score audit`.

Note what that last command does NOT cover. It re-runs the blocker quote check and the
profile contradiction check over stored verdicts with no API calls, which is what makes
those two rates watchable for free. It does not read `audit_flags`, so a flag raised here
and nowhere else -- `assessment_incomplete` is the one -- reaches the reader through
`score stats`, not `score audit`.
"""

import difflib
import re
import unicodedata
from typing import Any

from careerradar.profile.adapter import ProfileAdapter
from careerradar.profile.models import (
    Constraints,
    FitAssessment,
    Profile,
    normalize_requirement,
)
from careerradar.taxonomy.skills import Taxonomy

# Below this ratio a quote is not a garbled version of anything in the posting. 0.85
# accepts the usual damage -- a smart quote, a collapsed line break, a trimmed bullet --
# while rejecting a sentence the model composed itself.
FUZZY_THRESHOLD = 0.85

# A quote shorter than this cannot be matched meaningfully: "Go" appears in "Golang",
# "going" and "category". Short quotes are flagged rather than fuzzy-matched.
MIN_QUOTE_CHARS = 12

# Cap on how many places one shared word may anchor a candidate window. A quote full of
# common words would otherwise anchor everywhere and undo the prefilter; the distinctive
# words are what actually locate it.
MAX_ANCHORS_PER_WORD = 40

# How many of a blocker's candidate spans get the fuzzy treatment when none of them is an
# exact hit. Splitting on sentences and brackets can yield a dozen fragments from one
# blocker, and a full sweep costs about as much as all the others put together. Longest
# first, so this drops the fragments least likely to be carrying the requirement.
MAX_FUZZY_SPANS = 6

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = dict.fromkeys(map(ord, "‘’‚‛′"), "'")
_QUOTES.update(dict.fromkeys(map(ord, "“”„‟″"), '"'))


def normalize(text: str) -> str:
    """Fold the differences that are not the model's fault."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_DASHES).translate(_QUOTES)
    return re.sub(r"\s+", " ", text).strip().casefold()


_WORD = re.compile(r"\S+")


def _tokens(text: str) -> list[tuple[str, int, int]]:
    """`[(normalized_word, start, end), ...]` with offsets into the ORIGINAL string.

    Matching on words rather than characters is what keeps a repaired span readable. A
    fixed-width character window lands mid-word and returns things like "st hold an active
    TS/SCI clearance\n*", which is worse than the typo it replaced.
    """
    out: list[tuple[str, int, int]] = []
    for match in _WORD.finditer(text):
        word = normalize(match.group())
        if word:
            out.append((word, match.start(), match.end()))
    return out


def locate(quote: str, haystack: str) -> tuple[str, str | None]:
    """Find `quote` in `haystack`. Returns `(status, verbatim_span or None)`.

    Status is `verified`, `repaired`, `too_short` or `not_found`. A repair returns the span
    as it actually appears, so the stored blocker becomes something the reader can search
    for in the posting.
    """
    needle = normalize(quote)
    if not needle:
        return "not_found", None
    if needle in normalize(haystack):
        return "verified", None
    if len(needle) < MIN_QUOTE_CHARS:
        return "too_short", None

    words = needle.split()
    tokens = _tokens(haystack)
    if not words or not tokens:
        return "not_found", None

    # Only windows sharing a word with the quote are worth scoring. Sweeping every start
    # position made a 500-verdict pass take minutes: a 6,000-character description is ~900
    # tokens, and three window lengths over all of them is ~2,700 ratio() calls per quote.
    positions: dict[str, list[int]] = {}
    for index, (word, _s, _e) in enumerate(tokens):
        positions.setdefault(word, []).append(index)

    starts = set()
    for offset, word in enumerate(words):
        for index in positions.get(word, ())[:MAX_ANCHORS_PER_WORD]:
            starts.add(max(0, index - offset))
    if not starts:
        return "not_found", None

    matcher = difflib.SequenceMatcher(a=needle, autojunk=False)
    best_ratio: float = 0.0
    best_span: tuple[int, int] | None = None
    # Let the window breathe by one word either way: the model's paraphrase is usually the
    # same phrase with an article added or a bullet marker dropped.
    lengths = sorted({max(1, len(words) - 1), len(words), len(words) + 1})
    for start in sorted(starts):
        for length in lengths:
            window = tokens[start : start + length]
            if not window:
                continue
            candidate = " ".join(word for word, _s, _e in window)
            matcher.set_seq2(candidate)
            if matcher.real_quick_ratio() < best_ratio or matcher.quick_ratio() < best_ratio:
                continue
            ratio = matcher.ratio()
            if ratio > best_ratio:
                best_ratio, best_span = ratio, (window[0][1], window[-1][2])

    if best_ratio >= FUZZY_THRESHOLD and best_span is not None:
        return "repaired", haystack[best_span[0] : best_span[1]].strip() or None
    return "not_found", None


# --- false-blocker detection ------------------------------------------------------------
#
# The naive version of this check -- look for a profile constraint value anywhere in the
# blocker text -- has terrible precision, because the model routinely *explains* itself
# using the candidate's own attributes: "'Gode norsk- og engelskkunnskaper' -- candidate
# speaks English and Finnish, not Norwegian" is a correct blocker that names two things the
# candidate has. Over 500 stored verdicts it produced 21 hits of which roughly two were
# real.
#
# Two changes fix it. Match only inside the QUOTED requirement, never the surrounding
# prose. And for languages, fire only when EVERY language the requirement names is one the
# candidate speaks -- a posting wanting Norwegian and English is not a false blocker for
# someone who has only the English half.
#
# `HardBlocker.quote` now carries the requirement on its own, so callers pass that and the
# prose never arrives here in the first place. `quoted_spans` stays because verdicts
# written before the field was split still store the whole sentence in `quote`, and it is
# a no-op on text that has no quote marks in it.

_QUOTED = re.compile(r"['\"«»]([^'\"«»]{6,})['\"«»]")

# Language names as they appear in Nordic postings, which are frequently not in English.
# Scoped to what this corpus actually contains: Swedish blockers outnumber every other
# language four to one, and Finnish is the one the candidate actually speaks, so it is the
# one where a false blocker is possible.
LANGUAGE_SURFACES = {
    "finnish": (
        "finnish",
        "finska",
        "finsk",
        "suomi",
        "suomen",
        "suomea",
        "suomeksi",
        "finnische",
        "finnois",
    ),
    "english": ("english", "engelska", "engelsk", "englanti", "englannin", "englisch"),
    "swedish": ("swedish", "svenska", "svensk", "ruotsi", "ruotsin", "schwedisch"),
    "norwegian": ("norwegian", "norsk", "norska", "norja", "norjan"),
    "danish": ("danish", "dansk", "danska", "tanska", "tanskan"),
    "german": ("german", "deutsch", "tyska", "saksa", "saksan"),
    "french": ("french", "francais", "franska", "ranska"),
    "dutch": ("dutch", "nederlands", "hollanti"),
    "spanish": ("spanish", "espanol", "spanska"),
    "mandarin": ("mandarin", "chinese", "kiina"),
    "japanese": ("japanese", "japani"),
}

# Authorization surfaces, same idea. The candidate is a dual US/Finnish citizen, so a
# requirement naming only US or EU eligibility asks for nothing they lack.
AUTHORIZATION_SURFACES = {
    "united states": (
        "us citizen",
        "u.s. citizen",
        "us citizenship",
        "u.s. citizenship",
        "united states citizen",
        "authorized to work in the us",
        "authorized to work in the united states",
        "green card",
        "permanent resident",
        "us work authorization",
    ),
    "european union": (
        "eu citizen",
        "eu citizenship",
        "eu/eea",
        "eea citizen",
        "right to work in the eu",
        "eu work authorization",
        "authorised to work in the eu",
        "authorized to work in the eu",
    ),
}

# A clearance is a separate blocker that a work-authorization requirement often sits next
# to. If the quote names one, the blocker stands regardless of the authorization clause.
_OTHER_BLOCKER = re.compile(
    r"\b(clearance|cleared|ts/sci|sci\b|top secret|polygraph|poly\b|nppv|bpss|"
    r"security check|background investigation|no dual citizenship|"
    r"dual citizenship is not)\b"
)


# A language name alone is not a language requirement. "customer work in Finnish
# industry" names a market, not a skill, and firing on it teaches the reader to ignore the
# report -- the same failure mode as the false blocker it is meant to catch.
_LANGUAGE_CONTEXT = re.compile(
    r"(languag|fluen|proficien|speak|spoken|written|bilingual|native|"
    r"kiel|taito|sprak|spr\u00e5k|kunnskap|kunskap|sprachk)"
)

# "English Language Arts" is a school subject and "English Language Learner Authorization"
# is a teaching credential. Both carry a language name next to a language context word
# without asking anyone to speak anything.
_NOT_A_LANGUAGE_REQUIREMENT = re.compile(
    r"(language arts|language learner|language pathology|sign language)"
)


def quoted_spans(text: str) -> list[str]:
    """The quoted requirement(s) inside a blocker, or the whole thing if none are marked."""
    found = [match.group(1) for match in _QUOTED.finditer(text or "")]
    return found or [text or ""]


def _surfaces_in(text: str, table: dict[str, tuple[str, ...]]) -> set[str]:
    hit = set()
    for canonical, surfaces in table.items():
        if any(re.search(rf"\b{re.escape(s)}", text) for s in surfaces):
            hit.add(canonical)
    return hit


def _candidate_languages(constraints: Constraints) -> set[str]:
    spoken = set()
    for value in getattr(constraints, "languages", None) or []:
        spoken |= _surfaces_in(normalize(value), LANGUAGE_SURFACES)
    return spoken


def _candidate_authorizations(constraints: Constraints) -> set[str]:
    held = set()
    for value in getattr(constraints, "work_authorization", None) or []:
        text = normalize(value)
        if "united states" in text or re.search(r"\bus\b|\bu\.s\.", text):
            held.add("united states")
        # Any EU member state citizenship confers the right to work EU-wide, which is the
        # thing a posting is actually asking about.
        if any(country in text for country in _EU_MEMBERS):
            held.add("european union")
    return held


_EU_MEMBERS = (
    "austria",
    "belgium",
    "bulgaria",
    "croatia",
    "cyprus",
    "czech",
    "denmark",
    "estonia",
    "finland",
    "france",
    "germany",
    "greece",
    "hungary",
    "ireland",
    "italy",
    "latvia",
    "lithuania",
    "luxembourg",
    "malta",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "slovakia",
    "slovenia",
    "spain",
    "sweden",
    "european union",
    "eu",
)


def blocker_contradicts_profile(
    blocker: str, profile: "ProfileAdapter | Profile | None"
) -> list[dict[str, str]]:
    """Requirements in this blocker that the candidate demonstrably already satisfies.

    Conservative by construction. Every rule below fires only when the quoted requirement
    asks for nothing outside what the profile claims -- a partial overlap is not a
    contradiction, because the missing half is a genuine blocker.
    """
    constraints = getattr(getattr(profile, "profile", profile), "constraints", None)
    if constraints is None:
        return []

    spoken = _candidate_languages(constraints)
    held = _candidate_authorizations(constraints)
    hits: list[dict[str, str]] = []

    for span in quoted_spans(blocker):
        text = normalize(span)
        if _OTHER_BLOCKER.search(text):
            continue

        demanded = _surfaces_in(text, LANGUAGE_SURFACES)
        if (
            demanded
            and demanded <= spoken
            and _LANGUAGE_CONTEXT.search(text)
            and not _NOT_A_LANGUAGE_REQUIREMENT.search(text)
        ):
            hits.append(
                {
                    "field": "languages",
                    "value": ", ".join(sorted(demanded)),
                    "quote": span.strip()[:160],
                }
            )

        required = _surfaces_in(text, AUTHORIZATION_SURFACES)
        if required and required <= held:
            hits.append(
                {
                    "field": "work_authorization",
                    "value": ", ".join(sorted(required)),
                    "quote": span.strip()[:160],
                }
            )

    return hits


# The model separates a verbatim quote from its explanation with a dash about as often as
# it uses quote marks. Both halves are checked; only one has to be in the posting.
# The dash class must list the unicode variants explicitly: `normalize()` folds them to
# ASCII, but splitting happens on the raw string so the returned span stays verbatim.
#
# Dashes and colons were the whole list, and that was too narrow. The model punctuates the
# same two-part blocker with a full stop or a semicolon just as readily -- `Secret
# clearance is required. Candidate holds no clearance; as a US citizen they are eligible to
# be sponsored` was rejected three times and lost its verdict, on a posting whose
# requirements list literally reads `* Secret clearance`. Measured over the 33 blockers
# that lost a verdict in one run, 24 gain a sub-span from the additions below.
#
# The sentence rule requires an opening character after the stop, so `U.S. citizen` and
# `e.g. Kubernetes` are left whole; the bracket rule is what reaches the parenthetical
# aside, which is where a Nordic-language requirement usually sits.
#
# The ellipsis rule is first because it must beat the sentence rule to the same dots. The
# model abbreviates a long quote rather than copying it: `Amca is building America's new
# industrial base... deliver avionics, hydraulic, and electrical components`. Both halves
# are verbatim and the whole string is in no window, so the fuzzy sweep cannot reach 0.85
# against anything -- the elided middle is most of the text it is scored on. 13 of the 48
# fatal quote failures in one backlog run were this, and splitting on the ellipsis gives
# each half back as a candidate span that the cheap exact check already handles.
_SEPARATOR = re.compile(
    r"\s*(?:\.{3,}|\u2026)\s*"
    r"|\s+(?:--|[-\u2010\u2011\u2012\u2013\u2014\u2015\u2212]|:)\s+"
    r"|\s*;\s+"
    r"|(?<=[.!?])\s+(?=[\"'(\[A-Z\u00c0-\u00de])"
    r"|\s*[()\[\]]\s*"
)


def _candidate_spans(blocker: str) -> list[str]:
    spans = [span.strip() for span in _QUOTED.findall(blocker)]
    # The whole string stays a candidate even when it does split. Splitting is there to
    # rescue a quote buried in prose; a blocker that is verbatim *across* a sentence
    # boundary must not be broken up by the rule added for the ones that are not.
    for part in [blocker, *_SEPARATOR.split(blocker)]:
        part = (part or "").strip(" .,;\"'()")
        if len(part) >= MIN_QUOTE_CHARS:
            spans.append(part)
    # Longest first: the fullest match is the most convincing evidence, and a short leading
    # fragment can match by accident.
    return sorted({s for s in spans if s}, key=len, reverse=True)


def locate_blocker(blocker: str, haystack: str) -> tuple[str, str | None]:
    """Locate a blocker, which is usually prose wrapped around a quote.

    The model writes `Posting requires 'based within commuting distance of our hubs'
    (Boston); candidate is in Los Angeles` -- the quoted half is verbatim and the rest is
    explanation. Checking the whole string finds nothing, and because an unlocatable
    blocker on a non-eligible verdict is fatal, that cost one posting three retries and
    then its verdict entirely.

    So: if the blocker marks a quote, the quote is what must be in the posting. Only an
    unquoted blocker is checked whole.

    Quote marks are not the only separator. The model also writes
    `For denna position kravs svenskt medborgarskap - the candidate is a US and Finnish
    citizen` with the verbatim half first and an em dash before the explanation. Three
    correct blockers -- a Danish requirement, a Swedish citizenship requirement -- were
    rejected over exactly that punctuation, so dash-separated segments count as candidate
    quotes too.
    """
    spans = _candidate_spans(blocker or "")
    if not spans:
        return locate(blocker, haystack)

    # Exact containment across every span before any fuzzy work. `locate` re-normalizes and
    # re-tokenizes the whole description on each call, so running it per span made the pass
    # 2.6x slower once the separator list grew -- measured over 407 stored blockers, for a
    # verdict the cheap check already had on 94% of them.
    folded = normalize(haystack)
    for span in spans:
        needle = normalize(span)
        if needle and needle in folded:
            return "verified", None

    # Only the genuinely unlocatable reach the fuzzy sweep, and only the longest few: the
    # span that rescues a blocker is the one carrying the requirement, and a quote does not
    # hide in the shortest fragment of its own text.
    best: tuple[str, str | None] = ("not_found", None)
    for span in spans[:MAX_FUZZY_SPANS]:
        status, located = locate(span, haystack)
        if status == "verified":
            return status, None
        if status == "repaired" and best[0] != "verified":
            best = (status, located)
    return best


def audit(
    assessment: FitAssessment,
    *,
    posting: dict[str, Any],
    profile: "ProfileAdapter | Profile | None" = None,
    taxonomy: Taxonomy | None = None,  # noqa: ARG001
) -> tuple[FitAssessment, list[dict[str, Any]], str | None]:
    """Check one parsed assessment. Returns `(assessment, flags, fatal_reason)`.

    `assessment` is mutated where a quote can be repaired. `fatal_reason` is not None when
    the caller should discard the verdict and retry.
    """
    from careerradar.scoring.prompts import quotable_text

    # The model only ever saw the truncated description, so a quote must be checked against
    # what it was shown, not against the full row. `quotable_text` is that set, and it
    # lives next to `render_posting` so the two cannot drift apart again -- they had, over
    # the derived `<facts>` line.
    seen = quotable_text(posting)

    flags: list[dict[str, Any]] = []
    fatal: str | None = None
    decisive = assessment.eligibility != "eligible"

    # Only `quote` is checked. `why` is the model's argument for the blocker and is
    # expected to talk about the candidate -- running the quote check over it is what used
    # to reject correct verdicts whose reasoning cited a profile constraint.
    for blocker in assessment.hard_blockers:
        status, span = locate_blocker(blocker.quote, seen)
        if status == "repaired" and span:
            flags.append(
                {"flag": "quote_repaired", "where": "hard_blockers", "text": blocker.quote}
            )
            blocker.quote = span
        if status in ("not_found", "too_short"):
            flags.append(
                {"flag": f"quote_{status}", "where": "hard_blockers", "text": blocker.quote}
            )
            if decisive and fatal is None:
                fatal = (
                    f"hard_blocker quote not found in the posting: "
                    f"{blocker.quote[:120]!r}. A blocker that decides the verdict must "
                    "quote the posting verbatim -- the company name or the job title "
                    "counts, the reasoning in `why` does not."
                )
        if profile is not None:
            for hit in blocker_contradicts_profile(blocker.quote, profile):
                flags.append(
                    {
                        "flag": "blocker_contradicts_profile",
                        "where": "hard_blockers",
                        "text": blocker.quote[:200],
                        **hit,
                    }
                )

    for requirement in assessment.core_requirements:
        status, span = locate(requirement.quote, seen)
        if status == "repaired" and span:
            requirement.quote = span
            flags.append(
                {
                    "flag": "quote_repaired",
                    "where": "core_requirements",
                    "text": requirement.requirement,
                }
            )
        elif status in ("not_found", "too_short"):
            flags.append(
                {
                    "flag": f"quote_{status}",
                    "where": "core_requirements",
                    "text": requirement.requirement,
                }
            )

    assessed = {normalize_requirement(a.requirement) for a in assessment.requirement_assessments}
    for requirement in assessment.core_requirements:
        if normalize_requirement(requirement.requirement) not in assessed:
            # This used to say "must_have gaps already raise in the schema; these softer
            # ones degrade the explanation rather than the ordinals". The raise is gone --
            # it cost a full call to buy back a re-typed string -- so this flag now covers
            # both. `importance` is what separates them: a nice_to_have left unassessed is
            # a thin explanation, a must_have left unassessed is a verdict resting on a
            # requirement nobody checked, and a count that merges the two is not readable.
            flags.append(
                {
                    "flag": "assessment_incomplete",
                    "where": "requirement_assessments",
                    "importance": requirement.importance,
                    "text": requirement.requirement,
                }
            )

    # There was a `strength_for_unmentioned_skill` check here: a claimed strength naming
    # a skill the posting never mentions. It was removed after measuring it -- it fired
    # 1.4 times per posting on correct output, because a strength sentence properly cites
    # the candidate's own background ("300+ Jira tickets", "vLLM/Ollama serving") and that
    # background mentions skills the posting does not. It was detecting the model doing its
    # job. A flag that fires on correct behaviour teaches the reader to ignore the report,
    # which is the same failure it was meant to catch one level up.

    return assessment, flags, fatal


def summarize(flag_lists: list[list[dict[str, Any]]]) -> dict[str, int]:
    """Flag counts across many verdicts, for `careerradar score audit`."""
    counts: dict[str, int] = {}
    for flags in flag_lists:
        for flag in flags or []:
            counts[flag["flag"]] = counts.get(flag["flag"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
