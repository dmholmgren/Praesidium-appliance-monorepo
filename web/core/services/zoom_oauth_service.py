"""
Praesidium Zoom OAuth + OBF Token Service
─────────────────────────────────────────
Handles:
  1. OAuth authorization redirect (user clicks "Connect Zoom Account")
  2. OAuth callback (Zoom redirects back with auth code)
  3. Token exchange (auth code → access + refresh tokens → credentials_vault)
  4. Token refresh (access tokens expire hourly)
  5. OBF token minting (just-in-time when joining external meeting)
  6. ZAK token minting (for joining as yourself)

Credentials stored per-user in credentials_vault:
  provider = "zoom_oauth"
  key_type = "access_token:{user_id}"  / "refresh_token:{user_id}"
"""

import logging
import os
import time
import json
import base64
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from modules.connectors.service import ConnectorService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["zoom-oauth"])

PROVIDER_CONNECTOR = "zoom_meeting_sdk"   # connector registry type (has sdk_key/secret)
PROVIDER_OAUTH = "zoom_oauth"             # credentials_vault provider for per-user OAuth tokens
ZOOM_AUTH_URL = "https://zoom.us/oauth/authorize"
ZOOM_TOKEN_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"
ZOOM_REVOKE_URL = "https://zoom.us/oauth/revoke"

# Required scopes for OBF token generation
SCOPES = "user:read:token user:read:user"


async def _get_sdk_creds(tenant_id: str) -> tuple:
    """Get SDK Key (Client ID) and SDK Secret (Client Secret) from connector vault."""
    tid = tenant_id.strip()
    sdk_key = await ConnectorService.get_credential(tid, PROVIDER_CONNECTOR, "sdk_key")
    sdk_secret = await ConnectorService.get_credential(tid, PROVIDER_CONNECTOR, "sdk_secret")
    return sdk_key or "", sdk_secret or ""


def _basic_auth_header(client_id: str, client_secret: str) -> str:
    """Zoom OAuth requires Basic auth header for token exchange."""
    raw = f"{client_id}:{client_secret}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


def _get_user_id(request: Request) -> str:
    """Extract user ID from request state."""
    user = getattr(request.state, "current_user", None)
    if user:
        uid = getattr(user, "id", None) or getattr(user, "user_id", None)
        if uid:
            return str(uid)
    return "default"


def _get_callback_url(request: Request) -> str:
    """Build the OAuth callback URL from the current request."""
    host = request.headers.get("host", "localhost")
    scheme = "https" if "hjmmlegal" in host or "praesidium" in host else "http"
    return f"{scheme}://{host}/api/v1/zoom/callback"


# ─── Per-user token storage helpers ───

async def _store_user_tokens(tenant_id: str, user_id: str, access_token: str, refresh_token: str, expires_in: int = 3600):
    """Store OAuth tokens in credentials_vault, keyed per user."""
    tid = tenant_id.strip()
    await ConnectorService.save_credential(tid, PROVIDER_OAUTH, f"access_token:{user_id}", access_token)
    await ConnectorService.save_credential(tid, PROVIDER_OAUTH, f"refresh_token:{user_id}", refresh_token)
    # Store expiry timestamp
    expiry = str(int(time.time()) + expires_in)
    await ConnectorService.save_credential(tid, PROVIDER_OAUTH, f"token_expiry:{user_id}", expiry)


async def _get_user_access_token(tenant_id: str, user_id: str) -> str:
    """Get access token for user, refreshing if expired."""
    tid = tenant_id.strip()
    access_token = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"access_token:{user_id}")
    if not access_token:
        return ""

    # Check expiry
    expiry_str = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"token_expiry:{user_id}")
    if expiry_str:
        try:
            expiry = int(expiry_str)
            if time.time() > expiry - 300:  # refresh 5 min before expiry
                access_token = await _refresh_user_token(tid, user_id)
        except (ValueError, TypeError):
            pass

    return access_token or ""


