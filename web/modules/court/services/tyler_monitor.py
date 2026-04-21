"""
COMP 1 — Tyler/Odyssey Email Monitor Service.

Monitors incoming emails via EmailService for Tyler/Odyssey notifications.
Auto-detects service of process, new filings, docket updates.
Downloads documents to DMS via StorageService.

All external calls via core/services/ interfaces only.
All DB writes via write_audit().
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from core.audit import write_audit
from core.db.base import TenantSession
from core.services.email import EmailService
from core.services.storage import StorageService
from modules.court.models import DocketEntry

logger = logging.getLogger(__name__)

# Tyler/Odyssey sender patterns — loaded from env via tenant config
TYLER_SENDER_PATTERNS = [
    r"@tylerhost\.net$",
    r"@odyssey\..*\.courts\.state",
    r"@efiletexas\.gov$",
    r"noreply@.*tyler.*",
]

# Classification patterns for email subjects/bodies
CLASSIFICATION_PATTERNS = {
    "service": [
        r"service\s+of\s+process",
        r"you\s+have\s+been\s+served",
        r"citation\s+by\s+publication",
        r"electronic\s+service",
    ],
    "filing": [
        r"new\s+filing",
        r"document\s+filed",
        r"filing\s+accepted",
        r"filing\s+received",
    ],
    "order": [
        r"order\s+(signed|entered|issued)",
        r"scheduling\s+order",
        r"court\s+order",
    ],
    "scheduling_order": [
        r"scheduling\s+order",
        r"docket\s+control\s+order",
        r"case\s+management\s+order",
    ],
    "notice": [
        r"notice\s+of\s+hearing",
        r"notice\s+of\s+setting",
        r"notice\s+of\s+trial",
        r"docket\s+update",
    ],
}


def classify_tyler_email(subject: str, body: str) -> Optional[str]:
    """Classify a Tyler/Odyssey email into an entry type."""
    text = f"{subject} {body}".lower()

    # Check scheduling_order first (more specific than generic order)
    for pattern in CLASSIFICATION_PATTERNS["scheduling_order"]:
        if re.search(pattern, text):
            return "scheduling_order"

    for entry_type, patterns in CLASSIFICATION_PATTERNS.items():
        if entry_type == "scheduling_order":
            continue
        for pattern in patterns:
            if re.search(pattern, text):
                return entry_type

    return "notice"  # Default to notice for unclassified Tyler emails


def extract_case_info(subject: str, body: str) -> dict:
    """Extract case number and court from Tyler/Odyssey email content."""
    text = f"{subject}\n{body}"
    info = {}

    # Case number patterns (Texas state: e.g., 2024-12345-A)
    case_patterns = [
        r"[Cc]ause\s*(?:#|[Nn]o\.?|[Nn]umber)\s*:?\s*([\w\-]+)",
        r"[Cc]ase\s*(?:#|[Nn]o\.?|[Nn]umber)\s*:?\s*([\w\-]+)",
        r"(\d{4}-\d{3,6}-\w+)",
    ]
    for pattern in case_patterns:
        match = re.search(pattern, text)
        if match:
            info["case_number"] = match.group(1).strip()
            break

    # Court name extraction
    court_patterns = [
        r"(\d+\w*\s+(?:District|County)\s+Court[^,\n]*)",
        r"(?:In\s+the\s+)(.+?Court\s+of\s+.+?)(?:\n|,)",
    ]
    for pattern in court_patterns:
        match = re.search(pattern, text)
        if match:
            info["court_system"] = match.group(1).strip()
            break

    return info


async def process_tyler_email(
    tenant_id: str,
    email_message: dict,
    db: TenantSession,
    email_service: EmailService,
    storage_service: StorageService,
) -> Optional[DocketEntry]:
    """
    Process a single Tyler/Odyssey email notification.

    1. Classify the email type
    2. Extract case information
    3. Download any attachments to DMS
    4. Create docket_entries record
    5. If scheduling_order, flag for COMP 5 processing

    Returns the created DocketEntry or None if not a Tyler email.
    """
    sender = email_message.get("from", "")
    subject = email_message.get("subject", "")
    body = email_message.get("body", "")

    # Verify this is a Tyler/Odyssey email
    is_tyler = False
    for pattern in TYLER_SENDER_PATTERNS:
        if re.search(pattern, sender, re.IGNORECASE):
            is_tyler = True
            break

    if not is_tyler:
        return None

    logger.info(f"Processing Tyler email: {subject[:100]}")

    # Classify and extract
    entry_type = classify_tyler_email(subject, body)
    case_info = extract_case_info(subject, body)

    # Download attachments to DMS
    document_id = None
    attachments = email_message.get("attachments", [])
    if attachments:
        for attachment in attachments:
            try:
                # Store via StorageService — will be filed to correct matter folder
                stored = await storage_service.store(
                    tenant_id=tenant_id,
                    path=f"court_filings/{case_info.get('case_number', 'unmatched')}/{attachment['filename']}",
                    content=attachment["content"],
                    metadata={
                        "source": "tyler",
                        "case_number": case_info.get("case_number"),
                        "entry_type": entry_type,
                    },
                )
                document_id = stored.get("document_id")
            except Exception as e:
                logger.error(f"Failed to store Tyler attachment: {e}")

    # Match to matter by case number
    matter_id = None
    if case_info.get("case_number"):
        # Query matters table for matching case number
        matter = db.query_first(
            "matters",
            filters={"case_number": case_info["case_number"]},
        )
        if matter:
            matter_id = matter.id

    if not matter_id:
        logger.warning(f"No matter matched for case {case_info.get('case_number')} — storing as unmatched")
        matter_id = 0  # Will need manual association

    # Create docket entry
    entry = DocketEntry(
        tenant_id=tenant_id,
        matter_id=matter_id,
        source="tyler",
        court_system=case_info.get("court_system", "Texas State Court"),
        case_number=case_info.get("case_number"),
        entry_date=datetime.now(timezone.utc).date(),
        entry_text=f"{subject}\n\n{body[:2000]}",
        entry_type=entry_type,
        document_id=document_id,
        raw_data={"email_id": email_message.get("id"), "from": sender, "subject": subject},
        processed=False,
    )

    db.add(entry)
    db.flush()

    write_audit(
        tenant_id=tenant_id,
        table_name="docket_entries",
        record_id=entry.id,
        action="create",
        details={"source": "tyler", "entry_type": entry_type, "case_number": case_info.get("case_number")},
    )

    # Mark email as read
    try:
        await email_service.mark_read(tenant_id, email_message["id"])
    except Exception as e:
        logger.warning(f"Failed to mark Tyler email as read: {e}")

    logger.info(f"Created docket entry {entry.id} type={entry_type} for case {case_info.get('case_number')}")
    return entry
