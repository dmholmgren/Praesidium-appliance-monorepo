"""
AccountingService — abstract interface for accounting system integration.

Adapters: QuickBooksOnlineAdapter, QuickBooksDesktopAdapter
"""
from abc import ABC, abstractmethod
from typing import Optional


class AccountingService(ABC):
    @abstractmethod
    async def sync_invoice(self, tenant_id: str, invoice_data: dict) -> str:
        """Push invoice to accounting system. Returns external invoice ID."""
        ...

    @abstractmethod
    async def sync_payment(self, tenant_id: str, payment_data: dict) -> str:
        """Push payment to accounting system. Returns external payment ID."""
        ...

    @abstractmethod
    async def get_chart_of_accounts(self, tenant_id: str) -> list[dict]: ...

    @abstractmethod
    async def sync_client(self, tenant_id: str, client_data: dict) -> str:
        """Push client to accounting system. Returns external customer ID."""
        ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class AccountingServiceStub(AccountingService):
    async def sync_invoice(self, *args, **kwargs): raise NotImplementedError("AccountingService adapter not configured")
    async def sync_payment(self, *args, **kwargs): raise NotImplementedError("AccountingService adapter not configured")
    async def get_chart_of_accounts(self, *args, **kwargs): raise NotImplementedError("AccountingService adapter not configured")
    async def sync_client(self, *args, **kwargs): raise NotImplementedError("AccountingService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
