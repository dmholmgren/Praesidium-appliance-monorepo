from sqlalchemy import text as sa_text
"""
COMP 5 — Document Time Tracking
Tracks viewing and editing sessions for documents.
On session end, if duration > 60 seconds, enqueues draft_time_entry job.
Feeds directly into billing time capture.
"""

import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/documents", tags=["dms-time-tracking"])


class SessionStartRequest(BaseModel):
    activity_type: str = "viewing"  # viewing | editing


class SessionEndRequest(BaseModel):
    keystrokes: int = 0


class KeystrokeRequest(BaseModel):
    count: int = 1


@router.post("/{document_id}/session/start")
async def start_session(
    document_id: str,
    body: SessionStartRequest,
    request: Request,
):
    """Start a document viewing/editing session."""
    from core.db.base import TenantSession, get_session_factory
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    # Close any existing open session for this user+document
    session.execute(
        sa_text("""UPDATE document_time_tracking
        SET session_end = :now, session_end IS NOT NULL
        WHERE tenant_id = :tid AND document_id = :did
        AND user_id = :uid AND session_end IS NULL"""),
        {
            "now": datetime.now(timezone.utc).isoformat(),
            "tid": tenant_id,
            "did": document_id,
            "uid": user_id,
        },
    )

    session_id = str(uuid.uuid4())
    session.execute(
        sa_text("""INSERT INTO document_time_tracking
        (id, tenant_id, document_id, user_id, session_start,
         activity_type,  keystrokes)
        VALUES (:id, :tid, :did, :uid, :started, :atype, 1, 0)"""),
        {
            "id": session_id,
            "tid": tenant_id,
            "did": document_id,
            "uid": user_id,
            "started": datetime.now(timezone.utc).isoformat(),
            "atype": body.activity_type,
        },
    )

    write_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        action="session_start",
        module="dms",
        table_name="document_time_tracking",
        record_id=session_id,
        new_value={"document_id": document_id, "activity_type": body.activity_type},
        source="dms_time_tracking",
    )
    session.commit()

    return {"session_id": session_id, "session_start": datetime.now(timezone.utc).isoformat()}


@router.post("/{document_id}/session/end")
async def end_session(
    document_id: str,
    body: SessionEndRequest,
    request: Request,
):
    """End a document session. Enqueues time entry if > 60 seconds."""
    from core.audit import write_audit

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    # Find active session
    active = session.execute(
        sa_text("""SELECT id, session_start, activity_type, keystrokes
        FROM document_time_tracking
        WHERE tenant_id = :tid AND document_id = :did
        AND user_id = :uid AND session_end IS NULL
        ORDER BY session_start DESC LIMIT 1"""),
        {"tid": tenant_id, "did": document_id, "uid": user_id},
    ).fetchone()

    if not active:
        raise HTTPException(status_code=404, detail="No active session found")

    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(active["session_start"].replace("Z", "+00:00"))
    duration_seconds = int((now - started).total_seconds())
    total_keystrokes = (active["keystrokes"] or 0) + body.keystrokes

    session.execute(
        sa_text("""UPDATE document_time_tracking
        SET session_end = :now, session_end IS NOT NULL,
            duration_seconds = :dur, keystrokes = :ks
        WHERE id = :id AND tenant_id = :tid"""),
        {
            "now": now.isoformat(),
            "dur": duration_seconds,
            "ks": total_keystrokes,
            "id": active["id"],
            "tid": tenant_id,
        },
    )

    write_audit(
        tenant_id=tenant_id,
        user_id=user_id,
        action="session_end",
        module="dms",
        table_name="document_time_tracking",
        record_id=active["id"],
        new_value={
            "duration_seconds": duration_seconds,
            "keystrokes": total_keystrokes,
        },
        source="dms_time_tracking",
    )
    session.commit()

    # If duration > 60 seconds, enqueue draft time entry
    if duration_seconds > 60:
        from modules.dms.jobs.file_crawler import get_rq_queue
        q = get_rq_queue("billing")
        q.enqueue(
            _draft_time_entry,
            tenant_id, user_id, document_id,
            active["id"], duration_seconds,
            active["activity_type"], total_keystrokes,
        )

    return {
        "session_id": active["id"],
        "duration_seconds": duration_seconds,
        "keystrokes": total_keystrokes,
        "time_entry_queued": duration_seconds > 60,
    }


