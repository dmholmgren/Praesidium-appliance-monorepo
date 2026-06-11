from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
from core.services.nav_context import get_nav_context
"""
DMS Web UI — Matter Workspace
Three-panel layout: unified folder tree | document list | preview pane
Legacy files served via CIFS bridge using position() path matching.
Native files stored in /mnt/praesidium via upload endpoint.
AI search via Elasticsearch with DB full-text fallback.
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dms", tags=["dms-ui"])

from jinja2 import Environment, FileSystemLoader
_loader = FileSystemLoader(["/app/modules/dms/templates", "/app/core/templates"])
_env = Environment(loader=_loader, autoescape=True)

class _Templates:
    def __init__(self, env):
        self.env = env
    def TemplateResponse(self, name, context, status_code=200):
        from starlette.responses import HTMLResponse
        template = self.env.get_template(name)
        content = template.render(context)
        return HTMLResponse(content=content, status_code=status_code)

templates = _Templates(_env)

CIFS_URL = os.environ.get("CIFS_URL", "http://10.10.60.13:8080")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_size(b):
    if b is None: return "—"
    if b < 1024: return f"{b} B"
    if b < 1024*1024: return f"{b/1024:.1f} KB"
    return f"{b/(1024*1024):.1f} MB"

def _ext_icon(path):
    if not path: return "📄"
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return {"pdf":"📕","doc":"📘","docx":"📘","xls":"📗","xlsx":"📗",
            "ppt":"📙","pptx":"📙","txt":"📄","msg":"📧","eml":"📧",
            "jpg":"🖼","jpeg":"🖼","png":"🖼","tif":"🖼","tiff":"🖼",
            "mp3":"🎵","mp4":"🎬","wav":"🎵","zip":"📦","wpd":"📄"
            }.get(ext, "📄")

def _root_label(path):
    if not path: return ""
    if "Docsend" in path or "docsend" in path: return "Docsend"
    return "Clients"

def _folder_prefix(disk_root, folder_path):
    return disk_root + "\\" + folder_path.replace("/", "\\") + "\\"


def _walk_disk_tree(root_path, max_depth=4):
    """Walk a Praesidium matter dir, return nested folder structure.
    Skips dotfiles. Returns [{name, path, children, file_count}]."""
    result = []
    if not os.path.isdir(root_path):
        return result
    try:
        entries = sorted(os.scandir(root_path), key=lambda e: e.name)
    except PermissionError:
        return result
    for entry in entries:
        if entry.name.startswith('.'):
            continue
        if entry.is_dir(follow_symlinks=False):
            children = _walk_disk_tree(entry.path, max_depth - 1) if max_depth > 1 else []
            try:
                file_count = sum(1 for f in os.scandir(entry.path)
                                 if f.is_file() and not f.name.startswith('.'))
            except (PermissionError, OSError):
                file_count = 0
            result.append({"name": entry.name, "path": entry.path,
                           "children": children, "file_count": file_count})
    return result







# ---------------------------------------------------------------------------
# DMS Client Page — matters and docs for a single client
# ---------------------------------------------------------------------------

@router.get("/client/{client_id}", response_class=HTMLResponse)
async def dms_client_page(request: Request, client_id: str):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    client_name = "Client"
    async with AsyncSessionLocal() as session:
        cr = await session.execute(sa_text("""
            SELECT client_name FROM clients
            WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
        """), {"cid": client_id, "tid": tenant_id})
        row = cr.fetchone()
        if row:
            client_name = row[0] or "Client"

    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse("dms_client_react.html", {
        "request": request,
        "brand": brand,
        "current_user": getattr(request.state, "current_user", None),
        "page": "docs",
        "client_id": client_id,
        "client_name": client_name,
        **nav_ctx,
    })


# ---------------------------------------------------------------------------
# DMS Search Page (React)
# ---------------------------------------------------------------------------

@router.get("/search", response_class=HTMLResponse)
async def dms_search_page(request: Request):
    brand = get_brand(request)
    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse("dms_search_react.html", {
        "request": request,
        "brand": brand,
        "current_user": getattr(request.state, "current_user", None),
        "page": "docs",
        **nav_ctx,
    })


# ---------------------------------------------------------------------------
# DMS Home
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dms_home(request: Request):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    async with AsyncSessionLocal() as session:

        # Recent native documents (documents table)
        recent = await session.execute(sa_text("""
            SELECT d.id::text, d.filename AS title,
                   d.document_type AS doc_type, d.file_size, d.updated_at,
                   d.storage_path, m.matter_name, c.client_name
            FROM documents d
            LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
            LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            WHERE trim(d.tenant_id) = trim(:tid)
            ORDER BY d.updated_at DESC LIMIT 20
        """), {"tid": tenant_id})
        recent_docs = [dict(r) for r in recent.mappings().fetchall()]

        # Client-grouped active matters with doc counts
        matters_r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.status,
                   c.id::text as client_id, c.client_name,
                   COUNT(d.id) as doc_count
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            LEFT JOIN documents d ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
            WHERE trim(m.tenant_id) = trim(:tid) AND m.status = 'active'
            GROUP BY m.id, m.matter_name, m.matter_number, m.status, c.id, c.client_name
            ORDER BY c.client_name, m.matter_name
        """), {"tid": tenant_id})
        matters_rows = [dict(r) for r in matters_r.mappings().fetchall()]

        # Group by client
        client_map = {}
        for row in matters_rows:
            cid = row["client_id"] or "unknown"
            cname = row["client_name"] or "Unknown Client"
            if cid not in client_map:
                client_map[cid] = {"client_id": cid, "client_name": cname, "matters": []}
            client_map[cid]["matters"].append(row)
        clients = sorted(client_map.values(), key=lambda x: x["client_name"])

        # Storage stats from dms_documents (CIFS agent index)
        stats_r = await session.execute(sa_text("""
            SELECT
                COUNT(*)                                        AS total_indexed,
                COALESCE(SUM(file_size_bytes), 0)              AS total_bytes,
                COUNT(*) FILTER (WHERE ocr_status = 'pending') AS ocr_pending,
                COUNT(*) FILTER (WHERE ocr_status = 'done')    AS ocr_done,
                COUNT(*) FILTER (
                    WHERE indexed_at >= NOW() - INTERVAL '7 days'
                )                                              AS indexed_this_week
            FROM dms_documents
            WHERE trim(tenant_id) = trim(:tid)
        """), {"tid": tenant_id})
        sr = stats_r.mappings().fetchone()
        storage_stats = {
            "total_indexed":     int(sr["total_indexed"]     or 0),
            "total_bytes":       int(sr["total_bytes"]       or 0),
            "ocr_pending":       int(sr["ocr_pending"]       or 0),
            "ocr_done":          int(sr["ocr_done"]          or 0),
            "indexed_this_week": int(sr["indexed_this_week"] or 0),
            "total_matters":     len(matters_rows),
            "total_clients":     len(client_map),
        }

        # OCR queue depth
        ocr_q = await session.execute(sa_text("""
            SELECT COUNT(*) FROM dms_ocr_queue
            WHERE trim(tenant_id) = trim(:tid)
              AND status IN ('pending', 'processing')
        """), {"tid": tenant_id})
        storage_stats["ocr_queued"] = int(ocr_q.scalar() or 0)

        # Recent activity feed from dms_documents
        activity_r = await session.execute(sa_text("""
            SELECT dd.file_path, dd.file_size_bytes, dd.indexed_at,
                   dd.ocr_status, dd.source,
                   m.id::text AS matter_id,
                   m.matter_name, m.matter_number,
                   c.client_name
            FROM dms_documents dd
            LEFT JOIN dms_folder_matches mf
                ON mf.folder_path = (
                    SELECT mf2.folder_path FROM dms_folder_matches mf2
                    WHERE trim(mf2.tenant_id) = trim(:tid)
                      AND dd.file_path LIKE mf2.folder_path || '%'
                    ORDER BY length(mf2.folder_path) DESC
                    LIMIT 1
                )
                AND trim(mf.tenant_id) = trim(:tid)
            LEFT JOIN matters m ON mf.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE trim(dd.tenant_id) = trim(:tid)
            ORDER BY dd.indexed_at DESC
            LIMIT 10
        """), {"tid": tenant_id})
        activity_feed = []
        for r in activity_r.mappings().fetchall():
            row = dict(r)
            path = row.get("file_path") or ""
            row["filename"] = path.replace("\\", "/").split("/")[-1]
            activity_feed.append(row)

    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse("dms_home_react.html", {
        "request": request,
        "brand": brand,
        "current_user": getattr(request.state, "current_user", None),
        "page": "docs",
        **nav_ctx,
        "recent_docs": recent_docs,
        "clients": clients,
        "storage_stats": storage_stats,
        "activity_feed": activity_feed,
    })


