"""
modules/ediscovery/drift_detection.py

Drift Detection router — case theory drift map (S2-001, FIG. 2, FIG. 11).

Routes:
  GET  /ediscovery/matters/{matter_id}/drift-map      — drift timeline UI
  GET  /ediscovery/matters/{matter_id}/drift-events   — event list (HTMX partial)

Exported hooks:
  maybe_trigger_drift_from_issue_map(tenant_id, matter_id, version_num, magnitude)
    — called from issue_map_job after writing a new version with magnitude > 0.05

  maybe_trigger_drift_from_transcript(tenant_id, matter_id, doc_id)
    — called from document ingestion when doc_type == 'transcript'
    — per FIG. 2: transcripts trigger drift detection, not issue map refresh
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user
from redis import Redis

router = APIRouter(tags=["drift-detection"])
logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
MIN_TRIGGER_MAGNITUDE = 0.05


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _fetch_drift_events(session, tenant_id: str, matter_id: str) -> list[dict]:
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            SELECT id, version_before, version_after, dimension, drift_type,
                   description, source_doc_id, magnitude, detection_source,
                   wiam_surfaced, created_at
            FROM drift_events
            WHERE tenant_id = :tenant_id AND matter_id = :matter_id
            ORDER BY created_at DESC
            """
        ),
        {"tenant_id": tenant_id, "matter_id": matter_id},
    )
    return [dict(r) for r in result.mappings().fetchall()]


async def _fetch_matter(session, tenant_id: str, matter_id: str) -> Optional[dict]:
    from sqlalchemy import text

    result = await session.execute(
        text("SELECT id, matter_name FROM matters WHERE tenant_id = :tenant_id AND id = :matter_id"),
        {"tenant_id": tenant_id, "matter_id": matter_id},
    )
    row = result.mappings().fetchone()
    return dict(row) if row else None


def _enqueue_drift_job(
    tenant_id: str,
    matter_id: str,
    version_num: int,
    source_document_id: Optional[str] = None,
    triggered_by: str = "system",
) -> str:
    from rq import Queue

    redis_conn = Redis.from_url(REDIS_URL)
    q = Queue("ediscovery", connection=redis_conn)
    job = q.enqueue(
        "jobs.drift_detection_job.run_drift_detection",
        kwargs={
            "tenant_id": tenant_id,
            "matter_id": matter_id,
            "issue_map_version_num": version_num,
            "source_document_id": source_document_id,
            "triggered_by": triggered_by,
        },
        job_timeout=300,
    )
    logger.info(
        "drift_detection: enqueued job %s for matter=%s version=%d triggered_by=%s",
        job.id, matter_id, version_num, triggered_by,
    )
    return job.id


# ---------------------------------------------------------------------------
# Exported trigger hooks
# ---------------------------------------------------------------------------

def maybe_trigger_drift_from_issue_map(
    tenant_id: str,
    matter_id: str,
    version_num: int,
    magnitude: float,
) -> bool:
    """
    Called from issue_map_job after writing a new version.
    Enqueues drift detection if magnitude exceeds minimum threshold.
    Returns True if job was enqueued.
    """
    if magnitude < MIN_TRIGGER_MAGNITUDE:
        logger.debug(
            "drift_detection: magnitude %.4f below threshold — skipping drift for matter=%s",
            magnitude, matter_id,
        )
        return False

    try:
        _enqueue_drift_job(
            tenant_id=tenant_id.strip(),
            matter_id=matter_id,
            version_num=version_num,
            triggered_by="issue_map",
        )
        return True
    except Exception as exc:
        logger.error("drift_detection: failed to enqueue from issue_map: %s", exc)
        return False


def maybe_trigger_drift_from_transcript(
    tenant_id: str,
    matter_id: str,
    doc_id: str,
) -> bool:
    """
    Called from document ingestion when doc_type == 'transcript'.
    Per FIG. 2: transcripts trigger drift detection directly, not issue map refresh.
    Gets the latest issue map version number and enqueues drift analysis against it.
    Returns True if job was enqueued.
    """
    import psycopg2
    import psycopg2.extras

    try:
        import os
        raw_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        at = raw_url.rfind("@")
        creds = raw_url[len("postgresql://"):at]
        hp = raw_url[at + 1:]
        colon = creds.rfind(":")
        user = creds[:colon]
        pw = creds[colon + 1:]
        slash = hp.find("/")
        host = hp[:slash].split(":")[0]
        db = hp[slash + 1:]

        conn = psycopg2.connect(host=host, port=5432, dbname=db, user=user, password=pw)
        conn.autocommit = True
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute(
            """
            SELECT version_num FROM issue_map_versions
            WHERE tenant_id = %s AND matter_id = %s
            ORDER BY version_num DESC LIMIT 1
            """,
            (tenant_id.strip(), matter_id),
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            logger.info(
                "drift_detection: no issue map versions for matter=%s — skipping transcript drift",
                matter_id,
            )
            return False

        _enqueue_drift_job(
            tenant_id=tenant_id.strip(),
            matter_id=matter_id,
            version_num=row["version_num"],
            source_document_id=doc_id,
            triggered_by="transcript",
        )
        return True

    except Exception as exc:
        logger.error("drift_detection: failed to enqueue from transcript: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/ediscovery/matters/{matter_id}/drift-map", response_class=HTMLResponse)
async def drift_map(
    request: Request,
    matter_id: str,
    current_user=Depends(get_current_user),
):
    """Drift timeline UI — all drift events for a matter."""
    tenant_id = request.state.tenant_id.strip()

    async with AsyncSessionLocal() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")
        events = await _fetch_drift_events(session, tenant_id, matter_id)

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")
    branding = getattr(request.state, "branding", None)

    wiam_count    = sum(1 for e in events if e.get("wiam_surfaced"))
    high_count    = sum(1 for e in events if float(e.get("magnitude", 0)) > 0.6)
    moderate_count = sum(1 for e in events if 0.3 < float(e.get("magnitude", 0)) <= 0.6)

    return templates.TemplateResponse(
        request,
        "ediscovery/drift_map.html",
        {
            "matter": matter,
            "matter_id": matter_id,
            "events": events,
            "event_count": len(events),
            "wiam_count": wiam_count,
            "high_count": high_count,
            "moderate_count": moderate_count,
            "branding": branding,
            "user": current_user,
            "page": "drift_map",
        },
    )


@router.get("/ediscovery/matters/{matter_id}/drift-events", response_class=HTMLResponse)
async def drift_events_partial(
    request: Request,
    matter_id: str,
    current_user=Depends(get_current_user),
):
    """HTMX partial — drift event list."""
    tenant_id = request.state.tenant_id.strip()

    async with AsyncSessionLocal() as session:
        matter = await _fetch_matter(session, tenant_id, matter_id)
        if not matter:
            raise HTTPException(status_code=404, detail="Matter not found")
        events = await _fetch_drift_events(session, tenant_id, matter_id)

    from fastapi.templating import Jinja2Templates
    templates = Jinja2Templates(directory="templates")
    branding = getattr(request.state, "branding", None)

    return templates.TemplateResponse(
        request,
        "ediscovery/drift_events_partial.html",
        {
            "matter": matter,
            "matter_id": matter_id,
            "events": events,
            "branding": branding,
            "user": current_user,
        },
    )
