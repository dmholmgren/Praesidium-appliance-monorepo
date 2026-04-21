"""
modules/admin/licensing_api.py
Module 8 Component 3 — Licensing & Tenant Admin

FastAPI router: licensing management and tenant admin endpoints.
Served under /admin/* and wired into the global admin panel.

Pages (GET — session-protected, HTMX-compatible):
  GET  /admin/licensing              — global feature overrides + tier summary
  GET  /admin/tenant/{tenant_id}     — tenant detail: license, features, provisioning log

API endpoints (JSON — session-protected):
  GET  /admin/api/licensing/tenants          — all tenants with license summary
  GET  /admin/api/licensing/tenant/{id}      — single tenant full license detail
  POST /admin/api/licensing/tenant/{id}      — provision/update tenant license
  GET  /admin/api/licensing/overrides        — list all global feature overrides
  POST /admin/api/licensing/overrides        — set a global feature override
  DELETE /admin/api/licensing/overrides/{flag} — remove global override (restores tenant control)
  GET  /admin/api/licensing/features         — list all known feature flags with descriptions
  GET  /admin/api/licensing/check/{id}/{flag} — live check_feature() call for a tenant+flag

HTMX partials:
  GET  /admin/htmx/license-grid       — tenant license rows (auto-refresh target)
  POST /admin/htmx/license/provision  — provision form submit → returns result fragment

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from core.licensing import (
    FEATURE_KNOWLEDGE_GRAPH, FEATURE_ISSUE_MAP_VERSIONING,
    FEATURE_CASE_DRIFT_DETECTION, FEATURE_WIAM, FEATURE_DEPO_PREP,
    FEATURE_AI_REVIEW_PASSES, FEATURE_TAG_INTELLIGENCE,
    FEATURE_DISCOVERY_POSTMORTEM, FEATURE_PRODUCTION_BATES,
    FEATURE_INTELLIGENCE_CHAT, FEATURE_CROSS_MATTER,
    FEATURE_GLOBAL_LEARNING_NETWORK, FEATURE_TENANT_ADMIN,
    FEATURE_CASE_DRIFT_MAP_UI, FEATURE_EDISCOVERY, FEATURE_BILLING,
    FEATURE_COURT_CALENDAR, FEATURE_DMS, FEATURE_SMS_PORTAL,
    check_feature, provision_tenant_license,
)

log = logging.getLogger("praesidium.licensing_api")

router = APIRouter(prefix="/admin")

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../../templates/admin")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

# ── All known feature flags with human-readable labels ───────────────────────
ALL_FEATURES = [
    (FEATURE_KNOWLEDGE_GRAPH,         "Knowledge Graph",           "intelligence"),
    (FEATURE_ISSUE_MAP_VERSIONING,     "Issue Map Versioning",      "intelligence"),
    (FEATURE_CASE_DRIFT_DETECTION,     "Case Drift Detection",      "intelligence"),
    (FEATURE_WIAM,                     "WIAM",                      "intelligence"),
    (FEATURE_DEPO_PREP,                "Depo Prep",                 "intelligence"),
    (FEATURE_AI_REVIEW_PASSES,         "AI Review Passes",          "ediscovery"),
    (FEATURE_TAG_INTELLIGENCE,         "Tag Intelligence",          "ediscovery"),
    (FEATURE_DISCOVERY_POSTMORTEM,     "Discovery Post-Mortem",     "ediscovery"),
    (FEATURE_PRODUCTION_BATES,         "Production Bates",          "ediscovery"),
    (FEATURE_INTELLIGENCE_CHAT,        "Intelligence Chat",         "intelligence"),
    (FEATURE_CROSS_MATTER,             "Cross-Matter Intelligence", "enterprise"),
    (FEATURE_GLOBAL_LEARNING_NETWORK,  "Global Learning Network",   "enterprise"),
    (FEATURE_TENANT_ADMIN,             "Tenant Admin",              "all"),
    (FEATURE_CASE_DRIFT_MAP_UI,        "Case Drift Map UI",         "intelligence"),
    (FEATURE_EDISCOVERY,               "eDiscovery",                "litigation"),
    (FEATURE_BILLING,                  "Billing",                   "all"),
    (FEATURE_COURT_CALENDAR,           "Court Calendar",            "all"),
    (FEATURE_DMS,                      "DMS",                       "all"),
    (FEATURE_SMS_PORTAL,               "SMS Portal",                "litigation"),
]

# Tier → default feature set
TIER_DEFAULTS = {
    "foundation":   {f for f, _, t in ALL_FEATURES if t == "all"},
    "litigation":   {f for f, _, t in ALL_FEATURES if t in ("all", "litigation")},
    "intelligence": {f for f, _, t in ALL_FEATURES if t in ("all", "litigation", "intelligence")},
    "enterprise":   {f for f, _, _ in ALL_FEATURES},
}

VALID_TIERS = ["foundation", "litigation", "intelligence", "enterprise"]


# ── Auth (reuse admin panel session) ─────────────────────────────────────────
from modules.admin.admin_panel import require_admin_session


# ── Pydantic models ───────────────────────────────────────────────────────────

class ProvisionRequest(BaseModel):
    tier: str
    feature_flags: dict[str, bool]
    billing_plan: Optional[str] = None
    notes: Optional[str] = None
    expires_at: Optional[str] = None


class OverrideRequest(BaseModel):
    feature_flag: str
    enabled_globally: bool
    reason: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# Admin pages
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/licensing", response_class=HTMLResponse)
async def admin_licensing(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Global licensing overview: all tenants + global feature overrides."""
    async with AsyncSessionLocal() as session:
        # Tenants with license summary
        tenants_result = await session.execute(
            text("""
            SELECT t.id, t.slug, t.domain, t.status,
                   tl.tier, tl.expires_at, tl.feature_flags,
                   tl.billing_plan
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            ORDER BY t.slug
            """)
        )
        tenants = [dict(r) for r in tenants_result.mappings().fetchall()]

        # Global overrides
        overrides_result = await session.execute(
            text("""
            SELECT feature_flag, enabled_globally, reason, updated_at
            FROM feature_overrides
            ORDER BY feature_flag
            """)
        )
        overrides = {r["feature_flag"]: dict(r) for r in overrides_result.mappings().fetchall()}

    # Count enabled features per tenant
    for t in tenants:
        flags = t.get("feature_flags") or {}
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except Exception:
                flags = {}
        t["enabled_count"] = sum(1 for v in flags.values() if v)
        t["total_features"] = len(ALL_FEATURES)

    return templates.TemplateResponse("licensing.html", {
        "request": request,
        "tenants": tenants,
        "overrides": overrides,
        "all_features": ALL_FEATURES,
        "page": "licensing",
    })


