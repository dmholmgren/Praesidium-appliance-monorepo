import os
"""
modules/ediscovery/routes/review_api.py
JSON API endpoints for the React eDiscovery Review page.
Wraps the existing review_widget_service functions.
"""
import logging
from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery/review", tags=["ediscovery-review-api"])


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _fmt_size(n) -> str:
    if n is None: return "—"
    try: n = float(n)
    except: return "—"
    for u in ("B","KB","MB","GB"):
        if n < 1024: return f"{n:.1f} {u}" if u != "B" else f"{int(n)} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_date(val) -> str:
    from datetime import datetime, date
    if isinstance(val, (datetime, date)): return val.strftime("%Y-%m-%d")
    return str(val or "")[:10]


def _fmt_dt(val) -> str:
    from datetime import datetime, date
    if isinstance(val, (datetime, date)): return val.strftime("%b %d, %Y %I:%M %p")
    return str(val or "")[:16]


def _serialize(obj):
    """Make DB row dicts JSON-safe."""
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


@router.get("/collection/{collection_id}")
async def review_collection_info(request: Request, collection_id: str, user=Depends(get_current_user)):
    """Collection header info + seed default tags."""
    tid = _tenant(request)
    from modules.ediscovery.services.review_widget_service import seed_default_tags_for_collection
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT c.id::text, c.name, c.collection_name, c.matter_id::text,
                       c.total_docs,
                       COALESCE((SELECT COUNT(*) FROM ediscovery_documents ed
                                 WHERE ed.collection_id = c.id
                                   AND COALESCE(ed.review_status,'unreviewed') != 'unreviewed'), 0) AS reviewed_docs,
                       c.status, c.source_party,
                       m.matter_name, m.matter_number
                FROM ediscovery_collections c
                LEFT JOIN matters m ON m.id = c.matter_id
                WHERE c.id = CAST(:cid AS uuid) AND trim(c.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"cid": collection_id, "tid": tid})
            row = r.mappings().fetchone()
        if not row:
            return JSONResponse({"error": "Collection not found"}, 404)
        coll = dict(row)
        coll["display_name"] = coll.get("name") or coll.get("collection_name") or "Collection"
        try:
            uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
            await seed_default_tags_for_collection(tid, collection_id, uid)
        except: pass
        return JSONResponse(_serialize(coll))
    except Exception as e:
        logger.error("review_collection_info: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/collection/{collection_id}/docs")
