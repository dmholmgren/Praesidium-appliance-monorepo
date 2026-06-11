"""
M-DESK C1 — Checkout / checkin / release routes.

Three endpoints, all behind require_desktop_user:

  POST /api/v1/desktop/checkout/{doc_id}
    Claim a checkout on the document, return the file bytes for editing.
    409 with held-by metadata if another user holds a fresh lock.

  POST /api/v1/desktop/checkin/{doc_id}
    Multipart upload of edited bytes. Writes a new documents row with
    parent_doc_id linking back to the parent, increments version_number,
    clears the checkout on the parent. The new row IS the checked-in
    version; the parent row is preserved as historical record.

  POST /api/v1/desktop/checkout/{doc_id}/release
    Clear the checkout WITHOUT writing a new version (discards local edits
    server-side). Allowed for the holder or any admin (role rank >= 4).

require_desktop_user dependency:
  Validates the Authorization Bearer JWT, returns the AccessClaims dataclass.
  Defense in depth: also verifies the JWT's tenant_id claim matches the
  request's resolved tenant_id (so a token issued for tenant A cannot be
  presented at tenant B's hostname).

Storage: direct file IO under /mnt/praesidium/{tenant_id}/desktop_versions/.
On checkin we hash the upload bytes (sha256), write to a content-addressed
path, and record the path + checksum on the new documents row.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt as pyjwt
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path as PathParam,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.desktop import checkout_service as cs
from modules.desktop import jwt_service

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-checkout"])


# ═════════════════════════════════════════════════════════════════════════
# require_desktop_user — JWT verifier dependency
# ═════════════════════════════════════════════════════════════════════════

async def require_desktop_user(request: Request) -> jwt_service.AccessClaims:
    """Dependency: extract + verify Bearer JWT, return AccessClaims.

    Raises 401 on any verification failure with a discriminating reason
    in the body so the VSTO client can react (e.g. trigger /refresh on
    'expired').
    """
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_token", "reason": "no_bearer"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:].strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_token", "reason": "empty_bearer"},
        )

    try:
        claims = jwt_service.decode_access_token(token)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "token_expired", "reason": "expired"},
        )
    except pyjwt.InvalidSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "reason": "bad_signature"},
        )
    except pyjwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "reason": "malformed",
                    "message": str(exc)},
        )

    # Tenant pin: the JWT carries the tenant it was issued for. The request
    # carries the tenant resolved from the hostname. They MUST match.
    request_tenant = (getattr(request.state, "tenant_id", None) or "").strip()
    if request_tenant and claims.tenant_id != request_tenant:
        logger.warning(
            "[m-desk] tenant pin mismatch token=%s request=%s sub=%s",
            claims.tenant_id, request_tenant, claims.sub,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "tenant_mismatch", "reason": "tenant_mismatch"},
        )

    return claims


# ═════════════════════════════════════════════════════════════════════════
# Storage helpers — direct IO under /mnt/praesidium/{tenant_id}/...
# ═════════════════════════════════════════════════════════════════════════

def _storage_root() -> Path:
    """Filesystem root for content storage. Configured via env."""
    root = os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium")
    return Path(root)


def _tenant_versions_dir(tenant_id: str) -> Path:
    """Content-addressed write target for new desktop checkin versions."""
    tid = (tenant_id or "").strip()
    return _storage_root() / tid / "desktop_versions"


async def _read_file_bytes(storage_path: str, tenant_id: str = "") -> bytes:
    """Read file content from a stored documents.storage_path.

    storage_path patterns seen in the wild:
      - absolute path (starts with /)
      - 'praesidium/matters/{matter_uuid}/...' (missing tenant_id dir)
      - other relative
    We try multiple resolution strategies and use the first that exists.
    """
    tid = (tenant_id or "").strip()
    candidates = []

    p = Path(storage_path)
    if p.is_absolute():
        candidates.append(p)
    else:
        # Pattern: 'praesidium/matters/...' -> /mnt/praesidium/{tenant}/matters/...
        if storage_path.startswith("praesidium/") and tid:
            rest = storage_path[len("praesidium/"):]  # 'matters/...'
            candidates.append(Path("/mnt/praesidium") / tid / rest)
        # Also try /mnt/ prefix directly
        if storage_path.startswith("praesidium/"):
            candidates.append(Path("/mnt") / storage_path)
        # Generic: prepend storage root
        candidates.append(_storage_root() / storage_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate.read_bytes()

    tried = ", ".join(str(c) for c in candidates)
    raise FileNotFoundError(f"storage path not found. tried: {tried}")


# ═════════════════════════════════════════════════════════════════════════
# Document lookup helper
# ═════════════════════════════════════════════════════════════════════════

async def _load_document(doc_id: str, tenant_id: str) -> Optional[dict]:
    """Fetch the documents row scoped to tenant.

    Returns dict of column -> value (asyncpg Record-style mapping
    converted to plain dict) or None if not found in this tenant.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa_text("""
                SELECT
                  id::text       AS id,
                  tenant_id,
                  matter_id::text AS matter_id,
                  filename, original_filename, mime_type, file_size,
                  storage_path, document_type, doc_type, title,
                  version_number, parent_doc_id::text AS parent_doc_id,
                  metadata
                FROM documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": (tenant_id or "").strip()},
        )
        row = result.mappings().first()
        return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════
# POST /checkout/{doc_id}
# ═════════════════════════════════════════════════════════════════════════

@router.post("/checkout/{doc_id}")
async def checkout_document(
    request: Request,
    doc_id: str = PathParam(..., description="Document UUID to check out"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Claim a checkout and stream the file bytes back."""
    try:
        # Atomic claim — raises CheckoutConflict / DocumentNotFound on failure.
        state = await cs.attempt_checkout(
            doc_id=doc_id,
            tenant_id=claims.tenant_id,
            user_id=claims.user_id,
            user_email=claims.email,
            client=claims.client,
        )
    except cs.DocumentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "doc_id": doc_id},
        )
    except cs.CheckoutConflict as conflict:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": "checkout_conflict",
                "held_by": conflict.state.to_dict(),
            },
        )

    # We hold the lock — fetch the file.
    doc = await _load_document(doc_id, claims.tenant_id)
    if not doc:
        # Astonishing edge case: we just acquired a lock on this doc, then
        # it disappeared between attempt_checkout's SELECT and our load.
        # Release the lock so we don't leave a phantom hold.
        try:
            await cs.release_checkout(
                doc_id=doc_id, tenant_id=claims.tenant_id,
                requester_user_id=claims.user_id, requester_role=claims.role,
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_disappeared"},
        )

    storage_path = doc.get("storage_path")
    if not storage_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_content", "reason": "document has no storage_path",
                    "doc_id": doc_id},
        )

    try:
        content = await _read_file_bytes(storage_path, claims.tenant_id)
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "storage_miss", "storage_path": storage_path},
        )

    filename = doc.get("filename") or doc.get("original_filename") or "document"
    mime = doc.get("mime_type") or "application/octet-stream"

    logger.info(
        "[m-desk] checkout served doc=%s tenant=%s user=%s bytes=%d",
        doc_id, claims.tenant_id, claims.user_id, len(content),
    )

    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "X-Praesidium-Checkout-By":         state.checked_out_by,
        "X-Praesidium-Checkout-At":         state.checked_out_at.isoformat(),
        "X-Praesidium-Checkout-TTL":        str(state.checkout_lock_ttl),
        "X-Praesidium-Checkout-Expires-At": state.expires_at.isoformat(),
    }
    return Response(content=content, media_type=mime, headers=headers)


