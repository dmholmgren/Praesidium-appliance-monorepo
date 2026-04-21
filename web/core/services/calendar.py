"""
CalendarService — abstract interface for all calendar operations.

Adapters: ExchangeEWSCalendarAdapter, MicrosoftGraphCalendarAdapter,
          GoogleCalendarAdapter
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional


class CalendarService(ABC):
    @abstractmethod
    async def create_event(self, tenant_id: str, user_id: int, title: str,
                           start: datetime, end: datetime, description: str = "",
                           location: str = "", attendees: Optional[list[str]] = None) -> str:
        """Create a calendar event. Returns event ID."""
        ...

    @abstractmethod
    async def update_event(self, tenant_id: str, event_id: str, **kwargs) -> bool: ...

    @abstractmethod
    async def delete_event(self, tenant_id: str, event_id: str) -> bool: ...

    @abstractmethod
    async def get_events(self, tenant_id: str, user_id: int,
                         start: datetime, end: datetime) -> list[dict]: ...

    @abstractmethod
    async def health_check(self) -> dict: ...


class CalendarServiceStub(CalendarService):
    async def create_event(self, *args, **kwargs): raise NotImplementedError("CalendarService adapter not configured")
    async def update_event(self, *args, **kwargs): raise NotImplementedError("CalendarService adapter not configured")
    async def delete_event(self, *args, **kwargs): raise NotImplementedError("CalendarService adapter not configured")
    async def get_events(self, *args, **kwargs): raise NotImplementedError("CalendarService adapter not configured")
    async def health_check(self): return {"status": "stub", "message": "No adapter configured"}
