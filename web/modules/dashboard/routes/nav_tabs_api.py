"""
Nav Tabs API — Dynamic section tab resolution.

GET /api/v1/nav-tabs/{layout_slug}
    ?context_id={matter_id, etc.}

Resolves section tabs for a layout scope, filtered by:
  - tenant (platform defaults + tenant overrides)
  - user role hierarchy
  - feature flags
  - user tab preferences (hidden/reordered/pinned)

Resolution precedence (same as nav_service.py and context_menu_api.py):
  1. Tenant-custom tabs (tenant_id matches) override platform defaults
  2. Platform-default tabs (tenant_id IS NULL) inherited by all tenants
  3. User prefs layer on top (hide, reorder, pin)

Cached in Redis for 5 minutes per (tenant, role, layout_slug).

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger("praesidium.nav_tabs_api")

router = APIRouter(prefix="/api/v1/nav-tabs", tags=["navigation"])

_ROLE_RANK = {
    "superadmin": 100, "admin": 90, "partner": 80, "attorney": 70,
    "associate": 60, "paralegal": 50, "staff": 40, "read_only": 20,
    "client": 10, "deal_room_guest": 5,
}

def _role_satisfies(user_role, required_role):
    if not required_role: return True
    if not user_role: return False
    return _ROLE_RANK.get(user_role, 0) >= _ROLE_RANK.get(required_role, 0)

def _resolve_template(template, context):
    if not template: return None
    try: return template.format(**context)
    except (KeyError, ValueError): return template

@router.get("/{layout_slug}")
async def get_section_tabs(layout_slug: str, request: Request,
    context_id: Optional[str] = None, matter_id: Optional[str] = None,
    client_id: Optional[str] = None, matter_type: Optional[str] = None):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    current_user = getattr(request.state, "current_user", None)
    user_role = getattr(current_user, "role", None) if current_user else None
    user_id = getattr(current_user, "id", None) if current_user else None

    cache_key = f"nav_tabs:{tenant_id}:{user_role or 'none'}:{layout_slug}:{matter_type or 'all'}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        context = _build_context(context_id, matter_id, client_id)
        for tab in cached.get("tabs", []): tab["resolved_url"] = _resolve_template(tab.get("target_ref"), context)
        return cached

    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text("""
            SELECT id, layout_slug, tab_slug, display_name, display_order,
                icon, icon_svg, permission_level, is_visible,
                target_type, target_ref, config,
                required_role, feature_flag, is_platform_standard,
                is_active, separator_before, badge_source, tenant_id
            FROM layout_tabs
            WHERE layout_slug = :layout_slug AND is_active = TRUE AND is_visible = TRUE
              AND ((tenant_id IS NOT NULL AND trim(tenant_id) = :tid) OR tenant_id IS NULL)
              AND (matter_types IS NULL OR :matter_type = ANY(matter_types))
            ORDER BY display_order ASC
        """), {"layout_slug": layout_slug, "tid": tenant_id, "matter_type": matter_type or ""})
        rows = [dict(r) for r in result.mappings().fetchall()]

    by_slug = {}
    for row in rows:
        slug = row["tab_slug"]
        if row["tenant_id"] is not None: by_slug[slug] = row
        elif slug not in by_slug: by_slug[slug] = row
    tabs = sorted(by_slug.values(), key=lambda x: x["display_order"])
    tabs = [t for t in tabs if _role_satisfies(user_role, t.get("required_role"))]
    tabs = [t for t in tabs if _role_satisfies(user_role, t.get("permission_level"))]

    feature_flags = await _get_feature_flags(tenant_id)
    tabs = [t for t in tabs if not t.get("feature_flag") or feature_flags.get(t["feature_flag"], False)]
    filtered = []
    for t in tabs:
        cfg = t.get("config")
        if isinstance(cfg, str):
            try: cfg = json.loads(cfg)
            except: cfg = {}
        elif cfg is None: cfg = {}
        cfg_flag = cfg.get("feature_flag")
        if cfg_flag and not feature_flags.get(cfg_flag, False): continue
        t["config"] = cfg
        filtered.append(t)
    tabs = filtered

    user_prefs = {}
    if user_id: user_prefs = await _get_user_prefs(tenant_id, user_id, layout_slug)
    context = _build_context(context_id, matter_id, client_id)

    response_tabs = []
    for tab in tabs:
        slug = tab["tab_slug"]
        prefs = user_prefs.get(slug, {})
        if prefs.get("is_hidden", False): continue
        response_tabs.append({
            "tab_slug": slug, "display_name": tab["display_name"],
            "display_order": prefs.get("custom_order", tab["display_order"]),
            "icon": tab["icon"], "icon_svg": tab.get("icon_svg") or "",
            "target_type": tab["target_type"], "target_ref": tab.get("target_ref"),
            "resolved_url": _resolve_template(tab.get("target_ref"), context),
            "separator_before": tab.get("separator_before", False),
            "badge_source": tab.get("badge_source"), "badge_count": None,
            "config": tab.get("config", {}), "is_pinned": prefs.get("is_pinned", False),
        })
    response_tabs.sort(key=lambda x: (not x["is_pinned"], x["display_order"]))
    result_data = {"layout_slug": layout_slug, "tabs": response_tabs}
    await _cache_set(cache_key, {"layout_slug": layout_slug, "tabs": [{**t, "resolved_url": None} for t in response_tabs]}, ttl=300)
    return result_data

@router.put("/{layout_slug}/{tab_slug}/prefs")
async def update_tab_prefs(layout_slug: str, tab_slug: str, request: Request):
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    current_user = getattr(request.state, "current_user", None)
    user_id = getattr(current_user, "id", None) if current_user else None
    if not user_id: return {"error": "Authentication required"}
    body = await request.json()
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            INSERT INTO user_tab_prefs (tenant_id, user_id, layout_slug, tab_slug, is_hidden, custom_order, is_pinned, updated_at)
            VALUES (:tid, :uid, :ls, :ts, :hidden, :order, :pinned, now())
            ON CONFLICT ON CONSTRAINT uq_user_tab_prefs_user_layout_tab
            DO UPDATE SET is_hidden = :hidden, custom_order = :order, is_pinned = :pinned, updated_at = now()
        """), {"tid": tenant_id, "uid": user_id, "ls": layout_slug, "ts": tab_slug,
               "hidden": body.get("is_hidden", False), "order": body.get("custom_order"), "pinned": body.get("is_pinned", False)})
        await session.commit()
    await _invalidate_cache(tenant_id, layout_slug)
    return {"status": "ok", "layout_slug": layout_slug, "tab_slug": tab_slug}

