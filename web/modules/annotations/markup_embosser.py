"""
modules/annotations/markup_embosser.py — burn annotations into a flattened PDF.

Bakes the coordinate annotations stored for a (source_type, source_id) into a
new PDF via PyMuPDF (fitz):
  highlight -> semi-transparent colored rectangle
  redaction -> opaque black box, content underneath truly removed
  comment   -> a text box (callout) drawn with its text
  drawing   -> freehand polyline from path_data (percent points)

Result is written to the durable pool under
  /mnt/praesidium/<tenant>/_markups/<source_type>/<source_id>/<set_id>.pdf
and registered as a markup_sets row (kind='embossed').

Pure text-range highlights (transcript) carry no page geometry and are skipped.
"""
import os
import json
import uuid as _uuid
import logging

from starlette.concurrency import run_in_threadpool
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")
PRAESIDIUM_ROOT = "/mnt/praesidium"

HIGHLIGHT_RGB = {
    "yellow": (0.996, 0.941, 0.541),
    "green":  (0.733, 0.969, 0.816),
    "blue":   (0.749, 0.859, 0.996),
    "red":    (0.996, 0.792, 0.792),
    "pink":   (0.984, 0.812, 0.906),
    "orange": (0.996, 0.843, 0.667),
}


def _b64url_decode(s: str) -> str:
    import base64
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode("utf-8", errors="replace")


def _resolve_path_source(tenant_id: str, source_id: str):
    """source_id is the base64url-encoded absolute file path. Only allow paths
    under the tenant's own pool roots; realpath-checked (no ../ traversal)."""
    p = _b64url_decode(source_id)
    rp = os.path.realpath(p)
    t = (tenant_id or "").strip()
    roots = [os.path.realpath(os.path.join(PRAESIDIUM_ROOT, t)),
             os.path.realpath(os.path.join(EDISCOVERY_ROOT, t))]
    if not any(rp == r or rp.startswith(r + os.sep) for r in roots):
        return None
    return rp


async def _resolve_pdf_path(tenant_id: str, source_type: str, source_id: str):
    """Resolve the absolute on-disk PDF path for a source. Path comes from the
    DB only (no request input) — no traversal surface."""
    if source_type == "path":
        return _resolve_path_source(tenant_id, source_id)
    async with AsyncSessionLocal() as s:
        if source_type == "dms":
            r = await s.execute(sa_text(
                "SELECT storage_path FROM documents "
                "WHERE id=CAST(:id AS uuid) AND trim(tenant_id)=trim(:t)"),
                {"id": source_id, "t": tenant_id})
            row = r.fetchone()
            return row[0] if row else None
        if source_type == "record":
            r = await s.execute(sa_text(
                "SELECT storage_path FROM record_documents "
                "WHERE id=CAST(:id AS uuid) AND trim(tenant_id)=trim(:t)"),
                {"id": source_id, "t": tenant_id})
            row = r.fetchone()
            return row[0] if row else None
        if source_type == "ediscovery":
            r = await s.execute(sa_text(
                "SELECT c.storage_path, d.rendition_path, d.file_path, d.native_path "
                "FROM ediscovery_documents d "
                "JOIN ediscovery_collections c ON c.id = d.collection_id "
                "WHERE d.id=CAST(:id AS uuid) AND trim(d.tenant_id)=trim(:t)"),
                {"id": source_id, "t": tenant_id})
            row = r.mappings().fetchone()
            if not row:
                return None
            base = row["storage_path"] or ""
            rel = row["rendition_path"] or row["file_path"] or row["native_path"]
            if not rel:
                return None
            return rel if os.path.isabs(rel) else os.path.join(base, rel)
    return None


async def _fetch_annotations(tenant_id, source_type, source_id, only_created_by, markup_set_id):
    where = [
        "trim(tenant_id::text)=trim(:t)",
        "source_type=:st", "source_id=:sid",
        "deleted_at IS NULL",
        "page_number IS NOT NULL",
        "(x IS NOT NULL OR path_data IS NOT NULL)",  # drawable only
    ]
    params = {"t": tenant_id, "st": source_type, "sid": source_id}
    if only_created_by not in (None, "", "all"):
        where.append("created_by = :cb")
        params["cb"] = int(only_created_by)
    if markup_set_id:
        where.append("markup_set_id = CAST(:msid AS uuid)")
        params["msid"] = markup_set_id
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT annotation_type, page_number, x, y, width, height, "
            "       highlight_color, redaction_style, redaction_label, comment_text, path_data "
            "FROM doc_annotations WHERE " + " AND ".join(where)), params)
        rows = [dict(row) for row in r.mappings().fetchall()]
    for row in rows:
        pd = row.get("path_data")
        if isinstance(pd, str):
            try:
                row["path_data"] = json.loads(pd)
            except Exception:
                row["path_data"] = None
    return rows


