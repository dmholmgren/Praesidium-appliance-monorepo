"""
COMP 1 — CIFSStorageAdapter
StorageService implementation for CIFS file shares via the file-bridge container.
File bridge runs on MAIN-PRD-FBRG-01 (10.10.60.13:8001).
CIFS_URL read from environment — never hardcoded.
Mounts: /mnt/clients (rw), /mnt/docsend (ro).
"""

import os
import hashlib
from typing import Optional

import httpx

from core.services.storage import StorageService, FileInfo, StorageError
from sqlalchemy import text as sa_text


class CIFSStorageAdapter(StorageService):
    """
    Connects to the file-bridge HTTP API on FBRG-01.
    All file operations are proxied through the bridge container
    which has direct CIFS mount access.
    """

    def __init__(self):
        self.base_url = os.environ["CIFS_URL"]  # e.g. http://10.10.60.13:8001
        self.timeout = float(os.environ.get("CIFS_TIMEOUT", "30"))
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        """Make request to file-bridge with error handling."""
        client = await self._get_client()
        resp = await getattr(client, method)(endpoint, **kwargs)
        if resp.status_code >= 400:
            raise StorageError(
                f"CIFS bridge {method.upper()} {endpoint} "
                f"failed: {resp.status_code} {resp.text}"
            )
        return resp

    async def upload(self, tenant_id: str, path: str, content: bytes,
                     content_type: str = "application/octet-stream") -> FileInfo:
        resp = await self._request(
            "post", "/api/v1/files/upload",
            files={"file": (os.path.basename(path), content, content_type)},
            data={"tenant_id": tenant_id, "path": path},
        )
        return FileInfo(**resp.json())

    async def download(self, tenant_id: str, path: str) -> bytes:
        resp = await self._request(
            "get", "/api/v1/files/download",
            params={"tenant_id": tenant_id, "path": path},
        )
        return resp.content

    async def delete(self, tenant_id: str, path: str) -> bool:
        resp = await self._request(
            "delete", "/api/v1/files/delete",
            params={"tenant_id": tenant_id, "path": path},
        )
        return resp.json().get("deleted", False)

    async def list(self, tenant_id: str, prefix: str = "",
                   recursive: bool = False) -> list[FileInfo]:
        resp = await self._request(
            "get", "/api/v1/files/list",
            params={
                "tenant_id": tenant_id,
                "prefix": prefix,
                "recursive": str(recursive).lower(),
            },
        )
        return [FileInfo(**item) for item in resp.json().get("files", [])]

    async def get_url(self, tenant_id: str, path: str,
                      expires: int = 3600) -> str:
        return (
            f"{self.base_url}/api/v1/files/download"
            f"?tenant_id={tenant_id}&path={path}"
        )

    async def create_folder(self, tenant_id: str, path: str) -> bool:
        resp = await self._request(
            "post", "/api/v1/files/mkdir",
            json={"tenant_id": tenant_id, "path": path},
        )
        return resp.json().get("created", False)

    async def move(self, tenant_id: str, src_path: str,
                   dst_path: str) -> FileInfo:
        resp = await self._request(
            "post", "/api/v1/files/move",
            json={
                "tenant_id": tenant_id,
                "src_path": src_path,
                "dst_path": dst_path,
            },
        )
        return FileInfo(**resp.json())

    async def exists(self, tenant_id: str, path: str) -> bool:
        client = await self._get_client()
        resp = await client.head(
            "/api/v1/files/stat",
            params={"tenant_id": tenant_id, "path": path},
        )
        return resp.status_code == 200

    async def stat(self, tenant_id: str, path: str) -> FileInfo:
        resp = await self._request(
            "get", "/api/v1/files/stat",
            params={"tenant_id": tenant_id, "path": path},
        )
        return FileInfo(**resp.json())

    async def checksum(self, tenant_id: str, path: str) -> str:
        content = await self.download(tenant_id, path)
        return hashlib.sha256(content).hexdigest()
