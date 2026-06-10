"""
modules/ediscovery/routes/collection_status.py

Collection status page route.
GET /ediscovery/collections/{collection_id}/status
  Full page — processing stats, activity log, Legal Intelligence Chat

GET /ediscovery/collections/{collection_id}/status-partial
  HTMX partial — stats only, auto-refreshes while ingesting

GET /ediscovery/collections/{collection_id}/log-partial
  HTMX partial — activity log, auto-refreshes
"""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
_templates = Jinja2Templates(directory=["core/templates", "modules/ediscovery/templates/ediscovery", "templates/ediscovery"])

from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_collection(tenant_id: str, collection_id: str) -> Optional[dict]:
    """Load collection with matter info."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT
                        c.id::text,
                        c.collection_name,
                        c.status,
                        c.source_type,
                        c.source_party,
                        c.total_docs,
                        c.processed_docs,
                        c.reviewed_docs,
                        c.received_date,
                        c.stated_bates_range,
                        c.created_at,
                        c.updated_at,
                        c.matter_id::text,
                        m.matter_name,
                        m.matter_number
                    FROM ediscovery_collections c
                    LEFT JOIN matters m ON m.id = c.matter_id
                    WHERE c.id::text = :cid
                      AND TRIM(c.tenant_id) = :tid
                    LIMIT 1
                """),
                {"cid": collection_id, "tid": tenant_id}
            )
            row = result.mappings().first()
            if not row:
                return None
            col = dict(row)

            # Get duplicate and error counts from documents table
            counts = await session.execute(
                text("""
                    SELECT
                        COUNT(*) FILTER (WHERE is_duplicate = true) as dup_count,
                        COUNT(*) FILTER (WHERE extracted_text IS NULL AND is_duplicate = false) as error_count
                    FROM ediscovery_documents
                    WHERE collection_id::text = :cid
                      AND TRIM(tenant_id) = :tid
                """),
                {"cid": collection_id, "tid": tenant_id}
            )
            counts_row = counts.first()
            col['duplicate_count'] = counts_row[0] if counts_row else 0
            col['error_count'] = counts_row[1] if counts_row else 0

            return col
    except Exception as e:
        logger.error(f"_get_collection error: {e}")
        return None


async def _get_doc_types(tenant_id: str, collection_id: str) -> list:
    """Get document type breakdown for this collection."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT doc_type, COUNT(*) as count
                    FROM ediscovery_documents
                    WHERE collection_id::text = :cid
                      AND TRIM(tenant_id) = :tid
                      AND is_duplicate = false
                    GROUP BY doc_type
                    ORDER BY count DESC
                    LIMIT 8
                """),
                {"cid": collection_id, "tid": tenant_id}
            )
            rows = result.fetchall()
            if not rows:
                return []
            total = sum(r[1] for r in rows)
            return [
                {
                    "doc_type": r[0] or "other",
                    "count": r[1],
                    "pct": int(r[1] / total * 100) if total > 0 else 0,
                }
                for r in rows
            ]
    except Exception as e:
        logger.warning(f"_get_doc_types error: {e}")
        return []


