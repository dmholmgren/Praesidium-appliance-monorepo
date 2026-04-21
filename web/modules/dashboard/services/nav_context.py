"""
modules/dashboard/services/nav_context.py
Module 9 Component 3 — Left Nav Shell

Provides get_nav_context() — an async helper that builds the `nav` dict
injected into every tenant-facing TemplateResponse.

Usage in any tenant router:
    from modules.dashboard.services.nav_context import get_nav_context

    nav = await get_nav_context(request)
    return templates.TemplateResponse(request, "some_page.html", {
        "nav": nav,
        "page_data": ...,
    })

The nav dict contains:
    nav.page          — current page key for nav highlighting (set by caller)
    nav.user_name     — full name or username of current user
    nav.user_role     — role string
    nav.theme         — 'dark' | 'light' from user profile
    nav.firm_name     — tenant display name
    nav.tenant_id     — stripped tenant_id
    nav.matter_name   — active matter name (optional, set by caller)
    nav.features      — dict of feature_* flags for this tenant
    nav.is_admin      — True if user role is admin or attorney

Schema notes:
    tenants.name                  — firm display name (no display_name column)
    tenant_licenses.feature_flags — JSONB object e.g. {"feature_ediscovery": true}
                                    one row per tenant, not one row per flag

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

log = logging.getLogger("praesidium.nav_context")

# Feature flags surfaced in the nav — subset of full flag registry
_NAV_FLAGS = [
    "feature_ediscovery",
    "feature_intelligence_chat",
    "feature_knowledge_graph",
    "feature_wiam",
    "feature_cite_it",
    "feature_viaticum_depo",
    "feature_viaticum_trial",
    "feature_billing",
    "feature_dms",
    "feature_court_calendar",
    "feature_tenant_admin",
    "feature_production_bates",
    "feature_expert_witness",
    "feature_mediation",
    "feature_cost_tracking",
    "feature_war_room",
]


async def get_nav_context(
    request: Request,
    page: str = "",
    matter_name: str = "",
) -> dict[str, Any]:
    """
    Build and return the nav context dict for tenant-facing templates.

    Falls back gracefully if session is missing or DB is unavailable —
    never raises, never crashes the page render.

    Args:
        request:      FastAPI Request object
        page:         Current page key (e.g. 'ediscovery', 'matters')
        matter_name:  Optional active matter name shown in top bar
    """
    # ── Defaults (safe fallback if anything fails) ────────────────────────────
    nav: dict[str, Any] = {
        "page": page,
        "user_name": "",
        "user_role": "",
        "theme": "dark",
        "firm_name": "",
        "tenant_id": "",
        "matter_name": matter_name,
        "features": {flag: False for flag in _NAV_FLAGS},
        "is_admin": False,
    }

    try:
        # ── Resolve session token ─────────────────────────────────────────────
        session_token = (
            request.cookies.get("session_token")
            or request.cookies.get("praesidium_session")
        )
        if not session_token:
            return nav

        async with AsyncSessionLocal() as db:
            # ── User + session ────────────────────────────────────────────────
            user_row = await db.execute(
                text("""
                    SELECT u.id, u.full_name, u.username, u.role,
                           COALESCE(u.theme_preference, 'dark') AS theme,
                           s.tenant_id
                    FROM sessions s
                    JOIN users u ON u.id = s.user_id
                    WHERE s.token = :token AND s.expires_at > NOW()
                """),
                {"token": session_token},
            )
            user = user_row.fetchone()
            if not user:
                return nav

            tenant_id = (user.tenant_id or "").strip()
            nav["tenant_id"] = tenant_id
            nav["user_name"] = user.full_name or user.username or ""
            nav["user_role"] = user.role or ""
            nav["theme"] = user.theme or "dark"
            nav["is_admin"] = user.role in ("admin", "attorney")

            # ── Tenant display name ───────────────────────────────────────────
            # tenants.name is the firm name — no display_name column exists
            tenant_row = await db.execute(
                text("""
                    SELECT name
                    FROM tenants
                    WHERE id = :tid
                """),
                {"tid": tenant_id},
            )
            tenant = tenant_row.fetchone()
            if tenant:
                nav["firm_name"] = tenant.name or ""

            # ── Feature flags ─────────────────────────────────────────────────
            # tenant_licenses.feature_flags is a JSONB object — one row per
            # tenant, all flags in a single column:
            # {"feature_ediscovery": true, "feature_billing": false, ...}
            lic_row = await db.execute(
                text("""
                    SELECT feature_flags
                    FROM tenant_licenses
                    WHERE trim(tenant_id) = :tid
                    LIMIT 1
                """),
                {"tid": tenant_id},
            )
            lic = lic_row.fetchone()
            if lic and lic.feature_flags:
                flags = lic.feature_flags
                # asyncpg may return a dict already or a JSON string
                if isinstance(flags, str):
                    import json
                    flags = json.loads(flags)
                for flag in _NAV_FLAGS:
                    if flag in flags:
                        nav["features"][flag] = bool(flags[flag])

    except Exception as exc:
        log.warning("nav_context build failed (non-fatal): %s", exc)

    return nav
