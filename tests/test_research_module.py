"""Research: what happens when a schema-enforced call comes back without a tool call.

The stakes here are different from the profile graph's. A dossier that quietly loses its
intel does not look broken -- `node_synthesize` fills the hole with "not researched: web
search is unavailable (TAVILY_API_KEY is not set)", which would be a confident, false
explanation of a model failure. Degrading is worse than failing, so these nodes raise and
let the worker skip the company and retry it on the next run.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from careerradar.core.llm import StructuredOutputError
from careerradar.research.models import CompanyIntel


def prose_response(text="I looked into this company and here is what I found."):
    """V4 answering a forced tool choice with prose: parsed is None, nothing raised."""
    raw = mock.Mock(response_metadata={"finish_reason": "stop"},
                    invalid_tool_calls=[], content=text)
    return {"parsed": None, "raw": raw, "parsing_error": None}


def intel_response():
    return {"parsed": CompanyIntel(summary="A payments company."),
            "raw": mock.Mock(response_metadata={"finish_reason": "tool_calls"}),
            "parsing_error": None}


class GatherIntelTests(unittest.TestCase):
    def gather(self, responses):
        from careerradar.research.graph import node_gather_intel

        chain = mock.Mock()
        chain.invoke.side_effect = list(responses)
        model = mock.Mock()
        model.with_structured_output.return_value = chain
        hit = {"url": "https://example.com/a", "title": "A", "content": "text"}
        with mock.patch("careerradar.research.graph.web_search", return_value=[hit]), \
             mock.patch("careerradar.research.graph.structured_model",
                        return_value=model):
            state = {"company": "Acme", "search_enabled": True, "model": "deepseek-v4-pro"}
            return node_gather_intel(state), chain

    def test_a_transient_missing_tool_call_is_retried(self):
        result, chain = self.gather([prose_response(), intel_response()])

        self.assertEqual(result["intel"]["summary"], "A payments company.")
        self.assertEqual(chain.invoke.call_count, 2)

    def test_a_persistent_failure_raises_rather_than_returning_empty_intel(self):
        """Returning `{"intel": None}` here would blame a missing TAVILY_API_KEY."""
        with self.assertRaises(StructuredOutputError):
            self.gather([prose_response()] * 3)

    def test_no_search_results_still_degrades_quietly_without_a_model_call(self):
        """Nothing to summarize is a real, honest outcome -- not a failure."""
        from careerradar.research.graph import node_gather_intel

        with mock.patch("careerradar.research.graph.web_search", return_value=[]):
            result = node_gather_intel({"company": "Acme", "search_enabled": True})
        self.assertIsNone(result["intel"])


if __name__ == "__main__":
    unittest.main()
