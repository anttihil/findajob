"""Unit and integration tests for LLM providers and interface pattern."""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pydantic import BaseModel, Field

from findajob.core.llm import (
    ModelAdapter,
    Spend,
    get_llm_provider,
    list_available_providers,
)
from findajob.core.llm.providers.agy_cli import AGYCLIProvider
from findajob.core.llm.providers.claude_cli import ClaudeCLIProvider
from findajob.core.llm.providers.cli_base import BaseCLIProvider
from findajob.core.llm.providers.codex_cli import CodexCLIProvider
from findajob.core.llm.providers.deepseek_api import BalanceMeasuredChat
from findajob.core.llm.providers.opencode_cli import OpenCodeCLIProvider


class SimpleSchema(BaseModel):
    name: str = Field(description="Name")
    score: int = Field(description="Score between 0 and 100")


class BaseCLIProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenCodeCLIProvider()

    def test_strip_ansi(self) -> None:
        raw = "\x1b[32mSuccess\x1b[0m with \x1b[1mbold\x1b[0m text"
        self.assertEqual(self.provider._strip_ansi(raw), "Success with bold text")

    def test_format_messages_tuple(self) -> None:
        messages = [("system", "Sys instruction"), ("user", "Hello world")]
        sys_res, user_res = self.provider._format_messages(messages)
        self.assertEqual(sys_res, "Sys instruction")
        self.assertIn("<user>\nHello world\n</user>", user_res)

    def test_extract_json_direct(self) -> None:
        data = self.provider._extract_json_from_text('{"name": "Alice", "score": 95}')
        self.assertEqual(data, {"name": "Alice", "score": 95})

    def test_extract_json_markdown_block(self) -> None:
        raw = 'Here is the response:\n```json\n{"name": "Bob", "score": 88}\n```\nDone.'
        data = self.provider._extract_json_from_text(raw)
        self.assertEqual(data, {"name": "Bob", "score": 88})

    def test_extract_json_embedded(self) -> None:
        raw = 'Some prefix text {"name": "Charlie", "score": 77} and some trailing text.'
        data = self.provider._extract_json_from_text(raw)
        self.assertEqual(data, {"name": "Charlie", "score": 77})

    def test_estimate_tokens(self) -> None:
        usage = self.provider._estimate_tokens("a" * 40, "b" * 20)
        self.assertEqual(usage["prompt"], 10)
        self.assertEqual(usage["completion"], 5)


class AGYProviderTests(unittest.TestCase):
    @mock.patch.object(BaseCLIProvider, "_run_subprocess")
    def test_agy_complete_structured(self, mock_run: mock.MagicMock) -> None:
        envelope = {
            "status": "SUCCESS",
            "structured_output": {"name": "Antigravity", "score": 99},
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        mock_run.return_value = (0, json.dumps(envelope), "")

        provider = AGYCLIProvider()
        result = provider.complete_structured(
            SimpleSchema,
            "Give me Antigravity score",
            model="gemini-3.7-flash-high",
        )
        self.assertEqual(result.name, "Antigravity")
        self.assertEqual(result.score, 99)

        # Verify command arguments
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "agy")
        self.assertIn("--json-schema", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)

    @mock.patch.object(BaseCLIProvider, "_run_subprocess")
    def test_agy_resolves_deepseek_model(self, mock_run: mock.MagicMock) -> None:
        envelope = {
            "status": "SUCCESS",
            "structured_output": {"name": "Antigravity", "score": 99},
        }
        mock_run.return_value = (0, json.dumps(envelope), "")

        provider = AGYCLIProvider()
        provider.complete_structured(
            SimpleSchema,
            "Give me Antigravity score",
            model="deepseek-v4-flash",
        )
        cmd = mock_run.call_args[0][0]
        self.assertIn("gemini-3.7-flash-high", cmd)
        self.assertNotIn("deepseek-v4-flash", cmd)


class ClaudeProviderTests(unittest.TestCase):
    @mock.patch.object(BaseCLIProvider, "_run_subprocess")
    def test_claude_complete(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = (0, '```json\n{"name": "Claude", "score": 90}\n```', "")

        provider = ClaudeCLIProvider()
        res = provider.complete("Test claude")
        self.assertIn("Claude", res.content)

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "claude")
        self.assertIn("-p", cmd)
        self.assertIn("--tools", cmd)
        self.assertIn("", cmd)


