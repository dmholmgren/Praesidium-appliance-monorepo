"""
modules/intelligence/matter_extract.py
======================================
Matter Intelligence Extraction Engine — v2 (library-constrained roles)

The AI layer that reads documents and produces structured data primitives.
Context-dispatched: the same endpoint handles matter provisioning, witness
enrichment, property lookup, and deal term extraction.

v2 changes (0038_party_model):
  - Roles constrained to contact_role_library codes
  - role_code + is_client_side + confidence written to matter_contacts
  - GF number extraction from title commitments → matters.gf_number
  - Cause number extraction → matters.cause_number
  - Identifiers (loan numbers, policy numbers, etc.) → matter_identifiers

Endpoints:
  POST /api/v1/ai/extract           — extract from text/file, write to DB
  POST /api/v1/ai/matter-refresh    — read entire matter corpus, rebuild intelligence
  GET  /api/v1/ai/extract-status/{job_id} — poll async job status

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.intelligence.extract")
router = APIRouter(prefix="/api/v1/ai", tags=["matter-intelligence"])

# ── Valid role codes (from contact_role_library) ─────────────
# Loaded once at module init; the prompt embeds these so Claude picks from the menu.

LITIGATION_ROLE_CODES = [
    "plaintiff", "defendant", "petitioner", "respondent", "intervenor",
    "third_party", "party",
    "opposing_counsel", "co_counsel", "client_contact",
    "judge", "court_coordinator", "court_clerk", "mediator", "arbitrator",
    "insurer", "guardian_ad_litem", "regulator", "vendor", "notary",
    "court_reporter", "process_server",
    "fact_witness", "expert_witness", "custodian", "deponent",
    "corporate_rep", "affiant",
    "other",
]

TRANSACTIONAL_ROLE_CODES = [
    "buyer", "seller", "borrower", "lender", "broker",
    "title_company", "escrow_agent", "surveyor", "inspector", "appraiser",
    "accountant", "financial_advisor", "guarantor", "qualified_intermediary",
    "notary", "party",
    "opposing_counsel", "co_counsel", "client_contact",
    "regulator", "vendor",
    "other",
]

ALL_ROLE_CODES = set(LITIGATION_ROLE_CODES + TRANSACTIONAL_ROLE_CODES)


# ── Helpers ──────────────────────────────────────────────────

def _tid(req: Request) -> str:
    return (getattr(req.state, "tenant_id", "") or "").strip()

def _uid(req: Request):
    u = getattr(req.state, "current_user", None)
    return int(getattr(u, "id", 0) or 0) if u else 0


async def _get_api_key(tenant_id: str) -> str | None:
    from cryptography.fernet import Fernet
    import base64
    secret = os.environ.get("SECRET_KEY", "changeme-32-bytes-exactly!!!!!!!")
    key_bytes = (secret[:32]).encode().ljust(32, b"0")
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    f = Fernet(fernet_key)
    async with AsyncSessionLocal() as session:
        row = (await session.execute(sa_text(
            "SELECT encrypted_key FROM credentials_vault "
            "WHERE tenant_id = :tid AND provider = 'anthropic' AND key_type = 'api_key'"
        ), {"tid": tenant_id})).fetchone()
    if not row:
        return None
    return f.decrypt(row[0].encode()).decode()


async def _call_claude(api_key: str, system: str, user_msg: str,
                       model: str = "claude-sonnet-4-20250514") -> dict | None:
    """Call Claude API, return parsed JSON from response."""
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 8192,
                "system": system,
                "messages": [{"role": "user", "content": user_msg}],
            },
        )
    latency = int((time.time() - t0) * 1000)
    if resp.status_code != 200:
        log.error("Claude API error %d: %s", resp.status_code, resp.text[:500])
        return None

    data = resp.json()
    text_content = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_content += block["text"]

    text_content = text_content.strip()
    if text_content.startswith("```json"):
        text_content = text_content[7:]
    if text_content.startswith("```"):
        text_content = text_content[3:]
    if text_content.endswith("```"):
        text_content = text_content[:-3]
    text_content = text_content.strip()

    try:
        result = json.loads(text_content)
    except json.JSONDecodeError:
        log.error("Failed to parse Claude JSON: %s", text_content[:500])
        return None

    usage = data.get("usage", {})
    try:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await session.execute(sa_text("""
                    INSERT INTO ai_api_calls
                        (tenant_id, user_id, provider, model, module, purpose,
                         input_tokens, output_tokens, total_tokens, latency_ms, status)
                    VALUES (:tid, 0, 'anthropic', :model, 'matter_intelligence', 'extract',
                            :inp, :out, :tot, :lat, 'success')
                """), {
                    "tid": "", "model": model,
                    "inp": usage.get("input_tokens", 0),
                    "out": usage.get("output_tokens", 0),
                    "tot": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                    "lat": latency,
                })
    except Exception as e:
        log.warning("Failed to log AI call: %s", e)

    return result


def _validate_role_code(role: str | None, matter_type: str | None = None) -> str:
    """Validate and normalize a role code against the library."""
    if not role:
        return "other"
    role = role.strip().lower().replace(" ", "_").replace("-", "_")
    # Common aliases
    aliases = {
        "expert": "expert_witness",
        "witness": "fact_witness",
        "client": "party",  # client is not a role; fallback to party
        "court_reporter": "court_reporter",
        "process_server": "process_server",
    }
    role = aliases.get(role, role)
    if role in ALL_ROLE_CODES:
        return role
    return "other"


def _infer_client_side(role_code: str, matter_type: str | None = None) -> bool | None:
    """Infer is_client_side from role code where unambiguous. Returns None when ambiguous."""
    # Clearly adverse
    if role_code in ("opposing_counsel", "defendant", "respondent"):
        return False
    # Clearly neutral / third-party
    if role_code in ("judge", "court_coordinator", "court_clerk", "mediator",
                     "arbitrator", "guardian_ad_litem", "court_reporter",
                     "notary", "process_server", "regulator"):
        return False
    # Service providers — typically engaged by one side but not "client side"
    if role_code in ("title_company", "escrow_agent", "surveyor", "inspector",
                     "appraiser", "accountant", "financial_advisor",
                     "qualified_intermediary", "vendor", "insurer"):
        return None  # ambiguous — could be ours or theirs
    # Co-counsel is our side
    if role_code == "co_counsel":
        return True
    # Client contact is by definition our side
    if role_code == "client_contact":
        return True
    # For parties (plaintiff, defendant, buyer, seller, etc.) — ambiguous without
    # knowing which side is the client. Return None; the user resolves.
    return None
# ── Extraction Prompts ───────────────────────────────────────

SYSTEM_MATTER_PROVISION = """You are a legal document analysis AI for a law firm practice management platform.
Extract structured data from the provided document text. Return ONLY valid JSON, no markdown, no explanation.