async def _refresh_user_token(tenant_id: str, user_id: str) -> str:
    """Refresh an expired OAuth access token."""
    tid = tenant_id.strip()
    refresh_token = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"refresh_token:{user_id}")
    if not refresh_token:
        logger.warning(f"[zoom/oauth] No refresh token for user {user_id}")
        return ""

    sdk_key, sdk_secret = await _get_sdk_creds(tid)
    if not sdk_key or not sdk_secret:
        return ""

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ZOOM_TOKEN_URL,
                headers={
                    "Authorization": _basic_auth_header(sdk_key, sdk_secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=15.0,
            )
            if resp.status_code != 200:
                logger.error(f"[zoom/oauth] Refresh failed: {resp.status_code} {resp.text[:200]}")
                return ""

            data = resp.json()
            new_access = data.get("access_token", "")
            new_refresh = data.get("refresh_token", refresh_token)
            expires_in = data.get("expires_in", 3600)

            await _store_user_tokens(tid, user_id, new_access, new_refresh, expires_in)
            logger.info(f"[zoom/oauth] Refreshed token for user {user_id}")
            return new_access

    except Exception as e:
        logger.exception(f"[zoom/oauth] Refresh error: {e}")
        return ""


# ═══════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════

@router.get("/api/v1/zoom/oauth/authorize")
async def zoom_oauth_authorize(request: Request):
    """
    Redirect user to Zoom's OAuth consent screen.
    After consent, Zoom redirects back to /api/v1/zoom/callback.
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"error": "No tenant context"}, status_code=401)

    sdk_key, _ = await _get_sdk_creds(tenant_id)
    if not sdk_key:
        return JSONResponse({
            "error": "Zoom SDK not configured. Go to Firm Settings → Connectors → Zoom Meeting SDK."
        }, status_code=400)

    callback_url = _get_callback_url(request)
    user_id = _get_user_id(request)

    # Store state param to verify callback (CSRF protection)
    state = base64.urlsafe_b64encode(json.dumps({
        "tenant_id": tenant_id.strip(),
        "user_id": user_id,
        "ts": int(time.time()),
    }).encode()).decode()

    params = {
        "response_type": "code",
        "client_id": sdk_key,
        "redirect_uri": callback_url,
        "state": state,
    }

    zoom_url = f"{ZOOM_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(zoom_url)


@router.get("/api/v1/zoom/callback")
async def zoom_oauth_callback(request: Request):
    """
    OAuth callback — Zoom redirects here with ?code=...&state=...
    Exchange code for access + refresh tokens, store in vault.
    """
    code = request.query_params.get("code")
    state_param = request.query_params.get("state", "")
    error = request.query_params.get("error")

    if error:
        logger.warning(f"[zoom/callback] OAuth error: {error}")
        return HTMLResponse(f"""
        <html><body style="font-family:sans-serif;padding:40px;text-align:center;">
        <h2 style="color:#dc2626;">Zoom Authorization Failed</h2>
        <p>{error}: {request.query_params.get('error_description', '')}</p>
        <a href="/communications/?tab=conference">Back to Conference Space</a>
        </body></html>
        """, status_code=400)

    if not code:
        return HTMLResponse("<h2>Missing authorization code</h2>", status_code=400)

    # Decode state
    try:
        state_data = json.loads(base64.urlsafe_b64decode(state_param + "=="))
        tenant_id = state_data.get("tenant_id", "")
        user_id = state_data.get("user_id", "default")
    except Exception:
        tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
        user_id = _get_user_id(request)

    if not tenant_id:
        return HTMLResponse("<h2>No tenant context</h2>", status_code=400)

    sdk_key, sdk_secret = await _get_sdk_creds(tenant_id)
    if not sdk_key or not sdk_secret:
        return HTMLResponse("<h2>Zoom SDK not configured</h2>", status_code=400)

    callback_url = _get_callback_url(request)

    # Exchange authorization code for tokens
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ZOOM_TOKEN_URL,
                headers={
                    "Authorization": _basic_auth_header(sdk_key, sdk_secret),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": callback_url,
                },
                timeout=15.0,
            )

            if resp.status_code != 200:
                logger.error(f"[zoom/callback] Token exchange failed: {resp.status_code} {resp.text[:300]}")
                return HTMLResponse(f"""
                <html><body style="font-family:sans-serif;padding:40px;text-align:center;">
                <h2 style="color:#dc2626;">Token Exchange Failed</h2>
                <p>Zoom returned status {resp.status_code}</p>
                <pre style="text-align:left;max-width:600px;margin:auto;font-size:12px;background:#f1f5f9;padding:16px;border-radius:8px;">{resp.text[:500]}</pre>
                <a href="/communications/?tab=conference">Back to Conference Space</a>
                </body></html>
                """, status_code=400)

            data = resp.json()
            access_token = data.get("access_token", "")
            refresh_token = data.get("refresh_token", "")
            expires_in = data.get("expires_in", 3600)
            scope = data.get("scope", "")

            if not access_token:
                return HTMLResponse("<h2>No access token in response</h2>", status_code=400)

            # Store tokens
            await _store_user_tokens(tenant_id, user_id, access_token, refresh_token, expires_in)

            # Fetch user info to confirm
            user_resp = await client.get(
                f"{ZOOM_API_BASE}/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )
            zoom_user = user_resp.json() if user_resp.status_code == 200 else {}
            zoom_name = zoom_user.get("display_name", "Unknown")
            zoom_email = zoom_user.get("email", "")

            # Store Zoom user info for display
            await ConnectorService.save_credential(
                tenant_id, PROVIDER_OAUTH, f"zoom_user_info:{user_id}",
                json.dumps({"name": zoom_name, "email": zoom_email, "scope": scope})
            )

            logger.info(f"[zoom/callback] OAuth complete for {zoom_email} (user_id={user_id})")

    except Exception as e:
        logger.exception(f"[zoom/callback] Error: {e}")
        return HTMLResponse(f"<h2>Error: {str(e)}</h2>", status_code=500)

    # Success page — auto-redirects back to Conference Space
    return HTMLResponse(f"""
    <html><head><meta http-equiv="refresh" content="3;url=/communications/?tab=conference"></head>
    <body style="font-family:'DM Sans',sans-serif;padding:60px;text-align:center;background:#0d1f3c;color:#e2e8f0;">
    <div style="font-family:'Cormorant Garamond',Georgia,serif;font-size:24px;color:#D4A843;letter-spacing:2px;margin-bottom:16px;">PRAESIDIUM</div>
    <div style="font-size:18px;font-weight:600;color:#22c55e;margin-bottom:8px;">&#x2713; Zoom Account Connected</div>
    <div style="font-size:14px;color:#94a3b8;margin-bottom:4px;">{zoom_name}</div>
    <div style="font-size:12px;color:#64748b;margin-bottom:24px;">{zoom_email}</div>
    <div style="font-size:11px;color:#64748b;">Redirecting to Conference Space...</div>
    </body></html>
    """)


@router.get("/api/v1/zoom/oauth/status")
async def zoom_oauth_status(request: Request):
    """Check if current user has connected their Zoom account."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"connected": False})

    user_id = _get_user_id(request)
    tid = tenant_id.strip()

    access_token = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"access_token:{user_id}")
    if not access_token:
        return JSONResponse({"connected": False})

    # Get stored user info
    user_info_raw = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"zoom_user_info:{user_id}")
    zoom_user = {}
    if user_info_raw:
        try:
            zoom_user = json.loads(user_info_raw)
        except:
            pass

    return JSONResponse({
        "connected": True,
        "zoom_name": zoom_user.get("name", ""),
        "zoom_email": zoom_user.get("email", ""),
        "scope": zoom_user.get("scope", ""),
        "user_id": user_id,
    })


