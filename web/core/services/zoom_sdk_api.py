"""
Praesidium Zoom Meeting SDK API
- Server-side JWT signature generation for Meeting SDK
- SDK config endpoint (returns sdkKey, never secret)
- Reads credentials from credentials_vault via ConnectorService (Fernet-encrypted)
- Falls back to ZOOM_SDK_KEY / ZOOM_SDK_SECRET env vars if vault empty
"""

import time
import hmac
import hashlib
import base64
import json
import os
import logging
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from modules.connectors.service import ConnectorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/zoom", tags=["zoom"])

PROVIDER = "zoom_meeting_sdk"  # matches connector_registry.connector_type


async def _get_zoom_creds(tenant_id: str = None) -> tuple:
    """
    Get Zoom SDK key + secret.
    Priority: credentials_vault (per-tenant, encrypted) → env vars (fallback).
    """
    if tenant_id:
        tid = tenant_id.strip()
        sdk_key = await ConnectorService.get_credential(tid, PROVIDER, "sdk_key")
        sdk_secret = await ConnectorService.get_credential(tid, PROVIDER, "sdk_secret")
        if sdk_key and sdk_secret:
            return sdk_key, sdk_secret

    # Fallback to env
    return (
        os.environ.get("ZOOM_SDK_KEY", ""),
        os.environ.get("ZOOM_SDK_SECRET", ""),
    )


async def _get_zoom_config(tenant_id: str = None) -> dict:
    """Get non-secret config from tenant_connectors.config JSONB."""
    if not tenant_id:
        return {}
    connector = await ConnectorService.get_connector(tenant_id.strip(), PROVIDER)
    if connector and connector.get("config"):
        return connector["config"] if isinstance(connector["config"], dict) else {}
    return {}


def _generate_signature(sdk_key: str, sdk_secret: str, meeting_number: str, role: int) -> str:
    """
    Generate Meeting SDK JWT signature (HMAC-SHA256).
    """
    iat = int(time.time()) - 30
    exp = iat + (60 * 60 * 2)  # 2 hour expiry
    token_exp = exp

    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = base64.urlsafe_b64encode(
        json.dumps(header, separators=(',', ':')).encode()
    ).rstrip(b'=').decode()

    payload = {
        "sdkKey": sdk_key,
        "appKey": sdk_key,
        "mn": str(meeting_number),
        "role": role,
        "iat": iat,
        "exp": exp,
        "tokenExp": token_exp,
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).rstrip(b'=').decode()

    message = f"{header_b64}.{payload_b64}"
    sig = hmac.new(
        sdk_secret.encode(),
        message.encode(),
        hashlib.sha256
    ).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b'=').decode()

    return f"{header_b64}.{payload_b64}.{sig_b64}"


@router.get("/config")
async def get_zoom_config(request: Request):
    """Return SDK key and configuration (never the secret)."""
    tenant_id = getattr(request.state, "tenant_id", None)
    sdk_key, _ = await _get_zoom_creds(tenant_id)
    config = await _get_zoom_config(tenant_id)

    if not sdk_key:
        return JSONResponse({
            "configured": False,
            "message": "Zoom SDK credentials not configured. Go to Firm Settings → Connectors → Zoom Meeting SDK to enter your Client ID and Client Secret from the Zoom Marketplace."
        })

    return JSONResponse({
        "configured": True,
        "sdk_key": sdk_key,
        "default_display_name": config.get("default_display_name", ""),
        "default_role": config.get("default_role", "participant"),
        "auto_present_session": config.get("auto_present_session", "true") == "true",
        "features": {
            "component_view": True,
            "screen_share": True,
            "recording": False,
            "breakout_rooms": False,
        }
    })


@router.post("/signature")
async def generate_zoom_signature(request: Request):
    """
    Generate a Meeting SDK JWT signature for joining a meeting.

    Body: {
        "meetingNumber": "123456789",
        "role": 0
    }
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    sdk_key, sdk_secret = await _get_zoom_creds(tenant_id)

    if not sdk_key or not sdk_secret:
        return JSONResponse({
            "error": "Zoom SDK credentials not configured",
            "detail": "Go to Firm Settings → Connectors → Zoom Meeting SDK to configure."
        }, status_code=400)

    try:
        body = await request.json()
    except:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    meeting_number = str(body.get("meetingNumber", "")).strip()
    role = int(body.get("role", 0))

    if not meeting_number:
        return JSONResponse({"error": "meetingNumber is required"}, status_code=400)
    if role not in (0, 1):
        return JSONResponse({"error": "role must be 0 (participant) or 1 (host)"}, status_code=400)

    meeting_number = meeting_number.replace(" ", "").replace("-", "")

    signature = _generate_signature(sdk_key, sdk_secret, meeting_number, role)

    logger.info(f"[zoom/signature] Generated for meeting {meeting_number}, role={role}, tenant={tenant_id}")

    return JSONResponse({
        "signature": signature,
        "sdkKey": sdk_key,
        "meetingNumber": meeting_number,
        "role": role,
    })