def _build_context(context_id, matter_id, client_id):
    return {"context_id": context_id or "", "matter_id": matter_id or context_id or "", "client_id": client_id or ""}

async def _get_feature_flags(tenant_id):
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("SELECT feature_flags FROM tenant_licenses WHERE trim(tenant_id) = :tid LIMIT 1"), {"tid": tenant_id})
            row = r.fetchone()
            if row and row[0]:
                flags = row[0]
                if isinstance(flags, str): flags = json.loads(flags)
                return flags
    except Exception as exc: logger.warning("Failed to get feature flags: %s", exc)
    return {}

async def _get_user_prefs(tenant_id, user_id, layout_slug):
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(sa_text("SELECT tab_slug, is_hidden, custom_order, is_pinned FROM user_tab_prefs WHERE tenant_id = :tid AND user_id = :uid AND layout_slug = :ls"),
                {"tid": tenant_id, "uid": user_id, "ls": layout_slug})
            return {row["tab_slug"]: dict(row) for row in r.mappings().fetchall()}
    except Exception as exc: logger.warning("Failed to get user tab prefs: %s", exc)
    return {}

async def _cache_get(key):
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "")
        if not url: return None
        r = aioredis.from_url(url, decode_responses=True)
        val = await r.get(key)
        await r.aclose()
        if val: return json.loads(val)
    except: pass
    return None

async def _cache_set(key, value, ttl=300):
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "")
        if not url: return
        r = aioredis.from_url(url, decode_responses=True)
        await r.setex(key, ttl, json.dumps(value, default=str))
        await r.aclose()
    except: pass

async def _invalidate_cache(tenant_id, layout_slug=""):
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "")
        if not url: return
        r = aioredis.from_url(url, decode_responses=True)
        pattern = f"nav_tabs:{tenant_id}:*:{layout_slug}" if layout_slug else f"nav_tabs:{tenant_id}:*"
        keys = [k async for k in r.scan_iter(pattern)]
        if keys: await r.delete(*keys)
        await r.aclose()
    except: pass
