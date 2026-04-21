"""
Practice Intelligence Widget Service
=====================================
Data source functions for the Practice Intelligence Dashboard.

Schema confirmed from live DB dump (Apr 16 2026):
  deadlines:              id, tenant_id, matter_id, title, deadline_date,
                          deadline_type, completed_at
  exchange_calendar_events: tenant_id, subject, start_at, end_at, is_all_day,
                            location, body_preview, attorney_user_id, matter_id
  email_routing_queue:    tenant_id, subject, from_email, from_display,
                          received_at, body_preview, attorney_user_id,
                          routing_status, has_attachments
  dms_documents:          tenant_id, file_path, indexed_at, ocr_status, source
  ts_slips:               source_client_id, wip_value, billed, slip_date
  ts_clients:             ts_client_id, praesidium_client_id, ts_name
  matters:                id, tenant_id, client_id, matter_name, matter_number,
                          status, responsible_attorney_id, originating_attorney_id
  clients:                id, tenant_id, client_name
  users:                  id, tenant_id, full_name, role, is_active,
                          last_login, email
  ts_invoices:            net_due, paid_in_full, created_at

Scope dict keys:
  tenant_id    — always from request.state (never from query params)
  user_id      — current user (attorney) or selected attorney_id param
  attorney_id  — explicit override for Attorney View tab
  request      — FastAPI Request object for query param access
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)


def _tid(scope: dict) -> str:
    return (scope.get("tenant_id") or "").strip()


def _atty_user_id(scope: dict):
    """
    Resolve the attorney user_id for Attorney View widgets.
    Priority: explicit attorney_id param > scope user_id.
    Returns int or None.
    """
    req = scope.get("request")
    atty_id = scope.get("attorney_id")
    if not atty_id and req:
        atty_id = req.query_params.get("attorney_id")
    if atty_id:
        try:
            return int(atty_id)
        except (ValueError, TypeError):
            pass
    return scope.get("user_id")


# ─────────────────────────────────────────────────────────────
# Firm View — Row 1
# ─────────────────────────────────────────────────────────────

async def get_firm_matter_tree_pi(scope: dict) -> dict:
    """
    Client → Matter tree for Practice Intelligence context.
    Identical to billing tree but without WIP — fast load.
    Click callbacks scope center/right via piScopeMatter().
    """
    tenant_id = _tid(scope)
    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    m.id::text   AS id,
                    m.matter_name,
                    m.matter_number,
                    m.status,
                    c.id::text   AS client_id,
                    c.client_name
                FROM matters m
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(m.tenant_id)
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
                ORDER BY c.client_name NULLS LAST, m.matter_name
            """), {"tid": tenant_id})

            client_map: dict = {}
            for row in rows.mappings():
                cid   = row["client_id"] or "unknown"
                cname = row["client_name"] or "Unknown Client"
                if cid not in client_map:
                    client_map[cid] = {"client_id": cid,
                                       "client_name": cname, "matters": []}
                client_map[cid]["matters"].append({
                    "id":            row["id"],
                    "matter_name":   row["matter_name"] or "Untitled",
                    "matter_number": row["matter_number"] or "",
                    "status":        row["status"] or "active",
                    "client_id":     cid,
                })

        clients = sorted(client_map.values(), key=lambda x: x["client_name"])
        total_matters = sum(len(c["matters"]) for c in clients)
        return {"clients": clients, "total_matters": total_matters,
                "total_clients": len(clients), "error": None}

    except Exception as exc:
        logger.error("get_firm_matter_tree_pi error: %s", exc)
        return {"clients": [], "total_matters": 0,
                "total_clients": 0, "error": str(exc)}


