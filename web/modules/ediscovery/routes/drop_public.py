"""
modules/ediscovery/routes/drop_public.py
=========================================
Public (no-auth) routes for client file drop links.
Persistent Dropbox-style: client sees all previously uploaded files,
can keep adding more. Each upload batch creates a new ingest job.

Routes:
  GET  /drop/{token}           -- branded landing + persistent file inventory
  POST /drop/{token}/register  -- register name/email before uploading
  POST /drop/{token}/upload    -- accept file uploads (requires registration cookie)
  GET  /drop/{token}/files     -- JSON list of all uploaded files for this link
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["drop-public"])

DROP_COOKIE = "praesidium_drop_session"
EDISCOVERY_ROOT = os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery")


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _get_link(token: str):
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT dl.*,
                   m.matter_name, m.matter_number
            FROM client_drop_links dl
            JOIN matters m ON m.id = dl.matter_id
            WHERE dl.token = :tok
        """), {"tok": token})
        row = r.mappings().fetchone()
        return dict(row) if row else None


def _link_valid(link: dict) -> tuple:
    if link.get("is_revoked"):
        return False, "This upload link has been revoked."
    if not link.get("is_active"):
        return False, "This upload link is no longer active."
    if link.get("expires_at"):
        exp = link["expires_at"]
        if hasattr(exp, "replace") and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            return False, "This upload link has expired."
    return True, ""