async def _get_audit_log(tenant_id: str, collection_id: str) -> list:
    """Get audit log entries for this collection."""
    from sqlalchemy import text
    from core.db.base import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT action, details, created_at
                    FROM audit_log
                    WHERE record_id = :cid
                      AND table_name = 'ediscovery_collections'
                      AND TRIM(tenant_id) = :tid
                    ORDER BY created_at DESC
                    LIMIT 50
                """),
                {"cid": collection_id, "tid": tenant_id}
            )
            rows = result.fetchall()
            entries = []
            for row in rows:
                action, details, created_at = row
                level = "ok" if "complete" in (action or "") else \
                        "error" if "error" in (action or "") else ""
                time_str = created_at.strftime("%H:%M:%S") if created_at else ""

                # Build human-readable message from action + details
                detail_str = ""
                if details and isinstance(details, dict):
                    if details.get("total"):
                        detail_str = f" — {details['processed']} processed, {details.get('duplicates',0)} dupes, {details.get('errors',0)} errors"
                msg = (action or "").replace("_", " ").title() + detail_str

                entries.append({
                    "time_str": time_str,
                    "message": msg,
                    "level": level,
                })
            return entries
    except Exception as e:
        logger.warning(f"_get_audit_log error: {e}")
        return []


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/ediscovery/collections/{collection_id}/status-page", response_class=HTMLResponse)
async def collection_status_page(
    request: Request,
    collection_id: str,
    user=Depends(get_current_user),
):
    """Full collection status page."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    env = Environment(
        loader=FileSystemLoader(["/app/modules/ediscovery/templates", "/app/core/templates"]),
        autoescape=select_autoescape(["html"]),
    )

    collection = await _get_collection(tenant_id, collection_id)
    if not collection:
        return HTMLResponse("<h2>Collection not found</h2>", status_code=404)

    doc_types  = await _get_doc_types(tenant_id, collection_id)
    audit_log  = await _get_audit_log(tenant_id, collection_id)

    tmpl = env.get_template("ediscovery/collection_status.html")
    return HTMLResponse(tmpl.render(
        request=request,
        collection=collection,
        doc_types=doc_types,
        audit_log=audit_log,
        brand=getattr(request.state, "branding", None),
        user=user,
    ))


@router.get("/ediscovery/collections/{collection_id}/status-partial", response_class=HTMLResponse)
async def collection_status_partial(
    request: Request,
    collection_id: str,
    user=Depends(get_current_user),
):
    """HTMX partial — stats refreshed every 5s while ingesting."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    templates = _templates

    collection = await _get_collection(tenant_id, collection_id)
    if not collection:
        return HTMLResponse("")

    doc_types = await _get_doc_types(tenant_id, collection_id)

    return templates.TemplateResponse(
        request,
        "partials/collection_stats_partial.html",
        {"collection": collection, "doc_types": doc_types}
    )


@router.get("/ediscovery/collections/{collection_id}/log-partial", response_class=HTMLResponse)
async def collection_log_partial(
    request: Request,
    collection_id: str,
    user=Depends(get_current_user),
):
    """HTMX partial — audit log refreshed every 8s."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    templates = _templates

    audit_log = await _get_audit_log(tenant_id, collection_id)

    return templates.TemplateResponse(
        request,
        "ediscovery/partials/collection_log_partial.html",
        {"audit_log": audit_log}
    )


@router.post("/api/v1/ediscovery/review/collection-status/{collection_id}/retry")
async def retry_collection(request: Request, collection_id: str, user=Depends(get_current_user)):
    """Re-enqueue a failed/stalled collection for ingestion."""
    import os
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    user_id = None
    try:
        user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    except Exception:
        pass

    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT id, collection_name, status FROM ediscovery_collections WHERE id = CAST(:cid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"cid": collection_id, "tid": tid})).fetchone()
        if not row:
            return JSONResponse({"error": "Collection not found"}, status_code=404)

        # Reset status
        await session.execute(text(
            "UPDATE ediscovery_collections SET status = 'collecting' WHERE id = CAST(:cid AS uuid)"
        ), {"cid": collection_id})
        await session.commit()

    # Re-enqueue
    try:
        from redis import Redis
        from rq import Queue
        REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
        redis_conn = Redis.from_url(REDIS_URL)
        q = Queue("ediscovery", connection=redis_conn)
        job = q.enqueue(
            "modules.ediscovery.jobs.ledger_dag.run_collection_full",
            tid, collection_id, user_id,
            spine_workers=6,
            ocr_workers=2,
            embed_workers=1,
            job_timeout="24h",
            result_ttl=3600,
        )
        return JSONResponse({"ok": True, "job_id": job.id, "status": "collecting"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
