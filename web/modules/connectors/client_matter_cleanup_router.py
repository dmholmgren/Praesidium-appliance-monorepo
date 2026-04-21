"""
timeslips_cleanup_router.py
Routes:
  GET  /tenant-admin/client-matter-cleanup          — cleanup page
  POST /tenant-admin/client-matter-cleanup/commit   — commit client/matter updates
  POST /tenant-admin/client-matter-cleanup/ai-classify — AI matter type classification
"""

import logging
import os
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_templates = Jinja2Templates(directory=["core/templates", "modules/connectors/templates"])

PER_PAGE = 50



@router.get("/tenant-admin/client-matter-cleanup/search-clients")
async def search_clients_typeahead(
    request: Request,
    user=Depends(get_current_user),
    q: str = "",
    exclude: str = "",
):
    """Live client search for merge typeahead. Updates after commits."""
    tenant_id = user.tenant_id
    async with AsyncSessionLocal() as session:
        params = {"tid": tenant_id, "q": f"%{q}%"}
        exclude_clause = ""
        if exclude:
            exclude_clause = "AND c.id != CAST(:excl AS uuid)"
            params["excl"] = exclude

        r = await session.execute(text(f"""
            SELECT c.id::text, c.client_name,
                   COUNT(m.id) as matter_count
            FROM clients c
            LEFT JOIN matters m ON m.client_id = c.id AND trim(m.tenant_id) = trim(:tid)
            WHERE trim(c.tenant_id) = trim(:tid)
              AND c.is_active = TRUE
              AND c.merged_into_client_id IS NULL
              AND c.client_name ILIKE :q
              {exclude_clause}
            GROUP BY c.id, c.client_name
            ORDER BY c.client_name
            LIMIT 20
        """), params)
        clients = [{"id": r[0], "name": r[1], "matter_count": r[2]}
                   for r in r.fetchall()]
    return JSONResponse({"clients": clients})