async def _log_access(drop_link_id, tenant_id, action, **kwargs):
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO client_drop_access_log
                (drop_link_id, tenant_id, action, visitor_name, visitor_email,
                 visitor_firm, ip_address, user_agent, file_names, file_count,
                 total_bytes, metadata_json, accessed_at)
            VALUES (CAST(:lid AS uuid), :tid, :act, :vn, :ve, :vf, :ip, :ua,
                    :fnames, :fcnt, :tbytes, CAST(:meta AS jsonb), now())
        """), {
            "lid": str(drop_link_id), "tid": tenant_id, "act": action,
            "vn": kwargs.get("visitor_name"), "ve": kwargs.get("visitor_email"),
            "vf": kwargs.get("visitor_firm"), "ip": kwargs.get("ip_address"),
            "ua": kwargs.get("user_agent"), "fnames": kwargs.get("file_names"),
            "fcnt": kwargs.get("file_count"), "tbytes": kwargs.get("total_bytes"),
            "meta": json.dumps(kwargs.get("metadata")) if kwargs.get("metadata") else None,
        })
        await session.commit()


async def _get_upload_history(drop_link_id: str):
    """Return list of all upload batches for this drop link."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT visitor_name, visitor_email, file_names,
                   file_count, total_bytes, accessed_at
            FROM client_drop_access_log
            WHERE drop_link_id = CAST(:lid AS uuid)
              AND action = 'uploaded'
            ORDER BY accessed_at DESC
        """), {"lid": str(drop_link_id)})
        rows = r.mappings().fetchall()
    batches = []
    for row in rows:
        fnames = []
        if row["file_names"]:
            try:
                fnames = json.loads(row["file_names"])
            except Exception:
                fnames = [row["file_names"]]
        batches.append({
            "visitor_name": row["visitor_name"] or "",
            "visitor_email": row["visitor_email"] or "",
            "files": fnames,
            "file_count": row["file_count"] or len(fnames),
            "total_bytes": row["total_bytes"] or 0,
            "uploaded_at": row["accessed_at"].isoformat() if row["accessed_at"] else "",
        })
    return batches


def _get_visitor_from_cookie(request: Request, token: str):
    cookie_val = request.cookies.get(DROP_COOKIE, "")
    if not cookie_val or not cookie_val.startswith(token[:16]):
        return None
    parts = cookie_val.split(":", 3)
    if len(parts) >= 4:
        return {"name": parts[2], "email": parts[3]}
    return {"name": "", "email": ""}


# ── Render helpers ────────────────────────────────────────────────────────────

def _render_error(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Praesidium</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{font-family:'Source Sans 3',sans-serif;background:#f1f5f9;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;color:#1e293b}}.card{{background:#fff;border-radius:12px;box-shadow:0 4px 24px rgba(0,0,0,.08);max-width:480px;width:100%;padding:40px;text-align:center}}h1{{font-size:20px;margin-bottom:8px;color:#991b1b}}p{{font-size:14px;color:#64748b;line-height:1.5}}</style>
</head><body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


def _render_page(link: dict, registered: bool = False,
                 visitor: dict = None, upload_history: list = None) -> str:
    label = link.get("label", "Document Upload")
    matter_name = link.get("matter_name", "")
    instructions = (link.get("instructions") or "").strip()
    max_mb = link.get("max_file_size_mb") or 500
    visitor_name = (visitor or {}).get("name", "")
    total_uploaded = link.get("upload_count", 0) or 0

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Secure Document Upload — Praesidium</title>
<link href="https://fonts.googleapis.com/css2?family=Source+Sans+3:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*,*::before,*::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: 'Source Sans 3', -apple-system, sans-serif; background: #f1f5f9; min-height: 100vh;
       display: flex; align-items: flex-start; justify-content: center; padding: 32px 16px; color: #1e293b; }}
.card {{ background: #fff; border-radius: 12px; box-shadow: 0 4px 24px rgba(0,0,0,0.08);
         max-width: 680px; width: 100%; overflow: hidden; }}
.header {{ background: linear-gradient(135deg, #0f2b5b 0%, #1a3f7a 100%); padding: 24px 28px 20px; color: #fff; }}
.header h1 {{ font-size: 20px; font-weight: 700; margin-bottom: 2px; }}
.header .sub {{ font-size: 13px; opacity: 0.8; }}
.logo {{ display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }}
.logo svg {{ width: 26px; height: 26px; }}
.logo span {{ font-size: 15px; font-weight: 700; letter-spacing: 0.02em; }}
.body {{ padding: 24px 28px; }}
.instructions {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px;
                 padding: 12px 14px; font-size: 13px; color: #0c4a6e; margin-bottom: 18px; line-height: 1.5; }}
.form-group {{ margin-bottom: 12px; }}
.form-group label {{ font-size: 12px; font-weight: 600; color: #475569; display: block; margin-bottom: 3px; }}
.form-group input {{ width: 100%; padding: 9px 12px; border: 1px solid #cbd5e1; border-radius: 8px;
                     font-size: 14px; font-family: inherit; outline: none; }}
.form-group input:focus {{ border-color: #1a3f7a; box-shadow: 0 0 0 3px rgba(26,63,122,0.1); }}
.required {{ color: #dc2626; }}
.btn {{ width: 100%; padding: 11px; background: linear-gradient(135deg, #0f2b5b 0%, #1a3f7a 100%);
        color: #fff; border: none; border-radius: 8px; font-size: 14px; font-weight: 600;
        cursor: pointer; font-family: inherit; transition: opacity .15s; }}
.btn:hover {{ opacity: 0.9; }}
.btn:disabled {{ opacity: 0.5; cursor: default; }}
.btn-upload {{ background: linear-gradient(135deg, #059669 0%, #047857 100%); }}
.shield {{ display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: #64748b; margin-top: 10px; }}
.shield svg {{ width: 13px; height: 13px; }}
.footer {{ padding: 14px 28px; border-top: 1px solid #e2e8f0; text-align: center; }}
.footer span {{ font-size: 11px; color: #94a3b8; }}

/* Toast */
.toast {{ position: fixed; top: 20px; right: 20px; background: #059669; color: #fff; padding: 14px 20px;
          border-radius: 10px; font-size: 14px; font-weight: 600; box-shadow: 0 8px 24px rgba(0,0,0,0.15);
          z-index: 999; opacity: 0; transform: translateY(-12px); transition: all .3s ease;
          display: flex; align-items: center; gap: 8px; max-width: 380px; }}
.toast.show {{ opacity: 1; transform: translateY(0); }}
.toast.error {{ background: #dc2626; }}
.toast svg {{ width: 20px; height: 20px; flex-shrink: 0; }}

/* Drop zone */
.drop-zone {{ border: 2px dashed #cbd5e1; border-radius: 12px; padding: 32px 20px; text-align: center;
              cursor: pointer; transition: all .2s; background: #fafbfc; margin-bottom: 16px; }}
.drop-zone:hover, .drop-zone.drag-over {{ border-color: #1a3f7a; background: #f0f4ff; }}
.drop-zone svg {{ width: 40px; height: 40px; color: #94a3b8; margin-bottom: 8px; }}
.drop-zone h3 {{ font-size: 15px; margin-bottom: 3px; color: #334155; }}
.drop-zone p {{ font-size: 12px; color: #64748b; }}

/* Staging files (pending upload) */
.staging-list {{ margin-bottom: 14px; }}
.staging-item {{ display: flex; align-items: center; padding: 7px 10px; background: #fffbeb;
                 border: 1px solid #fde68a; border-radius: 6px; margin-bottom: 5px; font-size: 13px; }}
.staging-item .icon {{ color: #f59e0b; margin-right: 8px; font-size: 16px; flex-shrink: 0; }}
.staging-item .name {{ color: #1e293b; font-weight: 500; flex: 1; overflow: hidden;
                       text-overflow: ellipsis; white-space: nowrap; }}
.staging-item .size {{ color: #92400e; margin-left: 10px; white-space: nowrap; font-size: 12px; }}
.staging-item .remove {{ color: #dc2626; cursor: pointer; margin-left: 8px; font-weight: 700; font-size: 16px; }}

/* Progress */
.progress-bar {{ width: 100%; height: 5px; background: #e2e8f0; border-radius: 3px;
                 overflow: hidden; margin-bottom: 10px; display: none; }}
.progress-bar .fill {{ height: 100%; background: linear-gradient(90deg, #059669, #10b981);
                       width: 0%; transition: width .3s; }}
.upload-status {{ font-size: 12px; color: #64748b; margin-bottom: 10px; text-align: center; display: none; }}

/* Section divider */
.section-label {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em;
                  color: #94a3b8; margin: 20px 0 10px; padding-bottom: 6px; border-bottom: 1px solid #e2e8f0;
                  display: flex; align-items: center; justify-content: space-between; }}
.section-label .count {{ background: #e2e8f0; color: #475569; font-size: 10px; padding: 2px 7px;
                         border-radius: 10px; font-weight: 600; }}

/* Uploaded file inventory */
.batch-group {{ margin-bottom: 14px; }}
.batch-header {{ font-size: 11px; color: #64748b; margin-bottom: 4px; display: flex;
                 align-items: center; gap: 6px; }}
.batch-header .dot {{ width: 6px; height: 6px; background: #10b981; border-radius: 50%; }}
.batch-header .when {{ font-weight: 600; }}
.uploaded-item {{ display: flex; align-items: center; padding: 6px 10px; background: #f8fafc;
                  border: 1px solid #e2e8f0; border-radius: 6px; margin-bottom: 4px; font-size: 13px; }}
.uploaded-item .icon {{ color: #64748b; margin-right: 8px; font-size: 15px; flex-shrink: 0; }}
.uploaded-item .name {{ color: #1e293b; font-weight: 500; flex: 1; overflow: hidden;
                        text-overflow: ellipsis; white-space: nowrap; }}
.uploaded-item .check {{ color: #10b981; margin-left: 8px; font-size: 14px; }}

.empty-state {{ text-align: center; color: #94a3b8; font-size: 13px; padding: 16px 0; }}
.summary-bar {{ display: flex; gap: 16px; font-size: 12px; color: #64748b; margin-bottom: 8px; }}
.summary-bar span {{ display: flex; align-items: center; gap: 4px; }}
</style>
</head>
<body>
<div id="toast" class="toast"></div>
<div class="card">
  <div class="header">
    <div class="logo">
      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
        <path d="M12 2L3 7v10l9 5 9-5V7l-9-5z" stroke="currentColor" stroke-width="1.5" fill="rgba(255,255,255,0.1)"/>
        <path d="M12 8v8M8 12h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
      </svg>
      <span>Praesidium</span>
    </div>
    <h1>Secure Document Upload</h1>
    <div class="sub">{label}</div>
  </div>
  <div class="body">
    {'<div class="instructions">' + instructions + '</div>' if instructions else ''}
    {_render_upload_section(max_mb, visitor_name, upload_history or []) if registered else _render_register_form(link)}
  </div>
  <div class="footer">
    <span>Delivered securely via Praesidium{(' &middot; ' + matter_name) if matter_name else ''}</span>
  </div>
</div>
{_render_app_js(max_mb, upload_history or []) if registered else _render_register_js()}
</body>
</html>"""


