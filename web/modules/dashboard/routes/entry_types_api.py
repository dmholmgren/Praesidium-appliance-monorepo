"""
Entry Types API — shared classification registry for tasks / projects / deadlines /
calendar_events.

GET /api/v1/entry-types?entity_kind={task|project|deadline|calendar_event}
    Omit entity_kind to get every kind grouped: {"types": {kind: [...]}, ...}

Resolution precedence (same pattern as nav_service.py / nav_tabs_api.py):
  1. Tenant-custom types (tenant_id matches, TRIM both sides) override platform defaults
  2. Platform-default types (tenant_id IS NULL) inherited by all tenants
  3. is_active filter; ordered by display_order

Cached in Redis for 5 minutes per (tenant, entity_kind).

The value of an entity's existing *_type string column IS the registry `code`
(v1 keying decision — no FK normalization yet).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger("praesidium.entry_types_api")

router = APIRouter(prefix="/api/v1/entry-types", tags=["entry-types"])

_VALID_KINDS = {"task", "project", "deadline", "calendar_event"}


def _serialize(row: dict) -> dict:
    return {
        "entity_kind": row["entity_kind"],
        "code": row["code"],
        "display_name": row["display_name"],
        "display_order": row["display_order"],
        "color": row.get("color"),
        "icon": row.get("icon"),
        "parent_code": row.get("parent_code"),
        "matter_types": row.get("matter_types"),
    }


async def _resolve(tenant_id: str, entity_kind: Optional[str]) -> dict:
    """Return {kind: [types...]} merging platform defaults with tenant overrides."""
    cache_key = f"entry_types:{tenant_id}:{entity_kind or 'all'}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        return cached

    where_kind = "AND entity_kind = :kind" if entity_kind else ""
    async with AsyncSessionLocal() as session:
        result = await session.execute(sa_text(f"""
            SELECT entity_kind, code, display_name, display_order,
                   color, icon, parent_code, matter_types, tenant_id
            FROM entry_types
            WHERE is_active = TRUE
              {where_kind}
              AND ((tenant_id IS NOT NULL AND trim(tenant_id) = trim(:tid)) OR tenant_id IS NULL)
            ORDER BY display_order ASC
        """), {"tid": tenant_id, "kind": entity_kind or ""})
        rows = [dict(r) for r in result.mappings().fetchall()]

    # Tenant override (tenant_id set) replaces platform default (tenant_id NULL) for same (kind, code).
    by_key: dict = {}
    for row in rows:
        key = (row["entity_kind"], row["code"])
        if row["tenant_id"] is not None:
            by_key[key] = row
        elif key not in by_key:
            by_key[key] = row

    grouped: dict = {}
    for row in sorted(by_key.values(), key=lambda x: (x["entity_kind"], x["display_order"])):
        grouped.setdefault(row["entity_kind"], []).append(_serialize(row))

    data = {"types": grouped}
    await _cache_set(cache_key, data, ttl=300)
    return data


@router.get("")
async def list_entry_types(request: Request, entity_kind: Optional[str] = None):
    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    if entity_kind and entity_kind not in _VALID_KINDS:
        return {"error": f"invalid entity_kind '{entity_kind}'", "valid": sorted(_VALID_KINDS)}

    data = await _resolve(tenant_id, entity_kind)
    if entity_kind:
        # flat shape when a single kind is requested — convenient for the chooser
        return {"entity_kind": entity_kind, "types": data["types"].get(entity_kind, [])}
    return data


async def _cache_get(key):
    try:
        import redis.asyncio as aioredis
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


async def _cache_set(key, value, ttl=300):
    try:
        import redis.asyncio as aioredis
        url = os.environ.get("REDIS_URL", "")
        if not url:
            return
        r = aioredis.from_url(url, decode_responses=True)
        await r.setex(key, ttl, json.dumps(value, default=str))
        await r.aclose()
    except Exception:
        pass
