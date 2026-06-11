"""
Keymap API — GET/PUT/DELETE /api/v1/user/keymap
Three-layer resolution: platform defaults <- tenant overrides <- user overrides
"""

import json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db.base import AsyncSessionLocal


router = APIRouter(prefix="/api/v1/user", tags=["user"])


# -- Platform Default Keymaps --
PLATFORM_DEFAULT_KEYMAP = {
    "ediscovery_review": {
        "review.responsive": "1",
        "review.non_responsive": "2",
        "review.needs_further_review": "3",
        "review.hot": "4",
        "nav.skip": "5",
        "privilege.privileged": "p",
        "privilege.attorney_work_product": "w",
        "privilege.none": "u",
        "nav.next": "j",
        "nav.prev": "k",
        "tag.toggle.0": "6",
        "tag.toggle.1": "7",
        "tag.toggle.2": "8",
    },
    "global": {
        "search.global": "/",
        "go.dashboard": "g d",
        "go.matters": "g m",
    }
}


def _get_user(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

def _get_tenant(request: Request):
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(status_code=400, detail="Tenant context required")
    return tid.strip()


def _resolve_keymap(scope, platform, tenant_overrides, user_overrides):
    """Merge: platform <- tenant <- user. User wins."""
    base = dict(platform.get(scope, {}))
    for k, v in tenant_overrides.get(scope, {}).items():
        base[k] = v
    for k, v in user_overrides.get(scope, {}).items():
        base[k] = v
    return base


@router.get("/keymap")
async def get_keymap(request: Request):
    """
    Returns the resolved keymap for the authenticated user.
    Merges platform defaults <- tenant overrides <- user overrides.
    Also returns an inverted map (key -> actionId) for the hotkey hook.
    """
    user = _get_user(request)
    tenant_id = _get_tenant(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    tenant_overrides = {}
    user_overrides = {}

    async with AsyncSessionLocal() as session:
        # Tenant overrides — tenant_settings table not yet created.
        # Stub: returns empty. Uncomment when table exists.
        # trow = (await session.execute(text(
        #     "SELECT settings FROM tenant_settings WHERE TRIM(tenant_id) = :tid LIMIT 1"
        # ), {"tid": tenant_id})).mappings().first()
        # if trow and trow["settings"]:
        #     settings = trow["settings"] if isinstance(trow["settings"], dict) else json.loads(trow["settings"])
        #     tenant_overrides = settings.get("keymap", {})

        # User overrides from user_preferences
        urow = (await session.execute(text(
            "SELECT user_preferences FROM users WHERE id = :uid LIMIT 1"
        ), {"uid": user_id})).mappings().first()
        if urow and urow["user_preferences"]:
            prefs = urow["user_preferences"] if isinstance(urow["user_preferences"], dict) else json.loads(urow["user_preferences"])
            user_overrides = prefs.get("keymap", {})

    # Build resolved keymaps per scope
    result = {}
    all_scopes = set(list(PLATFORM_DEFAULT_KEYMAP.keys()) + list(tenant_overrides.keys()) + list(user_overrides.keys()))
    for scope in all_scopes:
        resolved = _resolve_keymap(scope, PLATFORM_DEFAULT_KEYMAP, tenant_overrides, user_overrides)
        # Build inverted: key -> actionId
        inverted = {}
        for action_id, key in resolved.items():
            inverted[key] = action_id
        result[scope] = {
            "actions": resolved,      # actionId -> key
            "bindings": inverted,      # key -> actionId
        }

    return {"keymap": result, "defaults": PLATFORM_DEFAULT_KEYMAP}


@router.put("/keymap")
async def update_keymap(request: Request):
    """
    Save user keymap overrides.
    Body: { "scope": "ediscovery_review", "bindings": { "review.responsive": "1", ... } }
    """
    user = _get_user(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    body = await request.json()
    scope = body.get("scope")
    bindings = body.get("bindings", {})

    if not scope:
        raise HTTPException(status_code=400, detail="scope is required")

    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT user_preferences FROM users WHERE id = :uid"
        ), {"uid": user_id})).mappings().first()

        prefs = {}
        if row and row["user_preferences"]:
            prefs = row["user_preferences"] if isinstance(row["user_preferences"], dict) else json.loads(row["user_preferences"])

        keymap = prefs.get("keymap", {})
        keymap[scope] = bindings
        prefs["keymap"] = keymap

        await session.execute(text(
            "UPDATE users SET user_preferences = CAST(:prefs AS jsonb) WHERE id = :uid"
        ), {"prefs": json.dumps(prefs), "uid": user_id})
        await session.commit()

    return {"ok": True, "scope": scope}


@router.delete("/keymap")
async def reset_keymap(request: Request):
    """Reset user keymap to platform defaults (removes all user overrides)."""
    user = _get_user(request)
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    async with AsyncSessionLocal() as session:
        row = (await session.execute(text(
            "SELECT user_preferences FROM users WHERE id = :uid"
        ), {"uid": user_id})).mappings().first()

        prefs = {}
        if row and row["user_preferences"]:
            prefs = row["user_preferences"] if isinstance(row["user_preferences"], dict) else json.loads(row["user_preferences"])

        prefs.pop("keymap", None)

        await session.execute(text(
            "UPDATE users SET user_preferences = CAST(:prefs AS jsonb) WHERE id = :uid"
        ), {"prefs": json.dumps(prefs), "uid": user_id})
        await session.commit()

    return {"ok": True}
