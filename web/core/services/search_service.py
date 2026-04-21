# core/services/search_service.py
# Praesidium Series 2.0
# Elasticsearch 8 — replaces Meilisearch entirely.
# All references to meilisearch, MEILISEARCH_URL, MEILISEARCH_API_KEY removed.
# Application code must NEVER import meilisearch or call Meilisearch APIs.
# All search goes through this service class.

from __future__ import annotations

import logging
import os
from typing import Any

from elasticsearch import AsyncElasticsearch, NotFoundError

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
# Was: MEILISEARCH_URL / MEILISEARCH_API_KEY
# Now: ELASTICSEARCH_URL (no API key needed in dev; configure xpack in prod)
ELASTICSEARCH_URL: str = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")

# Index name prefix — all indices are namespaced by tenant_id at query time.
INDEX_DOCUMENTS = "praesidium_documents"
INDEX_MATTERS   = "praesidium_matters"
INDEX_CLIENTS   = "praesidium_clients"


class SearchService:
    """Elasticsearch 8 service — wraps all search operations.

    Replaces MeilisearchService from Series 1.0.
    All application code must use this class — never import elasticsearch directly.
    """

    _client: AsyncElasticsearch | None = None

    @classmethod
    def client(cls) -> AsyncElasticsearch:
        if cls._client is None:
            cls._client = AsyncElasticsearch(
                hosts=[ELASTICSEARCH_URL],
                retry_on_timeout=True,
                max_retries=3,
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        if cls._client is not None:
            await cls._client.close()
            cls._client = None

    # ── Index setup ──────────────────────────────────────────────────────────

    @classmethod
    async def ensure_indices(cls) -> None:
        """Create indices with mappings if they do not exist.
        Called at application startup (lifespan handler).
        """
        es = cls.client()

        document_mapping = {
            "mappings": {
                "properties": {
                    "tenant_id":       {"type": "keyword"},
                    "matter_id":       {"type": "keyword"},
                    "filename":        {"type": "text", "analyzer": "english"},
                    "extracted_text":  {"type": "text", "analyzer": "english"},
                    "document_type":   {"type": "keyword"},
                    "status":          {"type": "keyword"},
                    "created_at":      {"type": "date"},
                    # kNN vector field for semantic search (pgvector handles storage;
                    # this enables ES-side similarity for cross-document retrieval)
                    "embedding": {
                        "type": "dense_vector",
                        "dims": 1536,
                        "index": True,
                        "similarity": "cosine",
                    },
                }
            },
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
                "analysis": {
                    "analyzer": {
                        "legal_text": {
                            "type": "custom",
                            "tokenizer": "standard",
                            "filter": ["lowercase", "stop", "snowball"],
                        }
                    }
                },
            },
        }

        matter_mapping = {
            "mappings": {
                "properties": {
                    "tenant_id":     {"type": "keyword"},
                    "matter_number": {"type": "keyword"},
                    "matter_name":   {"type": "text", "analyzer": "english"},
                    "practice_area": {"type": "keyword"},
                    "status":        {"type": "keyword"},
                    "client_name":   {"type": "text"},
                    "created_at":    {"type": "date"},
                }
            },
            "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        }

        for index, body in [
            (INDEX_DOCUMENTS, document_mapping),
            (INDEX_MATTERS, matter_mapping),
        ]:
            if not await es.indices.exists(index=index):
                await es.indices.create(index=index, body=body)
                logger.info("SearchService: created index %s", index)

    # ── Indexing ─────────────────────────────────────────────────────────────

    @classmethod
    async def index_document(
        cls,
        tenant_id: str,
        document_id: str,
        data: dict[str, Any],
    ) -> None:
        """Index or update a document in Elasticsearch."""
        es = cls.client()
        doc = {"tenant_id": tenant_id, **data}
        await es.index(
            index=INDEX_DOCUMENTS,
            id=f"{tenant_id}:{document_id}",
            document=doc,
        )

    @classmethod
    async def delete_document(cls, tenant_id: str, document_id: str) -> None:
        """Remove a document from the index."""
        es = cls.client()
        try:
            await es.delete(
                index=INDEX_DOCUMENTS,
                id=f"{tenant_id}:{document_id}",
            )
        except NotFoundError:
            pass

    @classmethod
    async def index_matter(
        cls,
        tenant_id: str,
        matter_id: str,
        data: dict[str, Any],
    ) -> None:
        es = cls.client()
        doc = {"tenant_id": tenant_id, **data}
        await es.index(
            index=INDEX_MATTERS,
            id=f"{tenant_id}:{matter_id}",
            document=doc,
        )

    # ── Search ───────────────────────────────────────────────────────────────

    @classmethod
    async def search_documents(
        cls,
        tenant_id: str,
        query: str,
        matter_id: str | None = None,
        document_type: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Full-text search over documents for a tenant.

        All queries are tenant-scoped — tenant_id filter is always applied first.
        """
        es = cls.client()

        must: list[dict] = [{"term": {"tenant_id": tenant_id}}]
        if matter_id:
            must.append({"term": {"matter_id": matter_id}})
        if document_type:
            must.append({"term": {"document_type": document_type}})

        should: list[dict] = []
        if query:
            should = [
                {"match": {"extracted_text": {"query": query, "boost": 1.0}}},
                {"match": {"filename": {"query": query, "boost": 2.0}}},
            ]

        es_query: dict[str, Any] = {
            "bool": {
                "must": must,
                "should": should if should else [{"match_all": {}}],
                "minimum_should_match": 1 if should else 0,
            }
        }

        response = await es.search(
            index=INDEX_DOCUMENTS,
            query=es_query,
            from_=offset,
            size=limit,
            highlight={
                "fields": {
                    "extracted_text": {"number_of_fragments": 3, "fragment_size": 200}
                }
            },
        )

        hits = response["hits"]
        return {
            "total": hits["total"]["value"],
            "results": [
                {
                    "id": hit["_source"].get("document_id", hit["_id"].split(":")[-1]),
                    "score": hit["_score"],
                    "filename": hit["_source"].get("filename"),
                    "document_type": hit["_source"].get("document_type"),
                    "highlight": hit.get("highlight", {}).get("extracted_text", []),
                }
                for hit in hits["hits"]
            ],
        }

    @classmethod
    async def semantic_search(
        cls,
        tenant_id: str,
        embedding: list[float],
        matter_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """kNN vector similarity search for semantic retrieval (RAG context building).

        Uses the dense_vector field mapped in ensure_indices().
        """
        es = cls.client()

        filter_clause: list[dict] = [{"term": {"tenant_id": tenant_id}}]
        if matter_id:
            filter_clause.append({"term": {"matter_id": matter_id}})

        response = await es.search(
            index=INDEX_DOCUMENTS,
            knn={
                "field": "embedding",
                "query_vector": embedding,
                "k": limit,
                "num_candidates": limit * 5,
                "filter": filter_clause,
            },
            size=limit,
        )

        hits = response["hits"]
        return {
            "total": hits["total"]["value"],
            "results": [
                {
                    "id": hit["_id"].split(":")[-1],
                    "score": hit["_score"],
                    "filename": hit["_source"].get("filename"),
                    "extracted_text_snippet": (hit["_source"].get("extracted_text") or "")[:500],
                }
                for hit in hits["hits"]
            ],
        }

    @classmethod
    async def search_matters(
        cls,
        tenant_id: str,
        query: str,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        es = cls.client()
        must: list[dict] = [{"term": {"tenant_id": tenant_id}}]
        if status:
            must.append({"term": {"status": status}})
        if query:
            must.append({
                "multi_match": {
                    "query": query,
                    "fields": ["matter_name^2", "matter_number", "client_name"],
                }
            })

        response = await es.search(
            index=INDEX_MATTERS,
            query={"bool": {"must": must}},
            size=limit,
        )
        hits = response["hits"]
        return {
            "total": hits["total"]["value"],
            "results": [hit["_source"] for hit in hits["hits"]],
        }

    @classmethod
    async def delete_tenant_data(cls, tenant_id: str) -> None:
        """Remove all indexed data for a tenant (used on tenant deletion/offboarding)."""
        es = cls.client()
        for index in [INDEX_DOCUMENTS, INDEX_MATTERS]:
            await es.delete_by_query(
                index=index,
                query={"term": {"tenant_id": tenant_id}},
            )
        logger.info("SearchService: deleted all data for tenant_id=%s", tenant_id)
