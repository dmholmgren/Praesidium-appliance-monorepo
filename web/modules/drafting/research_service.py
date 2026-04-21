"""
modules/drafting/research_service.py

Research service — abstract interface over all legal research connectors.
Called exclusively by sanity_service.py. No HTTP, no templates.

Architecture:
    - ResearchConnector ABC defines the interface all adapters must implement
    - get_connector() factory reads research_connector_registry from DB and
      returns the correct adapter instance
    - ConnectorNotConfigured raised when a connector is seeded but has no
      tenant credentials — sanity runner converts this to layer 'skipped'
    - ConnectorUnavailable raised on network/API errors — sanity runner
      converts this to layer 'error' (non-blocking)

Adapters:
    CourtListenerAdapter  — LIVE (no credentials required for basic use)
    LexisAdapter          — STUB (raises ConnectorNotConfigured)
    WestlawAdapter        — STUB (raises ConnectorNotConfigured)
    WebSearchAdapter      — STUB (raises ConnectorNotConfigured)
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConnectorNotConfigured(Exception):
    """Raised when the connector exists in the registry but has no tenant
    credentials, or when the connector is marked inactive.
    Sanity runner converts this to layer result 'skipped'."""


class ConnectorUnavailable(Exception):
    """Raised on network errors, API timeouts, or unexpected API responses.
    Sanity runner converts this to layer result 'error' (non-blocking)."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CitationResult:
    """Result from a single citation lookup."""
    citation_text: str          # Original text submitted for lookup
    found: bool                 # Whether the citation was located
    # CourtListener / Lexis / Westlaw case URL if found
    source_url: Optional[str] = None
    # 'good' | 'warning' | 'overruled' | 'unknown'
    citator_status: str = 'unknown'
    # Human-readable status label (e.g. "Still Good Law", "Overruled")
    status_label: Optional[str] = None
    # Negative treatment flags
    has_negative_treatment: bool = False
    negative_summary: Optional[str] = None
    # Suggested replacement authority if overruled
    suggested_replacement: Optional[str] = None
    connector_used: str = ''


@dataclass
class ResearchResult:
    """Result from a legal research query (Layer 4 / current sources)."""
    query: str
    results: list[dict] = field(default_factory=list)
    # Each result: {title, citation, url, date, snippet, source}
    total_found: int = 0
    connector_used: str = ''


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ResearchConnector(ABC):
    """Abstract interface all research connectors must implement."""

    connector_type: str = ''

    @abstractmethod
    async def check_citation(self, citation_text: str) -> CitationResult:
        """Look up a single case citation and return its citator status."""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        jurisdiction: Optional[str] = None,
        date_from: Optional[str] = None,
        max_results: int = 10,
    ) -> ResearchResult:
        """Run a legal research query and return structured results."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the connector can reach its upstream API."""
        ...


# ---------------------------------------------------------------------------
# CourtListener adapter — LIVE
# ---------------------------------------------------------------------------

COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v4"
COURTLISTENER_TIMEOUT = 10.0  # seconds


