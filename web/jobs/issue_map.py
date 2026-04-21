"""
modules/ediscovery/issue_map.py

Issue Map router — living, versioned case theory map (S2-001).

Routes:
  GET  /ediscovery/matters/{matter_id}/issue-map              — current version UI
  GET  /ediscovery/matters/{matter_id}/issue-map/versions     — version history (HTMX partial)
  GET  /ediscovery/matters/{matter_id}/issue-map/versions/{n} — single version view
  POST /ediscovery/matters/{matter_id}/issue-map/trigger      — manual refresh

Exported helper:
  maybe_trigger_issue_map(tenant_id, matter_id, doc_type, doc_id)
    — called from document ingestion pipeline when a doc is added.
    — fires for: pleading, motion, order, expert_report.
    — does NOT fire for: transcript, email, or other types.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.db.base import get_session_factory
from modules.dashboard.services.auth_helper import get_current_user
from redis import Redis

router = APIRouter(tags=["issue-map"])
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")

# Document types that trigger issue map refresh (FIG. 2 — trigger detection subsystem)
ISSUE_MAP_TRIGGER_TYPES = {"pleading", "motion", "order", "expert_report"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fetch_current_version(session, tenant_id: str, matter_id: str) -> Optional[dict]:
    """Return the latest issue_map_versions row for a matter, or None."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            SELECT id, version_num, trigger_type, source_doc_ids,
                   issue_map, differential, magnitude, created_at, created_by
            FROM issue_map_versions
            WHERE tenant_id = :tenant_id AND matter_id = :matter_id
            ORDER BY version_num DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "matter_id": matter_id},
    )
    row = result.mappings().fetchone()
    return dict(row) if row else None


async def _fetch_version_history(session, tenant_id: str, matter_id: str) -> list[dict]:
    """Return all versions for a matter, newest first."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            SELECT id, version_num, trigger_type, magnitude, created_at, created_by
            FROM issue_map_versions
            WHERE tenant_id = :tenant_id AND matter_id = :matter_id
            ORDER BY version_num DESC
            """
        ),
        {"tenant_id": tenant_id, "matter_id": matter_id},
    )
    return [dict(r) for r in result.mappings().fetchall()]