async def review_doc_list(
    request: Request, collection_id: str,
    page: int = Query(1), per_page: int = Query(50),
    status_filter: str = Query("all"), privilege_filter: str = Query("all"),
    q: str = Query(""),
    hide_dupes: str = Query(""),
    user=Depends(get_current_user),
):
    """Paginated doc list for the left panel."""
    tid = _tenant(request)
    offset = (max(1, page) - 1) * min(200, max(1, per_page))
    per_page = min(200, max(1, per_page))

    where = ["trim(ed.tenant_id::text) = trim(:tid)", "ed.collection_id = CAST(:cid AS uuid)"]
    params = {"tid": tid, "cid": collection_id, "limit": per_page, "offset": offset}

    if status_filter == "unreviewed":
        where.append("COALESCE(ed.review_status, 'unreviewed') = 'unreviewed'")
    elif status_filter == "reviewed":
        where.append("COALESCE(ed.review_status, 'unreviewed') <> 'unreviewed'")

    if privilege_filter == "privileged":
        where.append("ed.privilege_status = 'privileged'")
    elif privilege_filter == "non_privileged":
        where.append("(ed.privilege_status IS NULL OR ed.privilege_status <> 'privileged')")

    if hide_dupes == "1":
        where.append("(ed.is_duplicate IS NOT TRUE)")

    if q:
        where.append("(LOWER(COALESCE(ed.file_name,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.email_subject,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.custodian,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.email_from,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.email_to,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.email_cc,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(COALESCE(ed.bates_begin,'')) LIKE '%%'||LOWER(:q)||'%%' OR LOWER(LEFT(COALESCE(ed.extracted_text,''),5000)) LIKE '%%'||LOWER(:q)||'%%')")
        params["q"] = q

    wsql = " AND ".join(where)

    try:
        async with AsyncSessionLocal() as session:
            r_total = await session.execute(sa_text(f"SELECT COUNT(*) FROM ediscovery_documents ed WHERE {wsql}"), params)
            total = int(r_total.scalar() or 0)

            r = await session.execute(sa_text(f"""
                SELECT ed.id::text, ed.file_name, ed.file_size, ed.mime_type,
                       ed.doc_date, ed.custodian, ed.email_from, ed.email_subject,
                       ed.page_count, ed.bates_begin, ed.bates_end,
                       ed.review_status, ed.privilege_status, ed.is_duplicate,
                       ed.relevance_score, ed.doc_type
                FROM ediscovery_documents ed
                WHERE {wsql}
                ORDER BY COALESCE(ed.doc_date, ed.ingested_at::date) DESC NULLS LAST, ed.file_name ASC
                LIMIT :limit OFFSET :offset
            """), params)
            rows = [dict(row) for row in r.mappings().fetchall()]

        docs = []
        for rec in rows:
            name = rec.get("file_name") or rec.get("email_subject") or "(untitled)"
            rec["display_name"] = name.replace("\\", "/").rsplit("/", 1)[-1]
            rec["display_size"] = _fmt_size(rec.get("file_size"))
            rec["display_date"] = _fmt_date(rec.get("doc_date"))
            rec["status_display"] = (rec.get("review_status") or "unreviewed").replace("_", " ")
            docs.append(rec)

        total_pages = max(1, (total + per_page - 1) // per_page)
        return JSONResponse(_serialize({
            "docs": docs, "total": total, "page": page, "per_page": per_page,
            "total_pages": total_pages, "has_prev": page > 1, "has_next": page < total_pages,
        }))
    except Exception as e:
        logger.error("review_doc_list: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/doc/{doc_id}")
async def review_doc_detail(request: Request, doc_id: str, user=Depends(get_current_user)):
    """Full doc detail — viewer info, metadata, tags, review status, family."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            # Main doc
            r = await session.execute(sa_text("""
                SELECT ed.id::text, ed.file_name, ed.mime_type, ed.file_size,
                       ed.page_count, ed.working_path, ed.file_path, ed.native_path,
                       ed.text_path, ed.bates_begin, ed.bates_end, ed.collection_id::text,
                       ed.doc_type, ed.extracted_text IS NOT NULL AS has_text,
                       ed.file_hash, ed.doc_hash, ed.doc_date, ed.ingested_at,
                       ed.custodian, ed.original_path, ed.produced_in,
                       ed.email_from, ed.email_to, ed.email_cc, ed.email_subject,
                       ed.email_date, ed.email_message_id, ed.email_thread_id,
                       ed.is_duplicate, ed.is_near_duplicate, ed.near_dupe_score,
                       ed.relevance_score, ed.review_tier,
                       ed.review_status, ed.privilege_status,
                       ed.reviewed_at, ed.reviewed_by, ed.coding_notes
                FROM ediscovery_documents ed
                WHERE ed.id = CAST(:did AS uuid) AND trim(ed.tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tid})
            row = r.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "Document not found"}, 404)
            doc = dict(row)

            # Tags — grouped by tag_set (issue vs project)
            collection_id = doc["collection_id"]

            # Get matter_id for this collection
            r_matter = await session.execute(sa_text("""
                SELECT matter_id::text FROM ediscovery_collections
                WHERE id = CAST(:cid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"cid": collection_id, "tid": tid})
            matter_row = r_matter.fetchone()
            matter_id = matter_row[0] if matter_row else None

            # Available tags: collection-scoped issue tags + matter-scoped project tags
            r_avail = await session.execute(sa_text("""
                SELECT t.id::text, t.name, t.category, t.color, t.description,
                       t.is_system, COALESCE(t.tag_set, 'issue') as tag_set
                FROM tags t
                WHERE trim(t.tenant_id::text) = trim(:tid)
                  AND (
                      (t.collection_id IS NULL OR t.collection_id = CAST(:cid AS uuid))
                      OR (t.matter_id = CAST(:mid AS uuid) AND t.tag_set = 'project')
                  )
                ORDER BY t.tag_set,
                         CASE WHEN t.is_system THEN 0 ELSE 1 END,
                         t.name
            """), {"tid": tid, "cid": collection_id, "mid": matter_id or "00000000-0000-0000-0000-000000000000"})
            avail_tags = [dict(r) for r in r_avail.mappings().fetchall()]

            r_app = await session.execute(sa_text("""
                SELECT dt.tag_id::text, t.name, t.category, t.color,
                       COALESCE(t.tag_set, 'issue') as tag_set
                FROM document_tags dt JOIN tags t ON t.id = dt.tag_id
                WHERE dt.document_id = CAST(:did AS uuid) AND trim(dt.tenant_id::text) = trim(:tid)
            """), {"did": doc_id, "tid": tid})
            applied_tags = [dict(r) for r in r_app.mappings().fetchall()]
            applied_ids = {t["tag_id"] for t in applied_tags}

            for t in avail_tags:
                t["is_applied"] = t["id"] in applied_ids

            # Family — thread siblings + exact dupes
            thread_members = []
            if doc.get("email_thread_id"):
                r_t = await session.execute(sa_text("""
                    SELECT id::text, file_name, email_subject, email_from, email_date, doc_date, review_status
                    FROM ediscovery_documents
                    WHERE email_thread_id = :thread AND id <> CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                    ORDER BY COALESCE(email_date, doc_date) ASC LIMIT 50
                """), {"thread": doc["email_thread_id"], "did": doc_id, "tid": tid})
                thread_members = [dict(r) for r in r_t.mappings().fetchall()]

            duplicates = []
            if doc.get("file_hash"):
                r_d = await session.execute(sa_text("""
                    SELECT id::text, file_name, custodian, doc_date, bates_begin
                    FROM ediscovery_documents
                    WHERE file_hash = :fhash AND id <> CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                    LIMIT 20
                """), {"fhash": doc["file_hash"], "did": doc_id, "tid": tid})
                duplicates = [dict(r) for r in r_d.mappings().fetchall()]

        # Viewer mode
        from modules.ediscovery.services.review_widget_service import _viewer_mode
        native_ext = (doc.get("native_path") or "").rsplit(".", 1)[-1].lower() if doc.get("native_path") else ""
        AUDIO_EXTS = {"mp3","m4a","m4r","wav","ogg","aac","flac","wma"}
        mode = "audio" if native_ext in AUDIO_EXTS else _viewer_mode(doc.get("mime_type"), doc.get("file_name"), doc.get("doc_type"), bool(doc.get("has_text")))

        doc["display_name"] = (doc.get("file_name") or "(untitled)").replace("\\", "/").rsplit("/", 1)[-1]
        doc["display_size"] = _fmt_size(doc.get("file_size"))
        doc["display_date"] = _fmt_date(doc.get("doc_date"))

        return JSONResponse(_serialize({
            "doc": doc,
            "viewer_mode": mode,
            "file_url": f"/ediscovery/documents/{doc_id}/file",
            "text_url": f"/ediscovery/documents/{doc_id}/text",
            "native_url": f"/ediscovery/documents/{doc_id}/native" if doc.get("native_path") else None,
            "available_tags": avail_tags,
            "applied_tags": applied_tags,
            "thread_members": thread_members,
            "duplicates": duplicates,
            "review_statuses": [
                {"value":"unreviewed","label":"Unreviewed","color":"gray"},
                {"value":"responsive","label":"Responsive","color":"green"},
                {"value":"non_responsive","label":"Non-Responsive","color":"gray"},
                {"value":"needs_further_review","label":"Needs Further Review","color":"yellow"},
                {"value":"privileged","label":"Privileged","color":"red"},
                {"value":"hot","label":"Hot","color":"red"},
            ],
            "privilege_statuses": [
                {"value":"none","label":"Not Privileged","color":"gray"},
                {"value":"privileged","label":"Privileged","color":"red"},
                {"value":"attorney_work_product","label":"Attorney Work Product","color":"red"},
                {"value":"redact","label":"Needs Redaction","color":"yellow"},
            ],
        }))
    except Exception as e:
        logger.error("review_doc_detail: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/doc/{doc_id}/tag")
async def review_apply_tag(request: Request, doc_id: str, user=Depends(get_current_user)):
    """Apply or remove a tag. Body: {tag_id, action: 'apply'|'remove'}"""
    tid = _tenant(request)
    body = await request.json()
    tag_id = body.get("tag_id")
    action = body.get("action", "apply")
    try:
        async with AsyncSessionLocal() as session:
            if action == "remove":
                await session.execute(sa_text("""
                    DELETE FROM document_tags WHERE document_id = CAST(:did AS uuid)
                    AND tag_id = CAST(:tid2 AS uuid) AND trim(tenant_id::text) = trim(:tenant)
                """), {"did": doc_id, "tid2": tag_id, "tenant": tid})
            else:
                r = await session.execute(sa_text("""
                    SELECT 1 FROM document_tags WHERE document_id = CAST(:did AS uuid)
                    AND tag_id = CAST(:tid2 AS uuid) AND trim(tenant_id::text) = trim(:tenant) LIMIT 1
                """), {"did": doc_id, "tid2": tag_id, "tenant": tid})
                if not r.fetchone():
                    # Look up tag to get matter_id for routing
                    r_tag = await session.execute(sa_text("""
                        SELECT matter_id::text FROM tags WHERE id = CAST(:tid2 AS uuid) LIMIT 1
                    """), {"tid2": tag_id})
                    tag_row = r_tag.fetchone()
                    tag_matter_id = tag_row[0] if tag_row else None
                    await session.execute(sa_text("""
                        INSERT INTO document_tags (tenant_id, document_id, tag_id, source,
                                                    source_table, matter_id, applied_by, applied_at)
                        VALUES (:tenant, CAST(:did AS uuid), CAST(:tid2 AS uuid), 'manual',
                                'ediscovery', CAST(:mid AS uuid), NULL, now())
                    """), {"tenant": tid, "did": doc_id, "tid2": tag_id, "mid": tag_matter_id})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.post("/doc/{doc_id}/status")
async def review_update_status(request: Request, doc_id: str, user=Depends(get_current_user)):
    """Update review_status and/or privilege_status.
    Body: {review_status?, privilege_status?}
    When privilege_status is set (not 'none'), automatically:
      1. Mark as reviewed (review_status -> 'responsive' if currently unreviewed)
      2. Set reviewed_at
      3. Auto-apply the matching privilege tag (e.g. 'Privileged')
    """
    tid = _tenant(request)
    body = await request.json()
    priv_val = body.get("privilege_status")
    is_privilege_set = priv_val and priv_val != "none"
    updates = []
    params = {"did": doc_id, "tenant": tid}

    if "review_status" in body:
        updates.append("review_status = :rs")
        params["rs"] = body["review_status"]
        if body["review_status"] != "unreviewed":
            updates.append("reviewed_at = now()")
        else:
            updates.append("reviewed_at = NULL")

    if "privilege_status" in body:
        if priv_val == "none":
            updates.append("privilege_status = NULL")
        else:
            updates.append("privilege_status = :ps")
            params["ps"] = priv_val

    # Auto-mark as reviewed when privilege is set
    if is_privilege_set and "review_status" not in body:
        # Only override if currently unreviewed
        updates.append("review_status = 'privileged'")
        updates.append("reviewed_at = COALESCE(reviewed_at, now())")

    if not updates:
        return JSONResponse({"error": "No fields to update"}, 400)
    updates.append("updated_at = now()")

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE ediscovery_documents SET {', '.join(updates)}
                WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tenant)
            """), params)
            await session.commit()

            # Auto-apply privilege tag when privilege status is set
            if is_privilege_set:
                tag_name = {
                    "privileged": "Privileged",
                    "attorney_work_product": "Attorney Work Product",
                    "redact": "Needs Redaction",
                }.get(priv_val, "Privileged")

                # Find collection_id for this doc
                r_coll = await session.execute(sa_text("""
                    SELECT collection_id::text FROM ediscovery_documents
                    WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tenant)
                """), {"did": doc_id, "tenant": tid})
                coll_row = r_coll.fetchone()
                collection_id = coll_row[0] if coll_row else None

                if collection_id:
                    # Find or create the privilege tag
                    r_tag = await session.execute(sa_text("""
                        SELECT id::text FROM tags
                        WHERE trim(tenant_id::text) = trim(:tid)
                          AND name = :tag_name
                          AND (collection_id IS NULL OR collection_id = CAST(:cid AS uuid))
                        ORDER BY CASE WHEN collection_id = CAST(:cid AS uuid) THEN 0 ELSE 1 END
                        LIMIT 1
                    """), {"tid": tid, "tag_name": tag_name, "cid": collection_id})
                    tag_row = r_tag.fetchone()

                    if tag_row:
                        tag_id = tag_row[0]
                    else:
                        # Create the tag
                        r_new = await session.execute(sa_text("""
                            INSERT INTO tags (tenant_id, collection_id, name, tag_set,
                                              category, color, is_system, created_at)
                            VALUES (:tid, CAST(:cid AS uuid), :tag_name, 'ediscovery',
                                    'privilege', '#EF4444', true, now())
                            RETURNING id::text
                        """), {"tid": tid, "cid": collection_id, "tag_name": tag_name})
                        tag_id = r_new.scalar()
                        await session.commit()

                    # Apply tag if not already applied
                    r_exists = await session.execute(sa_text("""
                        SELECT 1 FROM document_tags
                        WHERE document_id = CAST(:did AS uuid)
                          AND tag_id = CAST(:tag_id AS uuid)
                          AND trim(tenant_id::text) = trim(:tenant)
                        LIMIT 1
                    """), {"did": doc_id, "tag_id": tag_id, "tenant": tid})
                    if not r_exists.fetchone():
                        await session.execute(sa_text("""
                            INSERT INTO document_tags
                                (tenant_id, document_id, tag_id, source,
                                 source_table, applied_by, applied_at)
                            VALUES (:tenant, CAST(:did AS uuid), CAST(:tag_id AS uuid),
                                    'auto_privilege', 'ediscovery', NULL, now())
                        """), {"tenant": tid, "did": doc_id, "tag_id": tag_id})
                        await session.commit()

            # When privilege is cleared back to 'none', remove auto-applied privilege tags
            if priv_val == "none":
                r_coll2 = await session.execute(sa_text("""
                    SELECT collection_id::text FROM ediscovery_documents
                    WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tenant)
                """), {"did": doc_id, "tenant": tid})
                coll_row2 = r_coll2.fetchone()
                if coll_row2:
                    await session.execute(sa_text("""
                        DELETE FROM document_tags
                        WHERE document_id = CAST(:did AS uuid)
                          AND trim(tenant_id::text) = trim(:tenant)
                          AND source = 'auto_privilege'
                    """), {"did": doc_id, "tenant": tid})
                    await session.commit()

        return JSONResponse({"ok": True, "auto_reviewed": is_privilege_set})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.post("/collection/{collection_id}/create-tag")
