"""
dashboard/routes/matter_dashboard_api.py
=========================================
Matter Dashboard — JSON API
GET /api/v1/matter/{matter_id}/dashboard   — all data for matter dashboard React page
GET /api/v1/matters/home                   — matters home (search, recent, my matters, attention)
GET /api/v1/matters/search?q=...           — matter search

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
Dennis M. Holmgren, Reg. No. 54,168

Performance notes (v3 — 2026-05-22):
- Removed all TRIM(tenant_id) — data verified clean, enables index usage
- Stale billing rewritten as LATERAL subquery (avoids full time_entries scan)
- Matters-home queries parallelized with asyncio.gather
- Matter-dashboard queries parallelized (3 concurrent groups)
- Combined WIP+billed into single FILTER query (was 3 separate queries)
- Legacy doc count uses disk_file_count from dms_folder_matches when available
- New indexes: idx_te_tenant_draft, idx_te_tenant_matter_date,
  idx_matters_tenant_updated, idx_invoices_tenant_matter,
  idx_dms_docs_tenant_filepath_pattern, idx_erq_matter_received,
  idx_te_matter_date_desc
- Duplicate client-tree and timekeepers sections removed
"""
from __future__ import annotations
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["matter-dashboard"])


def _tid(request: Request) -> str:
    return (getattr(request.state, "tenant_id", "") or "").strip()

def _user_id(request: Request):
    u = getattr(request.state, "current_user", None)
    return getattr(u, "id", None) if u else None

def _fmt_money(val) -> str:
    if val is None: return "$0"
    return f"${float(val):,.0f}"

def _fmt_dt(val) -> str:
    if isinstance(val, (datetime, date)):
        return val.strftime("%b %d, %Y")
    return str(val or "")

def _relative_time(dt) -> str:
    if not dt: return ""
    try:
        now = datetime.now(timezone.utc)
        if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
        mins = int((now - dt).total_seconds() / 60)
        if mins < 1: return "just now"
        if mins < 60: return f"{mins}m ago"
        hrs = mins // 60
        if hrs < 24: return f"{hrs}h ago"
        d = hrs // 24
        return "yesterday" if d == 1 else f"{d}d ago" if d < 7 else dt.strftime("%b %-d")
    except:
        return ""


