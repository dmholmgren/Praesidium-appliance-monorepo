"""
LegalResearchService — abstract interface for legal research providers.

Adapters: WestlawAdapter, LexisNexisAdapter
"""
from abc import ABC, abstractmethod
from typing import Optional


class LegalResearchService(ABC):
    @abstractmethod
    async def search_cases(self, tenant_id: str, query: str, jurisdiction: Optional[str] = None,
                           date_range: Optional[tuple] = None, limit: int = 20) -> list[dict]: ...

    @abstractmethod
    async def get_case(self, tenant_id: str, citation: str) -> dict: ...

    @abstractmethod
    async def check_citation(self, tenant_id: str, citation: str) -> dict:
        """Shepardize / KeyCite a citation."""
        ...

    @abstractmethod
    async def search_statutes(self, tenant_id: str, query: str,
                              jurisdiction: Optional[str] = None) -> list[dict]: ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class LegalResearchServiceStub(LegalResearchService):
    async def search_cases(self, *args, **kwargs): raise NotImplementedError("LegalResearchService adapter not configured")
    async def get_case(self, *args, **kwargs): raise NotImplementedError("LegalResearchService adapter not configured")
    async def check_citation(self, *args, **kwargs): raise NotImplementedError("LegalResearchService adapter not configured")
    async def search_statutes(self, *args, **kwargs): raise NotImplementedError("LegalResearchService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
