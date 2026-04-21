from sqlalchemy import text as sa_text
"""
COMP 9 — LexisAdapter & WestlawAdapter
LegalResearchService implementations.
- API key auth via Authorization header
- Rate limiting with exponential backoff
- Results cached in research_sessions table for 24 hours
- Tenant config determines active provider(s)
"""

import os
import time
import logging
import hashlib
import json
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from core.services.legal_research import (
    LegalResearchService, ResearchResult, CiteCheckResult, CiteSignal,
)

logger = logging.getLogger(__name__)


class LexisAdapter(LegalResearchService):
    """
    LegalResearchService for Lexis+ API.
    Provides: search, Shepards cite_check, get_document, get_citing_refs.
    """

    PROVIDER = "lexis"

    def __init__(self):
        self.base_url = os.environ.get("LEXIS_API_URL", "https://api.lexisnexis.com/v1")
        self.api_key = os.environ.get("LEXIS_API_KEY", "")
        self.timeout = float(os.environ.get("LEXIS_TIMEOUT", "30"))
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request = 0.0
        self._rate_limit_delay = 0.5  # Min seconds between requests

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def _rate_limited_request(self, method, url, **kwargs) -> httpx.Response:
        """Make request with rate limiting and exponential backoff."""
        client = await self._get_client()
        delay = self._rate_limit_delay
        for attempt in range(3):
            elapsed = time.time() - self._last_request
            if elapsed < delay:
                await _async_sleep(delay - elapsed)

            self._last_request = time.time()
            resp = await getattr(client, method)(url, **kwargs)

            if resp.status_code == 429:
                delay *= 2  # Exponential backoff
                logger.warning(f"Lexis rate limited, retrying in {delay}s")
                continue
            return resp

        raise Exception("Lexis API rate limit exceeded after retries")

    async def search(self, query: str, jurisdiction: str = "",
                     date_from: str = "", date_to: str = "",
                     max_results: int = 20) -> list[ResearchResult]:
        params = {"q": query, "limit": max_results}
        if jurisdiction:
            params["jurisdiction"] = jurisdiction
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to

        resp = await self._rate_limited_request("get", "/search", params=params)
        if resp.status_code != 200:
            logger.error(f"Lexis search failed: {resp.status_code}")
            return []

        results = []
        for item in resp.json().get("results", []):
            results.append(ResearchResult(
                citation=item.get("citation", ""),
                title=item.get("title", ""),
                court=item.get("court", ""),
                date=item.get("date", ""),
                relevance_score=item.get("score", 0.0),
                snippet=item.get("snippet", ""),
                cite_signal=_map_signal(item.get("shepards_signal", "")),
                provider=self.PROVIDER,
                provider_doc_id=item.get("documentId", ""),
            ))
        return results

    async def cite_check(self, citation: str) -> CiteCheckResult:
        """Run Shepards citation check."""
        resp = await self._rate_limited_request(
            "get", "/shepards/check",
            params={"citation": citation},
        )
        if resp.status_code != 200:
            return CiteCheckResult(
                citation=citation, signal=CiteSignal.UNKNOWN,
                provider=self.PROVIDER,
            )

        data = resp.json()
        return CiteCheckResult(
            citation=citation,
            signal=_map_signal(data.get("signal", "")),
            treatment=data.get("treatment", ""),
            citing_refs=[
                ResearchResult(
                    citation=ref.get("citation", ""),
                    title=ref.get("title", ""),
                    court=ref.get("court", ""),
                    date=ref.get("date", ""),
                    provider=self.PROVIDER,
                    provider_doc_id=ref.get("documentId", ""),
                )
                for ref in data.get("citingReferences", [])
            ],
            negative_refs=[
                ResearchResult(
                    citation=ref.get("citation", ""),
                    title=ref.get("title", ""),
                    provider=self.PROVIDER,
                )
                for ref in data.get("negativeReferences", [])
            ],
            provider=self.PROVIDER,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_document(self, provider_doc_id: str) -> ResearchResult:
        resp = await self._rate_limited_request(
            "get", f"/documents/{provider_doc_id}",
        )
        if resp.status_code != 200:
            raise Exception(f"Lexis document fetch failed: {resp.status_code}")

        data = resp.json()
        return ResearchResult(
            citation=data.get("citation", ""),
            title=data.get("title", ""),
            court=data.get("court", ""),
            date=data.get("date", ""),
            full_text=data.get("fullText", ""),
            provider=self.PROVIDER,
            provider_doc_id=provider_doc_id,
        )

    async def get_citing_refs(self, citation: str,
                              max_results: int = 50) -> list[ResearchResult]:
        resp = await self._rate_limited_request(
            "get", "/shepards/citing",
            params={"citation": citation, "limit": max_results},
        )
        if resp.status_code != 200:
            return []

        return [
            ResearchResult(
                citation=ref.get("citation", ""),
                title=ref.get("title", ""),
                court=ref.get("court", ""),
                date=ref.get("date", ""),
                provider=self.PROVIDER,
                provider_doc_id=ref.get("documentId", ""),
            )
            for ref in resp.json().get("citingReferences", [])
        ]

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


class WestlawAdapter(LegalResearchService):
    """
    LegalResearchService for Westlaw Edge API (Thomson Reuters).
    Same interface as LexisAdapter — drop-in replacement.
    Methods: search, cite_check (KeyCite), get_document, get_citing_refs.
    """

    PROVIDER = "westlaw"

    def __init__(self):
        self.base_url = os.environ.get("WESTLAW_API_URL", "https://api.westlaw.com/v2")
        self.api_key = os.environ.get("WESTLAW_API_KEY", "")
        self.client_id = os.environ.get("WESTLAW_CLIENT_ID", "")
        self.timeout = float(os.environ.get("WESTLAW_TIMEOUT", "30"))
        self._client: Optional[httpx.AsyncClient] = None
        self._last_request = 0.0
        self._rate_limit_delay = 0.5

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "X-Client-Id": self.client_id,
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def _rate_limited_request(self, method, url, **kwargs) -> httpx.Response:
        client = await self._get_client()
        delay = self._rate_limit_delay
        for attempt in range(3):
            elapsed = time.time() - self._last_request
            if elapsed < delay:
                await _async_sleep(delay - elapsed)
            self._last_request = time.time()
            resp = await getattr(client, method)(url, **kwargs)
            if resp.status_code == 429:
                delay *= 2
                continue
            return resp
        raise Exception("Westlaw API rate limit exceeded")

    async def search(self, query: str, jurisdiction: str = "",
                     date_from: str = "", date_to: str = "",
                     max_results: int = 20) -> list[ResearchResult]:
        params = {"query": query, "count": max_results}
        if jurisdiction:
            params["jurisdiction"] = jurisdiction
        if date_from:
            params["fromDate"] = date_from
        if date_to:
            params["toDate"] = date_to

        resp = await self._rate_limited_request("get", "/search", params=params)
        if resp.status_code != 200:
            return []

        return [
            ResearchResult(
                citation=item.get("cite", ""),
                title=item.get("name", ""),
                court=item.get("court", ""),
                date=item.get("decisionDate", ""),
                relevance_score=item.get("relevance", 0.0),
                snippet=item.get("headnote", ""),
                cite_signal=_map_keycite_signal(item.get("keyCiteStatus", "")),
                provider=self.PROVIDER,
                provider_doc_id=item.get("guid", ""),
            )
            for item in resp.json().get("documents", [])
        ]

    async def cite_check(self, citation: str) -> CiteCheckResult:
        resp = await self._rate_limited_request(
            "get", "/keycite/check",
            params={"cite": citation},
        )
        if resp.status_code != 200:
            return CiteCheckResult(citation=citation, signal=CiteSignal.UNKNOWN,
                                   provider=self.PROVIDER)

        data = resp.json()
        return CiteCheckResult(
            citation=citation,
            signal=_map_keycite_signal(data.get("status", "")),
            treatment=data.get("treatment", ""),
            citing_refs=[
                ResearchResult(
                    citation=ref.get("cite", ""), title=ref.get("name", ""),
                    court=ref.get("court", ""), date=ref.get("date", ""),
                    provider=self.PROVIDER, provider_doc_id=ref.get("guid", ""),
                )
                for ref in data.get("citingReferences", [])
            ],
            provider=self.PROVIDER,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

    async def get_document(self, provider_doc_id: str) -> ResearchResult:
        resp = await self._rate_limited_request("get", f"/documents/{provider_doc_id}")
        if resp.status_code != 200:
            raise Exception(f"Westlaw doc fetch failed: {resp.status_code}")
        data = resp.json()
        return ResearchResult(
            citation=data.get("cite", ""), title=data.get("name", ""),
            court=data.get("court", ""), date=data.get("decisionDate", ""),
            full_text=data.get("text", ""),
            provider=self.PROVIDER, provider_doc_id=provider_doc_id,
        )

    async def get_citing_refs(self, citation: str,
                              max_results: int = 50) -> list[ResearchResult]:
        resp = await self._rate_limited_request(
            "get", "/keycite/citing",
            params={"cite": citation, "count": max_results},
        )
        if resp.status_code != 200:
            return []
        return [
            ResearchResult(
                citation=ref.get("cite", ""), title=ref.get("name", ""),
                court=ref.get("court", ""), date=ref.get("date", ""),
                provider=self.PROVIDER, provider_doc_id=ref.get("guid", ""),
            )
            for ref in resp.json().get("citingReferences", [])
        ]

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ── Helpers ─────────────────────────────────────────────────

def _map_signal(signal: str) -> CiteSignal:
    mapping = {
        "positive": CiteSignal.POSITIVE,
        "cautionary": CiteSignal.CAUTIONARY,
        "warning": CiteSignal.CAUTIONARY,
        "negative": CiteSignal.NEGATIVE,
        "overruled": CiteSignal.OVERRULED,
    }
    return mapping.get(signal.lower(), CiteSignal.NEUTRAL)


def _map_keycite_signal(status: str) -> CiteSignal:
    mapping = {
        "positive": CiteSignal.POSITIVE,
        "caution": CiteSignal.CAUTIONARY,
        "negative": CiteSignal.NEGATIVE,
        "overruled": CiteSignal.OVERRULED,
        "superseded": CiteSignal.NEGATIVE,
    }
    return mapping.get(status.lower(), CiteSignal.NEUTRAL)


async def _async_sleep(seconds: float):
    import asyncio
    await asyncio.sleep(seconds)


# ── Research Session Caching ────────────────────────────────

def cache_research_results(
    tenant_id: str, user_id: str, matter_id: str,
    query: str, provider: str, results: list[ResearchResult],
):
    """Cache search results in research_sessions table for 24 hours."""
    from core.db.base import TenantSession, get_session_factory
    import uuid

    session = TenantSession(get_session_factory()(), tenant_id)
    session_id = str(uuid.uuid4())
    cache_key = hashlib.sha256(f"{query}:{provider}:{matter_id}".encode()).hexdigest()

    session.execute(
        sa_text("""INSERT INTO research_sessions
        (id, tenant_id, user_id, matter_id, provider, query,
         cache_key, results_json, created_at, expires_at)
        VALUES (:id, :tid, :uid, :mid, :provider, :query,
                :key, :results, :created, :expires)"""),
        {
            "id": session_id, "tid": tenant_id, "uid": user_id,
            "mid": matter_id, "provider": provider, "query": query,
            "key": cache_key,
            "results": json.dumps([_result_to_dict(r) for r in results]),
            "created": datetime.now(timezone.utc).isoformat(),
            "expires": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        },
    )
    session.commit()


def get_cached_results(
    tenant_id: str, query: str, provider: str, matter_id: str = "",
) -> Optional[list[dict]]:
    """Check cache for recent results."""

    session = TenantSession(get_session_factory()(), tenant_id)
    cache_key = hashlib.sha256(f"{query}:{provider}:{matter_id}".encode()).hexdigest()

    result = session.execute(
        sa_text("""SELECT results_json FROM research_sessions
        WHERE tenant_id = :tid AND cache_key = :key
        AND expires_at > :now
        ORDER BY created_at DESC LIMIT 1"""),
        {
            "tid": tenant_id, "key": cache_key,
            "now": datetime.now(timezone.utc).isoformat(),
        },
    ).fetchone()

    if result:
        return json.loads(result["results_json"])
    return None


def _result_to_dict(r: ResearchResult) -> dict:
    return {
        "citation": r.citation, "title": r.title, "court": r.court,
        "entry_date": r.date, "relevance_score": r.relevance_score,
        "snippet": r.snippet, "cite_signal": r.cite_signal.value,
        "provider": r.provider, "provider_doc_id": r.provider_doc_id,
    }
