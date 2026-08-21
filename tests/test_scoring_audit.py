"""Checking verdicts against evidence, with no labelled data.

Two objective questions: is the quote actually in the posting, and does the blocker demand
something the profile says the candidate already has. Both are decidable from text on
hand, which is what makes them usable while the app is too young for a golden set.

The precision bar here is unusually high, and the tests are shaped around it. A false
"this blocker is fabricated" trains the reader to ignore the report -- the same failure the
report exists to catch, one level up. So most of what follows checks that the auditor stays
quiet when it should.
"""

import os
import sys
import unittest
from typing import Any, ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.profile.models import (
    Constraints,
    Profile,
)
from careerradar.scoring.audit import (
    AuditAssessment,
    audit,
    blocker_contradicts_profile,
    locate,
    locate_blocker,
)

POSTING = {
    "id": 1,
    "title": "Platform Engineer",
    "company": "Acme",
    "location": "Boston, MA",
    "description": (
        "We are hiring a Platform Engineer.\n\n"
        "Requirements:\n"
        "  * 5+ years of production Python experience\n"
        "  * Must hold an active TS/SCI clearance with polygraph\n"
        "  * Comfortable with  both   Swedish and English as working languages\n"
        "  * This role requires you to be based within commuting distance of our hubs\n"
    ),
}


def profile(**overrides: Any) -> Profile:
    fields: dict[str, Any] = {
        "work_authorization": ["United States (citizen)", "Finland (citizen)"],
        "languages": ["English", "Finnish"],
        "locations": ["Los Angeles, CA", "remote"],
    }
    fields.update(overrides)
    return Profile(bio="test", constraints=Constraints(**fields))


def assessment(**overrides: Any) -> AuditAssessment:
    fields: dict[str, Any] = {
        "role_summary": "Platform engineering for a hosting product.",
        "core_requirements": [
            {
                "requirement": "Python",
                "importance": "must_have",
                "quote": "5+ years of production Python experience",
            }
        ],
        "requirement_assessments": [
            {"requirement": "Python", "status": "met", "candidate_evidence": "FastAPI services"}
        ],
        "eligibility": "eligible",
        "role_match": "adjacent",
        "capability_match": "meets",
        "seniority_gap": "matched",
        "evidence_quality": "strong",
        "hard_blockers": [],
        "key_gaps": [],
        "strengths": [],
        "reasoning": "Fine.",
        "research_worthy": False,
    }
    fields.update(overrides)
    return AuditAssessment(**fields)


class LocateTests(unittest.TestCase):
    def test_an_exact_quote_verifies(self) -> None:
        self.assertEqual(
            locate("Must hold an active TS/SCI clearance with polygraph", POSTING["description"])[
                0
            ],
            "verified",
        )

    def test_case_and_collapsed_whitespace_are_not_the_models_fault(self) -> None:
        self.assertEqual(
            locate(
                "comfortable with both SWEDISH and English as working languages",
                POSTING["description"],
            )[0],
            "verified",
        )

    def test_a_typo_is_repaired_to_the_span_as_it_appears(self) -> None:
        status, span = locate(
            "Must hold an active TS/SCI clearence with poligraph", POSTING["description"]
        )
        self.assertEqual(status, "repaired")
        self.assertEqual(span, "Must hold an active TS/SCI clearance with polygraph")

    def test_a_repaired_span_is_word_aligned(self) -> None:
        """A character window returns things like 'st hold an active...' -- worse than the typo."""
        _status, span = locate(
            "Must hold an active TS/SCI clearence with poligraph", POSTING["description"]
        )
        assert span is not None
        self.assertFalse(span.startswith("st "))
        self.assertNotIn("\n", span)

    def test_an_invented_requirement_is_not_found(self) -> None:
        self.assertEqual(
            locate("Requires ten years of Rust and a PhD in metallurgy", POSTING["description"])[0],
            "not_found",
        )

    def test_a_short_quote_is_flagged_rather_than_fuzzy_matched(self) -> None:
        """'Go' would match 'Golang', 'going' and 'category'."""
        self.assertEqual(locate("Rust", POSTING["description"])[0], "too_short")

    def test_an_empty_quote_is_not_found_rather_than_matching_everything(self) -> None:
        self.assertEqual(locate("", POSTING["description"])[0], "not_found")