async def review_create_tag(request: Request, collection_id: str, user=Depends(get_current_user)):
    """Create a new tag. Body: {name, apply_to_doc_id?}"""
    tid = _tenant(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    tag_set = body.get("tag_set", "issue")
    apply_to = body.get("apply_to_doc_id")
    if not name:
        return JSONResponse({"error": "Name required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            if tag_set == "project":
                # Project tags are matter-scoped, not collection-scoped
                r_matter = await session.execute(sa_text("""
                    SELECT matter_id::text FROM ediscovery_collections
                    WHERE id = CAST(:cid AS uuid) AND trim(tenant_id::text) = trim(:tid) LIMIT 1
                """), {"cid": collection_id, "tid": tid})
                matter_row = r_matter.fetchone()
                mid = matter_row[0] if matter_row else None
                r = await session.execute(sa_text("""
                    SELECT id::text FROM tags WHERE trim(tenant_id::text) = trim(:tid)
                    AND matter_id = CAST(:mid AS uuid) AND name = :name AND tag_set = 'project' LIMIT 1
                """), {"tid": tid, "mid": mid, "name": name})
                row = r.fetchone()
                if row:
                    tag_id = row[0]
                else:
                    r_new = await session.execute(sa_text("""
                        INSERT INTO tags (tenant_id, matter_id, name, tag_set, category, color,
                                          is_system, created_by, created_at)
                        VALUES (:tid, CAST(:mid AS uuid), :name, 'project', 'custom', '#6366F1',
                                false, NULL, now()) RETURNING id::text
                    """), {"tid": tid, "mid": mid, "name": name})
                    tag_id = r_new.scalar()
            else:
                r = await session.execute(sa_text("""
                    SELECT id::text FROM tags WHERE trim(tenant_id::text) = trim(:tid)
                    AND collection_id = CAST(:cid AS uuid) AND name = :name LIMIT 1
                """), {"tid": tid, "cid": collection_id, "name": name})
                row = r.fetchone()
                if row:
                    tag_id = row[0]
                else:
                    r_new = await session.execute(sa_text("""
                        INSERT INTO tags (tenant_id, collection_id, name, tag_set, category, color,
                                          is_system, created_by, created_at)
                        VALUES (:tid, CAST(:cid AS uuid), :name, 'issue', 'custom', '#2563EB',
                                false, NULL, now()) RETURNING id::text
                    """), {"tid": tid, "cid": collection_id, "name": name})
                    tag_id = r_new.scalar()
            if apply_to:
                r_ex = await session.execute(sa_text("""
                    SELECT 1 FROM document_tags WHERE document_id = CAST(:did AS uuid)
                    AND tag_id = CAST(:tid2 AS uuid) AND trim(tenant_id::text) = trim(:tenant) LIMIT 1
                """), {"did": apply_to, "tid2": tag_id, "tenant": tid})
                if not r_ex.fetchone():
                    await session.execute(sa_text("""
                        INSERT INTO document_tags (tenant_id, document_id, tag_id, source, applied_by, applied_at)
                        VALUES (:tenant, CAST(:did AS uuid), CAST(:tid2 AS uuid), 'manual', NULL, now())
                    """), {"tenant": tid, "did": apply_to, "tid2": tag_id})
            await session.commit()
        return JSONResponse({"ok": True, "tag_id": tag_id, "tag_set": tag_set})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)



@router.get("/doc/{doc_id}/attachments")
async def review_doc_attachments(request: Request, doc_id: str, user=Depends(get_current_user)):
    """Get child/attachment documents for an email."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            # First check if this doc has a family_id or if other docs reference it as parent
            r = await session.execute(sa_text("""
                SELECT family_id::text, file_hash FROM ediscovery_documents
                WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tid})
            parent = r.mappings().fetchone()
            if not parent:
                return JSONResponse({"attachments": []})

            # Try parent_id first
            r2 = await session.execute(sa_text("""
                SELECT id::text, file_name, mime_type, doc_type, file_size,
                       attachment_index, is_attachment
                FROM ediscovery_documents
                WHERE parent_id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                ORDER BY attachment_index NULLS LAST, file_name
            """), {"did": doc_id, "tid": tid})
            rows = [dict(r) for r in r2.mappings().fetchall()]

            # If no children via parent_id, try family_id
            if not rows and parent.get("family_id"):
                r3 = await session.execute(sa_text("""
                    SELECT id::text, file_name, mime_type, doc_type, file_size,
                           attachment_index, is_attachment
                    FROM ediscovery_documents
                    WHERE family_id = CAST(:fid AS uuid)
                      AND id <> CAST(:did AS uuid)
                      AND trim(tenant_id::text) = trim(:tid)
                    ORDER BY attachment_index NULLS LAST, file_name
                """), {"fid": parent["family_id"], "did": doc_id, "tid": tid})
                rows = [dict(r) for r in r3.mappings().fetchall()]

        return JSONResponse(_serialize({"attachments": rows}))
    except Exception as e:
        logger.error("review_doc_attachments: %s", e)
        return JSONResponse({"attachments": []})



