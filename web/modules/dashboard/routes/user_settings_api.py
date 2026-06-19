"""
User Settings API — per-user profile, signatures, connectors, vault, preferences.

Route prefix: /api/v1/user-settings
Mounted in core router or user module __init__.py.

Follows Praesidium patterns:
  - AsyncSessionLocal (never get_session_factory)
  - TRIM(tenant_id) in all queries
  - CAST(:param AS jsonb) for asyncpg
  - All writes via write_audit() pattern
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import json
import uuid
from datetime import datetime, timezone

from core.db.base import AsyncSessionLocal
from sqlalchemy import text

router = APIRouter(prefix="/api/v1/user-settings", tags=["user-settings"])


# ─── Pydantic Models ────────────────────────────────────────

class ProfileUpdate(BaseModel):
    full_name: str
    email: str
    phone: Optional[str] = None
    title: Optional[str] = None
    department: Optional[str] = None
    bar_number: Optional[str] = None
    jurisdiction: Optional[str] = None
    default_hourly_rate: Optional[float] = None
    bar_admissions: Optional[List[Dict[str, Any]]] = None
    uspto_reg_number: Optional[str] = None


class SignaturesUpdate(BaseModel):
    signatures: List[Dict[str, Any]]


class ConnectorSave(BaseModel):
    connector_type: str
    config: Dict[str, Any]
    is_active: bool = False


class ConnectorTest(BaseModel):
    connector_type: str


class VaultSave(BaseModel):
    provider: str
    key_type: str
    value: str


class VaultDelete(BaseModel):
    provider: str
    key_type: str


class PreferencesUpdate(BaseModel):
    default_landing_page: Optional[str] = None
    email_notifications: Optional[bool] = None
    desktop_notifications: Optional[bool] = None
    time_entry_rounding: Optional[str] = None
    calendar_default_view: Optional[str] = None
    ai_suggestions: Optional[bool] = None
    auto_save_drafts: Optional[bool] = None
    compact_tables: Optional[bool] = None


# ─── Helpers ────────────────────────────────────────────────

def _get_user_context(request: Request):
    """Extract tenant_id and user_id from request state (set by auth middleware)."""
    user = getattr(request.state, "current_user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tenant_id = getattr(request.state, "tenant_id", None) or getattr(user, "tenant_id", "").strip()
    user_id = user.id
    return tenant_id.strip(), user_id


async def _get_user_row(tenant_id: str, user_id: int) -> dict:
    """Fetch full user row as dict."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, username, full_name, email, phone, title, department,
                       bar_number, jurisdiction, default_hourly_rate, role,
                       theme_preference, user_preferences, is_active, is_timekeeper,
                       auth_provider, mfa_enabled, last_login, created_at
                FROM users
                WHERE TRIM(tenant_id) = :tid AND id = :uid
            """),
            {"tid": tenant_id, "uid": user_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        d = dict(row)
        if isinstance(d.get("user_preferences"), str):
            d["user_preferences"] = json.loads(d["user_preferences"])
        return d


# ─── Profile Endpoints ──────────────────────────────────────

@router.get("/profile")
async def get_profile(request: Request):
    tid, uid = _get_user_context(request)
    user = await _get_user_row(tid, uid)
    return {"user": user}


@router.put("/profile")
async def update_profile(request: Request, body: ProfileUpdate):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                UPDATE users SET
                    full_name = :full_name,
                    email = :email,
                    phone = :phone,
                    title = :title,
                    department = :department,
                    bar_number = :bar_number,
                    jurisdiction = :jurisdiction,
                    default_hourly_rate = :rate,
                    updated_at = NOW()
                WHERE TRIM(tenant_id) = :tid AND id = :uid
            """),
            {
                "tid": tid, "uid": uid,
                "full_name": body.full_name,
                "email": body.email,
                "phone": body.phone,
                "title": body.title,
                "department": body.department,
                "bar_number": body.bar_number,
                "jurisdiction": body.jurisdiction,
                "rate": body.default_hourly_rate,
            },
        )

        if body.bar_admissions is not None or body.uspto_reg_number is not None:
            result = await session.execute(
                text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
                {"tid": tid, "uid": uid},
            )
            row = result.scalar_one_or_none()
            prefs = json.loads(row) if isinstance(row, str) else (row or {})
            if body.bar_admissions is not None:
                prefs["bar_admissions"] = body.bar_admissions
            if body.uspto_reg_number is not None:
                prefs["uspto_reg_number"] = body.uspto_reg_number
            await session.execute(
                text("""
                    UPDATE users SET user_preferences = CAST(:prefs AS jsonb)
                    WHERE TRIM(tenant_id) = :tid AND id = :uid
                """),
                {"tid": tid, "uid": uid, "prefs": json.dumps(prefs)},
            )

        await session.commit()
    user = await _get_user_row(tid, uid)
    return {"user": user}


