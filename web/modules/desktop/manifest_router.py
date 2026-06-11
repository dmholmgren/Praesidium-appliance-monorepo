"""
M-DESK C1 — Manifest + heartbeat routes.

GET  /api/v1/desktop/manifest
  Unauthenticated. Returns the latest VSTO installer's version, SHA-256,
  and download URL so the tray app can self-update.

POST /api/v1/desktop/heartbeat
  Authenticated. Tray app pings every ~60s with the list of doc ids it
  currently has open / checked out. Server extends the checkout TTL on
  each one the user still holds.

Manifest source of truth:
  Filesystem directory configured via DESKTOP_INSTALLER_DIR (default
  /opt/praesidium-web/installers/). The newest file matching
  'Praesidium-Desktop-*.msi' OR '*.exe' wins. Version is parsed from
  the filename (Praesidium-Desktop-1.0.0.msi -> '1.0.0').

  Result is cached in process memory for 60 seconds — hashing a 50MB
  installer on every tray ping is wasteful, and the file rarely changes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from modules.desktop import checkout_service as cs
from modules.desktop import jwt_service
from modules.desktop.checkout_router import require_desktop_user

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/v1/desktop", tags=["m-desk-manifest"])


# ═════════════════════════════════════════════════════════════════════════
# Manifest discovery — file-system scan with in-process cache
# ═════════════════════════════════════════════════════════════════════════

_INSTALLER_FILENAME_RE = re.compile(
    r"^Praesidium-Desktop-(?P<version>\d+\.\d+\.\d+(?:[-+][\w\.]+)?)\.(msi|exe)$",
    re.IGNORECASE,
)

_MANIFEST_CACHE_TTL_SECONDS = 60
_manifest_cache: dict[str, Any] = {"value": None, "fetched_at": 0.0}


def _installer_dir() -> Path:
    return Path(os.environ.get(
        "DESKTOP_INSTALLER_DIR", "/opt/praesidium-web/installers"
    ))


def _installer_url_base() -> str:
    return os.environ.get(
        "DESKTOP_INSTALLER_URL_BASE",
        "https://login.hjmmlegal.com/desktop/installers",
    ).rstrip("/")


def _build_manifest() -> Optional[dict[str, Any]]:
    """Scan the installer directory for the newest Praesidium-Desktop-*.msi."""
    d = _installer_dir()
    if not d.exists() or not d.is_dir():
        return None

    candidates: list[tuple[str, Path]] = []
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        match = _INSTALLER_FILENAME_RE.match(entry.name)
        if match:
            candidates.append((match.group("version"), entry))

    if not candidates:
        return None

    # Sort by mtime descending, version descending — newest file wins.
    candidates.sort(
        key=lambda c: (c[1].stat().st_mtime, c[0]),
        reverse=True,
    )
    version, path = candidates[0]

    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    released_at = datetime.fromtimestamp(
        path.stat().st_mtime, tz=timezone.utc
    ).isoformat()

    return {
        "version":               version,
        "released_at":           released_at,
        "sha256":                sha256,
        "filename":              path.name,
        "size_bytes":            path.stat().st_size,
        "download_url":          f"{_installer_url_base()}/{path.name}",
        "min_supported_version": version,  # placeholder — refine in C2
        "release_notes_url":     "",
    }


def _get_manifest_cached() -> Optional[dict[str, Any]]:
    """Return the cached manifest if fresh, otherwise rebuild + cache."""
    now = time.time()
    if (_manifest_cache["value"] is not None
            and now - _manifest_cache["fetched_at"] < _MANIFEST_CACHE_TTL_SECONDS):
        return _manifest_cache["value"]
    manifest = _build_manifest()
    _manifest_cache["value"] = manifest
    _manifest_cache["fetched_at"] = now
    return manifest


# ═════════════════════════════════════════════════════════════════════════
# GET /manifest  — unauthenticated
# ═════════════════════════════════════════════════════════════════════════

@router.get("/manifest")
async def get_manifest():
    """Return the latest installer manifest, or 404 if no installer staged."""
    manifest = _get_manifest_cached()
    if manifest is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "no_installer_staged",
                "message": (
                    "No Praesidium-Desktop installer is currently staged "
                    "in the installer directory."
                ),
                "expected_dir": str(_installer_dir()),
            },
        )
    return manifest


# ═════════════════════════════════════════════════════════════════════════
# POST /heartbeat
# ═════════════════════════════════════════════════════════════════════════

class HeartbeatRequest(BaseModel):
    checked_out_doc_ids: list[str] = Field(default_factory=list)


@router.post("/heartbeat")
async def heartbeat(
    body: HeartbeatRequest,
    request: Request,
    claims: jwt_service.AccessClaims = Depends(require_desktop_user),
):
    """Extend checkouts the user still holds; return a per-doc map.

    Body:
      { "checked_out_doc_ids": ["<uuid>", ...] }

    Response:
      {
        "server_time": "2026-...",
        "client": "<bound to this token>",
        "extended": { "<uuid>": true|false, ... }
      }

    A `false` value means the user no longer holds that lock — the tray
    UI should clear the local checkout state for that doc.
    """
    extended = await cs.bulk_extend_checkouts(
        doc_ids=body.checked_out_doc_ids,
        tenant_id=claims.tenant_id,
        user_id=claims.user_id,
    )

    if body.checked_out_doc_ids:
        held_count = sum(1 for v in extended.values() if v)
        logger.debug(
            "[m-desk] heartbeat user=%s tenant=%s presented=%d still_held=%d",
            claims.user_id, claims.tenant_id,
            len(body.checked_out_doc_ids), held_count,
        )

    return {
        "server_time":  datetime.now(timezone.utc).isoformat(),
        "client":       claims.client,
        "extended":     extended,
    }
