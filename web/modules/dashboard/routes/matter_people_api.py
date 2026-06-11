"""
dashboard/routes/matter_people_api.py
======================================
People & Witnesses — JSON API for matter dashboard People tab.

GET  /api/v1/matter/{id}/people          — contacts + role library + proposals
POST /api/v1/matter/{id}/people          — add contact to matter
DELETE /api/v1/matter/{id}/people/{mc_id} — remove matter_contact
POST /api/v1/matter/{id}/people/{mc_id}/promote — promote proposed → confirmed
POST /api/v1/matter/{id}/people/seed     — scan emails/contacts for proposals

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["matter-people"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()

def _user_id(request: Request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) if u else None


class AddContactRequest(BaseModel):
    contact_id: Optional[int] = None
    # For new contact creation inline
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    role: str = "other"
    category: str = "people"  # people | witness
    notes: Optional[str] = None


# ─── GET PEOPLE ────────────────────────────────────────────────────

@router.get("/matter/{matter_id}/people")
async def get_matter_people(request: Request, matter_id: str):
    """All people + witnesses + proposals + role library for this matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    try:
        async with AsyncSessionLocal() as session:
            # 1. Get matter type to filter roles
            mt_r = await session.execute(sa_text("""
                SELECT matter_type FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            mt_row = mt_r.fetchone()
            if not mt_row:
                return JSONResponse({"error": "Matter not found"}, status_code=404)
            matter_type = mt_row[0] or "transactional"

            # 2. Confirmed people
            people_r = await session.execute(sa_text("""
                SELECT mc.id AS mc_id, mc.role, mc.category, mc.notes,
                       mc.source, mc.ai_summary, mc.created_at,
                       co.id AS contact_id, co.full_name, co.company,
                       co.email, co.phone, co.contact_type,
                       co.firm_name, co.bar_number, co.city, co.state,
                       co.address1, co.contact_type, co.notes AS contact_notes
                FROM matter_contacts mc
                JOIN contacts co ON mc.contact_id = co.id
                    AND TRIM(co.tenant_id) = :tid
                WHERE mc.matter_id = CAST(:mid AS uuid)
                  AND TRIM(mc.tenant_id) = :tid
                  AND COALESCE(mc.status, 'confirmed') = 'confirmed'
                ORDER BY mc.category,
                         CASE mc.role
                           WHEN 'client_contact' THEN 1
                           WHEN 'opposing_counsel' THEN 2
                           WHEN 'judge' THEN 3
                           ELSE 50
                         END,
                         co.full_name
            """), {"mid": matter_id, "tid": tid})

            confirmed = []
            for row in people_r.mappings():
                confirmed.append({
                    "mc_id": row["mc_id"],
                    "contact_id": row["contact_id"],
                    "full_name": row["full_name"] or "",
                    "company": row["company"] or "",
                    "firm_name": row["firm_name"] or "",
                    "email": row["email"] or "",
                    "phone": row["phone"] or "",
                    "city": row["city"] or "",
                    "state": row["state"] or "",
                    "bar_number": row["bar_number"] or "",
                    "role": row["role"] or "other",
                    "category": row["category"] or "people",
                    "notes": row["notes"] or "",
                    "source": row["source"] or "manual",
                    "ai_summary": row["ai_summary"] or "",
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "address1": row["address1"] or "",
                    "contact_type": row["contact_type"] or "",
                })

            # 3. Proposals (status = 'proposed')
            prop_r = await session.execute(sa_text("""
                SELECT mc.id AS mc_id, mc.role, mc.category, mc.notes,
                       mc.source, mc.ai_summary, mc.created_at,
                       co.id AS contact_id, co.full_name, co.company,
                       co.email, co.phone, co.firm_name,
                       co.bar_number, co.city, co.state, co.address1, co.contact_type
                FROM matter_contacts mc
                JOIN contacts co ON mc.contact_id = co.id
                    AND TRIM(co.tenant_id) = :tid
                WHERE mc.matter_id = CAST(:mid AS uuid)
                  AND TRIM(mc.tenant_id) = :tid
                  AND mc.status = 'proposed'
                ORDER BY mc.created_at DESC
            """), {"mid": matter_id, "tid": tid})

            proposals = []
            for row in prop_r.mappings():
                proposals.append({
                    "mc_id": row["mc_id"],
                    "contact_id": row["contact_id"],
                    "full_name": row["full_name"] or "",
                    "company": row["company"] or "",
                    "email": row["email"] or "",
                    "phone": row["phone"] or "",
                    "firm_name": row["firm_name"] or "",
                    "role": row["role"] or "other",
                    "category": row["category"] or "people",
                    "source": row["source"] or "",
                    "ai_summary": row["ai_summary"] or "",
                    "notes": row["notes"] or "",
                    "bar_number": row.get("bar_number") or "",
                    "city": row.get("city") or "",
                    "state": row.get("state") or "",
                    "address1": row.get("address1") or "",
                    "contact_type": row.get("contact_type") or "",
                })

            # 4. Role library filtered by matter type
            roles_r = await session.execute(sa_text("""
                SELECT code, display_name, category, matter_type_scope
                FROM contact_role_library
                WHERE is_active = true
                  AND (matter_type_scope IS NULL
                       OR :mt = ANY(matter_type_scope))
                ORDER BY category, sort_order
            """), {"mt": matter_type})
            roles = []
            for row in roles_r.mappings():
                roles.append({
                    "code": row["code"],
                    "display_name": row["display_name"],
                    "category": row["category"],
                })

        return JSONResponse({
            "matter_type": matter_type,
            "confirmed": confirmed,
            "proposals": proposals,
            "roles": roles,
        })

    except Exception as exc:
        logger.error("get_matter_people error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── ADD CONTACT ───────────────────────────────────────────────────

@router.post("/matter/{matter_id}/people")
async def add_matter_contact(request: Request, matter_id: str, body: AddContactRequest):
    """Add an existing or new contact to this matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    try:
        async with AsyncSessionLocal() as session:
            contact_id = body.contact_id

            # If no contact_id, create a new contact inline
            if not contact_id:
                if not body.full_name:
                    return JSONResponse({"error": "full_name required for new contact"}, status_code=400)
                ins_r = await session.execute(sa_text("""
                    INSERT INTO contacts (tenant_id, full_name, email, phone, company, contact_type, created_at, updated_at)
                    VALUES (:tid, :name, :email, :phone, :company, 'manual', NOW(), NOW())
                    RETURNING id
                """), {
                    "tid": tid, "name": body.full_name,
                    "email": body.email or None, "phone": body.phone or None,
                    "company": body.company or None,
                })
                contact_id = ins_r.scalar()

            # Check for duplicate
            dup_r = await session.execute(sa_text("""
                SELECT id FROM matter_contacts
                WHERE matter_id = CAST(:mid AS uuid)
                  AND contact_id = :cid
                  AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "cid": contact_id, "tid": tid})
            if dup_r.fetchone():
                return JSONResponse({"error": "Contact already on this matter"}, status_code=409)

            # Insert matter_contact
            await session.execute(sa_text("""
                INSERT INTO matter_contacts
                    (tenant_id, matter_id, contact_id, role, category, source, status, notes, created_at)
                VALUES (:tid, CAST(:mid AS uuid), :cid, :role, :cat, 'manual', 'confirmed', :notes, NOW())
            """), {
                "tid": tid, "mid": matter_id, "cid": contact_id,
                "role": body.role, "cat": body.category, "notes": body.notes or None,
            })
            await session.commit()

        return JSONResponse({"status": "ok", "contact_id": contact_id})

    except Exception as exc:
        logger.error("add_matter_contact error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── DELETE CONTACT ────────────────────────────────────────────────

@router.delete("/matter/{matter_id}/people/{mc_id}")
async def delete_matter_contact(request: Request, matter_id: str, mc_id: int):
    """Remove a contact from this matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                DELETE FROM matter_contacts
                WHERE id = :mcid
                  AND matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """), {"mcid": mc_id, "mid": matter_id, "tid": tid})
            await session.commit()
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("delete_matter_contact error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── PROMOTE PROPOSAL ─────────────────────────────────────────────

@router.post("/matter/{matter_id}/people/{mc_id}/promote")
async def promote_matter_contact(request: Request, matter_id: str, mc_id: int):
    """Promote a proposed contact to confirmed."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE matter_contacts
                SET status = 'confirmed'
                WHERE id = :mcid
                  AND matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND status = 'proposed'
            """), {"mcid": mc_id, "mid": matter_id, "tid": tid})
            await session.commit()
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        logger.error("promote_matter_contact error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── SEED FROM EMAIL ───────────────────────────────────────────────

@router.post("/matter/{matter_id}/people/seed")
async def seed_matter_contacts(request: Request, matter_id: str):
    """Scan emails + DMS documents for contacts using shared extraction service.
    Creates proposals for any contact not already linked to this matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    proposed = 0
    skipped = 0

    try:
        from core.services.contact_extraction import (
            extract_tier1, filter_internal_emails, merge_results, ExtractedContact
        )

        all_results = []

        async with AsyncSessionLocal() as session:
            # Get matter type
            mt_r = await session.execute(sa_text("""
                SELECT matter_type FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            mt_row = mt_r.fetchone()
            matter_type = mt_row[0] if mt_row else "transactional"

            # ── Source 1: Email routing queue ──
            from_r = await session.execute(sa_text("""
                SELECT DISTINCT from_email AS email, from_display AS display_name
                FROM email_routing_queue
                WHERE matched_matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND from_email IS NOT NULL AND from_email != ''
            """), {"mid": matter_id, "tid": tid})
            for row in from_r.mappings():
                all_results.append(ExtractedContact(
                    full_name=row["display_name"] or row["email"].split("@")[0].replace(".", " ").title(),
                    email=row["email"].lower().strip(),
                    source_type="email_queue",
                    confidence=0.9,
                ))

            to_r = await session.execute(sa_text("""
                SELECT DISTINCT jsonb_array_elements_text(to_emails) AS email
                FROM email_routing_queue
                WHERE matched_matter_id = CAST(:mid AS uuid)
                  AND TRIM(tenant_id) = :tid
                  AND to_emails IS NOT NULL
                  AND jsonb_typeof(to_emails) = 'array'
                  AND jsonb_array_length(to_emails) > 0
            """), {"mid": matter_id, "tid": tid})
            for row in to_r.mappings():
                addr = (row["email"] or "").lower().strip()
                if addr:
                    all_results.append(ExtractedContact(
                        full_name=addr.split("@")[0].replace(".", " ").title(),
                        email=addr,
                        source_type="email_queue",
                        confidence=0.7,
                    ))

            # ── Source 2: DMS documents (native — documents table) ──
            doc_r = await session.execute(sa_text("""
                SELECT d.filename, dd.content_text
                FROM documents d
                LEFT JOIN dms_documents dd
                    ON dd.file_path = d.storage_path
                    AND TRIM(dd.tenant_id) = :tid
                WHERE d.matter_id = CAST(:mid AS uuid)
                  AND TRIM(d.tenant_id) = :tid
                  AND dd.content_text IS NOT NULL
                  AND dd.content_text != ''
            """), {"mid": matter_id, "tid": tid})

            for row in doc_r.mappings():
                result = extract_tier1(
                    text=row["content_text"],
                    filename=row["filename"] or "",
                    matter_type=matter_type,
                )
                all_results.extend(result.contacts)

            # ── Source 3: DMS legacy docs via folder match ──
            leg_r = await session.execute(sa_text("""
                SELECT dd.file_path, LEFT(dd.content_text, 5000) AS content_text
                FROM dms_documents dd
                JOIN dms_folder_matches mf
                    ON TRIM(mf.tenant_id) = :tid
                    AND dd.file_path LIKE mf.folder_path || '%%'
                WHERE mf.matter_id = CAST(:mid AS uuid)
                  AND TRIM(dd.tenant_id) = :tid
                  AND dd.content_text IS NOT NULL
                  AND dd.content_text != ''
                  AND (dd.file_path LIKE '%%.msg' OR dd.file_path LIKE '%%.eml'
                       OR dd.file_path LIKE '%%.pdf' OR dd.file_path LIKE '%%.docx')
                LIMIT 50
            """), {"mid": matter_id, "tid": tid})

            for row in leg_r.mappings():
                fname = (row["file_path"] or "").rsplit("/", 1)[-1]
                result = extract_tier1(
                    text=row["content_text"] or "",
                    filename=fname,
                    matter_type=matter_type,
                )
                all_results.extend(result.contacts)

            # ── Dedup + filter internal ──
            filtered = filter_internal_emails(all_results)
            # Dedup by email key
            seen_keys = {}
            for c in filtered:
                k = c.key
                if not k:
                    continue
                if k not in seen_keys or c.confidence > seen_keys[k].confidence:
                    seen_keys[k] = c
            unique = list(seen_keys.values())

            # ── Create contacts + proposals ──
            for c in unique:
                if not c.email and not c.full_name:
                    continue

                # Find or create contact
                contact_id = None
                if c.email:
                    co_r = await session.execute(sa_text("""
                        SELECT id FROM contacts
                        WHERE TRIM(tenant_id) = :tid AND LOWER(TRIM(email)) = :email
                        LIMIT 1
                    """), {"tid": tid, "email": c.email.lower()})
                    co_row = co_r.fetchone()
                    if co_row:
                        contact_id = co_row[0]

                if not contact_id and c.full_name:
                    # Try name match
                    co_r = await session.execute(sa_text("""
                        SELECT id FROM contacts
                        WHERE TRIM(tenant_id) = :tid AND LOWER(TRIM(full_name)) = :name
                        LIMIT 1
                    """), {"tid": tid, "name": c.full_name.lower().strip()})
                    co_row = co_r.fetchone()
                    if co_row:
                        contact_id = co_row[0]

                if not contact_id:
                    # Create new contact
                    domain = c.email.split("@")[1] if c.email and "@" in c.email else ""
                    ins_r = await session.execute(sa_text("""
                        INSERT INTO contacts (tenant_id, full_name, email, phone, company, contact_type, created_at, updated_at)
                        VALUES (:tid, :name, :email, :phone, :company, :ctype, NOW(), NOW())
                        RETURNING id
                    """), {
                        "tid": tid,
                        "name": c.full_name or c.email.split("@")[0].replace(".", " ").title(),
                        "email": c.email or None,
                        "phone": c.phone or None,
                        "company": c.company or domain or None,
                        "ctype": c.source_type or "extraction",
                    })
                    contact_id = ins_r.scalar()

                # Check if already linked to this matter
                dup_r = await session.execute(sa_text("""
                    SELECT id FROM matter_contacts
                    WHERE matter_id = CAST(:mid AS uuid) AND contact_id = :cid AND TRIM(tenant_id) = :tid
                """), {"mid": matter_id, "cid": contact_id, "tid": tid})
                if dup_r.fetchone():
                    skipped += 1
                    continue

                # Insert as proposal
                await session.execute(sa_text("""
                    INSERT INTO matter_contacts
                        (tenant_id, matter_id, contact_id, role, category, source, status, ai_summary, created_at)
                    VALUES (:tid, CAST(:mid AS uuid), :cid, :role, :cat, :src, 'proposed', :summary, NOW())
                """), {
                    "tid": tid, "mid": matter_id, "cid": contact_id,
                    "role": c.role, "cat": c.category,
                    "src": c.source_type or "extraction",
                    "summary": c.raw_match[:200] if c.raw_match else None,
                })
                proposed += 1

            await session.commit()

        return JSONResponse({
            "status": "ok",
            "proposed": proposed,
            "skipped": skipped,
        })

    except Exception as exc:
        logger.error("seed_matter_contacts error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)




# ─── EDIT CONTACT ──────────────────────────────────────────────────

class EditContactRequest(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    firm_name: Optional[str] = None
    address1: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    bar_number: Optional[str] = None
    notes: Optional[str] = None
    # matter_contact level
    role: Optional[str] = None
    category: Optional[str] = None


@router.patch("/matter/{matter_id}/people/{mc_id}")
async def edit_matter_contact(request: Request, matter_id: str, mc_id: int, body: EditContactRequest):
    """Edit a contact's details and/or their role on this matter."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    try:
        async with AsyncSessionLocal() as session:
            # Get contact_id from matter_contacts
            mc_r = await session.execute(sa_text("""
                SELECT contact_id FROM matter_contacts
                WHERE id = :mcid AND matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mcid": mc_id, "mid": matter_id, "tid": tid})
            mc_row = mc_r.fetchone()
            if not mc_row:
                return JSONResponse({"error": "Not found"}, status_code=404)
            contact_id = mc_row[0]

            # Update contacts table fields
            contact_fields = {}
            for f in ("full_name", "email", "phone", "company", "firm_name", "address1", "city", "state", "bar_number", "notes"):
                val = getattr(body, f, None)
                if val is not None:
                    contact_fields[f] = val

            if contact_fields:
                sets = ", ".join(f"{k} = :{k}" for k in contact_fields)
                contact_fields["cid"] = contact_id
                contact_fields["tid"] = tid
                await session.execute(sa_text(f"""
                    UPDATE contacts SET {sets}, updated_at = NOW()
                    WHERE id = :cid AND TRIM(tenant_id) = :tid
                """), contact_fields)

            # Update matter_contacts fields (role, category, notes)
            mc_fields = {}
            if body.role is not None:
                mc_fields["role"] = body.role
            if body.category is not None:
                mc_fields["category"] = body.category

            if mc_fields:
                sets = ", ".join(f"{k} = :{k}" for k in mc_fields)
                mc_fields["mcid"] = mc_id
                mc_fields["mid"] = matter_id
                mc_fields["tid"] = tid
                await session.execute(sa_text(f"""
                    UPDATE matter_contacts SET {sets}
                    WHERE id = :mcid AND matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                """), mc_fields)

            await session.commit()

        return JSONResponse({"status": "ok"})

    except Exception as exc:
        logger.error("edit_matter_contact error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── DEEP SCAN (Tier 2 AI) ────────────────────────────────────────

@router.post("/matter/{matter_id}/people/deep-scan")
async def deep_scan_matter_contacts(request: Request, matter_id: str):
    """AI-powered contact extraction — uses Claude Haiku for speed.
    Scans key documents for full contact data (name, role, address, bar#, etc.)."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    proposed = 0
    updated = 0

    try:
        from core.services.contact_extraction import (
            extract_tier2, filter_internal_emails, ExtractedContact
        )

        async with AsyncSessionLocal() as session:
            # Get matter info
            mt_r = await session.execute(sa_text("""
                SELECT matter_type, matter_name FROM matters
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"mid": matter_id, "tid": tid})
            mt_row = mt_r.fetchone()
            if not mt_row:
                return JSONResponse({"error": "Matter not found"}, status_code=404)
            matter_type = mt_row[0] or "transactional"
            matter_name = mt_row[1] or ""

            # Get key docs + recent DMS docs with content
            doc_r = await session.execute(sa_text("""
                SELECT d.filename, dd.content_text
                FROM documents d
                LEFT JOIN dms_documents dd
                    ON dd.file_path = d.storage_path
                    AND TRIM(dd.tenant_id) = :tid
                WHERE d.matter_id = CAST(:mid AS uuid)
                  AND TRIM(d.tenant_id) = :tid
                  AND dd.content_text IS NOT NULL
                  AND LENGTH(dd.content_text) > 100
                ORDER BY d.created_at DESC
                LIMIT 10
            """), {"mid": matter_id, "tid": tid})

            all_contacts = []
            for row in doc_r.mappings():
                result = await extract_tier2(
                    text=row["content_text"],
                    filename=row["filename"] or "",
                    matter_type=matter_type,
                    matter_name=matter_name,
                    tenant_id=tid,
                )
                all_contacts.extend(result.contacts)

            # Filter internal + dedup
            filtered = filter_internal_emails(all_contacts)
            seen_keys = {}
            for c in filtered:
                k = c.key
                if not k:
                    continue
                if k not in seen_keys or c.confidence > seen_keys[k].confidence:
                    seen_keys[k] = c
            unique = list(seen_keys.values())

            # Upsert contacts
            for c in unique:
                if not c.email and not c.full_name:
                    continue

                contact_id = None
                # Find existing by email
                if c.email:
                    co_r = await session.execute(sa_text("""
                        SELECT id FROM contacts
                        WHERE TRIM(tenant_id) = :tid AND LOWER(TRIM(email)) = :email
                        LIMIT 1
                    """), {"tid": tid, "email": c.email.lower()})
                    co_row = co_r.fetchone()
                    if co_row:
                        contact_id = co_row[0]
                        # Enrich existing contact with new data
                        enrich_fields = {}
                        if c.phone:
                            enrich_fields["phone"] = c.phone
                        if c.company:
                            enrich_fields["company"] = c.company
                        if c.address:
                            enrich_fields["address1"] = c.address
                        if c.bar_number:
                            enrich_fields["bar_number"] = c.bar_number
                        extra = getattr(c, '_extra', {})
                        if extra.get('firm_name'):
                            enrich_fields["firm_name"] = extra['firm_name']
                        if extra.get('city'):
                            enrich_fields["city"] = extra['city']
                        if extra.get('state'):
                            enrich_fields["state"] = extra['state']
                        if enrich_fields:
                            sets = ", ".join(f"{k} = COALESCE(NULLIF({k}, ''), :{k})" for k in enrich_fields)
                            enrich_fields["cid"] = contact_id
                            enrich_fields["tid"] = tid
                            await session.execute(sa_text(f"""
                                UPDATE contacts SET {sets}, updated_at = NOW()
                                WHERE id = :cid AND TRIM(tenant_id) = :tid
                            """), enrich_fields)
                            updated += 1

                if not contact_id:
                    # Create new contact
                    ins_r = await session.execute(sa_text("""
                        INSERT INTO contacts (tenant_id, full_name, email, phone, company, firm_name,
                            address1, city, state, bar_number, contact_type, created_at, updated_at)
                        VALUES (:tid, :name, :email, :phone, :company, :firm,
                            :addr, :city, :st, :bar, 'ai_extraction', NOW(), NOW())
                        RETURNING id
                    """), {
                        "tid": tid, "name": c.full_name or "",
                        "email": c.email or None, "phone": c.phone or None,
                        "company": c.company or None,
                        "firm": getattr(c, '_extra', {}).get('firm_name') or None,
                        "addr": c.address or None,
                        "city": getattr(c, '_extra', {}).get('city') or None,
                        "st": getattr(c, '_extra', {}).get('state') or None,
                        "bar": c.bar_number or None,
                    })
                    contact_id = ins_r.scalar()

                # Link to matter if not already
                dup_r = await session.execute(sa_text("""
                    SELECT id FROM matter_contacts
                    WHERE matter_id = CAST(:mid AS uuid) AND contact_id = :cid AND TRIM(tenant_id) = :tid
                """), {"mid": matter_id, "cid": contact_id, "tid": tid})
                if not dup_r.fetchone():
                    await session.execute(sa_text("""
                        INSERT INTO matter_contacts
                            (tenant_id, matter_id, contact_id, role, category, source, status, ai_summary, created_at)
                        VALUES (:tid, CAST(:mid AS uuid), :cid, :role, :cat, 'ai_deep_scan', 'proposed', :summary, NOW())
                    """), {
                        "tid": tid, "mid": matter_id, "cid": contact_id,
                        "role": c.role, "cat": c.category,
                        "summary": c.raw_match[:200] if c.raw_match else None,
                    })
                    proposed += 1

            await session.commit()

        return JSONResponse({
            "status": "ok",
            "proposed": proposed,
            "updated": updated,
            "total_extracted": len(unique),
        })

    except Exception as exc:
        logger.error("deep_scan error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/contacts/search")
async def search_contacts(request: Request, q: str = ""):
    """Search contacts by name, email, company for typeahead."""
    tid = _tid(request)
    if not tid or not q.strip():
        return JSONResponse({"results": []})

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT id, full_name, email, phone, company, firm_name, contact_type
                FROM contacts
                WHERE TRIM(tenant_id) = :tid
                  AND (full_name ILIKE :q OR email ILIKE :q OR company ILIKE :q OR firm_name ILIKE :q)
                ORDER BY full_name
                LIMIT 15
            """), {"tid": tid, "q": f"%{q.strip()}%"})
            results = []
            for row in r.mappings():
                results.append({
                    "id": row["id"],
                    "full_name": row["full_name"] or "",
                    "email": row["email"] or "",
                    "phone": row["phone"] or "",
                    "company": row["company"] or "",
                    "firm_name": row["firm_name"] or "",
                })
        return JSONResponse({"results": results})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
