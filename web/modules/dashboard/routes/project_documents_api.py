"""
modules/dashboard/routes/project_documents_api.py — Project Document Assembly API

Endpoints for managing documents within a project workspace:
  GET    /api/projects/{pid}/documents          list project documents (grouped by role)
  POST   /api/projects/{pid}/documents          add document(s) to project
  DELETE /api/projects/{pid}/documents/{doc_id}  remove document from project
  PUT    /api/projects/{pid}/documents/reorder   reorder documents within a role
  PUT    /api/projects/{pid}/documents/{doc_id}/label    set exhibit label
  POST   /api/projects/{pid}/exhibits/auto-label         auto-assign exhibit labels
  POST   /api/projects/{pid}/exhibits/emboss-bates       Bates-emboss exhibit PDFs

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.project_documents_api")

router = APIRouter(prefix="/api/projects/{project_id}/documents", tags=["project-documents"])
exhibit_router = APIRouter(prefix="/api/projects/{project_id}/exhibits", tags=["project-exhibits"])


def _tid(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()


def _user(request: Request):
    return getattr(request.state, "current_user", None)


# ═══ LIST ═══

@router.get("", response_class=JSONResponse)
async def list_project_documents(request: Request, project_id: str):
    tid = _tid(request)

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT pd.id::text, pd.document_id::text, pd.storage_path,
                   pd.filename, pd.mime_type, pd.file_size, pd.role,
                   pd.sort_order, pd.exhibit_label, pd.exhibit_number,
                   pd.bates_start, pd.bates_end, pd.bates_embossed,
                   pd.page_count, pd.notes, pd.created_at,
                   pd.parent_id::text,
                   d.matter_id::text as source_matter_id
            FROM project_documents pd
            LEFT JOIN documents d ON pd.document_id = d.id
            WHERE pd.project_id = CAST(:pid AS uuid)
              AND TRIM(pd.tenant_id) = :tid
            ORDER BY pd.role, pd.sort_order, pd.created_at
        """), {"pid": project_id, "tid": tid})).fetchall()

    docs = {}
    for r in rows:
        role = r.role or "exhibit"
        if role not in docs:
            docs[role] = []
        docs[role].append({
            "id": r.id, "document_id": r.document_id,
            "storage_path": r.storage_path, "filename": r.filename,
            "mime_type": r.mime_type, "file_size": r.file_size,
            "role": role, "sort_order": r.sort_order,
            "exhibit_label": r.exhibit_label, "exhibit_number": r.exhibit_number,
            "bates_start": r.bates_start, "bates_end": r.bates_end,
            "bates_embossed": r.bates_embossed or False,
            "page_count": r.page_count, "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "source_matter_id": r.source_matter_id,
            "parent_id": getattr(r, "parent_id", None),
        })

    return {"documents": docs, "total": sum(len(v) for v in docs.values())}


# ═══ ADD DOCUMENTS ═══

@router.post("", response_class=JSONResponse)
async def add_documents(request: Request, project_id: str):
    tid = _tid(request)
    user = _user(request)
    body = await request.json()
    items = body.get("items", [])
    if not items:
        raise HTTPException(400, "items is required")

    added = []
    async with AsyncSessionLocal() as db:
        for item in items:
            role = item.get("role", "exhibit")
            filename = item.get("filename", "")
            document_id = item.get("document_id")
            storage_path = item.get("storage_path")

            if not filename and storage_path:
                filename = os.path.basename(storage_path)
            if not filename:
                continue

            max_row = (await db.execute(sa_text("""
                SELECT COALESCE(MAX(sort_order), -1) + 1 as next_order
                FROM project_documents
                WHERE project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid AND role = :role
            """), {"pid": project_id, "tid": tid, "role": role})).fetchone()
            next_order = max_row.next_order if max_row else 0

            mime_type = item.get("mime_type")
            file_size = item.get("file_size")
            page_count = item.get("page_count")
            if document_id and not storage_path:
                doc_row = (await db.execute(sa_text("""
                    SELECT storage_path, mime_type, file_size, page_count
                    FROM documents WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = :tid
                """), {"did": document_id, "tid": tid})).fetchone()
                if doc_row:
                    storage_path = doc_row.storage_path
                    mime_type = mime_type or doc_row.mime_type
                    file_size = file_size or doc_row.file_size
                    page_count = page_count or doc_row.page_count

            result = (await db.execute(sa_text("""
                INSERT INTO project_documents
                    (tenant_id, project_id, document_id, storage_path, filename,
                     mime_type, file_size, role, sort_order, page_count, added_by,
                     created_at, updated_at)
                VALUES
                    (:tid, CAST(:pid AS uuid), CAST(:did AS uuid), :spath, :fname,
                     :mime, :fsize, :role, :sort, :pages, :added_by,
                     NOW(), NOW())
                ON CONFLICT (project_id, document_id)
                    WHERE document_id IS NOT NULL
                DO UPDATE SET
                    role = EXCLUDED.role, updated_at = NOW()
                RETURNING id::text
            """), {
                "tid": tid, "pid": project_id, "did": document_id,
                "spath": storage_path, "fname": filename,
                "mime": mime_type, "fsize": file_size, "role": role,
                "sort": next_order, "pages": page_count,
                "added_by": user.id if user else None,
            })).fetchone()
            added.append({"id": result.id, "filename": filename, "role": role})

        await db.commit()

    return {"added": added, "count": len(added)}


