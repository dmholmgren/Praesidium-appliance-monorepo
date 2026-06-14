"""
Conferencing API — LiveKit access tokens for workspace video.

Routes:
  POST /api/conferencing/token   mint a LiveKit JWT for the current user
                                 body: {workspace_id|room, name?}
                                 returns: {token, url, room, identity, name}

LiveKit access tokens are plain HS256 JWTs carrying a `video` grant — no
LiveKit SDK required (PyJWT only). Browser connects to the SFU over the
same-origin nginx wss proxy (LIVEKIT_WS_PATH), so no mixed-content issues.

Dev API key pair mirrors /opt/praesidium-livekit/livekit.yaml `keys:`.
Override via LIVEKIT_API_KEY / LIVEKIT_API_SECRET env. ROTATE BEFORE
PRODUCTION (tracked on the pre-launch hardening punch list).

Patent Pending — D.M. Holmgren, Reg. No. 54,168
"""
import os
import time
import logging

import jwt
from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)

router = APIRouter(tags=["conferencing"])

# LiveKit dev key pair (mirror of livekit.yaml). Override via env; rotate for prod.
_LK_API_KEY = os.environ.get("LIVEKIT_API_KEY", "APIf1bb6baa6df7ca54c0fa3f0f")
_LK_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "SWU3ipLd043wrwqXtI7PvP3HlBGsrY0VRxQ6sw4ACrA")
# Same-origin path the browser uses; nginx proxies it (wss) to the SFU :7880.
_LK_WS_PATH = os.environ.get("LIVEKIT_WS_PATH", "/livekit")
_LK_TTL_SECONDS = int(os.environ.get("LIVEKIT_TOKEN_TTL", "21600"))  # 6h


def _conf_tid(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", None)
    if not tid:
        raise HTTPException(401, "No tenant context")
    return tid.strip()


def _conf_user(request: Request):
    return getattr(request.state, "current_user", None)


def _mint_livekit_token(identity: str, name: str, room: str) -> str:
    now = int(time.time())
    claims = {
        "iss": _LK_API_KEY,
        "sub": identity,
        "nbf": now - 5,
        "exp": now + _LK_TTL_SECONDS,
        "name": name,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    return jwt.encode(claims, _LK_API_SECRET, algorithm="HS256")


@router.post("/api/conferencing/token")
async def conferencing_token(request: Request):
    tenant_id = _conf_tid(request)
    user = _conf_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")

    try:
        body = await request.json()
    except Exception:
        body = {}

    workspace_id = body.get("workspace_id") or body.get("room")
    if not workspace_id:
        raise HTTPException(400, "workspace_id (or room) is required")

    uid = getattr(user, "id", None)
    if uid is None:
        raise HTTPException(401, "No user identity")

    # Tenant-namespaced room so the shared SFU can't collide across tenants.
    room = f"ws_{tenant_id}_{workspace_id}"
    identity = f"u{uid}"
    display = (
        body.get("name")
        or getattr(user, "full_name", None)
        or getattr(user, "email", None)
        or f"User {uid}"
    )

    token = _mint_livekit_token(identity, display, room)
    logger.info("livekit token issued tenant=%s room=%s identity=%s", tenant_id, room, identity)
    return {
        "token": token,
        "url": _LK_WS_PATH,
        "room": room,
        "identity": identity,
        "name": display,
    }
