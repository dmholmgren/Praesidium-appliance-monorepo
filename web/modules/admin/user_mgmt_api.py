"""
modules/admin/user_mgmt_api.py
Module 9 Component 2 — User Management UI

Endpoints:
  GET  /admin/users                       — user list (tenant-scoped)
  GET  /admin/users/new                   — create user form
  POST /admin/users/new                   — create user
  GET  /admin/users/{user_id}             — edit user form
  POST /admin/users/{user_id}             — update user (role, status, theme)
  POST /admin/users/{user_id}/deactivate  — deactivate user
  POST /admin/users/{user_id}/reactivate  — reactivate user
  POST /admin/users/{user_id}/theme       — HTMX inline theme toggle

Patent Pending — 64/020,027 — Dennis M. Holmgren, Reg. No. 54,168
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import bcrypt
from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from core.db.base import AsyncSessionLocal
from core.services.stalwart_service import (
    provision_mail_account, disable_mail_account,
    enable_mail_account, update_mail_password,
)

log = logging.getLogger("praesidium.admin.user_mgmt")

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "../../templates/admin")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

ASSIGNABLE_ROLES = ["attorney", "paralegal", "staff", "admin", "read_only"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip(val: str | None) -> str:
    return (val or "").strip()


def _valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


async def _require_admin(request: Request) -> dict:
    """Return session dict or raise 401."""
    session_token = (
        request.cookies.get("session_token")
        or request.cookies.get("admin_session")
    )
    if not session_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("""
                SELECT s.user_id, s.tenant_id, s.expires_at, u.role
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token = :token AND s.expires_at > NOW()
            """),
            {"token": session_token},
        )
        session = row.fetchone()
    if not session:
        raise HTTPException(status_code=401, detail="Session expired")
    return {
        "user_id": session.user_id,
        "tenant_id": _strip(session.tenant_id),
        "role": session.role,
    }


async def _get_users(tenant_id: str, search: str = "") -> list:
    async with AsyncSessionLocal() as db:
        if search:
            rows = await db.execute(
                text("""
                    SELECT id, tenant_id, username, email, full_name,
                           role, is_active,
                           COALESCE(theme_preference, 'dark') AS theme_preference,
                           last_active_at, created_at,
                           ldap_dn IS NOT NULL AS is_ldap
                    FROM users
                    WHERE tenant_id = :tid
                      AND (
                          username ILIKE :q
                          OR email ILIKE :q
                          OR full_name ILIKE :q
                      )
                    ORDER BY is_active DESC, full_name ASC
                """),
                {"tid": tenant_id, "q": f"%{search}%"},
            )
        else:
            rows = await db.execute(
                text("""
                    SELECT id, tenant_id, username, email, full_name,
                           role, is_active,
                           COALESCE(theme_preference, 'dark') AS theme_preference,
                           last_active_at, created_at,
                           ldap_dn IS NOT NULL AS is_ldap
                    FROM users
                    WHERE tenant_id = :tid
                    ORDER BY is_active DESC, full_name ASC
                """),
                {"tid": tenant_id},
            )
        return rows.fetchall()


async def _get_user(user_id: int, tenant_id: str):
    async with AsyncSessionLocal() as db:
        row = await db.execute(
            text("""
                SELECT id, tenant_id, username, email, full_name,
                       role, is_active,
                       COALESCE(theme_preference, 'dark') AS theme_preference,
                       last_active_at, created_at,
                       ldap_dn IS NOT NULL AS is_ldap,
                       ldap_dn
                FROM users
                WHERE id = :uid AND tenant_id = :tid
            """),
            {"uid": user_id, "tid": tenant_id},
        )
        return row.fetchone()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def user_list(
    request: Request,
    tenant_id: Optional[str] = None,
    search: str = "",
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    users = await _get_users(tid, search)
    return templates.TemplateResponse(request, "user_list.html", {
        "page": "users",
        "users": users,
        "tenant_id": tid,
        "search": search,
        "roles": ASSIGNABLE_ROLES,
        "branding": getattr(request.state, "branding", None),
    })


@router.get("/new", response_class=HTMLResponse)
async def user_new_form(request: Request, tenant_id: Optional[str] = None):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    return templates.TemplateResponse(request, "user_edit.html", {
        "page": "users",
        "user": None,
        "tenant_id": tid,
        "roles": ASSIGNABLE_ROLES,
        "error": None,
        "form": {},
        "branding": getattr(request.state, "branding", None),
    })


@router.post("/new")
async def user_create(
    request: Request,
    tenant_id: str = Form(...),
    username: str = Form(...),
    email: str = Form(...),
    full_name: str = Form(""),
    role: str = Form("staff"),
    theme_preference: str = Form("dark"),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]

    def _err(msg: str):
        return templates.TemplateResponse(request, "user_edit.html", {
            "page": "users",
            "user": None,
            "tenant_id": tid,
            "roles": ASSIGNABLE_ROLES,
            "error": msg,
            "form": {
                "username": username,
                "email": email,
                "full_name": full_name,
                "role": role,
                "theme_preference": theme_preference,
            },
            "branding": getattr(request.state, "branding", None),
        })

    username = _strip(username)
    email = _strip(email)

    if not username:
        return _err("Username is required.")
    if not email or not _valid_email(email):
        return _err("Valid email is required.")
    if role not in ASSIGNABLE_ROLES:
        return _err(f"Invalid role: {role}")
    if theme_preference not in ("dark", "light"):
        theme_preference = "dark"
    if len(password) < 8:
        return _err("Password must be at least 8 characters.")
    if password != password_confirm:
        return _err("Passwords do not match.")

    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""
                    INSERT INTO users
                      (tenant_id, username, email, full_name, role,
                       is_active, theme_preference, password_hash,
                       created_at, updated_at)
                    VALUES
                      (:tid, :username, :email, :full_name, :role,
                       true, :theme, :pw_hash,
                       NOW(), NOW())
                """),
                {
                    "tid": tid,
                    "username": username,
                    "email": email,
                    "full_name": _strip(full_name),
                    "role": role,
                    "theme": theme_preference,
                    "pw_hash": pw_hash,
                },
            )
            await db.commit()
    except Exception as e:
        err_str = str(e)
        if "unique" in err_str.lower():
            return _err("Username or email already exists for this tenant.")
        log.exception("User create failed")
        return _err(f"Database error: {err_str[:120]}")

    # ── Stalwart mail account provisioning (fire-and-forget) ────────
    try:
        result = await provision_mail_account(
            email=email,
            full_name=_strip(full_name),
            description=f"{_strip(full_name)} — {role}",
            password=password,
        )
        if result.get("success"):
            log.info("Stalwart account provisioned for %s", email)
        else:
            log.warning("Stalwart provisioning failed for %s: %s", email, result.get("error"))
    except Exception as e:
        log.warning("Stalwart provisioning error for %s: %s (non-blocking)", email, e)

    return RedirectResponse(f"/admin/users?tenant_id={tid}", status_code=303)