# ═══ REMOVE ═══

@router.delete("/{doc_id}", response_class=JSONResponse)
async def remove_document(request: Request, project_id: str, doc_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            DELETE FROM project_documents
            WHERE id = CAST(:did AS uuid) AND project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"did": doc_id, "pid": project_id, "tid": tid})
        await db.commit()
    return {"removed": doc_id}


# ═══ REORDER ═══

@router.put("/reorder", response_class=JSONResponse)
async def reorder_documents(request: Request, project_id: str):
    tid = _tid(request)
    body = await request.json()
    items = body.get("items", [])
    async with AsyncSessionLocal() as db:
        for item in items:
            await db.execute(sa_text("""
                UPDATE project_documents SET sort_order = :sort, updated_at = NOW()
                WHERE id = CAST(:did AS uuid) AND project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"sort": item["sort_order"], "did": item["id"], "pid": project_id, "tid": tid})
        await db.commit()
    return {"reordered": len(items)}


# ═══ SET EXHIBIT LABEL ═══

@router.put("/{doc_id}/label", response_class=JSONResponse)
async def set_exhibit_label(request: Request, project_id: str, doc_id: str):
    tid = _tid(request)
    body = await request.json()
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE project_documents
            SET exhibit_label = :label, exhibit_number = :num, updated_at = NOW()
            WHERE id = CAST(:did AS uuid) AND project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
        """), {
            "label": body.get("exhibit_label"),
            "num": body.get("exhibit_number"),
            "did": doc_id, "pid": project_id, "tid": tid,
        })
        await db.commit()
    return {"id": doc_id, "exhibit_label": body.get("exhibit_label")}


# ═══ AUTO-LABEL EXHIBITS ═══

