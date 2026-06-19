"""
Co-Counsel Access Provisioning API — v1 (2026-06-14).

Matter-scoped external access for outside attorneys (co-counsel). Mirrors the
client-portal provisioning model, but:
  * grants are PER-MATTER + PER-USER via external_user_scopes(scope_type='matter')
  * co-counsel get FULL matter visibility (no folder/work-product exclusion)
  * users live in the dedicated co_counsel sub-tenant (cocounsel.<firm-domain>)

Reuses the shared magic-link auth spine (core/auth/portal_auth.py): co-counsel
users are auth_provider='magic_link', role='co_counsel', confined to /portal/.

Endpoints (prefix /api/v1/matters/{matter_id}/co-counsel):
  GET    ""                      -> status: portal, granted users, contact suggestions, log
  POST   /provision             -> create/reuse co-counsel user + grant this matter + magic link
  POST   /magic-link/{user_id}  -> regenerate magic link
  POST   /user/{user_id}/revoke -> revoke this matter's grant for the user
  POST   /user/{user_id}/reinstate
  GET    /access-log
"""
from __future__ import annotations
import logging, secrets, hashlib
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text
from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/matters/{matter_id}/co-counsel", tags=["co-counsel"])

LINK_TTL_HOURS = 24 * 365


def _tid(r): return (getattr(r.state, "tenant_id", "") or "").strip()
def _uid(r): return getattr(getattr(r.state, "current_user", None), "id", None)


async def _portal(parent_tid, db):
    """The firm's dedicated co-counsel sub-tenant, or None if not provisioned."""
    r = await db.execute(sa_text(
        "SELECT TRIM(id) AS id, domain FROM tenants "
        "WHERE TRIM(parent_tenant_id) = :p AND sub_tenant_type = 'co_counsel' "
        "AND is_active = true LIMIT 1"), {"p": parent_tid})
    return r.mappings().fetchone()


async def _matter(tid, matter_id, db):
    r = await db.execute(sa_text(
        "SELECT id::text AS id, matter_name, matter_number, status "
        "FROM matters WHERE id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid"),
        {"mid": matter_id, "tid": tid})
    return r.mappings().fetchone()


async def _reconcile_cocounsel(db, ptid, actor_uid=None):
    """Keep matter_shares + the Chinese wall consistent for the co-counsel tenant.

    * Share (project) every matter with >= 1 ACTIVE co-counsel grant into the
      co-counsel tenant; unshare the rest. The cocounsel schema views read
      matter_shares, so this is what makes the matter visible in the real app.
    * Wall each active co-counsel user off every shared matter they were NOT
      granted, so one co-counsel tenant safely holds many matters/users
      (per-user matter scoping via the existing Chinese wall).
    """
    gm = await db.execute(sa_text(
        "SELECT DISTINCT CAST(scope_id AS uuid) AS mid FROM external_user_scopes "
        "WHERE TRIM(tenant_id) = :ptid AND scope_type = 'matter' AND is_active = true"),
        {"ptid": ptid})
    granted = [r.mid for r in gm.fetchall()]

    # shares: deactivate all, then (re)activate the granted set
    await db.execute(sa_text(
        "UPDATE matter_shares SET is_active = false WHERE TRIM(tenant_id) = :ptid"),
        {"ptid": ptid})
    for mid in granted:
        await db.execute(sa_text(
            "INSERT INTO matter_shares (matter_id, tenant_id, shared_by, is_active) "
            "VALUES (CAST(:m AS uuid), :ptid, :by, true) "
            "ON CONFLICT (matter_id, tenant_id) DO UPDATE SET is_active = true, shared_by = :by"),
            {"m": str(mid), "ptid": ptid, "by": actor_uid})

    # wall: per active co-counsel user, wall off matters they were not granted
    us = await db.execute(sa_text(
        "SELECT id FROM users WHERE TRIM(tenant_id) = :ptid "
        "AND role = 'co_counsel' AND is_active = true"), {"ptid": ptid})
    for u in us.fetchall():
        uid = u.id
        um = await db.execute(sa_text(
            "SELECT CAST(scope_id AS uuid) AS mid FROM external_user_scopes "
            "WHERE TRIM(tenant_id) = :ptid AND user_id = :uid "
            "AND scope_type = 'matter' AND is_active = true"), {"ptid": ptid, "uid": uid})
        u_matters = {r.mid for r in um.fetchall()}
        for mid in granted:
            if mid in u_matters:
                await db.execute(sa_text(
                    "UPDATE chinese_wall_exclusions SET is_active = false "
                    "WHERE TRIM(tenant_id) = :ptid AND user_id = :uid AND matter_id = CAST(:m AS uuid)"),
                    {"ptid": ptid, "uid": uid, "m": str(mid)})
            else:
                await db.execute(sa_text(
                    "INSERT INTO chinese_wall_exclusions "
                    "(tenant_id, user_id, matter_id, reason, created_by, created_at, is_active) "
                    "VALUES (:ptid, :uid, CAST(:m AS uuid), :reason, :by, NOW(), true) "
                    "ON CONFLICT (tenant_id, user_id, matter_id) "
                    "DO UPDATE SET is_active = true"),
                    {"ptid": ptid, "uid": uid, "m": str(mid),
                     "reason": "co-counsel scope: matter not granted to this user",
                     "by": actor_uid})


