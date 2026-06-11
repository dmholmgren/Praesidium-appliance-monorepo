"""
modules/ediscovery/routes/production_api.py
============================================
JSON API endpoints for the React eDiscovery Productions page.

Endpoints:
  GET  /api/v1/ediscovery/productions                  -- list production sets
  POST /api/v1/ediscovery/productions                  -- create production set
  GET  /api/v1/ediscovery/productions/{id}              -- production detail
  GET  /api/v1/ediscovery/productions/{id}/documents    -- docs in production
  POST /api/v1/ediscovery/productions/{id}/add-docs     -- add docs (tag filter or manual)
  POST /api/v1/ediscovery/productions/{id}/remove-doc   -- remove doc from draft
  POST /api/v1/ediscovery/productions/{id}/stage        -- assign Bates, freeze
  POST /api/v1/ediscovery/productions/{id}/unstage      -- release Bates, return to draft
  POST /api/v1/ediscovery/productions/{id}/run          -- enqueue RQ job (stamp PDFs)
  GET  /api/v1/ediscovery/productions/{id}/status       -- poll status
  POST /api/v1/ediscovery/productions/{id}/export       -- build ZIP
  GET  /api/v1/ediscovery/productions/{id}/download     -- download ZIP
  DELETE /api/v1/ediscovery/productions/{id}            -- delete (draft/error only)
  GET  /api/v1/ediscovery/bates-counters                -- list counters for matter
  POST /api/v1/ediscovery/bates-counters                -- create counter
  GET  /api/v1/ediscovery/productions/matters           -- matters with eDiscovery data
"""

import logging
import os
import secrets
import uuid as uuid_mod
import zipfile
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ediscovery", tags=["ediscovery-production-api"])

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()


def _uid(user) -> int:
    if isinstance(user, dict):
        return user.get("id", 0)
    return getattr(user, "id", 0)


def _ser(obj):
    """Make DB row dicts JSON-safe."""
    import uuid as _uuid
    from datetime import datetime as _dt, date as _d
    from decimal import Decimal
    if obj is None: return None
    if isinstance(obj, dict): return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_ser(v) for v in obj]
    if isinstance(obj, _uuid.UUID): return str(obj)
    if isinstance(obj, (_dt, _d)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    if isinstance(obj, bytes): return obj.decode("utf-8", errors="replace")
    return obj


@router.get("/productions/matters")
async def production_matters(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT DISTINCT m.id::text, m.matter_name, m.matter_number, cl.client_name
            FROM ediscovery_collections c
            JOIN matters m ON c.matter_id = m.id AND trim(c.tenant_id) = trim(m.tenant_id)
            LEFT JOIN clients cl ON m.client_id = cl.id AND trim(m.tenant_id) = trim(cl.tenant_id)
            WHERE trim(c.tenant_id) = trim(:tid)
            ORDER BY m.matter_name LIMIT 50
        """), {"tid": tid})
        return JSONResponse(_ser([dict(row) for row in r.mappings().fetchall()]))


@router.get("/productions")
async def list_productions(request: Request, matter_id: str = Query(""),
                           user=Depends(get_current_user)):
    tid = _tid(request)
    where = "trim(ps.tenant_id::text) = trim(:tid)"
    params = {"tid": tid}
    if matter_id:
        where += " AND ps.matter_id = CAST(:mid AS uuid)"
        params["mid"] = matter_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT ps.id::text, ps.name, ps.status, ps.matter_id::text,
                   ps.doc_count, ps.page_count, ps.start_bates, ps.end_bates,
                   ps.confidentiality_designation, ps.include_natives,
                   ps.created_at, ps.staged_at, ps.produced_at, ps.exported_at,
                   m.matter_name, m.matter_number
            FROM production_sets ps
            LEFT JOIN matters m ON m.id = ps.matter_id
            WHERE {where}
            ORDER BY ps.created_at DESC LIMIT 50
        """), params)
        rows = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser(rows))


