"""
modules/ediscovery/routes/share_public.py
==========================================
Public (no-auth) routes for production share links.
ShareFile-style: recipient must register name/email/firm before downloading.
All access is logged to production_share_access_log.

Routes:
  GET  /share/{token}           -- landing page (HTML)
  POST /share/{token}/register  -- register + get session cookie
  GET  /share/{token}/download  -- download ZIP (requires registration cookie)
"""

import logging
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["share-public"])

SHARE_COOKIE = "praesidium_share_session"


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _get_link(token: str):
    """Fetch share link + production info. Returns dict or None."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT sl.*, ps.name AS production_name, ps.export_path,
                   ps.start_bates, ps.end_bates, ps.doc_count, ps.page_count,
                   ps.status AS production_status,
                   m.matter_name
            FROM production_share_links sl
            JOIN production_sets ps ON ps.id = sl.production_set_id
            LEFT JOIN matters m ON m.id = ps.matter_id
            WHERE sl.token = :tok
        """), {"tok": token})
        row = r.mappings().fetchone()
        return dict(row) if row else None


def _link_valid(link: dict) -> tuple[bool, str]:
    """Check if link is valid. Returns (valid, reason)."""
    if link.get("is_revoked"):
        return False, "This share link has been revoked."
    if link.get("expires_at"):
        exp = link["expires_at"]
        if isinstance(exp, str):
            from datetime import datetime as _dt
            exp = _dt.fromisoformat(exp.replace("Z", "+00:00"))
        if exp.replace(tzinfo=None) < datetime.utcnow():
            return False, "This share link has expired."
    if link.get("max_downloads") and link.get("download_count", 0) >= link["max_downloads"]:
        return False, "Download limit reached for this link."
    return True, ""


