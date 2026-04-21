from sqlalchemy import text as sa_text
"""
COMP 4 — Meilisearch Integration
Index: documents_{tenant_id} — one index per tenant.
Meilisearch URL: MEILISEARCH_URL env var (10.10.60.12:7700 for HJMM).
RQ job: index_document dispatched to PROC-01.
Search endpoint: GET /api/v1/dms/search
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class MeilisearchService:
    """Manages per-tenant Meilisearch indexes for document search."""

    def __init__(self):
        self.base_url = os.environ["MEILISEARCH_URL"]  # e.g. http://10.10.60.12:7700
        self.api_key = os.environ.get("MEILISEARCH_MASTER_KEY", "")
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=30,
            )
        return self._client

    def _index_name(self, tenant_id: str) -> str:
        """Per-tenant index: documents_{tenant_id}. Uses tenant_id, never firm name."""
        return f"documents_{tenant_id}"

    async def ensure_index(self, tenant_id: str):
        """Create index if it doesn't exist. Configure searchable/filterable attributes."""
        client = await self._get_client()
        index = self._index_name(tenant_id)

        # Create index
        await client.post("/indexes", json={
            "uid": index,
            "primaryKey": "id",
        })

        # Configure searchable attributes
        await client.put(f"/indexes/{index}/settings/searchable-attributes", json=[
            "name", "ocr_text", "matter_name", "client_name",
            "file_type", "tags",
        ])

        # Configure filterable attributes
        await client.put(f"/indexes/{index}/settings/filterable-attributes", json=[
            "matter_id", "file_type", "created_at", "modified_at",
            "tags", "is_deleted",
        ])

        # Configure sortable attributes
        await client.put(f"/indexes/{index}/settings/sortable-attributes", json=[
            "created_at", "modified_at", "name", "file_size",
        ])

        # Configure ranking rules
        await client.put(f"/indexes/{index}/settings/ranking-rules", json=[
            "words", "typo", "proximity", "attribute", "sort", "exactness",
        ])

        logger.info(f"Index {index} configured for tenant {tenant_id}")

    async def index_document(self, tenant_id: str, doc_data: dict):
        """Add or update a document in the tenant's search index."""
        client = await self._get_client()
        index = self._index_name(tenant_id)
        await client.post(f"/indexes/{index}/documents", json=[doc_data])

    async def delete_document(self, tenant_id: str, document_id: str):
        """Remove a document from the search index."""
        client = await self._get_client()
        index = self._index_name(tenant_id)
        await client.delete(f"/indexes/{index}/documents/{document_id}")

    async def search(
        self,
        tenant_id: str,
        query: str,
        matter_id: Optional[str] = None,
        doc_type: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """
        Full-text search across tenant's documents.
        Returns results with highlighted snippets.
        """
        client = await self._get_client()
        index = self._index_name(tenant_id)

        # Build filter
        filters = ["1=1"]
        if matter_id:
            filters.append(f'matter_id = "{matter_id}"')
        if doc_type:
            filters.append(f'file_type = "{doc_type}"')
        if date_from:
            filters.append(f"created_at >= {_ts(date_from)}")
        if date_to:
            filters.append(f"created_at <= {_ts(date_to)}")

        body = {
            "q": query,
            "filter": " AND ".join(filters) if filters else None,
            "limit": limit,
            "offset": offset,
            "attributesToHighlight": ["name", "ocr_text"],
            "attributesToCrop": ["ocr_text"],
            "cropLength": 200,
            "showRankingScore": True,
        }
        # Remove None values
        body = {k: v for k, v in body.items() if v is not None}

        resp = await client.post(f"/indexes/{index}/search", json=body)
        if resp.status_code != 200:
            logger.error(f"Search failed: {resp.status_code} {resp.text}")
            return {"hits": [], "estimatedTotalHits": 0}
        return resp.json()

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


def _ts(date_str: str) -> int:
    """Convert date string to Unix timestamp for Meilisearch filtering."""
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return 0


# ── RQ Job ─────────────────────────────────────────────────────

def index_document(tenant_id: str, document_id: str):
    """
    RQ job: Index a single document in Meilisearch.
    Called after OCR completes.
    """
    from core.db.base import TenantSession, get_session_factory

    session = TenantSession(get_session_factory()(), tenant_id)

    doc = session.execute(
        sa_text("""SELECT d.id, d.title, d.storage_path, d.doc_type, d.file_size,
                  d.ocr_text, d.checksum, d.created_at, d.updated_at,
                  d.is_deleted, d.matter_id,
                  m.matter_name as matter_name, m.matter_number as matter_number,
                  c.client_name as client_name
           FROM documents d
           LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
           LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
           WHERE d.id = :id AND d.tenant_id = :tid"""),
        {"id": document_id, "tid": tenant_id},
    ).fetchone()

    if not doc:
        logger.warning(f"Document {document_id} not found for indexing")
        return

    # Build index document
    index_data = {
        "id": doc["id"],
        "name": doc["title"],
        "storage_path": doc["storage_path"],
        "file_type": doc["doc_type"] or "",
        "file_size": doc["file_size"] or 0,
        "ocr_text": (doc["ocr_text"] or "")[:100_000],  # Cap text for index
        "checksum": doc["checksum"] or "",
        "matter_id": doc["matter_id"] or "",
        "matter_name": doc.get("matter_name") or "",
        "matter_number": doc.get("matter_number") or "",
        "client_name": doc.get("client_name") or "",
        "created_at": _ts(doc["created_at"]) if doc["created_at"] else 0,
        "modified_at": _ts(doc["modified_at"]) if doc["modified_at"] else 0,
        "is_deleted": bool(doc["is_deleted"]),
        "tags": [],
    }

    # Sync call to Meilisearch (RQ jobs are sync)
    meili_url = os.environ["MEILISEARCH_URL"]
    meili_key = os.environ.get("MEILISEARCH_MASTER_KEY", "")
    index_name = f"documents_{tenant_id}"

    headers = {"Content-Type": "application/json"}
    if meili_key:
        headers["Authorization"] = f"Bearer {meili_key}"

    resp = httpx.post(
        f"{meili_url}/indexes/{index_name}/documents",
        json=[index_data],
        headers=headers,
        timeout=30,
    )

    if resp.status_code in (200, 202):
        logger.info(f"Indexed document {document_id} in {index_name}")
    else:
        logger.error(f"Index failed: {resp.status_code} {resp.text}")