def _render_register_form(link: dict) -> str:
    return f"""
    <div id="register-form">
      <p style="font-size:13px;color:#475569;margin-bottom:14px;line-height:1.5">
        To upload documents, please verify your identity. All access and uploads are logged
        for chain-of-custody purposes.
      </p>
      <div class="form-group">
        <label>Full Name <span class="required">*</span></label>
        <input type="text" id="reg-name" placeholder="Jane Smith" required
               value="{link.get('recipient_name','') or ''}">
      </div>
      <div class="form-group">
        <label>Email Address <span class="required">*</span></label>
        <input type="email" id="reg-email" placeholder="jane@example.com" required
               value="{link.get('recipient_email','') or ''}">
      </div>
      <div class="form-group">
        <label>Company / Organization</label>
        <input type="text" id="reg-firm" placeholder="Optional">
      </div>
      <button class="btn" id="reg-btn" onclick="doRegister()">Verify &amp; Continue to Upload</button>
      <div class="shield">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        Your access is tracked for legal chain-of-custody compliance
      </div>
    </div>"""


def _render_upload_section(max_mb: int, visitor_name: str, history: list) -> str:
    # Count total files across all batches
    total_files = sum(b.get("file_count", 0) for b in history)
    total_mb = round(sum(b.get("total_bytes", 0) for b in history) / 1024 / 1024, 1)

    return f"""
    <p style="font-size:13px;color:#475569;margin-bottom:14px;line-height:1.5">
      {'Welcome back, ' + visitor_name + '. ' if visitor_name else ''}Drop files below or click to browse.
      Files are received as they are uploaded &mdash; you can come back and add more at any time.
    </p>

    <!-- Drop zone -->
    <div class="drop-zone" id="drop-zone" onclick="document.getElementById('file-input').click()">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
      </svg>
      <h3>Drop files here</h3>
      <p>or click to browse &middot; max {max_mb} MB per file</p>
    </div>
    <input type="file" id="file-input" multiple style="display:none" onchange="addFiles(this.files)">

    <!-- Staged (pending) files -->
    <div id="staging-section" style="display:none">
      <div class="section-label">
        <span>Ready to Upload</span>
        <span class="count" id="staging-count">0</span>
      </div>
      <div id="staging-list" class="staging-list"></div>
      <div class="progress-bar" id="progress-bar"><div class="fill" id="progress-fill"></div></div>
      <div class="upload-status" id="upload-status"></div>
      <button class="btn btn-upload" id="upload-btn" onclick="doUpload()">Upload Files</button>
    </div>

    <!-- Previously uploaded files -->
    <div class="section-label">
      <span>Uploaded Files</span>
      <span class="count" id="total-count">{total_files}</span>
    </div>
    <div id="uploaded-inventory">
      {'<div class="empty-state">No files uploaded yet. Drop files above to get started.</div>' if not history else ''}
    </div>

    <div class="shield" style="margin-top:14px">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
      All uploads are encrypted and logged for chain-of-custody
    </div>"""