CRITICAL: For the "role" field on each party, you MUST use EXACTLY one of these codes:
  Transactional roles: buyer, seller, borrower, lender, broker, title_company, escrow_agent,
    surveyor, inspector, appraiser, accountant, financial_advisor, guarantor,
    qualified_intermediary, notary, party, opposing_counsel, co_counsel,
    client_contact, regulator, vendor, other
  Use "party" as a generic fallback when you can identify someone as a party to the
  transaction but cannot determine their specific role.
  Use "other" ONLY when the person is genuinely unclassifiable — not when you are uncertain
  about which specific role applies (prefer "party" for uncertain party roles).

CRITICAL: For "is_client_side", set true if the person is on the side being represented
by this law firm, false if they are on the opposing side, null if you cannot determine.

For title commitments and title insurance documents:
  - Extract the GF Number (Guaranty File Number, sometimes labeled "GF No.", "File No.",
    "Commitment No.", or "Order No." on title commitments). This is the title company's
    tracking number for the transaction.
  - Extract loan numbers, policy numbers, and other identifiers.

The JSON must have these top-level keys:
{
  "matter_type": "litigation" or "transactional",
  "matter_metadata": {
    "practice_area": string or null,
    "court": string or null,
    "cause_number": string or null,
    "judge": string or null,
    "jurisdiction": string or null,
    "gf_number": string or null (GF/file number from title commitment)
  },
  "identifiers": [
    {
      "id_type": "gf_number"|"loan_number"|"policy_number"|"escrow_number"|"mls_number"|"filing_number"|"other",
      "id_value": string,
      "label": string or null (human-readable label, e.g. "Chase Loan #")
    }
  ],
  "parties": [
    {
      "full_name": string,
      "role": string (MUST be one of the role codes listed above),
      "is_client_side": true|false|null,
      "confidence": number 0.0-1.0 (how confident you are in the role assignment),
      "company": string or null,
      "email": string or null,
      "phone": string or null,
      "address": string or null,
      "category": "people"|"party"|"witness"
    }
  ],
  "deal_points": [
    {
      "point_key": string (snake_case, e.g. "purchase_price"),
      "point_label": string (human readable),
      "point_value": string,
      "point_type": "text"|"currency"|"date"|"percentage"|"integer",
      "source_clause": string or null (e.g. "Section 2.01")
    }
  ],
  "property": {
    "display_name": string or null,
    "address_line1": string or null,
    "city": string or null,
    "state": string or null,
    "zip_code": string or null,
    "county": string or null,
    "parcel_id": string or null,
    "acreage": number or null,
    "legal_description": string or null (first 500 chars)
  } or null,
  "document_classification": {
    "document_type": "psa"|"title_commitment"|"loi"|"complaint"|"answer"|"motion"|"order"|"deed"|"lease"|"engagement_letter"|"correspondence"|"other",
    "title": string,
    "date_on_document": "YYYY-MM-DD" or null,
    "summary": string (2-3 sentence summary)
  },
  "key_dates": [
    {
      "label": string,
      "date": "YYYY-MM-DD" or null,
      "description": string
    }
  ]
}

Extract everything you can find. Use null for fields you cannot determine. For currency values, use plain numbers (no $ or commas). For dates, use ISO format."""


SYSTEM_LITIGATION_EXTRACT = """You are a litigation document analysis AI for a law firm practice management platform.
Extract structured data from the provided litigation document. Return ONLY valid JSON, no markdown.

CRITICAL: For the "role" field on each party, you MUST use EXACTLY one of these codes:
  Litigation roles: plaintiff, defendant, petitioner, respondent, intervenor, third_party,
    party, opposing_counsel, co_counsel, client_contact, judge, court_coordinator,
    court_clerk, mediator, arbitrator, insurer, guardian_ad_litem, regulator, vendor,
    notary, court_reporter, process_server, fact_witness, expert_witness, custodian,
    deponent, corporate_rep, affiant, other
  Use "party" as a generic fallback when you can identify someone as a party to the
  litigation but cannot determine plaintiff vs defendant (e.g. from a scheduling order
  that names parties without specifying sides).
  Use "plaintiff" for petitioners in family/dissolution/probate ONLY if the caption
  specifically uses "Plaintiff"; otherwise use "petitioner".
  Use "other" ONLY when the person is genuinely unclassifiable.

CRITICAL: For "is_client_side", set true if the person is on the side being represented
by this law firm, false if they are on the opposing side, null if you cannot determine.
Hint: if the document is filed by or on behalf of a party, that party is likely client-side.

The JSON must have these top-level keys:
{
  "case_summary": {
    "summary": string (3-5 sentence case summary for dashboard display),
    "case_type": string (e.g. "breach of contract", "personal injury", "fraud"),
    "filed_date": "YYYY-MM-DD" or null,
    "status": string or null
  },
  "matter_metadata": {
    "practice_area": string or null,
    "court": string or null,
    "cause_number": string or null (e.g. "DC-23-12345", "2024-CI-54321"),
    "judge": string or null,
    "jurisdiction": string or null
  },
  "identifiers": [
    {
      "id_type": "cause_number"|"case_number"|"filing_number"|"other",
      "id_value": string,
      "label": string or null
    }
  ],
  "scheduling_order": [
    {
      "label": string (e.g. "Discovery Deadline", "Mediation Deadline"),
      "date": "YYYY-MM-DD",
      "description": string or null
    }
  ],
  "causes_of_action": [
    {
      "name": string (e.g. "Breach of Contract"),
      "count_number": integer or null,
      "elements": [string],
      "statute": string or null
    }
  ],
  "defenses": [
    {
      "name": string,
      "description": string or null
    }
  ],
  "parties": [
    {
      "full_name": string,
      "role": string (MUST be one of the litigation role codes listed above),
      "is_client_side": true|false|null,
      "confidence": number 0.0-1.0,
      "company": string or null,
      "email": string or null,
      "phone": string or null,
      "category": "party"|"people"|"witness"
    }
  ],
  "financial_exposure": [
    {
      "label": string (e.g. "Monetary Relief Sought"),
      "value": string,
      "type": "currency"|"percentage"|"text"
    }
  ],
  "document_classification": {
    "document_type": "complaint"|"answer"|"counterclaim"|"motion"|"order"|"scheduling_order"|"discovery_request"|"discovery_response"|"deposition"|"expert_report"|"brief"|"notice"|"subpoena"|"correspondence"|"other",
    "title": string,
    "date_on_document": "YYYY-MM-DD" or null,
    "summary": string (2-3 sentence summary)
  },
  "property": {
    "display_name": string or null,
    "address_line1": string or null,
    "city": string or null,
    "state": string or null,
    "zip_code": string or null,
    "county": string or null
  } or null
}