def _emboss_bytes(pdf_bytes: bytes, annotations: list) -> tuple:
    """Pure-CPU fitz pass. Returns (out_bytes, page_count)."""
    import fitz
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    has_redaction = False
    for a in annotations:
        pno = (a.get("page_number") or 1) - 1
        if pno < 0 or pno >= doc.page_count:
            continue
        page = doc[pno]
        pr = page.rect
        atype = a.get("annotation_type")
        rgb = HIGHLIGHT_RGB.get(a.get("highlight_color") or "yellow", HIGHLIGHT_RGB["yellow"])

        if atype == "drawing" and a.get("path_data"):
            pts = []
            for pt in a["path_data"]:
                try:
                    px = pr.x0 + (float(pt[0]) / 100.0) * pr.width
                    py = pr.y0 + (float(pt[1]) / 100.0) * pr.height
                    pts.append(fitz.Point(px, py))
                except (TypeError, ValueError, IndexError):
                    continue
            if len(pts) >= 2:
                shape = page.new_shape()
                shape.draw_polyline(pts)
                shape.finish(color=rgb, width=1.5, closePath=False)
                shape.commit()
            continue

        # rect-based types
        try:
            x = float(a["x"]); y = float(a["y"])
            w = float(a["width"]); h = float(a["height"])
        except (TypeError, ValueError, KeyError):
            continue
        rect = fitz.Rect(
            pr.x0 + (x / 100.0) * pr.width,
            pr.y0 + (y / 100.0) * pr.height,
            pr.x0 + ((x + w) / 100.0) * pr.width,
            pr.y0 + ((y + h) / 100.0) * pr.height,
        )
        if atype == "highlight":
            shape = page.new_shape()
            shape.draw_rect(rect)
            shape.finish(color=rgb, fill=rgb, fill_opacity=0.35, stroke_opacity=0.5, width=0.5)
            shape.commit()
        elif atype == "redaction":
            page.add_redact_annot(rect, fill=(0, 0, 0))
            has_redaction = True
        elif atype == "comment":
            txt = a.get("comment_text") or a.get("redaction_label") or ""
            shape = page.new_shape()
            shape.draw_rect(rect)
            shape.finish(color=(0.85, 0.65, 0.13), fill=rgb, fill_opacity=0.25, width=1.0)
            shape.commit()
            if txt:
                try:
                    inner = fitz.Rect(rect.x0 + 2, rect.y0 + 2, rect.x1 - 2, rect.y1 - 2)
                    page.insert_textbox(inner, txt, fontsize=8, color=(0.1, 0.1, 0.1), align=0)
                except Exception as e:
                    logger.warning("insert_textbox: %s", e)
    if has_redaction:
        for page in doc:
            try:
                page.apply_redactions()
            except Exception as e:
                logger.warning("apply_redactions: %s", e)
    out = doc.tobytes(deflate=True, garbage=3)
    pc = doc.page_count
    doc.close()
    return out, pc


async def emboss_source(tenant_id, source_type, source_id, name="",
                        only_created_by=None, markup_set_id=None, owner=None):
    path = await _resolve_pdf_path(tenant_id, source_type, source_id)
    if not path:
        raise FileNotFoundError(
            f"No flattenable PDF for {source_type}:{source_id} "
            f"(transcript/text sources can't be embossed)")
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Source file missing on disk: {path}")
    with open(path, "rb") as f:
        pdf_bytes = f.read()

    anns = await _fetch_annotations(tenant_id, source_type, source_id,
                                    only_created_by, markup_set_id)
    out_bytes, page_count = await run_in_threadpool(_emboss_bytes, pdf_bytes, anns)

    base = os.path.join(PRAESIDIUM_ROOT, tenant_id.strip(), "_markups", source_type, source_id)
    os.makedirs(base, exist_ok=True)
    set_id = str(_uuid.uuid4())
    out_path = os.path.join(base, set_id + ".pdf")
    with open(out_path, "wb") as f:
        f.write(out_bytes)

    owner = owner or {}
    label = name or "Embossed markup"
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text("""
            INSERT INTO markup_sets
                (id, tenant_id, source_type, source_id, name, kind, scope,
                 embossed_path, page_count, owner_user_id, owner_name, meta)
            VALUES (CAST(:id AS uuid), :t, :st, :sid, :name, 'embossed', 'shared',
                 :path, :pc, :uid, :uname, CAST(:meta AS jsonb))
        """), {
            "id": set_id, "t": tenant_id, "st": source_type, "sid": source_id,
            "name": label, "path": out_path, "pc": page_count,
            "uid": owner.get("id"), "uname": owner.get("name"),
            "meta": '{"annotation_count": %d}' % len(anns),
        })
        await s.commit()

    return {
        "set_id": set_id,
        "embossed_path": out_path,
        "page_count": page_count,
        "annotation_count": len(anns),
        "download_url": f"/api/v1/annotations/sets/{set_id}/file",
    }
