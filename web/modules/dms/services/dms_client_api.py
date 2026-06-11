"""
DMS Client Page API
GET /api/v1/dms/client/{client_id} — client matters with doc counts + recent docs
GET /api/v1/dms/clients — client list for sidebar navigation
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms", tags=["dms-client"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


@router.get("/clients")
async def dms_client_list(request: Request):
    """Client list with matter counts and doc counts for DMS sidebar."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT c.id::text, c.client_name,
                       COUNT(DISTINCT m.id) AS matter_count,
                       COALESCE(SUM(doc.cnt), 0) AS doc_count
                FROM clients c
                JOIN matters m ON m.client_id = c.id
                    AND TRIM(m.tenant_id) = TRIM(c.tenant_id)
                    AND m.status = 'active'
                LEFT JOIN (
                    SELECT matter_id, COUNT(*) AS cnt FROM documents
                    WHERE TRIM(tenant_id) = :tid GROUP BY matter_id
                ) doc ON doc.matter_id = m.id
                WHERE TRIM(c.tenant_id) = :tid
                GROUP BY c.id, c.client_name
                HAVING COUNT(DISTINCT m.id) > 0
                ORDER BY c.client_name
            """), {"tid": tid})
            clients = []
            for row in r.mappings():
                clients.append({
                    "id": row["id"],
                    "client_name": row["client_name"],
                    "matter_count": int(row["matter_count"]),
                    "doc_count": int(row["doc_count"]),
                })
        return JSONResponse({"clients": clients})
    except Exception as exc:
        logger.error("dms_client_list: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/client/{client_id}")
async def dms_client_detail(request: Request, client_id: str):
    """Client detail with matters, doc counts, and recent documents."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            # Client info
            cr = await session.execute(sa_text("""
                SELECT id::text, client_name, client_type, phone, email
                FROM clients WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"cid": client_id, "tid": tid})
            client = cr.mappings().fetchone()
            if not client:
                return JSONResponse({"error": "Client not found"}, status_code=404)

            # Matters with doc counts
            mr = await session.execute(sa_text("""
                SELECT m.id::text, m.matter_name, m.matter_number, m.matter_type,
                       m.status, m.practice_area,
                       COALESCE(nd.cnt, 0) AS native_docs,
                       COALESCE(ld.cnt, 0) AS legacy_docs
                FROM matters m
                LEFT JOIN (
                    SELECT matter_id, COUNT(*) AS cnt FROM documents
                    WHERE TRIM(tenant_id) = :tid GROUP BY matter_id
                ) nd ON nd.matter_id = m.id
                LEFT JOIN (
                    SELECT mf.matter_id, COUNT(dd.id) AS cnt
                    FROM dms_folder_matches mf
                    JOIN dms_documents dd ON dd.file_path LIKE mf.folder_path || '%%'
                        AND TRIM(dd.tenant_id) = :tid
                    WHERE TRIM(mf.tenant_id) = :tid
                    GROUP BY mf.matter_id
                ) ld ON ld.matter_id = m.id
                WHERE m.client_id = CAST(:cid AS uuid) AND TRIM(m.tenant_id) = :tid
                ORDER BY
                    CASE m.status WHEN 'active' THEN 0 ELSE 1 END,
                    m.matter_name
            """), {"cid": client_id, "tid": tid})
            matters = []
            for row in mr.mappings():
                matters.append({
                    "id": row["id"],
                    "matter_name": row["matter_name"],
                    "matter_number": row["matter_number"] or "",
                    "matter_type": row["matter_type"] or "",
                    "status": row["status"],
                    "practice_area": row["practice_area"] or "",
                    "native_docs": int(row["native_docs"]),
                    "legacy_docs": int(row["legacy_docs"]),
                    "total_docs": int(row["native_docs"]) + int(row["legacy_docs"]),
                })

            # Recent documents across all client matters
            rd = await session.execute(sa_text("""
                SELECT d.id::text, d.filename, d.document_type, d.file_size,
                       d.updated_at, d.storage_path, m.matter_name, m.id::text AS matter_id
                FROM documents d
                JOIN matters m ON d.matter_id = m.id AND TRIM(m.tenant_id) = :tid
                WHERE m.client_id = CAST(:cid AS uuid) AND TRIM(d.tenant_id) = :tid
                ORDER BY d.updated_at DESC
                LIMIT 15
            """), {"cid": client_id, "tid": tid})
            recent_docs = []
            for row in rd.mappings():
                recent_docs.append({
                    "id": row["id"],
                    "filename": row["filename"] or "",
                    "document_type": row["document_type"] or "",
                    "file_size": int(row["file_size"]) if row["file_size"] else 0,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "matter_name": row["matter_name"] or "",
                    "matter_id": row["matter_id"],
                })

        return JSONResponse({
            "client": dict(client),
            "matters": matters,
            "recent_docs": recent_docs,
            "total_matters": len(matters),
            "total_docs": sum(m["total_docs"] for m in matters),
        })
    except Exception as exc:
        logger.error("dms_client_detail: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)