@router.post("/{document_id}/keystroke")
async def record_keystroke(
    document_id: str,
    body: KeystrokeRequest,
    request: Request,
):
    """Increment keystroke count for active session."""

    tenant_id = request.state.tenant_id
    user_id = getattr(request.state.current_user, "id", request.state.current_user) if request.state.current_user else "anonymous"
    session = TenantSession(get_session_factory()(), tenant_id)

    result = session.execute(
        sa_text("""UPDATE document_time_tracking
        SET keystrokes = keystrokes + :count,
            activity_type = 'editing'
        WHERE tenant_id = :tid AND document_id = :did
        AND user_id = :uid AND session_end IS NULL"""),
        {
            "count": body.count,
            "tid": tenant_id,
            "did": document_id,
            "uid": user_id,
        },
    )
    session.commit()

    return {"recorded": True}


def _draft_time_entry(
    tenant_id: str,
    user_id: str,
    document_id: str,
    tracking_session_id: str,
    duration_seconds: int,
    activity_type: str,
    keystrokes: int,
):
    """
    RQ job: Create a draft time entry from a document session.
    Status=pending, source=dms, ai_suggested=true.
    The billing module's review queue picks these up.
    """
    from core.audit import write_audit

    session = TenantSession(get_session_factory()(), tenant_id)

    # Get document and matter info
    doc = session.execute(
        sa_text("""SELECT d.title, d.matter_id, m.matter_name as matter_name
        FROM documents d
        LEFT JOIN matters m ON d.matter_id = m.id AND d.tenant_id = m.tenant_id
        WHERE d.id = :did AND d.tenant_id = :tid"""),
        {"did": document_id, "tid": tenant_id},
    ).fetchone()

    if not doc or not doc["matter_id"]:
        logger.warning(f"No matter for doc {document_id}, skipping time entry")
        return

    # Round to nearest 0.1 hour (6 min increments)
    hours = round(duration_seconds / 3600, 1)
    if hours < 0.1:
        hours = 0.1

    activity_desc = "Reviewing" if activity_type == "viewing" else "Drafting/editing"
    description = f"{activity_desc} {doc['title']}"

    entry_id = str(uuid.uuid4())
    session.execute(
        sa_text("""INSERT INTO time_entries
        (id, tenant_id, user_id, matter_id, entry_date, hours,
         narrative, status, source, ai_suggested,
         document_id, tracking_session_id, created_at)
        VALUES (:id, :tid, :uid, :mid, :entry_date, :hours,
                :description, 'ai_suggested', 'document', 1,
                :did, :tsid, :created)"""),
        {
            "id": entry_id,
            "tid": tenant_id,
            "uid": user_id,
            "mid": doc["matter_id"],
            "entry_date": datetime.now(timezone.utc).date().isoformat(),
            "hours": hours,
            "description": narrative,
            "did": document_id,
            "tsid": tracking_session_id,
            "created": datetime.now(timezone.utc).isoformat(),
        },
    )

    write_audit(
        tenant_id=tenant_id,
        user_id="system",
        action="create",
        module="billing",
        table_name="time_entries",
        record_id=entry_id,
        new_value={
            "hours": hours,
            "description": narrative,
            "source": "dms",
            "ai_suggested": True,
        },
        source="dms_time_tracking",
    )
    session.commit()
    logger.info(f"Draft time entry {entry_id}: {hours}h for {doc['title']}")