Extract everything you can find. Use null for fields you cannot determine.
For dates, use ISO format YYYY-MM-DD. For currency, use plain numbers.
SKIP transactional noise (ABA routing numbers, wire numbers, shipping weights, product descriptions, invoice line items)."""


SYSTEM_WITNESS_ENRICH = """You are a legal research AI. Extract structured information about a person from the provided text.
Return ONLY valid JSON:
{
  "full_name": string,
  "company": string or null,
  "title": string or null,
  "email": string or null,
  "phone": string or null,
  "address": string or null,
  "city": string or null,
  "state": string or null,
  "bar_number": string or null,
  "license_number": string or null,
  "specialty": string or null,
  "prior_testimony": [{"case": string, "court": string, "date": string, "role": string}],
  "education": [string],
  "publications": [string],
  "background_summary": string (2-3 sentences),
  "flags": [string] (anything notable — disciplinary actions, malpractice, sanctions)
}
Extract everything you can. Use null for unknown fields. Use empty arrays for lists with no data."""


SYSTEM_PROPERTY_ENRICH = """You are a property data extraction AI. Extract structured property information from the provided text (likely from a county appraisal district website, tax record, or real estate document).
Return ONLY valid JSON:
{
  "display_name": string,
  "address_line1": string or null,
  "city": string or null,
  "state": string or null,
  "zip_code": string or null,
  "county": string or null,
  "parcel_id": string or null,
  "acreage": number or null,
  "legal_description": string or null,
  "latitude": number or null,
  "longitude": number or null,
  "details": {
    "appraised_value": number or null,
    "land_value": number or null,
    "improvement_value": number or null,
    "tax_year": integer or null,
    "zoning": string or null,
    "improvements_sqft": number or null,
    "land_sqft": number or null,
    "year_built": integer or null,
    "school_district": string or null,
    "flood_zone": string or null,
    "owner_name": string or null,
    "deed_volume": string or null,
    "deed_page": string or null
  }
}
Extract everything you can find. Use null for unknown fields."""
# ── Orchestrator: write extraction results to DB ─────────────

async def _write_identifiers(session, tid: str, matter_id: str, identifiers: list, uid: int):
    """Write extracted identifiers to matter_identifiers table."""
    count = 0
    for ident in identifiers:
        id_type = (ident.get("id_type") or "other").strip().lower()
        id_value = (ident.get("id_value") or "").strip()
        if not id_value:
            continue
        # Check for duplicate
        existing = await session.execute(sa_text("""
            SELECT id FROM matter_identifiers
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
              AND id_type = :idt AND id_value = :idv
        """), {"tid": tid, "mid": matter_id, "idt": id_type, "idv": id_value})
        if existing.fetchone():
            continue
        await session.execute(sa_text("""
            INSERT INTO matter_identifiers (tenant_id, matter_id, id_type, id_value, label, created_by)
            VALUES (:tid, CAST(:mid AS uuid), :idt, :idv, :label, :uid)
        """), {"tid": tid, "mid": matter_id, "idt": id_type, "idv": id_value,
               "label": ident.get("label"), "uid": uid or None})
        count += 1
    return count


async def _write_gf_and_cause(session, tid: str, matter_id: str, metadata: dict):
    """Write gf_number and cause_number to matters table (only if currently null)."""
    gf = (metadata.get("gf_number") or "").strip()
    cause = (metadata.get("cause_number") or "").strip()
    if gf:
        await session.execute(sa_text("""
            UPDATE matters SET gf_number = :gf
            WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
              AND (gf_number IS NULL OR gf_number = '')
        """), {"gf": gf, "mid": matter_id, "tid": tid})
    if cause:
        await session.execute(sa_text("""
            UPDATE matters SET cause_number = :cn
            WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
              AND (cause_number IS NULL OR cause_number = '')
        """), {"cn": cause, "mid": matter_id, "tid": tid})


async def _write_party(session, tid: str, matter_id: str, party: dict,
                       matter_type: str | None = None) -> bool:
    """Write a single party to contacts + matter_contacts with library-constrained role.
    Returns True if a new matter_contact was created."""
    name = (party.get("full_name") or "").strip()
    if not name or len(name) < 2:
        return False

    raw_role = party.get("role", "other")
    role_code = _validate_role_code(raw_role, matter_type)
    confidence = party.get("confidence", 0.5)
    if not isinstance(confidence, (int, float)):
        confidence = 0.5
    confidence = max(0.0, min(1.0, float(confidence)))

    # is_client_side: prefer AI's answer, fall back to inference
    is_client_side_raw = party.get("is_client_side")
    if isinstance(is_client_side_raw, bool):
        is_client_side = is_client_side_raw
    else:
        is_client_side = _infer_client_side(role_code, matter_type)

    # Determine category from role_code
    cat = party.get("category", "people")
    if role_code in ("plaintiff", "defendant", "petitioner", "respondent",
                     "intervenor", "third_party", "party", "buyer", "seller",
                     "borrower", "lender", "guarantor"):
        cat = "party"
    elif role_code in ("fact_witness", "expert_witness", "custodian", "deponent",
                       "corporate_rep", "affiant"):
        cat = "witness"
    else:
        cat = "people"

    # Upsert contact
    row = await session.execute(sa_text(
        "SELECT id FROM contacts WHERE TRIM(tenant_id) = :tid AND full_name = :name LIMIT 1"
    ), {"tid": tid, "name": name})
    existing = row.fetchone()
    if existing:
        contact_id = existing[0]
    else:
        row = await session.execute(sa_text("""
            INSERT INTO contacts (tenant_id, full_name, company, email, phone, city, state)
            VALUES (:tid, :name, :co, :email, :phone, NULL, NULL) RETURNING id
        """), {"tid": tid, "name": name, "co": party.get("company"),
               "email": party.get("email"), "phone": party.get("phone")})
        contact_id = row.scalar()

    # Check if already linked
    exists = await session.execute(sa_text("""
        SELECT id, role_code FROM matter_contacts
        WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid) AND contact_id = :cid
    """), {"tid": tid, "mid": matter_id, "cid": contact_id})
    existing_link = exists.fetchone()

    if existing_link:
        # Update role_code if it was NULL or 'other' and we have something better
        old_role = existing_link[1]
        if (old_role is None or old_role == "other") and role_code != "other":
            await session.execute(sa_text("""
                UPDATE matter_contacts
                SET role_code = :rc, role = :role_display, is_client_side = :ics,
                    confidence = :conf, category = :cat
                WHERE id = :mcid
            """), {"rc": role_code, "role_display": raw_role, "ics": is_client_side,
                   "conf": confidence, "cat": cat, "mcid": existing_link[0]})
        return False
    else:
        await session.execute(sa_text("""
            INSERT INTO matter_contacts
                (tenant_id, matter_id, contact_id, role, role_code, is_client_side,
                 confidence, category, source, status)
            VALUES (:tid, CAST(:mid AS uuid), :cid, :role_display, :rc, :ics,
                    :conf, :cat, 'ai_extract', 'proposed')
        """), {"tid": tid, "mid": matter_id, "cid": contact_id,
               "role_display": raw_role, "rc": role_code, "ics": is_client_side,
               "conf": confidence, "cat": cat})
        return True


async def _write_matter_extraction(tid: str, matter_id: str, doc_id: str | None,
                                    result: dict, uid: int) -> dict:
    """Write the full matter extraction result to all destination tables."""
    stats = {"deal_points": 0, "contacts": 0, "identifiers": 0,
             "subject": False, "key_document": False, "metadata": False}

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # ── GF Number + Cause Number ──
            mm = result.get("matter_metadata", {})
            await _write_gf_and_cause(session, tid, matter_id, mm)

            # ── Identifiers ──
            stats["identifiers"] = await _write_identifiers(
                session, tid, matter_id, result.get("identifiers", []), uid)
            # Also write gf_number as an identifier if present
            gf = (mm.get("gf_number") or "").strip()
            if gf:
                stats["identifiers"] += await _write_identifiers(
                    session, tid, matter_id,
                    [{"id_type": "gf_number", "id_value": gf, "label": "GF Number"}], uid)

            # ── Deal Points ──
            for dp in result.get("deal_points", []):
                if not dp.get("point_key"):
                    continue
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, source_document_id,
                         source_clause, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), :pk, :pl, :pv, :pt,
                            CASE WHEN :did = '' THEN NULL ELSE CAST(:did AS uuid) END,
                            :sc, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value,
                                  point_type = EXCLUDED.point_type,
                                  source_document_id = EXCLUDED.source_document_id,
                                  source_clause = EXCLUDED.source_clause,
                                  updated_by = EXCLUDED.updated_by,
                                  updated_at = NOW()
                """), {
                    "tid": tid, "mid": matter_id,
                    "pk": dp["point_key"], "pl": dp.get("point_label", dp["point_key"]),
                    "pv": dp.get("point_value"), "pt": dp.get("point_type", "text"),
                    "did": doc_id or "", "sc": dp.get("source_clause"),
                    "uid": uid if uid else None,
                })
                stats["deal_points"] += 1

            # ── Contacts / Parties (library-constrained) ──
            matter_type = result.get("matter_type", "transactional")
            for party in result.get("parties", []):
                created = await _write_party(session, tid, matter_id, party, matter_type)
                if created:
                    stats["contacts"] += 1

            # ── Property / Subject ──
            prop = result.get("property")
            if prop and prop.get("display_name"):
                details = {}
                for k in ("appraised_value", "land_value", "improvement_value",
                          "tax_year", "zoning", "improvements_sqft", "land_sqft",
                          "year_built", "school_district", "flood_zone",
                          "owner_name", "deed_volume", "deed_page"):
                    if prop.get(k) is not None:
                        details[k] = prop[k]
                    elif prop.get("details", {}).get(k) is not None:
                        details[k] = prop["details"][k]

                await session.execute(sa_text("""
                    INSERT INTO matter_subjects
                        (tenant_id, matter_id, subject_type, display_name,
                         address_line1, city, state, zip_code, county,
                         latitude, longitude, parcel_id,
                         legal_description, acreage, details)
                    VALUES (:tid, CAST(:mid AS uuid), 'real_property', :dn,
                            :a1, :city, :state, :zip, :county,
                            :lat, :lng, :pid, :ld, :ac,
                            CAST(:det AS jsonb))
                    ON CONFLICT ON CONSTRAINT uq_ms_tenant_matter
                    DO UPDATE SET display_name = EXCLUDED.display_name,
                                  address_line1 = EXCLUDED.address_line1,
                                  city = EXCLUDED.city, state = EXCLUDED.state,
                                  zip_code = EXCLUDED.zip_code, county = EXCLUDED.county,
                                  latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                                  parcel_id = EXCLUDED.parcel_id,
                                  legal_description = EXCLUDED.legal_description,
                                  acreage = EXCLUDED.acreage,
                                  details = EXCLUDED.details, updated_at = NOW()
                """), {
                    "tid": tid, "mid": matter_id, "dn": prop["display_name"],
                    "a1": prop.get("address_line1"),
                    "city": prop.get("city"), "state": prop.get("state"),
                    "zip": prop.get("zip_code"), "county": prop.get("county"),
                    "lat": prop.get("latitude"), "lng": prop.get("longitude"),
                    "pid": prop.get("parcel_id"),
                    "ld": (prop.get("legal_description") or "")[:2000],
                    "ac": prop.get("acreage"),
                    "det": json.dumps(details),
                })
                stats["subject"] = True

            # ── Key Document Link ──
            if doc_id:
                doc_class = result.get("document_classification", {})
                doc_type = doc_class.get("document_type", "other")
                role_map = {
                    "psa": "contract", "title_commitment": "title_commitment",
                    "loi": "loi", "complaint": "complaint",
                    "answer": "answer", "motion": "other",
                    "order": "scheduling_order", "deed": "deed",
                    "lease": "contract", "engagement_letter": "engagement_letter",
                }
                doc_role = role_map.get(doc_type, "other")
                await session.execute(sa_text("""
                    INSERT INTO matter_key_documents
                        (tenant_id, matter_id, document_id, document_role, label, linked_by)
                    VALUES (:tid, CAST(:mid AS uuid), CAST(:did AS uuid), :role, :label, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mkd_tenant_matter_doc
                    DO UPDATE SET document_role = EXCLUDED.document_role, label = EXCLUDED.label
                """), {
                    "tid": tid, "mid": matter_id, "did": doc_id,
                    "role": doc_role, "label": doc_class.get("title", ""),
                    "uid": uid if uid else None,
                })
                stats["key_document"] = True

            # ── Document Metadata ──
            doc_in_dms = False
            if doc_id:
                dm_ck = await session.execute(sa_text(
                    "SELECT id FROM dms_documents WHERE id = CAST(:did AS uuid) LIMIT 1"
                ), {"did": doc_id})
                doc_in_dms = dm_ck.fetchone() is not None
            if doc_id and doc_in_dms:
                doc_class = result.get("document_classification", {})
                parties_json = json.dumps(result.get("parties", []))
                add_meta = json.dumps({
                    "deal_points": result.get("deal_points", []),
                    "key_dates": result.get("key_dates", []),
                    "property": result.get("property"),
                    "matter_metadata": result.get("matter_metadata"),
                    "identifiers": result.get("identifiers", []),
                })
                await session.execute(sa_text("""
                    DELETE FROM document_metadata
                    WHERE dms_document_id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                """), {"did": doc_id, "tid": tid})
                await session.execute(sa_text("""
                    INSERT INTO document_metadata
                        (id, tenant_id, dms_document_id, extraction_method,
                         title, date_on_document, parties_mentioned,
                         additional_metadata, confidence, extracted_at)
                    VALUES (gen_random_uuid(), :tid, CAST(:did AS uuid),
                            'claude_extract', :title,
                            CASE WHEN :dod = '' THEN NULL ELSE CAST(:dod AS date) END,
                            CAST(:parties AS jsonb),
                            CAST(:meta AS jsonb), 0.85, NOW())
                """), {
                    "tid": tid, "did": doc_id,
                    "title": doc_class.get("title", ""),
                    "dod": doc_class.get("date_on_document", ""),
                    "parties": parties_json, "meta": add_meta,
                })
                stats["metadata"] = True

            # ── Matter metadata auto-populate (only when null) ──
            for field in ("court", "judge", "cause_number", "jurisdiction", "practice_area"):
                val = mm.get(field)
                if val:
                    await session.execute(sa_text(f"""
                        UPDATE matters SET {field} = :val
                        WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                          AND ({field} IS NULL OR {field} = '')
                    """), {"val": val, "mid": matter_id, "tid": tid})

    return stats