# ── Bulk Operations (multi-select) ──────────────────────────────────

@router.post("/bulk-tag")
async def review_bulk_tag(request: Request, user=Depends(get_current_user)):
    """Apply or remove a tag from multiple documents.
    Body: {doc_ids: [str], tag_id: str, action: 'apply'|'remove'}
    """
    tid = _tenant(request)
    body = await request.json()
    doc_ids = body.get("doc_ids", [])
    tag_id = body.get("tag_id")
    action = body.get("action", "apply")

    if not doc_ids or not tag_id:
        return JSONResponse({"error": "doc_ids and tag_id required"}, 400)
    if len(doc_ids) > 500:
        return JSONResponse({"error": "Maximum 500 documents per bulk operation"}, 400)

    applied = 0
    try:
        async with AsyncSessionLocal() as session:
            if action == "remove":
                for did in doc_ids:
                    await session.execute(sa_text("""
                        DELETE FROM document_tags
                        WHERE tag_id = CAST(:tag_id AS uuid)
                          AND trim(tenant_id::text) = trim(:tid)
                          AND document_id = CAST(:did AS uuid)
                    """), {"tag_id": tag_id, "tid": tid, "did": did})
                    applied += 1
            else:
                r_tag = await session.execute(sa_text("""
                    SELECT matter_id::text FROM tags WHERE id = CAST(:tid2 AS uuid) LIMIT 1
                """), {"tid2": tag_id})
                tag_row = r_tag.fetchone()
                tag_matter_id = tag_row[0] if tag_row else None

                for did in doc_ids:
                    try:
                        r_ex = await session.execute(sa_text("""
                            SELECT 1 FROM document_tags
                            WHERE document_id = CAST(:did AS uuid)
                              AND tag_id = CAST(:tag_id AS uuid)
                              AND trim(tenant_id::text) = trim(:tid)
                            LIMIT 1
                        """), {"did": did, "tag_id": tag_id, "tid": tid})
                        if not r_ex.fetchone():
                            await session.execute(sa_text("""
                                INSERT INTO document_tags (tenant_id, document_id, tag_id, source,
                                                            source_table, matter_id, applied_by, applied_at)
                                VALUES (:tenant, CAST(:did AS uuid), CAST(:tag_id AS uuid), 'manual',
                                        'ediscovery', CAST(:mid AS uuid), NULL, now())
                            """), {"tenant": tid, "did": did, "tag_id": tag_id, "mid": tag_matter_id})
                            applied += 1
                    except Exception as inner_e:
                        logger.warning("bulk-tag skip doc %s: %s", did, inner_e)
            await session.commit()
        logger.info("bulk-tag: applied=%d, total=%d, tag=%s, action=%s", applied, len(doc_ids), tag_id, action)
        return JSONResponse({"ok": True, "count": len(doc_ids), "applied": applied})
    except Exception as e:
        logger.error("review_bulk_tag: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/bulk-status")