async def _log_access(link_id, tenant_id, action, visitor_name=None,
                      visitor_email=None, visitor_firm=None,
                      ip_address=None, user_agent=None, metadata=None):
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO production_share_access_log
                (tenant_id, share_link_id, action, visitor_name, visitor_email,
                 visitor_firm, ip_address, user_agent, accessed_at, metadata_json)
            VALUES (:tid, CAST(:lid AS uuid), :act, :vn, :ve, :vf, :ip, :ua, now(),
                    CAST(:meta AS jsonb))
        """), {
            "tid": tenant_id, "lid": str(link_id), "act": action,
            "vn": visitor_name, "ve": visitor_email, "vf": visitor_firm,
            "ip": ip_address, "ua": user_agent,
            "meta": json.dumps(metadata) if metadata else None,
        })
        await session.commit()


def _render_landing(link: dict, error: str = "", registered: bool = False) -> str:
    """Render the ShareFile-style landing page."""
    production_name = link.get("production_name", "Production")
    matter_name = link.get("matter_name", "")
    doc_count = link.get("doc_count", 0)
    page_count = link.get("page_count", 0)
    bates = ""
    if link.get("start_bates") and link.get("end_bates"):
        bates = f"{link['start_bates']} – {link['end_bates']}"
    message = (link.get("message") or "").strip()
    sender_name = link.get("recipient_name", "")  # sender perspective

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Secure Document Delivery — Praesidium</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Source Sans 3', -apple-system, sans-serif; background: #f1f5f9; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; color: #1e293b; }}
.card {{ background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); max-width: 520px; width: 100%; overflow: hidden; }}
.header {{ background: linear-gradient(135deg, #0f2b5b 0%, #1a3f7a 100%); padding: 28px 32px 24px; color: #fff; }}
.header h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 4px; }}
.header .sub {{ font-size: 13px; opacity: 0.8; }}
.logo {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; }}
.logo svg {{ width: 28px; height: 28px; }}
.logo span {{ font-size: 16px; font-weight: 700; letter-spacing: 0.02em; }}
.body {{ padding: 28px 32px; }}
.meta {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; }}
.meta-item {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 14px; }}
.meta-item .label {{ font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: #64748b; margin-bottom: 2px; }}
.meta-item .value {{ font-size: 14px; font-weight: 600; color: #1e293b; }}
.message {{ background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 14px 16px; font-size: 13px; color: #92400e; margin-bottom: 20px; line-height: 1.5; }}
.form-group {{ margin-bottom: 14px; }}
.form-group label {{ font-size: 12px; font-weight: 600; color: #475569; display: block; margin-bottom: 4px; }}
.form-group input {{ width: 100%; padding: 10px 14px; border: 1px solid #cbd5e1; border-radius: 8px; font-size: 14px; font-family: inherit; outline: none; transition: border-color .15s; }}
.form-group input:focus {{ border-color: #1a3f7a; box-shadow: 0 0 0 3px rgba(26,63,122,0.1); }}
.required {{ color: #dc2626; }}
.btn {{ width: 100%; padding: 12px; background: linear-gradient(135deg, #0f2b5b 0%, #1a3f7a 100%); color: #fff; border: none; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; font-family: inherit; transition: opacity .15s; }}
.btn:hover {{ opacity: 0.9; }}
.btn:disabled {{ opacity: 0.5; cursor: default; }}
.btn-download {{ background: linear-gradient(135deg, #059669 0%, #047857 100%); }}
.error {{ background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; border-radius: 8px; padding: 10px 14px; font-size: 13px; margin-bottom: 16px; }}
.footer {{ padding: 16px 32px; border-top: 1px solid #e2e8f0; text-align: center; }}
.footer span {{ font-size: 11px; color: #94a3b8; }}
.shield {{ display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: #64748b; margin-top: 12px; }}
.shield svg {{ width: 14px; height: 14px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="logo">
      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2L3 7v10l9 5 9-5V7l-9-5z" stroke="currentColor" stroke-width="1.5" fill="rgba(255,255,255,0.1)"/>
        <path d="M12 8v8M8 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span>Praesidium</span>
    </div>
    <h1>Secure Document Delivery</h1>
    <div class="sub">{production_name}</div>
  </div>
  <div class="body">
    {f'<div class="error">{error}</div>' if error else ''}

    <div class="meta">
      <div class="meta-item">
        <div class="label">Documents</div>
        <div class="value">{doc_count}</div>
      </div>
      <div class="meta-item">
        <div class="label">Pages</div>
        <div class="value">{page_count}</div>
      </div>
      {'<div class="meta-item" style="grid-column:span 2"><div class="label">Bates Range</div><div class="value" style="font-family:ui-monospace,monospace;font-size:13px">' + bates + '</div></div>' if bates else ''}
    </div>

    {f'<div class="message">{message}</div>' if message else ''}

    {'<div id="download-section"><button class="btn btn-download" onclick="doDownload()">⬇ Download Production ZIP</button><div class="shield"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>All access is logged and tracked</div></div>' if registered else f"""
    <div id="register-form">
      <p style="font-size:13px;color:#475569;margin-bottom:16px;line-height:1.5">
        To access these documents, please verify your identity below. Your access will be logged for chain-of-custody purposes.
      </p>
      <div class="form-group">
        <label>Full Name <span class="required">*</span></label>
        <input type="text" id="reg-name" placeholder="Jane Smith" required>
      </div>
      <div class="form-group">
        <label>Email Address <span class="required">*</span></label>
        <input type="email" id="reg-email" value="{link.get('recipient_email','')}" placeholder="jane@lawfirm.com" required>
      </div>
      <div class="form-group">
        <label>Firm / Organization</label>
        <input type="text" id="reg-firm" placeholder="Smith & Associates LLP">
      </div>
      <button class="btn" id="reg-btn" onclick="doRegister()">Verify & Access Documents</button>
      <div class="shield">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Your access is tracked for legal chain-of-custody compliance
      </div>
    </div>
    """}
  </div>
  <div class="footer">
    <span>Delivered securely via Praesidium · {matter_name}</span>
  </div>
</div>

<script>
async function doRegister() {{
  const name = document.getElementById('reg-name').value.trim();
  const email = document.getElementById('reg-email').value.trim();
  const firm = document.getElementById('reg-firm').value.trim();
  if (!name || !email) {{ alert('Name and email are required.'); return; }}
  const btn = document.getElementById('reg-btn');
  btn.disabled = true; btn.textContent = 'Verifying…';
  try {{
    const r = await fetch(window.location.pathname + '/register', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ name, email, firm }})
    }});
    const d = await r.json();
    if (d.ok) {{ window.location.reload(); }}
    else {{ alert(d.error || 'Registration failed'); btn.disabled = false; btn.textContent = 'Verify & Access Documents'; }}
  }} catch(e) {{ alert('Error: ' + e); btn.disabled = false; btn.textContent = 'Verify & Access Documents'; }}
}}

async function doDownload() {{
  // Log the download, then redirect
  try {{
    await fetch(window.location.pathname + '/download-log', {{ method: 'POST' }});
  }} catch(e) {{}}
  window.location.href = window.location.pathname + '/download';
}}
</script>
</body>
</html>"""


