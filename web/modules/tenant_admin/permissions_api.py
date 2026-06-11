"""
modules/tenant_admin/permissions_api.py
API endpoints for user permissions, matter access, and Chinese wall management.
"""
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/permissions", tags=["permissions"])

PERMISSION_KEYS = [
    {"key": "view_all_matters",    "label": "View All Matters",    "description": "See all firm matters unless Chinese-walled"},
    {"key": "view_financials",     "label": "View Financials",     "description": "See billing, AR, trust on accessible matters"},
    {"key": "edit_matters",        "label": "Edit Matters",        "description": "Create and modify matter metadata"},
    {"key": "close_matters",       "label": "Close Matters",       "description": "Close and reopen matters"},
    {"key": "manage_users",        "label": "Manage Users",        "description": "Add, edit, deactivate users"},
    {"key": "run_billing",         "label": "Run Billing",         "description": "Generate bills, approve prebills, manage invoices"},
    {"key": "ediscovery_admin",    "label": "eDiscovery Admin",    "description": "Manage collections, productions, review queues"},
    {"key": "manage_chinese_wall", "label": "Manage Chinese Wall", "description": "Create and remove Chinese wall exclusions"},
]

def _tid(request):
    return (getattr(request.state, "tenant_id", "") or "").strip()

def _ser(obj):
    import uuid as _uuid
    from decimal import Decimal
    from datetime import date
    if obj is None: return None
    if isinstance(obj, dict): return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_ser(v) for v in obj]
    if isinstance(obj, _uuid.UUID): return str(obj)
    if isinstance(obj, (datetime, date)): return obj.isoformat()
    if isinstance(obj, Decimal): return float(obj)
    return obj


# ── Permission definitions ────────────────────────────────────────────────

@router.get("/keys")
async def get_permission_keys(request: Request, user=Depends(get_current_user)):
    return JSONResponse({"keys": PERMISSION_KEYS})


# ── User permissions matrix (the checkbox grid) ──────────────────────────

@router.get("/matrix")
async def get_permissions_matrix(request: Request, user=Depends(get_current_user)):
    """Return all users with their permission checkboxes."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        users_r = await session.execute(text(
            "SELECT id, full_name, email, role, is_active "
            "FROM users WHERE TRIM(tenant_id) = :tid AND is_active = true "
            "ORDER BY role, full_name"
        ), {"tid": tid})
        users = [dict(row) for row in users_r.mappings().fetchall()]

        perms_r = await session.execute(text(
            "SELECT user_id, permission_key, granted "
            "FROM user_permissions WHERE TRIM(tenant_id) = :tid"
        ), {"tid": tid})
        perms = {}
        for row in perms_r.mappings().fetchall():
            uid = row["user_id"]
            if uid not in perms:
                perms[uid] = {}
            perms[uid][row["permission_key"]] = row["granted"]

    # Build matrix: each user gets a dict of permission_key -> granted
    for u in users:
        u["permissions"] = perms.get(u["id"], {})

    return JSONResponse(_ser({
        "users": users,
        "permission_keys": PERMISSION_KEYS,
    }))


@router.put("/user/{user_id}")
async def update_user_permissions(request: Request, user_id: int, user=Depends(get_current_user)):
    """Update permissions for a single user. Body: {permissions: {key: bool, ...}}"""
    tid = _tid(request)
    body = await request.json()
    updates = body.get("permissions", {})
    admin_id = getattr(user, "id", None) or getattr(user, "user_id", None)

    async with AsyncSessionLocal() as session:
        for pkey, granted in updates.items():
            await session.execute(text("""
                INSERT INTO user_permissions (tenant_id, user_id, permission_key, granted, granted_by, updated_at)
                VALUES (:tid, :uid, :pkey, :granted, :admin_id, NOW())
                ON CONFLICT (tenant_id, user_id, permission_key)
                DO UPDATE SET granted = :granted, granted_by = :admin_id, updated_at = NOW()
            """), {"tid": tid, "uid": user_id, "pkey": pkey, "granted": granted, "admin_id": admin_id})
        await session.commit()

    return JSONResponse({"status": "ok", "user_id": user_id, "updated": len(updates)})


# ── Chinese Wall ─────────────────────────────────────────────────────────

@router.get("/chinese-wall")
async def get_chinese_walls(request: Request, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT cw.id, cw.user_id, u.full_name as user_name, u.role as user_role,
                   cw.matter_id, m.matter_name, m.matter_number,
                   c.client_name, cw.reason,
                   cb.full_name as created_by_name, cw.created_at, cw.is_active
            FROM chinese_wall_exclusions cw
            JOIN users u ON u.id = cw.user_id
            JOIN matters m ON m.id = cw.matter_id
            JOIN clients c ON c.id = m.client_id
            LEFT JOIN users cb ON cb.id = cw.created_by
            WHERE TRIM(cw.tenant_id) = :tid AND cw.is_active = true
            ORDER BY cw.created_at DESC
        """), {"tid": tid})
        walls = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser({"walls": walls}))