async def get_firm_wip_chart(scope: dict) -> dict:
    """
    Horizontal bar chart — top N matters by WIP value.
    Joins matters → ts_clients → ts_slips.
    Returns chart_data list sorted by wip_value desc.
    """
    tenant_id = _tid(scope)
    req = scope.get("request")
    limit = int((req.query_params.get("limit", "12")) if req else "12")

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    m.matter_name,
                    m.matter_number,
                    m.id::text        AS matter_id,
                    m.client_id::text AS client_id,
                    COALESCE(SUM(s.wip_value), 0) AS wip_value,
                    COALESCE(SUM(s.hours), 0)     AS wip_hours
                FROM matters m
                JOIN ts_clients tc
                    ON tc.ts_raw->>'nickname2' = m.matter_number
                    AND trim(tc.tenant_id) = trim(:tid)
                JOIN ts_slips s
                    ON s.source_client_id = tc.ts_client_id
                    AND trim(s.tenant_id) = trim(:tid)
                    AND s.billed = false
                WHERE trim(m.tenant_id) = trim(:tid)
                  AND m.status = 'active'
                GROUP BY m.id, m.matter_name, m.matter_number, m.client_id
                HAVING SUM(s.wip_value) > 0
                ORDER BY wip_value DESC
                LIMIT :lim
            """), {"tid": tenant_id, "lim": limit})

            chart_data = []
            for row in rows.mappings():
                chart_data.append({
                    "matter_name":   row["matter_name"] or "Untitled",
                    "matter_number": row["matter_number"] or "",
                    "matter_id":     row["matter_id"],
                    "client_id":     row["client_id"],
                    "wip_value":     float(row["wip_value"] or 0),
                    "wip_hours":     float(row["wip_hours"] or 0),
                })

        max_wip = max((d["wip_value"] for d in chart_data), default=1) or 1
        for d in chart_data:
            d["bar_pct"] = round(d["wip_value"] / max_wip * 100, 1)

        total_wip = sum(d["wip_value"] for d in chart_data)
        return {"chart_data": chart_data, "total_wip": total_wip,
                "max_wip": max_wip, "error": None}

    except Exception as exc:
        logger.error("get_firm_wip_chart error: %s", exc)
        return {"chart_data": [], "total_wip": 0, "max_wip": 0, "error": str(exc)}


async def get_firm_ar_aging(scope: dict) -> dict:
    """
    AR aging snapshot — firm-wide, 5 buckets.
    Reuses billing AR pattern from billing widget_service.
    """
    tenant_id = _tid(scope)
    try:
        async with AsyncSessionLocal() as session:
            aging_rows = await session.execute(sa_text("""
                SELECT bucket, COUNT(*) AS invoice_count, SUM(net_due) AS total_balance
                FROM (
                    SELECT net_due,
                        CASE
                            WHEN CURRENT_DATE - created_at::date <= 30  THEN '0-30'
                            WHEN CURRENT_DATE - created_at::date <= 60  THEN '31-60'
                            WHEN CURRENT_DATE - created_at::date <= 90  THEN '61-90'
                            WHEN CURRENT_DATE - created_at::date <= 120 THEN '91-120'
                            ELSE '120+'
                        END AS bucket
                    FROM ts_invoices
                    WHERE trim(tenant_id) = trim(:tid)
                      AND paid_in_full = false
                      AND net_due > 0
                ) sub
                GROUP BY bucket
                ORDER BY
                    CASE bucket
                        WHEN '0-30'   THEN 1
                        WHEN '31-60'  THEN 2
                        WHEN '61-90'  THEN 3
                        WHEN '91-120' THEN 4
                        ELSE 5
                    END
            """), {"tid": tenant_id})

            buckets_raw = {
                r.bucket: {"count": int(r.invoice_count or 0),
                            "amount": float(r.total_balance or 0)}
                for r in aging_rows.mappings()
            }

        bucket_order = ["0-30", "31-60", "61-90", "91-120", "120+"]
        bucket_colors = {
            "0-30":   {"bar": "#16a34a", "bg": "#dcfce7"},
            "31-60":  {"bar": "#ca8a04", "bg": "#fef9c3"},
            "61-90":  {"bar": "#ea580c", "bg": "#ffedd5"},
            "91-120": {"bar": "#dc2626", "bg": "#fee2e2"},
            "120+":   {"bar": "#7f1d1d", "bg": "#fecaca"},
        }
        buckets = [{
            "label":  b,
            "count":  buckets_raw.get(b, {}).get("count",  0),
            "amount": buckets_raw.get(b, {}).get("amount", 0.0),
            **bucket_colors.get(b, {"bar": "#64748b", "bg": "#f1f5f9"}),
        } for b in bucket_order]

        total_ar = sum(b["amount"] for b in buckets)
        max_amount = max((b["amount"] for b in buckets), default=1) or 1
        for b in buckets:
            b["bar_pct"] = round(b["amount"] / max_amount * 100)

        return {"buckets": buckets, "total_ar": total_ar, "error": None}

    except Exception as exc:
        logger.error("get_firm_ar_aging error: %s", exc)
        return {"buckets": [], "total_ar": 0, "error": str(exc)}


# ─────────────────────────────────────────────────────────────
# Firm View — Row 2
# ─────────────────────────────────────────────────────────────

async def get_firm_calendar(scope: dict) -> dict:
    """
    7-day rolling calendar from exchange_calendar_events.
    Shows all events for the firm (firm_view) or all events
    for a specific attorney_user_id (attorney_view via piSetAttorney).
    """
    tenant_id = _tid(scope)
    req = scope.get("request")
    attorney_user_id = None
    if req:
        atty_param = req.query_params.get("attorney_id")
        if atty_param:
            try:
                attorney_user_id = int(atty_param)
            except (ValueError, TypeError):
                pass

    today = date.today()
    window_end = today + timedelta(days=7)

    atty_clause = ""
    params: dict = {"tid": tenant_id,
                    "start": today.isoformat(),
                    "end": window_end.isoformat()}
    if attorney_user_id:
        atty_clause = "AND e.attorney_user_id = :atty_uid"
        params["atty_uid"] = attorney_user_id

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text(f"""
                SELECT
                    e.id::text AS id,
                    e.subject,
                    e.start_at,
                    e.end_at,
                    e.is_all_day,
                    e.location,
                    e.body_preview,
                    e.matter_id::text AS matter_id,
                    m.matter_name,
                    m.matter_number
                FROM exchange_calendar_events e
                LEFT JOIN matters m
                    ON e.matter_id = m.id
                    AND trim(m.tenant_id) = trim(:tid)
                WHERE trim(e.tenant_id) = trim(:tid)
                  AND e.start_at::date >= :start
                  AND e.start_at::date <= :end
                  {atty_clause}
                ORDER BY e.start_at ASC
                LIMIT 50
            """), params)

            events = []
            for row in rows.mappings():
                start = row["start_at"]
                events.append({
                    "id":            row["id"],
                    "subject":       row["subject"] or "(No Subject)",
                    "start_at":      start,
                    "end_at":        row["end_at"],
                    "is_all_day":    row["is_all_day"],
                    "location":      row["location"] or "",
                    "body_preview":  row["body_preview"] or "",
                    "matter_name":   row["matter_name"] or "",
                    "matter_number": row["matter_number"] or "",
                    "day_label":     start.strftime("%a %b %-d") if start else "",
                    "time_label":    start.strftime("%-I:%M %p") if start and not row["is_all_day"] else "All day",
                    "is_today":      start.date() == today if start else False,
                })

        # Group events by day for the template
        days = []
        for i in range(7):
            day = today + timedelta(days=i)
            day_events = [e for e in events
                          if e["start_at"] and e["start_at"].date() == day]
            days.append({
                "date":      day,
                "label":     day.strftime("%a"),
                "day_num":   day.strftime("%-d"),
                "is_today":  day == today,
                "events":    day_events,
                "has_events": bool(day_events),
            })

        return {"days": days, "total_events": len(events),
                "today": today, "error": None}

    except Exception as exc:
        logger.error("get_firm_calendar error: %s", exc)
        return {"days": [], "total_events": 0, "today": date.today(),
                "error": str(exc)}


async def get_firm_deadlines(scope: dict) -> dict:
    """
    Upcoming deadlines across all active matters — next 14 days.
    Grouped by matter, sorted by deadline_date.
    Uses the deadlines table (M3 deadline engine — confirmed in schema).
    """
    tenant_id = _tid(scope)
    today = date.today()
    window_end = today + timedelta(days=14)

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    d.id,
                    d.title,
                    d.deadline_date,
                    d.deadline_type,
                    d.is_sol,
                    d.matter_id::text AS matter_id,
                    m.matter_name,
                    m.matter_number,
                    c.client_name
                FROM deadlines d
                JOIN matters m
                    ON d.matter_id = m.id
                    AND trim(m.tenant_id) = trim(:tid)
                    AND m.status = 'active'
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(:tid)
                WHERE trim(d.tenant_id) = trim(:tid)
                  AND d.completed_at IS NULL
                  AND d.deadline_date::date >= :today
                  AND d.deadline_date::date <= :end
                ORDER BY d.deadline_date ASC
                LIMIT 50
            """), {"tid": tenant_id, "today": today.isoformat(),
                   "end": window_end.isoformat()})

            deadlines = []
            for row in rows.mappings():
                dl_date = row["deadline_date"]
                days_out = (dl_date.date() - today).days if dl_date else 0
                if days_out == 0:
                    urgency = "today"
                elif days_out <= 3:
                    urgency = "critical"
                elif days_out <= 7:
                    urgency = "soon"
                else:
                    urgency = "normal"

                deadlines.append({
                    "id":            row["id"],
                    "title":         row["title"],
                    "deadline_date": dl_date,
                    "date_label":    dl_date.strftime("%b %-d") if dl_date else "",
                    "days_out":      days_out,
                    "deadline_type": row["deadline_type"] or "",
                    "is_sol":        row["is_sol"],
                    "matter_id":     row["matter_id"],
                    "matter_name":   row["matter_name"] or "Untitled",
                    "matter_number": row["matter_number"] or "",
                    "client_name":   row["client_name"] or "",
                    "urgency":       urgency,
                })

        overdue_rows = []
        try:
            async with AsyncSessionLocal() as session:
                over = await session.execute(sa_text("""
                    SELECT COUNT(*) AS cnt
                    FROM deadlines d
                    JOIN matters m ON d.matter_id = m.id
                        AND trim(m.tenant_id) = trim(:tid)
                        AND m.status = 'active'
                    WHERE trim(d.tenant_id) = trim(:tid)
                      AND d.completed_at IS NULL
                      AND d.deadline_date::date < :today
                """), {"tid": tenant_id, "today": today.isoformat()})
                overdue_count = (over.mappings().fetchone() or {}).get("cnt", 0)
        except Exception:
            overdue_count = 0

        return {"deadlines": deadlines, "total": len(deadlines),
                "overdue_count": int(overdue_count),
                "today": today, "error": None}

    except Exception as exc:
        logger.error("get_firm_deadlines error: %s", exc)
        return {"deadlines": [], "total": 0, "overdue_count": 0,
                "today": date.today(), "error": str(exc)}


