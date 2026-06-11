"""
modules/ediscovery/routes/drop_link_api.py
==========================================
Authenticated API for creating and managing client drop links.

Routes:
  POST   /api/v1/ediscovery/drop-links          -- create a new drop link
  GET    /api/v1/ediscovery/drop-links           -- list drop links (optionally by matter)
  GET    /api/v1/ediscovery/drop-links/{id}      -- get link detail + access log
  PATCH  /api/v1/ediscovery/drop-links/{id}      -- update (revoke, extend, etc.)
  DELETE /api/v1/ediscovery/drop-links/{id}      -- revoke a drop link
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.dashboard.services.auth_helper import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ediscovery/drop-links", tags=["drop-links"])


def _generate_token() -> str:
    """Generate a secure 48-char URL-safe token."""
    return secrets.token_urlsafe(36)


@router.post("")
async def create_drop_link(request: Request, user=Depends(get_current_user)):
    """Create a new client drop link for a matter."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()
    user_id = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, 400)

    matter_id = body.get("matter_id", "").strip()
    label = body.get("label", "").strip()
    instructions = body.get("instructions", "").strip()
    recipient_name = body.get("recipient_name", "").strip()
    recipient_email = body.get("recipient_email", "").strip()
    max_uploads = body.get("max_uploads")
    max_file_size_mb = body.get("max_file_size_mb", 500)
    allowed_extensions = body.get("allowed_extensions")
    expires_days = body.get("expires_days", 30)

    if not matter_id or not label:
        return JSONResponse({"error": "matter_id and label are required"}, 400)

    token = _generate_token()
    expires_at = None
    if expires_days and expires_days > 0:
        expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sa_text("""
                INSERT INTO client_drop_links
                    (tenant_id, matter_id, token, label, instructions,
                     recipient_name, recipient_email,
                     max_uploads, max_file_size_mb, allowed_extensions,
                     expires_at, created_by)
                VALUES
                    (:tid, CAST(:mid AS uuid), :token, :label, :instr,
                     :rname, :remail,
                     :max_up, :max_mb, :exts,
                     :exp, :uid)
                RETURNING id::text, token, created_at
            """), {
                "tid": tenant_id,
                "mid": matter_id,
                "token": token,
                "label": label,
                "instr": instructions or None,
                "rname": recipient_name or None,
                "remail": recipient_email or None,
                "max_up": max_uploads,
                "max_mb": max_file_size_mb,
                "exts": allowed_extensions,
                "exp": expires_at,
                "uid": user_id,
            })
            row = result.mappings().fetchone()
            await session.commit()

        # Build the full URL
        host = request.headers.get("host", "")
        scheme = "https" if request.url.scheme == "https" or "443" in host else "http"
        drop_url = f"{scheme}://{host}/drop/{token}"

        return JSONResponse({
            "id": row["id"],
            "token": row["token"],
            "url": drop_url,
            "label": label,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "created_at": str(row["created_at"]),
        })

    except Exception as e:
        logger.error(f"create_drop_link error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("")
async def list_drop_links(
    request: Request,
    matter_id: str = "",
    user=Depends(get_current_user),
):
    """List drop links, optionally filtered by matter."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    where = "TRIM(dl.tenant_id) = :tid"
    params = {"tid": tenant_id}

    if matter_id:
        where += " AND dl.matter_id = CAST(:mid AS uuid)"
        params["mid"] = matter_id

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(sa_text(f"""
                SELECT dl.id::text,
                       dl.token,
                       dl.label,
                       dl.recipient_name,
                       dl.recipient_email,
                       dl.instructions,
                       dl.max_uploads,
                       dl.upload_count,
                       dl.total_bytes_uploaded,
                       dl.is_active,
                       dl.is_revoked,
                       dl.expires_at,
                       dl.collection_id::text,
                       dl.matter_id::text,
                       dl.created_at,
                       dl.updated_at,
                       m.matter_name,
                       m.matter_number,
                       u.display_name as created_by_name
                FROM client_drop_links dl
                JOIN matters m ON m.id = dl.matter_id
                LEFT JOIN users u ON u.id = dl.created_by
                WHERE {where}
                ORDER BY dl.created_at DESC
            """), params)
            rows = [dict(r) for r in result.mappings().fetchall()]

        # Build URLs
        host = request.headers.get("host", "")
        scheme = "https" if request.url.scheme == "https" or "443" in host else "http"
        for row in rows:
            row["url"] = f"{scheme}://{host}/drop/{row['token']}"
            row["created_at"] = str(row["created_at"]) if row.get("created_at") else None
            row["updated_at"] = str(row["updated_at"]) if row.get("updated_at") else None
            row["expires_at"] = str(row["expires_at"]) if row.get("expires_at") else None

        return JSONResponse({"drop_links": rows})

    except Exception as e:
        logger.error(f"list_drop_links error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.get("/{link_id}")
async def get_drop_link(request: Request, link_id: str, user=Depends(get_current_user)):
    """Get drop link detail + recent access log."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    try:
        async with AsyncSessionLocal() as session:
            # Link detail
            r = await session.execute(sa_text("""
                SELECT dl.*,
                       m.matter_name, m.matter_number,
                       u.display_name as created_by_name
                FROM client_drop_links dl
                JOIN matters m ON m.id = dl.matter_id
                LEFT JOIN users u ON u.id = dl.created_by
                WHERE dl.id = CAST(:lid AS uuid) AND TRIM(dl.tenant_id) = :tid
            """), {"lid": link_id, "tid": tenant_id})
            link = r.mappings().fetchone()

            if not link:
                return JSONResponse({"error": "Link not found"}, 404)

            link = dict(link)

            # Access log
            al = await session.execute(sa_text("""
                SELECT action, visitor_name, visitor_email, visitor_firm,
                       ip_address, file_names, file_count, total_bytes, accessed_at
                FROM client_drop_access_log
                WHERE drop_link_id = CAST(:lid AS uuid)
                ORDER BY accessed_at DESC
                LIMIT 100
            """), {"lid": link_id})
            access_log = [dict(r) for r in al.mappings().fetchall()]

        host = request.headers.get("host", "")
        scheme = "https" if request.url.scheme == "https" or "443" in host else "http"
        link["url"] = f"{scheme}://{host}/drop/{link['token']}"

        # Serialize dates
        for key in ("created_at", "updated_at", "expires_at"):
            if link.get(key):
                link[key] = str(link[key])
        for entry in access_log:
            if entry.get("accessed_at"):
                entry["accessed_at"] = str(entry["accessed_at"])

        return JSONResponse({
            "drop_link": link,
            "access_log": access_log,
        })

    except Exception as e:
        logger.error(f"get_drop_link error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.patch("/{link_id}")
async def update_drop_link(request: Request, link_id: str, user=Depends(get_current_user)):
    """Update a drop link (revoke, extend expiry, toggle active)."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, 400)

    sets = []
    params = {"lid": link_id, "tid": tenant_id}

    if "is_revoked" in body:
        sets.append("is_revoked = :revoked")
        params["revoked"] = body["is_revoked"]
    if "is_active" in body:
        sets.append("is_active = :active")
        params["active"] = body["is_active"]
    if "expires_days" in body:
        new_exp = datetime.now(timezone.utc) + timedelta(days=body["expires_days"])
        sets.append("expires_at = :exp")
        params["exp"] = new_exp
    if "instructions" in body:
        sets.append("instructions = :instr")
        params["instr"] = body["instructions"]

    if not sets:
        return JSONResponse({"error": "No fields to update"}, 400)

    sets.append("updated_at = now()")

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text(f"""
                UPDATE client_drop_links
                SET {', '.join(sets)}
                WHERE id = CAST(:lid AS uuid) AND TRIM(tenant_id) = :tid
            """), params)
            await session.commit()

        return JSONResponse({"ok": True})

    except Exception as e:
        logger.error(f"update_drop_link error: {e}")
        return JSONResponse({"error": str(e)}, 500)


@router.delete("/{link_id}")
async def revoke_drop_link(request: Request, link_id: str, user=Depends(get_current_user)):
    """Revoke a drop link (soft delete — marks as revoked)."""
    tenant_id = getattr(request.state, "tenant_id", "").strip()

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(sa_text("""
                UPDATE client_drop_links
                SET is_revoked = true, is_active = false, updated_at = now()
                WHERE id = CAST(:lid AS uuid) AND TRIM(tenant_id) = :tid
            """), {"lid": link_id, "tid": tenant_id})
            await session.commit()

        return JSONResponse({"ok": True, "status": "revoked"})

    except Exception as e:
        logger.error(f"revoke_drop_link error: {e}")
        return JSONResponse({"error": str(e)}, 500)