@router.post("/chinese-wall")
async def create_chinese_wall(request: Request, user=Depends(get_current_user)):
    """Body: {user_id, matter_id, reason}"""
    tid = _tid(request)
    body = await request.json()
    admin_id = getattr(user, "id", None) or getattr(user, "user_id", None)

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            INSERT INTO chinese_wall_exclusions (tenant_id, user_id, matter_id, reason, created_by, created_at, is_active)
            VALUES (:tid, :uid, CAST(:mid AS uuid), :reason, :admin_id, NOW(), true)
            ON CONFLICT (tenant_id, user_id, matter_id)
            DO UPDATE SET is_active = true, reason = :reason, created_by = :admin_id, created_at = NOW()
        """), {
            "tid": tid, "uid": body["user_id"],
            "mid": body["matter_id"], "reason": body.get("reason", ""),
            "admin_id": admin_id,
        })
        await session.commit()

    return JSONResponse({"status": "ok"})


@router.delete("/chinese-wall/{wall_id}")
async def remove_chinese_wall(request: Request, wall_id: int, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE chinese_wall_exclusions SET is_active = false
            WHERE id = :wid AND TRIM(tenant_id) = :tid
        """), {"wid": wall_id, "tid": tid})
        await session.commit()
    return JSONResponse({"status": "ok"})


# ── Matter-level access (for billing matter detail) ──────────────────────

@router.get("/matter/{matter_id}")
async def get_matter_permissions(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Get all users and their access level for a specific matter."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        # Get matter info
        mr = await session.execute(text(
            "SELECT id, matter_name, is_personal, owner_user_id "
            "FROM matters WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"
        ), {"mid": matter_id, "tid": tid})
        matter = mr.mappings().fetchone()
        if not matter:
            return JSONResponse({"error": "not_found"}, status_code=404)

        # Get assigned timekeepers
        tkr = await session.execute(text("""
            SELECT mt.user_id, u.full_name, u.role as user_role, mt.role as matter_role,
                   mt.rate_override, mt.assigned_at
            FROM matter_timekeepers mt
            JOIN users u ON u.id = mt.user_id
            WHERE TRIM(mt.tenant_id) = :tid AND mt.matter_id = CAST(:mid AS uuid)
            ORDER BY mt.role, u.full_name
        """), {"tid": tid, "mid": matter_id})
        timekeepers = [dict(row) for row in tkr.mappings().fetchall()]

        # Get chinese wall exclusions for this matter
        cwr = await session.execute(text("""
            SELECT cw.id, cw.user_id, u.full_name, cw.reason
            FROM chinese_wall_exclusions cw
            JOIN users u ON u.id = cw.user_id
            WHERE TRIM(cw.tenant_id) = :tid AND cw.matter_id = CAST(:mid AS uuid) AND cw.is_active = true
        """), {"tid": tid, "mid": matter_id})
        walls = [dict(row) for row in cwr.mappings().fetchall()]

        # Get all active users for the assignment dropdown
        ur = await session.execute(text(
            "SELECT id, full_name, role, default_hourly_rate "
            "FROM users WHERE TRIM(tenant_id) = :tid AND is_active = true "
            "ORDER BY full_name"
        ), {"tid": tid})
        all_users = [dict(row) for row in ur.mappings().fetchall()]

    return JSONResponse(_ser({
        "matter": dict(matter),
        "timekeepers": timekeepers,
        "chinese_walls": walls,
        "all_users": all_users,
    }))


@router.post("/matter/{matter_id}/assign")
async def assign_attorney_to_matter(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Body: {user_id, role, rate_override?}"""
    tid = _tid(request)
    body = await request.json()

    async with AsyncSessionLocal() as session:
        # Upsert into matter_timekeepers
        await session.execute(text("""
            INSERT INTO matter_timekeepers (tenant_id, matter_id, user_id, role, rate_override, assigned_at)
            VALUES (:tid, CAST(:mid AS uuid), :uid, :role, :rate, NOW())
            ON CONFLICT (tenant_id, matter_id, user_id)
            DO UPDATE SET role = :role, rate_override = :rate
        """), {
            "tid": tid, "mid": matter_id,
            "uid": body["user_id"], "role": body.get("role", "assigned"),
            "rate": body.get("rate_override"),
        })
        await session.commit()

    return JSONResponse({"status": "ok"})


@router.delete("/matter/{matter_id}/assign/{user_id}")
async def unassign_attorney_from_matter(request: Request, matter_id: str, user_id: int, user=Depends(get_current_user)):
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            DELETE FROM matter_timekeepers
            WHERE TRIM(tenant_id) = :tid AND matter_id = CAST(:mid AS uuid) AND user_id = :uid
        """), {"tid": tid, "mid": matter_id, "uid": user_id})
        await session.commit()
    return JSONResponse({"status": "ok"})