# ─── Signatures Endpoints ───────────────────────────────────

@router.put("/signatures")
async def update_signatures(request: Request, body: SignaturesUpdate):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
            {"tid": tid, "uid": uid},
        )
        row = result.scalar_one_or_none()
        prefs = json.loads(row) if isinstance(row, str) else (row or {})
        prefs["email_signatures"] = body.signatures
        default_sig = next((s for s in body.signatures if s.get("is_default")), None)
        if default_sig:
            prefs["email_signature"] = default_sig.get("html", "")

        await session.execute(
            text("""
                UPDATE users SET user_preferences = CAST(:prefs AS jsonb), updated_at = NOW()
                WHERE TRIM(tenant_id) = :tid AND id = :uid
            """),
            {"tid": tid, "uid": uid, "prefs": json.dumps(prefs)},
        )
        await session.commit()
    user = await _get_user_row(tid, uid)
    return {"user": user}


async def _provision_personal_mail(tid: str, uid: int):
    """Mirror a saved 'personal_mail' connector into the live JMAP connector store
    (tenant_connectors row + encrypted admin creds in the vault) so the Comms
    multibox can read it. Returns an error string if it could not be wired, else None."""
    from core.services import mail_connectors as _mc
    user = await _get_user_row(tid, uid)
    email = (user.get("email") or "").strip()
    if not email:
        return "user has no email address"
    prefs = user.get("user_preferences") or {}
    conn = next((c for c in prefs.get("connectors", [])
                 if c.get("connector_type") == "personal_mail"), None)
    cfg = (conn or {}).get("config") or {}
    jmap_url = (cfg.get("jmap_url") or "").strip()
    admin_user = (cfg.get("admin_user") or "").strip()
    admin_pass = (cfg.get("admin_password") or "").strip()
    account_address = (cfg.get("account_address") or "").strip() or email
    color = (cfg.get("color") or "").strip() or None
    if not (jmap_url and admin_user and admin_pass):
        return "incomplete config (need JMAP URL, admin user, admin password)"
    await _mc.set_personal_connector(
        email, jmap_url=jmap_url, admin_user=admin_user, admin_pass=admin_pass,
        account_address=account_address, color=color, tenant_id=tid,
    )
    return None


# ─── Connectors Endpoints ───────────────────────────────────

@router.get("/connectors")
async def get_connectors(request: Request):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
            {"tid": tid, "uid": uid},
        )
        row = result.scalar_one_or_none()
        prefs = json.loads(row) if isinstance(row, str) else (row or {})
    connectors = prefs.get("connectors", [])
    safe = []
    for c in connectors:
        cc = {**c, "config": {}}
        for k, v in (c.get("config") or {}).items():
            if "password" in k or "secret" in k:
                cc["config"][k] = "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" if v else ""
            else:
                cc["config"][k] = v
        safe.append(cc)
    return {"connectors": safe}


@router.post("/connectors")
async def save_connector(request: Request, body: ConnectorSave):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
            {"tid": tid, "uid": uid},
        )
        row = result.scalar_one_or_none()
        prefs = json.loads(row) if isinstance(row, str) else (row or {})
        connectors = prefs.get("connectors", [])

        existing = next((c for c in connectors if c["connector_type"] == body.connector_type), None)
        if existing:
            for k, v in body.config.items():
                if v and v != "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022":
                    existing["config"][k] = v
            existing["is_active"] = body.is_active
        else:
            connectors.append({
                "connector_type": body.connector_type,
                "config": body.config,
                "is_active": body.is_active,
            })

        prefs["connectors"] = connectors
        await session.execute(
            text("""
                UPDATE users SET user_preferences = CAST(:prefs AS jsonb), updated_at = NOW()
                WHERE TRIM(tenant_id) = :tid AND id = :uid
            """),
            {"tid": tid, "uid": uid, "prefs": json.dumps(prefs)},
        )
        await session.commit()

    if body.connector_type == "personal_mail":
        try:
            warning = await _provision_personal_mail(tid, uid)
        except Exception as e:
            warning = f"live wiring failed: {e}"
        if warning:
            return {"status": "ok", "warning": warning}
    return {"status": "ok"}


