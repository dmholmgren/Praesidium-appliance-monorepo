"""
Nav Service — Dynamic Icon Rail

Queries ui_nav_items for the current tenant + user role,
returns rail items grouped by section ('main', 'bottom', 'divider').

Resolution order (same as widget_registry):
  1. Tenant-custom items (tenant_id matches) — override or extend platform defaults
  2. Platform-default items (tenant_id IS NULL) — inherited by all tenants

Role gating: required_role checked against current_user.role.
Feature flag gating: feature_flag checked against tenant_feature_flags.
Context scoping: context_scope filters items based on page context.

Results are cached in Redis for 5 minutes per tenant+role key.
"""

import json
import logging
from typing import Optional

from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

# Role hierarchy — higher rank sees everything below
_ROLE_RANK = {
    "superadmin": 100,
    "admin": 90,
    "partner": 80,
    "attorney": 70,
    "co_counsel": 70,
    "associate": 60,
    "paralegal": 50,
    "staff": 40,
    "read_only": 20,
    "client": 10,
    "deal_room_guest": 5,
}


def _role_satisfies(user_role: Optional[str], required_role: Optional[str]) -> bool:
    """Check if user_role meets or exceeds required_role."""
    if not required_role:
        return True
    if not user_role:
        return False
    return _ROLE_RANK.get(user_role, 0) >= _ROLE_RANK.get(required_role, 0)


# External roles are permission-gated: a nav item shows only when its module is
# granted in permission_matrix (default-deny). Internal roles are NEVER filtered
# here. This drives nav off the permission grant — no per-tenant nav rows.
_EXTERNAL_ROLES = frozenset({"client", "deal_room_guest", "co_counsel"})
# FIXME (fix-later flag, see chatprompts v19.0): this page_key->module map is the
# only non-DB part of external-role nav gating. It must be hand-edited (+ restart)
# every time a module is added, in lockstep with nav_tabs_api's map AND
# middleware._MODULE_PATH_PREFIXES. Move these mappings into a DB registry (a
# `module` column on ui_nav_items, or a nav_module_map table) so nav + API guard
# are 100% data-driven and a new module needs only DB rows, no code edit.
_NAV_MODULE = {
    "matters": "matters", "dms": "dms", "ediscovery": "ediscovery",
    "drafting": "drafting", "projects": "projects", "billing": "billing",
    "calendar": "calendar", "depositions": "depositions", "trial": "trial",
    "court": "court",
}


def _nav_module(item):
    return (_NAV_MODULE.get(item.get("page_key") or "")
            or _NAV_MODULE.get(item.get("nav_key") or ""))


async def _granted_modules(tenant_id, role):
    from core.db.base import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "SELECT DISTINCT module FROM permission_matrix "
            "WHERE TRIM(tenant_id) = trim(:t) AND role = :r "
            "AND action = 'view' AND allowed = TRUE"), {"t": tenant_id, "r": role})
        return {row[0] for row in r.fetchall()}


async def get_nav_items(tenant_id: str, user_role: Optional[str] = None, context: Optional[dict] = None) -> dict:
    """
    Return nav items for the icon rail, grouped by section.

    Returns:
        {
            "main": [...],
            "bottom": [...],
            "divider_positions": [450, 650, 750, ...]
        }

    The template iterates `main` items in order, inserting a divider
    when the current display_order crosses a value in divider_positions.
    """
    from core.db.base import AsyncSessionLocal

    # ── Try Redis cache ──
    cache_key = f"nav_rail:{tenant_id}:{user_role or 'none'}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    async with AsyncSessionLocal() as session:
        r = await session.execute(sa_text("""
            SELECT
                nav_key, label, rail_label, icon, icon_svg, url_path,
                parent_key, display_order, required_role, feature_flag,
                is_active, tenant_id, section, badge_source,
                context_scope, page_key, icon_emoji
            FROM ui_nav_items
            WHERE (
                    (tenant_id IS NOT NULL AND trim(tenant_id) = trim(:tid))
                    OR (tenant_id IS NULL AND is_active = TRUE)
                  )
            ORDER BY display_order ASC
        """), {"tid": tenant_id})
        rows = [dict(row) for row in r.mappings().fetchall()]

    # ── Resolve tenant overrides ──
    # Tenant rows override platform defaults; an INACTIVE tenant row SUPPRESSES
    # the platform default (registry-driven hide).
    _tenant_rows = {r["nav_key"]: r for r in rows if r["tenant_id"] is not None}
    _suppressed = {k for k, r in _tenant_rows.items() if not r["is_active"]}
    by_key = {}
    for row in rows:
        key = row["nav_key"]
        if key in _suppressed:
            by_key.pop(key, None)
            continue
        if row["tenant_id"] is not None:
            if row["is_active"]:
                by_key[key] = row
        elif key not in by_key:
            by_key[key] = row

    items = sorted(by_key.values(), key=lambda x: x["display_order"])

    # ── Filter by role ──
    items = [i for i in items if _role_satisfies(user_role, i.get("required_role"))]

    # ── External roles: permission-gated nav (default-deny) ──
    if user_role in _EXTERNAL_ROLES:
        _granted = await _granted_modules(tenant_id, user_role)
        items = [i for i in items if _nav_module(i) in _granted]

    # ── Filter by context scope ──
    if context:
        context_type = context.get("type")
        items = [
            i for i in items
            if not i.get("context_scope") or i["context_scope"] == context_type or context_type is None
        ]

    # ── Group by section ──
    main_items = []
    bottom_items = []
    divider_positions = set()

    for item in items:
        section = item.get("section") or "main"
        if section == "divider":
            divider_positions.add(item["display_order"])
        elif section == "bottom":
            bottom_items.append(item)
        else:
            main_items.append(item)

    result = {
        "main": main_items,
        "bottom": bottom_items,
        "divider_positions": sorted(divider_positions),
    }

    await _cache_set(cache_key, result, ttl=300)
    return result


async def _cache_get(key: str):
    """Try Redis cache. Returns None on miss or error."""
    try:
        import redis.asyncio as aioredis
        import os
        url = os.environ.get("REDIS_URL", "")
        if not url:
            return None
        r = aioredis.from_url(url, decode_responses=True)
        val = await r.get(key)
        await r.aclose()
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


async def _cache_set(key: str, value: dict, ttl: int = 300):
    """Write to Redis cache. Silent on error."""
    try:
        import redis.asyncio as aioredis
        import os
        url = os.environ.get("REDIS_URL", "")
        if not url:
            return
        r = aioredis.from_url(url, decode_responses=True)
        await r.setex(key, ttl, json.dumps(value, default=str))
        await r.aclose()
    except Exception:
        pass


async def invalidate_nav_cache(tenant_id: str):
    """Flush all nav_rail:* keys for a tenant. Call after admin edits nav items."""
    try:
        import redis.asyncio as aioredis
        import os
        url = os.environ.get("REDIS_URL", "")
        if not url:
            return
        r = aioredis.from_url(url, decode_responses=True)
        keys = []
        async for key in r.scan_iter(f"nav_rail:{tenant_id}:*"):
            keys.append(key)
        if keys:
            await r.delete(*keys)
        await r.aclose()
    except Exception:
        pass
