"""
modules/annotations/annotation_objects_api.py — Generic, polymorphic annotation API.

ONE store (doc_annotations) keyed by (source_type, source_id) so highlights /
redactions / comments are reusable objects, authored in any viewer and later
pushable to the trial-presentation viewer.

  source_type : 'ediscovery' | 'dms' | 'record' | 'transcript' | 'exhibit' | 'path'
  source_id   : the id of that thing (uuid string, or base64url path for 'path')

Endpoints (prefix /api/v1/annotations):
  GET    /{source_type}/{source_id}               -> list active annotations
  GET    /{source_type}/{source_id}/contributors  -> distinct authors (+counts)
  POST   /{source_type}/{source_id}               -> create
  PUT    /item/{ann_id}                            -> update
  DELETE /item/{ann_id}                            -> soft-delete (+replies)
  GET    /{source_type}/{source_id}/sets          -> list markup sets
  POST   /{source_type}/{source_id}/sets          -> create a saved markup set
  GET    /sets/{set_id}/file                       -> stream embossed PDF
  DELETE /sets/{set_id}                            -> soft-delete a markup set
  POST   /{source_type}/{source_id}/emboss        -> burn a flattened PDF (markup_embosser)

All soft-delete; annotations carry created_by (bigint) + created_by_name for the
Mine / Others / Combined per-user views.
"""
import json
import logging
import re
import uuid as _uuid
from datetime import datetime, date
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/annotations", tags=["annotations"])

VALID_SOURCES = {"ediscovery", "dms", "record", "transcript", "exhibit", "path"}
VALID_TYPES = {"redaction", "highlight", "comment", "drawing"}
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _tenant(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _is_uuid(s) -> bool:
    return bool(s) and bool(_UUID_RE.match(str(s)))


def _uuid_or_none(s):
    """Return the string if it's a valid UUID, else None (for CAST(:p AS uuid))."""
    return str(s) if _is_uuid(s) else None


def _user_info(user) -> dict:
    if isinstance(user, dict):
        uid = user.get("id")
        name = user.get("display_name") or user.get("full_name") or user.get("username") or ""
    else:
        uid = getattr(user, "id", None)
        name = getattr(user, "display_name", "") or getattr(user, "full_name", "") \
            or getattr(user, "username", "") or ""
    try:
        uid_int = int(uid) if uid is not None else None
    except (TypeError, ValueError):
        uid_int = None
    return {"id": uid_int, "name": name}


def _serialize(obj):
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
# GET — list annotations for a source
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/{source_type}/{source_id}")
async def list_annotations(
    request: Request,
    source_type: str,
    source_id: str,
    annotation_type: str = Query("", description="redaction|highlight|comment, or empty for all"),
    markup_set_id: str = Query("", description="filter to a markup set"),
    user=Depends(get_current_user),
):
    tid = _tenant(request)
    if source_type not in VALID_SOURCES:
        return JSONResponse({"error": f"unknown source_type {source_type}"}, 400)

    where = [
        "trim(a.tenant_id::text) = trim(:tid)",
        "a.source_type = :st",
        "a.source_id = :sid",
        "a.deleted_at IS NULL",
    ]
    params = {"tid": tid, "st": source_type, "sid": source_id}
    if annotation_type:
        where.append("a.annotation_type = :atype")
        params["atype"] = annotation_type
    if markup_set_id:
        where.append("a.markup_set_id = CAST(:msid AS uuid)")
        params["msid"] = markup_set_id
    wsql = " AND ".join(where)

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text(f"""
                SELECT a.id::text, a.annotation_type,
                       a.text_start, a.text_end, a.selected_text,
                       a.page_number, a.x, a.y, a.width, a.height,
                       a.redaction_style, a.redaction_label, a.highlight_color,
                       a.comment_text, a.parent_id::text, a.markup_set_id::text, a.path_data,
                       a.created_by, a.created_by_name,
                       a.created_at, a.updated_at
                FROM doc_annotations a
                WHERE {wsql}
                ORDER BY
                    COALESCE(a.page_number, 0),
                    COALESCE(a.text_start, 0),
                    a.created_at ASC
            """), params)
            rows = [dict(row) for row in r.mappings().fetchall()]
        for row in rows:
            pd = row.get("path_data")
            if isinstance(pd, str):
                try: row["path_data"] = json.loads(pd)
                except Exception: row["path_data"] = None
        return JSONResponse(_serialize({"annotations": rows, "total": len(rows)}))
    except Exception as e:
        logger.error("list_annotations: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GET — distinct contributors (drives per-user checkboxes)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/{source_type}/{source_id}/contributors")