async def review_bulk_status(request: Request, user=Depends(get_current_user)):
    """Update review_status and/or privilege_status for multiple documents.
    Body: {doc_ids: [str], review_status?: str, privilege_status?: str}
    """
    tid = _tenant(request)
    body = await request.json()
    doc_ids = body.get("doc_ids", [])
    priv_val = body.get("privilege_status")
    is_privilege_set = priv_val and priv_val != "none"

    if not doc_ids:
        return JSONResponse({"error": "doc_ids required"}, 400)
    if len(doc_ids) > 500:
        return JSONResponse({"error": "Maximum 500 documents per bulk operation"}, 400)

    updates = []
    params = {"tenant": tid, "dids": doc_ids}
    if "review_status" in body:
        updates.append("review_status = :rs")
        params["rs"] = body["review_status"]
        if body["review_status"] != "unreviewed":
            updates.append("reviewed_at = now()")
        else:
            updates.append("reviewed_at = NULL")
    if "privilege_status" in body:
        if priv_val == "none":
            updates.append("privilege_status = NULL")
        else:
            updates.append("privilege_status = :ps")
            params["ps"] = priv_val
        # Auto-mark as reviewed when privilege is set
        if is_privilege_set and "review_status" not in body:
            updates.append("review_status = 'privileged'")
            updates.append("reviewed_at = COALESCE(reviewed_at, now())")
    if not updates:
        return JSONResponse({"error": "No fields to update"}, 400)
    updates.append("updated_at = now()")

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE ediscovery_documents SET {', '.join(updates)}
                WHERE id = ANY(CAST(:dids AS uuid[]))
                  AND trim(tenant_id::text) = trim(:tenant)
            """), params)
            await session.commit()

            # Auto-apply/remove privilege tags (mirrors single-doc behavior)
            if "privilege_status" in body:
                if is_privilege_set:
                    tag_name = {"privileged": "Privileged", "attorney_work_product": "Attorney Work Product", "redact": "Needs Redaction"}.get(priv_val, "Privileged")
                    # Find the tag
                    r_tag = await session.execute(sa_text("""
                        SELECT id::text FROM tags
                        WHERE trim(tenant_id::text) = trim(:tid) AND name = :tag_name
                        ORDER BY CASE WHEN collection_id IS NOT NULL THEN 0 ELSE 1 END
                        LIMIT 1
                    """), {"tid": tid, "tag_name": tag_name})
                    tag_row = r_tag.fetchone()
                    if tag_row:
                        tag_id = tag_row[0]
                        for did in doc_ids:
                            try:
                                r_ex = await session.execute(sa_text("""
                                    SELECT 1 FROM document_tags
                                    WHERE document_id = CAST(:did AS uuid)
                                      AND tag_id = CAST(:tag_id AS uuid)
                                      AND trim(tenant_id::text) = trim(:tid)
                                    LIMIT 1
                                """), {"did": did, "tag_id": tag_id, "tid": tid})
                                if not r_ex.fetchone():
                                    await session.execute(sa_text("""
                                        INSERT INTO document_tags (tenant_id, document_id, tag_id, source,
                                                                    source_table, applied_by, applied_at)
                                        VALUES (:tenant, CAST(:did AS uuid), CAST(:tag_id AS uuid),
                                                'auto_privilege', 'ediscovery', NULL, now())
                                    """), {"tenant": tid, "did": did, "tag_id": tag_id})
                            except Exception:
                                pass
                        await session.commit()
                else:
                    # Privilege cleared — remove auto_privilege tags
                    for did in doc_ids:
                        try:
                            await session.execute(sa_text("""
                                DELETE FROM document_tags
                                WHERE document_id = CAST(:did AS uuid)
                                  AND trim(tenant_id::text) = trim(:tenant)
                                  AND source = 'auto_privilege'
                            """), {"did": did, "tenant": tid})
                        except Exception:
                            pass
                    await session.commit()

        logger.info("bulk-status: %d docs, priv=%s", len(doc_ids), priv_val)
        return JSONResponse({"ok": True, "count": len(doc_ids)})
    except Exception as e:
        logger.error("review_bulk_status: %s", e)
        return JSONResponse({"error": str(e)}, 500)




@router.get("/collections")
async def review_collections_list(request: Request, matter_id: str = "",
                                  user=Depends(get_current_user)):
    """List collections for a matter — parent/child collapsed, active first."""
    tid = _tenant(request)
    if not matter_id:
        return JSONResponse({"collections": []})
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                WITH child_stats AS (
                    SELECT parent_collection_id,
                           CAST(SUM(COALESCE(NULLIF(document_count, 0), total_docs, 0)) AS bigint) AS child_total,
                           COUNT(*) AS child_count,
                           COUNT(*) FILTER (WHERE status IN ('review_ready', 'failed')) AS child_done,
                           COUNT(*) FILTER (WHERE status = 'failed') AS child_failed,
                           CAST(COALESCE(SUM(
                               (SELECT COUNT(*) FROM ediscovery_documents ed
                                WHERE ed.collection_id = ch.id
                                  AND ed.review_status IS NOT NULL
                                  AND ed.review_status NOT IN ('pending', 'unreviewed'))
                           ), 0) AS bigint) AS child_reviewed
                    FROM ediscovery_collections ch
                    WHERE ch.matter_id = CAST(:mid AS uuid) AND trim(ch.tenant_id::text) = trim(:tid)
                      AND ch.parent_collection_id IS NOT NULL
                    GROUP BY ch.parent_collection_id
                )
                SELECT c.id::text, c.name, c.collection_name,
                       CASE
                         WHEN cs.child_count IS NOT NULL AND cs.child_done < cs.child_count THEN 'processing'
                         WHEN cs.child_count IS NOT NULL AND cs.child_done = cs.child_count THEN 'review_ready'
                         ELSE c.status
                       END AS status,
                       c.source_party, c.source_type, c.received_date::text,
                       c.received_method,
                       CAST(CASE
                         WHEN cs.child_total IS NOT NULL THEN cs.child_total
                         ELSE COALESCE(NULLIF(c.document_count, 0), c.total_docs, 0)
                       END AS bigint) AS total_docs,
                       COALESCE(cs.child_reviewed,
                         (SELECT COUNT(*) FROM ediscovery_documents ed
                          WHERE ed.collection_id = c.id
                            AND ed.review_status IS NOT NULL
                            AND ed.review_status NOT IN ('pending', 'unreviewed'))
                       ) AS reviewed_docs,
                       c.created_at, c.matter_id::text,
                       COALESCE(cs.child_count, 0) AS child_count,
                       COALESCE(cs.child_done, 0) AS children_complete
                FROM ediscovery_collections c
                LEFT JOIN child_stats cs ON cs.parent_collection_id = c.id
                WHERE c.matter_id = CAST(:mid AS uuid)
                  AND trim(c.tenant_id::text) = trim(:tid)
                  AND c.parent_collection_id IS NULL
                  AND COALESCE(c.is_internal, false) = false
                ORDER BY
                  CASE WHEN c.status IN ('processing', 'collecting', 'ingesting', 'queued')
                       OR (cs.child_count IS NOT NULL AND cs.child_done < cs.child_count)
                       THEN 0 ELSE 1 END,
                  c.created_at DESC
            """), {"mid": matter_id, "tid": tid})
            rows = [dict(row) for row in r.mappings().fetchall()]
        return JSONResponse(_serialize({"collections": rows}))
    except Exception as e:
        return JSONResponse({"collections": [], "error": str(e)})