def _render_register_js() -> str:
    return """<script>
async function doRegister() {
  const name = document.getElementById('reg-name').value.trim();
  const email = document.getElementById('reg-email').value.trim();
  const firm = document.getElementById('reg-firm').value.trim();
  if (!name || !email) { alert('Name and email are required.'); return; }
  const btn = document.getElementById('reg-btn');
  btn.disabled = true; btn.textContent = 'Verifying\u2026';
  try {
    const r = await fetch(window.location.pathname + '/register', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, email, firm })
    });
    const d = await r.json();
    if (d.ok) { window.location.reload(); }
    else { alert(d.error || 'Registration failed'); btn.disabled = false; btn.textContent = 'Verify & Continue to Upload'; }
  } catch(e) { alert('Error: ' + e); btn.disabled = false; btn.textContent = 'Verify & Continue to Upload'; }
}
</script>"""


def _file_icon(name: str) -> str:
    """Return an emoji icon based on file extension."""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    icons = {
        "pdf": "\U0001f4c4", "doc": "\U0001f4dd", "docx": "\U0001f4dd",
        "xls": "\U0001f4ca", "xlsx": "\U0001f4ca", "csv": "\U0001f4ca",
        "ppt": "\U0001f4ca", "pptx": "\U0001f4ca",
        "jpg": "\U0001f5bc", "jpeg": "\U0001f5bc", "png": "\U0001f5bc",
        "gif": "\U0001f5bc", "tif": "\U0001f5bc", "tiff": "\U0001f5bc",
        "zip": "\U0001f4e6", "rar": "\U0001f4e6", "7z": "\U0001f4e6",
        "msg": "\U00002709", "eml": "\U00002709", "pst": "\U00002709",
        "txt": "\U0001f4c3", "rtf": "\U0001f4c3",
    }
    return icons.get(ext, "\U0001f4ce")