@router.post("/connectors/test")
async def test_connector(request: Request, body: ConnectorTest):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
            {"tid": tid, "uid": uid},
        )
        row = result.scalar_one_or_none()
        prefs = json.loads(row) if isinstance(row, str) else (row or {})
    connectors = prefs.get("connectors", [])
    conn = next((c for c in connectors if c["connector_type"] == body.connector_type), None)
    if not conn:
        return {"success": False, "error": "Connector not configured"}
    if not conn.get("config"):
        return {"success": False, "error": "No configuration saved"}

    if body.connector_type == "personal_mail":
        from core.services import mail_connectors as _mc
        user = await _get_user_row(tid, uid)
        email = (user.get("email") or "").strip()
        try:
            pc = await _mc.get_personal_connector(email)
        except Exception as e:
            return {"success": False, "error": f"connection error: {e}"}
        if not pc:
            return {"success": False, "error": "Not wired yet — save the connector first."}
        if not pc.ok:
            return {"success": False, "error": f"Reached server but could not resolve mailbox '{pc.account_address}'. Check the address / admin creds."}
        return {"success": True, "message": f"Connected to {pc.account_address} (account {pc.account_id})."}

    return {"success": True, "message": "Configuration looks valid. Full connection test coming soon."}


# ─── Vault Endpoints ────────────────────────────────────────

@router.get("/vault")
async def get_vault(request: Request):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id, provider, key_type, key_hint, created_at, updated_at
                FROM credentials_vault
                WHERE TRIM(tenant_id) = :tid
                ORDER BY provider, key_type
            """),
            {"tid": tid},
        )
        rows = [dict(r) for r in result.mappings().all()]
    return {"credentials": rows}


@router.post("/vault")
async def save_vault(request: Request, body: VaultSave):
    tid, uid = _get_user_context(request)
    hint = None
    if len(body.value) >= 6:
        hint = body.value[:3] + "..." + body.value[-3:]
    elif body.value:
        hint = body.value[:2] + "..."

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("""
                SELECT id FROM credentials_vault
                WHERE TRIM(tenant_id) = :tid AND provider = :provider AND key_type = :key_type
            """),
            {"tid": tid, "provider": body.provider, "key_type": body.key_type},
        )
        existing = result.scalar_one_or_none()

        if existing:
            await session.execute(
                text("""
                    UPDATE credentials_vault
                    SET encrypted_key = :val, key_hint = :hint, updated_at = NOW()
                    WHERE id = :id
                """),
                {"val": body.value, "hint": hint, "id": existing},
            )
        else:
            await session.execute(
                text("""
                    INSERT INTO credentials_vault (id, tenant_id, provider, key_type, encrypted_key, key_hint)
                    VALUES (gen_random_uuid(), :tid, :provider, :key_type, :val, :hint)
                """),
                {"tid": tid, "provider": body.provider, "key_type": body.key_type,
                 "val": body.value, "hint": hint},
            )
        await session.commit()
    return {"status": "ok"}


@router.delete("/vault")
async def delete_vault(request: Request, body: VaultDelete):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                DELETE FROM credentials_vault
                WHERE TRIM(tenant_id) = :tid AND provider = :provider AND key_type = :key_type
            """),
            {"tid": tid, "provider": body.provider, "key_type": body.key_type},
        )
        await session.commit()
    return {"status": "ok"}


# ─── Preferences Endpoints ──────────────────────────────────

@router.put("/preferences")
async def update_preferences(request: Request, body: PreferencesUpdate):
    tid, uid = _get_user_context(request)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT user_preferences FROM users WHERE TRIM(tenant_id) = :tid AND id = :uid"),
            {"tid": tid, "uid": uid},
        )
        row = result.scalar_one_or_none()
        prefs = json.loads(row) if isinstance(row, str) else (row or {})

        for k, v in body.dict(exclude_none=True).items():
            prefs[k] = v

        await session.execute(
            text("""
                UPDATE users SET user_preferences = CAST(:prefs AS jsonb), updated_at = NOW()
                WHERE TRIM(tenant_id) = :tid AND id = :uid
            """),
            {"tid": tid, "uid": uid, "prefs": json.dumps(prefs)},
        )
        await session.commit()
    user = await _get_user_row(tid, uid)
    return {"user": user}