class BlockerQuoteTests(unittest.TestCase):
    """The model wraps its quote in prose. Three correct blockers were lost to this."""

    def test_a_quote_inside_explanatory_prose_is_found(self) -> None:
        blocker = (
            "Posting requires 'based within commuting distance of our hubs' "
            "(Boston); candidate is in Los Angeles"
        )
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "verified")

    def test_a_dash_separated_quote_is_found(self) -> None:
        blocker = (
            "Must hold an active TS/SCI clearance with polygraph — the candidate "
            "has no clearance and cannot obtain one"
        )
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "verified")

    def test_prose_around_an_invented_quote_is_still_not_found(self) -> None:
        blocker = "Posting requires 'a PhD in metallurgy' — the candidate has none"
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "not_found")

    def test_a_quote_ended_by_a_full_stop_is_found(self) -> None:
        """Dashes and colons were the whole separator list, and this shape is as common.

        The real one cost a correct verdict three retries on a posting whose requirements
        list reads `* Secret clearance`.
        """
        blocker = (
            "Must hold an active TS/SCI clearance with polygraph. Candidate holds "
            "no clearance and is not eligible to be sponsored for one."
        )
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "verified")

    def test_a_semicolon_separated_quote_is_found(self) -> None:
        blocker = "Must hold an active TS/SCI clearance with polygraph; the candidate has none"
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "verified")

    def test_a_parenthetical_aside_after_the_quote_is_found(self) -> None:
        blocker = (
            "Must hold an active TS/SCI clearance with polygraph (candidate is a "
            "dual US/Finnish citizen)"
        )
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "verified")

    def test_an_abbreviation_does_not_split_the_quote(self) -> None:
        """`U.S.` and `e.g.` end in a full stop without ending a sentence."""
        from careerradar.scoring.audit import _SEPARATOR

        self.assertEqual(
            _SEPARATOR.split("Must be a U.S. citizen with clearance"),
            ["Must be a U.S. citizen with clearance"],
        )

    def test_a_quote_spanning_a_full_stop_is_still_matched_whole(self) -> None:
        """Splitting is there to rescue buried quotes, not to break up verbatim ones."""
        posting = dict(POSTING)
        posting["description"] = "You must relocate. Boston only, no exceptions."
        blocker = "You must relocate. Boston only, no exceptions."
        self.assertEqual(locate_blocker(blocker, posting["description"])[0], "verified")

    def test_reasoning_alone_still_finds_nothing_after_the_split(self) -> None:
        blocker = (
            "This is an entry-level building maintenance role, not a software "
            "engineering position. The candidate is a software engineer."
        )
        self.assertEqual(locate_blocker(blocker, POSTING["description"])[0], "not_found")


