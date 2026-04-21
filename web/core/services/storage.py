"""
StorageService — Abstract interface for all storage adapters.
Implementations: CIFSStorageAdapter (on-prem), S3, Azure, SharePoint, Google Drive.
Adapter selected from tenants.storage_adapter column — never hardcoded.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from sqlalchemy import text as sa_text


class StorageError(Exception):
    """Raised when a storage operation fails."""
    pass


@dataclass
class FileInfo:
    """Metadata returned by storage operations."""
    path: str
    name: str
    size: int = 0
    content_type: str = "application/octet-stream"
    modified_at: Optional[str] = None
    checksum: Optional[str] = None
    is_directory: bool = False
    metadata: dict = field(default_factory=dict)


class StorageService(ABC):
    """Abstract storage interface — all adapters must implement these methods."""

    @abstractmethod
    async def upload(self, tenant_id: str, path: str, content: bytes,
                     content_type: str = "application/octet-stream") -> FileInfo:
        ...

    @abstractmethod
    async def download(self, tenant_id: str, path: str) -> bytes:
        ...

    @abstractmethod
    async def delete(self, tenant_id: str, path: str) -> bool:
        ...

    @abstractmethod
    async def list(self, tenant_id: str, prefix: str = "",
                   recursive: bool = False) -> list[FileInfo]:
        ...

    @abstractmethod
    async def get_url(self, tenant_id: str, path: str,
                      expires: int = 3600) -> str:
        ...

    @abstractmethod
    async def create_folder(self, tenant_id: str, path: str) -> bool:
        ...

    @abstractmethod
    async def move(self, tenant_id: str, src_path: str,
                   dst_path: str) -> FileInfo:
        ...

    @abstractmethod
    async def exists(self, tenant_id: str, path: str) -> bool:
        ...

    @abstractmethod
    async def stat(self, tenant_id: str, path: str) -> FileInfo:
        ...

    @abstractmethod
    async def checksum(self, tenant_id: str, path: str) -> str:
        ...

    async def close(self):
        """Cleanup resources. Override in adapters that hold connections."""
        pass