@router.get("/tenant/{tenant_id}", response_class=HTMLResponse)
async def admin_tenant_detail(
    tenant_id: str,
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Tenant detail: full license, feature flags, provisioning log."""
    async with AsyncSessionLocal() as session:
        # Tenant
        t_result = await session.execute(
            text("""
            SELECT t.id, t.slug, t.domain, t.status, t.ai_provider,
                   t.storage_adapter, t.middleware_server_id,
                   tl.tier, tl.feature_flags, tl.billing_plan,
                   tl.expires_at, tl.notes as license_notes,
                   ms.name as middleware_server_name
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            LEFT JOIN middleware_servers ms ON ms.id = t.middleware_server_id
            WHERE t.id = :id
            """),
            {"id": tenant_id}
        )
        tenant = t_result.mappings().fetchone()
        if not tenant:
            raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
        tenant = dict(tenant)

        # Parse feature flags
        flags = tenant.get("feature_flags") or {}
        if isinstance(flags, str):
            try:
                flags = json.loads(flags)
            except Exception:
                flags = {}
        tenant["feature_flags_parsed"] = flags

        # Global overrides
        overrides_result = await session.execute(
            text("SELECT feature_flag, enabled_globally FROM feature_overrides")
        )
        global_overrides = {r[0]: bool(r[1]) for r in overrides_result.fetchall()}

        # Provisioning log
        log_result = await session.execute(
            text("""
            SELECT action, triggered_by, detail, created_at
            FROM tenant_provisioning_log
            WHERE tenant_id = :id
            ORDER BY created_at DESC
            LIMIT 20
            """),
            {"id": tenant_id}
        )
        prov_log = [dict(r) for r in log_result.mappings().fetchall()]

    return templates.TemplateResponse("tenant_detail.html", {
        "request": request,
        "tenant": tenant,
        "all_features": ALL_FEATURES,
        "global_overrides": global_overrides,
        "prov_log": prov_log,
        "valid_tiers": VALID_TIERS,
        "tier_defaults": {k: list(v) for k, v in TIER_DEFAULTS.items()},
        "page": "tenants",
    })


# ══════════════════════════════════════════════════════════════════════════════
# JSON API endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/api/licensing/features")
async def list_features(_: str = Depends(require_admin_session)):
    """List all known feature flags with labels and tier associations."""
    return {
        "features": [
            {"flag": f, "label": label, "tier": tier}
            for f, label, tier in ALL_FEATURES
        ]
    }


@router.get("/api/licensing/tenants")
async def list_tenants_licensing(_: str = Depends(require_admin_session)):
    """All tenants with license summary."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT t.id, t.slug, t.domain, t.status,
                   tl.tier, tl.expires_at, tl.billing_plan,
                   tl.feature_flags
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            ORDER BY t.slug
            """)
        )
        rows = [dict(r) for r in result.mappings().fetchall()]
    return {"tenants": rows}


@router.get("/api/licensing/tenant/{tenant_id}")
async def get_tenant_license(
    tenant_id: str,
    _: str = Depends(require_admin_session),
):
    """Full license detail for a single tenant."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT t.id, t.slug, tl.tier, tl.feature_flags,
                   tl.billing_plan, tl.notes, tl.expires_at
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            WHERE t.id = :id
            """),
            {"id": tenant_id}
        )
        row = result.mappings().fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return dict(row)


@router.post("/api/licensing/tenant/{tenant_id}", status_code=200)
async def provision_tenant(
    tenant_id: str,
    body: ProvisionRequest,
    _: str = Depends(require_admin_session),
):
    """Provision or update a tenant license. Uses core/licensing.py."""
    if body.tier not in VALID_TIERS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid tier '{body.tier}'. Must be one of: {VALID_TIERS}"
        )
    ok = await provision_tenant_license(
        tenant_id=tenant_id,
        tier=body.tier,
        feature_flags=body.feature_flags,
        billing_plan=body.billing_plan,
        notes=body.notes,
        expires_at=body.expires_at,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="License provision failed — check logs")
    return {"ok": True, "tenant_id": tenant_id, "tier": body.tier}


@router.get("/api/licensing/overrides")
async def list_overrides(_: str = Depends(require_admin_session)):
    """List all global feature overrides."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT feature_flag, enabled_globally, reason, updated_at FROM feature_overrides ORDER BY feature_flag")
        )
        rows = [dict(r) for r in result.mappings().fetchall()]
    return {"overrides": rows, "count": len(rows)}


