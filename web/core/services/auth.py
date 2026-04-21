"""
AuthService — Abstract interface for authentication adapters.
Implementations: LDAPSAuthAdapter, AzureADAuthAdapter, NativeAuthAdapter.
Tenant config determines which adapter is active.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from sqlalchemy import text as sa_text


@dataclass
class AuthResult:
    """Result of an authentication attempt."""
    success: bool
    user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    display_name: Optional[str] = None
    groups: list[str] = field(default_factory=list)
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class UserInfo:
    """User information from auth provider."""
    username: str
    email: str
    display_name: str
    groups: list[str] = field(default_factory=list)
    department: str = ""
    title: str = ""
    phone: str = ""
    enabled: bool = True
    metadata: dict = field(default_factory=dict)


class AuthService(ABC):
    """Abstract authentication interface."""

    @abstractmethod
    async def authenticate(self, username: str, password: str,
                           tenant_id: str) -> AuthResult:
        ...

    @abstractmethod
    async def get_user_info(self, username: str,
                            tenant_id: str) -> Optional[UserInfo]:
        ...

    @abstractmethod
    async def list_users(self, tenant_id: str,
                         search: str = "") -> list[UserInfo]:
        ...

    @abstractmethod
    async def sync_users(self, tenant_id: str) -> int:
        """Sync users from external provider. Returns count synced."""
        ...

    async def close(self):
        pass
