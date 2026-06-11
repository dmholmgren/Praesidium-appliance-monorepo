"""Matter Workspace JSON API — disk-first DMS viewer.

GET  /api/v1/dms/matter/{id}          → matter header + stats
GET  /api/v1/dms/matter/{id}/tree     → disk folder tree
GET  /api/v1/dms/matter/{id}/files    → files in a disk folder
GET  /api/v1/dms/matter/{id}/stream   → stream a file (PDF/image inline)
GET  /api/v1/dms/matter/{id}/preview  → convert-and-stream (docx→PDF)
GET  /api/v1/dms/matter/{id}/download → download a file
POST /api/v1/dms/matter/{id}/move     → move file between folders
"""
from __future__ import annotations
import os, logging, mimetypes, shutil, tempfile, subprocess, hashlib, re
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dms.services.oo_native_endpoints import oo_saveas, oo_history, oo_history_data, oo_restore

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dms/matter", tags=["dms-matter-api"])
PRAESIDIUM_ROOT = "/mnt/praesidium"
PREVIEW_CACHE = "/tmp/preview_cache"
os.makedirs(PREVIEW_CACHE, exist_ok=True)

OFFICE_EXT = {"doc","docx","xls","xlsx","ppt","pptx","odt","ods","odp","rtf","txt","csv"}
IMAGE_EXT = {"jpg","jpeg","png","gif","webp","bmp"}
NATIVE_PREVIEW = {"pdf"} | IMAGE_EXT
EMAIL_EXT = {"msg", "eml"}

def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()

def _matter_disk_root(tid, client, matter):
    if not client or not matter: return None
    p = os.path.join(PRAESIDIUM_ROOT, tid, "matters", client, matter)
    return p if os.path.isdir(p) else None

def _safe_path(root, rel):
    resolved = os.path.realpath(os.path.join(root, rel))
    if not resolved.startswith(os.path.realpath(root)):
        raise HTTPException(status_code=403, detail="Path traversal denied")
    return resolved

def _walk_tree(root, max_depth=5, base=None):
    if base is None: base = root
    result = []
    if not os.path.isdir(root): return result
    try: entries = sorted(os.scandir(root), key=lambda e: e.name.lower())
    except PermissionError: return result
    for entry in entries:
        if entry.name.startswith('.'): continue
        if entry.is_dir(follow_symlinks=False):
            children = _walk_tree(entry.path, max_depth-1, base) if max_depth > 1 else []
            try: fc = sum(1 for f in os.scandir(entry.path) if f.is_file() and not f.name.startswith('.'))
            except: fc = 0
            result.append({"name": entry.name, "path": os.path.relpath(entry.path, base), "file_count": fc, "children": children})
    return result

def _list_files(dir_path):
    if not os.path.isdir(dir_path): return []
    files = []
    try:
        for entry in sorted(os.scandir(dir_path), key=lambda e: e.name.lower()):
            if entry.name.startswith('.') or not entry.is_file(follow_symlinks=False): continue
            try: st = entry.stat(); size = st.st_size; mt = st.st_mtime
            except: size = mt = 0
            ext = entry.name.rsplit('.', 1)[-1].lower() if '.' in entry.name else ''
            files.append({"name": entry.name, "size": size, "size_fmt": _fmt(size), "modified": int(mt), "ext": ext})
    except PermissionError: pass
    return files

def _fmt(b):
    if not b: return "—"
    if b < 1024: return f"{b} B"
    if b < 1048576: return f"{b/1024:.1f} KB"
    if b < 1073741824: return f"{b/1048576:.1f} MB"
    return f"{b/1073741824:.1f} GB"

async def _resolve_root(tid, mid):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.matter_name, c.client_name
            FROM matters m LEFT JOIN clients c ON m.client_id = c.id AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": mid, "tid": tid})
        row = r.mappings().fetchone()
    if not row: raise HTTPException(status_code=404, detail="Matter not found")
    root = _matter_disk_root(tid, row["client_name"], row["matter_name"])
    return root, row

def _convert_to_pdf(src_path):
    """Convert an Office doc to PDF via soffice, return cached PDF path.
    Uses per-request profile dir to prevent lock contention between concurrent calls."""
    h = hashlib.md5(f"{src_path}:{os.path.getmtime(src_path)}".encode()).hexdigest()
    cached = os.path.join(PREVIEW_CACHE, h + ".pdf")
    if os.path.exists(cached): return cached
    with tempfile.TemporaryDirectory() as td:
        profile_dir = os.path.join(td, "lo-profile")
        os.makedirs(profile_dir, exist_ok=True)
        try:
            result = subprocess.run([
                "soffice", "--headless", "--norestore", "--nofirststartwizard",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to", "pdf", "--outdir", td, src_path
            ], timeout=60, capture_output=True, text=True)
            if result.returncode != 0:
                logger.warning("soffice convert exit %d for %s: %s", result.returncode, src_path, result.stderr[:300])
        except subprocess.TimeoutExpired:
            logger.error("soffice convert timeout for %s", src_path)
            return None
        except Exception as e:
            logger.error("soffice convert failed: %s", e)
            return None
        for f in os.listdir(td):
            if f.endswith(".pdf"):
                shutil.copy2(os.path.join(td, f), cached)
                return cached
    return None