class ContradictionTests(unittest.TestCase):
    """Fire only when the requirement asks for nothing the candidate lacks."""

    def test_a_language_the_candidate_speaks_is_a_false_blocker(self) -> None:
        hits = blocker_contradicts_profile(
            "Requires 'fluent written and spoken Finnish' for customer work", profile()
        )
        self.assertEqual([h["field"] for h in hits], ["languages"])

    def test_a_requirement_naming_one_language_they_have_and_one_they_lack_is_real(self) -> None:
        """Norwegian plus English is not satisfied by having only the English half."""
        self.assertEqual(
            blocker_contradicts_profile(
                "Requires 'Gode norsk- og engelskkunnskaper' — candidate speaks English only",
                profile(),
            ),
            [],
        )

    def test_the_models_own_explanation_does_not_trigger_a_hit(self) -> None:
        """It cites the candidate's attributes to justify a correct blocker."""
        self.assertEqual(
            blocker_contradicts_profile(
                "'Fluent Danish required' — the candidate speaks English and Finnish, not Danish",
                profile(),
            ),
            [],
        )

    def test_us_citizenship_is_not_a_blocker_for_a_us_citizen(self) -> None:
        hits = blocker_contradicts_profile("'Must be a U.S. Citizen'", profile())
        self.assertEqual([h["field"] for h in hits], ["work_authorization"])

    def test_citizenship_plus_a_clearance_is_still_a_real_blocker(self) -> None:
        self.assertEqual(
            blocker_contradicts_profile(
                "'Candidate must be a U.S. Citizen with active TS/SCI clearance'", profile()
            ),
            [],
        )

    def test_a_language_name_that_is_not_a_language_requirement_is_ignored(self) -> None:
        """'customer work in Finnish industry' names a market, not a skill."""
        self.assertEqual(
            blocker_contradicts_profile(
                "'Experience in customer work in Finnish industry'", profile()
            ),
            [],
        )

    def test_a_profile_without_constraints_produces_no_hits(self) -> None:
        self.assertEqual(blocker_contradicts_profile("'Must be a U.S. Citizen'", None), [])


class AuditTests(unittest.TestCase):
    def test_an_unverifiable_blocker_that_decided_the_verdict_is_fatal(self) -> None:
        _a, _flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=["Requires a PhD in metallurgy from a top-10 school"],
            ),
            posting=POSTING,
        )
        self.assertIsNotNone(fatal)

    def test_the_same_blocker_on_an_eligible_verdict_is_only_flagged(self) -> None:
        """It did not decide anything, so discarding a whole verdict over it is waste."""
        _a, flags, fatal = audit(
            assessment(hard_blockers=["Requires a PhD in metallurgy from a top-10 school"]),
            posting=POSTING,
        )
        self.assertIsNone(fatal)
        self.assertIn("quote_not_found", [f["flag"] for f in flags])

    def test_a_verifiable_blocker_passes(self) -> None:
        _a, _flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=["Must hold an active TS/SCI clearance with polygraph"],
            ),
            posting=POSTING,
        )
        self.assertIsNone(fatal)

    def test_a_repaired_quote_replaces_the_stored_text(self) -> None:
        result, flags, _fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=["Must hold an active TS/SCI clearence with poligraph"],
            ),
            posting=POSTING,
        )
        self.assertIn("quote_repaired", [f["flag"] for f in flags])
        self.assertEqual(
            [b.quote for b in result.hard_blockers],
            ["Must hold an active TS/SCI clearance with polygraph"],
        )

    def test_reasoning_about_the_candidate_does_not_have_to_be_in_the_posting(self) -> None:
        """The blocker that used to lose verdicts: correct, and unquotable as prose.

        A defense-contractor blocker is decided by who the employer is and by a constraint
        that lives in the profile. Neither is a phrase in the requirements list, so when
        the two halves shared one string the whole verdict was discarded. With the quote
        naming the company, the same judgement survives.
        """
        _a, flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=[
                    {
                        "quote": "Acme",
                        "why": (
                            "Defense contractor; the candidate's NON-NEGOTIABLE constraints "
                            "exclude military technology."
                        ),
                    }
                ],
            ),
            posting=POSTING,
        )
        self.assertIsNone(fatal)
        self.assertEqual(flags, [])

    def test_an_invented_quote_is_still_fatal_however_good_the_reasoning(self) -> None:
        _a, _flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=[
                    {
                        "quote": "Requires a PhD in metallurgy from a top-10 school",
                        "why": "The candidate has a bachelor's degree.",
                    }
                ],
            ),
            posting=POSTING,
        )
        self.assertIsNotNone(fatal)

    def test_a_blocker_stored_as_a_plain_string_is_read_as_a_quote(self) -> None:
        """Verdicts written before the field was split still parse."""
        result = assessment(hard_blockers=["Must hold an active TS/SCI clearance with polygraph"])
        self.assertEqual(
            result.hard_blockers[0].quote, "Must hold an active TS/SCI clearance with polygraph"
        )
        self.assertEqual(result.hard_blockers[0].why, "")

    def test_a_quote_beyond_the_truncation_point_is_not_held_against_the_model(self) -> None:
        """The model only ever saw the first MAX_DESCRIPTION_CHARS."""
        from careerradar.scoring.prompts import MAX_DESCRIPTION_CHARS

        far = dict(POSTING)
        far["description"] = ("x " * MAX_DESCRIPTION_CHARS) + " a unique closing phrase"
        _a, flags, _f = audit(
            assessment(hard_blockers=["a unique closing phrase here"]), posting=far
        )
        self.assertIn("quote_not_found", [f["flag"] for f in flags])

    def test_a_contradicted_blocker_is_flagged_with_the_field_it_contradicts(self) -> None:
        _a, flags, _fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=[
                    (
                        "'Comfortable with both Swedish and English as working "
                        "languages' — the candidate would need Swedish"
                    )
                ],
            ),
            posting=POSTING,
            profile=profile(languages=["English", "Swedish"]),
        )
        contradictions = [f for f in flags if f["flag"] == "blocker_contradicts_profile"]
        self.assertTrue(contradictions)
        self.assertEqual(contradictions[0]["field"], "languages")

    def test_a_clean_verdict_produces_no_flags(self) -> None:
        _a, flags, fatal = audit(assessment(), posting=POSTING, profile=profile())
        self.assertEqual(flags, [])
        self.assertIsNone(fatal)


