"""Base types and interfaces for LLM providers in CareerRadar."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Base exception for LLM provider errors."""


class LLMUnavailableError(LLMError):
    """Raised when a requested LLM provider or binary is not available or not configured."""


class MissingApiKey(LLMUnavailableError):
    """Raised when an API key is required but missing."""


class StructuredOutputError(LLMError):
    """The model answered, but never made the tool call or valid JSON schema required."""


@dataclass
class LLMResponse:
    """Standardized response container across all LLM providers."""

    content: str
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt": 0, "completion": 0, "cache_hit": 0, "cache_miss": 0}
    )
    cost_usd: float = 0.0
    model: str = ""
    raw: Any = None


class BaseLLMProvider(ABC):
    """Abstract base class for all LLM providers (CLI-piped and API-based)."""

    name: str
    default_model: str = ""
    default_scoring_model: str = ""
    default_agent_model: str = ""

    @abstractmethod
    def is_available(self) -> tuple[bool, str]:
        """Check if the provider is available and properly configured/authenticated.

        Returns (is_available, status_or_reason_message).
        """

    @abstractmethod
    def complete(
        self,
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run standard text completion."""

    @abstractmethod
    def complete_structured(
        self,
        schema: type[T],
        messages: list[tuple[str, str]] | list[dict[str, str]] | str,
        *,
        system: str | None = None,
        model: str | None = None,
        temperature: float = 0.0,
        attempts: int = 3,
        label: str = "",
        **kwargs: Any,
    ) -> T:
        """Run schema-enforced structured completion returning a validated Pydantic model."""

    def estimate_cost(
        self,
        model: str,  # noqa: ARG002
        prompt_tokens: int,  # noqa: ARG002
        completion_tokens: int,  # noqa: ARG002
        cached_tokens: int = 0,  # noqa: ARG002
    ) -> float:
        """Pre-flight cost estimate in USD. Defaults to 0.0 for subscription-based CLI tools."""
        return 0.0
