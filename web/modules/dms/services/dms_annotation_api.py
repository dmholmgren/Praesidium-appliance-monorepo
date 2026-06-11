"""
modules/dms/services/dms_annotation_api.py — DMS Annotation API

CRUD for document annotations (highlights, redactions, comments)
+ in-document text search with hit positions.

Endpoints:
  GET  /api/v1/dms/annotations/{doc_id}       → list annotations
  POST /api/v1/dms/annotations/{doc_id}       → create annotation
  PUT  /api/v1/dms/annotations/item/{ann_id}  → update annotation
  DELETE /api/v1/dms/annotations/item/{ann_id} → soft-delete annotation
  GET  /api/v1/dms/doc-text/{doc_id}          → full extracted text for viewer
  GET  /api/v1/dms/doc-text/{doc_id}/search?q=term → search hits within doc text

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations
import logging, re, uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms", tags=["dms-annotations"])

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r): return getattr(r.state, "user_id", None)


# ── Pydantic models ──
class AnnotationCreate(BaseModel):
    annotation_type: str  # 'highlight', 'redaction', 'comment'
    text_start: Optional[int] = None
    text_end: Optional[int] = None
    selected_text: Optional[str] = None
    highlight_color: Optional[str] = "yellow"
    redaction_style: Optional[str] = "black"
    redaction_label: Optional[str] = None
    comment_text: Optional[str] = None
    parent_id: Optional[str] = None
    page_number: Optional[int] = None
    x: Optional[float] = None
    y: Optional[float] = None
    width: Optional[float] = None
    height: Optional[float] = None

class AnnotationUpdate(BaseModel):
    comment_text: Optional[str] = None
    redaction_label: Optional[str] = None
    highlight_color: Optional[str] = None


# ── Ensure table exists (idempotent) ──
_table_ensured = False
async def _ensure_table():
    global _table_ensured
    if _table_ensured:
        return
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS doc_annotations (
                    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    tenant_id       CHAR(36) NOT NULL,
                    document_id     UUID NOT NULL,
                    annotation_type VARCHAR(32) NOT NULL,
                    text_start      INTEGER,
                    text_end        INTEGER,
                    selected_text   TEXT,
                    page_number     INTEGER,
                    x               NUMERIC,
                    y               NUMERIC,
                    width           NUMERIC,
                    height          NUMERIC,
                    redaction_style VARCHAR(32),
                    redaction_label VARCHAR(255),
                    highlight_color VARCHAR(32) DEFAULT 'yellow',
                    comment_text    TEXT,
                    parent_id       UUID,
                    markup_set_id   UUID,
                    created_by      UUID,
                    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_by      UUID,
                    updated_at      TIMESTAMPTZ,
                    deleted_at      TIMESTAMPTZ
                )
            """))
            await s.execute(sa_text("""
                CREATE INDEX IF NOT EXISTS idx_doc_ann_doc
                ON doc_annotations(document_id) WHERE deleted_at IS NULL
            """))
            await s.execute(sa_text("""
                CREATE INDEX IF NOT EXISTS idx_doc_ann_tenant
                ON doc_annotations(tenant_id) WHERE deleted_at IS NULL
            """))
            await s.commit()
            _table_ensured = True
    except Exception as e:
        logger.warning("doc_annotations table check: %s", e)
        _table_ensured = True