def _render_error(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Praesidium</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Source Sans 3',sans-serif;background:#f1f5f9;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;color:#1e293b}}.card{{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:480px;width:100%;padding:40px;text-align:center}}h1{{font-size:20px;margin-bottom:8px;color:#991b1b}}p{{font-size:14px;color:#64748b;line-height:1.5}}</style>
</head><body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


@router.get("/share/{token}")
async def share_landing(request: Request, token: str):
    """Public landing page for a share link."""
    link = await _get_link(token)
    if not link:
        return HTMLResponse(_render_error("Link Not Found",
            "This share link does not exist or has been removed."), status_code=404)

    valid, reason = _link_valid(link)
    if not valid:
        return HTMLResponse(_render_error("Link Unavailable", reason), status_code=403)

    # Check if already registered (cookie)
    registered = False
    cookie_val = request.cookies.get(SHARE_COOKIE, "")
    if cookie_val and cookie_val.startswith(token[:16]):
        registered = True
        # Log view
        await _log_access(
            link["id"], link["tenant_id"], "viewed",
            ip_address=_client_ip(request),
            user_agent=str(request.headers.get("user-agent", ""))[:500],
        )

    html = _render_landing(link, registered=registered)
    return HTMLResponse(html)


@router.post("/share/{token}/register")
async def share_register(request: Request, token: str):
    """Register visitor for share link access. Sets session cookie."""
    link = await _get_link(token)
    if not link:
        return JSONResponse({"error": "Link not found"}, 404)

    valid, reason = _link_valid(link)
    if not valid:
        return JSONResponse({"error": reason}, 403)

    body = await request.json()
    visitor_name = (body.get("name") or "").strip()
    visitor_email = (body.get("email") or "").strip()
    visitor_firm = (body.get("firm") or "").strip()

    if not visitor_name or not visitor_email:
        return JSONResponse({"error": "Name and email required"}, 400)

    # Log registration
    await _log_access(
        link["id"], link["tenant_id"], "registered",
        visitor_name=visitor_name, visitor_email=visitor_email,
        visitor_firm=visitor_firm,
        ip_address=_client_ip(request),
        user_agent=str(request.headers.get("user-agent", ""))[:500],
    )

    # Set cookie
    cookie_value = token[:16] + ":" + hashlib.sha256(
        f"{visitor_email}:{token}".encode()
    ).hexdigest()[:16]

    response = JSONResponse({"ok": True})
    response.set_cookie(
        SHARE_COOKIE, cookie_value,
        max_age=60 * 60 * 24 * 90,  # 90 days
        httponly=True, samesite="lax",
        secure=str(request.url).startswith("https"),
    )
    return response


@router.get("/share/{token}/download")
async def share_download(request: Request, token: str):
    """Download the production ZIP. Requires registration cookie."""
    link = await _get_link(token)
    if not link:
        return HTMLResponse(_render_error("Link Not Found", "Link does not exist."), 404)

    valid, reason = _link_valid(link)
    if not valid:
        return HTMLResponse(_render_error("Link Unavailable", reason), 403)

    # Check registration cookie
    if link.get("require_registration"):
        cookie_val = request.cookies.get(SHARE_COOKIE, "")
        if not cookie_val or not cookie_val.startswith(token[:16]):
            return HTMLResponse(_render_error("Registration Required",
                "Please register before downloading. Go back and complete the form."), 403)

    export_path = link.get("export_path") or ""
    if not export_path or not Path(export_path).exists():
        return HTMLResponse(_render_error("File Not Available",
            "The production export file is not yet available. Please try again later."), 404)

    # Log download
    await _log_access(
        link["id"], link["tenant_id"], "downloaded",
        ip_address=_client_ip(request),
        user_agent=str(request.headers.get("user-agent", ""))[:500],
    )

    # Increment download counter
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE production_share_links
            SET download_count = download_count + 1, updated_at = now()
            WHERE id = CAST(:lid AS uuid)
        """), {"lid": str(link["id"])})
        await session.commit()

    filename = (link.get("production_name") or "Production").replace(" ", "_") + ".zip"
    return FileResponse(export_path, filename=filename, media_type="application/zip")


@router.post("/share/{token}/download-log")
async def share_download_log(request: Request, token: str):
    """Log a download attempt (called before redirect to actual download)."""
    link = await _get_link(token)
    if link:
        await _log_access(
            link["id"], link["tenant_id"], "download_initiated",
            ip_address=_client_ip(request),
            user_agent=str(request.headers.get("user-agent", ""))[:500],
        )
    return JSONResponse({"ok": True})