@router.get("")
async def status(request: Request, matter_id: str):
    tid = _tid(request)
    async with AsyncSessionLocal() as db:
        matter = await _matter(tid, matter_id, db)
        if not matter:
            return JSONResponse({"error": "Matter not found"}, status_code=404)
        portal = await _portal(tid, db)
        if not portal:
            return JSONResponse({"has_portal": False, "matter": dict(matter),
                                 "error": "No co-counsel portal configured for this firm"})
        ptid = portal["id"]

        # Co-counsel users granted THIS matter
        gu = await db.execute(sa_text("""
            SELECT u.id, u.email, u.full_name, u.is_active, u.last_login,
                   s.id AS scope_id, s.is_active AS scope_active,
                   s.access_level, s.expires_at, s.granted_by_id
            FROM external_user_scopes s
            JOIN users u ON u.id = s.user_id
            WHERE TRIM(s.tenant_id) = :ptid
              AND s.scope_type = 'matter'
              AND s.scope_id = :mid
            ORDER BY u.full_name
        """), {"ptid": ptid, "mid": matter_id})
        users = []
        for r in gu.mappings():
            ml = await db.execute(sa_text(
                "SELECT token, expires_at FROM portal_magic_links "
                "WHERE user_id = :uid AND used_at IS NULL AND expires_at > NOW() "
                "ORDER BY created_at DESC LIMIT 1"), {"uid": r["id"]})
            ml_row = ml.mappings().fetchone()
            la = await db.execute(sa_text(
                "SELECT created_at, ip_address, action FROM user_activity_log "
                "WHERE user_id = :uid ORDER BY created_at DESC LIMIT 1"), {"uid": r["id"]})
            la_row = la.mappings().fetchone()
            users.append({
                "id": r["id"], "email": r["email"], "full_name": r["full_name"],
                "is_active": bool(r["is_active"] and r["scope_active"]),
                "user_active": bool(r["is_active"]),
                "matter_active": bool(r["scope_active"]),
                "scope_id": r["scope_id"], "access_level": r["access_level"],
                "last_login": str(r["last_login"])[:10] if r["last_login"] else "Never",
                "pending_link": {"token": r["scope_id"] and (ml_row["token"][:8] + "..."),
                                 "expires": ml_row["expires_at"].isoformat()} if ml_row else None,
                "last_access": {"time": str(la_row["created_at"])[:16], "ip": la_row["ip_address"],
                                "action": la_row["action"]} if la_row else None,
            })

        # Contact suggestions: this matter's contacts tagged co-counsel, with email
        cs = await db.execute(sa_text("""
            SELECT DISTINCT c.id, c.full_name, c.email, c.firm_name, c.company
            FROM matter_contacts mc
            JOIN contacts c ON c.id = mc.contact_id AND TRIM(c.tenant_id) = :tid
            WHERE mc.matter_id = CAST(:mid AS uuid)
              AND (mc.role_code = 'co_counsel' OR mc.secondary_role_code = 'co_counsel'
                   OR mc.role ILIKE '%co-counsel%' OR mc.role ILIKE '%co_counsel%')
              AND c.email IS NOT NULL AND c.email != ''
            ORDER BY c.full_name
        """), {"tid": tid, "mid": matter_id})
        contacts = [{"id": c["id"], "full_name": c["full_name"], "email": c["email"],
                     "firm": c["firm_name"] or c["company"]} for c in cs.mappings()]

        # Access log tail for these users
        uids = [u["id"] for u in users]
        log_entries = []
        if uids:
            lg = await db.execute(sa_text(
                "SELECT al.action, al.ip_address, al.created_at, u.full_name "
                "FROM user_activity_log al JOIN users u ON u.id = al.user_id "
                "WHERE al.user_id = ANY(:uids) ORDER BY al.created_at DESC LIMIT 10"),
                {"uids": uids})
            log_entries = [{"action": l["action"], "ip": l["ip_address"],
                            "time": l["created_at"].isoformat() if l["created_at"] else "",
                            "name": l["full_name"]} for l in lg.mappings()]

        return JSONResponse({
            "has_portal": True, "portal_domain": portal["domain"], "portal_tenant_id": ptid,
            "matter": dict(matter), "users": users, "contacts": contacts,
            "access_log_tail": log_entries})