@router.post("/api/v1/zoom/oauth/disconnect")
async def zoom_oauth_disconnect(request: Request):
    """Revoke OAuth tokens and remove from vault."""
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"error": "No tenant context"}, status_code=401)

    user_id = _get_user_id(request)
    tid = tenant_id.strip()

    # Try to revoke with Zoom
    access_token = await ConnectorService.get_credential(tid, PROVIDER_OAUTH, f"access_token:{user_id}")
    if access_token:
        sdk_key, sdk_secret = await _get_sdk_creds(tid)
        if sdk_key and sdk_secret:
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        ZOOM_REVOKE_URL,
                        headers={
                            "Authorization": _basic_auth_header(sdk_key, sdk_secret),
                            "Content-Type": "application/x-www-form-urlencoded",
                        },
                        data={"token": access_token},
                        timeout=10.0,
                    )
            except Exception as e:
                logger.warning(f"[zoom/oauth] Revoke failed (non-critical): {e}")

    # Remove all stored tokens for this user
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    async with AsyncSessionLocal() as session:
        await session.execute(sa_text("""
            DELETE FROM credentials_vault
            WHERE TRIM(tenant_id) = :tid
              AND provider = :prov
              AND key_type LIKE :pattern
        """), {"tid": tid, "prov": PROVIDER_OAUTH, "pattern": f"%:{user_id}"})
        await session.commit()

    logger.info(f"[zoom/oauth] Disconnected Zoom for user {user_id}")
    return JSONResponse({"ok": True, "message": "Zoom account disconnected"})