def _render_app_js(max_mb: int, history: list) -> str:
    """Main app JS with persistent inventory."""
    history_json = json.dumps(history)
    return """<script>
const MAX_MB = """ + str(max_mb) + """;
const uploadHistory = """ + history_json + """;
let selectedFiles = [];

// -- Toast --
function toast(msg, isError) {
  const el = document.getElementById('toast');
  el.innerHTML = (isError
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
  ) + '<span>' + msg + '</span>';
  el.className = 'toast' + (isError ? ' error' : '') + ' show';
  setTimeout(() => el.className = 'toast', 4000);
}

// -- File icon helper --
function fileIcon(name) {
  const ext = name.includes('.') ? name.split('.').pop().toLowerCase() : '';
  const m = {pdf:'\u{1F4C4}',doc:'\u{1F4DD}',docx:'\u{1F4DD}',xls:'\u{1F4CA}',xlsx:'\u{1F4CA}',
    csv:'\u{1F4CA}',ppt:'\u{1F4CA}',pptx:'\u{1F4CA}',jpg:'\u{1F5BC}',jpeg:'\u{1F5BC}',
    png:'\u{1F5BC}',gif:'\u{1F5BC}',zip:'\u{1F4E6}',rar:'\u{1F4E6}',msg:'\u{2709}',
    eml:'\u{2709}',pst:'\u{2709}',txt:'\u{1F4C3}',rtf:'\u{1F4C3}'};
  return m[ext] || '\u{1F4CE}';
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes/1024).toFixed(0) + ' KB';
  return (bytes/1048576).toFixed(1) + ' MB';
}

function timeAgo(iso) {
  const d = new Date(iso);
  const now = new Date();
  const diff = Math.floor((now - d) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff/86400) + 'd ago';
  return d.toLocaleDateString(undefined, {month:'short', day:'numeric', year:'numeric'});
}

// -- Render uploaded inventory --
function renderInventory() {
  const container = document.getElementById('uploaded-inventory');
  if (!uploadHistory.length) {
    container.innerHTML = '<div class="empty-state">No files uploaded yet. Drop files above to get started.</div>';
    document.getElementById('total-count').textContent = '0';
    return;
  }
  let totalFiles = 0;
  let html = '';
  for (const batch of uploadHistory) {
    totalFiles += batch.files.length;
    html += '<div class="batch-group">';
    html += '<div class="batch-header"><span class="dot"></span><span class="when">' +
            timeAgo(batch.uploaded_at) + '</span><span>&middot; ' +
            batch.files.length + ' file' + (batch.files.length !== 1 ? 's' : '') +
            ' &middot; ' + fmtSize(batch.total_bytes) + '</span></div>';
    for (const fname of batch.files) {
      html += '<div class="uploaded-item">' +
              '<span class="icon">' + fileIcon(fname) + '</span>' +
              '<span class="name">' + fname + '</span>' +
              '<span class="check">\u2713</span></div>';
    }
    html += '</div>';
  }
  container.innerHTML = html;
  document.getElementById('total-count').textContent = totalFiles;
}

// -- Drop zone --
const dropZone = document.getElementById('drop-zone');
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => { e.preventDefault(); dropZone.classList.remove('drag-over'); addFiles(e.dataTransfer.files); });

function addFiles(fileList) {
  for (const f of fileList) {
    if (f.size > MAX_MB * 1024 * 1024) { toast(f.name + ' exceeds ' + MAX_MB + 'MB limit', true); continue; }
    if (!selectedFiles.find(x => x.name === f.name && x.size === f.size)) {
      selectedFiles.push(f);
    }
  }
  renderStaging();
}

function removeStaged(idx) { selectedFiles.splice(idx, 1); renderStaging(); }

function renderStaging() {
  const section = document.getElementById('staging-section');
  const list = document.getElementById('staging-list');
  const count = document.getElementById('staging-count');
  if (!selectedFiles.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  count.textContent = selectedFiles.length;
  list.innerHTML = selectedFiles.map((f, i) =>
    '<div class="staging-item">' +
      '<span class="icon">' + fileIcon(f.name) + '</span>' +
      '<span class="name">' + f.name + '</span>' +
      '<span class="size">' + fmtSize(f.size) + '</span>' +
      '<span class="remove" onclick="removeStaged(' + i + ')">&times;</span>' +
    '</div>'
  ).join('');
}

// -- Upload --
async function doUpload() {
  if (!selectedFiles.length) return;
  const btn = document.getElementById('upload-btn');
  const progress = document.getElementById('progress-bar');
  const fill = document.getElementById('progress-fill');
  const status = document.getElementById('upload-status');

  btn.disabled = true; btn.textContent = 'Uploading\u2026';
  progress.style.display = 'block'; status.style.display = 'block';
  status.textContent = 'Preparing upload\u2026';

  const formData = new FormData();
  const uploadingFiles = [...selectedFiles];
  uploadingFiles.forEach(f => formData.append('files', f));

  const xhr = new XMLHttpRequest();
  xhr.open('POST', window.location.pathname + '/upload');

  xhr.upload.onprogress = function(e) {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      fill.style.width = pct + '%';
      status.textContent = 'Uploading\u2026 ' + pct + '%';
    }
  };

  xhr.onload = function() {
    progress.style.display = 'none'; status.style.display = 'none';
    fill.style.width = '0%';

    if (xhr.status === 200) {
      const data = JSON.parse(xhr.responseText);
      toast(data.file_count + ' file' + (data.file_count !== 1 ? 's' : '') +
            ' uploaded successfully (' + data.total_mb + ' MB)', false);

      // Add to local history at the top
      uploadHistory.unshift({
        visitor_name: '', visitor_email: '',
        files: uploadingFiles.map(f => f.name),
        file_count: uploadingFiles.length,
        total_bytes: uploadingFiles.reduce((s, f) => s + f.size, 0),
        uploaded_at: new Date().toISOString(),
      });
      renderInventory();

      selectedFiles = [];
      renderStaging();
      btn.disabled = false; btn.textContent = 'Upload Files';
    } else {
      let msg = 'Upload failed';
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch(e) {}
      toast(msg, true);
      btn.disabled = false; btn.textContent = 'Retry Upload';
    }
  };

  xhr.onerror = function() {
    progress.style.display = 'none'; status.style.display = 'none';
    toast('Upload failed \u2014 network error', true);
    btn.disabled = false; btn.textContent = 'Retry Upload';
  };

  xhr.send(formData);
}

// -- Init --
renderInventory();
</script>"""


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/drop/{token}")
async def drop_landing(request: Request, token: str):
    link = await _get_link(token)
    if not link:
        return HTMLResponse(_render_error("Link Not Found",
            "This upload link does not exist or has been removed."), status_code=404)
    valid, reason = _link_valid(link)
    if not valid:
        return HTMLResponse(_render_error("Link Unavailable", reason), status_code=403)

    visitor = _get_visitor_from_cookie(request, token)
    registered = visitor is not None

    upload_history = []
    if registered:
        upload_history = await _get_upload_history(str(link["id"]))

    await _log_access(
        link["id"], link["tenant_id"], "viewed",
        visitor_name=visitor.get("name") if visitor else None,
        visitor_email=visitor.get("email") if visitor else None,
        ip_address=_client_ip(request),
        user_agent=str(request.headers.get("user-agent", ""))[:500],
    )

    html = _render_page(link, registered=registered, visitor=visitor,
                        upload_history=upload_history)
    return HTMLResponse(html)