@router.get("/stats")
async def review_stats(request: Request, matter_id: str = "",
                       user=Depends(get_current_user)):
    """Review statistics for a matter — status breakdown, doc types, processing pipeline."""
    tid = _tenant(request)
    if not matter_id:
        return JSONResponse({"error": "matter_id required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            # Review status breakdown
            r_status = await session.execute(sa_text("""
                SELECT COALESCE(review_status, 'unreviewed') AS status, COUNT(*) AS cnt
                FROM ediscovery_documents
                WHERE collection_id IN (
                    SELECT id FROM ediscovery_collections
                    WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                )
                GROUP BY COALESCE(review_status, 'unreviewed')
                ORDER BY cnt DESC
            """), {"mid": matter_id, "tid": tid})
            status_breakdown = [dict(r) for r in r_status.mappings().fetchall()]

            # Doc type breakdown
            r_types = await session.execute(sa_text("""
                SELECT COALESCE(doc_type, 'unknown') AS doc_type, COUNT(*) AS cnt
                FROM ediscovery_documents
                WHERE collection_id IN (
                    SELECT id FROM ediscovery_collections
                    WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                )
                GROUP BY COALESCE(doc_type, 'unknown')
                ORDER BY cnt DESC
            """), {"mid": matter_id, "tid": tid})
            type_breakdown = [dict(r) for r in r_types.mappings().fetchall()]

            # Privilege breakdown
            r_priv = await session.execute(sa_text("""
                SELECT COALESCE(privilege_status, 'none') AS status, COUNT(*) AS cnt
                FROM ediscovery_documents
                WHERE collection_id IN (
                    SELECT id FROM ediscovery_collections
                    WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                )
                AND privilege_status IS NOT NULL AND privilege_status != 'none'
                GROUP BY privilege_status
                ORDER BY cnt DESC
            """), {"mid": matter_id, "tid": tid})
            privilege_breakdown = [dict(r) for r in r_priv.mappings().fetchall()]

        return JSONResponse(_serialize({
            "status_breakdown": status_breakdown,
            "type_breakdown": type_breakdown,
            "privilege_breakdown": privilege_breakdown,
        }))
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)



# ── Collection Status Page API ──

@router.get("/collection-status/{coll_id}")
async def collection_status_detail(request: Request, coll_id: str, user=Depends(get_current_user)):
    """Full collection status: metadata, doc stats, errors, pipeline jobs."""
    tid = _tenant(request)
    try:
        result = {}
        async with AsyncSessionLocal() as session:
            # Collection detail
            r = await session.execute(sa_text("""
                SELECT c.*, m.matter_name, m.matter_number, cl.client_name
                FROM ediscovery_collections c
                LEFT JOIN matters m ON m.id = c.matter_id AND trim(m.tenant_id::text) = trim(c.tenant_id::text)
                LEFT JOIN clients cl ON m.client_id = cl.id AND trim(cl.tenant_id::text) = trim(c.tenant_id::text)
                WHERE c.id = CAST(:cid AS uuid) AND trim(c.tenant_id::text) = trim(:tid)
            """), {"cid": coll_id, "tid": tid})
            row = r.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "Not found"}, 404)
            result["collection"] = dict(row)

            # Doc type breakdown
            r2 = await session.execute(sa_text("""
                SELECT COALESCE(doc_type, 'unknown') AS doc_type, COUNT(*) AS cnt
                FROM ediscovery_documents WHERE collection_id = CAST(:cid AS uuid)
                GROUP BY COALESCE(doc_type, 'unknown') ORDER BY cnt DESC
            """), {"cid": coll_id})
            result["doc_stats"] = [dict(r) for r in r2.mappings().fetchall()]

            # Errors (docs with processing issues)
            r3 = await session.execute(sa_text("""
                SELECT id::text AS doc_id, file_name, processing_status,
                       doc_type, mime_type
                FROM ediscovery_documents
                WHERE collection_id = CAST(:cid AS uuid)
                AND processing_status IN ('error', 'failed')
                ORDER BY file_name LIMIT 100
            """), {"cid": coll_id})
            result["errors"] = [dict(r) for r in r3.mappings().fetchall()]

            # Pipeline jobs
            r4 = await session.execute(sa_text("""
                SELECT id::text, status, total_docs, processed_docs, chunked_docs,
                       skipped_docs, error_docs, error_detail,
                       started_at, finished_at, created_at
                FROM ediscovery_chunking_jobs
                WHERE collection_id = CAST(:cid AS uuid)
                ORDER BY created_at DESC LIMIT 20
            """), {"cid": coll_id})
            result["jobs"] = [dict(r) for r in r4.mappings().fetchall()]

            # Child collections (for decomposed parents)
            r_children = await session.execute(sa_text("""
                SELECT id::text, collection_name, name, status,
                       COALESCE(NULLIF(document_count, 0), total_docs, 0) AS total_docs,
                       COALESCE(processed_docs, 0) AS processed_docs,
                       source_type, created_at
                FROM ediscovery_collections
                WHERE parent_collection_id = CAST(:cid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                ORDER BY
                  CASE WHEN status IN ('processing','collecting','ingesting','queued') THEN 0 ELSE 1 END,
                  collection_name ASC
            """), {"cid": coll_id, "tid": tid})
            result["children"] = [dict(r) for r in r_children.mappings().fetchall()]

            # Synthetic logs from audit_log + job timestamps
            logs = []
            if result["collection"].get("created_at"):
                logs.append({"timestamp": str(result["collection"]["created_at"]), "level": "info", "message": f"Collection created: {result['collection'].get('collection_name','')}"})
            for j in result.get("jobs", []):
                if j.get("started_at"):
                    logs.append({"timestamp": str(j["started_at"]), "level": "info", "message": f"Pipeline job started ({j.get('total_docs',0)} docs)"})
                if j.get("finished_at"):
                    lvl = "success" if (j.get("error_docs") or 0) == 0 else "warn"
                    msg = f"Pipeline job completed: {j.get('processed_docs',0)} processed, {j.get('error_docs',0)} errors"
                    logs.append({"timestamp": str(j["finished_at"]), "level": lvl, "message": msg})
                if j.get("error_detail"):
                    logs.append({"timestamp": str(j.get("finished_at") or j.get("started_at","")), "level": "error", "message": j["error_detail"][:200]})
            logs.sort(key=lambda x: x.get("timestamp",""))
            result["logs"] = logs

        return JSONResponse(_serialize(result))
    except Exception as e:
        logger.error("collection_status_detail: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/collection-status/{coll_id}/logs")