async def list_contributors(
    request: Request, source_type: str, source_id: str, user=Depends(get_current_user),
):
    tid = _tenant(request)
    me = _user_info(user)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT a.created_by,
                       MAX(a.created_by_name) AS created_by_name,
                       COUNT(*) AS cnt
                FROM doc_annotations a
                WHERE trim(a.tenant_id::text) = trim(:tid)
                  AND a.source_type = :st AND a.source_id = :sid
                  AND a.deleted_at IS NULL
                GROUP BY a.created_by
                ORDER BY cnt DESC
            """), {"tid": tid, "st": source_type, "sid": source_id})
            rows = [dict(row) for row in r.mappings().fetchall()]
        for row in rows:
            row["is_me"] = (row.get("created_by") is not None and row.get("created_by") == me["id"])
        return JSONResponse(_serialize({"contributors": rows, "me": me["id"], "me_name": me["name"]}))
    except Exception as e:
        logger.error("list_contributors: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POST — create annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/{source_type}/{source_id}")
async def create_annotation(
    request: Request, source_type: str, source_id: str, user=Depends(get_current_user),
):
    tid = _tenant(request)
    if source_type not in VALID_SOURCES:
        return JSONResponse({"error": f"unknown source_type {source_type}"}, 400)
    body = await request.json()
    atype = (body.get("annotation_type") or "").strip()
    if atype not in VALID_TYPES:
        return JSONResponse({"error": "annotation_type must be redaction, highlight, or comment"}, 400)
    ui = _user_info(user)

    # Resolve uuids in Python so each SQL param is used with ONE type (asyncpg
    # can't deduce a param that is both text and CAST(... AS uuid)).
    # document_id has a FK to ediscovery_documents -> only set for ediscovery.
    doc_id = source_id if (source_type == "ediscovery" and _is_uuid(source_id)) else None
    coll_id = _uuid_or_none(body.get("collection_id")) if source_type == "ediscovery" else None
    parent_id = _uuid_or_none(body.get("parent_id"))
    markup_set_id = _uuid_or_none(body.get("markup_set_id"))

    try:
        async with AsyncSessionLocal() as session:
            r_new = await session.execute(sa_text("""
                INSERT INTO doc_annotations (
                    tenant_id, source_type, source_id,
                    document_id, collection_id, annotation_type,
                    text_start, text_end, selected_text,
                    page_number, x, y, width, height,
                    redaction_style, redaction_label, highlight_color,
                    comment_text, parent_id, markup_set_id, path_data,
                    created_by, created_by_name, created_at
                ) VALUES (
                    :tid, :st, :sid,
                    CAST(:doc_id AS uuid), CAST(:coll_id AS uuid), :atype,
                    :text_start, :text_end, :selected_text,
                    :page_number, :x, :y, :width, :height,
                    :redaction_style, :redaction_label, :highlight_color,
                    :comment_text,
                    CAST(:parent_id AS uuid), CAST(:markup_set_id AS uuid), CAST(:path_data AS jsonb),
                    :created_by, :created_by_name, now()
                ) RETURNING id::text
            """), {
                "tid": tid, "st": source_type, "sid": source_id,
                "doc_id": doc_id, "coll_id": coll_id,
                "atype": atype,
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
                "parent_id": parent_id, "markup_set_id": markup_set_id,
                "path_data": (json.dumps(body.get("path_data")) if body.get("path_data") is not None else None),
                "created_by": ui["id"],
                "created_by_name": ui["name"],
            })
            new_id = r_new.scalar()
            await session.commit()
        return JSONResponse({"ok": True, "id": new_id, "created_by": ui["id"],
                             "created_by_name": ui["name"]})
    except Exception as e:
        logger.error("create_annotation: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PUT — update annotation
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.put("/item/{annotation_id}")
async def update_annotation(request: Request, annotation_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    body = await request.json()
    ui = _user_info(user)
    allowed = {"redaction_label", "redaction_style", "highlight_color", "comment_text", "markup_set_id"}
    updates, params = [], {"aid": annotation_id, "tid": tid}
    for key in allowed:
        if key in body:
            if key == "markup_set_id":
                updates.append("markup_set_id = CAST(:markup_set_id AS uuid)")
                params["markup_set_id"] = _uuid_or_none(body[key])
            else:
                updates.append(f"{key} = :{key}")
                params[key] = body[key]
    if not updates:
        return JSONResponse({"error": "No valid fields to update"}, 400)
    updates.append("updated_at = now()")
    updates.append("updated_by = :updated_by")
    params["updated_by"] = ui["id"]
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
# DELETE — soft delete annotation (+replies)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.delete("/item/{annotation_id}")
async def delete_annotation(request: Request, annotation_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
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
# Markup sets — reusable annotation objects (the dropdown + trial-viewer feed)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/{source_type}/{source_id}/sets")
async def list_markup_sets(
    request: Request, source_type: str, source_id: str, user=Depends(get_current_user),
):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT s.id::text, s.name, s.kind, s.scope,
                       s.embossed_document_id::text, s.embossed_path, s.page_count,
                       s.owner_user_id, s.owner_name, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM doc_annotations a
                          WHERE a.markup_set_id = s.id AND a.deleted_at IS NULL) AS annotation_count
                FROM markup_sets s
                WHERE trim(s.tenant_id::text) = trim(:tid)
                  AND s.source_type = :st AND s.source_id = :sid
                  AND s.deleted_at IS NULL
                ORDER BY s.created_at DESC
            """), {"tid": tid, "st": source_type, "sid": source_id})
            rows = [dict(row) for row in r.mappings().fetchall()]
        return JSONResponse(_serialize({"sets": rows, "total": len(rows)}))
    except Exception as e:
        logger.error("list_markup_sets: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/{source_type}/{source_id}/sets")