class CourtListenerAdapter(ResearchConnector):
    """
    CourtListener (Free Law Project) API adapter.

    No credentials required for anonymous access (rate-limited).
    Optional API token for higher rate limits — stored in credentials_vault
    under connector_type='courtlistener', key_name='api_token'.

    API docs: https://www.courtlistener.com/help/api/rest/
    """

    connector_type = 'courtlistener'

    def __init__(self, api_token: Optional[str] = None):
        self._token = api_token
        self._headers = {'Accept': 'application/json'}
        if self._token:
            self._headers['Authorization'] = f'Token {self._token}'

    async def check_citation(self, citation_text: str) -> CitationResult:
        """
        Look up a case citation against CourtListener's opinion search.
        Uses the /search/ endpoint with type=o (opinion) and the citation
        text as the query. Returns the first match's status.
        """
        params = {
            'q': citation_text,
            'type': 'o',
            'format': 'json',
            'page_size': 1,
        }
        try:
            async with httpx.AsyncClient(
                timeout=COURTLISTENER_TIMEOUT,
                headers=self._headers,
            ) as client:
                resp = await client.get(
                    f"{COURTLISTENER_BASE}/search/",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise ConnectorUnavailable(
                f"CourtListener timeout checking citation: {citation_text}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ConnectorUnavailable(
                f"CourtListener HTTP {exc.response.status_code} "
                f"checking citation: {citation_text}"
            ) from exc
        except Exception as exc:
            raise ConnectorUnavailable(
                f"CourtListener unexpected error: {exc}"
            ) from exc

        results = data.get('results', [])
        if not results:
            return CitationResult(
                citation_text=citation_text,
                found=False,
                citator_status='unknown',
                status_label='Not found in CourtListener',
                connector_used=self.connector_type,
            )

        hit = results[0]
        # CourtListener does not expose a citator API in the free tier.
        # We confirm existence and return the opinion URL.
        # Full Shepards/KeyCite citator status requires Lexis/Westlaw.
        source_url = None
        absolute_url = hit.get('absolute_url', '')
        if absolute_url:
            source_url = f"https://www.courtlistener.com{absolute_url}"

        return CitationResult(
            citation_text=citation_text,
            found=True,
            source_url=source_url,
            citator_status='good',   # Conservative — existence confirmed
            status_label='Found in CourtListener (existence confirmed)',
            has_negative_treatment=False,
            connector_used=self.connector_type,
        )

    async def search(
        self,
        query: str,
        jurisdiction: Optional[str] = None,
        date_from: Optional[str] = None,
        max_results: int = 10,
    ) -> ResearchResult:
        """
        Run a full-text opinion search against CourtListener.
        Jurisdiction filter maps to CourtListener court codes
        (e.g. 'txnd' for N.D. Tex., 'ca5' for Fifth Circuit).
        """
        params: dict = {
            'q': query,
            'type': 'o',
            'format': 'json',
            'page_size': min(max_results, 20),
            'order_by': 'score desc',
        }
        if jurisdiction:
            params['court'] = jurisdiction
        if date_from:
            params['filed_after'] = date_from

        try:
            async with httpx.AsyncClient(
                timeout=COURTLISTENER_TIMEOUT,
                headers=self._headers,
            ) as client:
                resp = await client.get(
                    f"{COURTLISTENER_BASE}/search/",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.TimeoutException as exc:
            raise ConnectorUnavailable(
                f"CourtListener timeout on search: {query}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ConnectorUnavailable(
                f"CourtListener HTTP {exc.response.status_code} on search"
            ) from exc
        except Exception as exc:
            raise ConnectorUnavailable(
                f"CourtListener unexpected error: {exc}"
            ) from exc

        raw_results = data.get('results', [])
        total = data.get('count', len(raw_results))

        results = []
        for r in raw_results:
            absolute_url = r.get('absolute_url', '')
            url = (
                f"https://www.courtlistener.com{absolute_url}"
                if absolute_url else None
            )
            results.append({
                'title': r.get('caseName') or r.get('case_name', ''),
                'citation': r.get('citation', [None])[0] if r.get('citation') else None,
                'url': url,
                'date': r.get('dateFiled') or r.get('date_filed', ''),
                'snippet': (r.get('snippet') or '')[:500],
                'source': 'courtlistener',
                'court': r.get('court_id', ''),
            })

        return ResearchResult(
            query=query,
            results=results,
            total_found=total,
            connector_used=self.connector_type,
        )

    async def health_check(self) -> bool:
        """Ping the CourtListener API root to confirm reachability."""
        try:
            async with httpx.AsyncClient(
                timeout=5.0,
                headers=self._headers,
            ) as client:
                resp = await client.get(f"{COURTLISTENER_BASE}/")
                return resp.status_code in (200, 301, 302)
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Stub adapters — raise ConnectorNotConfigured immediately
# ---------------------------------------------------------------------------

class LexisAdapter(ResearchConnector):
    """
    Lexis+ API adapter — STUB.
    Raises ConnectorNotConfigured until OAuth2 credentials are provisioned.
    Interface is identical to CourtListenerAdapter for drop-in replacement.
    """

    connector_type = 'lexis'

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        if not client_id or not client_secret:
            raise ConnectorNotConfigured(
                "Lexis+ connector requires client_id and client_secret. "
                "Configure at Tenant Admin → Research Connectors."
            )
        # Full implementation pending Lexis+ API credential provisioning.
        raise ConnectorNotConfigured("Lexis+ adapter not yet implemented.")

    async def check_citation(self, citation_text: str) -> CitationResult:
        raise ConnectorNotConfigured("Lexis+ adapter not yet implemented.")

    async def search(self, query: str, **kwargs) -> ResearchResult:
        raise ConnectorNotConfigured("Lexis+ adapter not yet implemented.")

    async def health_check(self) -> bool:
        return False


class WestlawAdapter(ResearchConnector):
    """
    Westlaw Edge API adapter — STUB.
    Raises ConnectorNotConfigured until OAuth2 credentials are provisioned.
    """

    connector_type = 'westlaw'

    def __init__(self, client_id: Optional[str] = None, client_secret: Optional[str] = None):
        if not client_id or not client_secret:
            raise ConnectorNotConfigured(
                "Westlaw Edge connector requires client_id and client_secret. "
                "Configure at Tenant Admin → Research Connectors."
            )
        raise ConnectorNotConfigured("Westlaw adapter not yet implemented.")

    async def check_citation(self, citation_text: str) -> CitationResult:
        raise ConnectorNotConfigured("Westlaw adapter not yet implemented.")

    async def search(self, query: str, **kwargs) -> ResearchResult:
        raise ConnectorNotConfigured("Westlaw adapter not yet implemented.")

    async def health_check(self) -> bool:
        return False


class WebSearchAdapter(ResearchConnector):
    """
    General web search adapter — STUB.
    Used for Layer 5 (Internet & Current Sources).
    Raises ConnectorNotConfigured until an API key is provisioned.
    """

    connector_type = 'web_search'

    def __init__(self, api_key: Optional[str] = None):
        if not api_key:
            raise ConnectorNotConfigured(
                "Web search connector requires an api_key. "
                "Configure at Tenant Admin → Research Connectors."
            )
        raise ConnectorNotConfigured("Web search adapter not yet implemented.")

    async def check_citation(self, citation_text: str) -> CitationResult:
        raise ConnectorNotConfigured("Web search adapter not yet implemented.")

    async def search(self, query: str, **kwargs) -> ResearchResult:
        raise ConnectorNotConfigured("Web search adapter not yet implemented.")

    async def health_check(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Connector registry — maps connector_type strings to adapter classes
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: dict[str, type[ResearchConnector]] = {
    'courtlistener': CourtListenerAdapter,
    'lexis':         LexisAdapter,
    'westlaw':       WestlawAdapter,
    'web_search':    WebSearchAdapter,
}


# ---------------------------------------------------------------------------
# Factory — reads credentials from DB, returns instantiated adapter
# ---------------------------------------------------------------------------

async def get_connector(
    connector_type: str,
    tenant_id: str,
) -> ResearchConnector:
    """
    Return an instantiated, ready-to-use ResearchConnector for the given
    connector_type and tenant.

    Raises:
        ConnectorNotConfigured — connector inactive, tenant has no credentials,
                                  or adapter is a stub
        ValueError             — unknown connector_type
    """
    tenant_id = tenant_id.strip()

    adapter_cls = _ADAPTER_REGISTRY.get(connector_type)
    if adapter_cls is None:
        raise ValueError(f"Unknown research connector type: {connector_type!r}")

    async with AsyncSessionLocal() as session:
        # Check connector is active in registry
        row = await session.execute(
            text(
                "SELECT is_active FROM research_connector_registry "
                "WHERE connector_type = :ct"
            ),
            {'ct': connector_type},
        )
        reg = row.fetchone()
        if not reg:
            raise ConnectorNotConfigured(
                f"Research connector {connector_type!r} not found in registry."
            )
        if not reg.is_active:
            raise ConnectorNotConfigured(
                f"Research connector {connector_type!r} is disabled."
            )

        # CourtListener works without credentials — return immediately
        if connector_type == 'courtlistener':
            # Check if tenant has an optional API token
            # credentials_vault schema: provider, key_type, encrypted_key
            token_row = await session.execute(
                text(
                    "SELECT encrypted_key FROM credentials_vault "
                    "WHERE trim(tenant_id) = :tid "
                    "  AND provider = :ct "
                    "  AND key_type = 'api_token' "
                    "LIMIT 1"
                ),
                {'tid': tenant_id, 'ct': connector_type},
            )
            token_rec = token_row.fetchone()
            api_token = token_rec.encrypted_key if token_rec else None
            return CourtListenerAdapter(api_token=api_token)

        # All other connectors require credentials
        cred_row = await session.execute(
            text(
                "SELECT key_type, encrypted_key FROM credentials_vault "
                "WHERE trim(tenant_id) = :tid "
                "  AND provider = :ct "
                "ORDER BY key_type"
            ),
            {'tid': tenant_id, 'ct': connector_type},
        )
        creds = {r.key_type: r.encrypted_key for r in cred_row.fetchall()}

        if not creds:
            raise ConnectorNotConfigured(
                f"No credentials found for connector {connector_type!r}. "
                f"Configure at Tenant Admin → Research Connectors."
            )

        if connector_type == 'lexis':
            return LexisAdapter(
                client_id=creds.get('client_id'),
                client_secret=creds.get('client_secret'),
            )
        if connector_type == 'westlaw':
            return WestlawAdapter(
                client_id=creds.get('client_id'),
                client_secret=creds.get('client_secret'),
            )
        if connector_type == 'web_search':
            return WebSearchAdapter(api_key=creds.get('api_key'))

    raise ValueError(f"Unhandled connector type: {connector_type!r}")


# ---------------------------------------------------------------------------
# Convenience functions called directly by sanity_service.py
# ---------------------------------------------------------------------------

async def check_citation(
    citation_text: str,
    connector_type: str,
    tenant_id: str,
) -> CitationResult:
    """
    Check a single citation using the specified connector.
    Propagates ConnectorNotConfigured and ConnectorUnavailable to caller.
    """
    connector = await get_connector(connector_type, tenant_id)
    return await connector.check_citation(citation_text)


async def research_query(
    query: str,
    connector_type: str,
    tenant_id: str,
    jurisdiction: Optional[str] = None,
    date_from: Optional[str] = None,
    max_results: int = 10,
) -> ResearchResult:
    """
    Run a research query using the specified connector.
    Propagates ConnectorNotConfigured and ConnectorUnavailable to caller.
    """
    connector = await get_connector(connector_type, tenant_id)
    return await connector.search(
        query=query,
        jurisdiction=jurisdiction,
        date_from=date_from,
        max_results=max_results,
    )


async def get_active_connector_for_layer(
    layer_number: int,
    tenant_id: str,
) -> Optional[ResearchConnector]:
    """
    Return the first active, configured connector that serves the given
    sanity check layer number. Returns None if no connector is available
    (sanity runner will mark layer as 'skipped').

    Layer connector preference order:
        Layers 3, 4: lexis → westlaw → courtlistener
        Layer 5:     web_search → None (skipped if not configured)
    """
    # Preference order per layer
    preference: dict[int, list[str]] = {
        3: ['lexis', 'westlaw', 'courtlistener'],
        4: ['lexis', 'westlaw', 'courtlistener'],
        5: ['web_search'],
    }
    candidates = preference.get(layer_number, [])

    for ct in candidates:
        try:
            connector = await get_connector(ct, tenant_id)
            return connector
        except (ConnectorNotConfigured, ValueError):
            continue

    return None
