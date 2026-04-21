"""
modules/admin/seeding_api.py
Module 9 Component 5 — Matter & Client Seeding UI

Promotes records from Timeslips staging tables (ts_matters, ts_clients)
into the live matters and clients tables. Attorney reviews, approves,
and promotes — no shell data entry, no bulk import without review.

Workflow:
  1. View pending staging records (not yet promoted)
  2. Review each record — edit display name, status, conflict detection
  3. Approve individual records or batch-approve
  4. Promotion writes to matters/clients and stamps promoted_at

Conflict detection:
  - matter_number collision with existing matter in same tenant
  - client_number collision with existing client in same tenant
  - Duplicate legacy_id match

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.admin.seeding_api")

router = APIRouter(prefix="/admin/seed", tags=["admin-seeding"])

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../../templates/admin")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)


# ── Auth helper ───────────────────────────────────────────────────────────────

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


# ── Conflict detection helpers ────────────────────────────────────────────────

async def _check_matter_conflict(tenant_id: str, matter_number: str, legacy_id: str) -> str | None:
    """Returns conflict description or None."""
    if not matter_number and not legacy_id:
        return None
    async with AsyncSessionLocal() as db:
        if matter_number:
            row = await db.execute(
                text("""
                    SELECT matter_name FROM matters
                    WHERE tenant_id = :tid AND matter_number = :mn
                    LIMIT 1
                """),
                {"tid": tenant_id, "mn": matter_number},
            )
            existing = row.fetchone()
            if existing:
                return f"Matter number {matter_number!r} already exists: {existing.matter_name}"
        if legacy_id:
            row = await db.execute(
                text("""
                    SELECT matter_name FROM matters
                    WHERE tenant_id = :tid AND legacy_id = :lid
                    LIMIT 1
                """),
                {"tid": tenant_id, "lid": legacy_id},
            )
            existing = row.fetchone()
            if existing:
                return f"Legacy ID {legacy_id!r} already linked to: {existing.matter_name}"
    return None


async def _check_client_conflict(tenant_id: str, client_number: str, legacy_id: str) -> str | None:
    if not client_number and not legacy_id:
        return None
    async with AsyncSessionLocal() as db:
        if client_number:
            row = await db.execute(
                text("""
                    SELECT client_name FROM clients
                    WHERE tenant_id = :tid AND client_number = :cn
                    LIMIT 1
                """),
                {"tid": tenant_id, "cn": client_number},
            )
            existing = row.fetchone()
            if existing:
                return f"Client number {client_number!r} already exists: {existing.client_name}"
        if legacy_id:
            row = await db.execute(
                text("""
                    SELECT client_name FROM clients
                    WHERE tenant_id = :tid AND legacy_id = :lid
                    LIMIT 1
                """),
                {"tid": tenant_id, "lid": legacy_id},
            )
            existing = row.fetchone()
            if existing:
                return f"Legacy ID {legacy_id!r} already linked to: {existing.client_name}"
    return None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def seed_dashboard(
    request: Request,
    tenant_id: Optional[str] = None,
    source_id: Optional[str] = None,
    tab: str = "matters",
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        # Available import sources for this tenant
        sources_row = await db.execute(
            text("""
                SELECT id, display_name, adapter_type, last_sync_at
                FROM billing_import_sources
                WHERE tenant_id = :tid AND status = 'active'
                ORDER BY display_name
            """),
            {"tid": tid},
        )
        sources = [dict(r) for r in sources_row.mappings().fetchall()]

        active_source = source_id or (sources[0]["id"] if sources else None)

        matters_pending = []
        clients_pending = []
        matters_conflict = []
        clients_conflict = []

        if active_source:
            # Pending matters (not yet promoted)
            mp = await db.execute(
                text("""
                    SELECT id, ts_matter_id, ts_nickname, ts_client_name,
                           ts_description, ts_status, display_name,
                           praesidium_status, ts_last_billed,
                           first_seen_at, last_synced_at
                    FROM ts_matters
                    WHERE tenant_id = :tid AND source_id = :sid
                      AND promoted_at IS NULL
                    ORDER BY display_name ASC
                    LIMIT 200
                """),
                {"tid": tid, "sid": active_source},
            )
            matters_pending = [dict(r) for r in mp.mappings().fetchall()]

            # Pending clients
            cp = await db.execute(
                text("""
                    SELECT id, ts_client_id, ts_name, ts_address,
                           ts_email, ts_phone, ts_status,
                           first_seen_at, last_synced_at
                    FROM ts_clients
                    WHERE tenant_id = :tid AND source_id = :sid
                      AND promoted_at IS NULL
                    ORDER BY ts_name ASC
                    LIMIT 200
                """),
                {"tid": tid, "sid": active_source},
            )
            clients_pending = [dict(r) for r in cp.mappings().fetchall()]

            # Already promoted counts
            promoted_matters = await db.execute(
                text("""
                    SELECT COUNT(*) FROM ts_matters
                    WHERE tenant_id = :tid AND source_id = :sid
                      AND promoted_at IS NOT NULL
                """),
                {"tid": tid, "sid": active_source},
            )
            promoted_matters_count = promoted_matters.scalar() or 0

            promoted_clients = await db.execute(
                text("""
                    SELECT COUNT(*) FROM ts_clients
                    WHERE tenant_id = :tid AND source_id = :sid
                      AND promoted_at IS NOT NULL
                """),
                {"tid": tid, "sid": active_source},
            )
            promoted_clients_count = promoted_clients.scalar() or 0

        else:
            promoted_matters_count = 0
            promoted_clients_count = 0

    # Run conflict detection on pending matters
    for m in matters_pending:
        conflict = await _check_matter_conflict(
            tid,
            m.get("ts_matter_id", ""),
            m.get("ts_matter_id", ""),  # use ts_matter_id as legacy_id candidate
        )
        m["conflict"] = conflict

    # Run conflict detection on pending clients
    for c in clients_pending:
        conflict = await _check_client_conflict(
            tid,
            c.get("ts_client_id", ""),
            c.get("ts_client_id", ""),
        )
        c["conflict"] = conflict

    return templates.TemplateResponse(request, "seed_dashboard.html", {
        "page": "seeding",
        "sources": sources,
        "active_source": active_source,
        "tab": tab,
        "matters_pending": matters_pending,
        "clients_pending": clients_pending,
        "promoted_matters_count": promoted_matters_count,
        "promoted_clients_count": promoted_clients_count,
        "tenant_id": tid,
        "branding": getattr(request.state, "branding", None),
    })


@router.post("/matters/{staging_id}/promote")
async def promote_matter(
    request: Request,
    staging_id: str,
    tenant_id: str = Form(...),
    source_id: str = Form(...),
    display_name: str = Form(...),
    praesidium_status: str = Form("active"),
    force: str = Form("0"),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        # Load staging record
        row = await db.execute(
            text("""
                SELECT * FROM ts_matters
                WHERE id = :id AND tenant_id = :tid AND promoted_at IS NULL
            """),
            {"id": staging_id, "tid": tid},
        )
        staging = row.mappings().fetchone()
        if not staging:
            raise HTTPException(status_code=404, detail="Staging record not found or already promoted")

        # Conflict check (skip if force=1)
        if force != "1":
            conflict = await _check_matter_conflict(
                tid,
                staging.get("ts_matter_id", ""),
                staging.get("ts_matter_id", ""),
            )
            if conflict:
                raise HTTPException(status_code=409, detail=f"Conflict: {conflict}")

        # Insert into matters
        matter_id = str(uuid.uuid4())
        display = (display_name or staging.get("display_name") or staging.get("ts_nickname") or "").strip()
        status = praesidium_status if praesidium_status in ("active", "closed", "inactive") else "active"

        await db.execute(
            text("""
                INSERT INTO matters
                  (id, tenant_id, matter_name, matter_number, status,
                   legacy_id, notes, created_at, updated_at)
                VALUES
                  (:id, :tid, :name, :mnum, :status,
                   :legacy_id, :notes, NOW(), NOW())
                ON CONFLICT DO NOTHING
            """),
            {
                "id": matter_id,
                "tid": tid,
                "name": display,
                "mnum": staging.get("ts_matter_id"),
                "status": status,
                "legacy_id": staging.get("ts_matter_id"),
                "notes": staging.get("ts_description"),
            },
        )

        # Stamp staging record
        await db.execute(
            text("""
                UPDATE ts_matters
                SET praesidium_matter_id = :mid,
                    promoted_at = NOW(),
                    display_name = :display
                WHERE id = :id
            """),
            {"mid": matter_id, "display": display, "id": staging_id},
        )
        await db.commit()

    return RedirectResponse(
        f"/admin/seed?tenant_id={tid}&source_id={source_id}&tab=matters",
        status_code=303,
    )


@router.post("/matters/promote-all")
async def promote_all_matters(
    request: Request,
    tenant_id: str = Form(...),
    source_id: str = Form(...),
    skip_conflicts: str = Form("1"),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text("""
                SELECT id, ts_matter_id, display_name, ts_nickname,
                       ts_description, praesidium_status
                FROM ts_matters
                WHERE tenant_id = :tid AND source_id = :sid
                  AND promoted_at IS NULL
                ORDER BY display_name ASC
            """),
            {"tid": tid, "sid": source_id},
        )
        staging_records = [dict(r) for r in rows.mappings().fetchall()]

    promoted = 0
    skipped = 0

    for staging in staging_records:
        conflict = await _check_matter_conflict(
            tid,
            staging.get("ts_matter_id", ""),
            staging.get("ts_matter_id", ""),
        )
        if conflict and skip_conflicts == "1":
            skipped += 1
            continue

        matter_id = str(uuid.uuid4())
        display = (staging.get("display_name") or staging.get("ts_nickname") or "").strip()
        status = staging.get("praesidium_status") or "active"

        try:
            async with AsyncSessionLocal() as db2:
                await db2.execute(
                    text("""
                        INSERT INTO matters
                          (id, tenant_id, matter_name, matter_number, status,
                           legacy_id, notes, created_at, updated_at)
                        VALUES
                          (:id, :tid, :name, :mnum, :status,
                           :legacy_id, :notes, NOW(), NOW())
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "id": matter_id,
                        "tid": tid,
                        "name": display,
                        "mnum": staging.get("ts_matter_id"),
                        "status": status,
                        "legacy_id": staging.get("ts_matter_id"),
                        "notes": staging.get("ts_description"),
                    },
                )
                await db2.execute(
                    text("""
                        UPDATE ts_matters
                        SET praesidium_matter_id = :mid, promoted_at = NOW()
                        WHERE id = :id
                    """),
                    {"mid": matter_id, "id": staging["id"]},
                )
                await db2.commit()
            promoted += 1
        except Exception as exc:
            log.warning("Failed to promote matter %s: %s", staging["id"], exc)
            skipped += 1

    return RedirectResponse(
        f"/admin/seed?tenant_id={tid}&source_id={source_id}&tab=matters",
        status_code=303,
    )