@exhibit_router.post("/auto-label", response_class=JSONResponse)
async def auto_label_exhibits(request: Request, project_id: str):
    """Auto-label exhibits. Styles: alpha (A,B,C), numeric (1,2,3), legal (A, A-1, A-2)."""
    tid = _tid(request)
    body = await request.json()
    style = body.get("style", "alpha")
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT id::text, sort_order, parent_id::text
            FROM project_documents
            WHERE project_id = :pid::uuid AND TRIM(tenant_id) = :tid AND role = 'exhibit'
            ORDER BY sort_order, created_at
        """), {"pid": project_id, "tid": tid})).fetchall()
        top_level = [r for r in rows if not r.parent_id]
        kids_map = {}
        for r in rows:
            if r.parent_id:
                kids_map.setdefault(r.parent_id, []).append(r)
        num = 1
        for i, r in enumerate(top_level):
            plabel = _alpha_label(i) if style in ("alpha", "legal") else str(i + 1)
            await db.execute(sa_text("""
                UPDATE project_documents SET exhibit_label = :label, exhibit_number = :num, updated_at = NOW()
                WHERE id = :did::uuid
            """), {"label": f"Exhibit {plabel}", "num": num, "did": r.id})
            num += 1
            for j, kid in enumerate(kids_map.get(r.id, [])):
                clabel = f"Exhibit {plabel}-{j+1}"
                await db.execute(sa_text("""
                    UPDATE project_documents SET exhibit_label = :label, exhibit_number = :num, updated_at = NOW()
                    WHERE id = :did::uuid
                """), {"label": clabel, "num": num, "did": kid.id})
                num += 1
        await db.commit()
    return {"labeled": len(rows), "style": style}


def _alpha_label(index: int) -> str:
    result = ""
    n = index
    while True:
        result = chr(65 + n % 26) + result
        n = n // 26 - 1
        if n < 0:
            break
    return result



# ═══ INDENT / OUTDENT (nesting) ═══

@exhibit_router.post("/indent", response_class=JSONResponse)
async def indent_exhibit(request: Request, project_id: str):
    """Set parent_id to nest exhibit under another. Body: {"exhibit_id": "uuid", "parent_id": "uuid"|null}"""
    tid = _tid(request)
    body = await request.json()
    exhibit_id = body.get("exhibit_id")
    parent_id = body.get("parent_id")
    if not exhibit_id:
        raise HTTPException(400, "exhibit_id required")
    async with AsyncSessionLocal() as db:
        if parent_id:
            await db.execute(sa_text("""
                UPDATE project_documents SET parent_id = :pid::uuid, updated_at = NOW()
                WHERE id = :eid::uuid AND project_id = :proj::uuid AND TRIM(tenant_id) = :tid
            """), {"pid": parent_id, "eid": exhibit_id, "proj": project_id, "tid": tid})
        else:
            await db.execute(sa_text("""
                UPDATE project_documents SET parent_id = NULL, updated_at = NOW()
                WHERE id = :eid::uuid AND project_id = :proj::uuid AND TRIM(tenant_id) = :tid
            """), {"eid": exhibit_id, "proj": project_id, "tid": tid})
        await db.commit()
    return {"id": exhibit_id, "parent_id": parent_id}


# ═══ STICKER EMBOSSING ═══

def _resolve_exhibit_disk_path(storage_path, tenant_id):
    """Resolve an exhibit storage_path to an on-disk file, trying common layouts."""
    if not storage_path:
        return None
    candidates = [storage_path]
    if storage_path.startswith("praesidium/"):
        candidates.append("/mnt/" + storage_path)
        candidates.append("/mnt/praesidium/" + tenant_id.strip() + "/" + storage_path[len("praesidium/"):])
    if not storage_path.startswith("/mnt"):
        candidates.append("/mnt/praesidium/" + tenant_id.strip() + "/" + storage_path.lstrip("/"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


@exhibit_router.post("/emboss-all", response_class=JSONResponse)
async def emboss_all_exhibit_stickers(request: Request, project_id: str):
    """Stamp exhibit stickers on page 1 of every un-embossed exhibit PDF.

    Body: {color, style, position, case_info?, margin?}
    Colors: transparent (default), white, blue.  Styles: 2line, 3line.
    """
    tid = _tid(request)
    try:
        body = await request.json()
    except Exception:
        body = {}

    try:
        from modules.ediscovery.services.exhibit_sticker_engine import emboss_exhibit_sticker as _engine_emboss
    except Exception as ex:
        raise HTTPException(500, {"error": "sticker_engine_unavailable", "detail": str(ex)})

    async with AsyncSessionLocal() as db:
        proj = (await db.execute(sa_text("""
            SELECT p.config, m.matter_name, m.matter_number
            FROM projects p LEFT JOIN matters m ON m.id = p.matter_id
            WHERE p.id = CAST(:pid AS uuid) AND TRIM(p.tenant_id) = :tid LIMIT 1
        """), {"pid": project_id, "tid": tid})).mappings().first()
        rows = (await db.execute(sa_text("""
            SELECT id::text, storage_path, filename, mime_type, exhibit_label
            FROM project_documents
            WHERE project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid
              AND role = 'exhibit' AND COALESCE(bates_embossed, false) = false
            ORDER BY sort_order, created_at
        """), {"pid": project_id, "tid": tid})).mappings().all()

    if not rows:
        return {"embossed_count": 0, "skipped_count": 0, "error_count": 0,
                "message": "No un-embossed exhibits"}

    proj_cfg = {}
    if proj and isinstance(proj.get("config"), dict):
        proj_cfg = proj["config"].get("exhibit_sticker_config", {}) or {}
    default_case_info = None
    if proj and proj.get("matter_number") and proj.get("matter_name"):
        default_case_info = f"{proj['matter_number']} | {proj['matter_name']}"
    elif proj and proj.get("matter_name"):
        default_case_info = proj["matter_name"]

    color = body.get("color") or proj_cfg.get("color", "transparent")
    style = body.get("style") or proj_cfg.get("style", "3line")
    position = (body.get("position") or proj_cfg.get("position", "top-right")).replace("_", "-")
    margin = int(body.get("margin") or proj_cfg.get("margin", 36))
    case_info = body.get("case_info") or proj_cfg.get("case_info") or default_case_info

    embossed, skipped, errors = [], [], []
    for r in rows:
        filename = r["filename"] or ""
        ext = os.path.splitext(filename.lower())[1]
        is_pdf = ext == ".pdf" or (r["mime_type"] or "").startswith("application/pdf")
        if not is_pdf:
            skipped.append({"id": r["id"], "filename": filename, "reason": "not_pdf"})
            continue
        disk_path = _resolve_exhibit_disk_path(r["storage_path"], tid)
        if not disk_path:
            skipped.append({"id": r["id"], "filename": filename, "reason": "file_not_found"})
            continue

        label = r["exhibit_label"] or "Exhibit"
        label_short = label[8:].strip() if label.lower().startswith("exhibit ") else label
        tmp_path = disk_path + ".emboss.tmp"
        ok = False
        try:
            ok = _engine_emboss(
                input_pdf_path=disk_path, output_pdf_path=tmp_path,
                exhibit_label=label_short, case_info=case_info,
                color=color, style=style, position=position,
                margin=margin, page_number=0,
            )
        except Exception:
            log.exception("Sticker emboss failed for %s", filename)
            ok = False

        if ok:
            os.replace(tmp_path, disk_path)
            pc = None
            try:
                import fitz
                _d = fitz.open(disk_path); pc = len(_d); _d.close()
            except Exception:
                pass
            async with AsyncSessionLocal() as db:
                params = {"eid": r["id"], "tid": tid}
                sql = "UPDATE project_documents SET bates_embossed = true, updated_at = NOW()"
                if pc is not None:
                    sql += ", page_count = :pc"
                    params["pc"] = pc
                sql += " WHERE id = CAST(:eid AS uuid) AND TRIM(tenant_id) = :tid"
                await db.execute(sa_text(sql), params)
                await db.commit()
            embossed.append({"id": r["id"], "filename": filename, "label": label})
        else:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            errors.append({"id": r["id"], "filename": filename})

    return {
        "embossed_count": len(embossed), "skipped_count": len(skipped),
        "error_count": len(errors), "embossed": embossed,
        "skipped": skipped, "errors": errors,
        "sticker": {"color": color, "style": style, "position": position, "case_info": case_info},
    }


# ═══ BATES EMBOSSING ═══

@exhibit_router.post("/emboss-bates", response_class=JSONResponse)
async def emboss_bates(request: Request, project_id: str):
    tid = _tid(request)
    body = await request.json()
    prefix = body.get("prefix", "PROJ")
    start_number = body.get("start_number", 1)
    num_digits = body.get("num_digits", 6)
    position = body.get("position", "bottom_right")

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa_text("""
            SELECT id::text, storage_path, filename, page_count
            FROM project_documents
            WHERE project_id = CAST(:pid AS uuid) AND TRIM(tenant_id) = :tid AND role = 'exhibit'
            ORDER BY sort_order, created_at
        """), {"pid": project_id, "tid": tid})).fetchall()

    if not rows:
        return {"embossed": 0, "message": "No exhibit documents to emboss"}

    current_number = start_number
    results = []

    for r in rows:
        if not r.storage_path or not os.path.isfile(r.storage_path):
            results.append({"id": r.id, "filename": r.filename, "status": "skipped", "reason": "file not found"})
            continue

        src_path = r.storage_path
        is_pdf = src_path.lower().endswith(".pdf")

        if not is_pdf:
            try:
                out_dir = os.path.dirname(src_path)
                subprocess.run(
                    ["soffice", "--headless", "--convert-to", "pdf", "--outdir", out_dir, src_path],
                    timeout=120, check=True, capture_output=True
                )
                converted = src_path.rsplit(".", 1)[0] + ".pdf"
                if os.path.exists(converted):
                    src_path = converted
                    is_pdf = True
                else:
                    results.append({"id": r.id, "filename": r.filename, "status": "skipped", "reason": "conversion failed"})
                    continue
            except Exception as ex:
                results.append({"id": r.id, "filename": r.filename, "status": "skipped", "reason": str(ex)})
                continue

        try:
            page_count, bates_start, bates_end, output_path = _emboss_pdf(
                src_path, prefix, current_number, num_digits, position
            )
            current_number += page_count

            async with AsyncSessionLocal() as db:
                await db.execute(sa_text("""
                    UPDATE project_documents
                    SET bates_start = :bs, bates_end = :be, bates_embossed = true,
                        page_count = :pc, storage_path = :spath, updated_at = NOW()
                    WHERE id = CAST(:did AS uuid)
                """), {
                    "bs": bates_start, "be": bates_end, "pc": page_count,
                    "spath": output_path, "did": r.id,
                })
                await db.commit()

            results.append({
                "id": r.id, "filename": r.filename, "status": "embossed",
                "bates_start": bates_start, "bates_end": bates_end,
                "page_count": page_count, "output_path": output_path,
            })

        except Exception as ex:
            log.exception(f"Bates emboss failed for {r.filename}")
            results.append({"id": r.id, "filename": r.filename, "status": "error", "reason": str(ex)})

    return {
        "embossed": sum(1 for r in results if r["status"] == "embossed"),
        "next_number": current_number,
        "results": results,
    }


def _emboss_pdf(src_path: str, prefix: str, start: int, digits: int, position: str):
    import io
    from reportlab.pdfgen import canvas as rl_canvas

    try:
        from pikepdf import Pdf
        pdf = Pdf.open(src_path)
        page_count = len(pdf.pages)
        bates_start = f"{prefix}{str(start).zfill(digits)}"
        bates_end = f"{prefix}{str(start + page_count - 1).zfill(digits)}"
        output_path = src_path.rsplit(".", 1)[0] + "_bates.pdf"

        for i, page in enumerate(pdf.pages):
            bates_num = f"{prefix}{str(start + i).zfill(digits)}"
            pwidth = float(page.mediabox[2]) if hasattr(page, 'mediabox') else 612
            pheight = float(page.mediabox[3]) if hasattr(page, 'mediabox') else 792
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(pwidth, pheight))
            c.setFont("Courier", 8)
            c.setFillColorRGB(0, 0, 0)
            if position == "bottom_left":
                c.drawString(36, 18, bates_num)
            elif position == "bottom_center":
                c.drawCentredString(pwidth / 2, 18, bates_num)
            elif position == "top_right":
                c.drawRightString(pwidth - 36, pheight - 18, bates_num)
            else:
                c.drawRightString(pwidth - 36, 18, bates_num)
            c.save()
            buf.seek(0)
            overlay_pdf = Pdf.open(buf)
            page.add_overlay(overlay_pdf.pages[0])

        pdf.save(output_path)
        pdf.close()
        return page_count, bates_start, bates_end, output_path

    except ImportError:
        from PyPDF2 import PdfReader, PdfWriter
        reader = PdfReader(src_path)
        writer = PdfWriter()
        page_count = len(reader.pages)
        bates_start = f"{prefix}{str(start).zfill(digits)}"
        bates_end = f"{prefix}{str(start + page_count - 1).zfill(digits)}"
        output_path = src_path.rsplit(".", 1)[0] + "_bates.pdf"

        for i, page in enumerate(reader.pages):
            bates_num = f"{prefix}{str(start + i).zfill(digits)}"
            box = page.mediabox
            pwidth = float(box.width)
            pheight = float(box.height)
            buf = io.BytesIO()
            c = rl_canvas.Canvas(buf, pagesize=(pwidth, pheight))
            c.setFont("Courier", 8)
            c.setFillColorRGB(0, 0, 0)
            if position == "bottom_left":
                c.drawString(36, 18, bates_num)
            elif position == "bottom_center":
                c.drawCentredString(pwidth / 2, 18, bates_num)
            elif position == "top_right":
                c.drawRightString(pwidth - 36, pheight - 18, bates_num)
            else:
                c.drawRightString(pwidth - 36, 18, bates_num)
            c.save()
            buf.seek(0)
            from PyPDF2 import PdfReader as OverlayReader
            overlay = OverlayReader(buf)
            page.merge_page(overlay.pages[0])
            writer.add_page(page)

        with open(output_path, "wb") as f:
            writer.write(f)
        return page_count, bates_start, bates_end, output_path