if __name__ == "__main__":
    unittest.main()


class QuotableTextTests(unittest.TestCase):
    """The auditor must read exactly what the model was shown, `<facts>` included.

    `render_posting` derives a facts line -- seniority guess, remote, a formatted salary,
    access -- and shows it inside `<posting>`. The auditor built its haystack from title,
    company, location and description only, so a blocker quoting the salary was verbatim
    to the model and unlocatable to the auditor, and the posting lost its verdict.
    """

    PAID: ClassVar[dict[str, Any]] = {**POSTING, "salary_annual_usd": 61680, "is_remote": True}

    def test_the_two_sides_agree_on_what_the_model_saw(self) -> None:
        from careerradar.scoring.prompts import quotable_text, render_posting

        rendered = render_posting(self.PAID)
        for line in quotable_text(self.PAID).split("\n"):
            if line:
                self.assertIn(line, rendered)

    def test_a_blocker_quoting_the_derived_salary_is_locatable(self) -> None:
        _a, _flags, fatal = audit(
            assessment(eligibility="blocked", hard_blockers=["salary: $61,680/yr"]),
            posting=self.PAID,
        )
        self.assertIsNone(fatal)

    def test_the_taxonomy_hint_is_not_quotable(self) -> None:
        """It sits outside `<posting>` because it is our regex output, not scraped text.

        A blocker whose only evidence is our own skill label has cited nothing.
        """
        from careerradar.scoring.prompts import quotable_text

        self.assertNotIn("taxonomy_signal", quotable_text(self.PAID))

    def test_a_posting_with_no_facts_is_unchanged(self) -> None:
        from careerradar.scoring.prompts import quotable_text

        self.assertIn("Must hold an active TS/SCI clearance", quotable_text(POSTING))