@router.post("/drop/{token}/register")
async def drop_register(request: Request, token: str):
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

    await _log_access(
        link["id"], link["tenant_id"], "registered",
        visitor_name=visitor_name, visitor_email=visitor_email,
        visitor_firm=visitor_firm,
        ip_address=_client_ip(request),
        user_agent=str(request.headers.get("user-agent", ""))[:500],
    )

    cookie_value = (
        token[:16] + ":" +
        hashlib.sha256(f"{visitor_email}:{token}".encode()).hexdigest()[:16] + ":" +
        visitor_name[:50] + ":" +
        visitor_email[:100]
    )
    response = JSONResponse({"ok": True})
    response.set_cookie(
        DROP_COOKIE, cookie_value, max_age=60 * 60 * 24 * 90,
        httponly=True, samesite="lax",
        secure=str(request.url).startswith("https"),
    )
    return response


@router.get("/drop/{token}/files")
async def drop_files(request: Request, token: str):
    """Return JSON history of all uploads for this drop link."""
    link = await _get_link(token)
    if not link:
        return JSONResponse({"error": "Link not found"}, 404)
    visitor = _get_visitor_from_cookie(request, token)
    if not visitor:
        return JSONResponse({"error": "Registration required"}, 403)

    history = await _get_upload_history(str(link["id"]))
    total_files = sum(b["file_count"] for b in history)
    total_bytes = sum(b["total_bytes"] for b in history)
    return JSONResponse({
        "batches": history,
        "total_files": total_files,
        "total_bytes": total_bytes,
    })