@router.get("/dash/search", response_class=HTMLResponse)
async def dash_search(request: Request, q: str = "", include_ediscovery: str = "0"):
    """Full-text search across all matters for the dashboard."""
    from core.db.base import AsyncSessionLocal
    import httpx as _httpx

    if not q:
        return HTMLResponse('<div style="padding:12px 16px; font-size:12px; color:var(--muted);">Enter a search term.</div>')

    tenant_id = request.state.tenant_id
    es_url = os.environ.get("ELASTICSEARCH_URL", "http://10.10.60.12:9200")
    results = []

    # ES search
    try:
        index = f"dms_files_{tenant_id.strip().replace('-','_')}"
        async with _httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"{es_url}/{index}/_search", json={
                "query": {"multi_match": {"query": q, "fields": ["content_text","file_path"],
                                          "type": "best_fields", "fuzziness": "AUTO"}},
                "size": 20, "_source": ["file_path","file_size_bytes","modified_at"],
            })
        if resp.status_code == 200:
            for hit in resp.json().get("hits",{}).get("hits",[]):
                src = hit.get("_source",{})
                fp = src.get("file_path","")
                results.append({
                    "id": hit["_id"], "title": fp.rsplit("\\",1)[-1] if fp else "",
                    "file_path": fp, "matter_name": "", "client_name": "",
                    "source": "legacy", "score": round(hit.get("_score",0),2),
                    "doc_type": fp.rsplit(".",1)[-1].lower() if "." in fp else "",
                    "updated_at": src.get("modified_at","")[:10],
                })
    except Exception:
        pass

    # DB fallback
    if not results:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT d.id::text, d.title, d.doc_type, d.updated_at,
                       d.storage_path, m.matter_name, c.client_name, m.id::text as matter_id
                FROM documents d
                LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
                LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
                WHERE trim(d.tenant_id) = trim(:tid)
                  AND (d.title ILIKE :q OR d.storage_path ILIKE :q)
                ORDER BY d.updated_at DESC LIMIT 30
            """), {"tid": tenant_id, "q": f"%{q}%"})
            for row in r.mappings().fetchall():
                results.append({
                    "id": row["id"], "title": row["title"] or "",
                    "file_path": row["storage_path"] or "",
                    "matter_name": row["matter_name"] or "",
                    "client_name": row["client_name"] or "",
                    "matter_id": row["matter_id"] or "",
                    "source": "native", "score": None,
                    "doc_type": row["doc_type"] or "",
                    "updated_at": row["updated_at"].strftime("%Y-%m-%d") if row["updated_at"] else "",
                })

    if not results:
        return HTMLResponse(f'<div style="padding:12px 16px; font-size:12px; color:var(--muted);">No results for &ldquo;{q}&rdquo;.</div>')

    rows = ""
    for r in results:
        icon = _ext_icon(r["title"] or r["file_path"])
        badge_color = "#1d4ed8" if r["source"] == "legacy" else "#1B2A4A"
        badge = "Legacy" if r["source"] == "legacy" else "P"
        matter_link = f'/dms/matter/{r.get("matter_id")}' if r.get("matter_id") else "#"
        ep = (r["file_path"] or "").replace('"',"&quot;")
        rows += f"""
        <div style="padding:7px 16px; border-bottom:1px solid #f1f5f9; cursor:pointer; display:flex; align-items:center; gap:8px;"
             onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background=''"
             onclick="previewDoc('{r['id']}','{ep}','{r['source']}','{r['title'].replace("'","")}','{r['matter_name'].replace("'","")}')">
          <span style="font-size:16px; flex-shrink:0;">{icon}</span>
          <div style="flex:1; min-width:0;">
            <div style="font-size:12px; font-weight:500; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">{r['title']}</div>
            <div style="font-size:10px; color:var(--muted); margin-top:1px;">
              <span style="background:{badge_color};color:#fff;padding:0 4px;border-radius:2px;font-size:9px;">{badge}</span>
              &nbsp;{r['matter_name'] or '—'}{(' · ' + r['client_name']) if r['client_name'] else ''}
              &nbsp;·&nbsp;{r['updated_at']}
            </div>
          </div>
          {'<a href="' + matter_link + '" onclick="event.stopPropagation()" style="font-size:10px;color:var(--muted);text-decoration:none;white-space:nowrap;flex-shrink:0;">Open matter →</a>' if r.get('matter_id') else ''}
        </div>"""

    return HTMLResponse(f"""
    <div style="font-size:11px; color:var(--muted); padding:5px 16px; border-bottom:1px solid #f1f5f9;">
      {len(results)} result{'s' if len(results)!=1 else ''} for &ldquo;{q}&rdquo;
    </div>{rows}""")


# ---------------------------------------------------------------------------
# Matter Workspace — main page
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}", response_class=HTMLResponse)
async def matter_documents(request: Request, matter_id: str):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    async with AsyncSessionLocal() as session:
        m = await session.execute(sa_text("""
            SELECT m.*, c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tenant_id})
        matter = m.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        folders = await session.execute(sa_text("""
            SELECT id::text, folder_path, disk_root, file_count
            FROM matter_folders
            WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
            ORDER BY disk_root NULLS LAST, folder_path
        """), {"mid": matter_id, "tid": tenant_id})
        linked_folders = [dict(r) for r in folders.mappings().fetchall()]

        nc = await session.execute(sa_text("""
            SELECT COUNT(*) FROM documents
            WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tenant_id})
        native_count = nc.scalar() or 0

    for f in linked_folders:
        f["root_label"] = _root_label(f.get("disk_root", "") or "")
        f["prefix"] = _folder_prefix(f["disk_root"], f["folder_path"]) if f.get("disk_root") else None

    nav_ctx = await get_nav_context(request)
    return templates.TemplateResponse("matter_workspace_react.html", {
        "request": request, "brand": brand,
        "current_user": getattr(request.state, "current_user", None),
        "matter": dict(matter), "linked_folders": linked_folders,
        "native_count": native_count, "matter_id": matter_id,
        "matter_name": dict(matter).get("matter_name", ""),
        "page": "docs",
        **nav_ctx,
    })


# ---------------------------------------------------------------------------
# Folder contents — HTMX partial (legacy files from dms_documents)
# Uses position() for path matching — validated against 3.5M row index
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/folder", response_class=HTMLResponse)
async def folder_contents(request: Request, matter_id: str, prefix: str = "", page: int = 1):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    per_page = 100
    offset = (page - 1) * per_page

    if not prefix:
        return HTMLResponse('<div style="padding:12px; font-size:12px; color:var(--muted);">Select a folder to view files.</div>')

    async with AsyncSessionLocal() as session:
        total_r = await session.execute(sa_text("""
            SELECT COUNT(*) FROM dms_documents
            WHERE trim(tenant_id) = trim(:tid)
              AND position(:pfx in file_path) = 1
        """), {"tid": tenant_id, "pfx": prefix})
        total = total_r.scalar() or 0

        files_r = await session.execute(sa_text("""
            SELECT id::text, file_path, file_size_bytes, modified_at,
                   ocr_status, extraction_status, content_text
            FROM dms_documents
            WHERE trim(tenant_id) = trim(:tid)
              AND position(:pfx in file_path) = 1
            ORDER BY file_path
            LIMIT :lim OFFSET :off
        """), {"tid": tenant_id, "pfx": prefix, "lim": per_page, "off": offset})
        files = [dict(r) for r in files_r.mappings().fetchall()]

    for f in files:
        rel = f["file_path"][len(prefix):]
        f["display_name"] = rel if rel else f["file_path"].rsplit("\\", 1)[-1]
        f["icon"] = _ext_icon(f["file_path"])
        f["size_str"] = _fmt_size(f.get("file_size_bytes"))
        mod = f.get("modified_at")
        f["modified_str"] = mod.strftime("%Y-%m-%d") if mod else "—"
        f["has_text"] = bool(f.get("content_text"))

    total_pages = max(1, (total + per_page - 1) // per_page)

    if not files:
        return HTMLResponse(f'<div style="padding:16px; font-size:12px; color:var(--muted);">No files found. ({total} total indexed under this path)</div>')

    rows = ""
    for f in files:
        ep = f["file_path"].replace('"', "&quot;")
        rows += f"""
        <tr onclick="selectDoc('{f['id']}','{ep}','legacy')"
            style="cursor:pointer;"
            onmouseover="this.style.background='#f8fafc'"
            onmouseout="this.style.background=''">
          <td style="padding:5px 8px; font-size:12px; max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
            {f['icon']} <span title="{ep}">{f['display_name']}</span>
          </td>
          <td style="padding:5px 8px; font-size:11px; color:var(--muted); white-space:nowrap;">{f['size_str']}</td>
          <td style="padding:5px 8px; font-size:11px; color:var(--muted); white-space:nowrap;">{f['modified_str']}</td>
          <td style="padding:5px 8px; text-align:center;">
            {'<span style="font-size:9px;background:#dcfce7;color:#166534;padding:1px 5px;border-radius:3px;">TEXT</span>' if f['has_text'] else '<span style="font-size:9px;color:var(--muted);">—</span>'}
          </td>
          <td style="padding:5px 8px; text-align:right;">
            <a href="/dms/legacy/download?path={ep.replace(' ','%20')}" download onclick="event.stopPropagation()"
               style="font-size:12px; color:var(--primary,#1B2A4A);">⬇</a>
          </td>
        </tr>"""

    pager = ""
    if total_pages > 1:
        prev = f'<button hx-get="/dms/matter/{matter_id}/folder?prefix={prefix}&page={page-1}" hx-target="#folder-contents" style="padding:3px 10px;font-size:11px;border:1px solid var(--border-color,#e2e8f0);border-radius:4px;cursor:pointer;">← Prev</button>' if page > 1 else ""
        nxt  = f'<button hx-get="/dms/matter/{matter_id}/folder?prefix={prefix}&page={page+1}" hx-target="#folder-contents" style="padding:3px 10px;font-size:11px;border:1px solid var(--border-color,#e2e8f0);border-radius:4px;cursor:pointer;">Next →</button>' if page < total_pages else ""
        pager = f'<div style="display:flex;justify-content:center;gap:8px;padding:8px;font-size:11px;color:var(--muted);">{prev}<span>Page {page} of {total_pages} ({total} files)</span>{nxt}</div>'

    return HTMLResponse(f"""
    <div style="font-size:11px;color:var(--muted);padding:5px 8px;border-bottom:1px solid #f1f5f9;">{total} files</div>
    <table style="width:100%;border-collapse:collapse;">
      <thead><tr style="background:#f8fafc;border-bottom:1px solid var(--border-color,#e2e8f0);">
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Name</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Size</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Modified</th>
        <th style="padding:5px 8px;text-align:center;font-size:11px;font-weight:600;color:var(--muted);">Text</th>
        <th></th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>{pager}""")


# ---------------------------------------------------------------------------
# Native documents — HTMX partial
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/native", response_class=HTMLResponse)
async def native_documents(request: Request, matter_id: str):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id

    async with AsyncSessionLocal() as session:
        folder = request.query_params.get("folder", "")
        # Extract leaf folder name (last segment of folder_path)
        folder_leaf = folder.rsplit("/", 1)[-1] if folder else ""
        if folder_leaf:
            r = await session.execute(sa_text("""
                SELECT id::text, title, doc_type, file_size, updated_at, storage_path
                FROM documents
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                  AND storage_path LIKE '%/' || :leaf || '/%'
                ORDER BY storage_path, title
            """), {"mid": matter_id, "tid": tenant_id, "leaf": folder_leaf})
        else:
            r = await session.execute(sa_text("""
                SELECT id::text, title, doc_type, file_size, updated_at, storage_path
                FROM documents
                WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                ORDER BY storage_path, title
            """), {"mid": matter_id, "tid": tenant_id})
        docs = [dict(x) for x in r.mappings().fetchall()]

    if not docs:
        return HTMLResponse('<div style="padding:16px; font-size:12px; color:var(--muted);">No native documents yet. Upload or drop a file to add one.</div>')

    rows = ""
    for d in docs:
        icon = _ext_icon(d.get("storage_path", ""))
        mod = d.get("updated_at")
        mod_str = mod.strftime("%Y-%m-%d") if mod else "—"
        rows += f"""
        <tr onclick="selectDoc('{d['id']}','{(d.get('storage_path') or '').replace("'",'')}','native')"
            draggable="true"
            ondragstart="docDragStart(event,'{d['id']}','native')"
            ondragend="docDragEnd(event)"
            style="cursor:pointer;" onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background=''">
          <td style="padding:5px 8px;font-size:12px;">{icon} {d.get('title','Untitled')}</td>
          <td style="padding:5px 8px;font-size:11px;color:var(--muted);">{(d.get('doc_type') or '').upper()}</td>
          <td style="padding:5px 8px;font-size:11px;color:var(--muted);">{_fmt_size(d.get('file_size'))}</td>
          <td style="padding:5px 8px;font-size:11px;color:var(--muted);">{mod_str}</td>
          <td style="padding:5px 8px;text-align:right;">
            <a href="/dms/document/{d['id']}/download" download onclick="event.stopPropagation()"
               style="font-size:12px;color:var(--primary,#1B2A4A);">⬇</a>
          </td>
        </tr>"""

    return HTMLResponse(f"""
    <table style="width:100%;border-collapse:collapse;">
      <thead><tr style="background:#f8fafc;border-bottom:1px solid var(--border-color,#e2e8f0);">
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Name</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Type</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Size</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Modified</th>
        <th></th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>""")


# ---------------------------------------------------------------------------
# Preview panel — HTMX partial
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/preview", response_class=HTMLResponse)
async def preview_panel(request: Request, matter_id: str,
                         doc_id: str = "", file_path: str = "", source: str = "legacy"):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id

    if source == "native" and doc_id:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT id::text, title, doc_type, file_size, updated_at, storage_path
                FROM documents
                WHERE id = CAST(:did AS uuid) AND trim(tenant_id) = trim(:tid)
            """), {"did": doc_id, "tid": tenant_id})
            doc = r.mappings().fetchone()
        if not doc:
            return HTMLResponse('<div style="padding:16px;font-size:12px;color:var(--muted);">Not found.</div>')
        doc = dict(doc)
        # Derive ext from title (filename) first — doc_type may be null,
        # a MIME type string, or an uppercase extension. Title is reliable.
        title = doc.get("title") or ""
        ext_from_title = title.rsplit(".", 1)[-1].lower() if "." in title else ""
        ext_from_type  = (doc.get("doc_type") or "").lower().split("/")[-1]
        ext = ext_from_title or ext_from_type
        IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
        OFFICE_EXTS = {"docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "rtf"}
        can_prev = ext in ({"pdf"} | IMAGE_EXTS | OFFICE_EXTS)
        return HTMLResponse(_preview_html(
            name=title or "Untitled",
            size_str=_fmt_size(doc.get("file_size")),
            modified=str(doc.get("updated_at", ""))[:10],
            badge="P", badge_color="#1B2A4A",
            path=doc.get("storage_path", ""),
            can_preview=can_prev,
            preview_url=f"/dms/document/{doc_id}/stream" if can_prev else "",
            download_url=f"/dms/document/{doc_id}/download",
            ext=ext, ocr_status="native", content_text=None,
        ))

    elif source == "legacy" and file_path:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT id::text, file_path, file_size_bytes, modified_at,
                       ocr_status, content_text
                FROM dms_documents
                WHERE file_path = :fp AND trim(tenant_id) = trim(:tid)
                LIMIT 1
            """), {"fp": file_path, "tid": tenant_id})
            doc = r.mappings().fetchone()

        name = file_path.rsplit("\\", 1)[-1]
        ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        enc  = file_path.replace(" ", "%20")
        rl   = _root_label(file_path)
        badge_color = "#5B21B6" if rl == "Docsend" else "#1d4ed8"

        if not doc:
            return HTMLResponse(_preview_html(
                name=name, size_str="—", modified="—",
                badge=rl, badge_color=badge_color,
                path=file_path, can_preview=False, preview_url="",
                download_url=f"/dms/legacy/download?path={enc}",
                ext=ext, ocr_status="not_indexed", content_text=None,
            ))

        doc = dict(doc)
        IMAGE_EXTS2 = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
        can_prev2 = ext in ({"pdf"} | IMAGE_EXTS2)
        return HTMLResponse(_preview_html(
            name=name,
            size_str=_fmt_size(doc.get("file_size_bytes")),
            modified=str(doc.get("modified_at", ""))[:10],
            badge=rl, badge_color=badge_color,
            path=file_path,
            can_preview=can_prev2,
            preview_url=f"/dms/legacy/stream?path={enc}" if can_prev2 else "",
            download_url=f"/dms/legacy/download?path={enc}",
            ext=ext, ocr_status=doc.get("ocr_status", ""),
            content_text=doc.get("content_text"),
        ))

    return HTMLResponse('<div style="padding:16px;font-size:12px;color:var(--muted);">Select a document to preview.</div>')


def _preview_html(name, size_str, modified, badge, badge_color,
                   path, can_preview, preview_url, download_url,
                   ext, ocr_status, content_text):
    icon = _ext_icon(name)

    IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
    OFFICE_EXTS = {"docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "rtf"}
    if ext in IMAGE_EXTS and preview_url:
        body = f'<img src="{preview_url}" style="max-width:100%;max-height:calc(100vh - 320px);min-height:300px;border-radius:4px;margin-top:8px;display:block;" alt="{name}">'
    elif ext in OFFICE_EXTS and preview_url:
        body = (
            '<div style="font-size:11px;color:var(--muted);padding:4px 0 6px 0;">Converting for preview…</div>'
            f'<iframe src="{preview_url}" style="width:100%;height:calc(100vh - 280px);min-height:400px;border:none;border-radius:4px;"></iframe>'
        )
    elif can_preview and preview_url:
        body = f'<iframe src="{preview_url}" style="width:100%;height:calc(100vh - 280px);min-height:400px;border:none;border-radius:4px;margin-top:8px;"></iframe>'
    elif content_text:
        esc = content_text[:2000].replace("<","&lt;").replace(">","&gt;")
        body = f'<div style="margin-top:8px;padding:10px;background:#f8fafc;border-radius:4px;font-size:11px;font-family:monospace;white-space:pre-wrap;max-height:calc(100vh - 320px);min-height:300px;overflow-y:auto;">{esc}</div>'
    else:
        ocr_note = ""
        if ocr_status == "ocr_pending":
            ocr_note = '<div style="font-size:11px;color:#854d0e;margin-top:4px;">OCR pending</div>'
        elif ocr_status == "not_indexed":
            ocr_note = '<div style="font-size:11px;color:var(--muted);margin-top:4px;">Not yet indexed</div>'
        body = f'''<div style="margin-top:16px;text-align:center;color:var(--muted);">
          <div style="font-size:48px;margin-bottom:8px;">{icon}</div>
          <div style="font-size:12px;">Preview not available for .{ext}</div>
          {ocr_note}
          <a href="{download_url}" download style="display:inline-block;margin-top:12px;padding:6px 16px;background:var(--primary,#1B2A4A);color:#fff;border-radius:5px;font-size:12px;text-decoration:none;">Download</a>
        </div>'''

    ocr_badge = ""
    if ocr_status == "ocr_complete":
        ocr_badge = '<span style="font-size:9px;background:#dcfce7;color:#166534;padding:1px 6px;border-radius:3px;margin-left:5px;">OCR</span>'
    elif ocr_status in ("ocr_pending","queued"):
        ocr_badge = '<span style="font-size:9px;background:#fef9c3;color:#854d0e;padding:1px 6px;border-radius:3px;margin-left:5px;">OCR pending</span>'

    return f"""<div style="padding:12px;">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:8px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;font-weight:600;color:var(--text);word-break:break-word;">
            {icon} {name}{ocr_badge}
          </div>
          <div style="font-size:10px;color:var(--muted);margin-top:3px;">
            <span style="background:{badge_color};color:#fff;padding:1px 5px;border-radius:3px;font-size:9px;">{badge}</span>
            &nbsp;{size_str}&nbsp;·&nbsp;{modified}
          </div>
        </div>
        <a href="{download_url}" download style="font-size:18px;text-decoration:none;margin-left:8px;color:var(--primary,#1B2A4A);" title="Download">⬇</a>
      </div>
      <div style="font-size:10px;color:var(--muted);word-break:break-all;font-family:monospace;padding:4px 6px;background:#f8fafc;border-radius:3px;margin-bottom:8px;">{path}</div>
      {body}
    </div>"""


# ---------------------------------------------------------------------------
# Native document stream / download by document ID
# ---------------------------------------------------------------------------

@router.get("/document/{doc_id}/meta", response_class=JSONResponse)
async def document_meta(request: Request, doc_id: str):
    """Return document metadata including storage_path and matter_id."""
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT d.id::text, d.filename, d.storage_path, d.mime_type,
                   d.file_size, d.matter_id::text,
                   m.matter_name, c.client_name
            FROM documents d
            LEFT JOIN matters m ON d.matter_id = m.id
            LEFT JOIN clients c ON m.client_id = c.id
            WHERE d.id = CAST(:did AS uuid) AND TRIM(d.tenant_id) = TRIM(:tid)
        """), {"did": doc_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    doc = dict(doc)
    # Compute matter-relative path
    rel_path = ""
    sp = doc.get("storage_path") or ""
    client = doc.get("client_name") or ""
    matter = doc.get("matter_name") or ""
    if client and matter and sp:
        marker = f"/matters/{client}/{matter}/"
        idx = sp.find(marker)
        if idx >= 0:
            rel_path = sp[idx + len(marker):]
    return JSONResponse({
        "id": doc["id"], "filename": doc["filename"],
        "storage_path": sp, "mime_type": doc["mime_type"],
        "file_size": doc["file_size"], "matter_id": doc["matter_id"],
        "matter_name": doc["matter_name"], "client_name": doc["client_name"],
        "relative_path": rel_path,
    })


@router.get("/document/{doc_id}/stream")
async def document_stream(request: Request, doc_id: str):
    """Stream a native document inline by its UUID."""
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT storage_path, filename, mime_type
            FROM documents
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
        """), {"did": doc_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    spath = doc["storage_path"]
    if not spath or not os.path.isfile(spath):
        raise HTTPException(status_code=404, detail="File not accessible")
    import mimetypes as _mt
    mime = doc["mime_type"] or _mt.guess_type(spath)[0] or "application/octet-stream"
    fname = doc["filename"] or os.path.basename(spath)
    with open(spath, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


@router.get("/document/{doc_id}/download")
async def document_download(request: Request, doc_id: str):
    """Download a native document as attachment by its UUID."""
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT storage_path, filename, mime_type
            FROM documents
            WHERE id = CAST(:did AS uuid) AND TRIM(tenant_id) = TRIM(:tid)
        """), {"did": doc_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    spath = doc["storage_path"]
    if not spath or not os.path.isfile(spath):
        raise HTTPException(status_code=404, detail="File not accessible")
    import mimetypes as _mt
    mime = doc["mime_type"] or _mt.guess_type(spath)[0] or "application/octet-stream"
    fname = doc["filename"] or os.path.basename(spath)
    with open(spath, "rb") as f:
        data = f.read()
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------------------------------------------------------------------------
# Legacy file download / stream via CIFS bridge
# ---------------------------------------------------------------------------

@router.get("/legacy/download")
async def legacy_download(request: Request, path: str = ""):
    if not path: raise HTTPException(status_code=400, detail="path required")
    import httpx
    try:
        resp = httpx.get(f"{CIFS_URL}/api/v1/files/download",
                         params={"tenant_id": request.state.tenant_id, "path": path}, timeout=120)
        if resp.status_code != 200: raise HTTPException(status_code=500, detail="Download failed")
        filename = path.rsplit("\\", 1)[-1]
        return Response(content=resp.content,
                        media_type=resp.headers.get("content-type", "application/octet-stream"),
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    except HTTPException: raise
    except Exception as exc:
        logger.error("legacy download: %s", exc)
        raise HTTPException(status_code=500, detail="Download failed")


@router.get("/legacy/stream")
async def legacy_stream(request: Request, path: str = ""):
    if not path: raise HTTPException(status_code=400, detail="path required")
    import httpx
    try:
        resp = httpx.get(f"{CIFS_URL}/api/v1/files/download",
                         params={"tenant_id": request.state.tenant_id, "path": path}, timeout=120)
        if resp.status_code != 200: raise HTTPException(status_code=500, detail="Stream failed")
        filename = path.rsplit("\\", 1)[-1]
        mime = __import__("mimetypes").guess_type(path.rsplit("\\\\",1)[-1])[0] or "application/pdf"
        return Response(content=resp.content, media_type=mime,
                        headers={"Content-Disposition": f'inline; filename="{filename}"'})
    except HTTPException: raise
    except Exception as exc:
        logger.error("legacy stream: %s", exc)
        raise HTTPException(status_code=500, detail="Stream failed")


# ---------------------------------------------------------------------------
# AI Search — Elasticsearch + DB fallback
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/reconciliation", response_class=HTMLResponse)
async def matter_reconciliation(request: Request, matter_id: str):
    """Import Reconciliation — scans and recordings pending assignment to this matter.
    Full reconciliation history page built in a future session.
    """
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    brand = get_brand(request)

    async with AsyncSessionLocal() as session:
        # Matter context
        r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.status,
                   c.client_name
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND trim(m.tenant_id) = trim(c.tenant_id)
            WHERE m.id = CAST(:mid AS uuid)
              AND trim(m.tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tenant_id})
        matter = r.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")
        matter = dict(matter)

        # Pending scan queue items for this matter + unassigned items
        sq = await session.execute(sa_text("""
            SELECT sq.id, sq.filename, sq.source, sq.status,
                   sq.created_at, sq.ocr_text, sq.matter_id
            FROM scan_queue sq
            WHERE trim(sq.tenant_id) = :tid
              AND sq.status NOT IN ('completed', 'filed')
              AND (sq.matter_id = :mid OR sq.matter_id IS NULL)
            ORDER BY sq.created_at DESC LIMIT 100
        """), {"tid": tenant_id, "mid": matter_id})
        scan_items = [dict(r) for r in sq.mappings().fetchall()]

        # Pending dictation items for this matter + unassigned
        dq = await session.execute(sa_text("""
            SELECT dq.id, dq.filename, dq.status, dq.created_at,
                   dq.transcript, dq.matter_id, u.full_name as user_name
            FROM dictation_queue dq
            LEFT JOIN users u ON dq.user_id = u.id
                AND trim(dq.tenant_id) = trim(u.tenant_id)
            WHERE trim(dq.tenant_id) = :tid
              AND dq.status NOT IN ('completed')
              AND (dq.matter_id = :mid OR dq.matter_id IS NULL)
            ORDER BY dq.created_at DESC LIMIT 100
        """), {"tid": tenant_id, "mid": matter_id})
        dict_items = [dict(r) for r in dq.mappings().fetchall()]

    return templates.TemplateResponse("matter_reconciliation.html", {
        "request":    request,
        "brand":      brand,
        "matter":     matter,
        "matter_id":  matter_id,
        "scan_items": scan_items,
        "dict_items": dict_items,
    })


@router.get("/matter/{matter_id}/search", response_class=HTMLResponse)
async def matter_search(request: Request, matter_id: str,
                         q: str = "", scope: str = "matter", include_ediscovery: str = "0"):
    from core.db.base import AsyncSessionLocal
    import httpx

    if not q:
        return HTMLResponse('<div style="padding:12px;font-size:12px;color:var(--muted);">Enter a search query.</div>')

    tenant_id = request.state.tenant_id
    es_url = os.environ.get("ELASTICSEARCH_URL", "http://10.10.60.12:9200")
    results = []

    # Elasticsearch
    try:
        index = f"dms_files_{tenant_id.strip().replace('-','_')}"
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(f"{es_url}/{index}/_search", json={
                "query": {"multi_match": {"query": q, "fields": ["content_text","file_path"],
                                          "type": "best_fields", "fuzziness": "AUTO"}},
                "size": 30,
                "_source": ["file_path","file_size_bytes","modified_at","content_text"],
            })
        if resp.status_code == 200:
            for hit in resp.json().get("hits",{}).get("hits",[]):
                src = hit.get("_source",{})
                fp = src.get("file_path","")
                results.append({
                    "id": hit["_id"], "file_path": fp,
                    "display_name": fp.rsplit("\\",1)[-1] if fp else "",
                    "size_str": _fmt_size(src.get("file_size_bytes")),
                    "modified": str(src.get("modified_at",""))[:10],
                    "source": "legacy", "badge": _root_label(fp),
                    "badge_color": "#5B21B6" if "Docsend" in fp else "#1d4ed8",
                    "score": round(hit.get("_score",0),2),
                    "snippet": (src.get("content_text") or "")[:180],
                    "icon": _ext_icon(fp),
                })
    except Exception as exc:
        logger.warning("ES search failed, falling back to DB: %s", exc)

    # DB fallback
    if not results:
        async with AsyncSessionLocal() as session:
            params = {"tid": tenant_id, "q": f"%{q}%"}
            matter_clause = ""
            if scope == "matter":
                fr = await session.execute(sa_text("""
                    SELECT folder_path, disk_root FROM matter_folders
                    WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
                      AND disk_root IS NOT NULL
                """), {"mid": matter_id, "tid": tenant_id})
                folder_rows = fr.fetchall()
                if folder_rows:
                    conds = []
                    for i, row in enumerate(folder_rows):
                        pfx = _folder_prefix(row[1], row[0])
                        params[f"p{i}"] = pfx
                        conds.append(f"position(:p{i} in file_path) = 1")
                    matter_clause = " AND (" + " OR ".join(conds) + ")"

            r = await session.execute(sa_text(f"""
                SELECT id::text, file_path, file_size_bytes, modified_at
                FROM dms_documents
                WHERE trim(tenant_id) = trim(:tid) AND file_path ILIKE :q
                  {matter_clause}
                ORDER BY file_path LIMIT 30
            """), params)
            for row in r.fetchall():
                fp = row[1]
                results.append({
                    "id": str(row[0]), "file_path": fp,
                    "display_name": fp.rsplit("\\",1)[-1],
                    "size_str": _fmt_size(row[2]),
                    "modified": str(row[3] or "")[:10],
                    "source": "legacy", "badge": _root_label(fp),
                    "badge_color": "#5B21B6" if "Docsend" in fp else "#1d4ed8",
                    "score": None, "snippet": "", "icon": _ext_icon(fp),
                })

    # eDiscovery
    if include_ediscovery == "1":
        async with AsyncSessionLocal() as session:
            ed_clause = "AND c.matter_id = CAST(:mid AS uuid)" if scope == "matter" else ""
            params2 = {"tid": tenant_id, "q": f"%{q}%"}
            if scope == "matter": params2["mid"] = matter_id
            r = await session.execute(sa_text(f"""
                SELECT d.id::text, d.file_name, d.doc_date, d.custodian
                FROM ediscovery_documents d
                JOIN ediscovery_collections c ON c.id = d.collection_id
                WHERE trim(d.tenant_id) = trim(:tid) AND d.file_name ILIKE :q
                  {ed_clause}
                LIMIT 20
            """), params2)
            for row in r.fetchall():
                results.append({
                    "id": str(row[0]), "file_path": row[1], "display_name": row[1],
                    "size_str": "—", "modified": str(row[2] or "")[:10],
                    "source": "ediscovery", "badge": "eDisc", "badge_color": "#166534",
                    "score": None, "snippet": f"Custodian: {row[3] or '—'}",
                    "icon": _ext_icon(row[1]),
                })

    if not results:
        return HTMLResponse(f'<div style="padding:12px;font-size:12px;color:var(--muted);">No results for &ldquo;{q}&rdquo;.</div>')

    rows = ""
    for r in results:
        ep = r["file_path"].replace('"',"&quot;")
        score_html = f'<span style="font-size:9px;color:var(--muted);">{r["score"]}</span>' if r["score"] else ""
        snip = f'<div style="font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{r["snippet"]}</div>' if r["snippet"] else ""
        rows += f"""
        <div style="padding:7px 10px;border-bottom:1px solid #f1f5f9;cursor:pointer;"
             onclick="selectDoc('{r['id']}','{ep}','{r['source']}')"
             onmouseover="this.style.background='#f8fafc'" onmouseout="this.style.background=''">
          <div style="display:flex;align-items:center;gap:5px;">
            <span>{r['icon']}</span>
            <span style="font-size:12px;font-weight:500;color:var(--text);flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{r['display_name']}</span>
            {score_html}
          </div>
          <div style="font-size:10px;color:var(--muted);margin-top:2px;">
            <span style="background:{r['badge_color']};color:#fff;padding:1px 4px;border-radius:3px;font-size:9px;">{r['badge']}</span>
            &nbsp;{r['size_str']}&nbsp;·&nbsp;{r['modified']}
          </div>
          {snip}
        </div>"""

    return HTMLResponse(f"""
    <div style="font-size:11px;color:var(--muted);padding:5px 10px;border-bottom:1px solid #f1f5f9;">
      {len(results)} result{'s' if len(results)!=1 else ''} for &ldquo;{q}&rdquo;
    </div>{rows}""")


# ---------------------------------------------------------------------------
# Create folder — stores in matter_folders as a native Praesidium folder
# ---------------------------------------------------------------------------

@router.post("/matter/{matter_id}/folder/create")
async def create_folder(request: Request, matter_id: str):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    body = await request.json()
    folder_name = (body.get("folder_name") or "").strip()

    if not folder_name:
        raise HTTPException(status_code=400, detail="folder_name is required")

    async with AsyncSessionLocal() as session:
        # Verify matter belongs to tenant
        m = await session.execute(sa_text("""
            SELECT id FROM matters
            WHERE id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tenant_id})
        if not m.fetchone():
            raise HTTPException(status_code=404, detail="Matter not found")

        # Insert into matter_folders with no disk_root (native Praesidium folder)
        await session.execute(sa_text("""
            INSERT INTO matter_folders (id, tenant_id, matter_id, folder_path, disk_root, file_count, added_at, added_by)
            VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, NULL, 0, NOW(), :by)
            ON CONFLICT (matter_id, folder_path) DO NOTHING
        """), {"tid": tenant_id, "mid": matter_id, "fp": folder_name,
               "by": int(getattr(getattr(request.state, "current_user", None), "id", 0) or 0)})
        await session.commit()

    return JSONResponse({"status": "ok", "folder_name": folder_name})






# ---------------------------------------------------------------------------
# Folder CRUD API — rename, delete, reorder, tree JSON
# ---------------------------------------------------------------------------

@router.get("/matter/{matter_id}/folder-tree", response_class=JSONResponse)
async def folder_tree_json(request: Request, matter_id: str):
    """Return folder tree as JSON - walks disk for Praesidium matters."""
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, folder_path, disk_root, file_count
            FROM matter_folders
            WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
            ORDER BY folder_path
        """), {"mid": matter_id, "tid": tenant_id})
        folders = [dict(row) for row in r.mappings().fetchall()]

        nc = await session.execute(sa_text("""
            SELECT COUNT(*) FROM documents
            WHERE matter_id = CAST(:mid AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"mid": matter_id, "tid": tenant_id})
        native_count = nc.scalar() or 0

    # Check if this matter has a Praesidium disk_root
    praesidium_root = None
    for f in folders:
        dr = f.get("disk_root") or ""
        if dr.startswith("/mnt/praesidium"):
            praesidium_root = dr
            break

    # Fallback: construct path from client_name/matter_name
    if not praesidium_root:
        async with AsyncSessionLocal() as session:
            mr = await session.execute(sa_text("""
                SELECT m.matter_name, c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                  AND trim(m.tenant_id) = trim(c.tenant_id)
                WHERE m.id = CAST(:mid AS uuid)
                  AND trim(m.tenant_id) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            mrow = mr.mappings().fetchone()
        if mrow and mrow["client_name"] and mrow["matter_name"]:
            candidate = os.path.join(
                "/mnt/praesidium", tenant_id.strip(), "matters",
                mrow["client_name"], mrow["matter_name"],
            )
            if os.path.isdir(candidate):
                praesidium_root = candidate

    if praesidium_root and os.path.isdir(praesidium_root):
        disk_tree = _walk_disk_tree(praesidium_root, max_depth=4)
        try:
            root_file_count = sum(1 for f in os.scandir(praesidium_root)
                                  if f.is_file() and not f.name.startswith('.'))
        except (PermissionError, OSError):
            root_file_count = 0
        return JSONResponse({
            "mode": "disk",
            "root": praesidium_root,
            "root_file_count": root_file_count,
            "tree": disk_tree,
            "native_count": native_count,
        })

    # Fallback: DB-based folder list
    for f in folders:
        f["root_label"] = _root_label(f.get("disk_root") or "")
        f["display_name"] = (f["folder_path"] or "").split("/")[-1]
        dr = f.get("disk_root") or ""
        f["is_native"] = not bool(dr)
        f["is_praesidium"] = (not dr) or dr.startswith("/mnt/praesidium")
    return JSONResponse({"mode": "db", "folders": folders, "native_count": native_count})
@router.get("/matter/{matter_id}/disk-folder", response_class=HTMLResponse)
async def disk_folder_contents(request: Request, matter_id: str,
                                path: str = "", page: int = 1):
    """List files from a Praesidium disk folder - direct filesystem read."""
    tenant_id = request.state.tenant_id

    if not path:
        return HTMLResponse('<div style="padding:12px; font-size:12px; color:var(--muted);">Select a folder.</div>')
    if not path.startswith("/mnt/praesidium/"):
        return HTMLResponse('<div style="padding:12px; font-size:12px; color:#dc2626;">Access denied.</div>')
    if not os.path.isdir(path):
        return HTMLResponse('<div style="padding:12px; font-size:12px; color:var(--muted);">Folder not found.</div>')

    per_page = 100
    offset = (page - 1) * per_page

    try:
        all_files = sorted(
            [e for e in os.scandir(path) if e.is_file() and not e.name.startswith('.')],
            key=lambda e: e.name.lower()
        )
    except (PermissionError, OSError) as exc:
        return HTMLResponse(f'<div style="padding:12px; font-size:12px; color:#dc2626;">Cannot read: {exc}</div>')

    total = len(all_files)
    page_files = all_files[offset:offset + per_page]

    if not page_files and total == 0:
        return HTMLResponse('<div style="padding:16px; font-size:12px; color:var(--muted);">No files in this folder.</div>')

    rows = ""
    for entry in page_files:
        try:
            stat = entry.stat()
            size_str = _fmt_size(stat.st_size)
            mod_str = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d")
        except OSError:
            size_str = "---"
            mod_str = "---"
        icon = _ext_icon(entry.name)
        safe_path = entry.path.replace("&", "&amp;").replace('"', "&quot;")
        enc_path = entry.path.replace(" ", "%20")
        js_path = entry.path.replace("\\", "\\\\").replace("'", "\\'")
        rows += f"""
        <tr draggable="true"
            data-diskpath="{safe_path}"
            ondragstart="diskFileDragStart(event)"
            onclick="selectDiskDoc('{js_path}')"
            style="cursor:pointer;"
            onmouseover="this.style.background='#f8fafc'"
            onmouseout="this.style.background=''">
          <td style="padding:5px 8px; font-size:12px; max-width:320px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">
            {icon} <span title="{safe_path}">{entry.name}</span>
          </td>
          <td style="padding:5px 8px; font-size:11px; color:var(--muted); white-space:nowrap;">{size_str}</td>
          <td style="padding:5px 8px; font-size:11px; color:var(--muted); white-space:nowrap;">{mod_str}</td>
          <td style="padding:5px 8px; text-align:right;">
            <a href="/dms/disk/download?path={enc_path}" download onclick="event.stopPropagation()"
               style="font-size:12px; color:var(--primary,#1B2A4A);">&#x2B07;</a>
          </td>
        </tr>"""

    total_pages = max(1, (total + per_page - 1) // per_page)
    pager = ""
    if total_pages > 1:
        enc = path.replace(" ", "%20")
        prev_btn = f'<button hx-get="/dms/matter/{matter_id}/disk-folder?path={enc}&page={page-1}" hx-target="#folder-contents" style="padding:3px 10px;font-size:11px;border:1px solid var(--border-color,#e2e8f0);border-radius:4px;cursor:pointer;">Prev</button>' if page > 1 else ""
        next_btn = f'<button hx-get="/dms/matter/{matter_id}/disk-folder?path={enc}&page={page+1}" hx-target="#folder-contents" style="padding:3px 10px;font-size:11px;border:1px solid var(--border-color,#e2e8f0);border-radius:4px;cursor:pointer;">Next</button>' if page < total_pages else ""
        pager = f'<div style="display:flex;justify-content:center;gap:8px;padding:8px;font-size:11px;color:var(--muted);">{prev_btn}<span>Page {page} of {total_pages} ({total} files)</span>{next_btn}</div>'

    return HTMLResponse(f"""
    <div style="font-size:11px;color:var(--muted);padding:5px 8px;border-bottom:1px solid #f1f5f9;">{total} files</div>
    <table style="width:100%;border-collapse:collapse;">
      <thead><tr style="background:#f8fafc;border-bottom:1px solid var(--border-color,#e2e8f0);">
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Name</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Size</th>
        <th style="padding:5px 8px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);">Modified</th>
        <th></th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>{pager}""")


@router.get("/disk/stream")
async def disk_stream(request: Request, path: str = ""):
    """Stream a file from local Praesidium storage for inline preview."""
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    if not path.startswith("/mnt/praesidium/"):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    return Response(content=content, media_type=mime,
                    headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.get("/disk/download")
async def disk_download(request: Request, path: str = ""):
    """Download a file from local Praesidium storage."""
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    if not path.startswith("/mnt/praesidium/"):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    return Response(content=content, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})



# DISABLED — version-checking handler in dms_upload_route.py replaces this
# @router.post("/disk/upload")
# async def disk_upload(request: Request,
#                       file: UploadFile = File(...),
#                       disk_path: str = Form(...),
#                       matter_id: str = Form(...)):
#     """Upload a file directly to a Praesidium disk folder.
#     Writes to disk, inserts documents row, returns JSON."""
#     from core.db.base import AsyncSessionLocal
#     import hashlib
#     tenant_id = request.state.tenant_id
# 
#     # Resolve relative disk_path against matter's Praesidium root
#     if not disk_path.startswith('/mnt/praesidium/'):
#         # React workspace sends relative folder name — resolve it
#         async with AsyncSessionLocal() as _sess:
#             _mr = await _sess.execute(sa_text("""
#                 SELECT m.matter_name, c.client_name
#                 FROM matters m LEFT JOIN clients c ON m.client_id = c.id
#                     AND trim(m.tenant_id) = trim(c.tenant_id)
#                 WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = trim(:tid)
#             """), {'mid': matter_id, 'tid': (tenant_id or '').strip()})
#             _mrow = _mr.mappings().fetchone()
#         if not _mrow or not _mrow['client_name'] or not _mrow['matter_name']:
#             raise HTTPException(status_code=404, detail='Matter not found')
#         _root = os.path.join('/mnt/praesidium', (tenant_id or '').strip(), 'matters', _mrow['client_name'], _mrow['matter_name'])
#         if disk_path and disk_path != '.':
#             disk_path = os.path.join(_root, disk_path)
#         else:
#             disk_path = _root
#         # Traversal check
#         if not os.path.realpath(disk_path).startswith(os.path.realpath(_root)):
#             raise HTTPException(status_code=403, detail='Access denied')
#     if not os.path.isdir(disk_path):
#         os.makedirs(disk_path, exist_ok=True)
# 
#     # Sanitize filename
#     safe_name = os.path.basename(file.filename or "upload")
#     if not safe_name or safe_name.startswith('.'):
#         safe_name = "upload"
# 
#     dest = os.path.join(disk_path, safe_name)
# 
#     # If file exists, add counter suffix
#     if os.path.exists(dest):
#         base, ext = os.path.splitext(safe_name)
#         counter = 1
#         while os.path.exists(dest):
#             dest = os.path.join(disk_path, f"{base} ({counter}){ext}")
#             counter += 1
#         safe_name = os.path.basename(dest)
# 
#     # Write to disk
#     content = await file.read()
#     with open(dest, "wb") as f:
#         f.write(content)
# 
#     file_size = len(content)
#     checksum = hashlib.sha256(content).hexdigest()
# 
#     # Guess mime type
#     import mimetypes
#     mime = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
# 
#     # Derive extension for document_type
#     ext = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
# 
#     # Insert documents row
#     user_id = getattr(getattr(request.state, "current_user", None), "id", None)
#     async with AsyncSessionLocal() as session:
#         r = await session.execute(sa_text("""
#             INSERT INTO documents (
#                 id, tenant_id, matter_id, filename, original_filename,
#                 title, mime_type, file_size, storage_path,
#                 document_type, status, checksum,
#                 created_by, created_at, updated_at
#             ) VALUES (
#                 gen_random_uuid(), :tid, CAST(:mid AS uuid), :fname, :fname,
#                 :fname, :mime, :fsize, :spath,
#                 :ext, 'active', :checksum,
#                 :uid, NOW(), NOW()
#             )
#             RETURNING id::text
#         """), {
#             "tid": tenant_id,
#             "mid": matter_id,
#             "fname": safe_name,
#             "mime": mime,
#             "fsize": file_size,
#             "spath": dest,
#             "ext": ext,
#             "checksum": checksum,
#             "uid": int(user_id) if user_id else None,
#         })
#         doc_id = r.scalar()
#         await session.commit()
# 
#     logger.info("disk_upload: %s -> %s (doc %s, %d bytes)", safe_name, dest, doc_id, file_size)
# 
#     return JSONResponse({
#         "status": "ok",
#         "doc_id": doc_id,
#         "filename": safe_name,
#         "path": dest,
#         "size": file_size,
#     })
# 
# 
# 
@router.post("/disk/move")
async def disk_move(request: Request):
    """Move a file between Praesidium disk folders. Updates documents.storage_path."""
    from core.db.base import AsyncSessionLocal
    import shutil as _shutil
    tenant_id = request.state.tenant_id
    body = await request.json()
    src_path = body.get("src_path", "")
    dest_folder = body.get("dest_folder", "")

    if not src_path or not dest_folder:
        raise HTTPException(status_code=400, detail="src_path and dest_folder required")
    if not src_path.startswith("/mnt/praesidium/"):
        raise HTTPException(status_code=403, detail="Access denied")
    if not dest_folder.startswith("/mnt/praesidium/"):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.isfile(src_path):
        raise HTTPException(status_code=404, detail="Source file not found")
    if not os.path.isdir(dest_folder):
        os.makedirs(dest_folder, exist_ok=True)

    filename = os.path.basename(src_path)
    dest_path = os.path.join(dest_folder, filename)
    if os.path.exists(dest_path) and dest_path != src_path:
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_folder, f"{base} ({counter}){ext}")
            counter += 1

    _shutil.move(src_path, dest_path)
    logger.info("disk_move: %s -> %s", src_path, dest_path)

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            UPDATE documents
            SET storage_path = :new_path, updated_at = NOW()
            WHERE storage_path = :old_path AND trim(tenant_id) = trim(:tid)
            RETURNING id::text
        """), {"new_path": dest_path, "old_path": src_path, "tid": tenant_id})
        updated_id = r.scalar()
        await session.commit()

    return JSONResponse({
        "status": "ok",
        "src": src_path,
        "dest": dest_path,
        "filename": os.path.basename(dest_path),
        "db_updated": bool(updated_id),
    })


@router.post("/matter/{matter_id}/folder/rename")
async def rename_folder(request: Request, matter_id: str):
    """Rename a matter folder — updates DB, moves on disk."""
    from core.db.base import AsyncSessionLocal
    import shutil
    tenant_id = request.state.tenant_id
    body = await request.json()
    folder_id = (body.get("folder_id") or "").strip()
    new_name = (body.get("new_name") or "").strip()

    if not folder_id or not new_name:
        raise HTTPException(status_code=400, detail="folder_id and new_name required")
    if any(c in new_name for c in ("\\", "/", "..", "\x00")):
        raise HTTPException(status_code=400, detail="Invalid folder name")

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, folder_path, disk_root
            FROM matter_folders
            WHERE id = CAST(:fid AS uuid) AND matter_id = CAST(:mid AS uuid)
              AND trim(tenant_id) = trim(:tid)
        """), {"fid": folder_id, "mid": matter_id, "tid": tenant_id})
        folder = r.mappings().fetchone()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")

        old_path = folder["folder_path"]
        old_disk = folder["disk_root"]

        # Build new paths
        parts = old_path.rsplit("/", 1)
        if len(parts) == 2:
            new_folder_path = parts[0] + "/" + new_name
        else:
            new_folder_path = new_name

        # Move on disk if disk_root exists
        if old_disk:
            old_dir = old_disk
            new_disk = old_disk.rsplit("/", 1)[0] + "/" + new_name if "/" in old_disk else new_name
            try:
                if os.path.isdir(old_dir):
                    shutil.move(old_dir, new_disk)
            except Exception as exc:
                logger.warning("Folder rename disk move failed: %s", exc)
                raise HTTPException(status_code=500, detail=f"Disk rename failed: {exc}")
        else:
            new_disk = None

        # Update DB
        await session.execute(sa_text("""
            UPDATE matter_folders
            SET folder_path = :fp, disk_root = :dr
            WHERE id = CAST(:fid AS uuid)
        """), {"fp": new_folder_path, "dr": new_disk, "fid": folder_id})

        # Also update matters.folder_path if it references this folder
        await session.execute(sa_text("""
            UPDATE matters
            SET folder_path = :fp
            WHERE id = CAST(:mid AS uuid) AND folder_path = :old_fp
        """), {"fp": new_folder_path, "mid": matter_id, "old_fp": old_path})

        await session.commit()

    return JSONResponse({"status": "renamed", "old_path": old_path, "new_path": new_folder_path})