@router.post("/clients/{staging_id}/promote")
async def promote_client(
    request: Request,
    staging_id: str,
    tenant_id: str = Form(...),
    source_id: str = Form(...),
    force: str = Form("0"),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("""
                SELECT * FROM ts_clients
                WHERE id = :id AND tenant_id = :tid AND promoted_at IS NULL
            """),
            {"id": staging_id, "tid": tid},
        )
        staging = row.mappings().fetchone()
        if not staging:
            raise HTTPException(status_code=404, detail="Staging record not found or already promoted")

        if force != "1":
            conflict = await _check_client_conflict(
                tid,
                staging.get("ts_client_id", ""),
                staging.get("ts_client_id", ""),
            )
            if conflict:
                raise HTTPException(status_code=409, detail=f"Conflict: {conflict}")

        client_id = str(uuid.uuid4())
        name = (staging.get("ts_name") or "").strip()

        await db.execute(
            text("""
                INSERT INTO clients
                  (id, tenant_id, client_name, client_number,
                   email, phone, address1, legacy_id,
                   is_active, created_at, updated_at)
                VALUES
                  (:id, :tid, :name, :cnum,
                   :email, :phone, :addr, :legacy_id,
                   true, NOW(), NOW())
                ON CONFLICT DO NOTHING
            """),
            {
                "id": client_id,
                "tid": tid,
                "name": name,
                "cnum": staging.get("ts_client_id"),
                "email": staging.get("ts_email"),
                "phone": staging.get("ts_phone"),
                "addr": staging.get("ts_address"),
                "legacy_id": staging.get("ts_client_id"),
            },
        )
        await db.execute(
            text("""
                UPDATE ts_clients
                SET praesidium_client_id = :cid, promoted_at = NOW()
                WHERE id = :id
            """),
            {"cid": client_id, "id": staging_id},
        )
        await db.commit()

    return RedirectResponse(
        f"/admin/seed?tenant_id={tid}&source_id={source_id}&tab=clients",
        status_code=303,
    )


