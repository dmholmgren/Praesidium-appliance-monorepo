"""
Global Contact Seeder — Batch extraction across all data sources
================================================================
Sweeps every source of contact data and creates contact records + matter_contact
proposals for the entire tenant. Run as RQ job or one-shot script.

Sources:
  1. Email routing queue (all emails, not just matched) — from_email, to_emails, from_display
  2. Timeslip narratives — regex extraction of "T/C with Name", "email Name", "correspond with Name"
  3. DMS documents (.msg/.eml headers, contract parties, all emails in text)
  4. Calendar events — organizer, attendees, subject names

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import re
import logging
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dashboard/contacts", tags=["contacts-seeder"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


# ─── Name extraction from billing narratives ───────────────────────

# Patterns that precede a person's name in billing narratives
NARRATIVE_NAME_RE = re.compile(
    r'(?:'
    r'[Tt]/[Cc]\s+(?:with|w/)\s+'           # t/c with Name
    r'|[Tt]eleconference\s+with\s+'          # teleconference with Name
    r'|[Cc]onference?\s+with\s+'             # conference with Name / confer with Name
    r'|[Cc]ommunicat\w+\s+(?:with|to)\s+'   # communicate with Name
    r'|[Cc]orrespond\w*\s+(?:with|to|from)\s+'  # correspond with/to/from Name
    r'|[Ee]mail(?:ed|s)?\s+(?:to|from|with)?\s*'  # email Name / email to Name
    r'|[Ss]end\s+(?:email|letter|f/u)\s+(?:to|from)?\s*'  # send email to Name
    r'|[Mm]eet(?:ing)?\s+with\s+'            # meeting with Name
    r'|[Dd]eposition\s+of\s+'               # deposition of Name
    r'|[Ll]etter\s+(?:to|from)\s+'           # letter to/from Name
    r')'
    r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})',  # Capitalized Name (2-4 words)
    re.UNICODE
)

# "regarding Name" patterns — these name entities that are subjects, not necessarily contacts
# But useful for witness identification
REGARDING_NAME_RE = re.compile(
    r'(?:regarding|re:|pertaining\s+to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})',
    re.IGNORECASE
)

# Exclude these "names" — they're legal jargon, not people
EXCLUDED_NAMES = {
    'summary judgment', 'motion to', 'motion for', 'notice of',
    'findings of', 'conclusions of', 'certificate of', 'order of',
    'first amended', 'second amended', 'third amended',
    'no evidence', 'traditional and', 'response to', 'reply to',
    'production and', 'service of', 'service on', 'process on',
    'status of', 'need for', 'review of', 'same', 'regarding same',
    'discovery and', 'settlement and', 'mediation and',
    'fact and', 'conclusions and', 'arguments to',
    'additional items', 'working copy', 'deficiency letter',
    'motion to compel', 'settlement agreement', 'settlement portion',
    'brief in', 'brief for', 'brief regarding',
    'dennis holmgren', 'mitchell madden',  # firm attorneys — handled separately
}

FIRM_ATTORNEY_NAMES = {
    'dennis holmgren', 'mitchell madden',
}

def _is_valid_name(name: str) -> bool:
    """Filter out non-name matches."""
    lower = name.lower().strip()
    if lower in EXCLUDED_NAMES or lower in FIRM_ATTORNEY_NAMES:
        return False
    if len(lower) < 4 or len(lower) > 60:
        return False
    # Must have at least 2 words
    if len(lower.split()) < 2:
        return False
    # Each word must start with a letter
    for w in name.split():
        if not w[0].isalpha():
            return False
    return True


def extract_names_from_narrative(narrative: str) -> list[dict]:
    """Extract person names from a billing narrative string."""
    results = []
    seen = set()

    for m in NARRATIVE_NAME_RE.finditer(narrative):
        name = m.group(1).strip()
        if _is_valid_name(name) and name.lower() not in seen:
            seen.add(name.lower())
            results.append({
                "full_name": name,
                "source_type": "billing_narrative",
                "confidence": 0.80,
                "raw_match": m.group(0)[:120],
            })

    return results


# ─── Global Seed Endpoint ─────────────────────────────────────────

@router.post("/seed-all")
async def seed_all_contacts(request: Request):
    """
    Global contact seeder — sweeps all data sources for the tenant.

    Sources:
      1. Email routing queue — all from_email + to_emails (45K+ emails)
      2. Billing narratives — name extraction from 54K+ narrative entries
      3. DMS documents — .msg/.eml headers from extracted text
      4. Calendar events — attendee names from subjects

    Creates contacts + matter_contact proposals where matter association exists.
    """
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    stats = {
        "email_contacts_found": 0,
        "narrative_contacts_found": 0,
        "dms_contacts_found": 0,
        "calendar_contacts_found": 0,
        "new_contacts_created": 0,
        "new_matter_links_created": 0,
        "skipped_existing": 0,
        "skipped_internal": 0,
    }

    INTERNAL_DOMAINS = ["hjmmlegal.com"]

    try:
        async with AsyncSessionLocal() as session:

            # ════════════════════════════════════════════════════════════
            # SOURCE 1: Email routing queue — every from/to email
            # ════════════════════════════════════════════════════════════

            # All distinct senders
            sender_r = await session.execute(sa_text("""
                SELECT from_email, from_display,
                       matched_matter_id::text AS matter_id,
                       COUNT(*) AS email_count
                FROM email_routing_queue
                WHERE TRIM(tenant_id) = :tid
                  AND from_email IS NOT NULL AND from_email != ''
                GROUP BY from_email, from_display, matched_matter_id
            """), {"tid": tid})

            for row in sender_r.mappings():
                addr = (row["from_email"] or "").lower().strip()
                if not addr or any(addr.endswith(f"@{d}") for d in INTERNAL_DOMAINS):
                    stats["skipped_internal"] += 1
                    continue

                stats["email_contacts_found"] += 1
                display = row["from_display"] or addr.split("@")[0].replace(".", " ").title()
                domain = addr.split("@")[1] if "@" in addr else ""
                matter_id = row["matter_id"]

                contact_id = await _find_or_create_contact(
                    session, tid, display, addr, None, domain, "exchange", stats
                )
                if contact_id and matter_id:
                    await _link_contact_to_matter(
                        session, tid, contact_id, matter_id, "email_queue", stats
                    )

            # All distinct recipients
            recip_r = await session.execute(sa_text("""
                SELECT DISTINCT
                    jsonb_array_elements_text(to_emails) AS email,
                    matched_matter_id::text AS matter_id
                FROM email_routing_queue
                WHERE TRIM(tenant_id) = :tid
                  AND to_emails IS NOT NULL
                  AND jsonb_typeof(to_emails) = 'array'
                  AND jsonb_array_length(to_emails) > 0
            """), {"tid": tid})

            for row in recip_r.mappings():
                addr = (row["email"] or "").lower().strip()
                if not addr or any(addr.endswith(f"@{d}") for d in INTERNAL_DOMAINS):
                    stats["skipped_internal"] += 1
                    continue

                stats["email_contacts_found"] += 1
                display = addr.split("@")[0].replace(".", " ").title()
                domain = addr.split("@")[1] if "@" in addr else ""

                contact_id = await _find_or_create_contact(
                    session, tid, display, addr, None, domain, "exchange", stats
                )
                if contact_id and row["matter_id"]:
                    await _link_contact_to_matter(
                        session, tid, contact_id, row["matter_id"], "email_queue", stats
                    )

            await session.commit()

            # ════════════════════════════════════════════════════════════
            # SOURCE 2: Billing narratives — name extraction
            # ════════════════════════════════════════════════════════════

            # Process in batches of 1000
            offset = 0
            batch_size = 1000
            while True:
                slip_r = await session.execute(sa_text("""
                    SELECT s.narrative, s.source_client_id,
                           m.id::text AS matter_id
                    FROM ts_slips s
                    LEFT JOIN matters m
                        ON m.legacy_id = s.source_client_id
                        AND TRIM(m.tenant_id) = :tid
                    WHERE TRIM(s.tenant_id) = :tid
                      AND s.narrative IS NOT NULL AND s.narrative != ''
                      AND (s.narrative ILIKE '%t/c%'
                           OR s.narrative ILIKE '%teleconference%'
                           OR s.narrative ILIKE '%conference with%'
                           OR s.narrative ILIKE '%email%'
                           OR s.narrative ILIKE '%correspond%'
                           OR s.narrative ILIKE '%meeting with%'
                           OR s.narrative ILIKE '%deposition of%'
                           OR s.narrative ILIKE '%letter to%'
                           OR s.narrative ILIKE '%letter from%')
                    ORDER BY s.id
                    LIMIT :lim OFFSET :off
                """), {"tid": tid, "lim": batch_size, "off": offset})

                rows = slip_r.mappings().fetchall()
                if not rows:
                    break

                for row in rows:
                    names = extract_names_from_narrative(row["narrative"] or "")
                    for n in names:
                        stats["narrative_contacts_found"] += 1
                        contact_id = await _find_or_create_contact(
                            session, tid, n["full_name"], None, None, None,
                            "billing_narrative", stats
                        )
                        if contact_id and row["matter_id"]:
                            await _link_contact_to_matter(
                                session, tid, contact_id, row["matter_id"],
                                "billing_narrative", stats
                            )

                await session.commit()
                offset += batch_size

            # ════════════════════════════════════════════════════════════
            # SOURCE 3: DMS .msg/.eml files — header extraction
            # ════════════════════════════════════════════════════════════

            offset = 0
            while True:
                doc_r = await session.execute(sa_text("""
                    SELECT dd.file_path,
                           LEFT(dd.content_text, 3000) AS content_text
                    FROM dms_documents dd
                    WHERE TRIM(dd.tenant_id) = :tid
                      AND dd.content_text IS NOT NULL
                      AND dd.content_text != ''
                      AND (dd.file_path LIKE '%%.msg' OR dd.file_path LIKE '%%.eml')
                    ORDER BY dd.id
                    LIMIT :lim OFFSET :off
                """), {"tid": tid, "lim": batch_size, "off": offset})

                rows = doc_r.mappings().fetchall()
                if not rows:
                    break

                from core.services.contact_extraction import (
                    extract_from_msg_headers, filter_internal_emails
                )

                for row in rows:
                    text = row["content_text"] or ""
                    fname = (row["file_path"] or "").rsplit("/", 1)[-1]
                    extracted = extract_from_msg_headers(text)
                    filtered = filter_internal_emails(extracted)

                    for c in filtered:
                        if not c.email:
                            continue
                        stats["dms_contacts_found"] += 1
                        contact_id = await _find_or_create_contact(
                            session, tid, c.full_name, c.email, c.phone,
                            c.company, "msg_header", stats
                        )

                await session.commit()
                offset += batch_size

            # ════════════════════════════════════════════════════════════
            # SOURCE 4: Calendar events — attendee names from subjects
            # ════════════════════════════════════════════════════════════

            cal_r = await session.execute(sa_text("""
                SELECT subject, organizer, matter_id::text
                FROM exchange_calendar_events
                WHERE TRIM(tenant_id) = :tid
                  AND subject IS NOT NULL AND subject != ''
            """), {"tid": tid})

            # Extract "Meeting with Name" patterns from subjects
            CAL_NAME_RE = re.compile(
                r'(?:with|w/|re:|regarding)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})',
                re.IGNORECASE
            )
            for row in cal_r.mappings():
                subj = row["subject"] or ""
                for m in CAL_NAME_RE.finditer(subj):
                    name = m.group(1).strip()
                    if _is_valid_name(name):
                        stats["calendar_contacts_found"] += 1
                        contact_id = await _find_or_create_contact(
                            session, tid, name, None, None, None,
                            "calendar", stats
                        )
                        if contact_id and row["matter_id"]:
                            await _link_contact_to_matter(
                                session, tid, contact_id, row["matter_id"],
                                "calendar", stats
                            )

            await session.commit()

        return JSONResponse({"status": "ok", **stats})

    except Exception as exc:
        logger.error("seed_all_contacts error: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── Helper: Find or Create Contact ───────────────────────────────

async def _find_or_create_contact(
    session, tid: str, full_name: str, email: str | None,
    phone: str | None, company: str | None, contact_type: str,
    stats: dict,
) -> int | None:
    """Find existing contact by email or name, or create a new one."""
    contact_id = None

    # Try email match first
    if email:
        r = await session.execute(sa_text("""
            SELECT id FROM contacts
            WHERE TRIM(tenant_id) = :tid AND LOWER(TRIM(email)) = :email
            LIMIT 1
        """), {"tid": tid, "email": email.lower().strip()})
        row = r.fetchone()
        if row:
            contact_id = row[0]
            stats["skipped_existing"] += 1

    # Try name match
    if not contact_id and full_name:
        r = await session.execute(sa_text("""
            SELECT id FROM contacts
            WHERE TRIM(tenant_id) = :tid AND LOWER(TRIM(full_name)) = :name
            LIMIT 1
        """), {"tid": tid, "name": full_name.lower().strip()})
        row = r.fetchone()
        if row:
            contact_id = row[0]
            stats["skipped_existing"] += 1

    # Create if not found
    if not contact_id:
        try:
            r = await session.execute(sa_text("""
                INSERT INTO contacts
                    (tenant_id, full_name, email, phone, company, contact_type, created_at, updated_at)
                VALUES (:tid, :name, :email, :phone, :company, :ctype, NOW(), NOW())
                RETURNING id
            """), {
                "tid": tid,
                "name": full_name or (email.split("@")[0].replace(".", " ").title() if email else "Unknown"),
                "email": email or None,
                "phone": phone or None,
                "company": company or None,
                "ctype": contact_type,
            })
            contact_id = r.scalar()
            stats["new_contacts_created"] += 1
        except Exception as e:
            # Likely a race condition on unique constraint — try to find again
            logger.debug("Contact create failed (probably dupe): %s", e)
            await session.rollback()
            return None

    return contact_id


# ─── Helper: Link Contact to Matter ───────────────────────────────

async def _link_contact_to_matter(
    session, tid: str, contact_id: int, matter_id: str,
    source: str, stats: dict,
) -> None:
    """Create a matter_contact link if one doesn't already exist."""
    try:
        # Check existing
        r = await session.execute(sa_text("""
            SELECT id FROM matter_contacts
            WHERE TRIM(tenant_id) = :tid
              AND matter_id = CAST(:mid AS uuid)
              AND contact_id = :cid
            LIMIT 1
        """), {"tid": tid, "mid": matter_id, "cid": contact_id})
        if r.fetchone():
            return  # Already linked

        await session.execute(sa_text("""
            INSERT INTO matter_contacts
                (tenant_id, matter_id, contact_id, role, category, source, status, created_at)
            VALUES (:tid, CAST(:mid AS uuid), :cid, 'other', 'people', :src, 'confirmed', NOW())
        """), {
            "tid": tid, "mid": matter_id, "cid": contact_id, "src": source,
        })
        stats["new_matter_links_created"] += 1
    except Exception as e:
        logger.debug("Link failed (probably dupe): %s", e)