# ── List annotations for a document ──
@router.get("/annotations/{doc_id}")
async def list_annotations(request: Request, doc_id: str):
    tid = _tid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    await _ensure_table()
    try:
        async with AsyncSessionLocal() as s:
            rows = await s.execute(sa_text("""
                SELECT id::text, annotation_type, text_start, text_end, selected_text,
                       page_number, x, y, width, height,
                       redaction_style, redaction_label, highlight_color,
                       comment_text, parent_id::text,
                       created_by::text, created_at, updated_at
                FROM doc_annotations
                WHERE document_id = :did::uuid AND trim(tenant_id) = trim(:t)
                  AND deleted_at IS NULL
                ORDER BY COALESCE(text_start, 0), created_at
            """), {"did": doc_id, "t": tid})
            anns = []
            for r in rows.mappings():
                anns.append({
                    "id": r["id"],
                    "annotation_type": r["annotation_type"],
                    "text_start": r["text_start"],
                    "text_end": r["text_end"],
                    "selected_text": r["selected_text"],
                    "page_number": r["page_number"],
                    "x": float(r["x"]) if r["x"] is not None else None,
                    "y": float(r["y"]) if r["y"] is not None else None,
                    "width": float(r["width"]) if r["width"] is not None else None,
                    "height": float(r["height"]) if r["height"] is not None else None,
                    "redaction_style": r["redaction_style"],
                    "redaction_label": r["redaction_label"],
                    "highlight_color": r["highlight_color"],
                    "comment_text": r["comment_text"],
                    "parent_id": r["parent_id"],
                    "created_by": r["created_by"],
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
                })
            return JSONResponse({"annotations": anns, "count": len(anns)})
    except Exception as e:
        logger.error("list annotations: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ── Create annotation ──
@router.post("/annotations/{doc_id}")
async def create_annotation(request: Request, doc_id: str, body: AnnotationCreate):
    tid = _tid(request)
    uid = _uid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    if body.annotation_type not in ("highlight", "redaction", "comment"):
        return JSONResponse({"error": "invalid annotation_type"}, 400)
    await _ensure_table()
    try:
        ann_id = str(uuid.uuid4())
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("""
                INSERT INTO doc_annotations
                    (id, tenant_id, document_id, annotation_type,
                     text_start, text_end, selected_text,
                     page_number, x, y, width, height,
                     redaction_style, redaction_label, highlight_color,
                     comment_text, parent_id, created_by)
                VALUES
                    (:id::uuid, :t, :did::uuid, :atype,
                     :ts, :te, :st,
                     :pn, :x, :y, :w, :h,
                     :rs, :rl, :hc,
                     :ct, :pid, :uid)
            """), {
                "id": ann_id, "t": tid, "did": doc_id,
                "atype": body.annotation_type,
                "ts": body.text_start, "te": body.text_end,
                "st": body.selected_text,
                "pn": body.page_number,
                "x": body.x, "y": body.y, "w": body.width, "h": body.height,
                "rs": body.redaction_style, "rl": body.redaction_label,
                "hc": body.highlight_color,
                "ct": body.comment_text,
                "pid": body.parent_id if body.parent_id else None,
                "uid": uid,
            })
            await s.commit()
        return JSONResponse({"id": ann_id, "status": "created"}, 201)
    except Exception as e:
        logger.error("create annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ── Update annotation ──
@router.put("/annotations/item/{ann_id}")
async def update_annotation(request: Request, ann_id: str, body: AnnotationUpdate):
    tid = _tid(request)
    uid = _uid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    await _ensure_table()
    try:
        sets = ["updated_at = now()"]
        params = {"aid": ann_id, "t": tid}
        if uid:
            sets.append("updated_by = :uid")
            params["uid"] = uid
        if body.comment_text is not None:
            sets.append("comment_text = :ct")
            params["ct"] = body.comment_text
        if body.redaction_label is not None:
            sets.append("redaction_label = :rl")
            params["rl"] = body.redaction_label
        if body.highlight_color is not None:
            sets.append("highlight_color = :hc")
            params["hc"] = body.highlight_color

        async with AsyncSessionLocal() as s:
            await s.execute(sa_text(f"""
                UPDATE doc_annotations SET {', '.join(sets)}
                WHERE id = :aid::uuid AND trim(tenant_id) = trim(:t) AND deleted_at IS NULL
            """), params)
            await s.commit()
        return JSONResponse({"status": "updated"})
    except Exception as e:
        logger.error("update annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ── Soft-delete annotation ──
@router.delete("/annotations/item/{ann_id}")
async def delete_annotation(request: Request, ann_id: str):
    tid = _tid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    await _ensure_table()
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(sa_text("""
                UPDATE doc_annotations SET deleted_at = now()
                WHERE id = :aid::uuid AND trim(tenant_id) = trim(:t) AND deleted_at IS NULL
            """), {"aid": ann_id, "t": tid})
            await s.commit()
        return JSONResponse({"status": "deleted"})
    except Exception as e:
        logger.error("delete annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ── Get full document text (for viewer text mode) ──
@router.get("/doc-text/{doc_id}")
async def get_doc_text(request: Request, doc_id: str):
    tid = _tid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text("""
                SELECT d.extracted_text, d.filename, d.document_type
                FROM documents d
                WHERE d.id = :did::uuid AND trim(d.tenant_id) = trim(:t)
            """), {"did": doc_id, "t": tid})
            row = r.mappings().fetchone()
            if not row:
                return JSONResponse({"error": "not found"}, 404)
            return JSONResponse({
                "text": row["extracted_text"] or "",
                "filename": row["filename"] or "",
                "document_type": row["document_type"] or "",
                "length": len(row["extracted_text"] or ""),
            })
    except Exception as e:
        logger.error("doc text: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ── Search within document text ──
@router.get("/doc-text/{doc_id}/search")
async def search_doc_text(request: Request, doc_id: str, q: str = ""):
    """Find all occurrences of search term within document extracted text.
    Returns character offsets and surrounding context for each hit."""
    tid = _tid(request)
    if not tid: return JSONResponse({"error": "no tenant"}, 401)
    if not q or len(q) < 2:
        return JSONResponse({"hits": [], "total": 0, "query": q})

    try:
        async with AsyncSessionLocal() as s:
            r = await s.execute(sa_text("""
                SELECT d.extracted_text FROM documents d
                WHERE d.id = :did::uuid AND trim(d.tenant_id) = trim(:t)
            """), {"did": doc_id, "t": tid})
            row = r.mappings().fetchone()
            if not row or not row["extracted_text"]:
                return JSONResponse({"hits": [], "total": 0, "query": q})

            full_text = row["extracted_text"]
            hits = []
            ctx_chars = 80
            pattern = re.compile(re.escape(q), re.IGNORECASE)
            for m in pattern.finditer(full_text):
                start = m.start()
                end = m.end()
                ctx_start = max(0, start - ctx_chars)
                ctx_end = min(len(full_text), end + ctx_chars)
                hits.append({
                    "start": start,
                    "end": end,
                    "match": m.group(),
                    "context_before": full_text[ctx_start:start],
                    "context_after": full_text[end:ctx_end],
                })
                if len(hits) >= 500:
                    break

            return JSONResponse({
                "hits": hits, "total": len(hits), "query": q,
                "text_length": len(full_text),
            })
    except Exception as e:
        logger.error("doc text search: %s", e)
        return JSONResponse({"error": str(e)}, 500)