# ── Email rendering ───────────────────────────────────────────────────
def _render_email_html(fp, ext, att_base_url=""):
    """Parse .msg or .eml file and return sanitized HTML for inline preview."""
    import html as _html

    subject = ""
    from_addr = ""
    to_addr = ""
    cc_addr = ""
    date_str = ""
    body_html = ""
    body_text = ""
    attachments = []

    if ext == "msg":
        try:
            import extract_msg
            msg = extract_msg.Message(fp)
            subject = msg.subject or ""
            from_addr = msg.sender or ""
            to_addr = msg.to or ""
            cc_addr = msg.cc or ""
            date_str = str(msg.date or "")
            body_html = msg.htmlBody or ""
            if isinstance(body_html, bytes):
                body_html = body_html.decode("utf-8", errors="replace")
            body_text = msg.body or ""
            attachments = [a.longFilename or a.shortFilename or "attachment" for a in (msg.attachments or [])]
            msg.close()
        except Exception as e:
            logger.warning("MSG parse failed %s: %s", fp, e)
            return None
    elif ext == "eml":
        try:
            import email as _email
            import email.policy
            with open(fp, "rb") as f:
                msg = _email.message_from_binary_file(f, policy=_email.policy.default)
            subject = str(msg.get("Subject", ""))
            from_addr = str(msg.get("From", ""))
            to_addr = str(msg.get("To", ""))
            cc_addr = str(msg.get("Cc", "") or "")
            date_str = str(msg.get("Date", ""))
            body_html = msg.get_body(preferencelist=("html",))
            if body_html:
                body_html = body_html.get_content()
            else:
                body_html = ""
            body_text_part = msg.get_body(preferencelist=("plain",))
            if body_text_part:
                body_text = body_text_part.get_content()
            else:
                body_text = ""
            for part in msg.iter_attachments():
                fn = part.get_filename()
                if fn:
                    attachments.append(fn)
        except Exception as e:
            logger.warning("EML parse failed %s: %s", fp, e)
            return None
    else:
        return None

    # Build preview HTML
    esc = _html.escape
    header_rows = f"""
        <tr><td style="font-weight:600;color:#64748b;padding:2px 12px 2px 0;white-space:nowrap;vertical-align:top;">From</td><td style="padding:2px 0;">{esc(from_addr)}</td></tr>
        <tr><td style="font-weight:600;color:#64748b;padding:2px 12px 2px 0;white-space:nowrap;vertical-align:top;">To</td><td style="padding:2px 0;">{esc(to_addr)}</td></tr>"""
    if cc_addr:
        header_rows += f'\n        <tr><td style="font-weight:600;color:#64748b;padding:2px 12px 2px 0;white-space:nowrap;vertical-align:top;">Cc</td><td style="padding:2px 0;">{esc(cc_addr)}</td></tr>'
    header_rows += f"""
        <tr><td style="font-weight:600;color:#64748b;padding:2px 12px 2px 0;white-space:nowrap;vertical-align:top;">Date</td><td style="padding:2px 0;">{esc(date_str)}</td></tr>
        <tr><td style="font-weight:600;color:#64748b;padding:2px 12px 2px 0;white-space:nowrap;vertical-align:top;">Subject</td><td style="padding:2px 0;font-weight:600;">{esc(subject)}</td></tr>"""

    att_html = ""
    if attachments:
        att_items = "".join(f'<a href="{att_base_url}&index={i}" target="_blank" style="display:inline-block;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:4px;padding:2px 8px;margin:2px 4px 2px 0;font-size:12px;text-decoration:none;color:#1a1a1a;cursor:pointer;" title="Click to open">📎 {esc(a)}</a>' for i, a in enumerate(attachments))
        att_html = f'<div style="padding:8px 16px;border-bottom:1px solid #e2e8f0;background:#fafafa;">{att_items}</div>'

    # Use HTML body if available, otherwise wrap plain text
    if body_html:
        # Strip <html>/<head>/<body> wrappers, keep inner content
        import re
        content = re.sub(r'(?i)<html[^>]*>|</html>|<head[^>]*>.*?</head>|<body[^>]*>|</body>', '', body_html, flags=re.DOTALL)
        body_section = f'<div style="padding:16px;font-family:sans-serif;font-size:14px;line-height:1.5;overflow:auto;">{content}</div>'
    elif body_text:
        body_section = f'<pre style="padding:16px;font-family:monospace;font-size:13px;white-space:pre-wrap;line-height:1.5;margin:0;">{esc(body_text)}</pre>'
    else:
        body_section = '<div style="padding:16px;color:#94a3b8;font-size:13px;">(No message body)</div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin:0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color:#1a1a1a; background:#fff; }}
table {{ border-collapse:collapse; }}
img {{ max-width:100%; height:auto; }}
</style></head><body>
<div style="padding:12px 16px;border-bottom:1px solid #e2e8f0;background:#f8fafc;">
  <table style="font-size:13px;width:100%;">{header_rows}</table>
