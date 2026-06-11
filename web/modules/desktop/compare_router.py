"""
M-DESK C1 — Compare router.

Three endpoints, all behind require_desktop_user:

  POST /api/v1/desktop/compare
    Body: { "doc_a_id": "<uuid>", "doc_b_id": "<uuid>" }
    Validates both documents exist in the caller's tenant, then enqueues
    an RQ job. Returns 202 Accepted with { job_id, result_url }.

  GET /api/v1/desktop/compare/{job_id}
    Returns the job's current status and (when complete) the diff stats
    + a download URL. Verifies the requesting user matches the user that
    enqueued the job.
        Status values: queued | started | completed | failed | unknown

  GET /api/v1/desktop/compare/{job_id}/download
    Streams the redlined .docx bytes. Same auth: requesting user must
    match the enqueuer.

The compare worker (modules.desktop.compare_worker.run_compare_job)
runs the actual LibreOffice + python-docx + docx-revisions pipeline.
This router only enqueues + serves status/result.

Queue: 'default' (matches existing codebase pattern from
admin/timesheet_api.py — same queue, same Redis connection).

Result TTL: 24 hours (86400s). Default RQ TTL is 500s which isn't
enough for a "user enqueues, gets distracted, comes back later"
workflow. The worker also writes the output to a stable cache path
(/mnt/praesidium/{tenant_id}/.cache/compares/{job_id}.docx) so the
file is available even if the RQ job record itself has expired.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path as PathParam,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.desktop import jwt_service
from modules.desktop.checkout_router import require_desktop_user

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-compare"])


# ═════════════════════════════════════════════════════════════════════════
# Constants
# ═════════════════════════════════════════════════════════════════════════

COMPARE_QUEUE_NAME = "default"
COMPARE_JOB_TIMEOUT_SECONDS = 600        # 10 min — generous for 50pp pair
COMPARE_RESULT_TTL_SECONDS = 86400       # 24 hours
COMPARE_FAILURE_TTL_SECONDS = 604800     # 7 days for failed jobs (debugging)
COMPARE_FUNC_REF = "modules.desktop.compare_worker.run_compare_job"


# ═════════════════════════════════════════════════════════════════════════
# Request / response models
# ═════════════════════════════════════════════════════════════════════════

class CompareRequest(BaseModel):
    doc_a_id: str = Field(..., min_length=1, description="UUID of the OLD document")
    doc_b_id: str = Field(..., min_length=1, description="UUID of the NEW document")


class CompareEnqueueResponse(BaseModel):
    job_id: str
    status: str = "queued"
    result_url: str
    download_url: str


# ═════════════════════════════════════════════════════════════════════════
# RQ helpers
# ═════════════════════════════════════════════════════════════════════════

def _redis_connection():
    """Single-source Redis connection for compare queue + job lookup."""
    from redis import Redis
    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    return Redis.from_url(redis_url)


def _get_compare_queue():
    from rq import Queue
    return Queue(COMPARE_QUEUE_NAME, connection=_redis_connection())


def _fetch_job(job_id: str):
    """Look up an RQ Job by id. Returns None if not found / expired."""
    from rq.job import Job
    from rq.exceptions import NoSuchJobError
    try:
        return Job.fetch(job_id, connection=_redis_connection())
    except NoSuchJobError:
        return None
    except Exception as exc:
        logger.warning("[m-desk-compare] job fetch failed for %s: %s", job_id, exc)
        return None


# ═════════════════════════════════════════════════════════════════════════
# DB helper — verify both docs exist + scoped to tenant
# ═════════════════════════════════════════════════════════════════════════

async def _verify_documents_exist(
    tenant_id: str, doc_ids: list[str]
) -> dict[str, bool]:
    """Return {doc_id: True/False} for each id checking tenant scoping."""
    if not doc_ids:
        return {}
    tid = (tenant_id or "").strip()
    out: dict[str, bool] = {d: False for d in doc_ids}
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT id::text AS id
                FROM documents
                WHERE TRIM(tenant_id) = :tid
                  AND id::text = ANY(CAST(:ids AS text[]))
            """),
            {"tid": tid, "ids": doc_ids},
        )
        for row in result.mappings().all():
            out[row["id"]] = True
    return out


# ═════════════════════════════════════════════════════════════════════════
# POST /compare
# ═════════════════════════════════════════════════════════════════════════

