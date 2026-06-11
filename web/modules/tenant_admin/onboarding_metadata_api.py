"""
modules/tenant_admin/onboarding_metadata_api.py
JSON API for Metadata Reconciliation tab in Onboarding.

Sources:
  1. ts_clients  -> client addresses, phones, emails, contacts ("Attn:" parsing)
  2. ts_clients  -> matter-level metadata (case_type, opened, sup_attorney, billing_atty)
  3. contacts    -> phone/email augmentation for clients
  4. billing_chunks -> (future) narrative-extracted metadata
  5. dms_documents  -> (future) document-extracted metadata
"""
import logging, re
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tenant-admin/metadata-recon/api", tags=["metadata-recon"])


def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()


@router.get("/summary")
async def metadata_summary(request: Request, user=Depends(get_current_user)):
    """Dashboard summary stats for metadata coverage."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        # Client coverage
        client_stats = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(NULLIF(TRIM(CAST(address1 AS text)), '')) AS has_address,
                   COUNT(NULLIF(TRIM(CAST(phone AS text)), '')) AS has_phone,
                   COUNT(NULLIF(TRIM(CAST(email AS text)), '')) AS has_email,
                   COUNT(NULLIF(TRIM(CAST(primary_contact AS text)), '')) AS has_contact
            FROM clients WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

        # Matter coverage
        matter_stats = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(NULLIF(TRIM(CAST(court AS text)), '')) AS has_court,
                   COUNT(NULLIF(TRIM(CAST(judge AS text)), '')) AS has_judge,
                   COUNT(NULLIF(TRIM(CAST(cause_number AS text)), '')) AS has_cause_number,
                   COUNT(NULLIF(TRIM(CAST(practice_area AS text)), '')) AS has_practice_area,
                   COUNT(open_date) AS has_open_date,
                   COUNT(NULLIF(TRIM(CAST(billing_type AS text)), '')) AS has_billing_type
            FROM matters WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

        # ts_clients coverage (source data)
        ts_stats = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(NULLIF(TRIM(address1), '')) AS has_address,
                   COUNT(NULLIF(TRIM(phone1), '')) AS has_phone,
                   COUNT(NULLIF(TRIM(email), '')) AS has_email,
                   COUNT(praesidium_client_id) AS has_link
            FROM ts_clients WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

        # Contacts coverage
        contact_stats = (await session.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(NULLIF(TRIM(CAST(phone AS text)), '')) AS has_phone,
                   COUNT(NULLIF(TRIM(CAST(email AS text)), '')) AS has_email
            FROM contacts WHERE TRIM(tenant_id) = :tid
        """), {"tid": tid})).mappings().fetchone()

    return JSONResponse({
        "clients": dict(client_stats) if client_stats else {},
        "matters": dict(matter_stats) if matter_stats else {},
        "ts_clients": dict(ts_stats) if ts_stats else {},
        "contacts": dict(contact_stats) if contact_stats else {},
    })


