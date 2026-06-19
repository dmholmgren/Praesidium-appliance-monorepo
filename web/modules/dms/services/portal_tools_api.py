"""
Portal Tools API — v1 (2026-06-17).

Projects the firm's matter tools (Matter Home, e-Discovery, Drafting, Projects,
Trial Center / Deal Center) to EXTERNAL portal users — primarily co_counsel, who
get FULL matter visibility. Served under /api/portal/* so the magic-link
middleware confinement permits it.

Authorization reuses portal_dms_api._access (co_counsel -> external_user_scopes
FULL; client -> sub_tenant_matter_scope folder-filtered). Once access is granted
for a matter_id (a globally-unique uuid), tool data is queried by matter_id
directly — the grant check in _access is the gate.
"""
from __future__ import annotations
import os, logging, mimetypes
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dms.services.portal_dms_api import _access, _log, _folder_visible
from modules.dms.services.matter_workspace_api import _safe_path, _list_files

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/portal/matter", tags=["portal-tools"])

# Folders that hold draft / working documents, surfaced under the Drafting tool.
DRAFT_FOLDERS = ["10-Working Docs", "08-Working Docs", "00-Mobile Drafts", "00-Mobile Scans"]


def _fmt_size(n):
    n = n or 0
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n/1:.0f} {unit}" if False else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


@router.get("/{matter_id}/home")
async def home(request: Request, matter_id: str):
    """Matter overview: meta, counts, top folders. Read-only projection."""
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        mr = await db.execute(sa_text("""
            SELECT m.matter_name, m.matter_number, m.matter_type, m.status,
                   m.practice_area, m.court, m.cause_number, m.judge, m.jurisdiction,
                   m.open_date, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE m.id = CAST(:mid AS uuid)
        """), {"mid": matter_id})
        m = mr.mappings().fetchone()
        if not m:
            raise HTTPException(404, "Matter not found")

        counts = {}
        cr = await db.execute(sa_text(
            "SELECT COUNT(*) n FROM ediscovery_collections WHERE matter_id = CAST(:mid AS uuid)"),
            {"mid": matter_id})
        counts["ediscovery_collections"] = cr.scalar() or 0
        dr = await db.execute(sa_text(
            "SELECT COUNT(*) FROM ediscovery_documents d "
            "JOIN ediscovery_collections c ON d.collection_id = c.id "
            "WHERE c.matter_id = CAST(:mid AS uuid)"), {"mid": matter_id})
        counts["ediscovery_documents"] = dr.scalar() or 0
        for tbl, key, where in (
            ("projects", "projects", "matter_id = CAST(:mid AS uuid)"),
            ("tasks", "tasks_open", "matter_id = CAST(:mid AS uuid) AND status NOT IN ('done','completed','cancelled')"),
            ("trial_exhibits", "trial_exhibits", "matter_id = CAST(:mid AS uuid)"),
        ):
            try:
                rr = await db.execute(sa_text(f"SELECT COUNT(*) n FROM {tbl} WHERE {where}"), {"mid": matter_id})
                counts[key] = rr.scalar() or 0
            except Exception:
                counts[key] = 0

    # Disk: top-level folders with file counts (visible-only for clients)
    folders, doc_count = [], 0
    root = ctx.get("root")
    if root and os.path.isdir(root):
        try:
            for e in sorted(os.scandir(root), key=lambda x: x.name.lower()):
                if e.name.startswith('.'):
                    continue
                if e.is_dir():
                    if not _folder_visible(e.name, ctx):
                        continue
                    try:
                        fc = sum(1 for f in os.scandir(e.path) if f.is_file() and not f.name.startswith('.'))
                    except Exception:
                        fc = 0
                    folders.append({"name": e.name, "file_count": fc})
                    doc_count += fc
                elif e.is_file():
                    doc_count += 1
        except Exception as ex:
            logger.warning("portal home scandir failed: %s", ex)
    counts["documents"] = doc_count

    return JSONResponse({
        "matter": {
            "name": m["matter_name"], "number": m["matter_number"],
            "type": m["matter_type"] or "litigation", "status": m["status"],
            "client_name": m["client_name"], "practice_area": m["practice_area"],
            "court": m["court"], "cause_number": m["cause_number"], "judge": m["judge"],
            "jurisdiction": m["jurisdiction"],
            "open_date": m["open_date"].isoformat() if m["open_date"] else None,
        },
        "counts": counts, "folders": folders, "role": ctx["role"],
    })


