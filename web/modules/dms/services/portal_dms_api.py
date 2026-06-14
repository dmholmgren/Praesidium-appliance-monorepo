"""
Portal DMS browse API — v1 (2026-06-14).

Document browsing for EXTERNAL portal users (client + co_counsel), served under
/api/portal/* so the magic-link middleware confinement permits it.

Enforcement (portal users have tenant_id = their sub-tenant; the matter + files
live under the PARENT/firm tenant, so we resolve against the matter's own tenant
and verify the grant here):
  * co_counsel : grant via external_user_scopes(scope_type='matter') — FULL access
  * client     : grant via sub_tenant_matter_scope + matching portal_access_client_id,
                 folder-filtered by portal_folder_scope (DEFAULT_EXCLUDED hidden)

Reuses the disk-walk / preview helpers from matter_workspace_api.
"""
from __future__ import annotations
import os, logging, mimetypes, re
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal
from modules.dms.services.matter_workspace_api import (
    PRAESIDIUM_ROOT, NATIVE_PREVIEW, OFFICE_EXT, EMAIL_EXT,
    _walk_tree, _list_files, _safe_path, _convert_to_pdf, _render_email_html,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/portal/matter", tags=["portal-dms"])

# Folders hidden by default from CLIENT portal users (co_counsel see everything).
DEFAULT_EXCLUDED = {"10-Working Docs", "04-Research", "11-eDiscovery",
                    "08-Working Docs", "14-Trial Preparation", "15-Email"}


def _ip(request: Request) -> str:
    return (request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))[:50]


async def _log(db, tid, uid, action, request, details=None):
    import json as _json
    try:
        await db.execute(sa_text(
            "INSERT INTO user_activity_log (tenant_id, user_id, action, details, ip_address, user_agent) "
            "VALUES (:t,:u,:a,CAST(:d AS jsonb),:ip,:ua)"),
            {"t": tid, "u": uid, "a": action, "d": _json.dumps(details or {}),
             "ip": _ip(request), "ua": (request.headers.get("user-agent") or "")[:500]})
    except Exception as e:
        logger.warning("portal activity log failed (non-fatal): %s", e)


async def _access(request: Request, matter_id: str):
    """
    Authorize the portal user for matter_id. Returns dict with root, role,
    folder_map (client only), uid, tid — or None if denied.
    """
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "auth_provider", "") != "magic_link":
        return None
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    uid = user.id
    async with AsyncSessionLocal() as db:
        # role + client binding straight from the DB (don't trust ORM attrs)
        ur = await db.execute(sa_text(
            "SELECT role, portal_access_client_id FROM users WHERE id = :u"), {"u": uid})
        urow = ur.mappings().fetchone()
        if not urow:
            return None
        role = (urow["role"] or "").strip()

        mr = await db.execute(sa_text("""
            SELECT TRIM(m.tenant_id) AS mtid, m.matter_name, m.client_id::text AS client_id,
                   c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND TRIM(m.tenant_id) = TRIM(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid)
        """), {"mid": matter_id})
        m = mr.mappings().fetchone()
        if not m:
            return None

        folder_map = None
        if role == "co_counsel":
            g = await db.execute(sa_text("""
                SELECT 1 FROM external_user_scopes
                 WHERE TRIM(tenant_id) = :tid AND user_id = :u AND scope_type = 'matter'
                   AND scope_id = :mid AND is_active = TRUE
                   AND (expires_at IS NULL OR expires_at > NOW()) LIMIT 1
            """), {"tid": tid, "u": uid, "mid": matter_id})
            if not g.fetchone():
                return None
        else:  # client
            if not m["client_id"] or str(urow["portal_access_client_id"]) != m["client_id"]:
                return None
            sc = await db.execute(sa_text("""
                SELECT id FROM sub_tenant_matter_scope
                 WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid)
                   AND revoked_at IS NULL LIMIT 1
            """), {"tid": tid, "mid": matter_id})
            srow = sc.fetchone()
            if not srow:
                return None
            fr = await db.execute(sa_text(
                "SELECT folder_key, is_visible FROM portal_folder_scope WHERE scope_id = :s"),
                {"s": srow.id})
            folder_map = {r["folder_key"]: r["is_visible"] for r in fr.mappings()}

    root = os.path.join(PRAESIDIUM_ROOT, m["mtid"], "matters",
                        m["client_name"] or "", m["matter_name"] or "")
    if not os.path.isdir(root):
        root = None
    return {"root": root, "role": role, "folder_map": folder_map,
            "uid": uid, "tid": tid, "matter_name": m["matter_name"]}