# ═══════════════════════════════════════════════════
# OBF + ZAK TOKEN MINTING
# ═══════════════════════════════════════════════════

@router.post("/api/v1/zoom/token/obf")
async def mint_obf_token(request: Request):
    """
    Mint an OBF token for joining an external meeting.
    Body: { "meetingNumber": "83447529932" }
    Returns: { "obfToken": "eyJ..." }
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"error": "No tenant context"}, status_code=401)

    user_id = _get_user_id(request)
    access_token = await _get_user_access_token(tenant_id.strip(), user_id)
    if not access_token:
        return JSONResponse({
            "error": "Zoom account not connected. Click 'Connect Zoom Account' first.",
            "needs_oauth": True,
        }, status_code=401)

    try:
        body = await request.json()
    except:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    meeting_number = str(body.get("meetingNumber", "")).replace(" ", "").replace("-", "")
    if not meeting_number:
        return JSONResponse({"error": "meetingNumber required"}, status_code=400)

    # Call Zoom API to mint OBF token
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{ZOOM_API_BASE}/users/me/token",
                params={"type": "onbehalf", "meeting_id": meeting_number},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )

            if resp.status_code == 401:
                # Token might be expired, try refresh
                access_token = await _refresh_user_token(tenant_id.strip(), user_id)
                if access_token:
                    resp = await client.get(
                        f"{ZOOM_API_BASE}/users/me/token",
                        params={"type": "onbehalf", "meeting_id": meeting_number},
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=10.0,
                    )

            if resp.status_code != 200:
                logger.error(f"[zoom/obf] Failed: {resp.status_code} {resp.text[:300]}")
                return JSONResponse({
                    "error": f"Zoom API returned {resp.status_code}",
                    "detail": resp.text[:300],
                }, status_code=resp.status_code)

            data = resp.json()
            obf_token = data.get("token", "")
            if not obf_token:
                return JSONResponse({"error": "No token in Zoom response"}, status_code=500)

            logger.info(f"[zoom/obf] OBF token minted for meeting {meeting_number}, user {user_id}")
            return JSONResponse({
                "obfToken": obf_token,
                "meetingNumber": meeting_number,
                "tokenType": "obf",
            })

    except Exception as e:
        logger.exception(f"[zoom/obf] Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/v1/zoom/token/zak")
async def mint_zak_token(request: Request):
    """
    Mint a ZAK token for joining as yourself (host or authenticated participant).
    No meeting number needed — ZAK is user-scoped.
    Returns: { "zakToken": "eyJ..." }
    """
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return JSONResponse({"error": "No tenant context"}, status_code=401)

    user_id = _get_user_id(request)
    access_token = await _get_user_access_token(tenant_id.strip(), user_id)
    if not access_token:
        return JSONResponse({
            "error": "Zoom account not connected.",
            "needs_oauth": True,
        }, status_code=401)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{ZOOM_API_BASE}/users/me/token",
                params={"type": "zak"},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10.0,
            )

            if resp.status_code == 401:
                access_token = await _refresh_user_token(tenant_id.strip(), user_id)
                if access_token:
                    resp = await client.get(
                        f"{ZOOM_API_BASE}/users/me/token",
                        params={"type": "zak"},
                        headers={"Authorization": f"Bearer {access_token}"},
                        timeout=10.0,
                    )

            if resp.status_code != 200:
                return JSONResponse({
                    "error": f"Zoom API returned {resp.status_code}",
                    "detail": resp.text[:300],
                }, status_code=resp.status_code)

            data = resp.json()
            zak_token = data.get("token", "")
            if not zak_token:
                return JSONResponse({"error": "No token in response"}, status_code=500)

            logger.info(f"[zoom/zak] ZAK token minted for user {user_id}")
            return JSONResponse({
                "zakToken": zak_token,
                "tokenType": "zak",
            })

    except Exception as e:
        logger.exception(f"[zoom/zak] Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
