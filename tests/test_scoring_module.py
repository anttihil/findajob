"""Scoring: prompt layout, graph wiring, and the retry edge.

No API calls. The model is faked, because what needs testing is the machinery around it --
whether the cached half of the prompt stays constant, whether an unparseable response is
retried, and whether the retry is bounded.
"""

import os
import sys
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from findajob.profile.models import JobFitVerdict, PriorityVerdict
from findajob.scoring.prompts import (
    MAX_DESCRIPTION_CHARS,
    build_system,
    prompt_hash,
    render_posting,
)

PROFILE = "CANDIDATE PROFILE\n\nAn engineer who ships Python services.\n"


def posting(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": 1,
        "title": "Platform Engineer",
        "company": "Acme",
        "location": "Los Angeles, CA",
        "description": "We need someone who knows Kubernetes.",
    }
    base.update(overrides)
    return base


class PromptLayoutTests(unittest.TestCase):
    """The cached half must not vary with the posting. This is the cost model."""

    def test_system_half_is_identical_across_postings(self) -> None:
        system = build_system(PROFILE)
        self.assertEqual(system, build_system(PROFILE))

    def test_system_half_contains_no_posting_data(self) -> None:
        """A leak here silently multiplies input cost ~50x with no error."""
        system = build_system(PROFILE)
        job = posting()
        for value in (job["title"], job["company"], job["location"], job["description"]):
            self.assertNotIn(value, system)

    def test_build_system_ignores_legacy_threshold(self) -> None:
        system90 = build_system(PROFILE, fit_threshold=90)
        system80 = build_system(PROFILE, fit_threshold=80)
        self.assertNotIn("90%", system90)
        self.assertEqual(system90, system80)

    def test_prompt_hash_identifies_threshold_and_rules(self) -> None:
        h90 = prompt_hash(PROFILE, fit_threshold=90)
        h80 = prompt_hash(PROFILE, fit_threshold=80)
        self.assertEqual(h90, h80)
        self.assertEqual(len(h90), 16)

    def test_prompt_hash_changes_with_scoring_rules(self) -> None:
        original = prompt_hash(PROFILE)
        with mock.patch("findajob.scoring.prompts.RULES", "Changed scoring guidance."):
            self.assertNotEqual(prompt_hash(PROFILE), original)

    def test_render_posting_wraps_in_untrusted_delimiters(self) -> None:
        rendered = render_posting(posting())
        self.assertTrue(rendered.startswith("<posting>\n"))
        self.assertIn("<title>Platform Engineer</title>", rendered)
        self.assertIn("<company>Acme</company>", rendered)
        self.assertIn(
            "<description>\nWe need someone who knows Kubernetes.\n</description>", rendered
        )

    def test_render_posting_truncates_long_descriptions(self) -> None:
        huge = posting(description="x" * (MAX_DESCRIPTION_CHARS + 500))
        rendered = render_posting(huge)
        self.assertNotIn("x" * (MAX_DESCRIPTION_CHARS + 1), rendered)


class JobFitVerdictModelTests(unittest.TestCase):
    def test_verdict_fields(self) -> None:
        verdict = JobFitVerdict(
            fit=True,
            reason_type="match",
            reason_description="Strong backend and infrastructure experience.",
        )
        self.assertTrue(verdict.fit)
        self.assertEqual(verdict.reason_type, "match")
        self.assertEqual(
            verdict.reason_description, "Strong backend and infrastructure experience."
        )

    def test_reason_type_normalization(self) -> None:
        verdict = JobFitVerdict(
            fit=False,
            reason_type="  Skills  ",
            reason_description="Lacks required Golang experience.",
        )
        self.assertFalse(verdict.fit)
        self.assertEqual(verdict.reason_type, "skills")


