"""Provider factory and registry for Find a Job's LLM subsystem."""

import os
from typing import Any

from careerradar.core.config import load_config
from careerradar.core.llm.base import BaseLLMProvider, LLMUnavailableError
from careerradar.core.llm.providers.agy_cli import AGYCLIProvider
from careerradar.core.llm.providers.claude_cli import ClaudeCLIProvider
from careerradar.core.llm.providers.codex_cli import CodexCLIProvider
from careerradar.core.llm.providers.deepseek_api import DeepSeekAPIProvider
from careerradar.core.llm.providers.opencode_cli import OpenCodeCLIProvider
from careerradar.core.logger import get_logger

logger = get_logger()

# Global provider instances cache
_PROVIDERS: dict[str, BaseLLMProvider] = {
    "agy": AGYCLIProvider(),
    "claude": ClaudeCLIProvider(),
    "codex": CodexCLIProvider(),
    "opencode": OpenCodeCLIProvider(),
    "deepseek": DeepSeekAPIProvider(),
}

AUTO_DETECTION_ORDER = ["agy", "claude", "codex", "opencode", "deepseek"]


def list_available_providers() -> list[dict[str, Any]]:
    """Return status and diagnostics for all known providers."""
    results = []
    for name, provider in _PROVIDERS.items():
        avail, message = provider.is_available()
        results.append(
            {
                "name": name,
                "available": avail,
                "status": message,
                "type": "cli" if name != "deepseek" else "api",
            }
        )
    return results


def get_llm_provider(name: str | None = None) -> BaseLLMProvider:
    """Resolve and return an active LLM provider instance."""
    # 1. Explicit parameter
    target = name

    # 2. Environment variable override
    if not target:
        target = os.environ.get("LLM_PROVIDER")

    # 3. Config file setting
    if not target:
        config = load_config()
        llm_cfg = config.get("llm") or {}
        target = llm_cfg.get("provider")

    # If explicitly named and not 'auto'
    if target and target.lower() != "auto":
        provider = _PROVIDERS.get(target.lower())
        if not provider:
            raise LLMUnavailableError(
                f"Unknown LLM provider '{target}'. Supported providers: {list(_PROVIDERS.keys())}"
            )
        return provider

    # 4. Auto-detect from available tools
    for candidate_name in AUTO_DETECTION_ORDER:
        candidate = _PROVIDERS[candidate_name]
        is_avail, reason = candidate.is_available()
        if is_avail:
            logger.info("Auto-selected LLM provider: %s (%s)", candidate_name, reason)
            return candidate

    # If no provider is available
    status_summary = "\n".join(
        f"  - {p['name']}: {p['status']}" for p in list_available_providers()
    )
    raise LLMUnavailableError(
        "No LLM provider is currently available or authenticated.\n"
        f"Provider status:\n{status_summary}\n\n"
        "To fix: either authenticate with a CLI (e.g. 'claude auth login', 'codex login', 'agy'), "
        "or set DEEPSEEK_API_KEY in your .env file."
    )
