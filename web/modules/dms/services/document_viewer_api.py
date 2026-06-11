"""
modules/dms/services/document_viewer_api.py — Universal Document Viewer/Editor API

Serves preview and download for any file under /mnt/praesidium/{tenant}/.
Supports matter files, chat staging, eDiscovery, deal rooms, etc.

Security: validates that the resolved path is under /mnt/praesidium/{tenant_id}/
and the requesting user belongs to that tenant.

Routes:
  GET /api/v1/viewer/preview?path=<abs_or_rel_path>   — serve file for preview (inline)
  GET /api/v1/viewer/download?path=<abs_or_rel_path>   — serve file for download
  GET /api/v1/viewer/info?path=<abs_or_rel_path>       — file metadata (size, mime, pages)

Patent Pending — Series 1/2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import mimetypes
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

log = logging.getLogger("praesidium.document_viewer")

router = APIRouter(prefix="/api/v1/viewer", tags=["document-viewer"])

PRAESIDIUM_ROOT = "/mnt/praesidium"

# Mime types for inline preview vs download
PREVIEWABLE_MIMES = {
    "application/pdf",
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp", "image/tiff",
    "text/plain", "text/html", "text/csv",
}

# Office types that can be converted to PDF for preview
OFFICE_EXTENSIONS = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp", ".rtf"}


def _tid(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()


def _resolve_path(raw_path: str, tenant_id: str) -> Path:
    """Resolve and validate a file path. Must be under /mnt/praesidium/{tenant_id}/."""
    if not raw_path:
        raise HTTPException(400, "path is required")

    # If path is already absolute
    if raw_path.startswith("/"):
        resolved = Path(raw_path).resolve()
    else:
        # Relative path — prefix with tenant root
        resolved = Path(PRAESIDIUM_ROOT, tenant_id, raw_path).resolve()

    # Security: must be under /mnt/praesidium/{tenant_id}/
    tenant_root = Path(PRAESIDIUM_ROOT, tenant_id).resolve()
    if not str(resolved).startswith(str(tenant_root) + "/") and resolved != tenant_root:
        raise HTTPException(403, "Path outside tenant scope")

    if not resolved.is_file():
        raise HTTPException(404, f"File not found: {resolved.name}")

    return resolved


def _guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if not mime:
        ext = path.suffix.lower()
        ext_map = {
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".msg": "application/vnd.ms-outlook",
            ".eml": "message/rfc822",
        }
        mime = ext_map.get(ext, "application/octet-stream")
    return mime






def _render_eml_as_html(src: Path) -> str | None:
    """Parse EML or MSG and return styled HTML."""
    import html as html_mod
    ext = src.suffix.lower()
    subj = "(No Subject)"
    from_addr = to_addr = cc_addr = date_str = ""
    body_text = body_html = ""
    att_names = []
    try:
        if ext == ".msg":
            try:
                import extract_msg
                msg = extract_msg.openMsg(str(src))
                subj = msg.subject or "(No Subject)"
                from_addr = msg.sender or ""
                to_addr = msg.to or ""
                cc_addr = msg.cc or ""
                date_str = str(msg.date) if msg.date else ""
                bh = msg.htmlBody
                if isinstance(bh, bytes): bh = bh.decode("utf-8", errors="replace")
                body_html = bh or ""
                body_text = msg.body or ""
                for att in (msg.attachments or []):
                    n = getattr(att, "longFilename", None) or getattr(att, "shortFilename", None) or "file"
                    att_names.append(n)
                msg.close()
            except ImportError:
                return "<html><body><h3>extract-msg not installed</h3></body></html>"
            except Exception as e2:
                return "<html><body><h3>MSG error</h3><p>" + str(e2) + "</p></body></html>"
        else:
            import email as email_lib
            from email import policy
            with open(str(src), "rb") as f:
                msg = email_lib.message_from_binary_file(f, policy=policy.default)
            subj = msg.get("Subject", "(No Subject)")
            from_addr = msg.get("From", "")
            to_addr = msg.get("To", "")
            cc_addr = msg.get("Cc", "")
            date_str = msg.get("Date", "")
            if msg.is_multipart():
                for part in msg.walk():
                    ct = part.get_content_type()
                    fn = part.get_filename()
                    if fn: att_names.append(fn)
                    elif ct == "text/html" and not body_html:
                        try: body_html = part.get_content()
                        except: pass
                    elif ct == "text/plain" and not body_text:
                        try: body_text = part.get_content()
                        except: pass
            else:
                ct = msg.get_content_type()
                try: content = msg.get_content()
                except: content = ""
                if ct == "text/html": body_html = content
                else: body_text = content
        esc = html_mod.escape
        css = "body{font-family:-apple-system,sans-serif;margin:0;padding:0;color:#1f2937;background:#fff}"
        css += ".eh{background:#f8fafc;padding:16px 20px;border-bottom:1px solid #e2e8f0}"
        css += ".hr{margin:3px 0;font-size:13px;line-height:1.5}"
        css += ".hl{font-weight:600;color:#64748b;display:inline-block;width:55px;font-size:11px;text-transform:uppercase}"
        css += ".hs{font-size:16px;font-weight:600;color:#0f172a;margin-bottom:8px}"
        css += ".eb{padding:20px;font-size:14px;line-height:1.7}.eb img{max-width:100%;height:auto}.eb a{color:#2563eb}"
        css += ".ab{padding:10px 20px;border-top:1px solid #e2e8f0;background:#f8fafc;font-size:12px;color:#64748b}"
        css += ".ap{background:#e0f2fe;color:#0369a1;padding:2px 8px;border-radius:4px;margin:0 4px 2px 0;font-size:11px;display:inline-block}"
        css += "pre{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;margin:0}"
        o = '<!DOCTYPE html><html><head><meta charset="utf-8"><style>' + css + '</style></head><body>'
        o += '<div class="eh">'
        o += '<div class="hs">' + esc(str(subj)) + '</div>'
        o += '<div class="hr"><span class="hl">From</span> ' + esc(str(from_addr)) + '</div>'
        o += '<div class="hr"><span class="hl">To</span> ' + esc(str(to_addr)) + '</div>'
        if cc_addr: o += '<div class="hr"><span class="hl">CC</span> ' + esc(str(cc_addr)) + '</div>'
        o += '<div class="hr"><span class="hl">Date</span> ' + esc(str(date_str)) + '</div>'
        o += '</div><div class="eb">'
        if body_html:
            if isinstance(body_html, bytes): body_html = body_html.decode("utf-8", errors="replace")
            o += body_html
        elif body_text:
            o += "<pre>" + esc(str(body_text)) + "</pre>"
        else:
            o += '<p style="color:#999;font-style:italic;">No message body</p>'
        o += "</div>"
        if att_names:
            o += '<div class="ab">Attachments: '
            for a in att_names: o += '<span class="ap">' + esc(str(a)) + "</span>"
            o += "</div>"
        o += "</body></html>"
        return o
    except Exception as e:
        log.warning("Email render error for %s: %s", src.name, e)
        return "<html><body><h3>Render error</h3><p>" + str(e) + "</p></body></html>"


def _convert_to_pdf(src: Path) -> Path | None:
    """Convert an Office document to PDF for preview. Returns PDF path or None."""
    pdf_path = src.with_suffix(".pdf")
    # Check if PDF already exists and is newer than source
    if pdf_path.exists() and pdf_path.stat().st_mtime >= src.stat().st_mtime:
        return pdf_path
    try:
        result = subprocess.run(
            ["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(src.parent), str(src)],
            timeout=60, capture_output=True, text=True
        )
        if pdf_path.exists():
            return pdf_path
        log.warning("LibreOffice conversion failed for %s: %s", src.name, result.stderr)
    except Exception as e:
        log.warning("LibreOffice conversion error for %s: %s", src.name, e)
    return None


@router.get("/preview")
async def preview_file(request: Request, path: str = ""):
    """Serve a file for inline preview. Converts Office docs to PDF."""
    tid = _tid(request)
    resolved = _resolve_path(path, tid)
    mime = _guess_mime(resolved)
    ext = resolved.suffix.lower()

    # PDF and images: serve directly
    if mime in PREVIEWABLE_MIMES:
        return FileResponse(
            str(resolved), media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{resolved.name}"'}
        )

    # Office docs: convert to PDF for preview
    if ext in OFFICE_EXTENSIONS:
        pdf = _convert_to_pdf(resolved)
        if pdf:
            return FileResponse(
                str(pdf), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{pdf.name}"'}
            )
        # Fallback: force download
        return FileResponse(
            str(resolved), media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'}
        )

    # EML/MSG: render as HTML
    if ext in {".eml", ".msg"}:
        html_content = _render_eml_as_html(resolved)
        if html_content:
            return Response(content=html_content, media_type="text/html",
                headers={"Content-Disposition": f'inline; filename="{resolved.stem}.html"'})

    # Everything else: force download
    return FileResponse(
        str(resolved), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'}
    )


@router.get("/download")
async def download_file(request: Request, path: str = ""):
    """Serve a file for download (attachment)."""
    tid = _tid(request)
    resolved = _resolve_path(path, tid)
    mime = _guess_mime(resolved)
    return FileResponse(
        str(resolved), media_type=mime,
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}"'}
    )


@router.get("/info")
async def file_info(request: Request, path: str = ""):
    """Return metadata about a file."""
    tid = _tid(request)
    resolved = _resolve_path(path, tid)
    stat = resolved.stat()
    mime = _guess_mime(resolved)

    info = {
        "filename": resolved.name,
        "path": str(resolved),
        "size": stat.st_size,
        "mime_type": mime,
        "modified": stat.st_mtime,
        "extension": resolved.suffix.lower(),
    }

    # Count PDF pages if applicable
    if mime == "application/pdf":
        try:
            from pikepdf import Pdf
            with Pdf.open(str(resolved)) as pdf:
                info["page_count"] = len(pdf.pages)
        except Exception:
            try:
                from PyPDF2 import PdfReader
                reader = PdfReader(str(resolved))
                info["page_count"] = len(reader.pages)
            except Exception:
                pass

    return JSONResponse(info)
