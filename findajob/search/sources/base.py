"""The page boundary between board-specific parsing and search orchestration."""

from abc import ABC, abstractmethod
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from findajob.search.scheduler import ScrapeTaskPayload
from findajob.search.traffic import SourceTraffic


@dataclass
class SearchPage:
    rows: list[dict[str, Any]]
    proxy: str | None = None
    end_reason: str | None = None


class BaseJobSource(ABC):
    @abstractmethod
    def iter_pages(self, task: ScrapeTaskPayload, traffic: SourceTraffic) -> Generator[SearchPage]:
        """Yield raw board pages, preserving board order and explicit exhaustion.

        The runner owns the 500-row ceiling and the request budget. Empty raw
        pages terminate pagination; duplicate-only pages do not prove exhaustion.
        All HTTP must go through SourceTraffic. Errors propagate, so the caller
        can preserve earlier saved pages and record an incomplete observation.
        """
