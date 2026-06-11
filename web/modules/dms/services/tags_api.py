"""
modules/dms/services/tags_api.py
API endpoints for matter-level tag management.
Serves both eDiscovery review and DMS folder tree.
"""
import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/tags", tags=["tags-api"])


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _serialize(obj):
    import uuid
    from datetime import datetime, date
    from decimal import Decimal
    if obj is None: return None
    if isinstance(obj, dict): return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_serialize(v) for v in obj]
    if isinstance(obj, uuid.UUID): return str(obj)
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    if isinstance(obj, bytes): return obj.decode("utf-8", errors="replace")
    return obj


@router.get("/matter/{matter_id}")
async def get_matter_tags(request: Request, matter_id: str, user=Depends(get_current_user)):
    """All tags for a matter, grouped by tag_set.
    Returns issue tags (collection-scoped + matter-scoped) and project tags.
    """
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            # Issue tags: those with tag_set='issue' and either:
            #   - matter_id matches directly, OR
            #   - collection_id belongs to a collection on this matter
            r = await session.execute(sa_text("""
                SELECT t.id::text, t.name, t.category, t.color, t.description,
                       t.is_system, t.tag_set, t.collection_id::text, t.matter_id::text,
                       t.project_id::text,
                       COUNT(dt.id) as doc_count
                FROM tags t
                LEFT JOIN document_tags dt ON dt.tag_id = t.id
                    AND trim(dt.tenant_id::text) = trim(:tid)
                WHERE trim(t.tenant_id::text) = trim(:tid)
                  AND (
                      t.matter_id = CAST(:mid AS uuid)
                      OR t.collection_id IN (
                          SELECT id FROM ediscovery_collections
                          WHERE matter_id = CAST(:mid AS uuid)
                            AND trim(tenant_id::text) = trim(:tid)
                      )
                  )
                GROUP BY t.id, t.name, t.category, t.color, t.description,
                         t.is_system, t.tag_set, t.collection_id, t.matter_id, t.project_id
                ORDER BY t.tag_set, CASE WHEN t.is_system THEN 0 ELSE 1 END, t.name
            """), {"tid": tid, "mid": matter_id})
            rows = [dict(r) for r in r.mappings().fetchall()]

        issue_tags = [t for t in rows if t.get("tag_set") == "issue"]
        project_tags = [t for t in rows if t.get("tag_set") == "project"]

        return JSONResponse(_serialize({
            "issue_tags": issue_tags,
            "project_tags": project_tags,
        }))
    except Exception as e:
        logger.error("get_matter_tags: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/matter/{matter_id}/create")
async def create_matter_tag(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Create a new matter-level tag.
    Body: {name, tag_set: 'issue'|'project', color?, category?, description?}
    """
    tid = _tenant(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    tag_set = body.get("tag_set", "project")
    color = body.get("color", "#6366F1" if tag_set == "project" else "#2563EB")
    category = body.get("category", "custom")
    description = body.get("description", "")

    if not name:
        return JSONResponse({"error": "Name required"}, 400)
    if tag_set not in ("issue", "project"):
        return JSONResponse({"error": "tag_set must be 'issue' or 'project'"}, 400)

    try:
        async with AsyncSessionLocal() as session:
            # Check for existing tag with same name on this matter
            r = await session.execute(sa_text("""
                SELECT id::text FROM tags
                WHERE trim(tenant_id::text) = trim(:tid)
                  AND matter_id = CAST(:mid AS uuid)
                  AND name = :name AND tag_set = :ts
                LIMIT 1
            """), {"tid": tid, "mid": matter_id, "name": name, "ts": tag_set})
            existing = r.fetchone()
            if existing:
                return JSONResponse({"ok": True, "tag_id": existing[0], "existed": True})

            r_new = await session.execute(sa_text("""
                INSERT INTO tags (tenant_id, matter_id, name, tag_set, category, color,
                                  description, is_system, created_by, created_at)
                VALUES (:tid, CAST(:mid AS uuid), :name, :ts, :cat, :color,
                        :desc, false, NULL, now())
                RETURNING id::text
            """), {"tid": tid, "mid": matter_id, "name": name, "ts": tag_set,
                   "cat": category, "color": color, "desc": description})
            tag_id = r_new.scalar()
            await session.commit()

        return JSONResponse({"ok": True, "tag_id": tag_id, "existed": False})
    except Exception as e:
        logger.error("create_matter_tag: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/apply")
async def apply_tag(request: Request, user=Depends(get_current_user)):
    """Apply a tag to a document from any module.
    Body: {tag_id, document_id, source_table: 'ediscovery'|'dms', matter_id?}
    """
    tid = _tenant(request)
    body = await request.json()
    tag_id = body.get("tag_id")
    doc_id = body.get("document_id")
    source_table = body.get("source_table", "ediscovery")
    matter_id = body.get("matter_id")

    if not tag_id or not doc_id:
        return JSONResponse({"error": "tag_id and document_id required"}, 400)

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT 1 FROM document_tags
                WHERE document_id = CAST(:did AS uuid)
                  AND tag_id = CAST(:tid2 AS uuid)
                  AND trim(tenant_id::text) = trim(:tenant)
                  AND source = 'manual'
                LIMIT 1
            """), {"did": doc_id, "tid2": tag_id, "tenant": tid})
            if not r.fetchone():
                await session.execute(sa_text("""
                    INSERT INTO document_tags
                        (tenant_id, document_id, tag_id, source, source_table,
                         matter_id, applied_by, applied_at)
                    VALUES (:tenant, CAST(:did AS uuid), CAST(:tid2 AS uuid),
                            'manual', :st,
                            CAST(:mid AS uuid), NULL, now())
                """), {"tenant": tid, "did": doc_id, "tid2": tag_id,
                       "st": source_table, "mid": matter_id})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("apply_tag: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/remove")