import re as _re
_UUID_RE = _re.compile(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$', _re.IGNORECASE)

def _is_valid_uuid(val: str) -> bool:
    """Quick check — rejects 'new' and other non-UUID path segments before they hit CAST()."""
    return bool(_UUID_RE.match(val))


# ─── MATTER DASHBOARD (parallelized) ──────────────────────────────

async def _md_core(mid: str, tid: str):
    """Fetch matter details + client + attorney name."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number, m.matter_type,
                   m.practice_area, m.status, m.court, m.judge, m.jurisdiction,
                   m.cause_number, m.open_date, m.close_date, m.sol_date,
                   m.billing_type, m.hourly_rate, m.flat_fee_amount,
                   m.contingency_pct, m.retainer_amount, m.retainer_balance,
                   m.notes, m.folder_path,
                   m.court_address, m.court_phone, m.court_coordinator,
                   m.court_coordinator_phone, m.court_coordinator_email,
                   m.originating_attorney_id, m.responsible_attorney_id,
                   c.id::text AS client_id, c.client_name, c.client_type,
                   c.phone AS client_phone, c.email AS client_email,
                   c.primary_contact
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            WHERE m.id = CAST(:mid AS uuid)
              AND m.tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            return None

        matter = dict(row)
        for k in ("open_date", "close_date", "sol_date"):
            if matter.get(k):
                matter[k] = matter[k].isoformat() if hasattr(matter[k], "isoformat") else str(matter[k])
        for k in ("hourly_rate", "flat_fee_amount", "contingency_pct",
                   "retainer_amount", "retainer_balance"):
            if matter.get(k) is not None:
                matter[k] = float(matter[k])

        if matter.get("originating_attorney_id"):
            ar = await session.execute(sa_text("""
                SELECT full_name FROM users
                WHERE id = :uid AND tenant_id = :tid
            """), {"uid": int(matter["originating_attorney_id"]), "tid": tid})
            arow = ar.fetchone()
            matter["originating_attorney_name"] = arow[0] if arow else None

        return matter


async def _md_billing(mid: str, tid: str):
    """Fetch billing KPIs — combined WIP+billed in one FILTER query."""
    async with AsyncSessionLocal() as session:
        te_r = await session.execute(sa_text("""
            SELECT
                COALESCE(SUM(hours) FILTER (WHERE status = 'draft'), 0) AS wip_hours,
                COALESCE(SUM(amount) FILTER (WHERE status = 'draft'), 0) AS wip_amount,
                COALESCE(SUM(hours) FILTER (WHERE status IN ('billed','paid')), 0) AS billed_hours,
                COALESCE(SUM(amount) FILTER (WHERE status IN ('billed','paid')), 0) AS billed_amount,
                MAX(date) AS last_work_date
            FROM time_entries
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        te = te_r.mappings().fetchone()

        inv_r = await session.execute(sa_text("""
            SELECT COUNT(*) AS invoice_count,
                   COALESCE(SUM(total_amount), 0) AS total_invoiced,
                   COALESCE(SUM(CASE WHEN status = 'paid' THEN total_amount ELSE 0 END), 0) AS total_paid,
                   COALESCE(SUM(CASE WHEN status != 'paid' THEN total_amount ELSE 0 END), 0) AS ar_outstanding
            FROM invoices
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        inv = inv_r.mappings().fetchone()

        return {
            "wip_hours": float(te["wip_hours"]) if te else 0,
            "wip_amount": float(te["wip_amount"]) if te else 0,
            "billed_hours": float(te["billed_hours"]) if te else 0,
            "billed_amount": float(te["billed_amount"]) if te else 0,
            "invoice_count": int(inv["invoice_count"]) if inv else 0,
            "total_invoiced": float(inv["total_invoiced"]) if inv else 0,
            "total_paid": float(inv["total_paid"]) if inv else 0,
            "ar_outstanding": float(inv["ar_outstanding"]) if inv else 0,
            "last_work_date": te["last_work_date"].isoformat() if te and te["last_work_date"] else None,
        }


async def _md_docs_emails(mid: str, tid: str):
    """Fetch document stats + email stats in one session."""
    async with AsyncSessionLocal() as session:
        native_r = await session.execute(sa_text("""
            SELECT COUNT(*) FROM documents
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        native_count = native_r.scalar() or 0

        # Legacy doc count — use cached disk_file_count, fall back to LIKE scan
        legacy_count = 0
        try:
            cached_r = await session.execute(sa_text("""
                SELECT disk_file_count FROM dms_folder_matches
                WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            """), {"mid": mid, "tid": tid})
            cached = cached_r.fetchone()
            if cached and cached[0] is not None:
                legacy_count = int(cached[0])
            else:
                legacy_r = await session.execute(sa_text("""
                    SELECT COUNT(dd.id)
                    FROM dms_documents dd
                    JOIN dms_folder_matches mf
                        ON mf.tenant_id = :tid
                        AND dd.file_path LIKE mf.folder_path || '%%'
                    WHERE mf.matter_id = CAST(:mid AS uuid)
                      AND dd.tenant_id = :tid
                """), {"mid": mid, "tid": tid})
                legacy_count = legacy_r.scalar() or 0
        except Exception:
            pass

        folder_r = await session.execute(sa_text("""
            SELECT COUNT(*) FROM matter_folders
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        folder_count = folder_r.scalar() or 0

        email_r = await session.execute(sa_text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE filing_status = 'filed') AS filed
            FROM email_routing_queue
            WHERE matched_matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
        """), {"mid": mid, "tid": tid})
        em = email_r.mappings().fetchone()

        return {
            "documents": {
                "native_count": native_count,
                "legacy_count": legacy_count,
                "total_count": native_count + legacy_count,
                "folder_count": folder_count,
            },
            "emails": {
                "total": int(em["total"]) if em else 0,
                "filed": int(em["filed"]) if em else 0,
                "unfiled": int(em["total"]) - int(em["filed"]) if em else 0,
            },
        }


async def _md_lists(mid: str, tid: str):
    """Fetch recent time entries, invoices, emails, projects, contacts, timekeepers."""
    async with AsyncSessionLocal() as session:
        te_r = await session.execute(sa_text("""
            SELECT te.id::text, te.date, te.hours, te.amount,
                   te.description, te.status, te.timekeeper_name
            FROM time_entries te
            WHERE te.matter_id = CAST(:mid AS uuid) AND te.tenant_id = :tid
            ORDER BY te.date DESC NULLS LAST
            LIMIT 10
        """), {"mid": mid, "tid": tid})
        recent_time = []
        for row in te_r.mappings():
            recent_time.append({
                "id": row["id"],
                "date": row["date"].isoformat() if row["date"] else None,
                "hours": float(row["hours"]) if row["hours"] else 0,
                "amount": float(row["amount"]) if row["amount"] else 0,
                "description": (row["description"] or "")[:120],
                "status": row["status"] or "draft",
                "timekeeper": row["timekeeper_name"] or "",
            })

        inv_r = await session.execute(sa_text("""
            SELECT id::text, invoice_number, invoice_date, total_amount, status, created_at
            FROM invoices
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY invoice_date DESC NULLS LAST
            LIMIT 5
        """), {"mid": mid, "tid": tid})
        recent_invoices = []
        for row in inv_r.mappings():
            recent_invoices.append({
                "id": row["id"],
                "invoice_number": row["invoice_number"] or "",
                "invoice_date": row["invoice_date"].isoformat() if row["invoice_date"] else None,
                "total_amount": float(row["total_amount"]) if row["total_amount"] else 0,
                "status": row["status"] or "draft",
            })

        em_r = await session.execute(sa_text("""
            SELECT id::text, subject, from_display, received_at, has_attachments, filing_status
            FROM email_routing_queue
            WHERE matched_matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY received_at DESC NULLS LAST
            LIMIT 5
        """), {"mid": mid, "tid": tid})
        recent_emails = []
        for row in em_r.mappings():
            recent_emails.append({
                "id": row["id"],
                "subject": row["subject"] or "(No Subject)",
                "from": row["from_display"] or "",
                "received_at": row["received_at"].isoformat() if row["received_at"] else None,
                "time_label": _relative_time(row["received_at"]),
                "has_attachments": row["has_attachments"],
                "status": row["filing_status"] or "pending",
            })

        proj_r = await session.execute(sa_text("""
            SELECT id::text, title, template_type, status, created_at
            FROM projects
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY created_at DESC
            LIMIT 10
        """), {"mid": mid, "tid": tid})
        projects = []
        for row in proj_r.mappings():
            projects.append({
                "id": row["id"],
                "title": row["title"] or "Untitled",
                "template_type": row["template_type"] or "",
                "status": row["status"] or "active",
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            })

        contacts = []
        try:
            mc_r = await session.execute(sa_text("""
                SELECT mc.role, mc.created_at,
                       co.id::text AS contact_id, co.full_name, co.company,
                       co.email, co.phone, co.contact_type
                FROM matter_contacts mc
                JOIN contacts co ON mc.contact_id = co.id AND co.tenant_id = :tid
                WHERE mc.matter_id = CAST(:mid AS uuid) AND mc.tenant_id = :tid
                ORDER BY mc.role, co.full_name
                LIMIT 50
            """), {"mid": mid, "tid": tid})
            for row in mc_r.mappings():
                contacts.append({
                    "contact_id": row["contact_id"],
                    "full_name": row["full_name"] or "",
                    "company": row["company"] or "",
                    "email": row["email"] or "",
                    "phone": row["phone"] or "",
                    "role": row["role"] or "",
                    "contact_type": row["contact_type"] or "",
                })
        except Exception:
            pass

        timekeepers = []
        try:
            tk_r = await session.execute(sa_text("""
                SELECT mt.user_id, u.full_name, u.role AS user_role,
                       mt.role AS matter_role, mt.rate_override,
                       mt.assigned_at, u.default_hourly_rate
                FROM matter_timekeepers mt
                JOIN users u ON u.id = mt.user_id
                WHERE mt.matter_id = CAST(:mid AS uuid) AND mt.tenant_id = :tid
                ORDER BY mt.role, u.full_name
            """), {"mid": mid, "tid": tid})
            for row in tk_r.mappings():
                timekeepers.append({
                    "user_id": row["user_id"],
                    "full_name": row["full_name"] or "",
                    "user_role": row["user_role"] or "",
                    "matter_role": row["matter_role"] or "assigned",
                    "rate_override": float(row["rate_override"]) if row["rate_override"] else None,
                    "default_rate": float(row["default_hourly_rate"]) if row["default_hourly_rate"] else None,
                    "assigned_at": row["assigned_at"].isoformat() if row["assigned_at"] else None,
                })
        except Exception:
            pass

        return {
            "recent_time_entries": recent_time,
            "recent_invoices": recent_invoices,
            "recent_emails": recent_emails,
            "projects": projects,
            "contacts": contacts,
            "timekeepers": timekeepers,
        }


@router.get("/matter/{matter_id}/dashboard")
async def matter_dashboard(request: Request, matter_id: str):
    """Single JSON call — everything the matter dashboard React page needs.
    Parallelized: core first (404 check), then billing + docs + lists concurrent."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    if not _is_valid_uuid(matter_id):
        return JSONResponse({"error": "Invalid matter ID", "mode": "new"}, status_code=400)

    try:
        matter = await _md_core(matter_id, tid)
        if not matter:
            return JSONResponse({"error": "Matter not found"}, status_code=404)

        billing, docs_emails, lists = await asyncio.gather(
            _md_billing(matter_id, tid),
            _md_docs_emails(matter_id, tid),
            _md_lists(matter_id, tid),
        )

        result = {
            "matter": matter,
            "billing": billing,
            "documents": docs_emails["documents"],
            "emails": docs_emails["emails"],
            **lists,
        }
        return JSONResponse(result)

    except Exception as exc:
        logger.error("matter_dashboard error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)



# ─── MATTERS HOME (parallelized) ──────────────────────────────────

async def _fetch_attention(tid: str, today: date):
    """Fetch attention items: SOL, stale billing, high WIP."""
    attention = []
    async with AsyncSessionLocal() as session:
        # SOL dates approaching (next 90 days)
        sol_r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number,
                   c.client_name, m.sol_date
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            WHERE m.tenant_id = :tid
              AND m.status = 'active'
              AND m.sol_date IS NOT NULL
              AND m.sol_date >= :today
              AND m.sol_date <= :sol_end
            ORDER BY m.sol_date ASC
            LIMIT 5
        """), {"tid": tid, "today": today, "sol_end": today + timedelta(days=90)})
        for row in sol_r.mappings():
            days_out = (row["sol_date"] - today).days
            attention.append({
                "type": "sol_approaching",
                "matter_id": row["id"],
                "matter_name": row["matter_name"],
                "matter_number": row["matter_number"] or "",
                "client_name": row["client_name"] or "",
                "detail": f"SOL in {days_out} days ({row['sol_date'].strftime('%b %d, %Y')})",
                "urgency": "critical" if days_out <= 30 else "high" if days_out <= 60 else "normal",
                "sort_key": days_out,
            })

        # Stale billing — LATERAL subquery (avoids full time_entries scan)
        stale_r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number,
                   c.client_name, sub.last_work
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            CROSS JOIN LATERAL (
                SELECT MAX(te.date) AS last_work, COUNT(*) AS cnt
                FROM time_entries te
                WHERE te.matter_id = m.id AND te.tenant_id = :tid
            ) sub
            WHERE m.tenant_id = :tid
              AND m.status = 'active'
              AND sub.cnt > 0
              AND sub.last_work < :cutoff
            ORDER BY sub.last_work ASC
            LIMIT 5
        """), {"tid": tid, "cutoff": today - timedelta(days=30)})
        for row in stale_r.mappings():
            days_stale = (today - row["last_work"]).days if row["last_work"] else 999
            attention.append({
                "type": "stale_billing",
                "matter_id": row["id"],
                "matter_name": row["matter_name"],
                "matter_number": row["matter_number"] or "",
                "client_name": row["client_name"] or "",
                "detail": f"No time entry in {days_stale} days",
                "urgency": "high" if days_stale > 60 else "normal",
                "sort_key": 100 + days_stale,
            })

        # High WIP — matters with > $10K unbilled
        wip_r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number,
                   c.client_name,
                   COALESCE(SUM(te.amount), 0) AS wip_amount,
                   COALESCE(SUM(te.hours), 0) AS wip_hours
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            JOIN time_entries te ON te.matter_id = m.id
                AND te.tenant_id = :tid
                AND te.status = 'draft'
            WHERE m.tenant_id = :tid
              AND m.status = 'active'
            GROUP BY m.id, m.matter_name, m.matter_number, c.client_name
            HAVING SUM(te.amount) > 10000
            ORDER BY SUM(te.amount) DESC
            LIMIT 5
        """), {"tid": tid})
        for row in wip_r.mappings():
            attention.append({
                "type": "high_wip",
                "matter_id": row["id"],
                "matter_name": row["matter_name"],
                "matter_number": row["matter_number"] or "",
                "client_name": row["client_name"] or "",
                "detail": f"${float(row['wip_amount']):,.0f} unbilled ({float(row['wip_hours']):,.1f}h)",
                "urgency": "normal",
                "sort_key": 200,
            })

    return sorted(attention, key=lambda x: x.get("sort_key", 999))[:10]