class CodexProviderTests(unittest.TestCase):
    @mock.patch.object(BaseCLIProvider, "_run_subprocess")
    def test_codex_complete(self, mock_run: mock.MagicMock) -> None:
        mock_run.return_value = (0, '{"name": "Codex", "score": 85}', "")

        provider = CodexCLIProvider()
        structured = provider.complete_structured(SimpleSchema, "Test prompt")
        self.assertEqual(structured.name, "Codex")
        self.assertEqual(structured.score, 85)

        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd[0], "codex")
        self.assertIn("exec", cmd)
        self.assertIn("--ephemeral", cmd)


class FactoryAndRegistryTests(unittest.TestCase):
    def test_list_available_providers(self) -> None:
        providers = list_available_providers()
        names = [p["name"] for p in providers]
        self.assertIn("agy", names)
        self.assertIn("claude", names)
        self.assertIn("codex", names)
        self.assertIn("opencode", names)
        self.assertIn("deepseek", names)

    @mock.patch.dict(os.environ, {"LLM_PROVIDER": "opencode"})
    def test_explicit_env_provider(self) -> None:
        provider = get_llm_provider()
        self.assertEqual(provider.name, "opencode")

    @mock.patch.dict(os.environ, {"LLM_PROVIDER": "codex"})
    def test_explicit_codex_provider(self) -> None:
        provider = get_llm_provider()
        self.assertEqual(provider.name, "codex")

    def test_get_model_for_role_override(self) -> None:
        from findajob.core.llm import get_model_for_role

        self.assertEqual(get_model_for_role("scoring", override="custom-model"), "custom-model")
        self.assertEqual(get_model_for_role("agent", override="custom-agent"), "custom-agent")

    def test_get_model_for_role_fallback_when_unavailable(self) -> None:
        from findajob.core.llm import LLMUnavailableError, get_model_for_role

        with mock.patch(
            "findajob.core.llm.get_llm_provider",
            side_effect=LLMUnavailableError("none"),
        ):
            self.assertEqual(get_model_for_role("scoring"), "deepseek-chat")
            self.assertEqual(get_model_for_role("agent"), "deepseek-chat")

    def test_get_model_for_role_with_provider(self) -> None:
        from findajob.core.llm import get_model_for_role

        self.assertEqual(get_model_for_role("scoring", provider="agy"), "gemini-3.7-flash-high")
        self.assertEqual(get_model_for_role("agent", provider="agy"), "gemini-3.7-flash-high")

    def test_no_provider_available_raises(self) -> None:
        from findajob.core.llm import LLMUnavailableError

        with (
            mock.patch.dict(os.environ, {"LLM_PROVIDER": "auto"}, clear=True),
            mock.patch("shutil.which", return_value=None),
        ):
            with self.assertRaises(LLMUnavailableError):
                get_llm_provider()


class AdapterTests(unittest.TestCase):
    def test_model_adapter_with_structured_output(self) -> None:
        mock_provider = mock.MagicMock()
        mock_provider.complete_structured.return_value = SimpleSchema(name="Mocked", score=100)

        adapter = ModelAdapter(mock_provider, model_name="test-model")
        chain = adapter.with_structured_output(SimpleSchema, include_raw=True)
        result = chain.invoke([("user", "Hello")])

        self.assertEqual(result["parsed"].name, "Mocked")
        self.assertEqual(result["parsed"].score, 100)
        self.assertIsNone(result["parsing_error"])


class CostAndSpendTests(unittest.TestCase):
    def test_spend_tracker(self) -> None:
        spend = Spend("deepseek-v4-flash", max_usd=1.0)
        fake_msg = mock.Mock(
            response_metadata={
                "token_usage": {
                    "prompt_tokens": 1000,
                    "prompt_cache_hit_tokens": 800,
                    "prompt_cache_miss_tokens": 200,
                    "completion_tokens": 50,
                }
            }
        )
        fake_msg.findajob_cost_usd = 0.01
        usage, _cost = spend.record(fake_msg)
        self.assertEqual(usage["cache_hit"], 800)
        self.assertEqual(spend.calls, 1)
        self.assertFalse(spend.exhausted())


class DeepSeekBalanceTests(unittest.TestCase):
    @mock.patch("findajob.core.llm.providers.deepseek_api.usd_balance", side_effect=[10.0, 9.75])
    def test_measures_cost_from_balance_delta(self, balance: mock.MagicMock) -> None:
        chat = mock.MagicMock()
        chat.invoke.return_value = "response"

        response, cost = BalanceMeasuredChat(chat).invoke([("user", "hello")])

        self.assertEqual(response, "response")
        self.assertEqual(cost, 0.25)
        self.assertEqual(balance.call_count, 2)


if __name__ == "__main__":
    unittest.main()
