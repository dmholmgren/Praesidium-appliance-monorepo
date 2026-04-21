"""
AIService — abstract interface for ALL AI/LLM operations.

Mandatory Rule 4: All AI calls go through AIService only — never call SDK directly.

Adapters: AnthropicAdapter, OpenAIAdapter
"""
from abc import ABC, abstractmethod
from typing import Optional


class AIService(ABC):
    @abstractmethod
    async def complete(self, tenant_id: str, prompt: str, system: str = "",
                       model: Optional[str] = None, max_tokens: int = 4096,
                       temperature: float = 0.3, user_id: Optional[int] = None,
                       module: str = "unknown", purpose: str = "unknown") -> dict:
        """
        Send a completion request. Returns dict with:
        - text: response text
        - input_tokens: int
        - output_tokens: int
        - model: str
        - cost_usd: float
        """
        ...

    @abstractmethod
    async def embed(self, tenant_id: str, texts: list[str],
                    model: Optional[str] = None) -> list[list[float]]:
        """Generate embeddings for a list of texts."""
        ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class AIServiceStub(AIService):
    async def complete(self, *args, **kwargs): raise NotImplementedError("AIService adapter not configured")
    async def embed(self, *args, **kwargs): raise NotImplementedError("AIService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
