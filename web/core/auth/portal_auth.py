"""
Portal Auth — magic link redemption, portal sessions, password set.
Session 1 of client-access build (auth spine). v1 — 2026-06-12

Routes:
  GET  /auth/magic?token=...        — redeem magic link → token session → /portal/
  GET  /portal/                     — minimal landing stub (replaced by real UI in session 2)
  GET  /api/portal/me               — session info JSON
  POST /api/portal/set-password     — optional password for return visits

Portal users are marked by users.auth_provider = 'magic_link'.
Sessions are opaque tokens in the sessions table (never raw user ids).
"""
from __future__ import annotations
import logging, secrets, html
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(tags=["portal-auth"])

import os
SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "praesidium_session")
PORTAL_SESSION_DAYS = 7


def _ip(request: Request) -> str:
    return (request.headers.get("x-real-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else ""))[:50]


async def _log_activity(db, tenant_id: str, user_id: int, action: str, request: Request, details: dict | None = None):
    import json as _json
    await db.execute(sa_text(
        "INSERT INTO user_activity_log (tenant_id, user_id, action, details, ip_address, user_agent) "
        "VALUES (:tid, :uid, :act, CAST(:det AS jsonb), :ip, :ua)"),
        {"tid": tenant_id, "uid": user_id, "act": action,
         "det": _json.dumps(details or {}), "ip": _ip(request),
         "ua": (request.headers.get("user-agent") or "")[:500]})


async def _create_session(db, tenant_id: str, user_id: int) -> str:
    token = secrets.token_urlsafe(32)  # 43 chars, fits varchar(64)
    await db.execute(sa_text(
        "DELETE FROM sessions WHERE user_id = :uid AND expires_at < NOW()"), {"uid": user_id})
    await db.execute(sa_text(
        "INSERT INTO sessions (token, user_id, tenant_id, expires_at) "
        "VALUES (:tok, :uid, :tid, :exp)"),
        {"tok": token, "uid": user_id, "tid": tenant_id,
         "exp": datetime.now(timezone.utc) + timedelta(days=PORTAL_SESSION_DAYS)})
    return token


def _expired_page(msg: str = "This link is no longer valid.") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><title>Link Expired</title>
<style>body{{font-family:Georgia,serif;background:#0e1726;color:#e8e2d5;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}}
.card{{background:#16213a;border:1px solid #2a3a5c;border-radius:8px;padding:40px;
max-width:420px;text-align:center}}.gold{{color:#c9a55c}}</style></head><body>
<div class="card"><h2 class="gold">Praesidium</h2><p>{html.escape(msg)}</p>
<p style="color:#8a93a6;font-size:14px">Please contact your attorney for a new access link.</p>
</div></body></html>""", status_code=410)


@router.get("/auth/magic")
async def redeem_magic_link(request: Request):
    token = (request.query_params.get("token") or "").strip()
    if not token or len(token) > 128:
        return _expired_page()

    req_tid = (getattr(request.state, "tenant_id", "") or "").strip()

    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text("""
            SELECT ml.id AS link_id, ml.user_id, ml.expires_at, ml.used_at,
                   TRIM(ml.tenant_id) AS tenant_id,
                   u.is_active, u.full_name, TRIM(u.tenant_id) AS user_tenant_id
            FROM portal_magic_links ml
            JOIN users u ON u.id = ml.user_id
            WHERE ml.token = :tok
            LIMIT 1
        """), {"tok": token})
        ml = r.mappings().fetchone()

        if not ml:
            return _expired_page()
        if ml["used_at"] is not None:
            return _expired_page("This link has already been used.")
        exp = ml["expires_at"]
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            return _expired_page("This link has expired.")
        if not ml["is_active"]:
            return _expired_page("This account is not active.")
        # Must be redeemed on the portal domain that owns the link
        if req_tid and req_tid != ml["tenant_id"]:
            logger.warning(f"[portal] magic link tenant mismatch: req={req_tid} link={ml['tenant_id']}")
            return _expired_page()

        await db.execute(sa_text(
            "UPDATE portal_magic_links SET used_at = NOW(), used_ip = :ip WHERE id = :lid"),
            {"ip": _ip(request), "lid": ml["link_id"]})

        sess_token = await _create_session(db, ml["tenant_id"], ml["user_id"])
        await _log_activity(db, ml["tenant_id"], ml["user_id"], "portal_login_magic", request)
        await db.commit()

    logger.info(f"[portal] magic link redeemed: user={ml['user_id']} tenant={ml['tenant_id']}")
    resp = RedirectResponse(url="/portal/", status_code=302)
    resp.set_cookie(key=SESSION_COOKIE_NAME, value=sess_token, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax",
                    max_age=PORTAL_SESSION_DAYS * 86400)
    return resp


async def _portal_context(request: Request) -> dict | None:
    """Shared loader: portal user + client + granted matters + password state."""
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "auth_provider", "") != "magic_link":
        return None
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    async with AsyncSessionLocal() as db:
        r = await db.execute(sa_text(
            "SELECT password_hash, portal_access_client_id, email, full_name "
            "FROM users WHERE id = :uid"), {"uid": user.id})
        u = r.mappings().fetchone()
        password_set = bool(u and (u["password_hash"] or "").startswith("$2"))
        client_name = ""
        if u and u["portal_access_client_id"]:
            cr = await db.execute(sa_text(
                "SELECT client_name FROM clients WHERE id = :cid"),
                {"cid": u["portal_access_client_id"]})
            crow = cr.fetchone()
            client_name = crow.client_name if crow else ""
        mr = await db.execute(sa_text("""
            SELECT m.id, m.matter_name, m.matter_number
            FROM sub_tenant_matter_scope s
            JOIN matters m ON m.id = s.matter_id
            WHERE TRIM(s.tenant_id) = :tid AND s.revoked_at IS NULL
            ORDER BY m.matter_name
        """), {"tid": tid})
        matters = [{"id": str(row["id"]), "name": row["matter_name"],
                    "number": row["matter_number"]} for row in mr.mappings()]
    return {"user_id": user.id, "name": u["full_name"] if u else "",
            "email": u["email"] if u else "", "client_name": client_name,
            "password_set": password_set, "matters": matters, "tenant_id": tid}


@router.get("/api/portal/me")
async def portal_me(request: Request):
    ctx = await _portal_context(request)
    if not ctx:
        return JSONResponse({"error": "Not a portal session"}, status_code=403)
    return JSONResponse(ctx)


@router.post("/api/portal/set-password")
async def portal_set_password(request: Request):
    user = getattr(request.state, "current_user", None)
    if not user or getattr(user, "auth_provider", "") != "magic_link":
        return JSONResponse({"error": "Not a portal session"}, status_code=403)
    body = await request.json()
    pw = (body.get("password") or "")
    if len(pw) < 12:
        return JSONResponse({"error": "Password must be at least 12 characters"}, status_code=400)
    if len(pw) > 128:
        return JSONResponse({"error": "Password too long"}, status_code=400)
    import bcrypt
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    tid = (getattr(request.state, "tenant_id", "") or "").strip()
    async with AsyncSessionLocal() as db:
        await db.execute(sa_text(
            "UPDATE users SET password_hash = :pw, updated_at = NOW() WHERE id = :uid"),
            {"pw": pw_hash, "uid": user.id})
        await _log_activity(db, tid, user.id, "portal_password_set", request)
        await db.commit()
    logger.info(f"[portal] password set: user={user.id}")
    return JSONResponse({"status": "ok", "message": "Password set. You can now sign in with your email and password."})


@router.get("/portal/", response_class=HTMLResponse)
async def portal_home(request: Request):
    """Minimal landing stub — proves the auth spine. Replaced by real portal UI."""
    ctx = await _portal_context(request)
    if not ctx:
        return RedirectResponse(url="/login", status_code=302)
    matters_html = "".join(
        f"<li>{html.escape(m['name'] or '')} <span style='color:#8a93a6'>({html.escape(m['number'] or '')})</span></li>"
        for m in ctx["matters"]) or "<li style='color:#8a93a6'>No matters shared yet.</li>"
    pw_block = ("<p style='color:#7fb069'>✓ Password set — you can sign in at this address anytime.</p>"
                if ctx["password_set"] else """
      <form id="pwform" onsubmit="setpw(event)">
        <input type="password" id="pw" placeholder="Choose a password (12+ characters)"
               style="padding:8px;width:280px;border-radius:4px;border:1px solid #2a3a5c;background:#0e1726;color:#e8e2d5">
        <button type="submit" style="padding:8px 16px;background:#c9a55c;border:none;border-radius:4px;cursor:pointer">Set Password</button>
      </form>
      <p id="pwmsg" style="font-size:13px;color:#8a93a6">Optional — lets you return without a new link.</p>
      <script>
      async function setpw(e){e.preventDefault();
        const r=await fetch('/api/portal/set-password',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({password:document.getElementById('pw').value})});
        const j=await r.json();
        document.getElementById('pwmsg').textContent=j.message||j.error;
        if(r.ok){document.getElementById('pwform').style.display='none';}}
      </script>""")
    return HTMLResponse(f"""<!doctype html><html><head><title>Client Portal</title>
<style>body{{font-family:Georgia,serif;background:#0e1726;color:#e8e2d5;margin:0;padding:40px}}
.card{{background:#16213a;border:1px solid #2a3a5c;border-radius:8px;padding:32px;max-width:640px;margin:0 auto}}
h2{{color:#c9a55c;margin-top:0}}a{{color:#c9a55c}}</style></head><body>
<div class="card">
  <h2>Welcome, {html.escape(ctx['name'])}</h2>
  <p style="color:#8a93a6">{html.escape(ctx['client_name'])} — secure client portal</p>
  <h3>Your Matters</h3>
  <ul>{matters_html}</ul>
  <hr style="border-color:#2a3a5c">
  {pw_block}
  <p style="margin-top:24px"><a href="/auth/logout">Sign out</a></p>
</div></body></html>""")