def _folder_visible(name, ctx):
    if ctx["role"] == "co_counsel" or ctx["folder_map"] is None:
        return True
    fm = ctx["folder_map"]
    return fm.get(name, name not in DEFAULT_EXCLUDED)


def _path_top(path):
    return (path or "").replace("\\", "/").lstrip("/").split("/")[0]


@router.get("/{matter_id}/tree")
async def tree(request: Request, matter_id: str):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    if not ctx["root"]:
        return JSONResponse({"tree": [], "root_file_count": 0, "matter_name": ctx["matter_name"]})
    full = _walk_tree(ctx["root"])
    visible = [node for node in full if _folder_visible(node["name"], ctx)]
    try:
        rfc = sum(1 for f in os.scandir(ctx["root"]) if f.is_file() and not f.name.startswith('.'))
    except Exception:
        rfc = 0
    return JSONResponse({"tree": visible, "root_file_count": rfc, "matter_name": ctx["matter_name"]})


@router.get("/{matter_id}/files")
async def files(request: Request, matter_id: str, path: str = ""):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    if not ctx["root"]:
        return JSONResponse({"files": [], "folder_name": "", "path": path})
    if path and not _folder_visible(_path_top(path), ctx):
        raise HTTPException(403, "Folder not available")
    target = _safe_path(ctx["root"], path) if path else ctx["root"]
    flist = _list_files(target)
    return JSONResponse({"files": flist,
                         "folder_name": os.path.basename(target) if path else "Documents",
                         "path": path})


