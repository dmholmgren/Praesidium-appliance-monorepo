"""
modules/ediscovery/routes/annotation_api.py
JSON API endpoints for eDiscovery document annotations.

Tier 1:  text-range redactions, highlights, comments, in-document search
Tier 2+: coordinate-based redactions (future)

All soft-delete — annotations are never hard-deleted.
"""
import logging
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery/annotations", tags=["ediscovery-annotations"])


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _user_info(user) -> dict:
    """Extract id and display name from user object."""
    if isinstance(user, dict):
        uid = user.get("id")
        name = user.get("display_name") or user.get("full_name") or user.get("username") or ""
    else:
        uid = getattr(user, "id", None)
        name = getattr(user, "display_name", "") or getattr(user, "full_name", "") or getattr(user, "username", "") or ""
    return {"id": str(uid) if uid else None, "name": name}


def _serialize(obj):
    """Make DB row dicts JSON-safe."""
    import uuid as _uuid
    from datetime import datetime, date
    from decimal import Decimal
    if obj is None:
        return None
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(v) for v in obj]
    if isinstance(obj, _uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return obj


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET — list annotations for a document
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/doc/{doc_id}")
async def list_annotations(
    request: Request,
    doc_id: str,
    annotation_type: str = Query("", description="Filter: redaction, highlight, comment, or empty for all"),
    user=Depends(get_current_user),
):
    """List all active annotations for a document."""
    tid = _tenant(request)
    where = [
        "trim(a.tenant_id::text) = trim(:tid)",
        "a.document_id = CAST(:did AS uuid)",
        "a.deleted_at IS NULL",
    ]
    params = {"tid": tid, "did": doc_id}

    if annotation_type:
        where.append("a.annotation_type = :atype")
        params["atype"] = annotation_type

    wsql = " AND ".join(where)

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(f"""
                SELECT a.id::text, a.annotation_type,
                       a.text_start, a.text_end, a.selected_text,
                       a.page_number, a.x, a.y, a.width, a.height,
                       a.redaction_style, a.redaction_label,
                       a.highlight_color,
                       a.comment_text, a.parent_id::text,
                       a.created_by::text, a.created_by_name,
                       a.created_at, a.updated_at
                FROM doc_annotations a
                WHERE {wsql}
                ORDER BY
                    CASE a.annotation_type
                        WHEN 'redaction' THEN 1
                        WHEN 'highlight' THEN 2
                        WHEN 'comment'   THEN 3
                    END,
                    COALESCE(a.text_start, 0),
                    a.created_at ASC
            """), params)
            rows = [dict(row) for row in r.mappings().fetchall()]

        return JSONResponse(_serialize({"annotations": rows, "total": len(rows)}))
    except Exception as e:
        logger.error("list_annotations: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POST — create annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/doc/{doc_id}")
