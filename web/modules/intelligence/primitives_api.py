"""
modules/intelligence/primitives_api.py — Matter Primitives API
Reads extraction primitives, returns data shaped for dashboard widgets.
dms_documents has no matter_id — joins through file_path pattern.
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.intelligence.primitives")
router = APIRouter(prefix="/api/v1/matter", tags=["matter-primitives"])

def _tid(req: Request) -> str:
    return (getattr(req.state, "tenant_id", "") or "").strip()

async def _resolve_matter_path(tid, matter_id):
    async with AsyncSessionLocal() as session:
        row = await session.execute(sa_text(
            "SELECT c.client_name, m.matter_name FROM matters m "
            "LEFT JOIN clients c ON m.client_id = c.id "
            "WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        r = row.fetchone()
        if not r or not r[1]:
            return None
        client = r[0] or ""
        matter = r[1] or ""
        if client and matter:
            return f"%/{client}/{matter}/%"
        return f"%/{matter}/%"

@router.get("/{matter_id}/primitives")
async def get_matter_primitives(matter_id: str, request: Request):
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, 400)

    path_pattern = await _resolve_matter_path(tid, matter_id)
    if not path_pattern:
        return JSONResponse({"ok": True, "matter_id": matter_id,
            "deal_points": [], "key_documents": [], "key_contacts": [],
            "subject": None, "causes_of_action": [], "stats": {}})

    async with AsyncSessionLocal() as session:
        # 1. Future deadlines
        rows = await session.execute(sa_text("""
            SELECT DISTINCT ON (dd.deadline_date)
                dd.id::text, dd.deadline_text, dd.deadline_date,
                dd.confidence, dd.extraction_method
            FROM document_deadlines dd
            JOIN dms_documents doc ON dd.dms_document_id = doc.id
            WHERE TRIM(dd.tenant_id) = :tid
              AND doc.file_path LIKE :pattern
              AND dd.superseded_by_run_id IS NULL
              AND dd.deadline_date IS NOT NULL
              AND dd.deadline_date >= CURRENT_DATE
              AND dd.confidence >= 0.70
            ORDER BY dd.deadline_date, dd.confidence DESC
        """), {"tid": tid, "pattern": path_pattern})

        scheduling_deadlines = []
        for r in rows.mappings():
            txt = (r["deadline_text"] or "").strip()
            if len(txt) > 120:
                txt = txt[:117] + "..."
            scheduling_deadlines.append({
                "id": r["id"],
                "point_key": f"deadline_{r['deadline_date']}",
                "point_label": txt,
                "point_value": str(r["deadline_date"]) if r["deadline_date"] else None,
                "point_type": "deadline",
                "source_clause": r["extraction_method"],
            })

        # 2. Causes of action
        rows = await session.execute(sa_text("""
            SELECT DISTINCT ON (UPPER(TRIM(title)))
                id::text, title, count_number, status, ai_summary
            FROM causes_of_action
            WHERE TRIM(tenant_id) = :tid
              AND matter_id = CAST(:mid AS uuid)
              AND title NOT LIKE '{%%'
              AND title NOT LIKE 'of the %%'
              AND LOWER(TRIM(title)) != 'none'
              AND LENGTH(TRIM(title)) BETWEEN 3 AND 200
            ORDER BY UPPER(TRIM(title)), count_number
        """), {"tid": tid, "mid": matter_id})

        coa_raw = [dict(r._mapping) for r in rows.fetchall()]
        coa_points = []
        for c in coa_raw:
            coa_points.append({
                "id": c["id"],
                "point_key": f"cause_{c['count_number'] or 0}",
                "point_label": c["title"],
                "point_value": c["ai_summary"] or c["status"] or "",
                "point_type": "text",
                "source_clause": f"Count {c['count_number']}" if c["count_number"] else None,
            })

        # 3. Parties
        rows = await session.execute(sa_text("""
            SELECT party_name, COUNT(*) as freq, MAX(confidence) as max_conf
            FROM document_parties dp
            JOIN dms_documents doc ON dp.dms_document_id = doc.id
            WHERE TRIM(dp.tenant_id) = :tid
              AND doc.file_path LIKE :pattern
              AND dp.superseded_by_run_id IS NULL
              AND LENGTH(TRIM(dp.party_name)) > 2
              AND dp.party_name NOT IN ('parties','You','None','unknown')
              AND dp.party_name NOT LIKE '{%%'
              AND LENGTH(dp.party_name) <= 100
            GROUP BY party_name ORDER BY freq DESC LIMIT 25
        """), {"tid": tid, "pattern": path_pattern})

        key_contacts = []
        for r in rows.mappings():
            name = r["party_name"]
            role = "defendant" if any(k in name.upper() for k in ("LLC","INC","CORP","CO.","LP","LTD")) else "party"
            key_contacts.append({"contact_id": None, "full_name": name,
                "role": role, "company": None, "phone": None})

        # 4. Key documents
        rows = await session.execute(sa_text("""
            SELECT doc.id::text, doc.file_path
            FROM dms_documents doc
            WHERE TRIM(doc.tenant_id) = :tid
              AND doc.file_path LIKE :pattern
              AND (doc.file_path LIKE '%%/02-Pleading%%'
                   OR doc.file_path LIKE '%%/03-Discover%%')
              AND doc.content_text IS NOT NULL
            ORDER BY doc.indexed_at DESC NULLS LAST LIMIT 30
        """), {"tid": tid, "pattern": path_pattern})

        key_documents = []
        for r in rows.mappings():
            fp = r["file_path"] or ""
            fn = fp.rsplit("/", 1)[-1] if "/" in fp else fp
            fn_lower = fn.lower()
            role = "other"
            if "petition" in fn_lower or "complaint" in fn_lower: role = "complaint"
            elif "answer" in fn_lower: role = "answer"
            elif "counterclaim" in fn_lower: role = "counterclaim"
            elif "motion" in fn_lower: role = "motion"
            elif "order" in fn_lower: role = "order"
            elif "scheduling" in fn_lower: role = "scheduling_order"
            elif "response" in fn_lower or "reply" in fn_lower: role = "response"
            elif "disclosure" in fn_lower: role = "disclosure"
            elif "/03-" in fp.lower(): role = "discovery_request"
            key_documents.append({"id": r["id"], "document_id": r["id"],
                "document_role": role, "label": fn, "filename": fn, "storage_path": fp})

    all_deal_points = scheduling_deadlines + coa_points
    return JSONResponse({
        "ok": True, "matter_id": matter_id,
        "deal_points": all_deal_points,
        "key_documents": key_documents,
        "key_contacts": key_contacts,
        "subject": None,
        "causes_of_action": [{"id": c["id"], "title": c["title"],
            "count_number": c["count_number"], "status": c["status"]} for c in coa_raw],
        "scheduling_deadlines": scheduling_deadlines,
        "stats": {"total_deadlines": len(scheduling_deadlines),
            "total_coa": len(coa_raw), "total_parties": len(key_contacts),
            "total_key_docs": len(key_documents)},
    })