# ═════════════════════════════════════════════════════════════════════════
# POST /checkin/{doc_id}
# ═════════════════════════════════════════════════════════════════════════

@router.post("/checkin/{doc_id}")
async def checkin_document(
    request: Request,
    doc_id: str = PathParam(..., description="Document UUID to check in"),
    file: UploadFile = File(...),
    note: Optional[str] = Form(None),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Upload edited bytes, create a new version row, clear the checkout."""
    # Verify the caller actually holds the lock. We don't trust the client
    # to have called /checkout first — anyone with a valid JWT could try
    # to push a new version. This guard is the SAME concurrency control
    # the checkout flow already enforces.
    state = await cs.read_checkout(doc_id=doc_id, tenant_id=claims.tenant_id)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "no_checkout",
                    "reason": "document is not currently checked out"},
        )
    if not state.held_by_user(claims.user_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "not_holder",
                    "reason": "checkout held by different user",
                    "held_by": state.to_dict()},
        )
    if state.is_expired:
        # The TTL elapsed but no other user reclaimed yet. Refuse the
        # checkin — force re-checkout to prove the user still wants this.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "checkout_expired",
                    "reason": "lock expired before checkin; re-checkout required"},
        )

    parent = await _load_document(doc_id, claims.tenant_id)
    if not parent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "doc_id": doc_id},
        )

    # Read upload bytes, hash, write to disk under a stable per-version path.
    upload_bytes = await file.read()
    if not upload_bytes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "empty_upload"},
        )
    checksum = hashlib.sha256(upload_bytes).hexdigest()

    versions_dir = _tenant_versions_dir(claims.tenant_id)
    versions_dir.mkdir(parents=True, exist_ok=True)

    # We don't know the new doc UUID yet (the DB will mint it). Use the
    # checksum as a stable path component AND link to the new row id once
    # the INSERT returns. The path schema:
    #   /mnt/praesidium/{tenant_id}/desktop_versions/{checksum}_{filename}
    # Co-locating by checksum gives us natural dedup if the same content
    # is checked in twice — we can detect that later.
    safe_filename = (parent.get("filename") or "document").replace("/", "_")
    version_path = versions_dir / f"{checksum}_{safe_filename}"
    version_path.write_bytes(upload_bytes)

    parent_version = parent.get("version_number") or 0
    new_version_number = int(parent_version) + 1

    now = datetime.now(timezone.utc)

    # Insert new row, atomically clear checkout on the parent in the same
    # transaction.
    async with AsyncSessionLocal() as session:
        insert_result = await session.execute(
            sa_text("""
                INSERT INTO documents (
                  id, tenant_id, matter_id, filename, original_filename,
                  mime_type, file_size, storage_path,
                  document_type, doc_type, title,
                  version_number, parent_doc_id,
                  checksum, created_by,
                  status, metadata, created_at, updated_at
                )
                VALUES (
                  gen_random_uuid(),
                  :tid,
                  CASE WHEN CAST(:matter_id AS text) = '' THEN NULL
                       ELSE CAST(:matter_id AS uuid) END,
                  :filename, :original_filename,
                  :mime_type, :file_size, :storage_path,
                  :document_type, :doc_type, :title,
                  :version_number, CAST(:parent_doc_id AS uuid),
                  :checksum, :created_by,
                  'active', CAST(:metadata AS jsonb), :now, :now
                )
                RETURNING id::text AS id
            """),
            {
                "tid":                claims.tenant_id,
                "matter_id":          parent.get("matter_id") or "",
                "filename":           parent.get("filename") or safe_filename,
                "original_filename":  parent.get("original_filename")
                                       or parent.get("filename")
                                       or safe_filename,
                "mime_type":          (file.content_type
                                       or parent.get("mime_type")
                                       or "application/octet-stream"),
                "file_size":          len(upload_bytes),
                "storage_path":       str(version_path),
                "document_type":      parent.get("document_type"),
                "doc_type":           parent.get("doc_type"),
                "title":              parent.get("title"),
                "version_number":     new_version_number,
                "parent_doc_id":      doc_id,
                "checksum":           checksum,
                "created_by":         claims.user_id,
                "metadata":           '{"source":"m-desk-checkin"}',
                "now":                now,
            },
        )
        new_row = insert_result.mappings().first()
        new_doc_id = new_row["id"]

        # Clear checkout state on the parent in the same transaction.
        await session.execute(
            sa_text("""
                UPDATE documents
                SET metadata = COALESCE(metadata, '{}'::jsonb)
                               - CAST(:keys AS text[]),
                    updated_at = :now
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {
                "keys": list(cs.CHECKOUT_KEYS),
                "now":  now,
                "doc":  doc_id,
                "tid":  claims.tenant_id,
            },
        )
        await session.commit()

    logger.info(
        "[m-desk] checkin ok parent=%s new_doc=%s tenant=%s user=%s "
        "bytes=%d version=%d note=%r",
        doc_id, new_doc_id, claims.tenant_id, claims.user_id,
        len(upload_bytes), new_version_number, note,
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={
            "new_doc_id":      new_doc_id,
            "parent_doc_id":   doc_id,
            "version_number":  new_version_number,
            "checksum":        checksum,
            "file_size":       len(upload_bytes),
            "storage_path":    str(version_path),
            "checked_in_at":   now.isoformat(),
            "note":            note or "",
        },
    )