@router.post("/compare")
async def enqueue_compare(
    body: CompareRequest,
    request: Request,
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Validate then enqueue a compare job. Returns 202 + job_id."""
    if body.doc_a_id == body.doc_b_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "same_document",
                    "reason": "doc_a_id and doc_b_id must differ"},
        )

    presence = await _verify_documents_exist(
        claims.tenant_id, [body.doc_a_id, body.doc_b_id]
    )
    if not presence.get(body.doc_a_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "missing": "doc_a_id",
                    "doc_id": body.doc_a_id},
        )
    if not presence.get(body.doc_b_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "missing": "doc_b_id",
                    "doc_id": body.doc_b_id},
        )

    # Enqueue the job. We pass kwargs so the function signature in
    # compare_worker.run_compare_job (which is keyword-only) is satisfied
    # exactly. RQ supports both positional and kwargs.
    try:
        queue = _get_compare_queue()
        job = queue.enqueue(
            COMPARE_FUNC_REF,
            kwargs={
                "job_id":              "<assigned-by-rq>",  # overwritten below
                "tenant_id":           claims.tenant_id,
                "doc_a_id":            body.doc_a_id,
                "doc_b_id":            body.doc_b_id,
                "requesting_user_id":  claims.user_id,
            },
            job_timeout=COMPARE_JOB_TIMEOUT_SECONDS,
            result_ttl=COMPARE_RESULT_TTL_SECONDS,
            failure_ttl=COMPARE_FAILURE_TTL_SECONDS,
        )
    except Exception as exc:
        logger.exception("[m-desk-compare] enqueue failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "enqueue_failed", "reason": str(exc)},
        )

    # The worker uses `job_id` to name its output file. RQ assigns the id
    # at enqueue time, but our kwargs were already set with the placeholder
    # above. Patch the kwargs in-place so the worker function receives the
    # actual job id when it runs.
    try:
        job.kwargs["job_id"] = job.id
        job.save_meta()  # persist any meta changes; kwargs are already saved
        # RQ stores kwargs in the job hash. Update them by re-saving.
        from rq.serializers import resolve_serializer
        # Easier: just save the entire job since we mutated it.
        job.save()
    except Exception as exc:
        logger.warning(
            "[m-desk-compare] could not patch job kwargs with job_id; "
            "worker will use placeholder. job=%s err=%s",
            job.id, exc,
        )

    base_url = str(request.base_url).rstrip("/")
    result_url = f"{base_url}/api/v1/desktop/compare/{job.id}"
    download_url = f"{result_url}/download"

    logger.info(
        "[m-desk-compare] enqueued job=%s tenant=%s user=%s a=%s b=%s",
        job.id, claims.tenant_id, claims.user_id,
        body.doc_a_id, body.doc_b_id,
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "job_id":       job.id,
            "status":       "queued",
            "result_url":   result_url,
            "download_url": download_url,
        },
    )


# ═════════════════════════════════════════════════════════════════════════
# GET /compare/{job_id} — status / result poll
# ═════════════════════════════════════════════════════════════════════════

def _check_job_owner(job, claims: jwt_service.AccessClaims) -> Optional[Response]:
    """Return a 403 Response if the JWT user doesn't own this job, else None.

    Inspects job.kwargs (which include tenant_id + requesting_user_id) and
    confirms both match the access claims.
    """
    job_kwargs = job.kwargs or {}
    job_tenant = (job_kwargs.get("tenant_id") or "").strip()
    job_user = job_kwargs.get("requesting_user_id")

    if job_tenant != claims.tenant_id:
        logger.warning(
            "[m-desk-compare] tenant mismatch on job %s: job=%s claims=%s",
            job.id, job_tenant, claims.tenant_id,
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "not_owner", "reason": "tenant mismatch"},
        )
    if int(job_user or 0) != int(claims.user_id):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"error": "not_owner",
                     "reason": "requesting user does not match enqueuer"},
        )
    return None


@router.get("/compare/{job_id}")
async def get_compare_status(
    request: Request,
    job_id: str = PathParam(..., description="RQ job id from enqueue"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Poll status. Returns lightweight status while running, full result when done."""
    job = _fetch_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_job", "job_id": job_id,
                    "reason": "Job not found or result expired"},
        )

    deny = _check_job_owner(job, claims)
    if deny is not None:
        return deny

    base_url = str(request.base_url).rstrip("/")
    download_url = f"{base_url}/api/v1/desktop/compare/{job_id}/download"

    rq_status = job.get_status()
    if rq_status in ("queued", "deferred", "scheduled"):
        return {
            "job_id":  job_id,
            "status":  "queued",
            "enqueued_at": job.enqueued_at.isoformat() if job.enqueued_at else None,
        }
    if rq_status == "started":
        return {
            "job_id":  job_id,
            "status":  "started",
            "started_at": job.started_at.isoformat() if job.started_at else None,
        }
    if rq_status == "failed":
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "job_id":         job_id,
                "status":         "failed",
                "error":          (str(job.exc_info)[-500:]
                                   if job.exc_info else "unknown error"),
                "failed_at":      (job.ended_at.isoformat()
                                   if job.ended_at else None),
            },
        )
    if rq_status == "finished":
        result = job.result or {}
        # The worker dict already carries status/diff_stats/etc. Add the
        # download URL the client should use to fetch the .docx.
        result.setdefault("status", "completed")
        result["download_url"] = download_url
        return result

    # Unknown / stopped / canceled
    return {
        "job_id": job_id,
        "status": rq_status or "unknown",
    }


# ═════════════════════════════════════════════════════════════════════════
# GET /compare/{job_id}/download — stream the redlined .docx
# ═════════════════════════════════════════════════════════════════════════

@router.get("/compare/{job_id}/download")
async def download_compare_result(
    request: Request,
    job_id: str = PathParam(..., description="RQ job id"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Stream the redlined .docx. Available once the job is finished."""
    job = _fetch_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "unknown_job", "job_id": job_id},
        )

    deny = _check_job_owner(job, claims)
    if deny is not None:
        return deny

    rq_status = job.get_status()
    if rq_status != "finished":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "not_ready", "job_status": rq_status,
                    "reason": "compare job has not completed yet"},
        )

    result = job.result or {}
    if result.get("status") != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "job_failed",
                    "reason": result.get("error") or "compare worker returned failure"},
        )

    output_path = result.get("output_path")
    if not output_path:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "no_output_path"},
        )

    p = Path(output_path)
    if not p.exists():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail={"error": "output_missing",
                    "reason": ("Cached output has been pruned. "
                               "Re-enqueue the compare to regenerate."),
                    "expected_path": output_path},
        )

    file_bytes = p.read_bytes()
    headers = {
        "Content-Disposition":   f'attachment; filename="redline-{job_id}.docx"',
        "X-Praesidium-Job-Id":   job_id,
        "X-Praesidium-Checksum": result.get("checksum") or "",
    }
    return Response(
        content=file_bytes,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        headers=headers,
    )