@router.post("/productions")
async def create_production(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    name = (body.get("name") or "").strip()
    matter_id = body.get("matter_id", "")
    confidentiality = body.get("confidentiality_designation", "none")
    include_natives = body.get("include_natives", False)
    include_text = body.get("include_text_files", False)
    if not name or not matter_id:
        return JSONResponse({"error": "name and matter_id required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                INSERT INTO production_sets
                    (tenant_id, matter_id, name, status, confidentiality_designation,
                     include_natives, include_text_files, created_by, created_at, updated_at)
                VALUES (:tid, CAST(:mid AS uuid), :name, 'draft', :conf,
                        :nat, :txt, :uid, now(), now())
                RETURNING id::text
            """), {"tid": tid, "mid": matter_id, "name": name, "conf": confidentiality,
                   "nat": include_natives, "txt": include_text, "uid": _uid(user)})
            ps_id = r.scalar()
            await session.commit()
        return JSONResponse({"ok": True, "id": ps_id})
    except Exception as e:
        logger.error("create_production: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/productions/tags")
async def production_available_tags(request: Request, matter_id: str = Query(""),
                                    user=Depends(get_current_user)):
    tid = _tid(request)
    if not matter_id:
        return JSONResponse([])
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT DISTINCT t.id::text, t.name, t.color, t.category, t.is_system
            FROM tags t
            JOIN ediscovery_collections ec ON (t.collection_id = ec.id OR t.collection_id IS NULL)
            WHERE trim(t.tenant_id::text) = trim(:tid)
              AND ec.matter_id = CAST(:mid AS uuid)
            ORDER BY t.name
        """), {"tid": tid, "mid": matter_id})
        return JSONResponse(_ser([dict(row) for row in r.mappings().fetchall()]))


@router.get("/productions/{ps_id}")
async def get_production(request: Request, ps_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ps.*, m.matter_name, m.matter_number,
                   bc.prefix AS bates_prefix, bc.num_digits AS bates_digits,
                   bc.next_number AS bates_next
            FROM production_sets ps
            LEFT JOIN matters m ON m.id = ps.matter_id
            LEFT JOIN bates_counters bc ON bc.id = ps.bates_counter_id
            WHERE ps.id = CAST(:pid AS uuid) AND trim(ps.tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"error": "Not found"}, 404)
    return JSONResponse(_ser(dict(row)))


@router.delete("/productions/{ps_id}")
async def delete_production(request: Request, ps_id: str, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT status FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, 404)
        if row[0] not in ("draft", "error"):
            return JSONResponse({"error": "Can only delete draft or error sets"}, 400)
        await session.execute(sa_text("""
            DELETE FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        await session.commit()
    return JSONResponse({"ok": True})


@router.patch("/productions/{ps_id}")
async def update_production(request: Request, ps_id: str, user=Depends(get_current_user)):
    """Update production configuration (branding_config, confidentiality, etc.)."""
    tid = _tid(request)
    body = await request.json()
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT status FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, 404)
        if row[0] not in ("draft", "staged"):
            return JSONResponse({"error": "Can only update draft or staged sets"}, 400)
        sets = []
        params = {"pid": ps_id}
        if "branding_config" in body:
            import json
            sets.append("branding_config = CAST(:bc AS jsonb)")
            params["bc"] = json.dumps(body["branding_config"])
        if "confidentiality_designation" in body:
            sets.append("confidentiality_designation = :cd")
            params["cd"] = body["confidentiality_designation"]
        if "include_natives" in body:
            sets.append("include_natives = :nat")
            params["nat"] = body["include_natives"]
        if "include_text_files" in body:
            sets.append("include_text_files = :txt")
            params["txt"] = body["include_text_files"]
        if sets:
            sets.append("updated_at = now()")
            await session.execute(sa_text(f"""
                UPDATE production_sets SET {', '.join(sets)} WHERE id = CAST(:pid AS uuid)
            """), params)
            await session.commit()
    return JSONResponse({"ok": True})

@router.get("/productions/{ps_id}/documents")
async def production_documents(request: Request, ps_id: str,
                               page: int = Query(1), per_page: int = Query(50),
                               user=Depends(get_current_user)):
    tid = _tid(request)
    offset = (max(1, page) - 1) * min(200, max(1, per_page))
    per_page = min(200, max(1, per_page))
    async with AsyncSessionLocal() as session:
        r_total = await session.execute(sa_text("""
            SELECT COUNT(*) FROM production_documents
            WHERE production_set_id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        total = int(r_total.scalar() or 0)
        r = await session.execute(sa_text("""
            SELECT pd.id::text, pd.sort_order, pd.begin_bates, pd.end_bates,
                   pd.page_count, pd.production_type, pd.status, pd.produced_path,
                   ed.file_name, ed.email_subject, ed.doc_type, ed.custodian,
                   ed.mime_type, ed.review_status, ed.privilege_status,
                   ed.id::text AS ediscovery_document_id
            FROM production_documents pd
            JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
            WHERE pd.production_set_id = CAST(:pid AS uuid)
              AND trim(pd.tenant_id::text) = trim(:tid)
            ORDER BY pd.sort_order ASC, pd.created_at ASC
            LIMIT :lim OFFSET :off
        """), {"pid": ps_id, "tid": tid, "lim": per_page, "off": offset})
        docs = []
        for row in r.mappings().fetchall():
            d = dict(row)
            d["display_name"] = (d.get("email_subject") or d.get("file_name") or "(untitled)").replace("\\", "/").rsplit("/", 1)[-1]
            docs.append(d)
    return JSONResponse(_ser({
        "docs": docs, "total": total, "page": page, "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
    }))


@router.post("/productions/{ps_id}/add-docs")
async def add_docs_to_production(request: Request, ps_id: str,
                                 user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    mode = body.get("mode", "manual")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT status, matter_id::text FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        ps_row = r.fetchone()
        if not ps_row:
            return JSONResponse({"error": "Not found"}, 404)
        if ps_row[0] != "draft":
            return JSONResponse({"error": "Can only add docs to draft sets"}, 400)
        matter_id = ps_row[1]
    doc_ids = []
    if mode == "tag_filter":
        tag_ids = body.get("tag_ids", [])
        collection_id = body.get("collection_id", "")
        exclude_privileged = body.get("exclude_privileged", True)
        if not tag_ids:
            return JSONResponse({"error": "tag_ids required for tag_filter mode"}, 400)
        async with AsyncSessionLocal() as session:
            tag_placeholders = ", ".join(f"CAST('{t}' AS uuid)" for t in tag_ids if t)
            where = f"""
                trim(ed.tenant_id::text) = trim(:tid)
                AND ed.collection_id IN (
                    SELECT id FROM ediscovery_collections
                    WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id::text) = trim(:tid)
                )
                AND ed.id IN (
                    SELECT document_id FROM document_tags
                    WHERE tag_id IN ({tag_placeholders})
                    AND trim(tenant_id::text) = trim(:tid)
                )
                AND (ed.is_duplicate IS NOT TRUE)
            """
            params = {"tid": tid, "mid": matter_id}
            if collection_id:
                where += " AND ed.collection_id = CAST(:cid AS uuid)"
                params["cid"] = collection_id
            if exclude_privileged:
                where += " AND (ed.privilege_status IS NULL OR ed.privilege_status NOT IN ('privileged', 'attorney_work_product'))"
            r = await session.execute(sa_text(f"""
                SELECT ed.id::text FROM ediscovery_documents ed WHERE {where}
                ORDER BY COALESCE(ed.doc_date, ed.ingested_at::date) ASC, ed.file_name ASC
            """), params)
            doc_ids = [row[0] for row in r.fetchall()]
    elif mode == "manual":
        doc_ids = body.get("doc_ids", [])
    if not doc_ids:
        return JSONResponse({"ok": True, "added": 0, "message": "No documents matched"})
    added = 0
    async with AsyncSessionLocal() as session:
        r_max = await session.execute(sa_text("""
            SELECT COALESCE(MAX(sort_order), 0) FROM production_documents
            WHERE production_set_id = CAST(:pid AS uuid)
        """), {"pid": ps_id})
        sort_start = int(r_max.scalar() or 0) + 1
        for i, did in enumerate(doc_ids):
            r_ex = await session.execute(sa_text("""
                SELECT 1 FROM production_documents
                WHERE production_set_id = CAST(:pid AS uuid)
                  AND ediscovery_document_id = CAST(:did AS uuid) LIMIT 1
            """), {"pid": ps_id, "did": did})
            if r_ex.fetchone():
                continue
            await session.execute(sa_text("""
                INSERT INTO production_documents
                    (tenant_id, production_set_id, ediscovery_document_id,
                     sort_order, begin_bates, end_bates, page_count,
                     production_type, status, created_at)
                VALUES (:tid, CAST(:pid AS uuid), CAST(:did AS uuid),
                        :sort, '', '', 0, 'image', 'pending', now())
            """), {"tid": tid, "pid": ps_id, "did": did, "sort": sort_start + i})
            added += 1
        await session.execute(sa_text("""
            UPDATE production_sets SET doc_count = (
                SELECT COUNT(*) FROM production_documents
                WHERE production_set_id = CAST(:pid AS uuid)
            ), updated_at = now() WHERE id = CAST(:pid AS uuid)
        """), {"pid": ps_id})
        await session.commit()
    return JSONResponse({"ok": True, "added": added, "total_candidates": len(doc_ids)})


@router.post("/productions/{ps_id}/remove-doc")
async def remove_doc_from_production(request: Request, ps_id: str,
                                     user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    pd_id = body.get("production_document_id", "")
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ps.status FROM production_sets ps
            WHERE ps.id = CAST(:pid AS uuid) AND trim(ps.tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.fetchone()
        if not row or row[0] != "draft":
            return JSONResponse({"error": "Can only remove from draft sets"}, 400)
        await session.execute(sa_text("""
            DELETE FROM production_documents
            WHERE id = CAST(:pdid AS uuid) AND production_set_id = CAST(:pid AS uuid)
        """), {"pdid": pd_id, "pid": ps_id})
        await session.execute(sa_text("""
            UPDATE production_sets SET doc_count = (
                SELECT COUNT(*) FROM production_documents WHERE production_set_id = CAST(:pid AS uuid)
            ), updated_at = now() WHERE id = CAST(:pid AS uuid)
        """), {"pid": ps_id})
        await session.commit()
    return JSONResponse({"ok": True})


@router.get("/bates-counters")
async def list_bates_counters(request: Request, matter_id: str = Query(""),
                              user=Depends(get_current_user)):
    tid = _tid(request)
    where = "trim(tenant_id::text) = trim(:tid)"
    params = {"tid": tid}
    if matter_id:
        where += " AND matter_id = CAST(:mid AS uuid)"
        params["mid"] = matter_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(f"""
            SELECT id::text, matter_id::text, prefix, next_number, num_digits,
                   suffix, created_at, updated_at
            FROM bates_counters WHERE {where} ORDER BY prefix
        """), params)
        return JSONResponse(_ser([dict(row) for row in r.mappings().fetchall()]))


@router.post("/bates-counters")
async def create_bates_counter(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    matter_id = body.get("matter_id", "")
    prefix = (body.get("prefix") or "").strip()
    num_digits = int(body.get("num_digits", 7))
    if not matter_id or not prefix:
        return JSONResponse({"error": "matter_id and prefix required"}, 400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT 1 FROM bates_counters
                WHERE trim(tenant_id::text) = trim(:tid)
                  AND matter_id = CAST(:mid AS uuid) AND prefix = :pfx LIMIT 1
            """), {"tid": tid, "mid": matter_id, "pfx": prefix})
            if r.fetchone():
                return JSONResponse({"error": f"Counter with prefix '{prefix}' already exists"}, 409)
            r_new = await session.execute(sa_text("""
                INSERT INTO bates_counters (tenant_id, matter_id, prefix, next_number, num_digits, created_at, updated_at)
                VALUES (:tid, CAST(:mid AS uuid), :pfx, 1, :nd, now(), now()) RETURNING id::text
            """), {"tid": tid, "mid": matter_id, "pfx": prefix, "nd": num_digits})
            cid = r_new.scalar()
            await session.commit()
        return JSONResponse({"ok": True, "id": cid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, 500)


@router.post("/productions/{ps_id}/stage")
async def stage_production(request: Request, ps_id: str,
                           user=Depends(get_current_user)):
    tid = _tid(request)
    body = await request.json()
    counter_id = body.get("bates_counter_id", "")
    if not counter_id:
        return JSONResponse({"error": "bates_counter_id required"}, 400)
    from modules.ediscovery.services.bates_engine import (
        allocate_range_async, format_bates, get_page_count,
    )
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT status, matter_id::text FROM production_sets
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid})
            ps_row = r.fetchone()
            if not ps_row or ps_row[0] != "draft":
                return JSONResponse({"error": "Can only stage draft sets"}, 400)
            r_ctr = await session.execute(sa_text("""
                SELECT prefix, num_digits, suffix FROM bates_counters
                WHERE id = CAST(:cid AS uuid) FOR UPDATE
            """), {"cid": counter_id})
            ctr = r_ctr.mappings().fetchone()
            if not ctr:
                return JSONResponse({"error": "Bates counter not found"}, 404)
            r_docs = await session.execute(sa_text("""
                SELECT pd.id::text AS pd_id, pd.ediscovery_document_id::text AS ed_id,
                       ed.file_path, ed.mime_type, ed.page_count AS ed_page_count,
                       ec.storage_path AS col_storage
                FROM production_documents pd
                JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
                LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                WHERE pd.production_set_id = CAST(:pid AS uuid)
                ORDER BY pd.sort_order
            """), {"pid": ps_id})
            docs = [dict(row) for row in r_docs.mappings().fetchall()]
            if not docs:
                return JSONResponse({"error": "No documents in production set"}, 400)
            total_pages = 0
            doc_pages = []
            for d in docs:
                fp = d.get("file_path") or ""
                col = d.get("col_storage") or ""
                full_path = str(Path(col) / fp) if fp and col else ""
                mime = (d.get("mime_type") or "").lower()
                if mime == "application/pdf" and full_path and Path(full_path).exists():
                    pc = get_page_count(full_path)
                else:
                    pc = int(d.get("ed_page_count") or 1) or 1
                doc_pages.append(pc)
                total_pages += pc
            start_num, end_num = await allocate_range_async(session, counter_id, total_pages)
            current = start_num
            prefix = ctr["prefix"]
            digits = ctr["num_digits"]
            suffix = ctr["suffix"] or ""
            first_bates = format_bates(prefix, start_num, digits, suffix)
            last_bates = format_bates(prefix, end_num, digits, suffix)
            for i, d in enumerate(docs):
                pc = doc_pages[i]
                begin = format_bates(prefix, current, digits, suffix)
                end = format_bates(prefix, current + pc - 1, digits, suffix)
                await session.execute(sa_text("""
                    UPDATE production_documents
                    SET begin_bates = :bb, end_bates = :eb, page_count = :pc, status = 'staged'
                    WHERE id = CAST(:pdid AS uuid)
                """), {"bb": begin, "eb": end, "pc": pc, "pdid": d["pd_id"]})
                current += pc
            await session.execute(sa_text("""
                UPDATE production_sets
                SET status = 'staged', bates_counter_id = CAST(:cid AS uuid),
                    start_bates = :sb, end_bates = :eb,
                    doc_count = :dc, page_count = :pc,
                    staged_at = now(), updated_at = now()
                WHERE id = CAST(:pid AS uuid)
            """), {"cid": counter_id, "sb": first_bates, "eb": last_bates,
                   "dc": len(docs), "pc": total_pages, "pid": ps_id})
            await session.execute(sa_text("""
                INSERT INTO production_audit_log
                    (tenant_id, production_set_id, action, performed_by, details_json, created_at)
                VALUES (:tid, CAST(:pid AS uuid), 'staged', :uid, CAST(:det AS jsonb), now())
            """), {"tid": tid, "pid": ps_id, "uid": _uid(user),
                   "det": '{"doc_count": ' + str(len(docs)) + ', "page_count": ' + str(total_pages) + ', "start_bates": "' + first_bates + '", "end_bates": "' + last_bates + '"}'})
            await session.commit()
        return JSONResponse({"ok": True, "start_bates": first_bates, "end_bates": last_bates,
                             "doc_count": len(docs), "page_count": total_pages})
    except Exception as e:
        logger.exception("stage_production: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/productions/{ps_id}/unstage")
async def unstage_production(request: Request, ps_id: str,
                             user=Depends(get_current_user)):
    tid = _tid(request)
    from modules.ediscovery.services.bates_engine import release_range_async
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT status, bates_counter_id::text, page_count
                FROM production_sets
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid})
            row = r.fetchone()
            if not row or row[0] != "staged":
                return JSONResponse({"error": "Can only unstage staged sets"}, 400)
            counter_id, pages = row[1], row[2] or 0
            if counter_id and pages:
                await release_range_async(session, counter_id, pages)
            await session.execute(sa_text("""
                UPDATE production_documents SET begin_bates = '', end_bates = '',
                page_count = 0, status = 'pending'
                WHERE production_set_id = CAST(:pid AS uuid)
            """), {"pid": ps_id})
            await session.execute(sa_text("""
                UPDATE production_sets SET status = 'draft', bates_counter_id = NULL,
                start_bates = NULL, end_bates = NULL, page_count = NULL,
                staged_at = NULL, updated_at = now()
                WHERE id = CAST(:pid AS uuid)
            """), {"pid": ps_id})
            await session.execute(sa_text("""
                INSERT INTO production_audit_log
                    (tenant_id, production_set_id, action, performed_by, details_json, created_at)
                VALUES (:tid, CAST(:pid AS uuid), 'unstaged', :uid, NULL, now())
            """), {"tid": tid, "pid": ps_id, "uid": _uid(user)})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("unstage_production: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.post("/productions/{ps_id}/run")
async def run_production(request: Request, ps_id: str,
                         user=Depends(get_current_user)):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT status FROM production_sets
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid})
            row = r.fetchone()
            if not row or row[0] != "staged":
                return JSONResponse({"error": "Can only run staged sets"}, 400)
            await session.execute(sa_text("""
                UPDATE production_sets SET status = 'running', updated_at = now()
                WHERE id = CAST(:pid AS uuid)
            """), {"pid": ps_id})
            await session.execute(sa_text("""
                INSERT INTO production_audit_log
                    (tenant_id, production_set_id, action, performed_by, details_json, created_at)
                VALUES (:tid, CAST(:pid AS uuid), 'run_started', :uid, NULL, now())
            """), {"tid": tid, "pid": ps_id, "uid": _uid(user)})
            await session.commit()
        try:
            import redis as _redis
            from rq import Queue
            redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
            conn = _redis.Redis.from_url(redis_url)
            q = Queue("ediscovery", connection=conn)
            job = q.enqueue("jobs.run_production.run", ps_id, tid,
                            job_timeout=7200, result_ttl=86400)
            async with AsyncSessionLocal() as session:
                await session.execute(sa_text("""
                    UPDATE production_sets SET rq_job_id = :jid WHERE id = CAST(:pid AS uuid)
                """), {"jid": job.id, "pid": ps_id})
                await session.commit()
        except Exception as rq_err:
            logger.error("RQ enqueue failed, running inline: %s", rq_err)
            from jobs.run_production import run as run_inline
            run_inline(ps_id, tid)
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.exception("run_production: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/productions/{ps_id}/status")
async def production_status(request: Request, ps_id: str,
                            user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT ps.status, ps.doc_count, ps.page_count, ps.error_message,
                   ps.start_bates, ps.end_bates, ps.produced_at, ps.exported_at,
                   (SELECT COUNT(*) FROM production_documents
                    WHERE production_set_id = ps.id AND status = 'completed') AS completed_docs,
                   (SELECT COUNT(*) FROM production_documents
                    WHERE production_set_id = ps.id AND status = 'error') AS error_docs
            FROM production_sets ps
            WHERE ps.id = CAST(:pid AS uuid) AND trim(ps.tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row:
        return JSONResponse({"error": "Not found"}, 404)
    return JSONResponse(_ser(dict(row)))


@router.post("/productions/{ps_id}/export")
async def export_production(request: Request, ps_id: str,
                            user=Depends(get_current_user)):
    tid = _tid(request)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT status, name FROM production_sets
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid})
            row = r.fetchone()
            if not row or row[0] not in ("completed",):
                return JSONResponse({"error": "Can only export completed sets"}, 400)
            ps_name = row[1]
            r_docs = await session.execute(sa_text("""
                SELECT pd.begin_bates, pd.end_bates, pd.produced_path, pd.production_type,
                       ed.file_name, ed.file_path AS original_path,
                       ec.storage_path AS col_storage
                FROM production_documents pd
                JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
                LEFT JOIN ediscovery_collections ec ON ec.id = ed.collection_id
                WHERE pd.production_set_id = CAST(:pid AS uuid) AND pd.status = 'completed'
                ORDER BY pd.sort_order
            """), {"pid": ps_id})
            docs = [dict(row) for row in r_docs.mappings().fetchall()]
        export_dir = Path(EDISCOVERY_ROOT) / tid.strip() / "_productions" / ps_id
        export_dir.mkdir(parents=True, exist_ok=True)
        zip_path = export_dir / f"{ps_name.replace(' ', '_')}.zip"
        with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
            for d in docs:
                produced = d.get("produced_path") or ""
                if produced and Path(produced).exists():
                    zf.write(produced, f"IMAGES/{Path(produced).name}")
            # Include TEXT/, NATIVES/, and load files
            prod_root = Path(EDISCOVERY_ROOT) / tid.strip() / "_productions" / ps_id
            for subdir in ["TEXT", "NATIVES"]:
                sub_path = prod_root / subdir
                if sub_path.exists():
                    for f in sorted(sub_path.iterdir()):
                        if f.is_file():
                            zf.write(str(f), f"{subdir}/{f.name}")
            for lf_name in ["load_file.dat", "load_file.opt"]:
                lf_path = prod_root / lf_name
                if lf_path.exists():
                    zf.write(str(lf_path), lf_name)
            # Include TEXT/, NATIVES/, and load files
            prod_root = Path(EDISCOVERY_ROOT) / tid.strip() / "_productions" / ps_id
            for subdir in ["TEXT", "NATIVES"]:
                sub_path = prod_root / subdir
                if sub_path.exists():
                    for f in sorted(sub_path.iterdir()):
                        if f.is_file():
                            zf.write(str(f), f"{subdir}/{f.name}")
            for lf_name in ["load_file.dat", "load_file.opt"]:
                lf_path = prod_root / lf_name
                if lf_path.exists():
                    zf.write(str(lf_path), lf_name)
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE production_sets SET status = 'exported', export_path = :ep,
                exported_at = now(), updated_at = now() WHERE id = CAST(:pid AS uuid)
            """), {"pid": ps_id, "ep": str(zip_path)})
            await session.execute(sa_text("""
                INSERT INTO production_audit_log
                    (tenant_id, production_set_id, action, performed_by, details_json, created_at)
                VALUES (:tid, CAST(:pid AS uuid), 'exported', :uid, CAST(:det AS jsonb), now())
            """), {"tid": tid, "pid": ps_id, "uid": _uid(user),
                   "det": '{"zip_path": "' + str(zip_path) + '", "doc_count": ' + str(len(docs)) + '}'})
            await session.commit()
        return JSONResponse({"ok": True, "zip_path": str(zip_path)})
    except Exception as e:
        logger.exception("export_production: %s", e)
        return JSONResponse({"error": str(e)}, 500)


@router.get("/productions/{ps_id}/download")
async def download_production(request: Request, ps_id: str,
                              user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT export_path, name FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.fetchone()
    if not row or not row[0]:
        return JSONResponse({"error": "Export not found"}, 404)
    ep = Path(row[0])
    if not ep.exists():
        return JSONResponse({"error": "Export file missing"}, 404)
    return FileResponse(str(ep), filename=f"{row[1]}.zip", media_type="application/zip")


# ── Production Status Detail (for status page) ──


@router.post("/productions/{ps_id}/share")
async def create_share_link(request: Request, ps_id: str,
                            user=Depends(get_current_user)):
    """Create a share link for a completed production."""
    tid = _tid(request)
    body = await request.json()
    recipient_email = (body.get("recipient_email") or "").strip()
    recipient_name = (body.get("recipient_name") or "").strip()
    message = (body.get("message") or "").strip()
    expires_days = int(body.get("expires_days", 30))
    require_reg = body.get("require_registration", True)

    if not recipient_email:
        return JSONResponse({"error": "recipient_email required"}, 400)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT status, export_path FROM production_sets
            WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"pid": ps_id, "tid": tid})
        row = r.fetchone()
        if not row:
            return JSONResponse({"error": "Not found"}, 404)
        if row[0] not in ("completed", "exported"):
            return JSONResponse({"error": "Production must be completed or exported first"}, 400)

        token = secrets.token_urlsafe(32)
        expires_at = None
        if expires_days and expires_days > 0:
            from datetime import timedelta
            expires_at = datetime.utcnow() + timedelta(days=expires_days)

        await session.execute(sa_text("""
            INSERT INTO production_share_links
                (tenant_id, production_set_id, token, recipient_name, recipient_email,
                 message, require_registration, expires_at, created_by, created_at, updated_at)
            VALUES (:tid, CAST(:pid AS uuid), :tok, :rn, :re, :msg, :rr, :exp, :uid, now(), now())
        """), {"tid": tid, "pid": ps_id, "tok": token, "rn": recipient_name,
               "re": recipient_email, "msg": message, "rr": require_reg,
               "exp": expires_at, "uid": _uid(user)})

        await session.execute(sa_text("""
            INSERT INTO production_audit_log
                (tenant_id, production_set_id, action, performed_by, details_json, created_at)
            VALUES (:tid, CAST(:pid AS uuid), 'share_created', :uid,
                    CAST(:det AS jsonb), now())
        """), {"tid": tid, "pid": ps_id, "uid": _uid(user),
               "det": '{"recipient_email": "' + recipient_email + '", "token": "' + token[:8] + '..."}'})

        await session.commit()

    base_url = str(request.base_url).rstrip("/")
    share_url = f"{base_url}/share/{token}"

    # Send email notification via Stalwart SMTP
    email_sent = False
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        sender = "noreply@hjmmlegal.com"
        msg = MIMEMultipart("alternative")
        msg["From"] = "Praesidium <noreply@hjmmlegal.com>"
        msg["To"] = recipient_email
        msg["Subject"] = f"Document Production: {row[0] if row else 'Production'}"
        if recipient_name:
            msg["To"] = f"{recipient_name} <{recipient_email}>"

        text_body = f"""You have received a secure document production.

{message if message else ''}

Access your documents here:
{share_url}

You will be asked to verify your identity before downloading.

— Praesidium Secure Document Delivery
"""

        html_body = f"""<div style="font-family:-apple-system,sans-serif;max-width:600px;margin:0 auto">
<div style="background:linear-gradient(135deg,#0f2b5b,#1a3f7a);padding:24px 32px;border-radius:10px 10px 0 0;color:#fff">
<h2 style="margin:0 0 4px;font-size:18px">Secure Document Production</h2>
<p style="margin:0;font-size:13px;opacity:.8">HJMM Legal</p>
</div>
<div style="background:#fff;padding:28px 32px;border:1px solid #e2e8f0;border-top:none">
{f'<p style="color:#475569;font-size:14px;line-height:1.6;margin:0 0 20px">{message}</p>' if message else ''}
<a href="{share_url}" style="display:inline-block;padding:12px 28px;background:linear-gradient(135deg,#059669,#047857);color:#fff;text-decoration:none;border-radius:8px;font-weight:600;font-size:14px">Access Documents</a>
<p style="color:#94a3b8;font-size:12px;margin:20px 0 0;line-height:1.5">You will be asked to verify your identity before downloading. All access is tracked for chain-of-custody compliance.</p>
</div>
<div style="padding:16px 32px;text-align:center;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 10px 10px;background:#f8fafc">
<p style="margin:0;font-size:11px;color:#94a3b8">Delivered securely via Praesidium</p>
</div>
</div>"""

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP("10.10.0.10", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login("admin@hjmmlegal.com", "pAcpuyfNgssgssGu")
            smtp.send_message(msg)
        email_sent = True
        logger.info("Share link email sent to %s", recipient_email)
    except Exception as mail_err:
        logger.warning("Share link email failed: %s", mail_err)

    return JSONResponse({
        "ok": True,
        "share_url": share_url,
        "token": token,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "email_sent": email_sent,
    })


@router.get("/productions/{ps_id}/shares")
async def list_share_links(request: Request, ps_id: str,
                           user=Depends(get_current_user)):
    """List share links for a production."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, token, recipient_name, recipient_email,
                   is_revoked, download_count, expires_at, created_at
            FROM production_share_links
            WHERE production_set_id = CAST(:pid AS uuid)
              AND trim(tenant_id::text) = trim(:tid)
            ORDER BY created_at DESC
        """), {"pid": ps_id, "tid": tid})
        rows = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser(rows))


@router.get("/productions/{ps_id}/share-activity")
async def share_activity(request: Request, ps_id: str,
                         user=Depends(get_current_user)):
    """Access log for all share links on a production."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT al.action, al.visitor_name, al.visitor_email, al.visitor_firm,
                   al.ip_address, al.accessed_at,
                   sl.recipient_email AS link_recipient
            FROM production_share_access_log al
            JOIN production_share_links sl ON sl.id = al.share_link_id
            WHERE sl.production_set_id = CAST(:pid AS uuid)
              AND trim(al.tenant_id::text) = trim(:tid)
            ORDER BY al.accessed_at DESC
            LIMIT 100
        """), {"pid": ps_id, "tid": tid})
        rows = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser(rows))