# ═════════════════════════════════════════════════════════════════════════
# POST /checkout/{doc_id}/release
# ═════════════════════════════════════════════════════════════════════════

@router.post("/checkout/{doc_id}/release")
async def release_document_checkout(
    request: Request,
    doc_id: str = PathParam(..., description="Document UUID to release"),
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Force-release a checkout WITHOUT writing a new version.

    Allowed for the holder OR any admin (role rank >= ADMIN_RANK).
    Discards local edits — the VSTO client should warn the user before
    calling this.
    """
    try:
        cleared = await cs.release_checkout(
            doc_id=doc_id,
            tenant_id=claims.tenant_id,
            requester_user_id=claims.user_id,
            requester_role=claims.role,
        )
    except cs.DocumentNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "document_not_found", "doc_id": doc_id},
        )
    except cs.NotPermitted:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "not_permitted",
                    "reason": "must be holder or admin"},
        )
    except cs.CheckoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "release_failed", "reason": str(exc)},
        )

    logger.info(
        "[m-desk] release ok doc=%s tenant=%s requester=%s was_held_by=%s",
        doc_id, claims.tenant_id, claims.user_id, cleared.checked_out_by,
    )
    return {
        "released":     True,
        "doc_id":       doc_id,
        "was_held_by":  cleared.checked_out_by,
        "released_at":  datetime.now(timezone.utc).isoformat(),
    }