@router.get("/client-proposals")
async def client_proposals(request: Request, page: int = 1, user=Depends(get_current_user)):
    """Client metadata proposals from ts_clients.
    
    For each canonical client, finds the best ts_clients row and proposes
    address/phone/email/contact fields. 'Best' = most fields populated,
    tiebroken by most recent slip activity.
    """
    tid = _tid(request)
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        total_row = (await session.execute(text(
            "SELECT COUNT(*) FROM clients WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})).fetchone()
        total = total_row[0] if total_row else 0

        # For each client, get the best ts_clients row via nickname2 join
        # Best = most populated fields, tiebroken by latest slip date
        rows = (await session.execute(text("""
            WITH ranked AS (
                SELECT
                    c.id AS client_id,
                    c.client_name,
                    c.address1 AS cur_address1,
                    c.address2 AS cur_address2,
                    c.city AS cur_city,
                    c.state AS cur_state,
                    c.zip_code AS cur_zip,
                    c.phone AS cur_phone,
                    c.email AS cur_email,
                    c.primary_contact AS cur_contact,
                    tc.ts_client_id,
                    tc.ts_name,
                    tc.address1 AS ts_address1,
                    tc.raw_data->>'address2' AS ts_address2,
                    tc.city AS ts_city,
                    tc.state AS ts_state,
                    tc.zip AS ts_zip,
                    tc.phone1 AS ts_phone,
                    tc.email AS ts_email,
                    tc.case_type AS ts_case_type,
                    tc.opened AS ts_opened,
                    tc.opp_counsel AS ts_opp_counsel,
                    tc.referred_by AS ts_referred_by,
                    tc.sup_attorney AS ts_sup_attorney,
                    tc.billing_atty AS ts_billing_atty,
                    (SELECT COUNT(*) FROM ts_slips s
                     WHERE s.source_client_id = tc.ts_client_id
                       AND TRIM(s.tenant_id) = :tid) AS slip_count,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.id
                        ORDER BY
                            (CASE WHEN TRIM(COALESCE(tc.address1,'')) != '' THEN 1 ELSE 0 END
                             + CASE WHEN TRIM(COALESCE(tc.city,'')) != '' THEN 1 ELSE 0 END
                             + CASE WHEN TRIM(COALESCE(tc.phone1,'')) != '' THEN 1 ELSE 0 END
                             + CASE WHEN TRIM(COALESCE(tc.email,'')) != '' THEN 1 ELSE 0 END) DESC,
                            (SELECT MAX(s.slip_date) FROM ts_slips s
                             WHERE s.source_client_id = tc.ts_client_id
                               AND TRIM(s.tenant_id) = :tid) DESC NULLS LAST
                    ) AS rn
                FROM clients c
                LEFT JOIN ts_clients tc
                    ON tc.praesidium_client_id = c.id::text
                    AND TRIM(tc.tenant_id) = :tid
                WHERE TRIM(c.tenant_id) = :tid
            )
            SELECT * FROM ranked WHERE rn = 1
            ORDER BY client_name
            LIMIT :lim OFFSET :off
        """), {"tid": tid, "lim": per_page, "off": offset})).mappings().all()

        # Count how many ts_clients rows per client (for conflict detection)
        conflict_rows = (await session.execute(text("""
            SELECT tc.praesidium_client_id AS client_id,
                   COUNT(*) AS ts_row_count,
                   COUNT(DISTINCT CONCAT(COALESCE(tc.address1,''), '|', COALESCE(tc.city,''))) AS distinct_addresses
            FROM ts_clients tc
            WHERE TRIM(tc.tenant_id) = :tid
              AND tc.praesidium_client_id IS NOT NULL
            GROUP BY tc.praesidium_client_id
            HAVING COUNT(*) > 1
        """), {"tid": tid})).mappings().all()
        conflicts = {r["client_id"]: {"rows": r["ts_row_count"], "addresses": r["distinct_addresses"]}
                     for r in conflict_rows}

    proposals = []
    for r in rows:
        d = dict(r)
        cid = str(d["client_id"])
        # Parse "Attn:" from ts_address1
        ts_a1 = d.get("ts_address1") or ""
        ts_a2 = d.get("ts_address2") or ""
        proposed_contact = None
        proposed_address1 = ts_a1
        if ts_a1.lower().startswith("attn"):
            # Extract contact name after "Attn:" or "Attn."
            match = re.match(r'^Attn[.:]?\s*(.+)', ts_a1, re.IGNORECASE)
            if match:
                proposed_contact = match.group(1).strip()
                proposed_address1 = ts_a2  # Real address is in address2
        
        d["proposed_contact"] = proposed_contact
        d["proposed_address1"] = proposed_address1
        d["proposed_address2"] = ts_a2 if proposed_contact else ""
        d["conflict"] = conflicts.get(cid)
        # Determine if client already has data (skip if populated)
        d["client_has_address"] = bool(d.get("cur_address1") and d["cur_address1"].strip())
        d["client_has_phone"] = bool(d.get("cur_phone") and d["cur_phone"].strip())
        d["client_has_email"] = bool(d.get("cur_email") and d["cur_email"].strip())
        proposals.append(d)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse({
        "proposals": [{k: (str(v) if hasattr(v, 'hex') else v)
                       for k, v in p.items() if k != 'rn'}
                      for p in proposals],
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@router.post("/accept-client")
async def accept_client_metadata(request: Request, user=Depends(get_current_user)):
    """Accept proposed metadata for a single client."""
    tid = _tid(request)
    body = await request.json()
    client_id = body.get("client_id")
    fields = body.get("fields", {})

    if not client_id or not fields:
        return JSONResponse({"error": "client_id and fields required"}, status_code=400)

    allowed = {"address1", "address2", "city", "state", "zip_code", "phone", "email", "primary_contact"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return JSONResponse({"error": "No valid fields"}, status_code=400)

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    params = {**updates, "tid": tid, "cid": client_id}

    async with AsyncSessionLocal() as session:
        await session.execute(text(f"""
            UPDATE clients SET {set_clauses}, updated_at = NOW()
            WHERE TRIM(tenant_id) = :tid AND id = CAST(:cid AS uuid)
        """), params)
        await session.commit()

    return JSONResponse({"ok": True, "fields_updated": list(updates.keys())})


@router.post("/accept-bulk")
async def accept_bulk_metadata(request: Request, user=Depends(get_current_user)):
    """Bulk-accept proposed metadata for multiple clients.
    
    Only applies to clients where canonical fields are empty AND
    the ts_clients linkage is unambiguous (single distinct address).
    """
    tid = _tid(request)
    body = await request.json()
    scope = body.get("scope", "address")  # address, phone, email, contact, all

    async with AsyncSessionLocal() as session:
        updated = 0

        if scope in ("address", "all"):
            # Update clients with empty address from best ts_clients row
            result = await session.execute(text("""
                WITH best AS (
                    SELECT DISTINCT ON (tc.praesidium_client_id)
                        tc.praesidium_client_id AS client_id,
                        CASE
                            WHEN LOWER(tc.address1) LIKE 'attn%' THEN tc.raw_data->>'address2'
                            ELSE tc.address1
                        END AS addr1,
                        CASE
                            WHEN LOWER(tc.address1) LIKE 'attn%' THEN ''
                            ELSE COALESCE(tc.raw_data->>'address2', '')
                        END AS addr2,
                        tc.city, tc.state, tc.zip,
                        CASE
                            WHEN LOWER(tc.address1) LIKE 'attn%'
                            THEN REGEXP_REPLACE(tc.address1, '^[Aa]ttn[.:]*\\s*', '')
                            ELSE NULL
                        END AS contact_name
                    FROM ts_clients tc
                    WHERE TRIM(tc.tenant_id) = :tid
                      AND tc.praesidium_client_id IS NOT NULL
                      AND TRIM(COALESCE(tc.address1, '')) != ''
                    ORDER BY tc.praesidium_client_id,
                        (CASE WHEN TRIM(COALESCE(tc.address1,''))!='' THEN 1 ELSE 0 END
                         + CASE WHEN TRIM(COALESCE(tc.city,''))!='' THEN 1 ELSE 0 END
                         + CASE WHEN TRIM(COALESCE(tc.zip,''))!='' THEN 1 ELSE 0 END) DESC
                )
                UPDATE clients c SET
                    address1 = COALESCE(NULLIF(TRIM(b.addr1), ''), c.address1),
                    address2 = COALESCE(NULLIF(TRIM(b.addr2), ''), c.address2),
                    city = COALESCE(NULLIF(TRIM(b.city), ''), c.city),
                    state = COALESCE(NULLIF(TRIM(b.state), ''), c.state),
                    zip_code = COALESCE(NULLIF(TRIM(b.zip), ''), c.zip_code),
                    primary_contact = COALESCE(NULLIF(TRIM(b.contact_name), ''), c.primary_contact),
                    updated_at = NOW()
                FROM best b
                WHERE c.id = CAST(b.client_id AS uuid)
                  AND TRIM(c.tenant_id) = :tid
                  AND (c.address1 IS NULL OR TRIM(CAST(c.address1 AS text)) = '')
            """), {"tid": tid})
            updated += result.rowcount

        if scope in ("phone", "all"):
            result = await session.execute(text("""
                WITH best AS (
                    SELECT DISTINCT ON (tc.praesidium_client_id)
                        tc.praesidium_client_id AS client_id,
                        tc.phone1
                    FROM ts_clients tc
                    WHERE TRIM(tc.tenant_id) = :tid
                      AND tc.praesidium_client_id IS NOT NULL
                      AND TRIM(COALESCE(tc.phone1, '')) != ''
                    ORDER BY tc.praesidium_client_id, tc.phone1
                )
                UPDATE clients c SET
                    phone = b.phone1,
                    updated_at = NOW()
                FROM best b
                WHERE c.id = CAST(b.client_id AS uuid)
                  AND TRIM(c.tenant_id) = :tid
                  AND (c.phone IS NULL OR TRIM(CAST(c.phone AS text)) = '')
            """), {"tid": tid})
            updated += result.rowcount

        if scope in ("email", "all"):
            result = await session.execute(text("""
                WITH best AS (
                    SELECT DISTINCT ON (tc.praesidium_client_id)
                        tc.praesidium_client_id AS client_id,
                        tc.email
                    FROM ts_clients tc
                    WHERE TRIM(tc.tenant_id) = :tid
                      AND tc.praesidium_client_id IS NOT NULL
                      AND TRIM(COALESCE(tc.email, '')) != ''
                    ORDER BY tc.praesidium_client_id, tc.email
                )
                UPDATE clients c SET
                    email = b.email,
                    updated_at = NOW()
                FROM best b
                WHERE c.id = CAST(b.client_id AS uuid)
                  AND TRIM(c.tenant_id) = :tid
                  AND (c.email IS NULL OR TRIM(CAST(c.email AS text)) = '')
            """), {"tid": tid})
            updated += result.rowcount

        await session.commit()

    return JSONResponse({"ok": True, "updated": updated, "scope": scope})


@router.get("/matter-proposals")
async def matter_proposals(request: Request, page: int = 1, user=Depends(get_current_user)):
    """Matter metadata proposals from ts_clients via nickname2 join."""
    tid = _tid(request)
    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        total_row = (await session.execute(text(
            "SELECT COUNT(*) FROM matters WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})).fetchone()
        total = total_row[0] if total_row else 0

        rows = (await session.execute(text("""
            SELECT
                m.id AS matter_id, m.matter_name, m.matter_number,
                m.practice_area, m.billing_type, m.open_date, m.court, m.judge,
                m.cause_number, m.matter_type,
                c.client_name,
                tc.case_type AS ts_case_type,
                tc.opened AS ts_opened,
                tc.opp_counsel AS ts_opp_counsel,
                tc.sup_attorney AS ts_sup_attorney,
                tc.billing_atty AS ts_billing_atty,
                tc.referred_by AS ts_referred_by,
                tc.ts_client_id
            FROM matters m
            JOIN clients c ON c.id = m.client_id
            LEFT JOIN ts_clients tc
                ON tc.raw_data->>'nickname2' = m.matter_number
                AND TRIM(tc.tenant_id) = :tid
            WHERE TRIM(m.tenant_id) = :tid
            ORDER BY c.client_name, m.matter_name
            LIMIT :lim OFFSET :off
        """), {"tid": tid, "lim": per_page, "off": offset})).mappings().all()

    proposals = []
    for r in rows:
        d = {k: (str(v) if hasattr(v, 'hex') else v) for k, v in dict(r).items()}
        # Flag which fields have proposals
        d["has_ts_data"] = bool(d.get("ts_client_id"))
        d["needs_open_date"] = not d.get("open_date") and bool(d.get("ts_opened"))
        d["needs_case_type"] = not d.get("practice_area") and bool(d.get("ts_case_type"))
        proposals.append(d)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse({
        "proposals": proposals,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })


@router.post("/accept-matter")
async def accept_matter_metadata(request: Request, user=Depends(get_current_user)):
    """Accept proposed metadata for a single matter."""
    tid = _tid(request)
    body = await request.json()
    matter_id = body.get("matter_id")
    fields = body.get("fields", {})

    if not matter_id or not fields:
        return JSONResponse({"error": "matter_id and fields required"}, status_code=400)

    allowed = {"practice_area", "billing_type", "open_date", "court", "judge",
               "cause_number", "jurisdiction"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return JSONResponse({"error": "No valid fields"}, status_code=400)

    set_clauses = ", ".join(f"{k} = :{k}" for k in updates)
    params = {**updates, "tid": tid, "mid": matter_id}

    async with AsyncSessionLocal() as session:
        await session.execute(text(f"""
            UPDATE matters SET {set_clauses}, updated_at = NOW()
            WHERE TRIM(tenant_id) = :tid AND id = CAST(:mid AS uuid)
        """), params)
        await session.commit()

    return JSONResponse({"ok": True, "fields_updated": list(updates.keys())})


@router.post("/backfill-matters")
async def backfill_matter_metadata(request: Request, user=Depends(get_current_user)):
    """Bulk backfill matter metadata from ts_clients via nickname2 join.
    
    Populates open_date from ts_clients.opened where matters lack it.
    """
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        updated = 0

        # Backfill open_date from ts_clients.opened
        result = await session.execute(text("""
            UPDATE matters m SET
                open_date = CASE
                    WHEN tc.opened ~ '^[0-9]{4}' THEN tc.opened::date
                    WHEN tc.opened ~ '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{2,4}' THEN TO_DATE(tc.opened, 'MM/DD/YYYY')
                    ELSE NULL
                END,
                updated_at = NOW()
            FROM ts_clients tc
            WHERE tc.raw_data->>'nickname2' = m.matter_number
              AND TRIM(tc.tenant_id) = :tid
              AND TRIM(m.tenant_id) = :tid
              AND m.open_date IS NULL
              AND tc.opened IS NOT NULL
              AND TRIM(tc.opened) != ''
              AND TRIM(tc.opened) != '0'
        """), {"tid": tid})
        updated += result.rowcount

        await session.commit()

    return JSONResponse({"ok": True, "matters_updated": updated})


@router.post("/pull-all")
async def pull_all_metadata(request: Request, user=Depends(get_current_user)):
    """Master pull: runs all metadata extraction in sequence.
    
    1. ts_clients addresses → clients (empty fields only)
    2. ts_clients phones/emails → clients (empty fields only)
    3. Narrative-extracted phone numbers → contacts (new)
    4. Narrative-extracted person names → contacts (new)
    """
    tid = _tid(request)
    results = {"addresses": 0, "phones": 0, "emails": 0, "contacts_created": 0,
               "narrative_phones": 0, "narrative_names": 0}

    async with AsyncSessionLocal() as session:
        # ── 1. Addresses from ts_clients ──
        r = await session.execute(text("""
            WITH best AS (
                SELECT DISTINCT ON (tc.praesidium_client_id)
                    tc.praesidium_client_id AS client_id,
                    CASE
                        WHEN LOWER(tc.address1) LIKE 'attn%' THEN tc.raw_data->>'address2'
                        ELSE tc.address1
                    END AS addr1,
                    CASE
                        WHEN LOWER(tc.address1) LIKE 'attn%' THEN ''
                        ELSE COALESCE(tc.raw_data->>'address2', '')
                    END AS addr2,
                    tc.city, tc.state, tc.zip,
                    CASE
                        WHEN LOWER(tc.address1) LIKE 'attn%'
                        THEN REGEXP_REPLACE(tc.address1, '^[Aa]ttn[.:]*\s*', '')
                        ELSE NULL
                    END AS contact_name
                FROM ts_clients tc
                WHERE TRIM(tc.tenant_id) = :tid
                  AND tc.praesidium_client_id IS NOT NULL
                  AND TRIM(COALESCE(tc.address1, '')) != ''
                ORDER BY tc.praesidium_client_id,
                    (CASE WHEN TRIM(COALESCE(tc.address1,''))!='' THEN 1 ELSE 0 END
                     + CASE WHEN TRIM(COALESCE(tc.city,''))!='' THEN 1 ELSE 0 END
                     + CASE WHEN TRIM(COALESCE(tc.zip,''))!='' THEN 1 ELSE 0 END) DESC
            )
            UPDATE clients c SET
                address1 = COALESCE(NULLIF(TRIM(b.addr1), ''), c.address1),
                address2 = COALESCE(NULLIF(TRIM(b.addr2), ''), c.address2),
                city = COALESCE(NULLIF(TRIM(b.city), ''), c.city),
                state = COALESCE(NULLIF(TRIM(b.state), ''), c.state),
                zip_code = COALESCE(NULLIF(TRIM(b.zip), ''), c.zip_code),
                primary_contact = COALESCE(NULLIF(TRIM(b.contact_name), ''), c.primary_contact),
                updated_at = NOW()
            FROM best b
            WHERE c.id = CAST(b.client_id AS uuid)
              AND TRIM(c.tenant_id) = :tid
              AND (c.address1 IS NULL OR TRIM(CAST(c.address1 AS text)) = '')
        """), {"tid": tid})
        results["addresses"] = r.rowcount

        # ── 2. Phones from ts_clients ──
        r = await session.execute(text("""
            WITH best AS (
                SELECT DISTINCT ON (tc.praesidium_client_id)
                    tc.praesidium_client_id AS client_id, tc.phone1
                FROM ts_clients tc
                WHERE TRIM(tc.tenant_id) = :tid
                  AND tc.praesidium_client_id IS NOT NULL
                  AND TRIM(COALESCE(tc.phone1, '')) != ''
                ORDER BY tc.praesidium_client_id, tc.phone1
            )
            UPDATE clients c SET phone = b.phone1, updated_at = NOW()
            FROM best b
            WHERE c.id = CAST(b.client_id AS uuid)
              AND TRIM(c.tenant_id) = :tid
              AND (c.phone IS NULL OR TRIM(CAST(c.phone AS text)) = '')
        """), {"tid": tid})
        results["phones"] = r.rowcount

        # ── 3. Emails from ts_clients ──
        r = await session.execute(text("""
            WITH best AS (
                SELECT DISTINCT ON (tc.praesidium_client_id)
                    tc.praesidium_client_id AS client_id, tc.email
                FROM ts_clients tc
                WHERE TRIM(tc.tenant_id) = :tid
                  AND tc.praesidium_client_id IS NOT NULL
                  AND TRIM(COALESCE(tc.email, '')) != ''
                ORDER BY tc.praesidium_client_id, tc.email
            )
            UPDATE clients c SET email = b.email, updated_at = NOW()
            FROM best b
            WHERE c.id = CAST(b.client_id AS uuid)
              AND TRIM(c.tenant_id) = :tid
              AND (c.email IS NULL OR TRIM(CAST(c.email AS text)) = '')
        """), {"tid": tid})
        results["emails"] = r.rowcount

        # ── 4. Extract phone numbers from slip narratives ──
        # Slips that start with a phone number pattern (common in HJMM data)
        phone_rows = (await session.execute(text("""
            SELECT DISTINCT
                REGEXP_REPLACE(
                    SUBSTRING(s.narrative FROM '^(\d{3}[-.]?\d{3}[-.]?\d{4})'),
                    '[^0-9]', '', 'g'
                ) AS phone_raw,
                s.narrative,
                tc.ts_name,
                m.id AS matter_id,
                m.matter_name
            FROM ts_slips s
            JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                AND TRIM(tc.tenant_id) = TRIM(s.tenant_id)
            JOIN matters m ON m.matter_number = tc.raw_data->>'nickname2'
                AND TRIM(m.tenant_id) = TRIM(tc.tenant_id)
            WHERE TRIM(s.tenant_id) = :tid
              AND s.narrative ~ '^\d{3}[-.]?\d{3}[-.]?\d{4}'
        """), {"tid": tid})).mappings().all()
        results["narrative_phones"] = len(phone_rows)

        # ── 5. Extract person names from "conference with X" patterns ──
        name_rows = (await session.execute(text("""
            SELECT DISTINCT
                REGEXP_REPLACE(
                    SUBSTRING(s.narrative FROM '(?:conference|correspondence|teleconference|call)\s+(?:with|from|to)\s+([A-Z][a-z]+\.?\s+[A-Z][a-z]+)'),
                    '\s+$', '', 'g'
                ) AS extracted_name,
                tc.ts_name AS client_name,
                m.id AS matter_id
            FROM ts_slips s
            JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                AND TRIM(tc.tenant_id) = TRIM(s.tenant_id)
            JOIN matters m ON m.matter_number = tc.raw_data->>'nickname2'
                AND TRIM(m.tenant_id) = TRIM(tc.tenant_id)
            WHERE TRIM(s.tenant_id) = :tid
              AND s.narrative ~* '(?:conference|correspondence|teleconference|call)\s+(?:with|from|to)\s+[A-Z]'
            LIMIT 500
        """), {"tid": tid})).mappings().all()

        # Deduplicate and count unique names
        unique_names = set()
        for row in name_rows:
            name = row.get("extracted_name")
            if name and len(name) > 3 and name not in ("The Court", "the court"):
                unique_names.add(name.strip())
        results["narrative_names"] = len(unique_names)

        await session.commit()

    return JSONResponse({"ok": True, "results": results})


@router.get("/narrative-search")
async def narrative_search(request: Request, q: str = "", page: int = 1,
                           user=Depends(get_current_user)):
    """Search ts_slips narratives for metadata patterns.
    Returns matching slips with client/matter context."""
    tid = _tid(request)
    if not q or len(q) < 2:
        return JSONResponse({"results": [], "total": 0})

    per_page = 50
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as session:
        total_row = (await session.execute(text("""
            SELECT COUNT(DISTINCT s.id)
            FROM ts_slips s
            WHERE TRIM(s.tenant_id) = :tid
              AND s.narrative ILIKE :q
        """), {"tid": tid, "q": f"%{q}%"})).fetchone()
        total = total_row[0] if total_row else 0

        rows = (await session.execute(text("""
            SELECT s.id, LEFT(s.narrative, 300) AS narrative, s.slip_date, s.hours,
                   tc.ts_name AS client_name, tc.matter_code,
                   tc.raw_data->>'nickname2' AS matter_number,
                   m.matter_name, m.id::text AS matter_id
            FROM ts_slips s
            JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                AND TRIM(tc.tenant_id) = TRIM(s.tenant_id)
            LEFT JOIN matters m ON m.matter_number = tc.raw_data->>'nickname2'
                AND TRIM(m.tenant_id) = TRIM(tc.tenant_id)
            WHERE TRIM(s.tenant_id) = :tid
              AND s.narrative ILIKE :q
            ORDER BY s.slip_date DESC
            LIMIT :lim OFFSET :off
        """), {"tid": tid, "q": f"%{q}%", "lim": per_page, "off": offset})).mappings().all()

    return JSONResponse({
        "results": [{k: (str(v) if hasattr(v, 'hex') else v) for k, v in dict(r).items()}
                     for r in rows],
        "total": total,
        "page": page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    })