@router.put("/matter/{matter_id}/personal")
async def toggle_personal_matter(request: Request, matter_id: str, user=Depends(get_current_user)):
    """Body: {is_personal: bool, owner_user_id: int?}"""
    tid = _tid(request)
    body = await request.json()
    is_personal = body.get("is_personal", False)
    owner_uid = body.get("owner_user_id") if is_personal else None

    async with AsyncSessionLocal() as session:
        await session.execute(text("""
            UPDATE matters SET is_personal = :ip, owner_user_id = :ouid, updated_at = NOW()
            WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
        """), {"ip": is_personal, "ouid": owner_uid, "mid": matter_id, "tid": tid})
        await session.commit()

    return JSONResponse({"status": "ok"})


# ── Bulk matter close ────────────────────────────────────────────────────

@router.get("/matters/close-candidates")
async def get_close_candidates(request: Request, user=Depends(get_current_user)):
    """Matters with close_date in the past or no billing in 12+ months."""
    tid = _tid(request)
    async with AsyncSessionLocal() as session:
        r = await session.execute(text("""
            SELECT m.id, m.matter_name, m.matter_number, c.client_name,
                   m.status, m.close_date, m.open_date,
                   MAX(ts.slip_date) as last_billing
            FROM matters m
            JOIN clients c ON c.id = m.client_id
            LEFT JOIN ts_slips ts ON ts.matter_id = m.id
            WHERE TRIM(m.tenant_id) = :tid AND m.status = 'active'
              AND (m.close_date < CURRENT_DATE
                   OR NOT EXISTS (
                       SELECT 1 FROM ts_slips t2
                       WHERE t2.matter_id = m.id AND t2.slip_date > CURRENT_DATE - INTERVAL '12 months'
                   ))
            GROUP BY m.id, m.matter_name, m.matter_number, c.client_name, m.status, m.close_date, m.open_date
            ORDER BY m.close_date NULLS LAST, last_billing NULLS FIRST
            LIMIT 200
        """), {"tid": tid})
        candidates = [dict(row) for row in r.mappings().fetchall()]
    return JSONResponse(_ser({"candidates": candidates, "count": len(candidates)}))


@router.post("/matters/bulk-close")
async def bulk_close_matters(request: Request, user=Depends(get_current_user)):
    """Body: {matter_ids: [uuid, ...]}"""
    tid = _tid(request)
    body = await request.json()
    ids = body.get("matter_ids", [])
    if not ids:
        return JSONResponse({"error": "no_ids"}, status_code=400)

    async with AsyncSessionLocal() as session:
        for mid in ids:
            await session.execute(text("""
                UPDATE matters SET status = 'closed', closed_at = NOW(), updated_at = NOW()
                WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid AND status = 'active'
            """), {"mid": mid, "tid": tid})
        await session.commit()

    return JSONResponse({"status": "ok", "closed": len(ids)})