</div>
{att_html}
{body_section}
</body></html>"""




# ── Staging file preview (for drafting outputs in /chats/) ───────────
@router.get("/staging/preview")
async def staging_preview(request: Request, path: str = ""):
    """Preview a staged draft file (from /chats/ directory).
    Accepts absolute path — validates it's under PRAESIDIUM_ROOT for security."""
    tid = _tid(request)
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    # Security: must be under the tenant's praesidium directory
    tenant_root = os.path.join(PRAESIDIUM_ROOT, tid)
    resolved = os.path.realpath(path)
    if not resolved.startswith(os.path.realpath(tenant_root)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    ext = resolved.rsplit('.', 1)[-1].lower() if '.' in resolved else ''
    # Native preview
    if ext in NATIVE_PREVIEW:
        mime = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
        return FileResponse(resolved, media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{os.path.basename(resolved)}"'})
    # Office → PDF conversion
    if ext in OFFICE_EXT:
        pdf_path = _convert_to_pdf(resolved)
        if pdf_path and os.path.isfile(pdf_path):
            return FileResponse(pdf_path, media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{os.path.basename(resolved)}.pdf"'})
        from starlette.responses import HTMLResponse
        fname = os.path.basename(resolved)
        return HTMLResponse(content=f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin:0; display:flex; align-items:center; justify-content:center; height:100vh;
       font-family:sans-serif; background:#f8fafc; color:#334155; }}
.card {{ text-align:center; padding:32px; }}
</style></head><body><div class="card">
<div style="font-size:48px;margin-bottom:12px;">📄</div>
<h3 style="font-size:15px;">{fname}</h3>
<p style="font-size:12px;color:#64748b;">Preview conversion in progress or unavailable.<br>The document was drafted successfully.</p>
</div></body></html>''', media_type="text/html")
    raise HTTPException(status_code=415, detail=f"No preview for .{ext}")


# ── Matter header ────────────────────────────────────────────────────────
@router.get("/{matter_id}")
async def matter_info(request: Request, matter_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.status,
                   m.matter_type, m.practice_area, c.client_name, c.id::text AS client_id
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tid})
        row = r.mappings().fetchone()
    if not row: raise HTTPException(status_code=404, detail="Matter not found")
    matter = dict(row)
    disk_root = _matter_disk_root(tid, matter.get("client_name"), matter.get("matter_name"))
    matter["has_disk"] = disk_root is not None
    return JSONResponse(matter)


# ── Folder tree ──────────────────────────────────────────────────────────
@router.get("/{matter_id}/tree")
async def matter_tree(request: Request, matter_id: str):
    tid = _tid(request)
    root, _ = await _resolve_root(tid, matter_id)
    if not root: return JSONResponse({"tree": [], "root": None, "root_file_count": 0})
    tree = _walk_tree(root)
    try: rfc = sum(1 for f in os.scandir(root) if f.is_file() and not f.name.startswith('.'))
    except: rfc = 0
    return JSONResponse({"tree": tree, "root": root, "root_file_count": rfc})


# ── File list ────────────────────────────────────────────────────────────
@router.get("/{matter_id}/files")
async def matter_files(request: Request, matter_id: str, path: str = ""):
    tid = _tid(request)
    root, _ = await _resolve_root(tid, matter_id)
    if not root: return JSONResponse({"files": [], "folder_name": ""})
    target = _safe_path(root, path) if path else root
    files = _list_files(target)

    # ── Enrich with document metadata (disk stays source of truth) ──
    if files:
        abs_paths = [os.path.join(target, f["name"]) for f in files]
        try:
            async with AsyncSessionLocal() as session:
                # Batch query: get doc metadata for all files in this folder
                r = await session.execute(sa_text("""
                    SELECT storage_path, id::text AS document_id,
                           version_number, parent_doc_id::text
                    FROM documents
                    WHERE storage_path = ANY(:paths)
                      AND TRIM(tenant_id) = :tid
                """), {"paths": abs_paths, "tid": tid})
                doc_map = {}
                for row in r.mappings().fetchall():
                    sp = row["storage_path"]
                    doc_map[sp] = {
                        "document_id": row["document_id"],
                        "version_number": row["version_number"],
                        "parent_doc_id": row["parent_doc_id"],
                    }
            # Attach metadata to file objects
            for i, f in enumerate(files):
                ap = abs_paths[i]
                meta = doc_map.get(ap)
                if meta:
                    f["_documentId"] = meta["document_id"]
                    f["_versionNumber"] = meta["version_number"] or 1
                    f["_parentDocId"] = meta["parent_doc_id"]
                else:
                    f["_documentId"] = None
                    f["_versionNumber"] = None
                    f["_parentDocId"] = None
        except Exception as e:
            logger.warning("File enrichment query failed (non-fatal): %s", e)
            for f in files:
                f["_documentId"] = None
                f["_versionNumber"] = None
                f["_parentDocId"] = None

    return JSONResponse({"files": files, "folder_name": os.path.basename(target) if path else "Matter Root", "path": path})


# ── Stream file (native: PDF, images) ───────────────────────────────────
@router.get("/{matter_id}/stream")
async def matter_stream(request: Request, matter_id: str, path: str = ""):
    tid = _tid(request)
    if not path: raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, path)
    if not os.path.isfile(fp): raise HTTPException(status_code=404, detail="File not found")
    mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
    return FileResponse(fp, media_type=mime, headers={"Content-Disposition": f'inline; filename="{os.path.basename(fp)}"'})


# ── Preview file (converts Office→PDF on the fly) ───────────────────────
@router.get("/{matter_id}/preview")
async def matter_preview(request: Request, matter_id: str, path: str = ""):
    tid = _tid(request)
    if not path: raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, path)
    if not os.path.isfile(fp): raise HTTPException(status_code=404, detail="File not found")
    ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''
    # Native preview (PDF/image) — stream directly
    if ext in NATIVE_PREVIEW:
        mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        # Sanitize filename for Content-Disposition to prevent browser download triggers
        safe_name = os.path.basename(fp).replace('"', '').replace("'", "")
        return FileResponse(fp, media_type=mime, headers={
            "Content-Disposition": f'inline; filename="{safe_name}"',
            "Content-Type": mime,
            "X-Content-Type-Options": "nosniff",
        })
    # Office doc → convert to PDF
    if ext in OFFICE_EXT:
        pdf_path = _convert_to_pdf(fp)
        if pdf_path and os.path.isfile(pdf_path):
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(fp)) + ".pdf"
            return FileResponse(pdf_path, media_type="application/pdf",
                                headers={
                                    "Content-Disposition": f'inline; filename="{safe_name}"',
                                    "Content-Type": "application/pdf",
                                })
        # Serve a preview-unavailable HTML page instead of an error
        # (errors can trigger browser download behavior)
        from starlette.responses import HTMLResponse
        fname = os.path.basename(fp)
        dl_link = f"/api/v1/dms/matter/{matter_id}/download?path={path}"
        return HTMLResponse(content=f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ margin:0; display:flex; align-items:center; justify-content:center; height:100vh;
       font-family:-apple-system,BlinkMacSystemFont,sans-serif; background:#f8fafc; color:#334155; }}
.card {{ text-align:center; padding:32px; }}
.icon {{ font-size:48px; margin-bottom:12px; }}
h3 {{ margin:0 0 8px; font-size:15px; color:#1e293b; }}
p {{ margin:0 0 16px; font-size:12px; color:#64748b; }}
a {{ display:inline-block; padding:8px 24px; background:#1d4ed8; color:#fff; text-decoration:none;
    border-radius:6px; font-size:12px; font-weight:600; }}
a:hover {{ background:#1e40af; }}
</style></head><body><div class="card">
<div class="icon">📄</div>
<h3>{fname}</h3>
<p>Preview not available for this file type.<br>Download to view in your desktop application.</p>
<a href="{dl_link}" download>Download File</a>
</div></body></html>""", media_type="text/html")
    # Email files → render as HTML
    if ext in EMAIL_EXT:
        import urllib.parse as _urlparse
        rel_path = _urlparse.quote(os.path.relpath(fp, root))
        att_url = f"/api/v1/dms/matter/{matter_id}/email-attachment?path={rel_path}"
        rendered = _render_email_html(fp, ext, att_base_url=att_url)
        if rendered:
            from starlette.responses import HTMLResponse
            return HTMLResponse(content=rendered, media_type="text/html")
        raise HTTPException(status_code=500, detail="Email parse failed")
    raise HTTPException(status_code=415, detail=f"No preview for .{ext}")


# ── Download ─────────────────────────────────────────────────────────────
@router.get("/{matter_id}/download")
async def matter_download(request: Request, matter_id: str, path: str = ""):
    tid = _tid(request)
    if not path: raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, path)
    if not os.path.isfile(fp): raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(fp, filename=os.path.basename(fp), media_type="application/octet-stream")


# ── Email attachment download ─────────────────────────────────────────
@router.get("/{matter_id}/email-attachment")
async def email_attachment(request: Request, matter_id: str, path: str = "", index: int = 0):
    """Stream an attachment from a .msg or .eml file by index."""
    tid = _tid(request)
    if not path: raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, path)
    if not os.path.isfile(fp): raise HTTPException(status_code=404, detail="File not found")
    ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''

    from starlette.responses import Response

    if ext == "msg":
        try:
            import extract_msg
            msg = extract_msg.Message(fp)
            atts = msg.attachments or []
            if index < 0 or index >= len(atts):
                msg.close()
                raise HTTPException(status_code=404, detail=f"Attachment index {index} not found ({len(atts)} total)")
            att = atts[index]
            data = att.data
            if data is None:
                msg.close()
                raise HTTPException(status_code=404, detail="Attachment has no data")
            filename = att.longFilename or att.shortFilename or f"attachment_{index}"
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            msg.close()
            # For previewable types, serve inline; otherwise attachment
            att_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            inline_exts = {"pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp", "txt", "html", "htm"}
            disp = "inline" if att_ext in inline_exts else "attachment"
            return Response(content=data, media_type=mime,
                           headers={"Content-Disposition": f'{disp}; filename="{filename}"'})
        except HTTPException: raise
        except Exception as e:
            logger.error("MSG attachment extract failed %s[%d]: %s", fp, index, e)
            raise HTTPException(status_code=500, detail=f"Attachment extract failed: {e}")

    elif ext == "eml":
        try:
            import email as _email
            import email.policy
            with open(fp, "rb") as f:
                msg = _email.message_from_binary_file(f, policy=_email.policy.default)
            atts = list(msg.iter_attachments())
            if index < 0 or index >= len(atts):
                raise HTTPException(status_code=404, detail=f"Attachment index {index} not found ({len(atts)} total)")
            part = atts[index]
            data = part.get_content()
            if isinstance(data, str):
                data = data.encode("utf-8")
            filename = part.get_filename() or f"attachment_{index}"
            mime = part.get_content_type() or "application/octet-stream"
            att_ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            inline_exts = {"pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp", "txt", "html", "htm"}
            disp = "inline" if att_ext in inline_exts else "attachment"
            return Response(content=data, media_type=mime,
                           headers={"Content-Disposition": f'{disp}; filename="{filename}"'})
        except HTTPException: raise
        except Exception as e:
            logger.error("EML attachment extract failed %s[%d]: %s", fp, index, e)
            raise HTTPException(status_code=500, detail=f"Attachment extract failed: {e}")

    raise HTTPException(status_code=415, detail="Not an email file")


# ── Move file ────────────────────────────────────────────────────────────
@router.post("/{matter_id}/move")
async def matter_move(request: Request, matter_id: str):
    tid = _tid(request)
    body = await request.json()
    src = body.get("src", "")
    dst_folder = body.get("dst_folder", "")
    if not src: raise HTTPException(status_code=400, detail="src required")
    if not dst_folder and dst_folder != "": raise HTTPException(status_code=400, detail="dst_folder required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    src_path = _safe_path(root, src)
    if not os.path.isfile(src_path): raise HTTPException(status_code=404, detail="Source file not found")
    dst_dir = _safe_path(root, dst_folder) if dst_folder else root
    if not os.path.isdir(dst_dir): raise HTTPException(status_code=404, detail="Destination folder not found")
    dst_path = os.path.join(dst_dir, os.path.basename(src_path))
    if os.path.exists(dst_path):
        # Auto-rename: file (1).ext
        base, ext = os.path.splitext(os.path.basename(src_path))
        i = 1
        while os.path.exists(dst_path):
            dst_path = os.path.join(dst_dir, f"{base} ({i}){ext}")
            i += 1
    try:
        shutil.move(src_path, dst_path)
    except Exception as e:
        logger.error("Move failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Move failed: {e}")
    return JSONResponse({"status": "ok", "moved_to": os.path.relpath(dst_path, root)})


# ── Email metadata (for reply pre-population) ────────────────────────
@router.get("/{matter_id}/email-meta")
async def email_meta(request: Request, matter_id: str, path: str = ""):
    """Parse .msg or .eml and return metadata as JSON for reply pre-population."""
    tid = _tid(request)
    if not path: raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root: raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, path)
    if not os.path.isfile(fp): raise HTTPException(status_code=404, detail="File not found")
    ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''

    meta = {"subject":"","from_addr":"","from_name":"","to":"","cc":"","date":"",
            "message_id":"","body_text":"","body_html":"","attachments":[]}

    if ext == "msg":
        try:
            import extract_msg
            msg = extract_msg.Message(fp)
            meta["subject"] = msg.subject or ""
            meta["from_addr"] = msg.sender or ""
            meta["to"] = msg.to or ""
            meta["cc"] = msg.cc or ""
            meta["date"] = str(msg.date or "")
            meta["body_text"] = msg.body or ""
            html = msg.htmlBody or ""
            if isinstance(html, bytes): html = html.decode("utf-8", errors="replace")
            meta["body_html"] = html
            meta["attachments"] = [a.longFilename or a.shortFilename or "attachment"
                                   for a in (msg.attachments or [])]
            msg.close()
        except Exception as e:
            logger.warning("MSG meta parse failed %s: %s", fp, e)
            raise HTTPException(status_code=500, detail=f"Email parse failed: {e}")

    elif ext == "eml":
        try:
            import email as _email
            import email.policy
            with open(fp, "rb") as f:
                msg = _email.message_from_binary_file(f, policy=_email.policy.default)
            meta["subject"] = str(msg.get("Subject", ""))
            meta["from_addr"] = str(msg.get("From", ""))
            meta["to"] = str(msg.get("To", ""))
            meta["cc"] = str(msg.get("Cc", "") or "")
            meta["date"] = str(msg.get("Date", ""))
            meta["message_id"] = str(msg.get("Message-ID", "") or "")
            body_html_part = msg.get_body(preferencelist=("html",))
            if body_html_part: meta["body_html"] = body_html_part.get_content()
            body_text_part = msg.get_body(preferencelist=("plain",))
            if body_text_part: meta["body_text"] = body_text_part.get_content()
            for part in msg.iter_attachments():
                fn = part.get_filename()
                if fn: meta["attachments"].append(fn)
        except Exception as e:
            logger.warning("EML meta parse failed %s: %s", fp, e)
            raise HTTPException(status_code=500, detail=f"Email parse failed: {e}")
    else:
        raise HTTPException(status_code=415, detail="Not an email file")

    return JSONResponse(meta)



def _append_redline_summary(docx_path, name_a, name_b, stats=None):
    """Append a DeltaView-style changes summary page to the end of a redline .docx.

    Opens the docx, reads through paragraphs to count actual changes,
    appends a summary section with statistics and section-by-section inventory.
    """
    try:
        from docx import Document as _DocxDoc
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from datetime import datetime, timezone

        doc = _DocxDoc(docx_path)

        # Scan paragraphs for changes if stats not provided
        if not stats or stats.get("method"):
            ins_count = 0
            del_count = 0
            mod_count = 0
            unchanged = 0
            change_locations = []
            current_section = "Document Body"

            for i, p in enumerate(doc.paragraphs):
                # Detect section headings
                if p.style and p.style.name and "Heading" in p.style.name:
                    current_section = p.text[:60] if p.text else current_section

                # Check for tracked changes in XML (w:ins, w:del)
                xml = p._element.xml
                has_ins = "<w:ins " in xml or "<w:ins>" in xml
                has_del = "<w:del " in xml or "<w:del>" in xml

                # Check for colored text (fallback redline style)
                has_red = False
                has_blue = False
                for run in p.runs:
                    if run.font.color and run.font.color.rgb:
                        rgb = str(run.font.color.rgb)
                        if rgb in ("CC0000", "FF0000"):
                            has_red = True
                        elif rgb in ("0044CC", "0000FF", "0000CC"):
                            has_blue = True
                    if run.font.strike:
                        has_red = True

                if has_ins or has_blue:
                    ins_count += 1
                    if len(change_locations) == 0 or change_locations[-1][0] != current_section:
                        change_locations.append((current_section, {"ins": 0, "del": 0}))
                    change_locations[-1][1]["ins"] += 1
                elif has_del or has_red:
                    del_count += 1
                    if len(change_locations) == 0 or change_locations[-1][0] != current_section:
                        change_locations.append((current_section, {"ins": 0, "del": 0}))
                    change_locations[-1][1]["del"] += 1
                elif p.text and p.text.strip():
                    unchanged += 1

            stats = {"inserted": ins_count, "deleted": del_count, "replaced": mod_count, "equal": unchanged}
        else:
            change_locations = []

        now_str = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
        total_changes = (stats.get("inserted", 0) + stats.get("deleted", 0) + stats.get("replaced", 0))

        # ── Page break before summary ──
        from docx.oxml import OxmlElement
        br_para = doc.add_paragraph()
        run = br_para.add_run()
        br = OxmlElement("w:br")
        br.set(qn("w:type"), "page")
        run._element.append(br)

        # ── Summary header ──
        h = doc.add_heading("Comparison Summary", level=1)
        for run in h.runs:
            run.font.size = Pt(16)
            run.font.color.rgb = RGBColor(0x0D, 0x1F, 0x3C)

        # ── Document info table ──
        doc.add_paragraph("")
        tbl = doc.add_table(rows=4, cols=2)
        tbl.style = "Table Grid"
        cells = [
            ("Original Document", name_a),
            ("Revised Document", name_b),
            ("Comparison Date", now_str),
            ("Total Changes", str(total_changes)),
        ]
        for i, (label, value) in enumerate(cells):
            cell_l = tbl.cell(i, 0)
            cell_v = tbl.cell(i, 1)
            cell_l.text = label
            cell_v.text = value
            for p in cell_l.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(10)
                    r.font.bold = True
                    r.font.color.rgb = RGBColor(0x37, 0x41, 0x51)
            for p in cell_v.paragraphs:
                for r in p.runs:
                    r.font.size = Pt(10)

        doc.add_paragraph("")

        # ── Change statistics ──
        doc.add_heading("Change Statistics", level=2)

        stats_tbl = doc.add_table(rows=5, cols=3)
        stats_tbl.style = "Table Grid"
        stat_rows = [
            ("Change Type", "Count", ""),
            ("Insertions (new text)", str(stats.get("inserted", 0)), "█" * min(stats.get("inserted", 0), 30)),
            ("Deletions (removed text)", str(stats.get("deleted", 0)), "█" * min(stats.get("deleted", 0), 30)),
            ("Modifications (replaced)", str(stats.get("replaced", 0)), "█" * min(stats.get("replaced", 0), 30)),
            ("Unchanged paragraphs", str(stats.get("equal", 0)), ""),
        ]
        colors = [None, RGBColor(0x00, 0x44, 0xCC), RGBColor(0xCC, 0x00, 0x00), RGBColor(0xD9, 0x77, 0x06), RGBColor(0x6B, 0x72, 0x80)]
        for i, (label, count, bar) in enumerate(stat_rows):
            stats_tbl.cell(i, 0).text = label
            stats_tbl.cell(i, 1).text = count
            if bar:
                p = stats_tbl.cell(i, 2).paragraphs[0]
                r = p.add_run(bar)
                r.font.size = Pt(6)
                if colors[i]:
                    r.font.color.rgb = colors[i]
            if i == 0:
                for j in range(3):
                    for p in stats_tbl.cell(i, j).paragraphs:
                        for r in p.runs:
                            r.font.bold = True
                            r.font.size = Pt(9)
            else:
                for p in stats_tbl.cell(i, 0).paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(10)
                        if colors[i]:
                            r.font.color.rgb = colors[i]

        # ── Section-by-section inventory (if available) ──
        if change_locations:
            doc.add_paragraph("")
            doc.add_heading("Changes by Section", level=2)
            for section_name, counts in change_locations:
                p = doc.add_paragraph()
                r = p.add_run(f"▶ {section_name}")
                r.font.size = Pt(10)
                r.font.bold = True
                detail = []
                if counts.get("ins", 0) > 0:
                    detail.append(f"+{counts['ins']} inserted")
                if counts.get("del", 0) > 0:
                    detail.append(f"-{counts['del']} deleted")
                if detail:
                    r2 = p.add_run(f"  ({', '.join(detail)})")
                    r2.font.size = Pt(9)
                    r2.font.color.rgb = RGBColor(0x6B, 0x72, 0x80)

        # ── Legend ──
        doc.add_paragraph("")
        doc.add_heading("Legend", level=2)
        legend = doc.add_paragraph()
        r = legend.add_run("Red strikethrough")
        r.font.color.rgb = RGBColor(0xCC, 0x00, 0x00)
        r.font.strike = True
        r.font.size = Pt(10)
        r2 = legend.add_run(" = Deleted text    ")
        r2.font.size = Pt(10)
        r3 = legend.add_run("Blue underline")
        r3.font.color.rgb = RGBColor(0x00, 0x44, 0xCC)
        r3.font.underline = True
        r3.font.size = Pt(10)
        r4 = legend.add_run(" = Inserted text")
        r4.font.size = Pt(10)

        # ── Certification ──
        doc.add_paragraph("")
        cert = doc.add_paragraph()
        cert.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = cert.add_run("This comparison was generated by Praesidium Legal Platform")
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x94, 0xA3, 0xB8)
        r.font.italic = True

        doc.save(docx_path)
        logger.info("Appended redline summary to %s (%d changes)", docx_path, total_changes)

    except Exception as e:
        logger.warning("Failed to append redline summary: %s", e)
        # Non-fatal — the redline itself is still valid


# ── Document Comparison / Redline (LO native + fallback) ─────────────────
@router.post("/{matter_id}/compare")
async def matter_compare(request: Request, matter_id: str):
    """Generate a redline using LibreOffice native comparison.

    Body: { "path_a": "...", "path_b": "...", "format": "pdf"|"docx" }
    Default format: pdf. Returns tracked-changes redline.
    """
    import uuid as _uuid

    tid = _tid(request)
    body = await request.json()
    path_a = body.get("path_a", "")
    path_b = body.get("path_b", "")
    fmt = body.get("format", "pdf").lower()
    if fmt not in ("pdf", "docx"):
        fmt = "pdf"
    if not path_a or not path_b:
        raise HTTPException(status_code=400, detail="path_a and path_b required")
    if path_a == path_b:
        raise HTTPException(status_code=400, detail="Cannot compare a file with itself")

    root, _ = await _resolve_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")

    fp_a = _safe_path(root, path_a)
    fp_b = _safe_path(root, path_b)
    if not os.path.isfile(fp_a):
        raise HTTPException(status_code=404, detail=f"Original not found: {os.path.basename(fp_a)}")
    if not os.path.isfile(fp_b):
        raise HTTPException(status_code=404, detail=f"Revised not found: {os.path.basename(fp_b)}")

    job_id = str(_uuid.uuid4())[:8]
    name_a = os.path.basename(fp_a)
    name_b = os.path.basename(fp_b)
    short_a = os.path.splitext(name_a)[0][:25].replace("/", "_")
    short_b = os.path.splitext(name_b)[0][:25].replace("/", "_")
    ext = "pdf" if fmt == "pdf" else "docx"
    out_name = f"REDLINE_{short_a}_vs_{short_b}_{job_id}.{ext}"

    redline_dir = os.path.join(root, "99-Redlines")
    os.makedirs(redline_dir, exist_ok=True)
    output_path = os.path.join(redline_dir, out_name)
    profile_dir = f"/tmp/lo-compare-{job_id}"

    method = "unknown"

    try:
        # ── Try LibreOffice native compare ──
        lo_python = "/usr/lib/libreoffice/program/python"
        helper = "/app/modules/dms/services/lo_compare.py"

        lo_success = False
        if os.path.isfile(lo_python):
            # For LO native, always generate docx first (tracked changes),
            # then convert to PDF if needed
            if fmt == "pdf":
                docx_tmp = output_path.replace(".pdf", ".docx")
            else:
                docx_tmp = output_path

            result = subprocess.run(
                [lo_python, helper, fp_a, fp_b, docx_tmp, profile_dir],
                timeout=90, capture_output=True, text=True
            )
            if result.returncode == 0 and os.path.isfile(docx_tmp):
                method = "libreoffice_native"
                lo_success = True

                # Append DeltaView-style summary
                _append_redline_summary(docx_tmp, name_a, name_b)

                if fmt == "pdf":
                    # Convert the tracked-changes docx to PDF
                    pdf_profile = f"/tmp/lo-pdf-{job_id}"
                    os.makedirs(pdf_profile, exist_ok=True)
                    pdf_result = subprocess.run([
                        "soffice", "--headless", "--norestore",
                        f"-env:UserInstallation=file://{pdf_profile}",
                        "--convert-to", "pdf", "--outdir", redline_dir, docx_tmp
                    ], timeout=60, capture_output=True, text=True)

                    # soffice names output based on input filename
                    expected_pdf = os.path.join(redline_dir,
                        os.path.splitext(os.path.basename(docx_tmp))[0] + ".pdf")
                    if os.path.isfile(expected_pdf):
                        if expected_pdf != output_path:
                            shutil.move(expected_pdf, output_path)
                        # Clean up the intermediate docx
                        try: os.remove(docx_tmp)
                        except: pass
                    else:
                        # PDF conversion failed — serve the docx instead
                        logger.warning("PDF conversion failed, serving docx")
                        output_path = docx_tmp
                        out_name = os.path.basename(docx_tmp)
                        ext = "docx"

                    try: shutil.rmtree(pdf_profile, ignore_errors=True)
                    except: pass
            else:
                logger.warning("LO native compare failed (exit %d): %s",
                    result.returncode, (result.stderr or "")[:500])

        # ── Fallback: python-docx text diff ──
        if not lo_success:
            logger.info("Falling back to python-docx text diff")
            method = "python_docx_fallback"

            from docx import Document as DocxDocument
            from docx.shared import RGBColor
            from datetime import datetime, timezone
            import difflib

            def _extract(fpath):
                fext = fpath.rsplit(".", 1)[-1].lower() if "." in fpath else ""
                if fext == "docx":
                    return [p.text for p in DocxDocument(fpath).paragraphs]
                elif fext in ("txt", "csv", "md"):
                    with open(fpath, errors="replace") as f:
                        return f.read().splitlines()
                else:
                    with tempfile.TemporaryDirectory() as td:
                        pd = os.path.join(td, "profile")
                        os.makedirs(pd, exist_ok=True)
                        subprocess.run([
                            "soffice", "--headless", "--norestore",
                            f"-env:UserInstallation=file://{pd}",
                            "--convert-to", "docx", "--outdir", td, fpath
                        ], timeout=60, capture_output=True)
                        for fn in os.listdir(td):
                            if fn.endswith(".docx"):
                                return [p.text for p in DocxDocument(os.path.join(td, fn)).paragraphs]
                    return [f"[Could not extract: {os.path.basename(fpath)}]"]

            text_a = _extract(fp_a)
            text_b = _extract(fp_b)

            RED = RGBColor(0xCC, 0x00, 0x00)
            BLUE = RGBColor(0x00, 0x44, 0xCC)
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            doc = DocxDocument()
            doc.add_heading("Redline Comparison", level=1)
            doc.add_paragraph(f"Original: {name_a}")
            doc.add_paragraph(f"Revised: {name_b}")
            doc.add_paragraph(f"Generated: {now_str}")
            doc.add_paragraph("")

            matcher = difflib.SequenceMatcher(a=text_a, b=text_b, autojunk=False)
            stats = {"equal": 0, "deleted": 0, "inserted": 0, "replaced": 0}

            for op, a0, a1, b0, b1 in matcher.get_opcodes():
                if op == "equal":
                    for line in text_a[a0:a1]:
                        if line.strip(): doc.add_paragraph(line)
                        stats["equal"] += 1
                elif op == "delete":
                    for line in text_a[a0:a1]:
                        if line.strip():
                            run = doc.add_paragraph().add_run(line)
                            run.font.color.rgb = RED; run.font.strike = True
                        stats["deleted"] += 1
                elif op == "insert":
                    for line in text_b[b0:b1]:
                        if line.strip():
                            run = doc.add_paragraph().add_run(line)
                            run.font.color.rgb = BLUE; run.font.underline = True
                        stats["inserted"] += 1
                elif op == "replace":
                    for line in text_a[a0:a1]:
                        if line.strip():
                            run = doc.add_paragraph().add_run(line)
                            run.font.color.rgb = RED; run.font.strike = True
                    for line in text_b[b0:b1]:
                        if line.strip():
                            run = doc.add_paragraph().add_run(line)
                            run.font.color.rgb = BLUE; run.font.underline = True
                    stats["replaced"] += max(a1 - a0, b1 - b0)

            # Save as docx first
            docx_path = output_path if ext == "docx" else output_path.replace(".pdf", ".docx")
            doc.save(docx_path)

            # Append DeltaView-style summary
            _append_redline_summary(docx_path, name_a, name_b, stats)

            if fmt == "pdf":
                pdf_profile = f"/tmp/lo-pdf-fb-{job_id}"
                os.makedirs(pdf_profile, exist_ok=True)
                subprocess.run([
                    "soffice", "--headless", "--norestore",
                    f"-env:UserInstallation=file://{pdf_profile}",
                    "--convert-to", "pdf", "--outdir", redline_dir, docx_path
                ], timeout=60, capture_output=True)
                expected = os.path.join(redline_dir,
                    os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
                if os.path.isfile(expected):
                    if expected != output_path:
                        shutil.move(expected, output_path)
                    try: os.remove(docx_path)
                    except: pass
                else:
                    output_path = docx_path
                    out_name = os.path.basename(docx_path)
                    ext = "docx"
                try: shutil.rmtree(pdf_profile, ignore_errors=True)
                except: pass

        rel_path = os.path.relpath(output_path, root)
        return JSONResponse({
            "redline_path": rel_path,
            "redline_name": out_name,
            "format": ext,
            "changes": {"method": method},
        })

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Compare timed out")
    except Exception as e:
        logger.error("Compare failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Compare failed: {e}")
    finally:
        try: shutil.rmtree(profile_dir, ignore_errors=True)
        except: pass





# ── OnlyOffice Config Endpoint ───────────────────────────────────────
@router.get("/{matter_id}/oo-config")
async def oo_config(request: Request, matter_id: str, path: str = ""):
    """Return JWT-signed OnlyOffice editor config for a document."""
    import jwt as pyjwt

    tid = _tid(request)
    if not path:
        raise HTTPException(status_code=400, detail="path required")

    # Resolve the absolute file path
    root, _ = await _resolve_root(tid, matter_id)
    if root:
        fp = _safe_path(root, path)
    else:
        # Path might be absolute (e.g. from chats/ staging area)
        fp = path
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="File not found")

    OO_SECRET = "praesidium-oo-jwt-2026"
    filename = os.path.basename(fp)
    doc_key = hashlib.md5(f"{fp}:{os.path.getmtime(fp)}".encode()).hexdigest()[:20]
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "docx"
    doc_type = "word"
    if ext in ("xls", "xlsx", "csv"): doc_type = "cell"
    elif ext in ("ppt", "pptx"): doc_type = "slide"

    import urllib.parse as _up
    encoded_path = _up.quote(fp, safe="")

    # Internal URL — OnlyOffice container fetches from praesidium-web via Docker network
    doc_url = f"http://172.28.0.5:8000/api/v1/dms/matter/editor/download?path={encoded_path}"
    callback_url = f"http://172.28.0.5:8000/api/v1/dms/matter/editor/callback?path={encoded_path}"

    config = {
        "document": {
            "fileType": ext,
            "key": doc_key,
            "title": filename,
            "url": doc_url,
            "permissions": {"edit": True, "download": True, "print": True}
        },
        "documentType": doc_type,
        "editorConfig": {
            "mode": "edit",
            "callbackUrl": callback_url,
            "customization": {
                "autosave": True, "chat": False, "comments": True,
                "compactHeader": True, "compactToolbar": False,
                "feedback": False, "forcesave": True, "help": False,
            },
            "lang": "en",
        },
    }

    token = pyjwt.encode(config, OO_SECRET, algorithm="HS256")
    config["token"] = token

    host = request.headers.get("host", "localhost")
    api_url = f"https://{host}/oo/web-apps/apps/api/documents/api.js"

    return JSONResponse({"config": config, "api_url": api_url})


@router.get("/editor/download")
async def editor_download(request: Request, path: str = ""):
    """Serve raw document bytes for OnlyOffice to fetch internally."""
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or "application/octet-stream")


@router.post("/editor/callback")
async def editor_callback(request: Request, path: str = ""):
    """OnlyOffice save callback."""
    import urllib.request as _ur
    body = await request.json()
    if body.get("status") in (2, 6):
        url = body.get("url")
        if url and path:
            try:
                _ur.urlretrieve(url, path)
                logger.info("OnlyOffice saved: %s", path)
            except Exception as e:
                logger.error("OnlyOffice save failed: %s", e)
                return JSONResponse({"error": 1})
    return JSONResponse({"error": 0})


# ═══════════════════════════════════════════════════════════════════
# Native OnlyOffice endpoints (v5) — wired from oo_native_endpoints.py
# ═══════════════════════════════════════════════════════════════════

@router.post("/{matter_id}/oo-saveas")
async def _oo_saveas(request: Request, matter_id: str):
    return await oo_saveas(request, matter_id)

@router.get("/{matter_id}/oo-history")
async def _oo_history(request: Request, matter_id: str, path: str = ""):
    return await oo_history(request, matter_id, path)

@router.get("/{matter_id}/oo-history-data")
async def _oo_history_data(request: Request, matter_id: str, path: str = "", version: int = 1):
    return await oo_history_data(request, matter_id, path, version)

@router.post("/{matter_id}/oo-restore")
async def _oo_restore(request: Request, matter_id: str):
    return await oo_restore(request, matter_id)


# --- CREATE BLANK DOC ---
from pydantic import BaseModel as _BM2
class _CreateBlankDocReq(_BM2):
    filename: str = "New Document.docx"
    folder: str = ""

@router.post("/matter/{matter_id}/create-blank-doc")
async def create_blank_doc(matter_id: str, req: _CreateBlankDocReq, request: Request):
    """Create a blank Word document in the matter folder and return its path."""
    tid = _tid(request)
    disk_root = await _get_disk_root(matter_id, tid)
    if not disk_root:
        raise HTTPException(404, "Matter has no disk root")

    import os
    from docx import Document as DocxDocument

    folder_path = os.path.join(disk_root, req.folder) if req.folder else disk_root
    os.makedirs(folder_path, exist_ok=True)

    # Avoid overwriting — append (1), (2) etc if file exists
    fname = req.filename
    base, ext = os.path.splitext(fname)
    full_path = os.path.join(folder_path, fname)
    counter = 1
    while os.path.exists(full_path):
        fname = f"{base} ({counter}){ext}"
        full_path = os.path.join(folder_path, fname)
        counter += 1

    # Create minimal docx
    doc = DocxDocument()
    doc.add_paragraph("")
    doc.save(full_path)

    # Compute relative path from disk_root
    rel = os.path.relpath(full_path, disk_root)

    # Insert documents row
    import hashlib, uuid as _uuid
    from datetime import datetime, timezone
    sha = hashlib.sha256(open(full_path, "rb").read()).hexdigest()
    fsize = os.path.getsize(full_path)
    doc_id = str(_uuid.uuid4())

    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO documents (id, tenant_id, matter_id, storage_path, file_name, file_size, checksum, version_number, created_at)
            VALUES (:id, :tid, CAST(:mid AS uuid), :spath, :fname, :fsize, :sha, 1, :now)
            ON CONFLICT DO NOTHING
        """), {"id": doc_id, "tid": tid, "mid": matter_id, "spath": full_path, "fname": fname, "fsize": fsize, "sha": sha, "now": datetime.now(timezone.utc)})
        await session.commit()

    return JSONResponse({"filename": fname, "path": full_path, "relative_path": rel, "document_id": doc_id})



# ── Rename file ──────────────────────────────────────────────────────────
@router.post("/{matter_id}/rename")
async def matter_rename(request: Request, matter_id: str):
    tid = _tid(request)
    body = await request.json()
    old_name = body.get("old_name", "")
    new_name = body.get("new_name", "")
    folder_path = body.get("folder_path") or body.get("folder") or body.get("parent_path") or ""
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name required")
    if "/" in new_name or chr(92) in new_name:
        raise HTTPException(status_code=400, detail="new_name must not contain path separators")
    # Gate: extension must not change
    old_ext = old_name.rsplit('.', 1)[-1].lower() if '.' in old_name else ''
    new_ext = new_name.rsplit('.', 1)[-1].lower() if '.' in new_name else ''
    if old_ext != new_ext:
        raise HTTPException(status_code=400, detail=f"Cannot change file extension (.{old_ext} -> .{new_ext}). Rename the file name only.")
    root, _ = await _resolve_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")
    folder_abs = _safe_path(root, folder_path) if folder_path else root
    old_path = os.path.join(folder_abs, old_name)
    new_path = os.path.join(folder_abs, new_name)
    if not os.path.isfile(old_path):
        raise HTTPException(status_code=404, detail="File not found: " + old_name)
    if os.path.exists(new_path):
        raise HTTPException(status_code=409, detail="File already exists: " + new_name)
    try:
        os.rename(old_path, new_path)
    except Exception as e:
        logger.error("Rename failed: %s", e)
        raise HTTPException(status_code=500, detail="Rename failed: " + str(e))
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "UPDATE documents SET storage_path = :new_path, file_name = :new_name "
                "WHERE storage_path = :old_path AND TRIM(tenant_id) = :tid"
            ), {"new_path": new_path, "new_name": new_name, "old_path": old_path, "tid": tid})
            await session.execute(sa_text(
                "UPDATE dms_documents SET file_path = :new_path, file_name = :new_name "
                "WHERE file_path = :old_path AND TRIM(tenant_id) = :tid"
            ), {"new_path": new_path, "new_name": new_name, "old_path": old_path, "tid": tid})
            await session.commit()
    except Exception as e:
        logger.warning("Rename DB update non-fatal: %s", e)
    return JSONResponse({"status": "ok", "old_name": old_name, "new_name": new_name})


# ── Rename folder ────────────────────────────────────────────────────────
@router.post("/{matter_id}/rename-folder")
async def matter_rename_folder(request: Request, matter_id: str):
    tid = _tid(request)
    body = await request.json()
    old_name = body.get("old_name", "")
    new_name = body.get("new_name", "")
    parent_path = body.get("parent_path", "")
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old_name and new_name required")
    if "/" in new_name or chr(92) in new_name:
        raise HTTPException(status_code=400, detail="new_name must not contain path separators")
    # Gate: extension must not change
    old_ext = old_name.rsplit('.', 1)[-1].lower() if '.' in old_name else ''
    new_ext = new_name.rsplit('.', 1)[-1].lower() if '.' in new_name else ''
    if old_ext != new_ext:
        raise HTTPException(status_code=400, detail=f"Cannot change file extension (.{old_ext} -> .{new_ext}). Rename the file name only.")
    root, _ = await _resolve_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")
    parent_abs = _safe_path(root, parent_path) if parent_path else root
    old_folder = os.path.join(parent_abs, old_name)
    new_folder = os.path.join(parent_abs, new_name)
    if not os.path.isdir(old_folder):
        raise HTTPException(status_code=404, detail="Folder not found: " + old_name)
    if os.path.exists(new_folder):
        raise HTTPException(status_code=409, detail="Folder already exists: " + new_name)
    try:
        os.rename(old_folder, new_folder)
    except Exception as e:
        logger.error("Folder rename failed: %s", e)
        raise HTTPException(status_code=500, detail="Folder rename failed: " + str(e))
    old_prefix = old_folder + "/"
    new_prefix = new_folder + "/"
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "UPDATE documents SET storage_path = :np || substring(storage_path from :pl + 1) "
                "WHERE storage_path LIKE :ol AND TRIM(tenant_id) = :tid"
            ), {"np": new_prefix.rstrip("/"), "pl": len(old_prefix) - 1, "ol": old_prefix + "%", "tid": tid})
            await session.execute(sa_text(
                "UPDATE dms_documents SET file_path = :np || substring(file_path from :pl + 1) "
                "WHERE file_path LIKE :ol AND TRIM(tenant_id) = :tid"
            ), {"np": new_prefix.rstrip("/"), "pl": len(old_prefix) - 1, "ol": old_prefix + "%", "tid": tid})
            await session.execute(sa_text(
                "UPDATE documents SET storage_path = :nf WHERE storage_path = :of AND TRIM(tenant_id) = :tid"
            ), {"nf": new_folder, "of": old_folder, "tid": tid})
            await session.execute(sa_text(
                "UPDATE dms_documents SET file_path = :nf WHERE file_path = :of AND TRIM(tenant_id) = :tid"
            ), {"nf": new_folder, "of": old_folder, "tid": tid})
            await session.commit()
    except Exception as e:
        logger.warning("Folder rename DB update non-fatal: %s", e)
    return JSONResponse({"status": "ok", "old_name": old_name, "new_name": new_name})


# ── Create blank document (correct path) ─────────────────────────────────
@router.post("/{matter_id}/create-blank-doc")
async def create_blank_doc_fixed(request: Request, matter_id: str):
    tid = _tid(request)
    body = await request.json()
    folder_path = body.get("folder_path", "")
    filename = body.get("filename", "New Document.docx")
    if not filename.endswith(".docx"):
        filename += ".docx"
    root, _ = await _resolve_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")
    folder_abs = _safe_path(root, folder_path) if folder_path else root
    if not os.path.isdir(folder_abs):
        os.makedirs(folder_abs, exist_ok=True)
    target = os.path.join(folder_abs, filename)
    if os.path.exists(target):
        base, ext = os.path.splitext(filename)
        i = 1
        while os.path.exists(target):
            target = os.path.join(folder_abs, base + " (" + str(i) + ")" + ext)
            i += 1
        filename = os.path.basename(target)
    try:
        from docx import Document as DocxDocument
        doc = DocxDocument()
        doc.save(target)
    except Exception as e:
        logger.error("Create blank doc failed: %s", e)
        raise HTTPException(status_code=500, detail="Create failed: " + str(e))
    doc_id = None
    try:
        import uuid as _uuid
        doc_id = str(_uuid.uuid4())
        file_hash = hashlib.sha256(open(target, "rb").read()).hexdigest()
        file_size = os.path.getsize(target)
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(
                "INSERT INTO documents (id, tenant_id, file_name, storage_path, file_size, "
                "checksum, version_number, created_at, updated_at) "
                "VALUES (CAST(:id AS uuid), :tid, :fname, :spath, :fsize, :chk, 1, NOW(), NOW())"
            ), {"id": doc_id, "tid": tid, "fname": filename, "spath": target,
                "fsize": file_size, "chk": file_hash})
            await session.commit()
    except Exception as e:
        logger.warning("Create blank doc DB insert non-fatal: %s", e)
    rel_path = os.path.relpath(target, root)
    return JSONResponse({"filename": filename, "relative_path": rel_path, "document_id": doc_id})


# ── Delete file(s) ───────────────────────────────────────────────────────
@router.post("/{matter_id}/delete")
async def matter_delete(request: Request, matter_id: str):
    """Delete a file from the matter disk folder.
    Also removes the corresponding documents table record if one exists.
    Body: { "path": "relative/path/to/file" }
    """
    tid = _tid(request)
    body = await request.json()
    rel_path = body.get("path", "")
    if not rel_path:
        raise HTTPException(status_code=400, detail="path required")
    root, _ = await _resolve_root(tid, matter_id)
    if not root:
        raise HTTPException(status_code=404, detail="No disk root")
    fp = _safe_path(root, rel_path)
    if not os.path.isfile(fp):
        raise HTTPException(status_code=404, detail="File not found")
    # Remove DB record if exists
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                DELETE FROM documents
                WHERE storage_path = :sp AND TRIM(tenant_id) = :tid
            """), {"sp": fp, "tid": tid})
            await session.commit()
    except Exception as e:
        logger.warning("DB delete (non-fatal): %s", e)
    # Remove from disk
    try:
        os.remove(fp)
    except Exception as e:
        logger.error("Disk delete failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Delete failed: {e}")
    logger.info("Deleted file: %s", fp)
    return JSONResponse({"status": "ok", "deleted": rel_path})