@router.post("/drop/{token}/upload")
async def drop_upload(request: Request, token: str):
    link = await _get_link(token)
    if not link:
        return JSONResponse({"error": "Link not found"}, 404)
    valid, reason = _link_valid(link)
    if not valid:
        return JSONResponse({"error": reason}, 403)

    visitor = _get_visitor_from_cookie(request, token)
    if not visitor:
        return JSONResponse({"error": "Registration required. Please reload and register."}, 403)

    form = await request.form()
    files = form.getlist("files")
    if not files:
        return JSONResponse({"error": "No files provided"}, 400)

    tenant_id = str(link["tenant_id"]).strip()
    matter_id = str(link["matter_id"])
    max_bytes = (link.get("max_file_size_mb") or 500) * 1024 * 1024

    # Each upload batch gets its own timestamped subdirectory
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9._-]", "_", link.get("label", "drop"))[:64]

    # Persistent base directory for this drop link (all batches under one parent)
    base_dir = os.path.join(EDISCOVERY_ROOT, tenant_id, matter_id, f"client_drop_{safe_label}")
    batch_dir = os.path.join(base_dir, f"batch_{timestamp}")
    os.makedirs(batch_dir, exist_ok=True)

    saved_files = []
    total_bytes_saved = 0

    for upload in files:
        content = await upload.read()
        file_size = len(content)
        if file_size > max_bytes:
            return JSONResponse({
                "error": f"{upload.filename} exceeds {link.get('max_file_size_mb', 500)}MB limit"
            }, 413)
        total_bytes_saved += file_size
        safe_filename = Path(upload.filename).name
        dest_path = os.path.join(batch_dir, safe_filename)
        with open(dest_path, "wb") as fout:
            fout.write(content)
        saved_files.append(safe_filename)
        logger.info(f"Client drop saved: {dest_path} ({file_size} bytes)")

    if not saved_files:
        return JSONResponse({"error": "No files were saved"}, 400)

    # Create or reuse eDiscovery collection (one per drop link, persistent)
    collection_id = link.get("collection_id")
    collection_name = f"Client Upload \u2014 {link.get('label', 'Drop')}"

    if not collection_id:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sa_text("""
                INSERT INTO ediscovery_collections
                    (tenant_id, matter_id, name, collection_name, status,
                     source_type, source_party,
                     dms_source_path, storage_path,
                     received_by, received_method, received_date,
                     created_at, updated_at)
                VALUES
                    (:tid, CAST(:mid AS uuid), :name, :name, 'collecting',
                     'client_upload', :source_party,
                     :src_path, :storage,
                     :uid, 'client_drop_link', CURRENT_DATE,
                     NOW(), NOW())
                RETURNING id::text
            """), {
                "tid": tenant_id, "mid": matter_id, "name": collection_name,
                "source_party": visitor.get("name", ""),
                "src_path": batch_dir, "storage": base_dir,
                "uid": link.get("created_by"),
            })
            collection_id = result.scalar()
            await session.commit()

        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE client_drop_links
                SET collection_id = CAST(:cid AS uuid), updated_at = now()
                WHERE id = CAST(:lid AS uuid)
            """), {"cid": collection_id, "lid": str(link["id"])})
            await session.commit()
    else:
        # Update dms_source_path to latest batch for the ingest job
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE ediscovery_collections
                SET dms_source_path = :src_path, updated_at = now()
                WHERE id = CAST(:cid AS uuid)
            """), {"cid": str(collection_id), "src_path": batch_dir})
            await session.commit()

    # Update counters
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            UPDATE client_drop_links
            SET upload_count = upload_count + 1,
                total_bytes_uploaded = total_bytes_uploaded + :tbytes,
                updated_at = now()
            WHERE id = CAST(:lid AS uuid)
        """), {"tbytes": total_bytes_saved, "lid": str(link["id"])})
        await session.commit()

    # Log the upload
    await _log_access(
        link["id"], link["tenant_id"], "uploaded",
        visitor_name=visitor.get("name"), visitor_email=visitor.get("email"),
        ip_address=_client_ip(request),
        user_agent=str(request.headers.get("user-agent", ""))[:500],
        file_names=json.dumps(saved_files),
        file_count=len(saved_files),
        total_bytes=total_bytes_saved,
    )

    # Enqueue ingest job for this batch
    try:
        import redis as redis_lib
        from rq import Queue
        redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        q = Queue("ediscovery", connection=redis_lib.Redis.from_url(redis_url))
        q.enqueue(
            "modules.ediscovery.jobs.ledger_dag.run_collection",
            tenant_id, str(collection_id), link.get("created_by"),
            spine_workers=6,
            ocr_workers=2,
            embed_workers=1,
            job_timeout="24h",
            result_ttl=3600,
        )
        logger.info(f"Enqueued ingest for client drop batch {batch_dir}")
    except Exception as e:
        logger.error(f"Failed to enqueue ingest for client drop: {e}")

    return JSONResponse({
        "ok": True,
        "collection_id": str(collection_id),
        "file_count": len(saved_files),
        "total_mb": round(total_bytes_saved / 1024 / 1024, 1),
    })