async def _write_witness_enrichment(tid: str, matter_id: str,
                                     contact_id: int, result: dict) -> dict:
    """Write witness/contact enrichment to contacts table."""
    stats = {"updated_fields": 0}
    updates = {}
    for field in ("company", "email", "phone", "city", "state",
                  "bar_number", "firm_name"):
        val = result.get(field)
        if val:
            updates[field] = val

    if not updates:
        return stats

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    updates["cid"] = contact_id
    updates["tid"] = tid

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(sa_text(f"""
                UPDATE contacts SET {set_clauses}
                WHERE id = :cid AND TRIM(tenant_id) = :tid
            """), updates)

            summary = result.get("background_summary", "")
            if summary:
                await session.execute(sa_text("""
                    UPDATE matter_contacts SET ai_summary = :summary
                    WHERE contact_id = :cid AND matter_id = CAST(:mid AS uuid)
                      AND TRIM(tenant_id) = :tid
                """), {"summary": summary, "cid": contact_id,
                       "mid": matter_id, "tid": tid})

    stats["updated_fields"] = len(updates) - 2
    return stats


async def _write_property_enrichment(tid: str, matter_id: str, result: dict) -> dict:
    """Write property enrichment to matter_subjects."""
    stats = {"updated": False}
    if not result.get("display_name"):
        return stats

    details = result.get("details", {})
    det_json = json.dumps(details)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(sa_text("""
                INSERT INTO matter_subjects
                    (tenant_id, matter_id, subject_type, display_name,
                     address_line1, city, state, zip_code, county,
                     latitude, longitude, parcel_id,
                     legal_description, acreage, details)
                VALUES (:tid, CAST(:mid AS uuid), 'real_property', :dn,
                        :a1, :city, :state, :zip, :county,
                        :lat, :lng, :pid, :ld, :ac,
                        CAST(:det AS jsonb))
                ON CONFLICT ON CONSTRAINT uq_ms_tenant_matter
                DO UPDATE SET display_name = EXCLUDED.display_name,
                              address_line1 = EXCLUDED.address_line1,
                              city = EXCLUDED.city, state = EXCLUDED.state,
                              zip_code = EXCLUDED.zip_code, county = EXCLUDED.county,
                              latitude = EXCLUDED.latitude, longitude = EXCLUDED.longitude,
                              parcel_id = EXCLUDED.parcel_id,
                              legal_description = EXCLUDED.legal_description,
                              acreage = EXCLUDED.acreage,
                              details = EXCLUDED.details, updated_at = NOW()
            """), {
                "tid": tid, "mid": matter_id,
                "dn": result["display_name"],
                "a1": result.get("address_line1"),
                "city": result.get("city"), "state": result.get("state"),
                "zip": result.get("zip_code"), "county": result.get("county"),
                "lat": result.get("latitude"), "lng": result.get("longitude"),
                "pid": result.get("parcel_id"),
                "ld": (result.get("legal_description") or "")[:2000],
                "ac": result.get("acreage"),
                "det": det_json,
            })
    stats["updated"] = True
    return stats