async def _new_magic_link(db, ptid, user_id, created_by):
    await db.execute(sa_text(
        "UPDATE portal_magic_links SET expires_at = NOW() "
        "WHERE user_id = :uid AND used_at IS NULL AND expires_at > NOW()"), {"uid": user_id})
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(hours=LINK_TTL_HOURS)
    await db.execute(sa_text(
        "INSERT INTO portal_magic_links (tenant_id, user_id, token, expires_at, created_by) "
        "VALUES (:ptid, :uid, :tok, :exp, :gby)"),
        {"ptid": ptid, "uid": user_id, "tok": token, "exp": expires, "gby": created_by})
    return token, expires


async def _send_invite_email(tid, portal_domain, to_email, to_name,
                             matter_label, sender, token, expires):
    """Send the co-counsel invite email via the firm provider (Stalwart per
    _detect_provider ordering). Non-fatal: returns (email_sent, email_error)."""
    magic_url = f"https://{portal_domain}/auth/magic?token={token}"
    from_email = (sender and sender["email"]) or "dennis@hjmmlegal.com"
    from_name = (sender and sender["full_name"]) or "HJMM Legal"
    try:
        from core.services.email_send_connector import send_email
        subject = f"Secure access to {matter_label} \u2014 HJMM Legal"
        body_html = (
            "<div style=\"font-family:Georgia,serif;color:#1a1a1a;line-height:1.6\">"
            f"<p>Dear {to_name or 'Counsel'},</p>"
            f"<p>{from_name} at HJMM Legal has granted you secure access to "
            f"<strong>{matter_label}</strong> through the firm's co-counsel portal.</p>"
            f"<p style=\"margin:28px 0\"><a href=\"{magic_url}\" "
            "style=\"background:#0D1F3C;color:#ffffff;padding:12px 22px;border-radius:6px;"
            "text-decoration:none;display:inline-block\">Open the secure portal</a></p>"
            f"<p style=\"color:#555;font-size:13px\">This link is single-use and expires on "
            f"{expires.strftime('%B %d, %Y')}. You can set a password inside the portal for "
            f"return visits. If the link has expired, ask {from_name} to resend it.</p>"
            "<p style=\"color:#999;font-size:12px\">If you did not expect this, you can ignore "
            "this message.</p></div>")
        body_text = (
            f"Dear {to_name or 'Counsel'},\n\n"
            f"{from_name} at HJMM Legal has granted you secure access to {matter_label} "
            "through the firm's co-counsel portal.\n\n"
            f"Open the secure portal:\n{magic_url}\n\n"
            f"This link is single-use and expires on {expires.strftime('%B %d, %Y')}.\n"
            "You can set a password inside the portal for return visits.\n")
        result = await send_email(
            tenant_id=tid, from_email=from_email, to=to_email,
            subject=subject, body_html=body_html, body_text=body_text)
        if result and result.get("status") in ("ok", "sent", "queued", "success"):
            return True, None
        return False, (result or {}).get("error") or "send returned no success status"
    except Exception as e:
        import logging
        err = str(e)[:300]
        logging.getLogger("praesidium.co_counsel").warning(
            "[co_counsel] invite email to %s failed: %s", to_email, err)
        return False, err


