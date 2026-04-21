"""
CommunicationService — abstract interface for phone/video/SMS operations.

Adapters: FreePBXAdapter, TwilioAdapter, TeamsAdapter, ZoomAdapter, WebexAdapter
"""
from abc import ABC, abstractmethod
from typing import Optional


class CommunicationService(ABC):
    @abstractmethod
    async def get_call_records(self, tenant_id: str, since: Optional[str] = None,
                               limit: int = 100) -> list[dict]:
        """Fetch call detail records."""
        ...

    @abstractmethod
    async def send_sms(self, tenant_id: str, to: str, body: str,
                       from_number: Optional[str] = None) -> dict: ...

    @abstractmethod
    async def get_recording(self, tenant_id: str, call_id: str) -> bytes: ...

    @abstractmethod
    async def get_consent_requirement(self, tenant_id: str, caller_state: str,
                                      callee_state: str) -> str:
        """Returns 'one_party' or 'two_party'."""
        ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class CommunicationServiceStub(CommunicationService):
    async def get_call_records(self, *args, **kwargs): raise NotImplementedError("CommunicationService adapter not configured")
    async def send_sms(self, *args, **kwargs): raise NotImplementedError("CommunicationService adapter not configured")
    async def get_recording(self, *args, **kwargs): raise NotImplementedError("CommunicationService adapter not configured")
    async def get_consent_requirement(self, *args, **kwargs): raise NotImplementedError("CommunicationService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