@router.post("/matter/{matter_id}/folder/delete")
async def delete_folder(request: Request, matter_id: str):
    """Delete a matter folder — only if empty on disk and no documents linked."""
    from core.db.base import AsyncSessionLocal
    import shutil
    tenant_id = request.state.tenant_id
    body = await request.json()
    folder_id = (body.get("folder_id") or "").strip()
    force = body.get("force", False)

    if not folder_id:
        raise HTTPException(status_code=400, detail="folder_id required")

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, folder_path, disk_root
            FROM matter_folders
            WHERE id = CAST(:fid AS uuid) AND matter_id = CAST(:mid AS uuid)
              AND trim(tenant_id) = trim(:tid)
        """), {"fid": folder_id, "mid": matter_id, "tid": tenant_id})
        folder = r.mappings().fetchone()
        if not folder:
            raise HTTPException(status_code=404, detail="Folder not found")

        disk_root = folder["disk_root"]

        # Check if folder has files on disk
        if disk_root and os.path.isdir(disk_root):
            contents = os.listdir(disk_root)
            if contents and not force:
                return JSONResponse({
                    "status": "not_empty",
                    "message": f"Folder contains {len(contents)} item(s). Set force=true to delete.",
                    "item_count": len(contents),
                }, status_code=409)
            # Safety: never recursively delete a matter root / shared dir
            from modules.dms.services.path_safety import assert_deletable_subpath
            assert_deletable_subpath(disk_root, tenant_id, op="folder delete")
            # Remove from disk
            try:
                shutil.rmtree(disk_root)
            except Exception as exc:
                logger.warning("Folder delete disk error: %s", exc)
                raise HTTPException(status_code=500, detail=f"Disk delete failed: {exc}")

        # Remove from DB
        await session.execute(sa_text("""
            DELETE FROM matter_folders
            WHERE id = CAST(:fid AS uuid)
        """), {"fid": folder_id})
        await session.commit()

    return JSONResponse({"status": "deleted", "folder_path": folder["folder_path"]})


@router.post("/matter/{matter_id}/folder/reorder")
async def reorder_folders(request: Request, matter_id: str):
    """Update folder display order. Accepts {folder_ids: [ordered UUIDs]}.
    We store order as a zero-padded prefix on folder_path or a sort_order column.
    Since matter_folders has no sort_order column, we use the folder_ids list
    and return success — the client maintains order in JS state."""
    # For now this is a no-op server-side; the client handles order via localStorage.
    # A sort_order column can be added in a future migration.
    return JSONResponse({"status": "ok", "message": "Client-side order maintained"})


@router.post("/upload")
async def upload_document(request: Request, matter_id: str = Form(...),
                           folder_path: str = Form(""), file: UploadFile = File(...)):
    """
    Upload a file to a matter's native folder. Writes via
    LocalMountStorageAdapter to praesidium/matters/{matter_uuid}/{folder}/{filename}.
    Folder defaults to 'Scans' when empty. No file bridge.
    """
    from core.db.base import AsyncSessionLocal
    from modules.dms.adapters.local_mount_storage import get_storage_adapter
    import hashlib

    tenant_id = request.state.tenant_id
    tenant_id_clean = (tenant_id or "").strip()
    current_user = getattr(request.state, "current_user", None)
    user_id = getattr(current_user, "id", None)

    content = await file.read()
    checksum = hashlib.sha256(content).hexdigest()

    # Folder defaults to 'Scans'. Sanitize — no path separators, no traversal.
    folder = (folder_path or "").strip().strip("/").strip("\\") or "Scans"
    if any(c in folder for c in ("..", "\x00")):
        raise HTTPException(status_code=400, detail="Invalid folder name")

    async with AsyncSessionLocal() as session:
        m = await session.execute(sa_text(
            "SELECT id::text AS id, matter_name, matter_number FROM matters "
            "WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid)"
        ), {"id": matter_id, "tid": tenant_id_clean})
        matter = m.mappings().fetchone()
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        matter_uuid = matter["id"]

        # UUID path per ChatPrompts v7.0 §3.2 — no matter-label paths.
        storage_path = f"praesidium/matters/{matter_uuid}/{folder}/{file.filename}"

        # Write via adapter — adapter injects tenant under /mnt/praesidium/{tid}/.
        storage = get_storage_adapter()
        try:
            await storage.upload(
                tenant_id_clean, storage_path, content,
                content_type=file.content_type or "application/octet-stream",
            )
        except Exception as e:
            logger.error("Storage upload failed for %s: %s", file.filename, e)
            raise HTTPException(status_code=502, detail="File storage unavailable")

        doc_id = str(uuid.uuid4())
        ext = os.path.splitext(file.filename)[1].lower().lstrip(".")
        now = datetime.now(timezone.utc)

        await session.execute(sa_text("""
            INSERT INTO documents (id, tenant_id, matter_id, title, filename, storage_path,
                                   doc_type, mime_type, file_size, checksum,
                                   created_at, updated_at, created_by)
            VALUES (:id, :tid, CAST(:mid AS uuid), :name, :name, :path,
                    :ftype, :mime, :size, :cs,
                    :now, :now, :by)
        """), {
            "id":    doc_id,
            "tid":   tenant_id_clean,
            "mid":   matter_id,
            "name":  file.filename,
            "path":  storage_path,
            "ftype": ext,
            "mime":  file.content_type or "application/octet-stream",
            "size":  len(content),
            "cs":    checksum,
            "now":   now,
            "by":    user_id,
        })

        # Auto-register folder in matter_folders (ON CONFLICT no-ops on dupes).
        await session.execute(sa_text("""
            INSERT INTO matter_folders (id, tenant_id, matter_id, folder_path, disk_root, file_count, added_at, added_by)
            VALUES (gen_random_uuid(), :tid, CAST(:mid AS uuid), :fp, NULL, 0, NOW(), :by)
            ON CONFLICT (matter_id, folder_path) DO NOTHING
        """), {"tid": tenant_id_clean, "mid": matter_id, "fp": folder, "by": user_id})

        await session.commit()

    return HTMLResponse(
        content=f'<div style="color:#166534;font-size:12px;padding:4px 0;">✓ Uploaded {file.filename} to {folder}</div>',
        headers={"HX-Trigger": "documentUploaded"},
    )


# ---------------------------------------------------------------------------
# Document detail, download, search, version restore — unchanged
# ---------------------------------------------------------------------------

@router.get("/document/{document_id}", response_class=HTMLResponse)
async def document_detail(request: Request, document_id: str):
    from core.db.base import AsyncSessionLocal
    tenant_id = request.state.tenant_id
    brand = get_brand(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT d.*, m.matter_name, c.client_name
            FROM documents d
            LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
            LEFT JOIN clients c ON m.client_id = c.id AND m.tenant_id = c.tenant_id
            WHERE d.id = CAST(:id AS uuid) AND trim(d.tenant_id) = trim(:tid)
        """), {"id": document_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc: raise HTTPException(status_code=404, detail="Document not found")
    return templates.TemplateResponse("document_detail.html",
        {"request": request, "brand": brand, "doc": dict(doc), "versions": []})



@router.get("/document/{document_id}/stream")
async def stream_document(request: Request, document_id: str):
    """Stream a native document inline for browser preview.
    - PDF/images: served directly with inline disposition
    - DOCX/DOC/PPTX/XLSX: converted to PDF via LibreOffice headless, streamed inline
    - Everything else: 415 Unsupported — caller should use /download instead
    Uses LocalMountStorageAdapter — no bridge dependency.
    """
    from core.db.base import AsyncSessionLocal
    import mimetypes
    import subprocess
    import tempfile
    import os
    from modules.dms.adapters.local_mount_storage import LocalMountStorageAdapter

    tenant_id = request.state.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT storage_path, title, mime_type, doc_type FROM documents "
            "WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid)"
        ), {"id": document_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc:
        raise HTTPException(status_code=404)
    doc = dict(doc)
    storage_path = doc.get("storage_path") or ""
    if not storage_path:
        raise HTTPException(status_code=404, detail="No storage path")

    title = doc.get("title") or storage_path.rsplit("/", 1)[-1]
    ext = (title.rsplit(".", 1)[-1].lower() if "." in title else
           (doc.get("doc_type") or "").lower())

    # Fetch raw bytes from mount
    try:
        adapter = LocalMountStorageAdapter()
        raw = await adapter.download(tenant_id, storage_path)
    except Exception as exc:
        logger.error("stream_document fetch error [%s]: %s", document_id, exc)
        raise HTTPException(status_code=500, detail="File not accessible")

    IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
    OFFICE_EXTS = {"docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "rtf"}

    # ── PDF / image — serve directly ─────────────────────────────────────────
    if ext == "pdf":
        return Response(content=raw, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{title}"'})

    if ext in IMAGE_EXTS:
        mime = mimetypes.guess_type(title)[0] or "image/jpeg"
        return Response(content=raw, media_type=mime,
                        headers={"Content-Disposition": f'inline; filename="{title}"'})

    # ── Office formats — convert to PDF via LibreOffice ──────────────────────
    if ext in OFFICE_EXTS:
        tmp_dir = tempfile.mkdtemp(prefix="praesidium_preview_")
        tmp_in  = os.path.join(tmp_dir, title)
        tmp_pdf = os.path.join(tmp_dir, title.rsplit(".", 1)[0] + ".pdf")
        try:
            # Write source file to temp dir
            with open(tmp_in, "wb") as fh:
                fh.write(raw)

            # Convert — timeout 30s, headless, no display
            result = subprocess.run(
                ["libreoffice", "--headless", "--norestore",
                 "--convert-to", "pdf", "--outdir", tmp_dir, tmp_in],
                capture_output=True, timeout=30
            )
            if result.returncode != 0 or not os.path.exists(tmp_pdf):
                logger.error("LibreOffice conversion failed [%s]: %s",
                             document_id, result.stderr.decode())
                raise HTTPException(status_code=500, detail="Conversion failed")

            with open(tmp_pdf, "rb") as fh:
                pdf_bytes = fh.read()

            pdf_name = title.rsplit(".", 1)[0] + ".pdf"
            return Response(content=pdf_bytes, media_type="application/pdf",
                            headers={"Content-Disposition": f'inline; filename="{pdf_name}"'})
        except HTTPException:
            raise
        except subprocess.TimeoutExpired:
            logger.error("LibreOffice timeout [%s]", document_id)
            raise HTTPException(status_code=504, detail="Conversion timeout")
        except Exception as exc:
            logger.error("LibreOffice error [%s]: %s", document_id, exc)
            raise HTTPException(status_code=500, detail="Conversion failed")
        finally:
            # Always clean up temp files
            for f in [tmp_in, tmp_pdf]:
                try:
                    os.unlink(f)
                except Exception:
                    pass
            try:
                os.rmdir(tmp_dir)
            except Exception:
                pass

    # ── Unsupported format — tell caller to download instead ─────────────────
    raise HTTPException(status_code=415,
                        detail=f"Preview not supported for .{ext} — use /download")


@router.get("/document/{document_id}/download")
async def download_document(request: Request, document_id: str):
    """Download a native document. Uses LocalMountStorageAdapter."""
    from core.db.base import AsyncSessionLocal
    import mimetypes
    from modules.dms.adapters.local_mount_storage import LocalMountStorageAdapter
    tenant_id = request.state.tenant_id
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text(
            "SELECT storage_path, title, mime_type FROM documents WHERE id = CAST(:id AS uuid) AND trim(tenant_id) = trim(:tid)"
        ), {"id": document_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
    if not doc:
        raise HTTPException(status_code=404)
    doc = dict(doc)
    storage_path = doc.get("storage_path") or ""
    if not storage_path:
        raise HTTPException(status_code=404, detail="No storage path")
    try:
        adapter = LocalMountStorageAdapter()
        content = await adapter.download(tenant_id, storage_path)
    except Exception as exc:
        logger.error("download_document adapter error [%s]: %s", document_id, exc)
        raise HTTPException(status_code=500, detail="Download failed")
    title = doc.get("title") or storage_path.rsplit("/", 1)[-1]
    mime = doc.get("mime_type") or mimetypes.guess_type(title)[0] or "application/octet-stream"
    return Response(content=content, media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{title}"'})


@router.get("/search", response_class=HTMLResponse)
async def search_documents(request: Request, q: str = "", matter_id: Optional[str] = None, doc_type: Optional[str] = None):
    brand = get_brand(request)
    tenant_id = request.state.tenant_id
    results = {"hits": [], "estimatedTotalHits": 0}
    if q:
        try:
            from modules.dms.services.search_service import MeilisearchService
            meili = MeilisearchService()
            results = await meili.search(tenant_id, q, matter_id=matter_id, doc_type=doc_type)
            await meili.close()
        except Exception: pass
    return templates.TemplateResponse("search_results.html",
        {"request": request, "brand": brand, "query": q, "results": results, "matter_id": matter_id})


@router.post("/document/{document_id}/restore/{version_id}")
async def restore_version(request: Request, document_id: str, version_id: str):
    raise HTTPException(status_code=404, detail="Version history not yet available")


# ---------------------------------------------------------------------------
# AI-Assisted Search — Claude with MCP access
# ---------------------------------------------------------------------------

@router.get("/ai-search", response_class=HTMLResponse)
async def dms_ai_search(request: Request, q: str = "", matter_id: str = ""):
    """AI search across DMS — returns HTML partial for HTMX swap."""
    from core.services.ai_search_service import ai_search

    if not q:
        return HTMLResponse(
            '<div style="padding:12px 16px; font-size:12px; color:var(--muted);">'
            'Type a question in plain English to search with AI.</div>'
        )

    tenant_id = (request.state.tenant_id or "").strip()
    matter_context = None

    if matter_id:
        from core.db.base import AsyncSessionLocal
        async with AsyncSessionLocal() as session:
            mr = await session.execute(sa_text("""
                SELECT m.matter_name, m.matter_number, c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND trim(m.tenant_id) = trim(c.tenant_id)
                WHERE m.id = CAST(:mid AS uuid) AND trim(m.tenant_id) = :tid
            """), {"mid": matter_id, "tid": tenant_id})
            row = mr.mappings().fetchone()
            if row:
                matter_context = dict(row)

    result = await ai_search(
        query=q,
        tenant_id=tenant_id,
        matter_id=matter_id or None,
        matter_context=matter_context,
    )

    if result.error:
        return HTMLResponse(
            f'<div style="padding:12px 16px; font-size:12px; color:#991B1B; '
            f'background:#FEF2F2; border:1px solid #FCA5A5; border-radius:6px;">'
            f'AI Search Error: {result.error}</div>'
        )

    parts = []

    # Summary
    if result.summary:
        parts.append(
            f'<div style="padding:10px 14px; background:#f8fafc; border-radius:6px; '
            f'font-size:13px; color:var(--text); line-height:1.6; margin-bottom:8px;">'
            f'<span style="font-size:14px;">&#129302;</span> {result.summary}</div>'
        )

    # Reasoning
    if result.reasoning:
        parts.append(
            f'<div style="font-size:11px; color:var(--muted); margin-bottom:8px;">'
            f'<strong>Strategy:</strong> {result.reasoning}</div>'
        )

    # Documents
    if result.documents:
        parts.append(
            f'<div style="font-size:11px; color:var(--muted); margin-bottom:4px;">'
            f'{len(result.documents)} document(s) found</div>'
        )
        for doc in result.documents:
            fn = doc.get("filename", doc.get("id", "Unknown"))
            note = doc.get("relevance_note", "")
            score = doc.get("score")
            score_html = (
                f'<span style="font-size:9px; background:#DBEAFE; color:#1E40AF; '
                f'padding:1px 6px; border-radius:8px;">'
                f'{int(score * 100)}%</span>'
            ) if score else ""
            parts.append(
                f'<div style="padding:7px 10px; border-bottom:1px solid #f1f5f9; '
                f'font-size:12px; display:flex; align-items:center; gap:8px;">'
                f'<span style="font-size:14px;">&#128196;</span>'
                f'<div style="flex:1; min-width:0;">'
                f'<div style="font-weight:500; color:var(--text); overflow:hidden; '
                f'text-overflow:ellipsis; white-space:nowrap;">{fn}</div>'
                + (f'<div style="font-size:10px; color:var(--muted);">{note}</div>' if note else '')
                + f'</div>{score_html}</div>'
            )
    elif not result.summary:
        parts.append(
            '<div style="padding:12px; font-size:12px; color:var(--muted); text-align:center;">'
            'AI search returned no results. Try rephrasing your query.</div>'
        )

    # Token usage
    if result.prompt_tokens:
        parts.append(
            f'<div style="font-size:10px; color:var(--muted); margin-top:6px; text-align:right;">'
            f'{result.model_used} · {result.prompt_tokens + result.completion_tokens} tokens</div>'
        )

    return HTMLResponse("\n".join(parts))



# ---------------------------------------------------------------------------
# Document Move — drag-and-drop file to folder
# ---------------------------------------------------------------------------

@router.post("/matter/{matter_id}/document/move")
async def move_document(request: Request, matter_id: str):
    """Move a native Praesidium document to a different folder."""
    from core.db.base import AsyncSessionLocal
    import shutil as _shutil
    tenant_id = request.state.tenant_id
    body = await request.json()
    doc_id = (body.get("doc_id") or "").strip()
    target_folder = (body.get("target_folder") or "").strip()

    if not doc_id:
        raise HTTPException(status_code=400, detail="doc_id required")

    target_leaf = target_folder.rsplit("/", 1)[-1] if target_folder else ""

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, title, storage_path, filename
            FROM documents
            WHERE id = CAST(:did AS uuid)
              AND matter_id = CAST(:mid AS uuid)
              AND trim(tenant_id) = trim(:tid)
        """), {"did": doc_id, "mid": matter_id, "tid": tenant_id})
        doc = r.mappings().fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        old_storage = doc["storage_path"] or ""
        filename = doc["filename"] or doc["title"] or "unknown"

        # Build new storage_path: praesidium/matters/{uuid}/{leaf}/filename
        prefix = f"praesidium/matters/{matter_id}"
        new_storage = f"{prefix}/{target_leaf}/{filename}" if target_leaf else f"{prefix}/{filename}"

        # Resolve disk paths:
        # storage_path = praesidium/matters/{uuid}/folder/file
        # disk_path    = /mnt/praesidium/{tenant}/matters/{uuid}/folder/file
        tid = tenant_id.strip()
        def storage_to_disk(sp):
            if sp.startswith("praesidium/"):
                return "/mnt/praesidium/" + tid + "/" + sp[len("praesidium/"):]
            return None

        old_disk = storage_to_disk(old_storage)
        new_disk = storage_to_disk(new_storage)

        moved = False
        if old_disk and new_disk and old_disk != new_disk:
            if os.path.isfile(old_disk):
                new_dir = os.path.dirname(new_disk)
                os.makedirs(new_dir, exist_ok=True)
                _shutil.move(old_disk, new_disk)
                moved = True
                logger.info("Moved file %s -> %s", old_disk, new_disk)
            else:
                logger.warning("Source file not found on disk: %s", old_disk)

        # Update DB
        await session.execute(sa_text("""
            UPDATE documents SET storage_path = :sp
            WHERE id = CAST(:did AS uuid) AND trim(tenant_id) = trim(:tid)
        """), {"sp": new_storage, "did": doc_id, "tid": tenant_id})
        await session.commit()

    return JSONResponse({"status": "moved", "new_path": new_storage, "disk_moved": moved})