async def _fetch_my_matters(tid: str, uid: int):
    """Fetch matters assigned to user via matter_timekeepers."""
    my_matters = []
    async with AsyncSessionLocal() as session:
        my_r = await session.execute(sa_text("""
            SELECT DISTINCT m.id::text, m.matter_name, m.matter_number,
                   m.matter_type, m.status, m.practice_area,
                   c.client_name,
                   COALESCE(wip.wip_amount, 0) AS wip_amount,
                   COALESCE(wip.wip_hours, 0) AS wip_hours,
                   m.updated_at,
                   mt.role AS my_role
            FROM matters m
            JOIN matter_timekeepers mt ON mt.matter_id = m.id
                AND mt.tenant_id = m.tenant_id
                AND mt.user_id = :uid
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            LEFT JOIN (
                SELECT matter_id,
                       SUM(amount) AS wip_amount,
                       SUM(hours) AS wip_hours
                FROM time_entries
                WHERE tenant_id = :tid AND status = 'draft'
                GROUP BY matter_id
            ) wip ON wip.matter_id = m.id
            WHERE m.tenant_id = :tid
              AND m.status = 'active'
              AND (m.is_personal = false OR m.owner_user_id = :uid)
              AND NOT EXISTS (
                  SELECT 1 FROM chinese_wall_exclusions cw
                  WHERE cw.matter_id = m.id AND cw.user_id = :uid
                    AND cw.is_active = true
              )
            ORDER BY m.matter_name
        """), {"tid": tid, "uid": uid})
        for row in my_r.mappings():
            my_matters.append({
                "id": row["id"],
                "matter_name": row["matter_name"],
                "matter_number": row["matter_number"] or "",
                "matter_type": row["matter_type"] or "",
                "status": row["status"],
                "practice_area": row["practice_area"] or "",
                "client_name": row["client_name"] or "",
                "wip_amount": float(row["wip_amount"]),
                "wip_hours": float(row["wip_hours"]),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "my_role": row["my_role"] or "",
            })
    return my_matters