async def remove_tag(request: Request, user=Depends(get_current_user)):
    """Remove a tag from a document.
    Body: {tag_id, document_id}
    """
    tid = _tenant(request)
    body = await request.json()
    tag_id = body.get("tag_id")
    doc_id = body.get("document_id")

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                DELETE FROM document_tags
                WHERE document_id = CAST(:did AS uuid)
                  AND tag_id = CAST(:tid2 AS uuid)
                  AND trim(tenant_id::text) = trim(:tenant)
            """), {"did": doc_id, "tid2": tag_id, "tenant": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.get("/matter/{matter_id}/project-tag-docs/{tag_id}")
async def get_project_tag_documents(request: Request, matter_id: str, tag_id: str,
                                     user=Depends(get_current_user)):
    """Get all documents tagged with a specific project tag across all modules.
    Used by DMS folder tree virtual folders and project document panels.
    """
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            # eDiscovery documents tagged with this tag
            r_ed = await session.execute(sa_text("""
                SELECT dt.document_id::text, dt.source_table,
                       ed.file_name, ed.email_subject, ed.mime_type,
                       ed.file_size, ed.doc_date, ed.bates_begin,
                       ed.review_status, ed.collection_id::text,
                       'ediscovery' as doc_source
                FROM document_tags dt
                JOIN ediscovery_documents ed ON ed.id = dt.document_id
                WHERE dt.tag_id = CAST(:tid2 AS uuid)
                  AND trim(dt.tenant_id::text) = trim(:tenant)
                  AND dt.source_table = 'ediscovery'
                ORDER BY COALESCE(ed.doc_date, ed.ingested_at::date) DESC
            """), {"tid2": tag_id, "tenant": tid})
            ed_docs = [dict(r) for r in r_ed.mappings().fetchall()]

            # DMS documents tagged with this tag
            r_dms = await session.execute(sa_text("""
                SELECT dt.document_id::text, dt.source_table,
                       d.filename as file_name, d.mime_type,
                       d.file_size, d.created_at as doc_date,
                       d.storage_path,
                       'dms' as doc_source
                FROM document_tags dt
                JOIN dms_documents d ON d.id = dt.document_id
                WHERE dt.tag_id = CAST(:tid2 AS uuid)
                  AND trim(dt.tenant_id::text) = trim(:tenant)
                  AND dt.source_table = 'dms'
                ORDER BY d.created_at DESC
            """), {"tid2": tag_id, "tenant": tid})
            dms_docs = [dict(r) for r in r_dms.mappings().fetchall()]

        all_docs = ed_docs + dms_docs
        for d in all_docs:
            d["display_name"] = (d.get("file_name") or d.get("email_subject") or "(untitled)").replace("\\", "/").rsplit("/", 1)[-1]

        return JSONResponse(_serialize({
            "documents": all_docs,
            "total": len(all_docs),
            "ediscovery_count": len(ed_docs),
            "dms_count": len(dms_docs),
        }))
    except Exception as e:
        logger.error("get_project_tag_documents: %s", e)
        return JSONResponse({"error": str(e)}, 500)
