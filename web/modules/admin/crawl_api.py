"""
modules/admin/crawl_api.py
Module 9 Component 4 — File Crawl Trigger

Admin UI to trigger CIFS share crawls, view crawl status and progress,
and browse the legacy mount tree via the CIFS bridge.

Crawls are read-only. They index /mnt/clients and /mnt/docsend for
federated search — never writing to those mounts.

Endpoints:
  GET  /admin/crawl                  — crawl dashboard
  POST /admin/crawl/trigger          — trigger a new crawl job
  GET  /admin/crawl/{job_id}         — job detail + entry list
  POST /admin/crawl/{job_id}/cancel  — cancel a queued/running job
  GET  /admin/crawl/browse           — HTMX: browse CIFS share tree

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

import httpx
from fastapi import APIRouter, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin.crawl_api")

router = APIRouter(prefix="/admin/crawl", tags=["admin-crawl"])

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../../templates/admin")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

CIFS_URL = os.environ.get("CIFS_URL", "http://10.10.60.13:8080")
REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

MOUNT_OPTIONS = [
    {"value": "clients",    "label": "Legacy Clients (\\\\Clients)",   "readonly": False},
    {"value": "docsend",    "label": "Legacy DocSend (\\\\DocSend)",   "readonly": True},
    {"value": "praesidium", "label": "Praesidium (\\\\Praesidium)",   "readonly": False},
]


# ── Auth helper (reuse admin session pattern) ─────────────────────────────────

async def _require_admin(request: Request) -> dict:
    session_token = (
        request.cookies.get("session_token")
        or request.cookies.get("admin_session")
    )
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("""
                SELECT s.user_id, s.tenant_id, s.expires_at, u.role
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = :token AND s.expires_at > NOW()
            """),
            {"token": session_token},
        )
        session = row.fetchone()
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    return {
        "user_id": session.user_id,
        "tenant_id": (session.tenant_id or "").strip(),
        "role": session.role,
    }


# ── CIFS bridge helpers ───────────────────────────────────────────────────────

async def _cifs_health() -> dict:
    """Check CIFS bridge health. Returns health dict or error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{CIFS_URL}/health")
            if resp.status_code == 200:
                return resp.json()
    except Exception as exc:
        log.warning("CIFS bridge health check failed: %s", exc)
    return {"status": "unreachable"}


async def _cifs_list(mount: str, path: str = "", tenant_id: str = "") -> list:
    """List files/dirs at a path on the CIFS bridge. Returns [] on failure."""
    try:
        rel = f"{mount}/{path}".strip("/")
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{CIFS_URL}/api/v1/files/list",
                params={"tenant_id": tenant_id or "platform", "path": rel}
            )
            if resp.status_code == 200:
                return resp.json().get("files", [])
    except Exception as exc:
        log.warning("CIFS list failed for %s/%s: %s", mount, path, exc)
    return []


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def crawl_dashboard(request: Request, tenant_id: Optional[str] = None):
    sess = await _require_admin(request)
    tid = tenant_id or sess["tenant_id"]

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text("""
                SELECT id, tenant_id, mount, root_path, status,
                       queued_at, started_at, completed_at,
                       files_discovered, files_indexed, files_skipped,
                       error_message, rq_job_id
                FROM cifs_crawl_jobs
                WHERE tenant_id = :tid
                ORDER BY queued_at DESC
                LIMIT 50
            """),
            {"tid": tid},
        )
        jobs = [dict(r) for r in rows.mappings().fetchall()]

    cifs_health = await _cifs_health()

    return templates.TemplateResponse(request, "crawl.html", {
        "page": "crawl",
        "jobs": jobs,
        "tenant_id": tid,
        "mount_options": MOUNT_OPTIONS,
        "cifs_health": cifs_health,
        "branding": getattr(request.state, "branding", None),
    })