async def collection_status_logs(request: Request, coll_id: str,
                                 after_id: int = 0, before_id: int = 0,
                                 limit: int = 200, user=Depends(get_current_user)):
    """Keyset-paged ingestion progress logs.
    No params -> last `limit` events (live tail).
    after_id  -> events newer than that id (incremental follow).
    before_id -> events older than that id (scroll-up history)."""
    tid = _tenant(request)
    lim = max(1, min(int(limit or 200), 500))
    try:
        logs = []
        total = 0
        async with AsyncSessionLocal() as session:
            base = ("FROM ediscovery_ingestion_log "
                    "WHERE collection_id = CAST(:cid AS uuid) "
                    "AND TRIM(tenant_id) = :tid")
            params = {"cid": coll_id, "tid": tid, "lim": lim}
            if after_id:
                q = ("SELECT id, level, message, created_at " + base +
                     " AND id > :aid ORDER BY id ASC LIMIT :lim")
                params["aid"] = int(after_id)
            elif before_id:
                q = ("SELECT id, level, message, created_at " + base +
                     " AND id < :bid ORDER BY id DESC LIMIT :lim")
                params["bid"] = int(before_id)
            else:
                q = ("SELECT id, level, message, created_at " + base +
                     " ORDER BY id DESC LIMIT :lim")
            r = await session.execute(sa_text(q), params)
            rows = [dict(m) for m in r.mappings().fetchall()]
            if not after_id:
                rows.reverse()  # DESC fetch -> chronological for display
            for row in rows:
                logs.append({"id": row["id"], "timestamp": str(row["created_at"]),
                             "level": row["level"], "message": row["message"]})
            rc = await session.execute(sa_text("SELECT count(*) " + base),
                                       {"cid": coll_id, "tid": tid})
            total = rc.scalar() or 0

            # If no real-time logs, fall back to synthetic from chunking jobs
            if not logs and not after_id and not before_id:
                r2 = await session.execute(sa_text("""
                    SELECT id::text, status, total_docs, processed_docs, error_docs,
                           error_detail, started_at, finished_at
                    FROM ediscovery_chunking_jobs
                    WHERE collection_id = CAST(:cid AS uuid)
                    ORDER BY created_at DESC LIMIT 20
                """), {"cid": coll_id})
                for j in r2.mappings().fetchall():
                    j = dict(j)
                    if j.get("started_at"):
                        logs.append({"timestamp": str(j["started_at"]), "level": "info", "message": f"Job started ({j.get('total_docs',0)} docs)"})
                    if j.get("finished_at"):
                        lvl = "success" if (j.get("error_docs") or 0) == 0 else "warn"
                        logs.append({"timestamp": str(j["finished_at"]), "level": lvl, "message": f"Completed: {j.get('processed_docs',0)} processed, {j.get('error_docs',0)} errors"})
                logs.sort(key=lambda x: x.get("timestamp",""))
        return JSONResponse(_serialize({
            "logs": logs, "total": total,
            "first_id": (logs[0].get("id") if logs else None),
            "last_id": (logs[-1].get("id") if logs else None)}))
    except Exception as e:
        return JSONResponse({"logs": []})