async def _fetch_recent_matters(tid: str, uid: int):
    """Fetch 8 most recently updated active matters."""
    recent_matters = []
    async with AsyncSessionLocal() as session:
        recent_r = await session.execute(sa_text("""
            SELECT m.id::text, m.matter_name, m.matter_number,
                   m.matter_type, m.status, m.practice_area,
                   c.client_name,
                   COALESCE(wip.wip_amount, 0) AS wip_amount,
                   m.updated_at
            FROM matters m
            LEFT JOIN clients c ON m.client_id = c.id
                AND c.tenant_id = m.tenant_id
            LEFT JOIN (
                SELECT matter_id, SUM(amount) AS wip_amount
                FROM time_entries
                WHERE tenant_id = :tid AND status = 'draft'
                GROUP BY matter_id
            ) wip ON wip.matter_id = m.id
            WHERE m.tenant_id = :tid
              AND m.status = 'active'
              AND (m.is_personal = false OR m.owner_user_id = :uid)
              AND NOT EXISTS (
                  SELECT 1 FROM chinese_wall_exclusions cw
                  WHERE cw.matter_id = m.id AND cw.user_id = :uid
                    AND cw.is_active = true
              )
            ORDER BY m.updated_at DESC
            LIMIT 8
        """), {"tid": tid, "uid": uid})
        for row in recent_r.mappings():
            recent_matters.append({
                "id": row["id"],
                "matter_name": row["matter_name"],
                "matter_number": row["matter_number"] or "",
                "matter_type": row["matter_type"] or "",
                "status": row["status"],
                "practice_area": row["practice_area"] or "",
                "client_name": row["client_name"] or "",
                "wip_amount": float(row["wip_amount"]),
                "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                "time_label": _relative_time(row["updated_at"]),
            })
    return recent_matters


async def _fetch_stats(tid: str):
    """Fetch firm-wide matter/client counts."""
    async with AsyncSessionLocal() as session:
        stats_r = await session.execute(sa_text("""
            SELECT
                (SELECT COUNT(*) FROM matters
                 WHERE tenant_id = :tid AND status = 'active') AS active_count,
                (SELECT COUNT(DISTINCT client_id) FROM matters
                 WHERE tenant_id = :tid AND status = 'active') AS active_clients
        """), {"tid": tid})
        stats = stats_r.mappings().fetchone()
        return {
            "active_matters": int(stats["active_count"]) if stats else 0,
            "active_clients": int(stats["active_clients"]) if stats else 0,
        }