@router.post("/provision")
async def provision(request: Request, matter_id: str):
    tid = _tid(request); uid = _uid(request); body = await request.json()
    email = (body.get("email") or "").strip().lower()
    full_name = (body.get("full_name") or "").strip()
    if not email: return JSONResponse({"error": "Email required"}, status_code=400)
    if not full_name: return JSONResponse({"error": "Name required"}, status_code=400)
    async with AsyncSessionLocal() as db:
        matter = await _matter(tid, matter_id, db)
        if not matter: return JSONResponse({"error": "Matter not found"}, status_code=404)
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"error": "No co-counsel portal configured"}, status_code=400)
        ptid = portal["id"]

        # Reuse an existing co-counsel user in this sub-tenant by email, else create.
        ex = await db.execute(sa_text(
            "SELECT id FROM users WHERE TRIM(tenant_id) = :ptid AND lower(email) = :e LIMIT 1"),
            {"ptid": ptid, "e": email})
        row = ex.fetchone()
        if row:
            user_id = row.id
            await db.execute(sa_text(
                "UPDATE users SET is_active = true WHERE id = :uid"), {"uid": user_id})
        else:
            username = email.split("@")[0].replace(".", "_")[:50]
            pw_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()  # placeholder; set via portal
            await db.execute(sa_text(
                "INSERT INTO users (tenant_id, username, email, full_name, role, password_hash, "
                "is_active, auth_provider) VALUES "
                "(:ptid, :u, :e, :fn, CAST('co_counsel' AS user_role_enum), :pw, true, 'magic_link')"),
                {"ptid": ptid, "u": username, "e": email, "fn": full_name, "pw": pw_hash})
            nu = await db.execute(sa_text(
                "SELECT id FROM users WHERE TRIM(tenant_id) = :ptid AND lower(email) = :e "
                "ORDER BY id DESC LIMIT 1"), {"ptid": ptid, "e": email})
            user_id = nu.fetchone().id
            await db.execute(sa_text(
                "INSERT INTO user_tenant_memberships (canonical_email, tenant_id, user_id, "
                "role_in_tenant, is_primary, granted_by) VALUES "
                "(:e, :ptid, :uid, CAST('co_counsel' AS user_role_enum), false, :gby)"),
                {"e": email, "ptid": ptid, "uid": user_id, "gby": uid})

        # Grant THIS matter (per-user). Upsert: reactivate if a row exists, else insert.
        upd = await db.execute(sa_text(
            "UPDATE external_user_scopes SET is_active = true, expires_at = NULL "
            "WHERE TRIM(tenant_id) = :ptid AND user_id = :uid AND scope_type = 'matter' "
            "AND scope_id = :mid"), {"ptid": ptid, "uid": user_id, "mid": matter_id})
        if upd.rowcount == 0:
            await db.execute(sa_text(
                "INSERT INTO external_user_scopes (tenant_id, user_id, scope_type, scope_id, "
                "access_level, granted_by_id, is_active) VALUES "
                "(:ptid, :uid, 'matter', :mid, 'full', :gby, true)"),
                {"ptid": ptid, "uid": user_id, "mid": matter_id, "gby": uid})

        # Project the matter into the co-counsel tenant + reconcile the wall.
        await _reconcile_cocounsel(db, ptid, uid)

        token, expires = await _new_magic_link(db, ptid, user_id, uid)
        sr = await db.execute(sa_text(
            "SELECT email, full_name FROM users WHERE id = :uid"), {"uid": uid})
        sender = sr.mappings().fetchone()
        await db.commit()

    magic_url = f"https://{portal['domain']}/auth/magic?token={token}"
    matter_label = matter["matter_name"] or matter["matter_number"] or "a matter"
    email_sent, email_error = await _send_invite_email(
        tid, portal["domain"], email, full_name, matter_label, sender, token, expires)

    return JSONResponse({"status": "provisioned", "user_id": user_id, "email": email,
                         "magic_link": magic_url,
                         "expires_at": expires.isoformat(),
                         "email_sent": email_sent, "email_error": email_error})


