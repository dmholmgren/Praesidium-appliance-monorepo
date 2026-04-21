"""
LegalResearchService — Abstract interface for legal research providers.
Implementations: LexisAdapter, WestlawAdapter.
Tenant config determines which provider(s) are active.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum
from sqlalchemy import text as sa_text


class CiteSignal(Enum):
    """Shepards/KeyCite signal strength."""
    POSITIVE = "positive"
    CAUTIONARY = "cautionary"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    OVERRULED = "overruled"
    UNKNOWN = "unknown"


@dataclass
class ResearchResult:
    """A single search result from a legal research provider."""
    citation: str
    title: str
    court: str = ""
    date: str = ""
    relevance_score: float = 0.0
    snippet: str = ""
    full_text: Optional[str] = None
    cite_signal: CiteSignal = CiteSignal.UNKNOWN
    provider: str = ""
    provider_doc_id: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class CiteCheckResult:
    """Result of a Shepards/KeyCite citation check."""
    citation: str
    signal: CiteSignal
    treatment: str = ""
    citing_refs: list[ResearchResult] = field(default_factory=list)
    negative_refs: list[ResearchResult] = field(default_factory=list)
    provider: str = ""
    checked_at: str = ""


class LegalResearchService(ABC):
    """Abstract interface for legal research APIs."""

    @abstractmethod
    async def search(self, query: str, jurisdiction: str = "",
                     date_from: str = "", date_to: str = "",
                     max_results: int = 20) -> list[ResearchResult]:
        ...

    @abstractmethod
    async def cite_check(self, citation: str) -> CiteCheckResult:
        ...

    @abstractmethod
    async def get_document(self, provider_doc_id: str) -> ResearchResult:
        ...

    @abstractmethod
    async def get_citing_refs(self, citation: str,
                              max_results: int = 50) -> list[ResearchResult]:
        ...

    async def close(self):
        pass
