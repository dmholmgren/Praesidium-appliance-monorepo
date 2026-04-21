"""
dashboard/services/matter_widget_service.py
=============================================
Widget service functions for matter-scoped dashboard widgets.

Scope contract: every function receives scope dict and returns
a plain dict of template context variables.

Registered widget slugs:
    matter_summary              data_panel  matter
    matter_tasks                data_panel  matter
    matter_communications       data_panel  matter
    matter_documents_launcher   data_panel  matter
    matter_deadlines            data_panel  matter, attorney, firm
    matter_ai_chat              data_panel  matter, attorney, firm
"""

import logging
from datetime import datetime, date, timedelta, timezone

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _fmt_dt(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%b %d")
    return str(val or "")[:10]


def _fmt_dt_full(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%b %d, %Y %I:%M %p")
    return str(val or "")[:16]


# ---------------------------------------------------------------------------
# matter_summary
# AI-generated state-of-case from matter_summaries table.
# Regeneration is triggered via POST /dashboard/matter/{id}/summary/regenerate
# ---------------------------------------------------------------------------

async def get_matter_summary(scope: dict) -> dict:
    """
    data_source for widget: matter_summary
    Reads cached summary from matter_summaries table.
    Returns status: pending|ready|error and the summary text.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {"status": "error", "summary_text": None,
                "generated_at": None, "urgent_items": [], "matter_id": matter_id}

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT overview_paragraph, critical_issues_paragraph,
                       generated_at, status, error_message
                FROM matter_summaries
                WHERE matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                ORDER BY generated_at DESC NULLS LAST
                LIMIT 1
            """), {"mid": matter_id, "tid": tenant_id})
            row = r.mappings().fetchone()

        if not row:
            return {"status": "none", "summary_text": None,
                    "generated_at": None, "urgent_items": [], "matter_id": matter_id}

        status = row.get("status") or "pending"

        # Combine paragraphs into readable summary
        parts = []
        if row.get("overview_paragraph"):
            parts.append(row["overview_paragraph"])
        if row.get("critical_issues_paragraph"):
            parts.append(row["critical_issues_paragraph"])
        summary_text = "\n\n".join(parts) if parts else None

        generated_at = row.get("generated_at")
        generated_at_str = _fmt_dt_full(generated_at) if generated_at else None

        return {
            "status":       status,
            "summary_text": summary_text,
            "generated_at": generated_at_str,
            "urgent_items": [],  # Future: extract from summary parsing
            "matter_id":    matter_id,
        }

    except Exception as exc:
        logger.error("get_matter_summary error: %s", exc)
        return {"status": "error", "summary_text": None,
                "generated_at": None, "urgent_items": [],
                "matter_id": matter_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# matter_tasks
# Open tasks for this matter from the tasks table.
# ---------------------------------------------------------------------------

async def get_matter_tasks(scope: dict) -> dict:
    """
    data_source for widget: matter_tasks
    Returns open tasks for the matter, sorted by priority then due date.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {"tasks": [], "total": 0, "matter_id": matter_id}

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT
                    id, title, priority, status,
                    due_date, source, source_ref,
                    created_by
                FROM tasks
                WHERE matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                  AND status != 'complete'
                ORDER BY
                    CASE priority
                        WHEN 'critical' THEN 1
                        WHEN 'high'     THEN 2
                        WHEN 'medium'   THEN 3
                        ELSE 4
                    END,
                    due_date ASC NULLS LAST
                LIMIT 20
            """), {"mid": matter_id, "tid": tenant_id})
            rows = r.mappings().fetchall()

        tasks = []
        for row in rows:
            due = row.get("due_date")
            due_str = None
            if due:
                due_dt = due if isinstance(due, (datetime, date)) else None
                if due_dt:
                    today = date.today()
                    due_date = due_dt.date() if isinstance(due_dt, datetime) else due_dt
                    if due_date < today:
                        due_str = f"⚠ {_fmt_dt(due_dt)}"
                    elif due_date <= today + timedelta(days=3):
                        due_str = f"🔴 {_fmt_dt(due_dt)}"
                    else:
                        due_str = _fmt_dt(due_dt)

            tasks.append({
                "id":           row["id"],
                "title":        row["title"] or "Untitled",
                "priority":     row.get("priority") or "medium",
                "status":       row.get("status") or "open",
                "due_date_str": due_str,
                "source":       row.get("source") or "manual",
            })

        return {"tasks": tasks, "total": len(tasks), "matter_id": matter_id}

    except Exception as exc:
        logger.error("get_matter_tasks error: %s", exc)
        return {"tasks": [], "total": 0, "matter_id": matter_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# matter_communications
# Exchange emails (matched_matter_id) + correspondence folder docs.
# ---------------------------------------------------------------------------

async def get_matter_communications(scope: dict) -> dict:
    """
    data_source for widget: matter_communications
    Merges:
      1. email_routing_queue rows where matched_matter_id = matter_id
      2. dms_documents in Correspondence/correspondence folders for this matter
    Sorted by date desc, limited to 15.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {"items": [], "total": 0, "matter_id": matter_id}

    try:
        async with AsyncSessionLocal() as session:

            # Emails matched to this matter
            email_r = await session.execute(sa_text("""
                SELECT
                    'email'         AS type,
                    subject,
                    from_display,
                    body_preview    AS preview,
                    received_at,
                    has_attachments,
                    'email'         AS source
                FROM email_routing_queue
                WHERE matched_matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
                ORDER BY received_at DESC
                LIMIT 10
            """), {"mid": matter_id, "tid": tenant_id})
            email_rows = email_r.mappings().fetchall()

            # Correspondence docs from DMS folder matches
            # Match folders with 'correspondence' or 'Correspondence' in path
            doc_r = await session.execute(sa_text("""
                SELECT
                    'document'      AS type,
                    dd.file_path    AS subject,
                    NULL            AS from_display,
                    NULL            AS preview,
                    dd.indexed_at   AS received_at,
                    FALSE           AS has_attachments,
                    'dms'           AS source
                FROM dms_documents dd
                JOIN dms_folder_matches mf
                    ON trim(mf.tenant_id::text) = trim(:tid)
                    AND dd.file_path LIKE mf.folder_path || '%'
                WHERE mf.matter_id = CAST(:mid AS uuid)
                  AND trim(dd.tenant_id::text) = trim(:tid)
                  AND (
                    LOWER(mf.folder_path) LIKE '%correspondence%'
                    OR LOWER(mf.folder_path) LIKE '%pleading%'
                    OR LOWER(mf.folder_path) LIKE '%discovery%'
                  )
                ORDER BY dd.indexed_at DESC
                LIMIT 10
            """), {"mid": matter_id, "tid": tenant_id})
            doc_rows = doc_r.mappings().fetchall()

        # Merge and sort by date
        items = []
        for row in list(email_rows) + list(doc_rows):
            received = row.get("received_at")
            subject = row.get("subject") or ""
            if row.get("type") == "document":
                # Extract filename from path
                subject = subject.replace("\\", "/").split("/")[-1]

            items.append({
                "type":            row.get("type") or "email",
                "subject":         subject,
                "from_display":    row.get("from_display") or "",
                "preview":         (row.get("preview") or "")[:120],
                "received_str":    _fmt_dt(received) if received else "",
                "has_attachments": bool(row.get("has_attachments")),
                "source":          row.get("source") or "email",
                "sort_dt":         received,
            })

        # Sort merged list by date desc
        items.sort(
            key=lambda x: x.get("sort_dt") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True
        )
        for item in items:
            item.pop("sort_dt", None)

        return {"items": items[:15], "total": len(items), "matter_id": matter_id}

    except Exception as exc:
        logger.error("get_matter_communications error: %s", exc)
        return {"items": [], "total": 0, "matter_id": matter_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# matter_deadlines
# Deadlines scoped to a matter (or attorney or firm-wide).
# Extends atty_deadlines with matter scope.
# ---------------------------------------------------------------------------

async def get_matter_deadlines(scope: dict) -> dict:
    """
    data_source for widget: matter_deadlines
    Branches: matter_id → attorney_id → firm-wide.
    14-day lookahead, sorted by deadline_date ASC.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")
    attorney_id = scope.get("attorney_id")

    try:
        async with AsyncSessionLocal() as session:
            if matter_id:
                r = await session.execute(sa_text("""
                    SELECT d.id, d.title, d.deadline_date,
                           d.deadline_type, d.is_sol, d.completed_at,
                           m.matter_name
                    FROM deadlines d
                    LEFT JOIN matters m
                        ON d.matter_id = m.id
                        AND trim(m.tenant_id::text) = trim(:tid)
                    WHERE d.matter_id = CAST(:mid AS uuid)
                      AND trim(d.tenant_id::text) = trim(:tid)
                      AND d.completed_at IS NULL
                      AND d.deadline_date >= NOW()
                      AND d.deadline_date <= NOW() + INTERVAL '30 days'
                    ORDER BY d.deadline_date ASC
                    LIMIT 15
                """), {"mid": matter_id, "tid": tenant_id})

            elif attorney_id:
                r = await session.execute(sa_text("""
                    SELECT d.id, d.title, d.deadline_date,
                           d.deadline_type, d.is_sol, d.completed_at,
                           m.matter_name
                    FROM deadlines d
                    JOIN matters m
                        ON d.matter_id = m.id
                        AND trim(m.tenant_id::text) = trim(:tid)
                    WHERE trim(d.tenant_id::text) = trim(:tid)
                      AND m.originating_attorney_id = :atty_id
                      AND d.completed_at IS NULL
                      AND d.deadline_date >= NOW()
                      AND d.deadline_date <= NOW() + INTERVAL '14 days'
                    ORDER BY d.deadline_date ASC
                    LIMIT 15
                """), {"tid": tenant_id, "atty_id": int(attorney_id)})

            else:
                r = await session.execute(sa_text("""
                    SELECT d.id, d.title, d.deadline_date,
                           d.deadline_type, d.is_sol, d.completed_at,
                           m.matter_name
                    FROM deadlines d
                    LEFT JOIN matters m
                        ON d.matter_id = m.id
                        AND trim(m.tenant_id::text) = trim(:tid)
                    WHERE trim(d.tenant_id::text) = trim(:tid)
                      AND d.completed_at IS NULL
                      AND d.deadline_date >= NOW()
                      AND d.deadline_date <= NOW() + INTERVAL '14 days'
                    ORDER BY d.deadline_date ASC
                    LIMIT 20
                """), {"tid": tenant_id})

            rows = r.mappings().fetchall()

        today = date.today()
        deadlines = []
        for row in rows:
            dl_dt = row.get("deadline_date")
            dl_date = dl_dt.date() if isinstance(dl_dt, datetime) else dl_dt
            days_out = (dl_date - today).days if dl_date else 999

            if days_out <= 2:
                urgency = "critical"
            elif days_out <= 7:
                urgency = "high"
            else:
                urgency = "normal"

            deadlines.append({
                "id":            row["id"],
                "title":         row["title"] or "Deadline",
                "deadline_str":  _fmt_dt(dl_dt) if dl_dt else "",
                "days_out":      days_out,
                "urgency":       urgency,
                "deadline_type": row.get("deadline_type") or "",
                "is_sol":        bool(row.get("is_sol")),
                "matter_name":   row.get("matter_name") or "",
            })

        return {
            "deadlines": deadlines,
            "total":     len(deadlines),
            "matter_id": matter_id,
        }

    except Exception as exc:
        logger.error("get_matter_deadlines error: %s", exc)
        return {"deadlines": [], "total": 0,
                "matter_id": matter_id, "error": str(exc)}


# ---------------------------------------------------------------------------
# matter_documents_launcher
# Stats for the documents tab launcher card.
# ---------------------------------------------------------------------------

async def get_matter_documents_launcher(scope: dict) -> dict:
    """
    data_source for widget: matter_documents_launcher
    Returns doc count and folder count for the launcher card.
    """
    tenant_id = (scope.get("tenant_id") or "").strip()
    matter_id = scope.get("matter_id")

    if not matter_id:
        return {"doc_count": 0, "folder_count": 0,
                "matter_name": None, "matter_id": matter_id}

    try:
        async with AsyncSessionLocal() as session:
            nc = await session.execute(sa_text("""
                SELECT COUNT(*) FROM documents
                WHERE matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            native_count = nc.scalar() or 0

            fc = await session.execute(sa_text("""
                SELECT COUNT(*) FROM matter_folders
                WHERE matter_id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            folder_count = fc.scalar() or 0

            # Also count legacy docs
            lc = await session.execute(sa_text("""
                SELECT COUNT(dd.id)
                FROM dms_documents dd
                JOIN dms_folder_matches mf
                    ON trim(mf.tenant_id::text) = trim(:tid)
                    AND dd.file_path LIKE mf.folder_path || '%'
                WHERE mf.matter_id = CAST(:mid AS uuid)
                  AND trim(dd.tenant_id::text) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            legacy_count = lc.scalar() or 0

            mn = await session.execute(sa_text("""
                SELECT matter_name FROM matters
                WHERE id = CAST(:mid AS uuid)
                  AND trim(tenant_id::text) = trim(:tid)
            """), {"mid": matter_id, "tid": tenant_id})
            mn_row = mn.fetchone()
            matter_name = mn_row[0] if mn_row else None

        return {
            "doc_count":   native_count + legacy_count,
            "folder_count": folder_count,
            "matter_name": matter_name,
            "matter_id":   matter_id,
        }

    except Exception as exc:
        logger.error("get_matter_documents_launcher error: %s", exc)
        return {"doc_count": 0, "folder_count": 0,
                "matter_name": None, "matter_id": matter_id, "error": str(exc)}