@router.post("/trigger")
async def crawl_trigger(
    request: Request,
    tenant_id: str = Form(...),
    mount: str = Form(...),
    root_path: str = Form(""),
    depth_limit: int = Form(10),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    if mount not in ("clients", "docsend", "praesidium"):
        raise HTTPException(status_code=400, detail=f"Invalid mount: {mount}")

    job_id = str(uuid.uuid4())

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                INSERT INTO cifs_crawl_jobs
                  (id, tenant_id, mount, root_path, status,
                   triggered_by, options)
                VALUES
                  (:id, :tid, :mount, :root_path, 'queued',
                   :uid, :options)
            """),
            {
                "id": job_id,
                "tid": tid,
                "mount": mount,
                "root_path": root_path.strip("/"),
                "uid": sess["user_id"],
                "options": f'{{"depth_limit": {depth_limit}}}',
            },
        )
        await db.commit()

    # Enqueue RQ job
    try:
        from redis import Redis
        from rq import Queue
        redis_conn = Redis.from_url(REDIS_URL)
        q = Queue("default", connection=redis_conn)
        q.enqueue(
            "jobs.crawl_job.run",
            job_id,
            job_timeout=3600,
        )
        async with AsyncSessionLocal() as db2:
            await db2.execute(
                text("UPDATE cifs_crawl_jobs SET rq_job_id = :jid WHERE id = :id"),
                {"jid": "enqueued", "id": job_id},
            )
            await db2.commit()
    except Exception as exc:
        log.error("Failed to enqueue crawl job %s: %s", job_id, exc)
        async with AsyncSessionLocal() as db3:
            await db3.execute(
                text("""
                    UPDATE cifs_crawl_jobs
                    SET status = 'failed', error_message = :err
                    WHERE id = :id
                """),
                {"err": f"Queue error: {str(exc)[:200]}", "id": job_id},
            )
            await db3.commit()

    return RedirectResponse(f"/admin/crawl?tenant_id={tid}", status_code=303)


@router.get("/browse", response_class=HTMLResponse)
async def crawl_browse(
    request: Request,
    mount: str = Query("clients"),
    path: str = Query(""),
    tenant_id: Optional[str] = None,
):
    """HTMX partial — browse CIFS share tree."""
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    if mount not in ("clients", "docsend", "praesidium"):
        return HTMLResponse('<p class="text-red-400 text-xs">Invalid mount.</p>')

    entries = await _cifs_list(mount, path, tid)

    # Sort: directories first, then files
    entries.sort(key=lambda e: (not e.get("is_directory", False), e.get("name", "")))

    lines = []
    for e in entries[:200]:  # cap at 200 per request
        name = e.get("name", "")
        is_dir = e.get("is_directory", False)
        child_path = f"{path}/{name}".strip("/") if path else name
        size = e.get("size", 0)
        size_str = _fmt_size(size) if not is_dir else ""

        if is_dir:
            lines.append(
                f'<div class="flex items-center gap-2 py-1 hover:bg-gray-800 px-2 rounded cursor-pointer"'
                f' hx-get="/admin/crawl/browse?mount={mount}&path={child_path}&tenant_id={tid}"'
                f' hx-target="#browse-tree" hx-swap="innerHTML">'
                f'<svg class="w-4 h-4 text-yellow-400 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">'
                f'<path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>'
                f'<span class="text-sm text-gray-200">{name}</span>'
                f'</div>'
            )
        else:
            lines.append(
                f'<div class="flex items-center gap-2 py-1 px-2">'
                f'<svg class="w-4 h-4 text-gray-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
                f'<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"'
                f' d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>'
                f'<span class="text-sm text-gray-400 flex-1 truncate">{name}</span>'
                f'<span class="text-xs text-gray-600">{size_str}</span>'
                f'</div>'
            )

    if not lines:
        lines.append('<p class="text-sm text-gray-500 px-2 py-4">Empty directory or unreachable.</p>')

    if path:
        parent = "/".join(path.split("/")[:-1])
        lines.insert(0,
            f'<div class="flex items-center gap-2 py-1 hover:bg-gray-800 px-2 rounded cursor-pointer mb-1"'
            f' hx-get="/admin/crawl/browse?mount={mount}&path={parent}&tenant_id={tid}"'
            f' hx-target="#browse-tree" hx-swap="innerHTML">'
            f'<span class="text-gray-400 text-sm">← Back</span>'
            f'</div>'
        )

    return HTMLResponse("\n".join(lines))


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size // 1024} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size // (1024 * 1024)} MB"
    return f"{size / (1024 ** 3):.1f} GB"


@router.get("/{job_id}", response_class=HTMLResponse)
async def crawl_job_detail(
    request: Request,
    job_id: str,
    tenant_id: Optional[str] = None,
    page: int = 1,
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()
    per_page = 100
    offset = (page - 1) * per_page

    async with AsyncSessionLocal() as db:
        job_row = await db.execute(
            text("""
                SELECT id, tenant_id, mount, root_path, status,
                       queued_at, started_at, completed_at,
                       files_discovered, files_indexed, files_skipped,
                       error_message, rq_job_id
                FROM cifs_crawl_jobs
                WHERE id = :jid AND tenant_id = :tid
            """),
            {"jid": job_id, "tid": tid},
        )
        job = job_row.mappings().fetchone()
        if not job:
            raise HTTPException(status_code=404, detail="Crawl job not found")

        entries_row = await db.execute(
            text("""
                SELECT file_path, file_name, file_size_bytes, mime_type,
                       modified_at, is_directory, matter_id, match_confidence,
                       discovered_at
                FROM cifs_crawl_entries
                WHERE job_id = :jid
                ORDER BY file_path ASC
                LIMIT :limit OFFSET :offset
            """),
            {"jid": job_id, "limit": per_page, "offset": offset},
        )
        entries = [dict(r) for r in entries_row.mappings().fetchall()]

        count_row = await db.execute(
            text("SELECT COUNT(*) FROM cifs_crawl_entries WHERE job_id = :jid"),
            {"jid": job_id},
        )
        total_entries = count_row.scalar() or 0

    return templates.TemplateResponse(request, "crawl_detail.html", {
        "page": "crawl",
        "job": dict(job),
        "entries": entries,
        "tenant_id": tid,
        "current_page": page,
        "total_entries": total_entries,
        "per_page": per_page,
        "total_pages": max(1, (total_entries + per_page - 1) // per_page),
        "branding": getattr(request.state, "branding", None),
    })


@router.post("/{job_id}/cancel")
async def crawl_cancel(
    request: Request,
    job_id: str,
    tenant_id: str = Form(...),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("""
                UPDATE cifs_crawl_jobs
                SET status = 'cancelled', completed_at = NOW()
                WHERE id = :jid AND tenant_id = :tid
                  AND status IN ('queued', 'running')
                RETURNING id
            """),
            {"jid": job_id, "tid": tid},
        )
        cancelled = result.fetchone()
        await db.commit()

    if not cancelled:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")

    return RedirectResponse(f"/admin/crawl?tenant_id={tid}", status_code=303)