async def create_annotation(request: Request, doc_id: str, user=Depends(get_current_user)):
    """
    Create a redaction, highlight, or comment.

    Body fields:
      annotation_type: 'redaction' | 'highlight' | 'comment'
      text_start, text_end: char offsets (for text-range annotations)
      selected_text: the selected text (for audit snapshot)
      redaction_style: 'black' | 'cross' | 'text' | 'white'
      redaction_label: e.g. 'Privileged'
      highlight_color: 'yellow' | 'green' | 'blue' | 'red' | 'pink' | 'orange'
      comment_text: the comment body
      parent_id: UUID of parent comment (for replies)
      page_number, x, y, width, height: coordinate-based (Tier 2)
    """
    tid = _tenant(request)
    body = await request.json()
    atype = body.get("annotation_type", "").strip()

    if atype not in ("redaction", "highlight", "comment"):
        return JSONResponse({"error": "annotation_type must be redaction, highlight, or comment"}, 400)

    # Look up collection_id from the document
    ui = _user_info(user)

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT collection_id::text FROM ediscovery_documents
                WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tid})
            row = r.fetchone()
            if not row:
                return JSONResponse({"error": "Document not found"}, 404)
            collection_id = row[0]

            r_new = await session.execute(sa_text("""
                INSERT INTO doc_annotations (
                    tenant_id, document_id, collection_id, annotation_type,
                    text_start, text_end, selected_text,
                    page_number, x, y, width, height,
                    redaction_style, redaction_label,
                    highlight_color,
                    comment_text, parent_id,
                    created_by, created_by_name, created_at
                ) VALUES (
                    :tid, CAST(:did AS uuid), CAST(:cid AS uuid), :atype,
                    :text_start, :text_end, :selected_text,
                    :page_number, :x, :y, :width, :height,
                    :redaction_style, :redaction_label,
                    :highlight_color,
                    :comment_text, CAST(:parent_id AS uuid),
                    :created_by, :created_by_name, now()
                ) RETURNING id::text
            """), {
                "tid": tid, "did": doc_id, "cid": collection_id, "atype": atype,
                "text_start": body.get("text_start"),
                "text_end": body.get("text_end"),
                "selected_text": body.get("selected_text"),
                "page_number": body.get("page_number"),
                "x": body.get("x"), "y": body.get("y"),
                "width": body.get("width"), "height": body.get("height"),
                "redaction_style": body.get("redaction_style"),
                "redaction_label": body.get("redaction_label"),
                "highlight_color": body.get("highlight_color"),
                "comment_text": body.get("comment_text"),
                "parent_id": body.get("parent_id") or None,
                "created_by": (int(ui["id"]) if ui["id"] else None),
                "created_by_name": ui["name"],
            })
            new_id = r_new.scalar()
            await session.commit()

        return JSONResponse({"ok": True, "id": new_id})
    except Exception as e:
        logger.error("create_annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PUT — update annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.put("/{annotation_id}")
async def update_annotation(request: Request, annotation_id: str, user=Depends(get_current_user)):
    """
    Update annotation fields. Allowed updates:
      redaction_label, redaction_style, highlight_color, comment_text
    """
    tid = _tenant(request)
    body = await request.json()
    ui = _user_info(user)

    allowed = {"redaction_label", "redaction_style", "highlight_color", "comment_text"}
    updates = []
    params = {"aid": annotation_id, "tid": tid}

    for key in allowed:
        if key in body:
            updates.append(f"{key} = :{key}")
            params[key] = body[key]

    if not updates:
        return JSONResponse({"error": "No valid fields to update"}, 400)

    updates.append("updated_at = now()")
    updates.append("updated_by = :updated_by")
    params["updated_by"] = (int(ui["id"]) if ui["id"] else None)

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE doc_annotations SET {', '.join(updates)}
                WHERE id = CAST(:aid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                  AND deleted_at IS NULL
            """), params)
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("update_annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# DELETE — soft delete annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.delete("/{annotation_id}")
async def delete_annotation(request: Request, annotation_id: str, user=Depends(get_current_user)):
    """Soft-delete an annotation (and any child replies)."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            # Soft-delete the annotation and any replies
            await session.execute(sa_text("""
                UPDATE doc_annotations SET deleted_at = now()
                WHERE (id = CAST(:aid AS uuid) OR parent_id = CAST(:aid AS uuid))
                  AND trim(tenant_id::text) = trim(:tid)
                  AND deleted_at IS NULL
            """), {"aid": annotation_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("delete_annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET — in-document text search
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/doc/{doc_id}/search")
async def search_in_document(
    request: Request,
    doc_id: str,
    q: str = Query("", description="Search term"),
    user=Depends(get_current_user),
):
    """
    Search within a document's extracted text.
    Returns character offset ranges for each hit, plus surrounding context.
    """
    tid = _tenant(request)
    q = (q or "").strip()
    if not q or len(q) < 2:
        return JSONResponse({"hits": [], "total": 0, "query": q})

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT extracted_text FROM ediscovery_documents
                WHERE id = CAST(:did AS uuid) AND trim(tenant_id::text) = trim(:tid)
                LIMIT 1
            """), {"did": doc_id, "tid": tid})
            row = r.fetchone()

        if not row or not row[0]:
            return JSONResponse({"hits": [], "total": 0, "query": q,
                                 "message": "No extracted text available for this document."})

        full_text = row[0]
        text_lower = full_text.lower()
        q_lower = q.lower()

        hits = []
        start = 0
        context_chars = 80  # chars of context on each side

        while True:
            idx = text_lower.find(q_lower, start)
            if idx == -1:
                break
            ctx_start = max(0, idx - context_chars)
            ctx_end = min(len(full_text), idx + len(q) + context_chars)
            context = full_text[ctx_start:ctx_end]
            # Add ellipsis if truncated
            if ctx_start > 0:
                context = "\u2026" + context
            if ctx_end < len(full_text):
                context = context + "\u2026"

            hits.append({
                "start": idx,
                "end": idx + len(q),
                "context": context,
                "context_offset": idx - ctx_start + (1 if ctx_start > 0 else 0),
            })
            start = idx + 1

            # Cap at 500 hits to avoid giant responses
            if len(hits) >= 500:
                break

        return JSONResponse({
            "hits": hits,
            "total": len(hits),
            "query": q,
            "text_length": len(full_text),
        })
    except Exception as e:
        logger.error("search_in_document: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET — annotation summary counts for a document
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/doc/{doc_id}/summary")
async def annotation_summary(request: Request, doc_id: str, user=Depends(get_current_user)):
    """Counts of each annotation type for a document."""
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT annotation_type, COUNT(*) AS cnt
                FROM doc_annotations
                WHERE document_id = CAST(:did AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                  AND deleted_at IS NULL
                GROUP BY annotation_type
            """), {"did": doc_id, "tid": tid})
            rows = {row["annotation_type"]: row["cnt"] for row in r.mappings().fetchall()}

        return JSONResponse({
            "redactions": rows.get("redaction", 0),
            "highlights": rows.get("highlight", 0),
            "comments": rows.get("comment", 0),
        })
    except Exception as e:
        logger.error("annotation_summary: %s", e)
        return JSONResponse({"error": str(e)}, 500)