class ElidedQuoteTests(unittest.TestCase):
    """The model abbreviates a long quote instead of copying it.

    Both halves are verbatim; the elided middle is most of what the fuzzy sweep would be
    scored on, so no window reaches the threshold. 13 of 48 fatal quote failures in one
    backlog run were this shape.
    """

    def test_an_elided_blocker_is_located_by_its_halves(self) -> None:
        """Lowercase after the ellipsis, which is the shape the log actually shows.

        `Amca is building America's new industrial base... deliver avionics`. An uppercase
        continuation was already split by the sentence rule, so it proves nothing here.
        """
        _a, _flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=[
                    "We are hiring a Platform Engineer... years of production Python experience"
                ],
            ),
            posting=POSTING,
        )
        self.assertIsNone(fatal)

    def test_a_unicode_ellipsis_works_the_same_way(self) -> None:
        status, _span = locate_blocker(
            "We are hiring a Platform Engineer… with polygraph", POSTING["description"]
        )
        self.assertEqual(status, "verified")

    def test_eliding_does_not_make_an_invented_blocker_locatable(self) -> None:
        """Splitting must not turn two fabrications into a pass."""
        _a, _flags, fatal = audit(
            assessment(
                eligibility="blocked",
                hard_blockers=["Requires a PhD in metallurgy... and a commercial pilot licence"],
            ),
            posting=POSTING,
        )
        self.assertIsNotNone(fatal)


class AssessmentFlagTests(unittest.TestCase):
    """An unassessed must_have is recorded here now, not refused in the schema."""

    def test_an_unassessed_must_have_is_flagged_with_its_importance(self) -> None:
        _a, flags, fatal = audit(assessment(requirement_assessments=[]), posting=POSTING)
        self.assertIsNone(fatal)
        incomplete = [f for f in flags if f["flag"] == "assessment_incomplete"]
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(incomplete[0]["importance"], "must_have")
        self.assertEqual(incomplete[0]["text"], "Python")

    def test_a_requirement_that_drifted_by_punctuation_is_not_flagged(self) -> None:
        """The join folds what is not a difference of meaning, so this is assessed."""
        _a, flags, _fatal = audit(
            assessment(
                requirement_assessments=[
                    {"requirement": "  python ", "status": "met", "candidate_evidence": "x"}
                ]
            ),
            posting=POSTING,
        )
        self.assertEqual([f for f in flags if f["flag"] == "assessment_incomplete"], [])


class MarkdownQuoteTests(unittest.TestCase):
    """The boards hand over Markdown; the model quotes the words, not the markup.

    22,360 of 23,415 scraped descriptions carry `**`. Every fatal quote failure left in the
    last production run was this and nothing else.
    """

    MARKDOWN: ClassVar[dict[str, Any]] = {
        **POSTING,
        "description": (
            "**Clearance Required:** TS/SCI.\n\n"
            "****This position requires an******active U.S. national security "
            "clearance.****\n"
            "* \\*\\*Relocation\\*\\* to Boston is required\n"
        ),
    }

    def test_bold_around_the_phrase_is_not_a_difference(self) -> None:
        self.assertEqual(
            locate_blocker("Clearance Required: TS/SCI.", self.MARKDOWN["description"])[0],
            "verified",
        )

    def test_bold_inside_the_phrase_does_not_weld_the_words_together(self) -> None:
        """`an******active` must fold to two words, not to `anactive`."""
        self.assertEqual(
            locate_blocker(
                "This position requires an active U.S. national security clearance.",
                self.MARKDOWN["description"],
            )[0],
            "verified",
        )

    def test_escaped_markers_fold_the_same_way(self) -> None:
        self.assertEqual(
            locate("Relocation to Boston is required", self.MARKDOWN["description"])[0],
            "verified",
        )

    def test_folding_markup_does_not_make_an_invented_quote_locatable(self) -> None:
        self.assertEqual(
            locate("Requires a commercial pilot licence", self.MARKDOWN["description"])[0],
            "not_found",
        )

    def test_a_quote_that_carries_its_own_markup_still_matches(self) -> None:
        """Both sides are folded, so an underscore in the quote is not a mismatch."""
        self.assertEqual(
            locate_blocker("**Clearance Required:** TS/SCI.", self.MARKDOWN["description"])[0],
            "verified",
        )