@router.post("/productions/shares/{link_id}/revoke")
async def revoke_share_link(request: Request, link_id: str,
                            user=Depends(get_current_user)):
    """Revoke a share link."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE production_share_links
            SET is_revoked = true, updated_at = now()
            WHERE id = CAST(:lid AS uuid) AND trim(tenant_id::text) = trim(:tid)
        """), {"lid": link_id, "tid": tid})
        await session.commit()
    return JSONResponse({"ok": True})


@router.get("/productions/{ps_id}/status-detail")
async def production_status_detail(request: Request, ps_id: str,
                                   user=Depends(get_current_user)):
    """Full production status detail: audit trail, errors, document status breakdown."""
    tid = _tid(request)
    result = {}
    async with AsyncSessionLocal() as session:
        # Audit log
        r_audit = await session.execute(sa_text("""
            SELECT action, performed_by, details_json, created_at
            FROM production_audit_log
            WHERE production_set_id = CAST(:pid AS uuid)
              AND trim(tenant_id::text) = trim(:tid)
            ORDER BY created_at ASC
        """), {"pid": ps_id, "tid": tid})
        result["audit_log"] = [dict(row) for row in r_audit.mappings().fetchall()]

        # Error documents
        r_err = await session.execute(sa_text("""
            SELECT pd.id::text, pd.begin_bates, pd.end_bates, pd.status,
                   pd.error_message, ed.file_name, ed.email_subject
            FROM production_documents pd
            JOIN ediscovery_documents ed ON ed.id = pd.ediscovery_document_id
            WHERE pd.production_set_id = CAST(:pid AS uuid)
              AND trim(pd.tenant_id::text) = trim(:tid)
              AND pd.status = 'error'
            ORDER BY pd.sort_order
        """), {"pid": ps_id, "tid": tid})
        errs = []
        for row in r_err.mappings().fetchall():
            d = dict(row)
            d["display_name"] = (d.get("email_subject") or d.get("file_name") or "(untitled)").replace("\\", "/").rsplit("/", 1)[-1]
            errs.append(d)
        result["errors"] = errs

        # Document status breakdown
        r_break = await session.execute(sa_text("""
            SELECT status, COUNT(*) AS cnt
            FROM production_documents
            WHERE production_set_id = CAST(:pid AS uuid)
              AND trim(tenant_id::text) = trim(:tid)
            GROUP BY status
            ORDER BY cnt DESC
        """), {"pid": ps_id, "tid": tid})
        result["doc_status_breakdown"] = [dict(row) for row in r_break.mappings().fetchall()]

    return JSONResponse(_ser(result))


@router.post("/productions/{ps_id}/notes")
async def save_production_notes(request: Request, ps_id: str,
                                user=Depends(get_current_user)):
    """Save notes to production_sets.branding_config.notes."""
    tid = _tid(request)
    body = await request.json()
    notes_text = body.get("notes", "")
    import json
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT branding_config FROM production_sets
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid})
            row = r.fetchone()
            if not row:
                return JSONResponse({"error": "Not found"}, 404)
            existing = row[0] or {}
            if isinstance(existing, str):
                try:
                    existing = json.loads(existing)
                except Exception:
                    existing = {}
            existing["notes"] = notes_text
            await session.execute(sa_text("""
                UPDATE production_sets
                SET branding_config = CAST(:bc AS jsonb), updated_at = now()
                WHERE id = CAST(:pid AS uuid) AND trim(tenant_id::text) = trim(:tid)
            """), {"pid": ps_id, "tid": tid, "bc": json.dumps(existing)})
            await session.commit()
        return JSONResponse({"ok": True})
    except Exception as e:
        logger.error("save_production_notes: %s", e)
        return JSONResponse({"error": str(e)}, 500)