@router.get("/{matter_id}/preview")
async def preview(request: Request, matter_id: str, path: str = ""):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    if not path or not ctx["root"]:
        raise HTTPException(400, "path required")
    if not _folder_visible(_path_top(path), ctx):
        raise HTTPException(403, "Folder not available")
    fp = _safe_path(ctx["root"], path)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")
    ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''
    async with AsyncSessionLocal() as db:
        await _log(db, ctx["tid"], ctx["uid"], "document_view", request,
                   {"matter_id": matter_id, "file": os.path.basename(fp)})
        await db.commit()
    if ext in NATIVE_PREVIEW:
        mime = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        safe = os.path.basename(fp).replace('"', '').replace("'", "")
        return FileResponse(fp, media_type=mime, headers={
            "Content-Disposition": f'inline; filename="{safe}"',
            "Content-Type": mime, "X-Content-Type-Options": "nosniff"})
    if ext in OFFICE_EXT:
        pdf = _convert_to_pdf(fp)
        if pdf and os.path.isfile(pdf):
            safe = re.sub(r'[^a-zA-Z0-9._-]', '_', os.path.basename(fp)) + ".pdf"
            return FileResponse(pdf, media_type="application/pdf", headers={
                "Content-Disposition": f'inline; filename="{safe}"', "Content-Type": "application/pdf"})
        dl = f"/api/portal/matter/{matter_id}/download?path={path}"
        return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{margin:0;display:flex;align-items:center;justify-content:center;height:100vh;
font-family:-apple-system,sans-serif;background:#f8fafc;color:#334155}}.c{{text-align:center;padding:32px}}
a{{display:inline-block;padding:8px 24px;background:#1d4ed8;color:#fff;text-decoration:none;border-radius:6px;
font-size:12px;font-weight:600}}</style></head><body><div class="c"><div style="font-size:48px">📄</div>
<h3>{os.path.basename(fp)}</h3><p style="font-size:12px;color:#64748b">Preview not available — download to view.</p>
<a href="{dl}" download>Download File</a></div></body></html>""")
    if ext in EMAIL_EXT:
        import urllib.parse as up
        att = f"/api/portal/matter/{matter_id}/email-attachment?path={up.quote(os.path.relpath(fp, ctx['root']))}"
        rendered = _render_email_html(fp, ext, att_base_url=att)
        if rendered:
            return HTMLResponse(rendered)
        raise HTTPException(500, "Email parse failed")
    raise HTTPException(415, f"No preview for .{ext}")


@router.get("/{matter_id}/download")
async def download(request: Request, matter_id: str, path: str = ""):
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    if not path or not ctx["root"]:
        raise HTTPException(400, "path required")
    if not _folder_visible(_path_top(path), ctx):
        raise HTTPException(403, "Folder not available")
    fp = _safe_path(ctx["root"], path)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")
    async with AsyncSessionLocal() as db:
        await _log(db, ctx["tid"], ctx["uid"], "document_download", request,
                   {"matter_id": matter_id, "file": os.path.basename(fp)})
        await db.commit()
    return FileResponse(fp, filename=os.path.basename(fp), media_type="application/octet-stream")


@router.get("/{matter_id}/email-attachment")
async def email_attachment(request: Request, matter_id: str, path: str = "", index: int = 0):
    """Stream an attachment from a .msg/.eml file (portal-scoped)."""
    ctx = await _access(request, matter_id)
    if ctx is None:
        raise HTTPException(403, "Access denied")
    if not path or not ctx["root"]:
        raise HTTPException(400, "path required")
    if not _folder_visible(_path_top(path), ctx):
        raise HTTPException(403, "Folder not available")
    fp = _safe_path(ctx["root"], path)
    if not os.path.isfile(fp):
        raise HTTPException(404, "File not found")
    ext = fp.rsplit('.', 1)[-1].lower() if '.' in fp else ''
    from starlette.responses import Response
    inline_exts = {"pdf", "jpg", "jpeg", "png", "gif", "webp", "bmp", "txt", "html", "htm"}
    if ext == "msg":
        try:
            import extract_msg
            msg = extract_msg.Message(fp)
            atts = msg.attachments or []
            if index < 0 or index >= len(atts):
                msg.close(); raise HTTPException(404, "Attachment not found")
            att = atts[index]; data = att.data
            if data is None:
                msg.close(); raise HTTPException(404, "Attachment has no data")
            filename = att.longFilename or att.shortFilename or f"attachment_{index}"
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            msg.close()
            ae = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            disp = "inline" if ae in inline_exts else "attachment"
            return Response(content=data, media_type=mime,
                            headers={"Content-Disposition": f'{disp}; filename="{filename}"'})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("portal MSG attachment failed %s[%d]: %s", fp, index, e)
            raise HTTPException(500, "Attachment extract failed")
    elif ext == "eml":
        try:
            import email as _email, email.policy
            with open(fp, "rb") as f:
                msg = _email.message_from_binary_file(f, policy=_email.policy.default)
            atts = list(msg.iter_attachments())
            if index < 0 or index >= len(atts):
                raise HTTPException(404, "Attachment not found")
            part = atts[index]; data = part.get_content()
            if isinstance(data, str):
                data = data.encode("utf-8")
            filename = part.get_filename() or f"attachment_{index}"
            mime = part.get_content_type() or "application/octet-stream"
            ae = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
            disp = "inline" if ae in inline_exts else "attachment"
            return Response(content=data, media_type=mime,
                            headers={"Content-Disposition": f'{disp}; filename="{filename}"'})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("portal EML attachment failed %s[%d]: %s", fp, index, e)
            raise HTTPException(500, "Attachment extract failed")
    raise HTTPException(415, "Not an email file")
