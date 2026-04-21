from sqlalchemy import text as sa_text
from modules.dms.brand_helper import get_brand
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

    return templates.TemplateResponse("dms_home.html", {
        "request": request,
        "brand": brand,
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

    return templates.TemplateResponse("matter_documents.html", {
        "request": request, "brand": brand,
        "matter": dict(matter), "linked_folders": linked_folders,
        "native_count": native_count, "matter_id": matter_id,
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
        body = f'<img src="{preview_url}" style="max-width:100%;max-height:420px;border-radius:4px;margin-top:8px;display:block;" alt="{name}">'
    elif ext in OFFICE_EXTS and preview_url:
        body = (
            '<div style="font-size:11px;color:var(--muted);padding:4px 0 6px 0;">Converting for preview…</div>'
            f'<iframe src="{preview_url}" style="width:100%;height:480px;border:none;border-radius:4px;"></iframe>'
        )
    elif can_preview and preview_url:
        body = f'<iframe src="{preview_url}" style="width:100%;height:420px;border:none;border-radius:4px;margin-top:8px;"></iframe>'
    elif content_text:
        esc = content_text[:2000].replace("<","&lt;").replace(">","&gt;")
        body = f'<div style="margin-top:8px;padding:10px;background:#f8fafc;border-radius:4px;font-size:11px;font-family:monospace;white-space:pre-wrap;max-height:420px;overflow-y:auto;">{esc}</div>'
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