@router.get("/matters/home")
async def matters_home(request: Request):
    """Matters home page data — attention, recent, my matters.
    Queries run in parallel via separate DB sessions for speed."""
    tid = _tid(request)
    uid = _user_id(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    today = date.today()
    uid_int = int(uid) if uid else 0

    try:
        # Fire all independent queries concurrently (each gets own session)
        tasks = [
            _fetch_attention(tid, today),
            _fetch_recent_matters(tid, uid_int),
            _fetch_stats(tid),
        ]
        if uid:
            tasks.append(_fetch_my_matters(tid, uid_int))

        results = await asyncio.gather(*tasks)

        attention = results[0]
        recent_matters = results[1]
        stats = results[2]
        my_matters = results[3] if uid else []

        return JSONResponse({
            "attention": attention,
            "my_matters": my_matters,
            "recent_matters": recent_matters,
            "stats": stats,
        })

    except Exception as exc:
        logger.error("matters_home error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)



# ─── MATTER SEARCH ─────────────────────────────────────────────────

@router.get("/deals")
async def deals_list(request: Request):
    """Deal Center landing data — transactional matters with deal metadata.

    Read-only twin of the matters list, scoped to matter_type='transactional'.
    Surfaces extracted deal value / closing date / party + key-doc counts where
    present (mostly dormant per matter; populated on the seeded demo deals)."""
    tid = _tid(request)
    uid = int(_user_id(request) or 0)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT m.id::text AS id, m.matter_name, m.matter_number,
                       m.status, m.open_date, m.practice_area,
                       c.client_name,
                       fin.label AS value_label, fin.raw AS deal_value,
                       cd.label AS closing_label, cd.date_value AS closing_date,
                       COALESCE(dpc.n, 0) AS deal_points_count,
                       COALESCE(ct.n, 0)  AS contacts_count,
                       COALESCE(kd.n, 0)  AS key_docs_count,
                       subj.display_name  AS subject_name
                FROM matters m
                LEFT JOIN clients c
                       ON c.id = m.client_id AND c.tenant_id = m.tenant_id
                LEFT JOIN LATERAL (
                    SELECT point_label AS label, point_value AS raw
                    FROM matter_deal_points dp
                    WHERE dp.matter_id = m.id AND dp.tenant_id = m.tenant_id
                      AND dp.point_type = 'currency'
                    ORDER BY (point_label ILIKE '%purchase%'
                              OR point_label ILIKE '%sale%'
                              OR point_label ILIKE '%price%'
                              OR point_label ILIKE '%loan%') DESC,
                             point_label
                    LIMIT 1
                ) fin ON TRUE
                LEFT JOIN LATERAL (
                    SELECT point_label AS label, point_value AS date_value
                    FROM matter_deal_points dp
                    WHERE dp.matter_id = m.id AND dp.tenant_id = m.tenant_id
                      AND dp.point_type = 'date'
                    ORDER BY (point_label ILIKE '%clos%') DESC, point_value
                    LIMIT 1
                ) cd ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS n FROM matter_deal_points dp
                    WHERE dp.matter_id = m.id AND dp.tenant_id = m.tenant_id
                ) dpc ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS n FROM matter_contacts mc
                    WHERE mc.matter_id = m.id AND mc.tenant_id = m.tenant_id
                ) ct ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*) AS n FROM matter_key_documents k
                    WHERE k.matter_id = m.id AND k.tenant_id = m.tenant_id
                ) kd ON TRUE
                LEFT JOIN LATERAL (
                    SELECT display_name FROM matter_subjects s
                    WHERE s.matter_id = m.id AND s.tenant_id = m.tenant_id
                    ORDER BY id LIMIT 1
                ) subj ON TRUE
                WHERE TRIM(m.tenant_id) = :tid
                  AND m.matter_type = 'transactional'
                  AND (m.is_personal = false OR m.owner_user_id = :uid)
                  AND NOT EXISTS (
                      SELECT 1 FROM chinese_wall_exclusions cw
                      WHERE cw.matter_id = m.id AND cw.user_id = :uid
                        AND cw.is_active = true
                  )
                ORDER BY CASE WHEN m.status = 'active' THEN 0 ELSE 1 END,
                         (COALESCE(dpc.n, 0) > 0) DESC,
                         m.matter_name
            """), {"tid": tid, "uid": uid})
            deals, active, enriched = [], 0, 0
            for row in r.mappings():
                od, cl = row["open_date"], row["closing_date"]
                if row["status"] == "active":
                    active += 1
                if (row["deal_points_count"] or 0) > 0:
                    enriched += 1
                deals.append({
                    "id": row["id"],
                    "matter_name": row["matter_name"] or "Untitled deal",
                    "matter_number": row["matter_number"] or "",
                    "status": row["status"] or "",
                    "open_date": od.isoformat() if hasattr(od, "isoformat") else (od or None),
                    "practice_area": row["practice_area"] or "",
                    "client_name": row["client_name"] or "",
                    "subject_name": row["subject_name"] or "",
                    "deal_value": row["deal_value"] or "",
                    "value_label": row["value_label"] or "",
                    "closing_date": cl.isoformat() if hasattr(cl, "isoformat") else (cl or ""),
                    "closing_label": row["closing_label"] or "",
                    "deal_points_count": int(row["deal_points_count"] or 0),
                    "contacts_count": int(row["contacts_count"] or 0),
                    "key_docs_count": int(row["key_docs_count"] or 0),
                })
        return JSONResponse({
            "deals": deals,
            "summary": {"total": len(deals), "active": active, "enriched": enriched},
        })
    except Exception as exc:
        logger.error("deals_list error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/matters/search")
async def matters_search(request: Request, q: str = Query("", min_length=0)):
    """Search matters by name, number, client name, or cause number."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    if not q.strip():
        return JSONResponse({"results": []})

    search_term = f"%{q.strip()}%"

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT m.id::text, m.matter_name, m.matter_number,
                       m.matter_type, m.status, m.practice_area,
                       c.client_name, m.cause_number, m.court
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND c.tenant_id = m.tenant_id
                WHERE m.tenant_id = :tid
                  AND (
                    m.matter_name ILIKE :q
                    OR m.matter_number ILIKE :q
                    OR c.client_name ILIKE :q
                    OR m.cause_number ILIKE :q
                  )
                  AND (m.is_personal = false OR m.owner_user_id = :uid)
                  AND NOT EXISTS (
                      SELECT 1 FROM chinese_wall_exclusions cw
                      WHERE cw.matter_id = m.id AND cw.user_id = :uid
                        AND cw.is_active = true
                  )
                ORDER BY
                    CASE WHEN m.status = 'active' THEN 0 ELSE 1 END,
                    m.matter_name
                LIMIT 20
            """), {"tid": tid, "q": search_term, "uid": int(_user_id(request) or 0)})
            results = []
            for row in r.mappings():
                results.append({
                    "id": row["id"],
                    "matter_name": row["matter_name"],
                    "matter_number": row["matter_number"] or "",
                    "matter_type": row["matter_type"] or "",
                    "status": row["status"],
                    "practice_area": row["practice_area"] or "",
                    "client_name": row["client_name"] or "",
                    "cause_number": row["cause_number"] or "",
                    "court": row["court"] or "",
                })

        return JSONResponse({"results": results, "query": q})

    except Exception as exc:
        logger.error("matters_search error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/matter/{matter_id}/widget-layout")
async def get_widget_layout(request: Request, matter_id: str):
    """Return the saved widget layout for a matter dashboard."""
    tid = _tid(request)
    uid = _user_id(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    if not _is_valid_uuid(matter_id):
        return JSONResponse({"error": "Invalid matter ID"}, status_code=400)

    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("""
                SELECT widget_layout, layout_version, updated_at
                FROM user_widget_layouts
                WHERE context_type = 'matter_dashboard'
                  AND context_id = CAST(:mid AS uuid)
                  AND tenant_id = :tid
                  AND (user_id = :uid OR user_id IS NULL)
                ORDER BY user_id DESC NULLS LAST
                LIMIT 1
            """), {"mid": matter_id, "tid": tid, "uid": int(uid) if uid else 0})
            row = r.mappings().fetchone()

            if row and row["widget_layout"]:
                layout = row["widget_layout"]
                if isinstance(layout, str):
                    import json
                    layout = json.loads(layout)
                return JSONResponse({
                    "source": "saved",
                    "layout": layout,
                    "version": row["layout_version"],
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                })

            wr = await session.execute(sa_text("""
                SELECT widget_slug, widget_name, category, icon, default_size,
                       component_path, config_schema
                FROM widget_registry
                WHERE tenant_id = :tid
                  AND category IN ('matter', 'intelligence', 'ediscovery')
                ORDER BY category, sort_order
            """), {"tid": tid})
            available = []
            for w in wr.mappings():
                available.append({
                    "slug": w["widget_slug"],
                    "name": w["widget_name"],
                    "category": w["category"],
                    "icon": w["icon"],
                    "default_size": w["default_size"],
                    "config": w["config_schema"] if isinstance(w["config_schema"], dict) else {},
                })

            return JSONResponse({
                "source": "none",
                "layout": None,
                "available_widgets": available,
            })

    except Exception as exc:
        logger.error("get_widget_layout error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)




# ─── TRANSACTIONAL DASHBOARD ─────────────────────────────────────

async def _txn_property(mid: str, tid: str):
    """Fetch property/subject data from matter_subjects."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT display_name, address_line1, address_line2, city, state,
                   zip_code, county, acreage, parcel_id, legal_description,
                   latitude, longitude, details, data_sources
            FROM matter_subjects
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY id
            LIMIT 1
        """), {"mid": mid, "tid": tid})
        row = r.mappings().fetchone()
        if not row:
            return None
        result = dict(row)
        for k in ("acreage", "latitude", "longitude"):
            if result.get(k) is not None:
                result[k] = float(result[k])
        for k in ("details", "data_sources"):
            if result.get(k) and isinstance(result[k], str):
                import json
                try: result[k] = json.loads(result[k])
                except: pass
        return result


async def _txn_contacts(mid: str, tid: str):
    """Fetch contacts grouped by role_code with library display names."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT mc.role, mc.role_code, mc.secondary_role_code,
                   mc.is_client_side, mc.is_primary, mc.confidence,
                   mc.is_signatory, mc.signing_authority, mc.notes AS mc_notes,
                   c.id::text AS contact_id, c.full_name, c.company,
                   c.email, c.phone, c.contact_type,
                   COALESCE(lib.display_name, mc.role_code, mc.role) AS role_display
            FROM matter_contacts mc
            JOIN contacts c ON mc.contact_id = c.id AND c.tenant_id = :tid
            LEFT JOIN contact_role_library lib ON lib.code = mc.role_code AND lib.is_active = true
            WHERE mc.matter_id = CAST(:mid AS uuid) AND mc.tenant_id = :tid
            ORDER BY
                CASE mc.role_code
                    WHEN 'seller' THEN 1 WHEN 'buyer' THEN 2
                    WHEN 'lender' THEN 3 WHEN 'title_company' THEN 4
                    WHEN 'escrow_agent' THEN 5 WHEN 'broker' THEN 6
                    WHEN 'opposing_counsel' THEN 7 WHEN 'client_contact' THEN 8
                    WHEN 'surveyor' THEN 9 WHEN 'appraiser' THEN 10
                    ELSE 50
                END,
                c.full_name
        """), {"mid": mid, "tid": tid})
        contacts = []
        for row in r.mappings():
            contacts.append({
                "contact_id": row["contact_id"],
                "full_name": row["full_name"] or "",
                "company": row["company"] or "",
                "email": row["email"] or "",
                "phone": row["phone"] or "",
                "role": row["role"] or "",
                "role_code": row["role_code"] or "",
                "role_display": row["role_display"] or "",
                "is_client_side": row["is_client_side"],
                "is_signatory": row["is_signatory"],
                "contact_type": row["contact_type"] or "",
            })
        return contacts


async def _txn_deal_points(mid: str, tid: str):
    """Fetch deal points split into financial vs terms vs dates."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT point_key, point_label, point_value, point_type,
                   source_clause
            FROM matter_deal_points
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY
                CASE point_type
                    WHEN 'currency' THEN 1 WHEN 'date' THEN 2
                    WHEN 'percentage' THEN 3 ELSE 4
                END,
                point_label
        """), {"mid": mid, "tid": tid})
        financial = []
        dates = []
        terms = []
        for row in r.mappings():
            item = {
                "key": row["point_key"],
                "label": row["point_label"] or row["point_key"],
                "value": row["point_value"] or "",
                "type": row["point_type"] or "text",
                "source": row["source_clause"] or "",
            }
            if row["point_type"] == "currency":
                financial.append(item)
            elif row["point_type"] == "date":
                dates.append(item)
            else:
                terms.append(item)
        return {"financial": financial, "dates": dates, "terms": terms}


async def _txn_key_docs(mid: str, tid: str):
    """Fetch key documents with document details."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT kd.id, kd.document_role, kd.label, kd.display_order, kd.notes,
                   d.id::text AS doc_id, d.title AS doc_title,
                   d.storage_path, d.mime_type, d.file_size
            FROM matter_key_documents kd
            JOIN documents d ON kd.document_id = d.id AND d.tenant_id = :tid
            WHERE kd.matter_id = CAST(:mid AS uuid) AND kd.tenant_id = :tid
            ORDER BY kd.display_order, kd.document_role
        """), {"mid": mid, "tid": tid})
        docs = []
        for row in r.mappings():
            docs.append({
                "id": row["id"],
                "document_id": row["doc_id"],
                "role": row["document_role"] or "",
                "label": row["label"] or row["doc_title"] or "Untitled",
                "storage_path": row["storage_path"] or "",
                "mime_type": row["mime_type"] or "",
                "file_size": row["file_size"],
                "notes": row["notes"] or "",
            })
        return docs


async def _txn_critical_dates(mid: str, tid: str):
    """Fetch critical dates from deal_points (type=date) + deadlines table."""
    async with AsyncSessionLocal() as session:
        items = []
        # Deal point dates
        dp_r = await session.execute(sa_text("""
            SELECT point_label AS title, point_value AS date_value,
                   'deal_point' AS source, source_clause AS notes
            FROM matter_deal_points
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
              AND point_type = 'date'
            ORDER BY point_value
        """), {"mid": mid, "tid": tid})
        for row in dp_r.mappings():
            items.append({
                "title": row["title"] or "",
                "date": row["date_value"] or "",
                "source": "deal_point",
                "notes": row["notes"] or "",
                "completed": False,
            })

        # Deadline table entries
        dl_r = await session.execute(sa_text("""
            SELECT title, deadline_date, deadline_type, notes, completed_at
            FROM deadlines
            WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY deadline_date
        """), {"mid": mid, "tid": tid})
        for row in dl_r.mappings():
            items.append({
                "title": row["title"] or "",
                "date": row["deadline_date"].isoformat() if row["deadline_date"] else "",
                "source": row["deadline_type"] or "deadline",
                "notes": row["notes"] or "",
                "completed": row["completed_at"] is not None,
            })

        return items


async def _txn_tasks(mid: str, tid: str):
    """Fetch open tasks for the matter."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT t.id, t.title, t.priority, t.status, t.due_date,
                   t.task_type, t.description
            FROM tasks t
            WHERE t.matter_id = CAST(:mid AS uuid) AND t.tenant_id = :tid
              AND t.status NOT IN ('completed', 'cancelled')
            ORDER BY
                CASE t.priority
                    WHEN 'critical' THEN 1 WHEN 'high' THEN 2
                    WHEN 'normal' THEN 3 WHEN 'low' THEN 4
                    ELSE 5
                END,
                t.due_date NULLS LAST
            LIMIT 15
        """), {"mid": mid, "tid": tid})
        tasks = []
        for row in r.mappings():
            tasks.append({
                "id": row["id"],
                "title": row["title"] or "",
                "priority": row["priority"] or "normal",
                "status": row["status"] or "open",
                "due_date": row["due_date"].isoformat() if row["due_date"] else None,
                "task_type": row["task_type"] or "",
                "overdue": row["due_date"] is not None and row["due_date"].date() < datetime.date.today() if hasattr(row["due_date"], 'date') else False,
            })
        return tasks


async def _txn_recent_correspondence(mid: str, tid: str):
    """Fetch recent emails matched to this matter."""
    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT id::text, subject, from_display, received_at,
                   has_attachments, filing_status
            FROM email_routing_queue
            WHERE matched_matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
            ORDER BY received_at DESC NULLS LAST
            LIMIT 10
        """), {"mid": mid, "tid": tid})
        emails = []
        for row in r.mappings():
            emails.append({
                "id": row["id"],
                "subject": row["subject"] or "(No Subject)",
                "from": row["from_display"] or "",
                "received_at": row["received_at"].isoformat() if row["received_at"] else None,
                "time_label": _relative_time(row["received_at"]),
                "has_attachments": row["has_attachments"],
                "status": row["filing_status"] or "pending",
            })
        return emails