@router.get("/{matter_id}/tasks")
async def tasks(request: Request, matter_id: str):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT t.id, t.title, t.status, t.priority, t.due_date, t.task_type,
                   t.completion_pct, p.title AS project_title
            FROM tasks t LEFT JOIN projects p ON t.project_id = p.id
            WHERE t.matter_id = CAST(:mid AS uuid)
            ORDER BY (t.status NOT IN ('done','completed','cancelled')) DESC,
                     t.due_date NULLS LAST, t.created_at DESC
            LIMIT 200
        """), {"mid": matter_id})
        rows = [{"id": x["id"], "title": x["title"], "status": x["status"],
                 "priority": x["priority"], "task_type": x["task_type"],
                 "completion_pct": x["completion_pct"], "project": x["project_title"],
                 "due_date": x["due_date"].isoformat() if x["due_date"] else None}
                for x in r.mappings()]
    return JSONResponse({"tasks": rows})


@router.get("/{matter_id}/projects")
async def projects(request: Request, matter_id: str):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT p.id, p.title, p.description, p.status, p.priority, p.project_type,
                   p.template_type, p.due_date,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id
                      AND t.status IN ('done','completed')) AS task_done
            FROM projects p
            WHERE p.matter_id = CAST(:mid AS uuid)
            ORDER BY p.sort_order NULLS LAST, p.created_at DESC
        """), {"mid": matter_id})
        rows = [{"id": str(x["id"]), "title": x["title"], "description": x["description"],
                 "status": x["status"], "priority": x["priority"],
                 "type": x["project_type"] or x["template_type"],
                 "task_count": x["task_count"], "task_done": x["task_done"],
                 "due_date": x["due_date"].isoformat() if x["due_date"] else None}
                for x in r.mappings()]
    return JSONResponse({"projects": rows})


@router.get("/{matter_id}/ediscovery")
async def ediscovery(request: Request, matter_id: str):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT c.id, COALESCE(c.collection_name, c.name) AS name, c.status, c.source_party,
                   c.source_type, c.custodian, c.stated_bates_range, c.received_date,
                   COALESCE((SELECT COUNT(*) FROM ediscovery_documents d WHERE d.collection_id = c.id),
                            NULLIF(c.total_docs, 0), 0) AS doc_count,
                   COALESCE(c.reviewed_docs, 0) AS reviewed
            FROM ediscovery_collections c
            WHERE c.matter_id = CAST(:mid AS uuid) AND COALESCE(c.is_internal, FALSE) = FALSE
            ORDER BY c.received_date DESC NULLS LAST, c.created_at DESC
        """), {"mid": matter_id})
        rows = [{"id": str(x["id"]), "name": x["name"], "status": x["status"],
                 "source_party": x["source_party"], "source_type": x["source_type"],
                 "custodian": x["custodian"], "bates_range": x["stated_bates_range"],
                 "doc_count": int(x["doc_count"] or 0), "reviewed": int(x["reviewed"] or 0),
                 "received_date": x["received_date"].isoformat() if x["received_date"] else None}
                for x in r.mappings()]
    return JSONResponse({"collections": rows})


@router.get("/{matter_id}/ediscovery/{collection_id}/documents")
async def ediscovery_documents(request: Request, matter_id: str, collection_id: str,
                               q: str = "", limit: int = 100, offset: int = 0):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    limit = max(1, min(limit, 300))
    async with AsyncSessionLocal() as db:
        # verify the collection belongs to this matter
        cv = await db.execute(sa_text(
            "SELECT 1 FROM ediscovery_collections WHERE id = CAST(:c AS uuid) "
            "AND matter_id = CAST(:m AS uuid)"), {"c": collection_id, "m": matter_id})
        if not cv.fetchone():
            raise HTTPException(404, "Collection not found")
        where = "collection_id = CAST(:c AS uuid)"
        params = {"c": collection_id, "lim": limit, "off": offset}
        if q:
            where += " AND (file_name ILIKE :q OR COALESCE(email_subject,'') ILIKE :q " \
                     "OR COALESCE(title,'') ILIKE :q OR COALESCE(bates_begin,'') ILIKE :q)"
            params["q"] = f"%{q}%"
        tot = await db.execute(sa_text(f"SELECT COUNT(*) FROM ediscovery_documents WHERE {where}"), params)
        total = tot.scalar() or 0
        r = await db.execute(sa_text(f"""
            SELECT id, file_name, title, doc_type, COALESCE(bates_begin, bates_start) AS bates_begin,
                   bates_end, doc_date, page_count, custodian, file_size, mime_type,
                   email_subject, email_from, email_date, review_status,
                   rendition_path, native_path, working_path, file_path
            FROM ediscovery_documents WHERE {where}
            ORDER BY COALESCE(bates_begin, bates_start) NULLS LAST, file_name
            LIMIT :lim OFFSET :off
        """), params)
        rows = []
        for x in r.mappings():
            has_preview = bool(x["rendition_path"] or x["native_path"] or x["working_path"] or x["file_path"])
            rows.append({
                "id": str(x["id"]),
                "name": x["file_name"] or x["title"] or x["email_subject"] or "(untitled)",
                "doc_type": x["doc_type"], "bates_begin": x["bates_begin"], "bates_end": x["bates_end"],
                "doc_date": x["doc_date"].isoformat() if x["doc_date"] else None,
                "page_count": x["page_count"], "custodian": x["custodian"],
                "size_fmt": _fmt_size(x["file_size"]),
                "email_subject": x["email_subject"], "email_from": x["email_from"],
                "review_status": x["review_status"], "has_preview": has_preview,
            })
    return JSONResponse({"documents": rows, "total": total, "limit": limit, "offset": offset})


@router.get("/{matter_id}/ediscovery/doc/{doc_id}/preview")
async def ediscovery_preview(request: Request, matter_id: str, doc_id: str):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT d.rendition_path, d.native_path, d.working_path, d.file_path, d.file_name, d.mime_type
            FROM ediscovery_documents d
            JOIN ediscovery_collections c ON d.collection_id = c.id
            WHERE d.id = CAST(:d AS uuid) AND c.matter_id = CAST(:m AS uuid)
        """), {"d": doc_id, "m": matter_id})
        x = r.mappings().fetchone()
        if not x:
            raise HTTPException(404, "Document not found")
        await _log(db, ctx["tid"], ctx["uid"], "ediscovery_view", request,
                   {"matter_id": matter_id, "doc_id": doc_id})
        await db.commit()
    for col in ("rendition_path", "native_path", "working_path", "file_path"):
        fp = x[col]
        if fp and os.path.isfile(fp):
            ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''
            mime = x["mime_type"] or mimetypes.guess_type(fp)[0] or "application/octet-stream"
            safe = (x["file_name"] or os.path.basename(fp)).replace('"', '').replace("'", "")
            inline = ext in {"pdf", "jpg", "jpeg", "png", "gif", "webp", "txt", "html"}
            return FileResponse(fp, media_type=mime, headers={
                "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"',
                "X-Content-Type-Options": "nosniff"})
    raise HTTPException(404, "No previewable file on disk")