@router.post("/magic-link/{user_id}")
async def magic_link(request: Request, matter_id: str, user_id: int):
    tid = _tid(request); admin_uid = _uid(request)
    async with AsyncSessionLocal() as db:
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"error": "No co-counsel portal"}, status_code=400)
        ptid = portal["id"]
        matter = await _matter(tid, matter_id, db)
        ru = await db.execute(sa_text(
            "SELECT email, full_name FROM users WHERE id = :uid"), {"uid": user_id})
        recipient = ru.mappings().fetchone()
        sr = await db.execute(sa_text(
            "SELECT email, full_name FROM users WHERE id = :uid"), {"uid": admin_uid})
        sender = sr.mappings().fetchone()
        token, expires = await _new_magic_link(db, ptid, user_id, admin_uid)
        await db.commit()

    if not recipient:
        return JSONResponse({"error": "User not found"}, status_code=404)
    matter_label = (matter and (matter["matter_name"] or matter["matter_number"])) or "a matter"
    email_sent, email_error = await _send_invite_email(
        tid, portal["domain"], recipient["email"], recipient["full_name"],
        matter_label, sender, token, expires)
    return JSONResponse({"magic_link": f"https://{portal['domain']}/auth/magic?token={token}",
                         "expires_at": expires.isoformat(),
                         "email_sent": email_sent, "email_error": email_error})


@router.post("/user/{user_id}/revoke")
async def revoke(request: Request, matter_id: str, user_id: int):
    tid = _tid(request); actor = _uid(request)
    async with AsyncSessionLocal() as db:
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"error": "No co-counsel portal"}, status_code=400)
        ptid = portal["id"]
        # Revoke just this matter's grant.
        await db.execute(sa_text(
            "UPDATE external_user_scopes SET is_active = false "
            "WHERE TRIM(tenant_id) = :ptid AND user_id = :uid AND scope_type = 'matter' "
            "AND scope_id = :mid"), {"ptid": ptid, "uid": user_id, "mid": matter_id})
        await db.execute(sa_text(
            "UPDATE portal_magic_links SET expires_at = NOW() "
            "WHERE user_id = :uid AND used_at IS NULL"), {"uid": user_id})
        # If no active matter grants remain, deactivate the user entirely.
        rem = await db.execute(sa_text(
            "SELECT COUNT(*) FROM external_user_scopes WHERE user_id = :uid "
            "AND scope_type = 'matter' AND is_active = true"), {"uid": user_id})
        if (rem.scalar() or 0) == 0:
            await db.execute(sa_text(
                "UPDATE users SET is_active = false WHERE id = :uid"), {"uid": user_id})
        await _reconcile_cocounsel(db, ptid, actor)
        await db.commit()
    return JSONResponse({"status": "revoked"})


@router.post("/user/{user_id}/reinstate")
async def reinstate(request: Request, matter_id: str, user_id: int):
    tid = _tid(request); actor = _uid(request)
    async with AsyncSessionLocal() as db:
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"error": "No co-counsel portal"}, status_code=400)
        ptid = portal["id"]
        await db.execute(sa_text(
            "UPDATE external_user_scopes SET is_active = true, expires_at = NULL "
            "WHERE TRIM(tenant_id) = :ptid AND user_id = :uid AND scope_type = 'matter' "
            "AND scope_id = :mid"), {"ptid": ptid, "uid": user_id, "mid": matter_id})
        await db.execute(sa_text(
            "UPDATE users SET is_active = true WHERE id = :uid"), {"uid": user_id})
        await _reconcile_cocounsel(db, ptid, actor)
        await db.commit()
    return JSONResponse({"status": "reinstated"})


@router.get("/access-log")
async def access_log(request: Request, matter_id: str):
    tid = _tid(request); limit = int(request.query_params.get("limit", 50))
    async with AsyncSessionLocal() as db:
        portal = await _portal(tid, db)
        if not portal: return JSONResponse({"logs": []})
        ptid = portal["id"]
        uids = await db.execute(sa_text("""
            SELECT DISTINCT s.user_id FROM external_user_scopes s
            WHERE TRIM(s.tenant_id) = :ptid AND s.scope_type = 'matter' AND s.scope_id = :mid
        """), {"ptid": ptid, "mid": matter_id})
        user_ids = [r.user_id for r in uids]
        if not user_ids: return JSONResponse({"logs": []})
        logs = await db.execute(sa_text(
            "SELECT al.action, al.details, al.ip_address, al.created_at, u.full_name, u.email "
            "FROM user_activity_log al JOIN users u ON u.id = al.user_id "
            "WHERE al.user_id = ANY(:uids) ORDER BY al.created_at DESC LIMIT :lim"),
            {"uids": user_ids, "lim": limit})
        return JSONResponse({"logs": [
            {"action": r["action"], "ip": r["ip_address"],
             "time": r["created_at"].isoformat() if r["created_at"] else "",
             "name": r["full_name"], "details": r["details"]} for r in logs.mappings()]})