async def _txn_summary(mid: str, tid: str):
    """Fetch AI-generated matter summary if one exists."""
    async with AsyncSessionLocal() as session:
        try:
            r = await session.execute(sa_text("""
                SELECT summary_text, status, updated_at
                FROM matter_summaries
                WHERE matter_id = CAST(:mid AS uuid) AND tenant_id = :tid
                ORDER BY updated_at DESC
                LIMIT 1
            """), {"mid": mid, "tid": tid})
            row = r.mappings().fetchone()
            if row:
                return {
                    "text": row["summary_text"] or "",
                    "status": row["status"] or "ready",
                    "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
                }
        except Exception:
            pass
        return None


@router.get("/matter/{matter_id}/transactional-dashboard")
async def transactional_dashboard(request: Request, matter_id: str):
    """Single JSON call — everything the transactional matter Overview needs.
    Parallelized: core first (404 check), then all panels concurrent."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)
    if not _is_valid_uuid(matter_id):
        return JSONResponse({"error": "Invalid matter ID", "mode": "new"}, status_code=400)

    try:
        matter = await _md_core(matter_id, tid)
        if not matter:
            return JSONResponse({"error": "Matter not found"}, status_code=404)

        # Fire all panel queries concurrently
        (property_data, contacts, deal_points, key_docs,
         critical_dates, tasks, correspondence, summary,
         billing, docs_emails) = await asyncio.gather(
            _txn_property(matter_id, tid),
            _txn_contacts(matter_id, tid),
            _txn_deal_points(matter_id, tid),
            _txn_key_docs(matter_id, tid),
            _txn_critical_dates(matter_id, tid),
            _txn_tasks(matter_id, tid),
            _txn_recent_correspondence(matter_id, tid),
            _txn_summary(matter_id, tid),
            _md_billing(matter_id, tid),
            _md_docs_emails(matter_id, tid),
        )

        return JSONResponse({
            "matter": matter,
            "property": property_data,
            "contacts": contacts,
            "deal_points": deal_points,
            "key_documents": key_docs,
            "critical_dates": critical_dates,
            "tasks": tasks,
            "recent_correspondence": correspondence,
            "summary": summary,
            "billing": billing,
            "documents": docs_emails["documents"],
            "emails": docs_emails["emails"],
        })

    except Exception as exc:
        logger.error("transactional_dashboard error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─── CLIENT TREE ───────────────────────────────────────────────────

@router.get("/matters/client-tree")
async def matters_client_tree(request: Request, status: str = Query("active")):
    """Client->matter tree grouped by client, filterable by status."""
    tid = _tid(request)
    if not tid:
        return JSONResponse({"error": "No tenant"}, status_code=400)

    try:
        async with AsyncSessionLocal() as session:
            rows = await session.execute(sa_text("""
                SELECT m.id::text, m.matter_name, m.matter_number, m.matter_type,
                       m.status,
                       c.id::text AS cid, c.client_name,
                       COUNT(d.id) AS dc
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND c.tenant_id = m.tenant_id
                LEFT JOIN documents d ON d.matter_id = m.id
                    AND d.tenant_id = m.tenant_id
                WHERE m.tenant_id = :tid
                  AND m.status = :status
                GROUP BY m.id, m.matter_name, m.matter_number, m.matter_type,
                         m.status, c.id, c.client_name
                ORDER BY c.client_name NULLS LAST, m.matter_name
            """), {"tid": tid, "status": status})
            cm = {}
            for r in rows.mappings():
                cid = r["cid"] or "unknown"
                if cid not in cm:
                    cm[cid] = {
                        "client_id": cid,
                        "client_name": r["client_name"] or "Unknown Client",
                        "matters": [],
                    }
                cm[cid]["matters"].append({
                    "id": r["id"],
                    "matter_name": r["matter_name"] or "Untitled",
                    "matter_number": r["matter_number"] or "",
                    "matter_type": r["matter_type"] or "",
                    "status": r["status"] or "",
                    "doc_count": int(r["dc"] or 0),
                })
        cl = sorted(cm.values(), key=lambda x: x["client_name"])
        return JSONResponse({
            "clients": cl,
            "total_matters": sum(len(c["matters"]) for c in cl),
            "total_clients": len(cl),
            "status_filter": status,
        })

    except Exception as exc:
        logger.error("matters_client_tree error: %s", exc)
        return JSONResponse({"error": str(exc)}, status_code=500)



# === ACTIVE MATTER PICKER (web session) ===========================
# Sticky "active matter" persisted in users.user_preferences JSONB.
# Single source of truth shared with the desktop/VSTO client
# (modules/desktop/desktop_c3_router.py writes the same two keys).
_ACTIVE_MATTER_STALE_HOURS = 12

@router.get("/active-matter")
async def web_get_active_matter(request: Request):
    # Delegate to the shared reader (core.services.active_matter) so this
    # endpoint and the nav_context sync seed can never drift.
    from core.services.active_matter import read_active_matter
    am = await read_active_matter(_user_id(request), _tid(request))
    return JSONResponse({"active_matter": am})


@router.put("/active-matter")
async def web_set_active_matter(request: Request):
    tid = _tid(request)
    uid = _user_id(request)
    if not tid or not uid:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    matter_id = (body or {}).get("matter_id")

    if not matter_id:
        async with AsyncSessionLocal() as db:
            await db.execute(sa_text("""
                UPDATE users
                   SET user_preferences = COALESCE(user_preferences, '{}'::jsonb)
                       - 'active_matter_id' - 'active_matter_set_at'
                 WHERE id = :uid AND TRIM(tenant_id) = :tid
            """), {"uid": int(uid), "tid": tid})
            await db.commit()
        return JSONResponse({"active_matter": None})

    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT m.matter_name, m.matter_number, c.client_name
            FROM matters m
            LEFT JOIN clients c ON c.id = m.client_id
            WHERE m.id = CAST(:mid AS uuid) AND TRIM(m.tenant_id) = :tid
              AND (m.is_personal = false OR m.owner_user_id = :uid)
              AND NOT EXISTS (
                  SELECT 1 FROM chinese_wall_exclusions cw
                  WHERE cw.matter_id = m.id AND cw.user_id = :uid AND cw.is_active = true
              )
        """), {"mid": matter_id, "tid": tid, "uid": int(uid)})
        matter = r.mappings().first()
        if not matter:
            return JSONResponse({"error": "Matter not found"}, status_code=404)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text("""
            UPDATE users
               SET user_preferences = COALESCE(user_preferences, '{}'::jsonb)
                   || jsonb_build_object(
                        'active_matter_id', CAST(:mid AS text),
                        'active_matter_set_at', CAST(:now AS text))
             WHERE id = :uid AND TRIM(tenant_id) = :tid
        """), {"mid": matter_id, "now": now, "uid": int(uid), "tid": tid})
        await db.commit()
    return JSONResponse({"active_matter": {
        "matter_id": matter_id,
        "matter_name": matter["matter_name"],
        "matter_number": matter["matter_number"] or "",
        "client_name": matter["client_name"] or "",
        "set_at": now,
        "is_stale": False,
    }})
# === END ACTIVE MATTER PICKER =====================================