class FakeChain:
    """Stands in for `model.with_structured_output(...)`."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[list[Any]] = []
        self.invoke_error: Exception | None = None

    def invoke(self, messages: list[Any]) -> dict[str, Any]:
        self.calls.append(messages)
        if not self.responses and self.invoke_error is not None:
            raise self.invoke_error
        return self.responses.pop(0) if self.responses else self.responses[-1]


def assessment(**overrides: Any) -> JobFitVerdict:
    fields: dict[str, Any] = {
        "fit": True,
        "reason_type": "match",
        "reason_description": "Strong alignment with tech stack.",
    }
    fields.update(overrides)
    return JobFitVerdict(**fields)


def ok_response(**overrides: Any) -> dict[str, Any]:
    return {
        "parsed": assessment(**overrides),
        "raw": mock.Mock(response_metadata={}),
        "parsing_error": None,
    }


def billed(prompt: int, cache_hit: int, completion: int) -> mock.Mock:
    return mock.Mock(
        response_metadata={
            "token_usage": {
                "prompt_tokens": prompt,
                "prompt_cache_hit_tokens": cache_hit,
                "prompt_cache_miss_tokens": prompt - cache_hit,
                "completion_tokens": completion,
            }
        }
    )


def bad_response() -> dict[str, Any]:
    return {"parsed": None, "raw": mock.Mock(response_metadata={}), "parsing_error": "no tool call"}


def priority_response() -> dict[str, Any]:
    return {
        "parsed": PriorityVerdict(
            priority="consider", reason="Credible transferable work with a manageable stretch."
        ),
        "raw": mock.Mock(response_metadata={}),
        "parsing_error": None,
    }


class GraphTests(unittest.TestCase):
    def run_graph(self, responses: list[dict[str, Any]]) -> tuple[dict[str, Any], FakeChain]:
        from findajob.scoring.graph import build_graph

        chain = FakeChain(responses)
        model = mock.Mock()
        model.with_structured_output.return_value = chain
        with mock.patch("findajob.scoring.graph.structured_model", return_value=model):
            state = build_graph().invoke(
                {
                    "system": build_system(PROFILE),
                    "posting": posting(),
                    "model": "deepseek-v4-flash",
                    "review_model": "pro",
                }
            )
        return state, chain

    def test_a_good_response_is_scored_in_one_call(self) -> None:
        state, chain = self.run_graph([priority_response()])
        self.assertEqual(len(chain.calls), 1)
        self.assertIsNone(state.get("error"))
        self.assertEqual(state["verdict"]["priority"], "consider")
        self.assertTrue(state["verdict"]["reason"])

    def test_model_receives_complete_system_prompt_without_duplicate_rules(self) -> None:
        _, chain = self.run_graph([priority_response()])
        self.assertEqual(chain.calls[0][0], ("system", build_system(PROFILE)))

    def test_an_unparseable_response_is_retried(self) -> None:
        state, chain = self.run_graph([bad_response(), priority_response()])
        self.assertEqual(len(chain.calls), 2)
        self.assertIsNotNone(state["verdict"])
        self.assertEqual(state["verdict"]["priority"], "consider")

    def test_retries_are_bounded_and_give_up_cleanly(self) -> None:
        from findajob.scoring.graph import MAX_ATTEMPTS

        state, chain = self.run_graph([bad_response()] * (MAX_ATTEMPTS + 3))
        self.assertEqual(len(chain.calls), MAX_ATTEMPTS)
        self.assertIsNone(state["verdict"])
        self.assertIsNotNone(state.get("error"))

    def test_an_api_exception_is_caught_rather_than_killing_the_run(self) -> None:
        from findajob.scoring.graph import build_graph

        chain = mock.Mock()
        chain.invoke.side_effect = RuntimeError("429 rate limited")
        model = mock.Mock()
        model.with_structured_output.return_value = chain
        with mock.patch("findajob.scoring.graph.structured_model", return_value=model):
            state = build_graph().invoke(
                {
                    "system": "s",
                    "posting": posting(),
                    "model": "deepseek-v4-flash",
                    "review_model": "pro",
                }
            )
        self.assertIsNone(state["verdict"])
        self.assertIn("429", state["error"])

    def test_a_retry_is_charged_to_the_posting_that_needed_it(self) -> None:
        rejected = bad_response()
        rejected["raw"] = billed(6_000, 4_800, 20)
        accepted = priority_response()
        accepted["raw"] = billed(6_100, 4_800, 50)

        state, chain = self.run_graph([rejected, accepted])

        self.assertEqual(len(chain.calls), 2)
        self.assertIsNotNone(state["verdict"])
        self.assertEqual(state["usage"]["prompt"], 12_100)
        self.assertEqual(state["usage"]["cache_hit"], 9_600)
        self.assertEqual(state["usage"]["cache_miss"], 2_500)
        self.assertEqual(state["usage"]["completion"], 70)


if __name__ == "__main__":
    unittest.main()