@router.get("/{user_id}", response_class=HTMLResponse)
async def user_edit_form(
    request: Request,
    user_id: int,
    tenant_id: Optional[str] = None,
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    user = await _get_user(user_id, tid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return templates.TemplateResponse(request, "user_edit.html", {
        "page": "users",
        "user": user,
        "tenant_id": tid,
        "roles": ASSIGNABLE_ROLES,
        "error": None,
        "form": {},
        "branding": getattr(request.state, "branding", None),
    })


@router.post("/{user_id}")
async def user_update(
    request: Request,
    user_id: int,
    tenant_id: str = Form(...),
    full_name: str = Form(""),
    email: str = Form(...),
    role: str = Form("staff"),
    theme_preference: str = Form("dark"),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    user = await _get_user(user_id, tid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if role not in ASSIGNABLE_ROLES:
        role = "staff"
    if theme_preference not in ("dark", "light"):
        theme_preference = "dark"
    email = _strip(email)

    def _err(msg: str):
        return templates.TemplateResponse(request, "user_edit.html", {
            "page": "users",
            "user": user,
            "tenant_id": tid,
            "roles": ASSIGNABLE_ROLES,
            "error": msg,
            "form": {},
            "branding": getattr(request.state, "branding", None),
        })

    if not email or not _valid_email(email):
        return _err("Valid email is required.")

    pw_clause = ""
    pw_params: dict = {}
    if new_password:
        if len(new_password) < 8:
            return _err("New password must be at least 8 characters.")
        if new_password != new_password_confirm:
            return _err("Passwords do not match.")
        pw_hash = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
        pw_clause = ", password_hash = :pw_hash"
        pw_params["pw_hash"] = pw_hash

    async with AsyncSessionLocal() as db:
        await db.execute(
            text(f"""
                UPDATE users
                SET full_name = :full_name,
                    email = :email,
                    role = :role,
                    theme_preference = :theme,
                    updated_at = NOW()
                    {pw_clause}
                WHERE id = :uid AND tenant_id = :tid
            """),
            {
                "full_name": _strip(full_name),
                "email": email,
                "role": role,
                "theme": theme_preference,
                "uid": user_id,
                "tid": tid,
                **pw_params,
            },
        )
        await db.commit()

    # ── Sync password to Stalwart if changed ─────────────────────────
    if new_password and email:
        try:
            result = await update_mail_password(email, new_password)
            if result.get("success"):
                log.info("Stalwart password synced for %s", email)
            else:
                log.warning("Stalwart password sync failed for %s: %s", email, result.get("error"))
        except Exception as e:
            log.warning("Stalwart password sync error (non-blocking): %s", e)

    return RedirectResponse(f"/admin/users?tenant_id={tid}", status_code=303)


@router.post("/{user_id}/deactivate")
async def user_deactivate(
    request: Request,
    user_id: int,
    tenant_id: str = Form(...),
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE users SET is_active = false, updated_at = NOW()
                WHERE id = :uid AND tenant_id = :tid
            """),
            {"uid": user_id, "tid": tid},
        )
        await db.commit()

    # ── Disable Stalwart mail account ────────────────────────────────
    try:
        user = await _get_user(user_id, tid)
        if user and user.email:
            result = await disable_mail_account(user.email)
            if result.get("success"):
                log.info("Stalwart account disabled for %s", user.email)
            else:
                log.warning("Stalwart disable failed for %s: %s", user.email, result.get("error"))
    except Exception as e:
        log.warning("Stalwart disable error (non-blocking): %s", e)

    return RedirectResponse(f"/admin/users?tenant_id={tid}", status_code=303)


@router.post("/{user_id}/reactivate")
async def user_reactivate(
    request: Request,
    user_id: int,
    tenant_id: str = Form(...),
):
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE users SET is_active = true, updated_at = NOW()
                WHERE id = :uid AND tenant_id = :tid
            """),
            {"uid": user_id, "tid": tid},
        )
        await db.commit()

    # ── Re-enable Stalwart mail account ──────────────────────────────
    try:
        user = await _get_user(user_id, tid)
        if user and user.email:
            result = await enable_mail_account(user.email)
            if result.get("success"):
                log.info("Stalwart account re-enabled for %s", user.email)
            else:
                log.warning("Stalwart enable failed for %s: %s", user.email, result.get("error"))
    except Exception as e:
        log.warning("Stalwart enable error (non-blocking): %s", e)

    return RedirectResponse(f"/admin/users?tenant_id={tid}", status_code=303)


@router.post("/{user_id}/theme", response_class=HTMLResponse)
async def user_theme_toggle(
    request: Request,
    user_id: int,
    tenant_id: str = Form(...),
    theme_preference: str = Form("dark"),
):
    """HTMX inline theme toggle — returns badge fragment only."""
    sess = await _require_admin(request)
    tid = _strip(tenant_id) or sess["tenant_id"]
    if theme_preference not in ("dark", "light"):
        theme_preference = "dark"
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                UPDATE users SET theme_preference = :theme, updated_at = NOW()
                WHERE id = :uid AND tenant_id = :tid
            """),
            {"theme": theme_preference, "uid": user_id, "tid": tid},
        )
        await db.commit()
    badge_class = (
        "bg-yellow-100 text-yellow-800"
        if theme_preference == "light"
        else "bg-gray-700 text-gray-300"
    )
    icon = "☀️" if theme_preference == "light" else "🌙"
    return HTMLResponse(
        f'<span class="badge {badge_class}">{icon} {theme_preference.capitalize()}</span>'
    )