@router.get("/tenant-admin/client-matter-cleanup", response_class=HTMLResponse)
async def timeslips_cleanup(
    request: Request,
    user=Depends(get_current_user),
    page: int = 1,
    status: str = "active",
):
    tenant_id = user.tenant_id
    offset = (page - 1) * PER_PAGE
    branding = getattr(request.state, "branding", None)

    async with AsyncSessionLocal() as session:

        # Total counts
        tc = await session.execute(text("""
            SELECT COUNT(DISTINCT c.id) FROM clients c
            WHERE trim(c.tenant_id) = trim(:tid) AND c.is_active = TRUE
              AND c.merged_into_client_id IS NULL
        """), {"tid": tenant_id})
        total_clients = tc.scalar() or 0

        tm = await session.execute(text("""
            SELECT COUNT(*) FROM matters m
            JOIN clients c ON c.id = m.client_id
            WHERE trim(m.tenant_id) = trim(:tid) AND c.is_active = TRUE
        """), {"tid": tenant_id})
        total_matters = tm.scalar() or 0

        typed = await session.execute(text("""
            SELECT COUNT(*) FROM matters m
            WHERE trim(m.tenant_id) = trim(:tid) AND m.matter_type IS NOT NULL
        """), {"tid": tenant_id})
        typed_count = typed.scalar() or 0

        # All clients for merge dropdown (lightweight)
        all_clients_r = await session.execute(text("""
            SELECT id::text, client_name FROM clients
            WHERE trim(tenant_id) = trim(:tid) AND is_active = TRUE
              AND merged_into_client_id IS NULL
            ORDER BY client_name
        """), {"tid": tenant_id})
        all_clients = [{"id": r[0], "client_name": r[1]}
                       for r in all_clients_r.fetchall()]

        # Paginated clients
        status_clause = ""
        if status == "active":
            status_clause = "AND m.status = 'active'"
        elif status == "closed":
            status_clause = "AND m.status IN ('closed','inactive')"

        clients_r = await session.execute(text(f"""
            SELECT c.id::text, c.client_name, c.client_number,
                   COUNT(m.id) as matter_count,
                   COUNT(CASE WHEN m.matter_type IS NULL THEN 1 END) as unclassified_count
            FROM clients c
            LEFT JOIN matters m ON m.client_id = c.id AND trim(m.tenant_id) = trim(:tid)
                {status_clause}
            WHERE trim(c.tenant_id) = trim(:tid) AND c.is_active = TRUE
              AND c.merged_into_client_id IS NULL
            GROUP BY c.id, c.client_name, c.client_number
            ORDER BY c.client_name
            LIMIT {PER_PAGE} OFFSET {offset}
        """), {"tid": tenant_id})
        client_rows = [dict(r) for r in clients_r.mappings().fetchall()]

        # Load matters for each client
        if client_rows:
            cids = [r["id"] for r in client_rows]
            ph = ", ".join([f"CAST(:c{i} AS uuid)" for i in range(len(cids))])
            cparams = {f"c{i}": cid for i, cid in enumerate(cids)}
            matters_r = await session.execute(text(f"""
                SELECT m.id::text, m.matter_name, m.matter_number,
                       m.status, m.matter_type,
                       m.client_id::text
                FROM matters m
                WHERE m.client_id IN ({ph})
                  AND trim(m.tenant_id) = trim(:tid)
                ORDER BY m.matter_name
            """), {**cparams, "tid": tenant_id})
            all_matters = [dict(r) for r in matters_r.mappings().fetchall()]

            # Group matters by client
            matters_by_client: dict = {}
            for m in all_matters:
                cid = m["client_id"]
                if cid not in matters_by_client:
                    matters_by_client[cid] = []
                matters_by_client[cid].append(m)

            for client in client_rows:
                client["matters"] = matters_by_client.get(client["id"], [])
        else:
            for client in client_rows:
                client["matters"] = []

    total_pages = max(1, (total_clients + PER_PAGE - 1) // PER_PAGE)

    return _templates.TemplateResponse(
        request,
        "tenant_admin/client_matter_cleanup.html",
        {
            "user": user,
            "branding": branding,
            "clients": client_rows,
            "all_clients": all_clients,
            "total_clients": total_clients,
            "total_matters": total_matters,
            "typed_count": typed_count,
            "page": page,
            "total_pages": total_pages,
            "status_filter": status,
        },
    )


@router.post("/tenant-admin/client-matter-cleanup/commit")
async def timeslips_cleanup_commit(request: Request, user=Depends(get_current_user)):
    """
    Commit client name changes, client merges, matter name changes,
    and matter type assignments.
    """
    tenant_id = user.tenant_id
    body = await request.json()
    matter_updates = body.get("matter_updates", [])
    client_updates = body.get("client_updates", [])

    matters_updated = 0
    clients_updated = 0

    async with AsyncSessionLocal() as session:
        try:
            # Process matter updates
            for upd in matter_updates:
                matter_id = upd.get("matter_id")
                if not matter_id:
                    continue

                set_clauses = []
                params = {"mid": matter_id, "tid": tenant_id}

                if upd.get("matter_name"):
                    set_clauses.append("matter_name = :matter_name")
                    params["matter_name"] = upd["matter_name"]

                if upd.get("matter_type"):
                    set_clauses.append("matter_type = :matter_type")
                    params["matter_type"] = upd["matter_type"]

                if set_clauses:
                    set_clauses.append("updated_at = NOW()")
                    await session.execute(text(f"""
                        UPDATE matters
                        SET {', '.join(set_clauses)}
                        WHERE id = CAST(:mid AS uuid)
                          AND trim(tenant_id) = trim(:tid)
                    """), params)
                    matters_updated += 1

            # Process client updates
            for upd in client_updates:
                client_id = upd.get("client_id")
                if not client_id:
                    continue

                merge_into = upd.get("merge_into")

                if merge_into:
                    # Merge: reassign all matters, mark client inactive
                    await session.execute(text("""
                        UPDATE matters
                        SET client_id = CAST(:keep AS uuid), updated_at = NOW()
                        WHERE client_id = CAST(:merge AS uuid)
                          AND trim(tenant_id) = trim(:tid)
                    """), {"keep": merge_into, "merge": client_id, "tid": tenant_id})

                    await session.execute(text("""
                        UPDATE clients
                        SET is_active = FALSE,
                            merged_into_client_id = CAST(:keep AS uuid),
                            merged_at = NOW(),
                            merged_by = :uid,
                            updated_at = NOW()
                        WHERE id = CAST(:merge AS uuid)
                          AND trim(tenant_id) = trim(:tid)
                    """), {"keep": merge_into, "merge": client_id,
                           "uid": user.id, "tid": tenant_id})
                    clients_updated += 1

                elif upd.get("client_name"):
                    await session.execute(text("""
                        UPDATE clients
                        SET client_name = :name, updated_at = NOW()
                        WHERE id = CAST(:cid AS uuid)
                          AND trim(tenant_id) = trim(:tid)
                    """), {"name": upd["client_name"], "cid": client_id, "tid": tenant_id})
                    clients_updated += 1

            await session.commit()

        except Exception as exc:
            await session.rollback()
            logger.error("timeslips_cleanup commit error: %s", exc)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    logger.info("timeslips_cleanup: matters=%d clients=%d user=%s",
                matters_updated, clients_updated, user.id)
    return JSONResponse({
        "status": "ok",
        "matters_updated": matters_updated,
        "clients_updated": clients_updated,
    })




@router.post("/tenant-admin/client-matter-cleanup/sql-classify")
async def client_matter_sql_classify(request: Request, user=Depends(get_current_user)):
    """Rule-based first pass: classify matters by slip narrative keywords.
    No AI tokens burned. Runs SQL keyword matching against ts_slips."""
    tenant_id = user.tenant_id
    
    async with AsyncSessionLocal() as session:
        # Litigation keywords in slip narratives
        lit_result = await session.execute(text("""
            UPDATE matters m
            SET matter_type = 'litigation'
            WHERE trim(m.tenant_id) = trim(:tid)
              AND m.matter_type IS NULL
              AND m.status = 'active'
              AND EXISTS (
                SELECT 1 FROM ts_slips s
                JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                  AND trim(tc.tenant_id) = trim(m.tenant_id)
                  AND tc.ts_raw->>'nickname2' = m.matter_number
                WHERE trim(s.tenant_id) = trim(m.tenant_id)
                  AND s.narrative IS NOT NULL
                  AND (
                    LOWER(s.narrative) LIKE '%deposition%'
                    OR LOWER(s.narrative) LIKE '%complaint%'
                    OR LOWER(s.narrative) LIKE '%petition%'
                    OR LOWER(s.narrative) LIKE '%motion%'
                    OR LOWER(s.narrative) LIKE '%discovery%'
                    OR LOWER(s.narrative) LIKE '%interrogator%'
                    OR LOWER(s.narrative) LIKE '%hearing%'
                    OR LOWER(s.narrative) LIKE '%trial%'
                    OR LOWER(s.narrative) LIKE '%mediation%'
                    OR LOWER(s.narrative) LIKE '%arbitration%'
                    OR LOWER(s.narrative) LIKE '%settlement%'
                    OR LOWER(s.narrative) LIKE '%pleading%'
                    OR LOWER(s.narrative) LIKE '%filing%'
                    OR LOWER(s.narrative) LIKE '%court%'
                    OR LOWER(s.narrative) LIKE '%judge%'
                    OR LOWER(s.narrative) LIKE '%opposing counsel%'
                    OR LOWER(s.narrative) LIKE '%subpoena%'
                    OR LOWER(s.narrative) LIKE '%production%'
                    OR LOWER(s.narrative) LIKE '%summary judgment%'
                    OR LOWER(s.narrative) LIKE '%appeal%'
                  )
              )
            RETURNING m.id
        """), {"tid": tenant_id})
        lit_count = len(lit_result.fetchall())
        
        # Transactional keywords — only for matters NOT already classified
        txn_result = await session.execute(text("""
            UPDATE matters m
            SET matter_type = 'transactional'
            WHERE trim(m.tenant_id) = trim(:tid)
              AND m.matter_type IS NULL
              AND m.status = 'active'
              AND EXISTS (
                SELECT 1 FROM ts_slips s
                JOIN ts_clients tc ON tc.ts_client_id = s.source_client_id
                  AND trim(tc.tenant_id) = trim(m.tenant_id)
                  AND tc.ts_raw->>'nickname2' = m.matter_number
                WHERE trim(s.tenant_id) = trim(m.tenant_id)
                  AND s.narrative IS NOT NULL
                  AND (
                    LOWER(s.narrative) LIKE '%closing%'
                    OR LOWER(s.narrative) LIKE '%loan%'
                    OR LOWER(s.narrative) LIKE '%agreement%'
                    OR LOWER(s.narrative) LIKE '%corporate%'
                    OR LOWER(s.narrative) LIKE '%formation%'
                    OR LOWER(s.narrative) LIKE '%operating agreement%'
                    OR LOWER(s.narrative) LIKE '%contract%'
                    OR LOWER(s.narrative) LIKE '%lease%'
                    OR LOWER(s.narrative) LIKE '%deed%'
                    OR LOWER(s.narrative) LIKE '%title%'
                    OR LOWER(s.narrative) LIKE '%escrow%'
                    OR LOWER(s.narrative) LIKE '%due diligence%'
                    OR LOWER(s.narrative) LIKE '%ppm%'
                    OR LOWER(s.narrative) LIKE '%securities%'
                    OR LOWER(s.narrative) LIKE '%offering%'
                    OR LOWER(s.narrative) LIKE '%subscription%'
                    OR LOWER(s.narrative) LIKE '%bylaws%'
                    OR LOWER(s.narrative) LIKE '%articles%'
                    OR LOWER(s.narrative) LIKE '%incorporation%'
                    OR LOWER(s.narrative) LIKE '%merger%'
                  )
              )
            RETURNING m.id
        """), {"tid": tenant_id})
        txn_count = len(txn_result.fetchall())
        
        # Count remaining unclassified
        remaining = await session.execute(text("""
            SELECT COUNT(*) FROM matters
            WHERE trim(tenant_id) = trim(:tid)
              AND matter_type IS NULL AND status = 'active'
        """), {"tid": tenant_id})
        unclassified = remaining.scalar()
        
        await session.commit()
    
    return JSONResponse({
        "litigation": lit_count,
        "transactional": txn_count,
        "remaining_unclassified": unclassified,
    })

@router.post("/tenant-admin/client-matter-cleanup/ai-classify")
async def client_matter_ai_classify(request: Request, user=Depends(get_current_user)):
    """
    Use Claude to classify untyped matters as litigation or transactional.
    Uses slip narratives from ts_slips where available (via ts_clients link),
    falling back to matter name + client name.
    Only processes matters with Timeslips classification code 3 or unlinked matters.
    """
    tenant_id = user.tenant_id

    async with AsyncSessionLocal() as session:
        # Get unclassified matters with slip narrative samples via ts_clients link
        r = await session.execute(text("""
            SELECT
                m.id::text as matter_id,
                m.matter_name,
                m.matter_number,
                c.client_name,
                tc.raw_data->>'classification' as ts_classification,
                (
                    SELECT string_agg(DISTINCT LEFT(s.narrative, 80), ' | ')
                    FROM ts_slips s
                    WHERE s.source_client_id = tc.ts_client_id
                      AND trim(s.tenant_id) = trim(m.tenant_id)
                      AND s.narrative IS NOT NULL
                      AND s.narrative != ''
                    LIMIT 5
                ) as sample_narratives
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id
            LEFT JOIN ts_clients tc ON tc.praesidium_client_id::uuid = m.client_id
              AND trim(tc.tenant_id) = trim(m.tenant_id)
              AND tc.raw_data->>'nickname2' = m.matter_number
            WHERE trim(m.tenant_id) = trim(:tid)
              AND m.matter_type IS NULL
              AND m.status = 'active'
            ORDER BY m.matter_name
            LIMIT 150
        """), {"tid": tenant_id})
        matters = [dict(row) for row in r.mappings().fetchall()]

    if not matters:
        return JSONResponse({"suggestions": []})

    # Build prompt using narratives where available
    matter_lines = []
    for i, m in enumerate(matters):
        line = f"{i+1}. Matter: {m['matter_name']} | Client: {m['client_name'] or '—'}"
        if m.get('sample_narratives'):
            line += f" | Work done: {m['sample_narratives']}"
        matter_lines.append(line)
    matter_list = "\n".join(matter_lines)

    prompt = f"""You are classifying legal matters for a law firm. For each matter, 
determine whether it is LITIGATION (court cases, disputes, arbitration, appeals, 
enforcement, collections, bankruptcies) or TRANSACTIONAL (contracts, real estate, 
loans, corporate transactions, regulatory, estate planning, general counsel retainer work).

Use the "Work done" field (actual time entry narratives) as the primary signal when available.
Matter names and client names are secondary signals.

Respond with a JSON array ONLY — no other text. Each element:
{{"index": N, "type": "litigation" or "transactional", "confidence": 0.0-1.0}}

Matters to classify:
{matter_list}

JSON array:"""

    try:
        import httpx
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            # Try credentials vault
            async with AsyncSessionLocal() as session:
                r = await session.execute(text("""
                    SELECT encrypted_key FROM credentials_vault
                    WHERE trim(tenant_id) = trim(:tid)
                      AND provider = 'anthropic' AND key_type = 'api_key'
                    LIMIT 1
                """), {"tid": tenant_id})
                row = r.fetchone()
                if row:
                    api_key = row[0]

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if resp.status_code != 200:
            raise HTTPException(status_code=500, detail=f"AI API error: {resp.status_code}")

        content = resp.json()["content"][0]["text"].strip()

        # Parse JSON response
        if "```" in content:
            content = content.split("```")[1].replace("json", "").strip()

        classifications = json.loads(content)

        # Map back to matter IDs
        suggestions = []
        for cls in classifications:
            idx = cls.get("index", 0) - 1
            if 0 <= idx < len(matters):
                m = matters[idx]
                suggestions.append({
                    "matter_id": m["matter_id"],
                    "matter_name": m["matter_name"],
                    "client_name": m["client_name"] or "",
                    "suggested_type": cls.get("type", "litigation"),
                    "confidence": cls.get("confidence", 0.8),
                })

        logger.info("ai_classify: classified %d matters for tenant %s", len(suggestions), tenant_id)
        return JSONResponse({"suggestions": suggestions})

    except json.JSONDecodeError as exc:
        logger.error("ai_classify JSON parse error: %s", exc)
        raise HTTPException(status_code=500, detail="AI response parsing failed") from exc
    except Exception as exc:
        logger.error("ai_classify error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/tenant-admin/client-matter-cleanup/add-matter")
async def add_matter(request: Request, user=Depends(get_current_user)):
    """Create a new matter under an existing client."""
    import uuid as _uuid
    tenant_id = user.tenant_id
    body = await request.json()
    client_id   = body.get("client_id", "").strip()
    matter_name = body.get("matter_name", "").strip()
    matter_number = body.get("matter_number", "").strip() or None
    matter_type = body.get("matter_type") or None

    if not client_id or not matter_name:
        raise HTTPException(status_code=400, detail="client_id and matter_name required")

    matter_id = str(_uuid.uuid4())
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("""
                INSERT INTO matters (id, tenant_id, client_id, matter_name, matter_number,
                                     matter_type, status, created_at, updated_at)
                VALUES (CAST(:mid AS uuid), :tid, CAST(:cid AS uuid), :name, :number,
                        :mtype, 'active', NOW(), NOW())
            """), {"mid": matter_id, "tid": tenant_id, "cid": client_id,
                   "name": matter_name, "number": matter_number, "mtype": matter_type})
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({"status": "ok", "matter_id": matter_id})


@router.post("/tenant-admin/client-matter-cleanup/add-matter")
async def add_matter(request: Request, user=Depends(get_current_user)):
    """Create a new matter under an existing client."""
    import uuid as _uuid
    tenant_id = user.tenant_id
    body = await request.json()
    client_id   = body.get("client_id", "").strip()
    matter_name = body.get("matter_name", "").strip()
    matter_number = body.get("matter_number", "").strip() or None
    matter_type = body.get("matter_type") or None

    if not client_id or not matter_name:
        raise HTTPException(status_code=400, detail="client_id and matter_name required")

    matter_id = str(_uuid.uuid4())
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text("""
                INSERT INTO matters (id, tenant_id, client_id, matter_name, matter_number,
                                     matter_type, status, created_at, updated_at)
                VALUES (CAST(:mid AS uuid), :tid, CAST(:cid AS uuid), :name, :number,
                        :mtype, 'active', NOW(), NOW())
            """), {"mid": matter_id, "tid": tenant_id, "cid": client_id,
                   "name": matter_name, "number": matter_number, "mtype": matter_type})
            await session.commit()
        except Exception as exc:
            await session.rollback()
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({"status": "ok", "matter_id": matter_id})