async def _fetch_version_by_number(
    session, tenant_id: str, matter_id: str, version_number: int
) -> Optional[dict]:
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            SELECT id, version_num, trigger_type, source_doc_ids,
                   issue_map, differential, magnitude, created_at, created_by
            FROM issue_map_versions
            WHERE tenant_id = :tenant_id AND matter_id = :matter_id
              AND version_num = :version_number
            """
        ),
        {"tenant_id": tenant_id, "matter_id": matter_id, "version_number": version_number},
    )
    row = result.mappings().fetchone()
    return dict(row) if row else None


async def _fetch_matter(session, tenant_id: str, matter_id: str) -> Optional[dict]:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT id, matter_name FROM matters WHERE tenant_id = :tenant_id AND id = :matter_id"
        ),
        {"tenant_id": tenant_id, "matter_id": matter_id},
    )
    row = result.mappings().fetchone()
    return dict(row) if row else None


def _enqueue_refresh(tenant_id: str, matter_id: str, triggered_by: str, source_doc_ids: list):
    """Enqueue issue map refresh on the ediscovery RQ queue."""
    from rq import Queue

    redis_conn = Redis.from_url(REDIS_URL)
    q = Queue("ediscovery", connection=redis_conn)
    job = q.enqueue(
        "jobs.issue_map_job.run_issue_map_refresh",
        kwargs={
            "tenant_id": tenant_id,
            "matter_id": matter_id,
            "triggered_by": triggered_by,
            "source_document_ids": source_doc_ids,
        },
        job_timeout=600,
    )
    logger.info(
        "issue_map: enqueued refresh job %s for matter=%s triggered_by=%s",
        job.id,
        matter_id,
        triggered_by,
    )
    return job.id


# ---------------------------------------------------------------------------
# Exported trigger hook — called by document ingestion pipeline
# ---------------------------------------------------------------------------

def maybe_trigger_issue_map(
    tenant_id: str,
    matter_id: str,
    doc_type: str,
    doc_id: str,
) -> bool:
    """
    Called after a document is ingested. Enqueues an issue map refresh if
    the document type is trigger-eligible.

    Returns True if a job was enqueued, False otherwise.

    Trigger types: pleading | motion | order | expert_report
    Non-trigger types (pass-through): transcript | email | contract | other
    """
    if doc_type not in ISSUE_MAP_TRIGGER_TYPES:
        logger.debug(
            "issue_map: doc_type=%s is not trigger-eligible — skipping issue map refresh",
            doc_type,
        )
        return False

    try:
        _enqueue_refresh(
            tenant_id=tenant_id.strip(),
            matter_id=matter_id,
            triggered_by="document_added",
            source_doc_ids=[doc_id],
        )
        return True
    except Exception as exc:
        logger.error("issue_map: failed to enqueue refresh: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/ediscovery/matters/{matter_id}/issue-map", response_class=HTMLResponse)
async def issue_map_current(
    request: Request,
    matter_id: str,
    current_user=Depends(get_current_user),
):
    """Current version of the issue map for a matter."""
    tenant_id = request.state.tenant_id.strip()
    session_factory = get_session_factory()

    async with session_factory() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        version = await _fetch_current_version(session, tenant_id, matter_id)
        version_count = 0
        if version:
            history = await _fetch_version_history(session, tenant_id, matter_id)
            version_count = len(history)

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")

    branding = getattr(request.state, "branding", None)

    return templates.TemplateResponse(
        request,
        "ediscovery/issue_map.html",
        {
            "matter": matter,
            "matter_id": matter_id,
            "version": version,
            "version_count": version_count,
            "branding": branding,
            "user": current_user,
            "page": "issue_map",
        },
    )


@router.get("/ediscovery/matters/{matter_id}/issue-map/versions", response_class=HTMLResponse)
async def issue_map_version_history(
    request: Request,
    matter_id: str,
    current_user=Depends(get_current_user),
):
    """Version history list — HTMX partial."""
    tenant_id = request.state.tenant_id.strip()
    session_factory = get_session_factory()

    async with session_factory() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")
        versions = await _fetch_version_history(session, tenant_id, matter_id)

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")
    branding = getattr(request.state, "branding", None)

    return templates.TemplateResponse(
        request,
        "ediscovery/issue_map_history.html",
        {
            "matter": matter,
            "matter_id": matter_id,
            "versions": versions,
            "branding": branding,
            "user": current_user,
        },
    )


@router.get(
    "/ediscovery/matters/{matter_id}/issue-map/versions/{version_number}",
    response_class=HTMLResponse,
)
async def issue_map_single_version(
    request: Request,
    matter_id: str,
    version_number: int,
    current_user=Depends(get_current_user),
):
    """Single version view."""
    tenant_id = request.state.tenant_id.strip()
    session_factory = get_session_factory()

    async with session_factory() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

        version = await _fetch_version_by_number(session, tenant_id, matter_id, version_number)
        if not version:
            raise HTTPException(status_code=404, detail="Version not found")

        total_versions = len(await _fetch_version_history(session, tenant_id, matter_id))

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")
    branding = getattr(request.state, "branding", None)

    return templates.TemplateResponse(
        request,
        "ediscovery/issue_map.html",
        {
            "matter": matter,
            "matter_id": matter_id,
            "version": version,
            "version_count": total_versions,
            "branding": branding,
            "user": current_user,
            "page": "issue_map",
        },
    )


@router.post("/ediscovery/matters/{matter_id}/issue-map/trigger")
async def issue_map_trigger(
    request: Request,
    matter_id: str,
    current_user=Depends(get_current_user),
):
    """Manual refresh — enqueues job and returns 202."""
    tenant_id = request.state.tenant_id.strip()
    session_factory = get_session_factory()

    async with session_factory() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")

    try:
        job_id = _enqueue_refresh(
            tenant_id=tenant_id,
            matter_id=matter_id,
            triggered_by="manual",
            source_doc_ids=[],
        )
    except Exception as exc:
        logger.error("issue_map trigger: enqueue failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to enqueue issue map refresh")

    return JSONResponse(
        status_code=202,
        content={"status": "queued", "job_id": job_id, "matter_id": matter_id},
    )
