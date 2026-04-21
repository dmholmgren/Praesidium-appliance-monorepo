"""
EmailService — abstract interface for all email operations.
"""
from abc import ABC, abstractmethod
from typing import Optional


class EmailService(ABC):
    @abstractmethod
    async def send_email(self, tenant_id: str, to: list[str], subject: str, body: str,
                         cc: Optional[list[str]] = None, attachments: Optional[list] = None) -> bool: ...

    @abstractmethod
    async def fetch_emails(self, tenant_id: str, folder: str, since: Optional[str] = None,
                           limit: int = 50) -> list[dict]: ...

    @abstractmethod
    async def search_emails(self, tenant_id: str, query: str, limit: int = 50) -> list[dict]: ...

    @abstractmethod
    async def file_email(self, tenant_id: str, email_id: str, folder: str) -> bool: ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class EmailServiceStub(EmailService):
    async def send_email(self, *args, **kwargs): raise NotImplementedError("EmailService adapter not configured")
    async def fetch_emails(self, *args, **kwargs): raise NotImplementedError("EmailService adapter not configured")
    async def search_emails(self, *args, **kwargs): raise NotImplementedError("EmailService adapter not configured")
    async def file_email(self, *args, **kwargs): raise NotImplementedError("EmailService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