async def _write_litigation_extraction(tid: str, matter_id: str, doc_id: str | None,
                                        result: dict, uid: int) -> dict:
    """Write litigation-specific extraction to DB."""
    stats = {"deal_points": 0, "contacts": 0, "identifiers": 0,
             "subject": False, "key_document": False, "metadata": False,
             "scheduling_dates": 0}

    async with AsyncSessionLocal() as session:
        async with session.begin():
            # ── Cause Number + Identifiers ──
            mm = result.get("matter_metadata", {})
            await _write_gf_and_cause(session, tid, matter_id, mm)
            stats["identifiers"] = await _write_identifiers(
                session, tid, matter_id, result.get("identifiers", []), uid)

            # ── Case Summary → matter_deal_points ──
            cs = result.get("case_summary", {})
            if cs.get("summary"):
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), 'case_summary', 'Case Summary',
                            :val, 'case_summary', :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "val": cs["summary"], "uid": uid or None})
                stats["deal_points"] += 1

            if cs.get("case_type"):
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), 'case_type', 'Case Type',
                            :val, 'text', :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "val": cs["case_type"], "uid": uid or None})
                stats["deal_points"] += 1

            # ── Scheduling Order → matter_deal_points ──
            for so in result.get("scheduling_order", []):
                if not so.get("label") or not so.get("date"):
                    continue
                pk = ("sched_" + so["label"].lower().replace(" ", "_"))[:95]
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), :pk, :pl, :pv, 'deadline', :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value,
                                  point_label = EXCLUDED.point_label, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "pk": pk,
                       "pl": so["label"], "pv": so["date"], "uid": uid or None})
                stats["deal_points"] += 1
                stats["scheduling_dates"] += 1

            # ── Causes of Action → matter_deal_points ──
            for i, coa in enumerate(result.get("causes_of_action", [])):
                if not coa.get("name"):
                    continue
                pk = f"coa_{i+1}_{coa['name'].lower().replace(' ', '_')[:30]}"[:95]
                elements = ", ".join(coa.get("elements", []))[:495]
                val = coa["name"]
                if coa.get("statute"):
                    val += f" ({coa['statute']})"
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, source_clause, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), :pk, :pl, :pv, 'cause_of_action', :sc, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value,
                                  source_clause = EXCLUDED.source_clause, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "pk": pk,
                       "pl": coa["name"], "pv": val,
                       "sc": elements if elements else None, "uid": uid or None})
                stats["deal_points"] += 1

            # ── Financial Exposure → matter_deal_points ──
            for fe in result.get("financial_exposure", []):
                if not fe.get("label"):
                    continue
                pk = ("fin_" + fe["label"].lower().replace(" ", "_"))[:95]
                pt = fe.get("type", "text")
                if pt not in ("currency", "percentage", "text"):
                    pt = "text"
                await session.execute(sa_text("""
                    INSERT INTO matter_deal_points
                        (tenant_id, matter_id, point_key, point_label,
                         point_value, point_type, updated_by)
                    VALUES (:tid, CAST(:mid AS uuid), :pk, :pl, :pv, :pt, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mdp_tenant_matter_key
                    DO UPDATE SET point_value = EXCLUDED.point_value, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "pk": pk,
                       "pl": fe["label"], "pv": fe.get("value", ""), "pt": pt,
                       "uid": uid or None})
                stats["deal_points"] += 1

            # ── Contacts / Parties (library-constrained) ──
            for party in result.get("parties", []):
                created = await _write_party(session, tid, matter_id, party, "litigation")
                if created:
                    stats["contacts"] += 1

            # ── Property / Subject ──
            prop = result.get("property")
            if prop and prop.get("display_name"):
                await session.execute(sa_text("""
                    INSERT INTO matter_subjects
                        (tenant_id, matter_id, subject_type, display_name,
                         address_line1, city, state, zip_code, county)
                    VALUES (:tid, CAST(:mid AS uuid), 'real_property', :dn,
                            :a1, :city, :state, :zip, :county)
                    ON CONFLICT ON CONSTRAINT uq_ms_tenant_matter
                    DO UPDATE SET display_name = EXCLUDED.display_name,
                                  address_line1 = EXCLUDED.address_line1,
                                  city = EXCLUDED.city, state = EXCLUDED.state, updated_at = NOW()
                """), {"tid": tid, "mid": matter_id, "dn": prop["display_name"],
                       "a1": prop.get("address_line1"), "city": prop.get("city"),
                       "state": prop.get("state"), "zip": prop.get("zip_code"),
                       "county": prop.get("county")})
                stats["subject"] = True

            # ── Key Document Link ──
            if doc_id:
                doc_class = result.get("document_classification", {})
                doc_type = doc_class.get("document_type", "other")
                await session.execute(sa_text("""
                    INSERT INTO matter_key_documents
                        (tenant_id, matter_id, document_id, document_role, label, linked_by)
                    VALUES (:tid, CAST(:mid AS uuid), CAST(:did AS uuid), :role, :label, :uid)
                    ON CONFLICT ON CONSTRAINT uq_mkd_tenant_matter_doc
                    DO UPDATE SET document_role = EXCLUDED.document_role, label = EXCLUDED.label
                """), {"tid": tid, "mid": matter_id, "did": doc_id,
                       "role": doc_type, "label": doc_class.get("title", ""),
                       "uid": uid or None})
                stats["key_document"] = True

            # ── Matter metadata auto-populate (only when null) ──
            for field in ("court", "judge", "cause_number", "jurisdiction", "practice_area"):
                val = mm.get(field)
                if val:
                    await session.execute(sa_text(f"""
                        UPDATE matters SET {field} = :val
                        WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                          AND ({field} IS NULL OR {field} = '')
                    """), {"val": val, "mid": matter_id, "tid": tid})

    return stats

# ── Endpoints ────────────────────────────────────────────────

@router.post("/extract")
async def extract_intelligence(request: Request):
    """Extract structured data from provided text or document."""
    tid = _tid(request)
    uid = _uid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    api_key = await _get_api_key(tid)
    if not api_key:
        return JSONResponse({"error": "No API key configured"}, 400)

    body = await request.json()
    ctx_type = body.get("context_type", "matter_provision")
    matter_id = body.get("matter_id", "")
    doc_id = body.get("document_id", "")
    contact_id = body.get("contact_id")
    content = body.get("content", "")

    if doc_id and not content:
        async with AsyncSessionLocal() as session:
            row = await session.execute(sa_text("""
                SELECT LEFT(extracted_text, 80000) FROM documents
                WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
            """), {"did": doc_id, "tid": tid})
            r = row.fetchone()
            if r and r[0]:
                content = r[0]

    if not content:
        return JSONResponse({"error": "No content to extract from"}, 400)

    content = content[:80000]

    try:
        if ctx_type in ("matter_provision", "document"):
            result = await _call_claude(api_key, SYSTEM_MATTER_PROVISION, content)
            if not result:
                return JSONResponse({"error": "AI extraction failed"}, 500)
            stats = await _write_matter_extraction(tid, matter_id, doc_id, result, uid)
            return JSONResponse({
                "ok": True, "context_type": ctx_type,
                "extraction": result, "stats": stats,
            })

        elif ctx_type == "witness":
            if not contact_id:
                return JSONResponse({"error": "contact_id required for witness enrichment"}, 400)
            result = await _call_claude(api_key, SYSTEM_WITNESS_ENRICH, content)
            if not result:
                return JSONResponse({"error": "AI extraction failed"}, 500)
            stats = await _write_witness_enrichment(tid, matter_id, contact_id, result)
            return JSONResponse({
                "ok": True, "context_type": ctx_type,
                "extraction": result, "stats": stats,
            })

        elif ctx_type == "property":
            result = await _call_claude(api_key, SYSTEM_PROPERTY_ENRICH, content)
            if not result:
                return JSONResponse({"error": "AI extraction failed"}, 500)
            stats = await _write_property_enrichment(tid, matter_id, result)
            return JSONResponse({
                "ok": True, "context_type": ctx_type,
                "extraction": result, "stats": stats,
            })

        else:
            return JSONResponse({"error": f"Unknown context_type: {ctx_type}"}, 400)

    except Exception as exc:
        log.exception("Extraction failed: %s", exc)
        return JSONResponse({"error": str(exc)}, 500)


@router.post("/matter-refresh")
async def matter_refresh(request: Request):
    """Read the entire matter document corpus and rebuild all intelligence.
    
    Dual-source: queries `documents` table first (upload/ingestion pipeline),
    then falls back to `dms_documents` via `matter_folders` path mapping
    for files that only exist in the DMS file tree.
    """
    tid = _tid(request)
    uid = _uid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    api_key = await _get_api_key(tid)
    if not api_key:
        return JSONResponse({"error": "No API key configured"}, 400)

    body = await request.json()
    matter_id = body.get("matter_id", "")
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)

    async with AsyncSessionLocal() as session:
        mt_row = await session.execute(sa_text(
            "SELECT matter_type FROM matters WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        mt_r = mt_row.fetchone()
        matter_type = mt_r[0] if mt_r else "litigation"

    is_litigation = matter_type == "litigation"
    extraction_prompt = SYSTEM_LITIGATION_EXTRACT if is_litigation else SYSTEM_MATTER_PROVISION

    # ── Source 1: `documents` table (upload/ingestion pipeline) ──
    docs = []
    async with AsyncSessionLocal() as session:
        rows = await session.execute(sa_text("""
            SELECT id::text AS doc_id, original_filename AS filename,
                   LEFT(extracted_text, 50000) AS txt,
                   LENGTH(extracted_text) AS txt_len,
                   'documents' AS source_table
            FROM documents
            WHERE matter_id = CAST(:mid AS uuid)
              AND TRIM(tenant_id) = :tid
              AND extracted_text IS NOT NULL
              AND LENGTH(extracted_text) > 100
            ORDER BY
                CASE
                    WHEN original_filename ILIKE '%%title%%commit%%' THEN 0
                    WHEN original_filename ILIKE '%%purchase%%sale%%' THEN 1
                    WHEN original_filename ILIKE '%%contract%%' THEN 2
                    WHEN original_filename ILIKE '%%agreement%%' THEN 3
                    WHEN original_filename ILIKE '%%complaint%%' THEN 4
                    WHEN original_filename ILIKE '%%petition%%' THEN 5
                    WHEN original_filename ILIKE '%%loi%%' THEN 6
                    WHEN original_filename ILIKE '%%letter%%intent%%' THEN 7
                    ELSE 10
                END,
                LENGTH(extracted_text) DESC
        """), {"mid": matter_id, "tid": tid})
        docs = [dict(r) for r in rows.mappings().fetchall()]

    # ── Source 2: `dms_documents` via `matter_folders` (DMS file tree) ──
    # Only query if `documents` returned fewer than 5 results — avoids
    # redundant scanning on matters that are fully in the pipeline.
    if len(docs) < 5:
        async with AsyncSessionLocal() as session:
            # Get folder paths for this matter
            fp_rows = await session.execute(sa_text("""
                SELECT folder_path, disk_root FROM matter_folders
                WHERE matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            folder_paths = []
            for r in fp_rows.fetchall():
                # Prefer disk_root (actual filesystem path); fall back to folder_path
                dr = (r[1] or "").strip()
                fp = (r[0] or "").strip()
                if dr:
                    folder_paths.append(dr)
                elif fp:
                    folder_paths.append(fp)

        # Collect doc IDs we already have from documents table to avoid dupes
        existing_doc_ids = {d["doc_id"] for d in docs}

        for fp in folder_paths:
            if not fp:
                continue
            async with AsyncSessionLocal() as session:
                dms_rows = await session.execute(sa_text("""
                    SELECT d.id::text AS doc_id,
                           REVERSE(SPLIT_PART(REVERSE(d.file_path), '/', 1)) AS filename,
                           LEFT(d.content_text, 50000) AS txt,
                           LENGTH(d.content_text) AS txt_len,
                           'dms_documents' AS source_table
                    FROM dms_documents d
                    WHERE TRIM(d.tenant_id) = :tid
                      AND d.file_path LIKE :prefix
                      AND d.content_text IS NOT NULL
                      AND LENGTH(d.content_text) > 100
                    ORDER BY
                        CASE
                            WHEN d.file_path ILIKE '%%title%%commit%%' THEN 0
                            WHEN d.file_path ILIKE '%%purchase%%sale%%' THEN 1
                            WHEN d.file_path ILIKE '%%contract%%' THEN 2
                            WHEN d.file_path ILIKE '%%agreement%%' THEN 3
                            WHEN d.file_path ILIKE '%%complaint%%' THEN 4
                            WHEN d.file_path ILIKE '%%petition%%' THEN 5
                            ELSE 10
                        END,
                        LENGTH(d.content_text) DESC
                    LIMIT 30
                """), {"tid": tid, "prefix": f"%{fp}%"})
                for r in dms_rows.mappings().fetchall():
                    rd = dict(r)
                    if rd["doc_id"] not in existing_doc_ids:
                        docs.append(rd)
                        existing_doc_ids.add(rd["doc_id"])

    if not docs:
        return JSONResponse({"error": "No documents with extracted text found (checked documents and DMS)"}, 400)

    results = []
    total_stats = {"documents_processed": 0, "deal_points": 0,
                   "contacts": 0, "identifiers": 0, "subject": False,
                   "key_documents": 0, "metadata": 0, "source_documents": 0,
                   "source_dms": 0}

    for doc in docs:
        content = doc["txt"]
        if not content or len(content) < 100:
            continue

        header = f"[Document: {doc['filename']}]\n\n"
        result = await _call_claude(api_key, extraction_prompt,
                                     header + content)
        if not result:
            log.warning("Extraction failed for doc %s", doc["doc_id"])
            continue

        doc_id_for_write = doc["doc_id"]
        if doc["source_table"] == "dms_documents":
            # DMS docs can link via dms_document_id in document_metadata
            # but cannot be referenced in matter_key_documents (different FK)
            doc_id_for_write = None
            total_stats["source_dms"] += 1
        else:
            total_stats["source_documents"] += 1

        if is_litigation:
            stats = await _write_litigation_extraction(
                tid, matter_id, doc_id_for_write, result, uid)
        else:
            stats = await _write_matter_extraction(
                tid, matter_id, doc_id_for_write, result, uid)

        # Write document_metadata for DMS docs separately
        if doc["source_table"] == "dms_documents":
            try:
                doc_class = result.get("document_classification", {})
                parties_json = json.dumps(result.get("parties", []))
                add_meta = json.dumps({
                    "deal_points": result.get("deal_points", []),
                    "key_dates": result.get("key_dates", []),
                    "property": result.get("property"),
                    "matter_metadata": result.get("matter_metadata"),
                    "identifiers": result.get("identifiers", []),
                })
                async with AsyncSessionLocal() as session:
                    async with session.begin():
                        await session.execute(sa_text("""
                            DELETE FROM document_metadata
                            WHERE dms_document_id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                        """), {"did": doc["doc_id"], "tid": tid})
                        await session.execute(sa_text("""
                            INSERT INTO document_metadata
                                (id, tenant_id, dms_document_id, extraction_method,
                                 title, date_on_document, parties_mentioned,
                                 additional_metadata, confidence, extracted_at)
                            VALUES (gen_random_uuid(), :tid, CAST(:did AS uuid),
                                    'claude_extract', :title,
                                    CASE WHEN :dod = '' THEN NULL ELSE CAST(:dod AS date) END,
                                    CAST(:parties AS jsonb),
                                    CAST(:meta AS jsonb), 0.85, NOW())
                        """), {
                            "tid": tid, "did": doc["doc_id"],
                            "title": doc_class.get("title", ""),
                            "dod": doc_class.get("date_on_document", ""),
                            "parties": parties_json, "meta": add_meta,
                        })
                stats["metadata"] = True
            except Exception as e:
                log.warning("Failed to write DMS doc metadata for %s: %s", doc["doc_id"], e)

        total_stats["documents_processed"] += 1
        total_stats["deal_points"] += stats["deal_points"]
        total_stats["contacts"] += stats["contacts"]
        total_stats["identifiers"] += stats.get("identifiers", 0)
        if stats.get("subject"):
            total_stats["subject"] = True
        if stats.get("key_document"):
            total_stats["key_documents"] += 1
        if stats.get("metadata"):
            total_stats["metadata"] += 1

        results.append({
            "document": doc["filename"],
            "doc_id": doc["doc_id"],
            "source": doc["source_table"],
            "stats": stats,
        })

    return JSONResponse({
        "ok": True,
        "matter_id": matter_id,
        "total_stats": total_stats,
        "documents": results,
    })


@router.post("/compose-layout")
async def compose_layout(request: Request):
    """After extraction, compose a widget layout based on what data exists."""
    tid = _tid(request)
    uid = _uid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    body = await request.json()
    matter_id = body.get("matter_id", "")
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)

    summary_parts = []
    async with AsyncSessionLocal() as session:
        mt = await session.execute(sa_text(
            "SELECT matter_type FROM matters WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        mt_row = mt.fetchone()
        matter_type = mt_row[0] if mt_row else "litigation"
        summary_parts.append(f"Matter type: {matter_type}")

        dp = await session.execute(sa_text(
            "SELECT COUNT(*) FROM matter_deal_points WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        dp_count = dp.scalar()

        kd = await session.execute(sa_text(
            "SELECT COUNT(*) FROM matter_key_documents WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        kd_count = kd.scalar()

        ms = await session.execute(sa_text(
            "SELECT subject_type, display_name, latitude, longitude, parcel_id FROM matter_subjects WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        ms_row = ms.fetchone()

        mc = await session.execute(sa_text(
            "SELECT COUNT(*) FROM matter_contacts WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid AND status = 'confirmed'"
        ), {"mid": matter_id, "tid": tid})
        mc_count = mc.scalar()

        mc2 = await session.execute(sa_text(
            "SELECT COUNT(*) FROM matter_contacts WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        mc_total = mc2.scalar()

    # Deterministic layout — no AI call needed
    widgets = []
    row = 1

    if matter_type == "transactional":
        if ms_row:
            widgets.append({"widget_slug": "deal_subject", "col_start": 1, "col_span": 12, "row": row})
            row += 1
        if dp_count > 0:
            widgets.append({"widget_slug": "deal_points", "col_start": 1, "col_span": 6, "row": row})
            widgets.append({"widget_slug": "deal_key_documents", "col_start": 7, "col_span": 6, "row": row})
        else:
            widgets.append({"widget_slug": "deal_key_documents", "col_start": 1, "col_span": 12, "row": row})
        row += 1
        if mc_total > 0:
            widgets.append({"widget_slug": "deal_parties", "col_start": 1, "col_span": 12, "row": row})
            row += 1
        widgets.append({"widget_slug": "billing_matter_kpi", "col_start": 1, "col_span": 12, "row": row})
        row += 1
        widgets.append({"widget_slug": "ai_drop_zone", "col_start": 1, "col_span": 12, "row": row})
    else:
        # Litigation
        widgets.append({"widget_slug": "deal_key_documents", "col_start": 1, "col_span": 12, "row": row})
        row += 1
        if dp_count > 0:
            widgets.append({"widget_slug": "deal_points", "col_start": 1, "col_span": 6, "row": row})
            widgets.append({"widget_slug": "deal_timeline", "col_start": 7, "col_span": 6, "row": row})
            row += 1
        if mc_total > 0:
            widgets.append({"widget_slug": "deal_parties", "col_start": 1, "col_span": 12, "row": row})
            row += 1
        widgets.append({"widget_slug": "billing_matter_kpi", "col_start": 1, "col_span": 12, "row": row})
        row += 1
        widgets.append({"widget_slug": "billing_matter_recent_slips", "col_start": 1, "col_span": 6, "row": row})
        widgets.append({"widget_slug": "billing_matter_open_invoices", "col_start": 7, "col_span": 6, "row": row})
        row += 1
        widgets.append({"widget_slug": "ai_drop_zone", "col_start": 1, "col_span": 12, "row": row})

    return JSONResponse({"ok": True, "layout": {"widgets": widgets}})