async def create_markup_set(
    request: Request, source_type: str, source_id: str, user=Depends(get_current_user),
):
    tid = _tenant(request)
    body = await request.json()
    name = (body.get("name") or "").strip() or "Untitled markup"
    kind = (body.get("kind") or "saved").strip()
    scope = (body.get("scope") or "shared").strip()
    ui = _user_info(user)
    attach_ids = [str(x) for x in (body.get("annotation_ids") or []) if _is_uuid(x)]
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                INSERT INTO markup_sets
                    (tenant_id, source_type, source_id, name, kind, scope,
                     owner_user_id, owner_name)
                VALUES (:tid, :st, :sid, :name, :kind, :scope, :uid, :uname)
                RETURNING id::text
            """), {"tid": tid, "st": source_type, "sid": source_id, "name": name,
                   "kind": kind, "scope": scope, "uid": ui["id"], "uname": ui["name"]})
            set_id = r.scalar()
            if attach_ids:
                await session.execute(sa_text("""
                    UPDATE doc_annotations SET markup_set_id = CAST(:sid AS uuid)
                    WHERE id = ANY(CAST(:ids AS uuid[]))
                      AND trim(tenant_id::text) = trim(:tid)
                """), {"sid": set_id, "ids": attach_ids, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True, "id": set_id})
    except Exception as e:
        logger.error("create_markup_set: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/sets/{set_id}/file")
async def stream_markup_set_file(
    request: Request, set_id: str, download: int = 0, user=Depends(get_current_user),
):
    """Stream the embossed (flattened) PDF for a markup set."""
    import os
    tid = _tenant(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT name, embossed_path
            FROM markup_sets WHERE id = CAST(:sid AS uuid)
              AND trim(tenant_id::text) = trim(:tid) AND deleted_at IS NULL
        """), {"sid": set_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row or not row["embossed_path"]:
        return JSONResponse({"error": "embossed file not found"}, 404)
    path = row["embossed_path"]
    if not os.path.isfile(path):
        return JSONResponse({"error": "file missing on disk"}, 404)
    fn = (row["name"] or "markup").replace("/", "_") + ".pdf"
    disp = "attachment" if download else "inline"
    return FileResponse(path, media_type="application/pdf", filename=fn,
                        content_disposition_type=disp)


@router.delete("/sets/{set_id}")
async def delete_markup_set(request: Request, set_id: str, user=Depends(get_current_user)):
    tid = _tenant(request)
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE markup_sets SET deleted_at = now()
                WHERE id = CAST(:sid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                  AND deleted_at IS NULL
            """), {"sid": set_id, "tid": tid})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("delete_markup_set: %s", e)
        return JSONResponse({"error": str(e)}, 500)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# POST — emboss (flatten) annotations into a baked PDF
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/{source_type}/{source_id}/emboss")
async def emboss(request: Request, source_type: str, source_id: str, user=Depends(get_current_user)):
    """Burn the visible annotations into a flattened PDF and register a markup set.

    Body: { name?, created_by? (filter to a user id), markup_set_id? }
    Returns: { ok, set_id, embossed_document_id|embossed_path }
    """
    from modules.annotations.markup_embosser import emboss_source
    tid = _tenant(request)
    body = await request.json()
    ui = _user_info(user)
    try:
        result = await emboss_source(
            tenant_id=tid, source_type=source_type, source_id=source_id,
            name=(body.get("name") or "").strip(),
            only_created_by=body.get("created_by"),
            markup_set_id=body.get("markup_set_id"),
            owner=ui,
        )
        return JSONResponse(_serialize({"ok": True, **result}))
    except FileNotFoundError as e:
        return JSONResponse({"error": str(e)}, 404)
    except Exception as e:
        logger.error("emboss: %s", e)
        return JSONResponse({"error": str(e)}, 500)