@router.get("/collection-status/{coll_id}/logfile")
async def collection_status_logfile(request: Request, coll_id: str,
                                    tail: int = 300,
                                    before_offset: int = -1,
                                    after_offset: int = -1,
                                    user=Depends(get_current_user)):
    """Per-job log file, byte-offset paged.
    No params      -> last `tail` lines + start_offset/end_offset.
    after_offset   -> only complete lines written past that byte (follow).
    before_offset  -> a chunk of older lines ending at that byte (history)."""
    tid = _tenant(request)
    CHUNK = 262144
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT storage_path FROM ediscovery_collections
                WHERE id = CAST(:cid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"cid": coll_id, "tid": tid})
            row = r.fetchone()
            if not row:
                return JSONResponse({"error": "Not found"}, 404)
            log_path = os.path.join(row[0], "ingestion.log")
        if not os.path.exists(log_path):
            return JSONResponse({"lines": [], "path": log_path, "exists": False})
        size = os.path.getsize(log_path)

        def read_range(lo, hi):
            with open(log_path, "rb") as f:
                f.seek(lo)
                return f.read(max(0, hi - lo))

        if after_offset >= 0:
            lo = min(int(after_offset), size)
            data = read_range(lo, size)
            j = data.rfind(b"\n")
            if j < 0:
                return JSONResponse({"lines": [], "end_offset": lo,
                                     "size": size, "exists": True})
            lines_b = data[:j].split(b"\n")
            return JSONResponse({
                "lines": [x.decode("utf-8", "replace") for x in lines_b],
                "end_offset": lo + j + 1, "size": size, "exists": True})

        if before_offset >= 0:
            hi = min(int(before_offset), size)
            lo = max(0, hi - CHUNK)
            data = read_range(lo, hi)
            if lo > 0:
                i = data.find(b"\n")
                if i < 0:
                    return JSONResponse({"lines": [], "start_offset": lo,
                                         "size": size, "exists": True})
                lo += i + 1
                data = data[i + 1:]
            if data.endswith(b"\n"):
                data = data[:-1]
            lines_b = data.split(b"\n") if data else []
            return JSONResponse({
                "lines": [x.decode("utf-8", "replace") for x in lines_b],
                "start_offset": lo, "size": size, "exists": True})

        # default: tail
        lo = max(0, size - CHUNK)
        data = read_range(lo, size)
        if lo > 0:
            i = data.find(b"\n")
            if i < 0:
                lo, data = size, b""
            else:
                lo += i + 1
                data = data[i + 1:]
        j = data.rfind(b"\n")
        if j < 0:
            lines_b, end = [], lo
        else:
            lines_b, end = data[:j].split(b"\n"), lo + j + 1
        if len(lines_b) > int(tail):
            dropped = lines_b[:-int(tail)]
            lo += sum(len(x) + 1 for x in dropped)
            lines_b = lines_b[-int(tail):]
        return JSONResponse({
            "lines": [x.decode("utf-8", "replace") for x in lines_b],
            "start_offset": lo, "end_offset": end,
            "path": log_path, "size": size, "exists": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.post("/collection-status/{coll_id}/notes")
async def collection_status_save_notes(request: Request, coll_id: str, user=Depends(get_current_user)):
    """Save notes to collection.issue_map.notes."""
    tid = _tenant(request)
    body = await request.json()
    notes_text = body.get("notes", "")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE ediscovery_collections
                SET issue_map = COALESCE(issue_map, '{}'::jsonb) || jsonb_build_object('notes', CAST(:notes AS text)),
                    updated_at = now()
                WHERE id = CAST(:cid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"cid": coll_id, "tid": tid, "notes": notes_text})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.post("/collection-status/{coll_id}/metadata")
async def collection_status_save_metadata(request: Request, coll_id: str, user=Depends(get_current_user)):
    """Update editable collection metadata fields."""
    tid = _tenant(request)
    body = await request.json()
    sets = []
    params = {"cid": coll_id, "tid": tid}
    for field in ["source_party", "source_type", "received_method", "stated_bates_range"]:
        if field in body:
            sets.append(f"{field} = :{field}")
            params[field] = body[field]
    if "received_date" in body and body["received_date"]:
        sets.append("received_date = CAST(:rd AS date)")
        params["rd"] = body["received_date"]
    if not sets:
        return JSONResponse({"ok": True})
    sets.append("updated_at = now()")
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE ediscovery_collections SET {', '.join(sets)}
                WHERE id = CAST(:cid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), params)
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)
