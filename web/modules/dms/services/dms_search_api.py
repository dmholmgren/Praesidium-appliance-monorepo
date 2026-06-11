"""
modules/dms/services/dms_search_api.py — DMS Search JSON API

GET /api/v1/dms/search?q=...&matter_id=...&doc_type=...&page=1&per_page=25
  → { matters: [...], documents: [...], total, total_pages, page }

GET /api/v1/dms/search/facets?q=...
  → { doc_types: [...], matters: [...] }

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms/search", tags=["dms-search-api"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def _fmt(b):
    if not b: return "—"
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.1f} GB"


@router.get("")
async def dms_search(request: Request, q: str = "", matter_id: str = "",
                     doc_type: str = "", page: int = 1, per_page: int = 25):
    """Full-text search across DMS documents. Returns matching matters + documents."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "no tenant"}, 401)

    offset = (max(1, page) - 1) * per_page
    like = f"%{q}%" if q else "%"

    # -- Matching matters (always search matters by name/number)
    matters = []
    if q:
        try:
            async with AsyncSessionLocal() as s:
                rows = await s.execute(sa_text("""
                    SELECT m.id::text, m.matter_name, m.matter_number, m.status,
                           m.matter_type, c.client_name,
                           COUNT(d.id) AS doc_count
                    FROM matters m
                    LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                    LEFT JOIN documents d ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                    WHERE trim(m.tenant_id) = trim(:t)
                      AND (m.matter_name ILIKE :q OR m.matter_number ILIKE :q
                           OR c.client_name ILIKE :q OR m.cause_number ILIKE :q)
                    GROUP BY m.id, m.matter_name, m.matter_number, m.status, m.matter_type, c.client_name
                    ORDER BY m.status = 'active' DESC, m.matter_name
                    LIMIT 10
                """), {"t": tid, "q": like})
                for r in rows.mappings():
                    matters.append({
                        "id": r["id"], "matter_name": r["matter_name"],
                        "matter_number": r["matter_number"] or "",
                        "status": r["status"] or "", "matter_type": r["matter_type"] or "",
                        "client_name": r["client_name"] or "",
                        "doc_count": int(r["doc_count"] or 0),
                    })
        except Exception as e:
            logger.error("dms search matters: %s", e)

    # -- Matching documents
    docs = []
    total = 0
    try:
        async with AsyncSessionLocal() as s:
            # Build WHERE clauses
            wheres = ["trim(d.tenant_id) = trim(:t)"]
            params = {"t": tid, "lim": per_page, "off": offset}

            if q:
                wheres.append("(d.filename ILIKE :q OR d.original_filename ILIKE :q OR d.extracted_text ILIKE :q)")
                params["q"] = like
            if matter_id:
                wheres.append("d.matter_id = :mid::uuid")
                params["mid"] = matter_id
            if doc_type:
                wheres.append("d.document_type ILIKE :dt")
                params["dt"] = f"%{doc_type}%"

            where_sql = " AND ".join(wheres)

            # Count
            cr = await s.execute(sa_text(f"SELECT COUNT(*) FROM documents d WHERE {where_sql}"), params)
            total = cr.scalar() or 0

            # Results
            rows = await s.execute(sa_text(f"""
                SELECT d.id::text, d.filename, d.original_filename, d.document_type,
                       d.mime_type, d.file_size, d.storage_path, d.page_count,
                       d.created_at, d.updated_at, d.status,
                       d.matter_id::text,
                       CASE WHEN d.extracted_text IS NOT NULL AND d.extracted_text != ''
                            THEN SUBSTRING(d.extracted_text FROM 1 FOR 300)
                            ELSE NULL END AS snippet,
                       m.matter_name, m.matter_number, c.client_name
                FROM documents d
                LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                WHERE {where_sql}
                ORDER BY d.updated_at DESC NULLS LAST
                LIMIT :lim OFFSET :off
            """), params)

            for r in rows.mappings():
                fn = r["filename"] or r["original_filename"] or "Untitled"
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                docs.append({
                    "id": r["id"],
                    "filename": fn,
                    "document_type": r["document_type"] or ext or "",
                    "mime_type": r["mime_type"] or "",
                    "file_size": int(r["file_size"] or 0),
                    "file_size_fmt": _fmt(r["file_size"]),
                    "storage_path": r["storage_path"] or "",
                    "page_count": r["page_count"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                    "status": r["status"] or "",
                    "matter_id": r["matter_id"] or "",
                    "matter_name": r["matter_name"] or "",
                    "matter_number": r["matter_number"] or "",
                    "client_name": r["client_name"] or "",
                    "snippet": r["snippet"] or "",
                })
    except Exception as e:
        logger.error("dms search docs: %s", e)

    total_pages = max(1, (total + per_page - 1) // per_page)
    return JSONResponse({
        "matters": matters,
        "documents": docs,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "per_page": per_page,
        "query": q,
    })


@router.get("/facets")
async def dms_search_facets(request: Request, q: str = ""):
    """Return facet counts for document types and matters."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "no tenant"}, 401)

    doc_types = []
    matter_facets = []
    try:
        async with AsyncSessionLocal() as s:
            # Document type facets
            rows = await s.execute(sa_text("""
                SELECT COALESCE(document_type, 'unknown') AS dt, COUNT(*) AS cnt
                FROM documents WHERE trim(tenant_id) = trim(:t)
                  AND document_type IS NOT NULL AND document_type != ''
                GROUP BY document_type ORDER BY cnt DESC LIMIT 20
            """), {"t": tid})
            for r in rows.mappings():
                doc_types.append({"doc_type": r["dt"], "cnt": int(r["cnt"])})

            # Matter facets (top 30 by doc count)
            rows = await s.execute(sa_text("""
                SELECT m.id::text, m.matter_name, m.matter_number, c.client_name, COUNT(d.id) AS cnt
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                JOIN documents d ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                WHERE trim(m.tenant_id) = trim(:t)
                GROUP BY m.id, m.matter_name, m.matter_number, c.client_name
                ORDER BY cnt DESC LIMIT 30
            """), {"t": tid})
            for r in rows.mappings():
                matter_facets.append({
                    "id": r["id"], "matter_name": r["matter_name"] or "",
                    "matter_number": r["matter_number"] or "",
                    "client_name": r["client_name"] or "",
                    "cnt": int(r["cnt"]),
                })
    except Exception as e:
        logger.error("dms search facets: %s", e)

    return JSONResponse({"doc_types": doc_types, "matters": matter_facets})


@router.get("/doc/{doc_id}")
async def dms_doc_detail(request: Request, doc_id: str):
    """Get document detail for preview panel."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "no tenant"}, 401)

    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text("""
                SELECT d.id::text, d.filename, d.original_filename, d.document_type,
                       d.mime_type, d.file_size, d.storage_path, d.page_count,
                       d.created_at, d.updated_at, d.status, d.matter_id::text,
                       d.extracted_text,
                       m.matter_name, m.matter_number, c.client_name
                FROM documents d
                LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                WHERE d.id = :did::uuid AND trim(d.tenant_id) = trim(:t)
            """), {"did": doc_id, "t": tid})
            row = r.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "not found"}, 404)

            fn = row["filename"] or row["original_filename"] or ""
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            mime = row["mime_type"] or ""
            sp = row["storage_path"] or ""

            # Determine viewer mode
            if mime == "application/pdf" or ext == "pdf":
                viewer_mode = "pdf"
            elif mime.startswith("image/"):
                viewer_mode = "image"
            elif ext in ("doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf"):
                viewer_mode = "convert"
            elif mime.startswith("text/") or ext in ("txt", "csv", "log", "json", "xml", "html", "md"):
                viewer_mode = "text"
            else:
                viewer_mode = "unsupported"

            # Build preview URL using universal viewer
            preview_url = f"/api/v1/viewer/preview?path={sp}" if sp else None
            download_url = f"/api/v1/viewer/download?path={sp}" if sp else None

            return JSONResponse({
                "doc": {
                    "id": row["id"],
                    "filename": fn,
                    "document_type": row["document_type"] or ext,
                    "mime_type": mime,
                    "file_size": int(row["file_size"] or 0),
                    "file_size_fmt": _fmt(row["file_size"]),
                    "storage_path": sp,
                    "page_count": row["page_count"],
                    "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                    "status": row["status"] or "",
                    "matter_id": row["matter_id"] or "",
                    "matter_name": row["matter_name"] or "",
                    "matter_number": row["matter_number"] or "",
                    "client_name": row["client_name"] or "",
                },
                "viewer_mode": viewer_mode,
                "preview_url": preview_url,
                "download_url": download_url,
                "has_text": bool(row["extracted_text"]),
                "text_snippet": (row["extracted_text"] or "")[:500],
            })
    except Exception as e:
        logger.error("dms doc detail: %s", e)
        return JSONResponse({"error": str(e)}, 500)
