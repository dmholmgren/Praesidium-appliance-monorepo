"""
COMP 2 — PACER Integration Service.

PACER Next Gen: single credential set covers all federal districts.
Periodic docket monitoring on all active federal matters.
Auto-download new filings to DMS.
CourtListener RECAP supplement for free documents.

All external calls via core/services/ interfaces.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from core.audit import write_audit
from core.db.tenant_session import TenantSession
from core.services.storage import StorageService
from modules.court.models import DocketEntry

logger = logging.getLogger(__name__)

# Federal district court identifiers
TEXAS_FEDERAL_DISTRICTS = ["txnd", "txsd", "txed", "txwd"]
CALIFORNIA_FEDERAL_DISTRICTS = ["cand", "cacd", "caed", "casd"]
COLORADO_FEDERAL_DISTRICTS = ["cod"]

ALL_MONITORED_DISTRICTS = (
    TEXAS_FEDERAL_DISTRICTS
    + CALIFORNIA_FEDERAL_DISTRICTS
    + COLORADO_FEDERAL_DISTRICTS
)


class PACERClient:
    """
    PACER Next Gen API client.

    Credentials loaded from tenant credential vault — never hardcoded.
    All network calls are made from this client, injected via dependency.
    """

    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None

    async def authenticate(self) -> str:
        """Authenticate with PACER Next Gen and return session token."""
        # Implementation calls PACER auth endpoint
        # POST {base_url}/login
        # Returns JWT token for subsequent requests
        raise NotImplementedError("Wire to PACER Next Gen auth endpoint")

    async def search_cases(
        self, district: str, case_number: Optional[str] = None,
        date_from: Optional[date] = None, date_to: Optional[date] = None,
    ) -> list[dict]:
        """Search for cases in a federal district."""
        raise NotImplementedError("Wire to PACER case search API")

    async def get_docket_entries(
        self, district: str, case_id: str,
        date_from: Optional[date] = None,
    ) -> list[dict]:
        """Get docket entries for a specific case."""
        raise NotImplementedError("Wire to PACER docket report API")

    async def download_document(
        self, district: str, case_id: str, document_number: str,
    ) -> bytes:
        """Download a document from PACER (incurs per-page fee)."""
        raise NotImplementedError("Wire to PACER document download API")


class CourtListenerClient:
    """
    CourtListener RECAP API client — free supplement.

    Checks if a document is available for free via RECAP before
    downloading from PACER (which incurs fees).
    """

    def __init__(self, api_token: str):
        self._api_token = api_token

    async def check_recap_availability(
        self, court: str, pacer_case_id: str, document_number: str,
    ) -> Optional[str]:
        """Check if document is available in RECAP archive. Returns URL or None."""
        raise NotImplementedError("Wire to CourtListener RECAP API")

    async def download_recap_document(self, recap_url: str) -> bytes:
        """Download a free document from RECAP archive."""
        raise NotImplementedError("Wire to CourtListener download")


async def poll_federal_docket(
    tenant_id: str,
    matter_id: int,
    district: str,
    case_number: str,
    last_check_date: date,
    db: TenantSession,
    pacer_client: PACERClient,
    courtlistener_client: CourtListenerClient,
    storage_service: StorageService,
) -> list[DocketEntry]:
    """
    Poll a single federal case for new docket entries since last check.

    1. Query PACER for new docket entries
    2. For each new entry, check RECAP for free download
    3. If not in RECAP, download from PACER
    4. Store document in DMS via StorageService
    5. Create docket_entries record

    Returns list of new DocketEntry records created.
    """
    new_entries = []

    try:
        pacer_entries = await pacer_client.get_docket_entries(
            district=district,
            case_id=case_number,
            date_from=last_check_date,
        )
    except Exception as e:
        logger.error(f"PACER docket pull failed for {district}/{case_number}: {e}")
        return new_entries

    for pacer_entry in pacer_entries:
        entry_number = pacer_entry.get("entry_number")
        entry_date_str = pacer_entry.get("date_filed")
        entry_text = pacer_entry.get("description", "")

        # Check if we already have this entry
        existing = db.query_first(
            "docket_entries",
            filters={
                "matter_id": matter_id,
                "source": "pacer",
                "docket_number": str(entry_number),
            },
        )
        if existing:
            continue

        # Classify entry type
        entry_type = _classify_federal_entry(entry_text)

        # Attempt document download
        document_id = None
        doc_number = pacer_entry.get("document_number")
        if doc_number:
            document_id = await _download_federal_document(
                tenant_id=tenant_id,
                district=district,
                case_number=case_number,
                doc_number=doc_number,
                entry_text=entry_text,
                pacer_client=pacer_client,
                courtlistener_client=courtlistener_client,
                storage_service=storage_service,
            )

        entry = DocketEntry(
            tenant_id=tenant_id,
            matter_id=matter_id,
            source="pacer",
            court_system=f"US District Court — {district.upper()}",
            case_number=case_number,
            docket_number=str(entry_number) if entry_number else None,
            entry_date=_parse_date(entry_date_str),
            entry_text=entry_text,
            entry_type=entry_type,
            document_id=document_id,
            raw_data=pacer_entry,
            processed=False,
        )
        db.add(entry)
        db.flush()

        write_audit(
            tenant_id=tenant_id,
            table_name="docket_entries",
            record_id=entry.id,
            action="create",
            details={"source": "pacer", "district": district, "entry_type": entry_type},
        )

        new_entries.append(entry)

    logger.info(f"PACER poll {district}/{case_number}: {len(new_entries)} new entries")
    return new_entries


async def _download_federal_document(
    tenant_id: str,
    district: str,
    case_number: str,
    doc_number: str,
    entry_text: str,
    pacer_client: PACERClient,
    courtlistener_client: CourtListenerClient,
    storage_service: StorageService,
) -> Optional[int]:
    """Download federal court document, preferring free RECAP when available."""
    content = None

    # Try RECAP first (free)
    try:
        recap_url = await courtlistener_client.check_recap_availability(
            court=district, pacer_case_id=case_number, document_number=doc_number,
        )
        if recap_url:
            content = await courtlistener_client.download_recap_document(recap_url)
            logger.info(f"Downloaded from RECAP: {district}/{case_number}/{doc_number}")
    except Exception as e:
        logger.debug(f"RECAP check failed (will try PACER): {e}")

    # Fall back to PACER (paid)
    if content is None:
        try:
            content = await pacer_client.download_document(
                district=district, case_id=case_number, document_number=doc_number,
            )
            logger.info(f"Downloaded from PACER: {district}/{case_number}/{doc_number}")
        except Exception as e:
            logger.error(f"PACER download failed: {district}/{case_number}/{doc_number}: {e}")
            return None

    # Store to DMS
    try:
        stored = await storage_service.store(
            tenant_id=tenant_id,
            path=f"court_filings/{case_number}/dkt_{doc_number}.pdf",
            content=content,
            metadata={
                "source": "pacer",
                "district": district,
                "case_number": case_number,
                "docket_number": doc_number,
            },
        )
        return stored.get("document_id")
    except Exception as e:
        logger.error(f"Failed to store federal document: {e}")
        return None


def _classify_federal_entry(text: str) -> str:
    """Classify a federal docket entry into a type."""
    text_lower = text.lower()
    if "scheduling order" in text_lower or "case management order" in text_lower:
        return "scheduling_order"
    if "order" in text_lower:
        return "order"
    if "motion" in text_lower or "brief" in text_lower or "memorandum" in text_lower:
        return "filing"
    if "notice" in text_lower:
        return "notice"
    if "summons" in text_lower or "service" in text_lower:
        return "service"
    return "filing"


def _parse_date(date_str: Optional[str]) -> date:
    """Parse a date string from PACER, defaulting to today."""
    if not date_str:
        return datetime.now(timezone.utc).date()
    try:
        return datetime.strptime(date_str, "%m/%d/%Y").date()
    except ValueError:
        try:
            return datetime.fromisoformat(date_str).date()
        except ValueError:
            return datetime.now(timezone.utc).date()