@router.post("/clients/promote-all")
async def promote_all_clients(
    request: Request,
    tenant_id: str = Form(...),
    source_id: str = Form(...),
    skip_conflicts: str = Form("1"),
):
    sess = await _require_admin(request)
    tid = (tenant_id or sess["tenant_id"]).strip()

    async with AsyncSessionLocal() as db:
        rows = await db.execute(
            text("""
                SELECT id, ts_client_id, ts_name, ts_email, ts_phone, ts_address
                FROM ts_clients
                WHERE tenant_id = :tid AND source_id = :sid
                  AND promoted_at IS NULL
                ORDER BY ts_name ASC
            """),
            {"tid": tid, "sid": source_id},
        )
        staging_records = [dict(r) for r in rows.mappings().fetchall()]

    promoted = 0
    skipped = 0

    for staging in staging_records:
        conflict = await _check_client_conflict(
            tid,
            staging.get("ts_client_id", ""),
            staging.get("ts_client_id", ""),
        )
        if conflict and skip_conflicts == "1":
            skipped += 1
            continue

        client_id = str(uuid.uuid4())
        name = (staging.get("ts_name") or "").strip()

        try:
            async with AsyncSessionLocal() as db2:
                await db2.execute(
                    text("""
                        INSERT INTO clients
                          (id, tenant_id, client_name, client_number,
                           email, phone, address1, legacy_id,
                           is_active, created_at, updated_at)
                        VALUES
                          (:id, :tid, :name, :cnum,
                           :email, :phone, :addr, :legacy_id,
                           true, NOW(), NOW())
                        ON CONFLICT DO NOTHING
                    """),
                    {
                        "id": client_id,
                        "tid": tid,
                        "name": name,
                        "cnum": staging.get("ts_client_id"),
                        "email": staging.get("ts_email"),
                        "phone": staging.get("ts_phone"),
                        "addr": staging.get("ts_address"),
                        "legacy_id": staging.get("ts_client_id"),
                    },
                )
                await db2.execute(
                    text("""
                        UPDATE ts_clients
                        SET praesidium_client_id = :cid, promoted_at = NOW()
                        WHERE id = :id
                    """),
                    {"cid": client_id, "id": staging["id"]},
                )
                await db2.commit()
            promoted += 1
        except Exception as exc:
            log.warning("Failed to promote client %s: %s", staging["id"], exc)
            skipped += 1

    return RedirectResponse(
        f"/admin/seed?tenant_id={tid}&source_id={source_id}&tab=clients",
        status_code=303,
    )