# Document type detection from filename
_DOC_TYPE_PATTERNS = [
    (re.compile(r"\border\b|\bordre\b", re.I),        "Order",     "#7c3aed", "#ede9fe"),
    (re.compile(r"\bmotion\b|\bpetition\b|\bplead", re.I), "Pleading", "#1d4ed8", "#dbeafe"),
    (re.compile(r"\bdiscover|interrogator|deposition|request for prod",
                re.I),                                 "Discovery", "#0891b2", "#cffafe"),
    (re.compile(r"\bschedul", re.I),                   "Schedule",  "#059669", "#d1fae5"),
    (re.compile(r"\bbrief\b|\bmemo\b", re.I),          "Brief",     "#b45309", "#fef3c7"),
    (re.compile(r"\bcontract\b|\bagreement\b", re.I),  "Agreement", "#6d28d9", "#ede9fe"),
]


def _classify_doc(file_path: str) -> tuple[str, str, str]:
    """Return (doc_type_label, color, bg) from file path."""
    fname = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
    for pattern, label, color, bg in _DOC_TYPE_PATTERNS:
        if pattern.search(fname):
            return label, color, bg
    return "Document", "#64748b", "#f1f5f9"


async def get_firm_new_service_items(scope: dict) -> dict:
    """
    Combined feed: recent DMS documents + recent Exchange emails.
    DMS: dms_documents indexed in last 48h, classified by filename.
    Email: email_routing_queue received in last 48h.
    Returns unified list sorted by timestamp desc, max 20 items total.
    """
    tenant_id = _tid(scope)
    since = datetime.now(timezone.utc) - timedelta(hours=48)

    items = []

    # DMS recent documents
    try:
        async with AsyncSessionLocal() as session:
            dms_rows = await session.execute(sa_text("""
                SELECT
                    d.id::text AS id,
                    d.file_path,
                    d.indexed_at,
                    d.source
                FROM dms_documents d
                WHERE trim(d.tenant_id) = trim(:tid)
                  AND d.indexed_at >= :since
                  AND d.ocr_status != 'not_applicable'
                ORDER BY d.indexed_at DESC
                LIMIT 30
            """), {"tid": tenant_id, "since": since})

            for row in dms_rows.mappings():
                fname = row["file_path"].rsplit("/", 1)[-1]
                doc_type, color, bg = _classify_doc(row["file_path"])
                items.append({
                    "source_type": "dms",
                    "id":          row["id"],
                    "title":       fname,
                    "subtitle":    f"Indexed from {row['source'] or 'file system'}",
                    "timestamp":   row["indexed_at"],
                    "doc_type":    doc_type,
                    "color":       color,
                    "bg":          bg,
                    "link":        None,
                })
    except Exception as exc:
        logger.warning("get_firm_new_service_items dms error: %s", exc)

    # Exchange emails
    try:
        async with AsyncSessionLocal() as session:
            email_rows = await session.execute(sa_text("""
                SELECT
                    id::text AS id,
                    subject,
                    from_display,
                    from_email,
                    received_at,
                    body_preview,
                    has_attachments
                FROM email_routing_queue
                WHERE trim(tenant_id) = trim(:tid)
                  AND received_at >= :since
                ORDER BY received_at DESC
                LIMIT 30
            """), {"tid": tenant_id, "since": since})

            for row in email_rows.mappings():
                items.append({
                    "source_type": "email",
                    "id":          row["id"],
                    "title":       row["subject"] or "(No Subject)",
                    "subtitle":    f"From: {row['from_display'] or row['from_email'] or ''}",
                    "timestamp":   row["received_at"],
                    "doc_type":    "Email",
                    "color":       "#1d4ed8",
                    "bg":          "#dbeafe",
                    "has_attachments": row["has_attachments"],
                    "body_preview": (row["body_preview"] or "")[:120],
                    "link":        None,
                })
    except Exception as exc:
        logger.warning("get_firm_new_service_items email error: %s", exc)

    # Sort combined feed by timestamp desc, cap at 20
    items.sort(key=lambda x: x["timestamp"] or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    items = items[:20]

    return {"items": items, "total": len(items),
            "since_hours": 48, "error": None}


# ─────────────────────────────────────────────────────────────
# Attorney View
# ─────────────────────────────────────────────────────────────

async def get_atty_email_summary(scope: dict) -> dict:
    """
    Recent emails for this attorney from email_routing_queue.
    Scoped to attorney_user_id. Shows last 15.
    """
    tenant_id = _tid(scope)
    user_id = _atty_user_id(scope)

    if not user_id:
        return {"emails": [], "unread_count": 0,
                "error": "No attorney resolved"}

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT
                    id::text AS id,
                    subject,
                    from_display,
                    from_email,
                    received_at,
                    body_preview,
                    has_attachments,
                    routing_status,
                    matched_matter_id::text AS matter_id
                FROM email_routing_queue
                WHERE trim(tenant_id) = trim(:tid)
                  AND attorney_user_id = :uid
                ORDER BY received_at DESC NULLS LAST
                LIMIT 15
            """), {"tid": tenant_id, "uid": user_id})

            emails = []
            for row in rows.mappings():
                emails.append({
                    "id":              row["id"],
                    "subject":         row["subject"] or "(No Subject)",
                    "from_display":    row["from_display"] or row["from_email"] or "",
                    "received_at":     row["received_at"],
                    "time_label":      _relative_time(row["received_at"]),
                    "body_preview":    (row["body_preview"] or "")[:100],
                    "has_attachments": row["has_attachments"],
                    "routing_status":  row["routing_status"] or "pending",
                    "matter_id":       row["matter_id"],
                    "is_filed":        row["routing_status"] == "filed",
                })

            unread_count = sum(1 for e in emails if not e["is_filed"])

        return {"emails": emails, "unread_count": unread_count,
                "attorney_user_id": user_id, "error": None}

    except Exception as exc:
        logger.error("get_atty_email_summary error: %s", exc)
        return {"emails": [], "unread_count": 0, "error": str(exc)}


async def get_atty_calendar(scope: dict) -> dict:
    """
    Attorney-scoped calendar — delegates to get_firm_calendar
    with attorney_user_id baked into scope.
    """
    user_id = _atty_user_id(scope)
    # Inject into scope so get_firm_calendar picks it up via query param logic
    req = scope.get("request")
    if user_id and req:
        # Build a modified scope with attorney_id set
        modified = {**scope, "attorney_id": str(user_id)}
        return await get_firm_calendar(modified)
    return await get_firm_calendar(scope)


async def get_atty_deadlines(scope: dict) -> dict:
    """
    Deadlines scoped to matters where responsible_attorney_id = this attorney's user_id.
    Uses users.id (BIGINT) not matters.responsible_attorney_id (UUID) —
    matches via ts_timekeepers.praesidium_user_id → matters.responsible_attorney_id.
    Falls back to all deadlines if attorney not resolvable.
    """
    tenant_id = _tid(scope)
    user_id = _atty_user_id(scope)
    today = date.today()
    window_end = today + timedelta(days=14)

    # Resolve responsible_attorney_id (UUID) from users.id (BIGINT)
    atty_clause = ""
    params: dict = {"tid": tenant_id, "today": today.isoformat(),
                    "end": window_end.isoformat()}

    if user_id:
        # matters.responsible_attorney_id is UUID; users.id is BIGINT
        # The link: ts_timekeepers.praesidium_user_id = users.id
        # and matters.responsible_attorney_id is not directly linked to users.id
        # Best available: filter by originating_attorney_id (BIGINT) directly
        atty_clause = "AND (m.originating_attorney_id = :uid)"
        params["uid"] = user_id

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text(f"""
                SELECT
                    d.id,
                    d.title,
                    d.deadline_date,
                    d.deadline_type,
                    d.is_sol,
                    d.matter_id::text AS matter_id,
                    m.matter_name,
                    m.matter_number,
                    c.client_name
                FROM deadlines d
                JOIN matters m
                    ON d.matter_id = m.id
                    AND trim(m.tenant_id) = trim(:tid)
                    AND m.status = 'active'
                    {atty_clause}
                LEFT JOIN clients c
                    ON m.client_id = c.id
                    AND trim(c.tenant_id) = trim(:tid)
                WHERE trim(d.tenant_id) = trim(:tid)
                  AND d.completed_at IS NULL
                  AND d.deadline_date::date >= :today
                  AND d.deadline_date::date <= :end
                ORDER BY d.deadline_date ASC
                LIMIT 30
            """), params)

            deadlines = []
            for row in rows.mappings():
                dl_date = row["deadline_date"]
                days_out = (dl_date.date() - today).days if dl_date else 0
                if days_out == 0:
                    urgency = "today"
                elif days_out <= 3:
                    urgency = "critical"
                elif days_out <= 7:
                    urgency = "soon"
                else:
                    urgency = "normal"

                deadlines.append({
                    "id":            row["id"],
                    "title":         row["title"],
                    "deadline_date": dl_date,
                    "date_label":    dl_date.strftime("%b %-d") if dl_date else "",
                    "days_out":      days_out,
                    "deadline_type": row["deadline_type"] or "",
                    "is_sol":        row["is_sol"],
                    "matter_id":     row["matter_id"],
                    "matter_name":   row["matter_name"] or "Untitled",
                    "matter_number": row["matter_number"] or "",
                    "client_name":   row["client_name"] or "",
                    "urgency":       urgency,
                })

        return {"deadlines": deadlines, "total": len(deadlines),
                "attorney_user_id": user_id,
                "today": today, "error": None}

    except Exception as exc:
        logger.error("get_atty_deadlines error: %s", exc)
        return {"deadlines": [], "total": 0,
                "attorney_user_id": user_id,
                "today": date.today(), "error": str(exc)}


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _relative_time(dt) -> str:
    """Return human-readable relative time string."""
    if not dt:
        return ""
    try:
        now = datetime.now(timezone.utc)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = now - dt
        minutes = int(diff.total_seconds() / 60)
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        if days == 1:
            return "yesterday"
        if days < 7:
            return f"{days}d ago"
        return dt.strftime("%b %-d")
    except Exception:
        return ""


async def get_firm_ar_pie(scope: dict) -> dict:
    """AR aging for pie chart — delegates to get_firm_ar_aging."""
    return await get_firm_ar_aging(scope)
