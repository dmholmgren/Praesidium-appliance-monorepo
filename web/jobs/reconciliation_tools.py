"""
reconciliation_tools.py
=======================

Curated DB-backed tool registry for the M9 AI Timesheet Reconciliation engine.

ARCHITECTURE
------------
This module is the **single source of truth** for the tools Claude is allowed
to call during Pass-2 (matcher) reconciliation. The worker runs a local
tool-use loop against the Anthropic API; when Claude requests a tool, the
worker dispatches via TOOL_REGISTRY in this module.

We deliberately do NOT expose `run_readonly_query` / arbitrary SQL. Each tool
is a narrow, parameterized question with curated output. This is the
"AI follows workflow" principle:
    - Tools encode the questions a senior reconciliation clerk would ask
    - Tool implementations encode the appliance's quirks (TRIM(tenant_id),
      varchar/char/text padding, AsyncSessionLocal, etc.)
    - Claude reasons over tool outputs; it does not reason about SQL syntax

CONSTRAINTS BAKED IN
--------------------
- TRIM(tenant_id) on EVERY query (tenant_id is varchar(36) or char(36),
  trailing-space-prone in some rows)
- AsyncSessionLocal exclusively (never get_session_factory())
- asyncpg-safe parameter casting where needed
- All tools are tenant-scoped: callers pass tenant_id which we trim and bind
- All tools cap result size to keep prompts within reasonable token budgets

TOOL CATALOG
------------
1. find_contact_by_phone        - Phone number -> contact records
2. find_contact_by_email        - Email address -> contact records
3. find_contact_by_name         - Name fuzzy search -> contact records
4. get_matters_for_contact      - Contact id -> matters they're linked to
5. search_matters               - Keyword search over matter_name/number/notes
6. get_matter_details           - Matter id -> full matter record + client info
7. get_recent_slips_for_attorney - Recent ts_slips by source_tk_id (last N days)
8. find_email_thread_matter     - Look up already-routed emails by party/subject
9. list_active_matters          - All active matters (small firm: ~50-200 rows)

These are exposed to Claude via ANTHROPIC_TOOL_DEFS (see bottom of file).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Output caps - keep individual tool responses bounded to protect token budget
# ---------------------------------------------------------------------------
MAX_CONTACTS = 10
MAX_MATTERS = 25
MAX_SLIPS = 15
MAX_EMAILS = 10
MAX_SEARCH = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize_phone(raw: str) -> str:
    """Reduce a phone string to digits-only for matching.

    Examples:
        "(480) 231-2760"    -> "4802312760"
        "+1 480-231-2760"   -> "14802312760"
        "480.231.2760"      -> "4802312760"
    """
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    # Strip US country code prefix to maximize match likelihood
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def _row_to_dict(row) -> Dict[str, Any]:
    """SQLAlchemy Row -> plain dict (json-serializable for tool output)."""
    d = dict(row._mapping)
    for k, v in list(d.items()):
        if isinstance(v, (datetime, date)):
            d[k] = v.isoformat()
        elif hasattr(v, "hex") and not isinstance(v, (bytes, bytearray)):
            # UUID -> string
            d[k] = str(v)
    return d


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
async def find_contact_by_phone(
    tenant_id: str, phone: str, limit: int = MAX_CONTACTS
) -> List[Dict[str, Any]]:
    """Find contact records whose phone digits match the given number.

    Matches against the last 10 digits of either:
      - the contact's `phone` column (preferred), OR
      - any 10-consecutive-digit run found inside `full_name`

    The full_name fallback is intentional: contact imports occasionally bake
    the phone number into the name field (e.g. full_name = "Bhadresh Trivedi
    (214) 208-5078") rather than into the phone column. We harden the tool
    against this rather than rely on perfect upstream data.

    Returns: list of contact dicts with id, full_name, company, contact_type,
    email, phone, firm_name, notes. Includes a `match_source` field indicating
    'phone_column' or 'name_field' so downstream code can flag data-quality
    issues for cleanup.
    """
    digits = _normalize_phone(phone)
    if len(digits) < 7:
        return []

    last10 = digits[-10:]

    sql = text(
        """
        SELECT id, full_name, company, contact_type, email, phone,
               firm_name, notes, external_id,
               CASE
                   WHEN phone IS NOT NULL
                        AND regexp_replace(phone, '\\D', '', 'g') LIKE :pattern
                   THEN 'phone_column'
                   ELSE 'name_field'
               END AS match_source
        FROM contacts
        WHERE TRIM(tenant_id) = :tenant_id
          AND (
            (phone IS NOT NULL
             AND regexp_replace(phone, '\\D', '', 'g') LIKE :pattern)
            OR
            (regexp_replace(full_name, '\\D', '', 'g') LIKE :pattern)
          )
        ORDER BY
          -- Prefer matches via the proper phone column over name_field hits
          CASE
              WHEN phone IS NOT NULL
                   AND regexp_replace(phone, '\\D', '', 'g') LIKE :pattern
              THEN 0 ELSE 1
          END,
          full_name
        LIMIT :limit
        """
    )
    pattern = f"%{last10}"

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {"tenant_id": tenant_id.strip(), "pattern": pattern, "limit": limit},
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def find_contact_by_email(
    tenant_id: str, email: str, limit: int = MAX_CONTACTS
) -> List[Dict[str, Any]]:
    """Find contact records by email address (case-insensitive)."""
    e = _normalize_email(email)
    if "@" not in e:
        return []

    sql = text(
        """
        SELECT id, full_name, company, contact_type, email, phone,
               firm_name, notes, external_id
        FROM contacts
        WHERE TRIM(tenant_id) = :tenant_id
          AND LOWER(email) = :email
        ORDER BY full_name
        LIMIT :limit
        """
    )

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {"tenant_id": tenant_id.strip(), "email": e, "limit": limit},
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def find_contact_by_name(
    tenant_id: str, name: str, limit: int = MAX_CONTACTS
) -> List[Dict[str, Any]]:
    """Fuzzy name search using ILIKE on full_name and company.

    Use sparingly - prefer phone/email lookups when available.
    """
    if not name or len(name.strip()) < 3:
        return []

    sql = text(
        """
        SELECT id, full_name, company, contact_type, email, phone,
               firm_name, notes, external_id
        FROM contacts
        WHERE TRIM(tenant_id) = :tenant_id
          AND (full_name ILIKE :pattern OR company ILIKE :pattern)
        ORDER BY
          CASE WHEN full_name ILIKE :exact THEN 0 ELSE 1 END,
          full_name
        LIMIT :limit
        """
    )
    pattern = f"%{name.strip()}%"
    exact = f"{name.strip()}%"

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {
                "tenant_id": tenant_id.strip(),
                "pattern": pattern,
                "exact": exact,
                "limit": limit,
            },
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def get_matters_for_contact(
    tenant_id: str, contact_id: int, limit: int = MAX_MATTERS
) -> List[Dict[str, Any]]:
    """Given a contact_id, return matters where that contact is linked.

    Includes the role (client, opposing counsel, witness, etc.) and whether
    they are the primary contact for the matter. Joins to clients for the
    client name.
    """
    sql = text(
        """
        SELECT
            m.id            AS matter_id,
            m.matter_number,
            m.matter_name,
            m.status,
            m.practice_area,
            m.matter_type,
            c.client_name,
            mc.role,
            mc.is_primary
        FROM matter_contacts mc
        JOIN matters m  ON m.id = mc.matter_id
        LEFT JOIN clients c ON c.id = m.client_id
        WHERE TRIM(mc.tenant_id) = :tenant_id
          AND mc.contact_id = :contact_id
        ORDER BY
          CASE WHEN m.status = 'active' THEN 0 ELSE 1 END,
          mc.is_primary DESC,
          m.matter_name
        LIMIT :limit
        """
    )

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {
                "tenant_id": tenant_id.strip(),
                "contact_id": contact_id,
                "limit": limit,
            },
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def search_matters(
    tenant_id: str,
    query: str,
    active_only: bool = True,
    limit: int = MAX_SEARCH,
) -> List[Dict[str, Any]]:
    """Keyword search across matter_name, matter_number, and notes."""
    if not query or len(query.strip()) < 2:
        return []

    status_clause = "AND m.status = 'active'" if active_only else ""

    sql = text(
        f"""
        SELECT
            m.id            AS matter_id,
            m.matter_number,
            m.matter_name,
            m.status,
            m.practice_area,
            m.matter_type,
            c.client_name
        FROM matters m
        LEFT JOIN clients c ON c.id = m.client_id
        WHERE TRIM(m.tenant_id) = :tenant_id
          {status_clause}
          AND (
            m.matter_name   ILIKE :pattern
            OR m.matter_number ILIKE :pattern
            OR m.notes      ILIKE :pattern
            OR c.client_name ILIKE :pattern
          )
        ORDER BY
          CASE WHEN m.matter_name ILIKE :exact THEN 0 ELSE 1 END,
          m.matter_name
        LIMIT :limit
        """
    )
    pattern = f"%{query.strip()}%"
    exact = f"{query.strip()}%"

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {
                "tenant_id": tenant_id.strip(),
                "pattern": pattern,
                "exact": exact,
                "limit": limit,
            },
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def get_matter_details(
    tenant_id: str, matter_id: str
) -> Optional[Dict[str, Any]]:
    """Full record for a single matter, including client and primary contact."""
    sql = text(
        """
        SELECT
            m.id            AS matter_id,
            m.matter_number,
            m.matter_name,
            m.status,
            m.practice_area,
            m.matter_type,
            m.cause_number,
            m.court,
            m.judge,
            m.notes         AS matter_notes,
            c.id            AS client_id,
            c.client_name,
            c.notes         AS client_notes,
            (SELECT json_agg(json_build_object(
                'contact_id', co.id,
                'full_name', co.full_name,
                'company', co.company,
                'role', mc.role,
                'is_primary', mc.is_primary
            ))
             FROM matter_contacts mc
             JOIN contacts co ON co.id = mc.contact_id
             WHERE TRIM(mc.tenant_id) = :tenant_id
               AND mc.matter_id = m.id
            ) AS contacts
        FROM matters m
        LEFT JOIN clients c ON c.id = m.client_id
        WHERE TRIM(m.tenant_id) = :tenant_id
          AND m.id = CAST(:matter_id AS uuid)
        """
    )

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql, {"tenant_id": tenant_id.strip(), "matter_id": matter_id}
        )
        row = r.fetchone()
        return _row_to_dict(row) if row else None


async def get_recent_slips_for_attorney(
    tenant_id: str,
    source_tk_id: str,
    days_back: int = 60,
    limit: int = MAX_SLIPS,
) -> List[Dict[str, Any]]:
    """Recent legacy ts_slips for an attorney by their source_tk_id.

    Returns slip_date, hours, narrative, source_client_id. Useful for Claude
    to detect billing patterns ("attorney billed Hat Creek 4x/week last month
    so a 12-min call to that party is probably Hat Creek again").
    """
    if not source_tk_id:
        return []

    sql = text(
        """
        SELECT
            slip_date,
            hours,
            narrative,
            source_client_id,
            value
        FROM ts_slips
        WHERE TRIM(tenant_id) = :tenant_id
          AND source_tk_id = :tk_id
          AND slip_date >= :cutoff
          AND narrative IS NOT NULL
        ORDER BY slip_date DESC, slip_date DESC
        LIMIT :limit
        """
    )
    # asyncpg requires a real date object for DATE columns - never a string
    cutoff = date.today() - timedelta(days=days_back)

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql,
            {
                "tenant_id": tenant_id.strip(),
                "tk_id": source_tk_id,
                "cutoff": cutoff,
                "limit": limit,
            },
        )
        return [_row_to_dict(row) for row in r.fetchall()]


async def find_email_thread_matter(
    tenant_id: str,
    party_email: Optional[str] = None,
    subject_keyword: Optional[str] = None,
    days_back: int = 30,
    limit: int = MAX_EMAILS,
) -> List[Dict[str, Any]]:
    """Look up already-routed emails to find a matter assignment.

    Useful when reconciling phone/iMazing entries with a counterparty whose
    email traffic has already been matter-routed. Returns:
        - matched_matter_id (already routed)
        - subject, from_email, received_at, conversation_topic
        - match_confidence
    """
    conditions = ["TRIM(tenant_id) = :tenant_id", "received_at >= :cutoff"]
    params: Dict[str, Any] = {
        "tenant_id": tenant_id.strip(),
        "cutoff": (datetime.utcnow() - timedelta(days=days_back)),
        "limit": limit,
    }

    if party_email:
        conditions.append("(LOWER(from_email) = :email OR to_emails::text ILIKE :pattern_email)")
        params["email"] = _normalize_email(party_email)
        params["pattern_email"] = f"%{_normalize_email(party_email)}%"

    if subject_keyword:
        conditions.append("subject ILIKE :pattern_subj")
        params["pattern_subj"] = f"%{subject_keyword.strip()}%"

    if not party_email and not subject_keyword:
        return []

    sql = text(
        f"""
        SELECT
            id,
            subject,
            from_email,
            from_display,
            received_at,
            conversation_topic,
            matched_matter_id,
            match_confidence,
            routing_status
        FROM email_routing_queue
        WHERE {' AND '.join(conditions)}
          AND matched_matter_id IS NOT NULL
        ORDER BY received_at DESC
        LIMIT :limit
        """
    )

    async with AsyncSessionLocal() as s:
        r = await s.execute(sql, params)
        return [_row_to_dict(row) for row in r.fetchall()]


async def list_active_matters(
    tenant_id: str, limit: int = 200
) -> List[Dict[str, Any]]:
    """All active matters for the tenant. Small firms typically have <500.

    Returned to Claude as fallback context when no specific lookup yields a
    confident match.
    """
    sql = text(
        """
        SELECT
            m.id            AS matter_id,
            m.matter_number,
            m.matter_name,
            m.practice_area,
            m.matter_type,
            c.client_name
        FROM matters m
        LEFT JOIN clients c ON c.id = m.client_id
        WHERE TRIM(m.tenant_id) = :tenant_id
          AND m.status = 'active'
        ORDER BY m.matter_name
        LIMIT :limit
        """
    )

    async with AsyncSessionLocal() as s:
        r = await s.execute(
            sql, {"tenant_id": tenant_id.strip(), "limit": limit}
        )
        return [_row_to_dict(row) for row in r.fetchall()]


# ---------------------------------------------------------------------------
# Dispatcher: tool name -> coroutine
# ---------------------------------------------------------------------------
TOOL_REGISTRY = {
    "find_contact_by_phone": find_contact_by_phone,
    "find_contact_by_email": find_contact_by_email,
    "find_contact_by_name": find_contact_by_name,
    "get_matters_for_contact": get_matters_for_contact,
    "search_matters": search_matters,
    "get_matter_details": get_matter_details,
    "get_recent_slips_for_attorney": get_recent_slips_for_attorney,
    "find_email_thread_matter": find_email_thread_matter,
    "list_active_matters": list_active_matters,
}


async def dispatch_tool(
    tool_name: str, tenant_id: str, **kwargs: Any
) -> Dict[str, Any]:
    """Execute a tool call by name. Always returns {"ok": bool, "result"|"error"}.

    Errors are caught and returned as structured data so the agent loop can
    feed the error back to Claude rather than crashing the worker.

    Note: parameter is `tool_name` (not `name`) to avoid collisions with tools
    that accept a `name` keyword argument (e.g. find_contact_by_name).
    """
    fn = TOOL_REGISTRY.get(tool_name)
    if fn is None:
        return {"ok": False, "error": f"Unknown tool: {tool_name}"}

    try:
        result = await fn(tenant_id=tenant_id, **kwargs)
        return {"ok": True, "result": result}
    except TypeError as e:
        # Bad argument shape from the model
        log.warning("Tool %s called with bad args: %s", tool_name, e)
        return {"ok": False, "error": f"Argument error: {e}"}
    except Exception as e:
        log.exception("Tool %s failed", tool_name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Anthropic tool definitions (passed via the `tools` parameter)
# ---------------------------------------------------------------------------
ANTHROPIC_TOOL_DEFS: List[Dict[str, Any]] = [
    {
        "name": "find_contact_by_phone",
        "description": (
            "Look up contacts by phone number. Returns matching contact "
            "records (id, name, company, email, phone, firm). Use this for "
            "phone-based timesheet drafts (calls, texts) where you have the "
            "counterparty's number.\n\n"
            "Each result includes a `match_source` field:\n"
            "  - 'phone_column': the contact has the phone in its proper field (clean)\n"
            "  - 'name_field': the phone digits were extracted from inside the "
            "contact's full_name (a known data-quality artifact from the phone "
            "CSV import). These matches are still valid but indicate a contact "
            "record that needs cleanup. Treat them with slightly lower "
            "confidence and look for a cleaner duplicate record before final "
            "matter assignment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "phone": {
                    "type": "string",
                    "description": "Phone number, any format (will be normalized to digits).",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {MAX_CONTACTS}).",
                },
            },
            "required": ["phone"],
        },
    },
    {
        "name": "find_contact_by_email",
        "description": (
            "Look up contacts by email address. Returns matching contact records. "
            "Use for email-based timesheet drafts where you have the counterparty's email."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["email"],
        },
    },
    {
        "name": "find_contact_by_name",
        "description": (
            "Fuzzy name/company search across contacts. Use only when phone/email "
            "lookups are unavailable - this is less precise."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name or company keyword (>=3 chars)."},
                "limit": {"type": "integer"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_matters_for_contact",
        "description": (
            "Given a contact_id (from a find_contact_* result), list every "
            "matter that contact is linked to via matter_contacts. Returns "
            "matter_id, matter_name, role (client/OC/witness), is_primary, status. "
            "This is the strongest signal for routing a draft to a matter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "name": "search_matters",
        "description": (
            "Keyword search over matter_name, matter_number, notes, and client_name. "
            "Use when a draft's description mentions a project name, party name, or "
            "case keyword (e.g., 'Hat Creek brief', 'Trivedi closing')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "active_only": {
                    "type": "boolean",
                    "description": "Default true. Set false to include closed matters.",
                },
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_matter_details",
        "description": (
            "Full record for one matter, including client info and the full "
            "contact roster (with roles and primary flag). Use to confirm a "
            "candidate match before final answer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "matter_id": {"type": "string", "description": "Matter UUID."},
            },
            "required": ["matter_id"],
        },
    },
    {
        "name": "get_recent_slips_for_attorney",
        "description": (
            "Recent legacy time slips (ts_slips) for an attorney, by their "
            "source_tk_id. Returns slip_date, hours, narrative, source_client_id. "
            "Useful for detecting billing PATTERNS - if the attorney billed "
            "Hat Creek 4x last month, an ambiguous Hat-Creek-adjacent draft "
            "is likely Hat Creek again. Provide source_tk_id from the draft's "
            "user context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_tk_id": {"type": "string"},
                "days_back": {"type": "integer", "description": "Default 60."},
                "limit": {"type": "integer"},
            },
            "required": ["source_tk_id"],
        },
    },
    {
        "name": "find_email_thread_matter",
        "description": (
            "Look up emails that have ALREADY been routed to a matter, by "
            "party email and/or subject keyword. Returns the matter_id those "
            "emails were routed to and the routing confidence. Powerful for "
            "cross-source matching: if Bill's emails routed to Hat Creek, his "
            "phone calls probably belong there too."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "party_email": {"type": "string"},
                "subject_keyword": {"type": "string"},
                "days_back": {"type": "integer", "description": "Default 30."},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "list_active_matters",
        "description": (
            "All active matters for the tenant, name + number + practice area. "
            "Use as a last-resort context when no specific lookup yields a "
            "candidate. Limit defaults to 200 (most small firms have fewer)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer"},
            },
        },
    },
]