@router.get("/{matter_id}/drafting")
async def drafting(request: Request, matter_id: str):
    """Project the matter's working/draft documents (read-only)."""
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    root = ctx.get("root")
    out = []
    if root and os.path.isdir(root):
        for folder in DRAFT_FOLDERS:
            if not _folder_visible(folder, ctx):
                continue
            sub = os.path.join(root, folder)
            if not os.path.isdir(sub):
                continue
            try:
                files = _list_files(sub)
            except Exception:
                files = []
            if files:
                out.append({"folder": folder, "files": files})
    return JSONResponse({"groups": out})


@router.get("/{matter_id}/trial")
async def trial(request: Request, matter_id: str):
    """Trial Center projection (litigation): exhibits + admission status."""
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT id, party, exhibit_number, exhibit_label, sponsoring_witness,
                   status, admitted, document_source, rr_page_label
            FROM trial_exhibits
            WHERE matter_id = CAST(:mid AS uuid)
            ORDER BY party, exhibit_number
        """), {"mid": matter_id})
        exhibits = [{"id": str(x["id"]), "party": x["party"], "number": x["exhibit_number"],
                     "label": x["exhibit_label"], "witness": x["sponsoring_witness"],
                     "status": x["status"], "admitted": x["admitted"],
                     "source": x["document_source"], "rr_page": x["rr_page_label"]}
                    for x in r.mappings()]
    admitted = sum(1 for e in exhibits if e["admitted"])
    return JSONResponse({"exhibits": exhibits,
                         "summary": {"total": len(exhibits), "admitted": admitted}})


@router.get("/{matter_id}/deal")
async def deal(request: Request, matter_id: str):
    """Deal Center projection (transactional): deal meta + workstreams + key docs."""
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    async with AsyncSessionLocal() as db:
        mr = await db.execute(sa_text("""
            SELECT m.matter_name, m.matter_number, m.status, m.practice_area, m.open_date,
                   c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id
            WHERE m.id = CAST(:mid AS uuid)
        """), {"mid": matter_id})
        m = mr.mappings().fetchone()
        pr = await db.execute(sa_text("""
            SELECT p.id, p.title, p.status, p.project_type, p.template_type, p.due_date,
                   (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id) AS task_count
            FROM projects p WHERE p.matter_id = CAST(:mid AS uuid)
            ORDER BY p.sort_order NULLS LAST, p.created_at DESC
        """), {"mid": matter_id})
        workstreams = [{"id": str(x["id"]), "title": x["title"], "status": x["status"],
                        "type": x["project_type"] or x["template_type"],
                        "task_count": x["task_count"],
                        "due_date": x["due_date"].isoformat() if x["due_date"] else None}
                       for x in pr.mappings()]
    return JSONResponse({
        "deal": {"name": m["matter_name"] if m else "", "number": m["matter_number"] if m else "",
                 "status": m["status"] if m else "", "client_name": m["client_name"] if m else "",
                 "practice_area": m["practice_area"] if m else None,
                 "open_date": m["open_date"].isoformat() if m and m["open_date"] else None},
        "workstreams": workstreams,
    })