@router.post("/api/licensing/overrides", status_code=200)
async def set_override(
    body: OverrideRequest,
    _: str = Depends(require_admin_session),
):
    """Set or update a global feature override."""
    # Validate flag name
    known_flags = {f for f, _, _ in ALL_FEATURES}
    if body.feature_flag not in known_flags:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown feature flag '{body.feature_flag}'"
        )
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
            INSERT INTO feature_overrides (feature_flag, enabled_globally, reason, updated_at)
            VALUES (:flag, :enabled, :reason, NOW())
            ON CONFLICT (feature_flag) DO UPDATE
              SET enabled_globally = :enabled,
                  reason = :reason,
                  updated_at = NOW()
            """),
            {
                "flag": body.feature_flag,
                "enabled": body.enabled_globally,
                "reason": body.reason,
            }
        )
        await session.commit()
    log.info(f"Global override set: {body.feature_flag} = {body.enabled_globally}")
    return {"ok": True, "feature_flag": body.feature_flag, "enabled_globally": body.enabled_globally}


@router.delete("/api/licensing/overrides/{feature_flag}", status_code=200)
async def delete_override(
    feature_flag: str,
    _: str = Depends(require_admin_session),
):
    """Remove a global feature override — restores tenant-level control."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("DELETE FROM feature_overrides WHERE feature_flag = :flag"),
            {"flag": feature_flag}
        )
        await session.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Override for '{feature_flag}' not found")
    return {"ok": True, "feature_flag": feature_flag, "removed": True}


@router.get("/api/licensing/check/{tenant_id}/{feature_flag}")
async def check_feature_live(
    tenant_id: str,
    feature_flag: str,
    _: str = Depends(require_admin_session),
):
    """Live check_feature() call — returns current effective value for a tenant+flag."""
    result = await check_feature(tenant_id, feature_flag)
    return {
        "tenant_id": tenant_id,
        "feature_flag": feature_flag,
        "enabled": result,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# HTMX partials
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/htmx/license-grid", response_class=HTMLResponse)
async def htmx_license_grid(
    request: Request,
    _: str = Depends(require_admin_session),
):
    """Tenant license rows fragment for auto-refresh."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
            SELECT t.id, t.slug, t.status,
                   tl.tier, tl.expires_at, tl.feature_flags
            FROM tenants t
            LEFT JOIN tenant_licenses tl ON tl.tenant_id = t.id
            ORDER BY t.slug
            """)
        )
        tenants = []
        for r in result.mappings().fetchall():
            row = dict(r)
            flags = row.get("feature_flags") or {}
            if isinstance(flags, str):
                try:
                    flags = json.loads(flags)
                except Exception:
                    flags = {}
            row["enabled_count"] = sum(1 for v in flags.values() if v)
            tenants.append(row)

    return templates.TemplateResponse("partials/license_grid.html", {
        "request": request,
        "tenants": tenants,
    })


@router.post("/htmx/license/provision", response_class=HTMLResponse)
async def htmx_provision_license(
    request: Request,
    tenant_id: str = Form(...),
    tier: str = Form(...),
    _: str = Depends(require_admin_session),
):
    """Provision a tenant license with tier defaults — returns result fragment."""
    if tier not in VALID_TIERS:
        return HTMLResponse(
            f'<div class="text-red-400 text-xs p-2">Invalid tier: {tier}</div>'
        )

    # Build feature flags from tier defaults
    default_flags = TIER_DEFAULTS.get(tier, set())
    feature_flags = {f: (f in default_flags) for f, _, _ in ALL_FEATURES}

    ok = await provision_tenant_license(
        tenant_id=tenant_id,
        tier=tier,
        feature_flags=feature_flags,
        notes=f"Provisioned via admin panel — tier default for {tier}",
    )

    if ok:
        return templates.TemplateResponse("partials/provision_result.html", {
            "request": request,
            "ok": True,
            "tenant_id": tenant_id,
            "tier": tier,
            "enabled_count": sum(1 for v in feature_flags.values() if v),
        })
    return HTMLResponse(
        '<div class="text-red-400 text-xs p-2">Provision failed — check application logs.</div>'
    )
