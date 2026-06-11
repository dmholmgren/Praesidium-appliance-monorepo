"""
COMP 7 — WebDAV Endpoint v2.1 (Appliance)

Local mount provider for /mnt/praesidium + /mnt/legacy.
Presents a virtual tree:
    /docs/{client_name}/{matter_name}/{subfolders}/{files}   ← RW (praesidium)
    /docs/Legacy/{Clients,Docsend,Accounting,...}            ← RO (legacy)

Backed by the on-disk layouts:
    /mnt/praesidium/{tenant_id}/matters/...   (read-write)
    /mnt/legacy/...                           (read-only)

Authentication via webdav_auth.py (LDAP against tenant_connectors).
Mounted in FastAPI at /docs/ via ASGI middleware.

⚖  PATENT NOTICE: Patent Pending — 64/020,027
"""

import asyncio
import io
import logging
import mimetypes
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

logger = logging.getLogger("praesidium.webdav")

STORAGE_ROOT = os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium")
LEGACY_ROOT = os.environ.get("PRAESIDIUM_LEGACY_ROOT", "/mnt/legacy")
LEGACY_PREFIX = "Legacy"

router = APIRouter(prefix="/docs", tags=["webdav"])


# ── Helpers ──────────────────────────────────────────────────────────────────

def _tenant_root(tenant_id: str) -> Path:
    """Resolve the on-disk root for this tenant's matter tree."""
    return Path(STORAGE_ROOT) / tenant_id.strip() / "matters"


def _is_legacy_path(webdav_path: str) -> bool:
    """Check if a WebDAV path targets the Legacy virtual folder."""
    clean = webdav_path.strip("/")
    return clean == LEGACY_PREFIX or clean.startswith(LEGACY_PREFIX + "/")


def _resolve_path(tenant_id: str, webdav_path: str) -> Tuple[Optional[Path], bool]:
    """
    Resolve a WebDAV path to an on-disk path, with traversal protection.
    webdav_path is relative (no leading /docs/).
    Returns (resolved_path, is_legacy).
    Returns (None, False) if path escapes allowed roots.
    """
    clean = webdav_path.strip("/")

    # Virtual root — return the tenant matter root (legacy shows as virtual child)
    if not clean:
        return _tenant_root(tenant_id), False

    # Legacy subtree
    if _is_legacy_path(clean):
        legacy_root = Path(LEGACY_ROOT).resolve()
        # Strip the "Legacy" prefix to get the sub-path
        sub = clean[len(LEGACY_PREFIX):].strip("/")
        if not sub:
            resolved = legacy_root
        else:
            resolved = (legacy_root / sub).resolve()
        # Traversal protection
        try:
            resolved.relative_to(legacy_root)
        except ValueError:
            logger.warning(f"Legacy path traversal attempt: {webdav_path}")
            return None, False
        return resolved, True

    # Praesidium matter tree
    root = _tenant_root(tenant_id)
    resolved = (root / clean).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        logger.warning(f"Path traversal attempt: {webdav_path}")
        return None, False
    return resolved, False


def _assert_writable(is_legacy: bool, method: str):
    """Raise 403 if a write operation is attempted on the legacy tree."""
    if is_legacy:
        raise HTTPException(
            status_code=403,
            detail=f"{method} not permitted on Legacy (read-only)",
        )


def _stat_to_props(p: Path) -> dict:
    """Build WebDAV property dict from a filesystem path."""
    try:
        st = p.stat()
    except OSError:
        return {}
    is_dir = stat.S_ISDIR(st.st_mode)
    return {
        "name": p.name,
        "is_collection": is_dir,
        "content_length": 0 if is_dir else st.st_size,
        "content_type": "httpd/unix-directory" if is_dir else (
            mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        ),
        "last_modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        ),
        "creation_date": datetime.fromtimestamp(st.st_ctime, tz=timezone.utc).isoformat(),
    }


def _virtual_legacy_props() -> dict:
    """Build props for the virtual 'Legacy' folder entry."""
    return {
        "name": LEGACY_PREFIX,
        "is_collection": True,
        "content_length": 0,
        "content_type": "httpd/unix-directory",
        "last_modified": datetime.now(tz=timezone.utc).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        ),
        "creation_date": datetime.now(tz=timezone.utc).isoformat(),
    }


def _propfind_xml(href: str, props: dict, children: list = None) -> str:
    """Build a PROPFIND multistatus XML response."""
    ns = 'xmlns:D="DAV:"'
    responses = []

    def _response_xml(h, p):
        rt = "<D:collection/>" if p.get("is_collection") else ""
        return f"""<D:response>
  <D:href>{h}</D:href>
  <D:propstat>
    <D:prop>
      <D:displayname>{p.get('name', '')}</D:displayname>
      <D:getcontentlength>{p.get('content_length', 0)}</D:getcontentlength>
      <D:getcontenttype>{p.get('content_type', 'application/octet-stream')}</D:getcontenttype>
      <D:getlastmodified>{p.get('last_modified', '')}</D:getlastmodified>
      <D:creationdate>{p.get('creation_date', '')}</D:creationdate>
      <D:resourcetype>{rt}</D:resourcetype>
    </D:prop>
    <D:status>HTTP/1.1 200 OK</D:status>
  </D:propstat>
</D:response>"""

    responses.append(_response_xml(href, props))

    if children:
        for child_href, child_props in children:
            responses.append(_response_xml(child_href, child_props))

    body = "\n".join(responses)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<D:multistatus {ns}>\n{body}\n</D:multistatus>'


# ── Auth dependency ──────────────────────────────────────────────────────────

async def _get_webdav_user(request: Request) -> dict:
    """
    Extract and validate Basic auth credentials.
    Returns dict with tenant_id, user_id, username.
    """
    from modules.dms.services.webdav_auth import webdav_authenticate

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Praesidium DMS"'},
        )

    import base64
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, detail="Malformed credentials")

    hostname = request.headers.get("Host", "").split(":")[0]
    result = await webdav_authenticate(hostname, username, password)

    if not result:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Praesidium DMS"'},
        )

    tenant_id, user_id, uname = result
    return {"tenant_id": tenant_id, "user_id": user_id, "username": uname}


# ── OPTIONS (WebDAV discovery) ───────────────────────────────────────────────

@router.api_route("/{path:path}", methods=["OPTIONS"])
@router.api_route("/", methods=["OPTIONS"])
async def webdav_options(request: Request):
    return Response(
        status_code=200,
        headers={
            "Allow": "OPTIONS, GET, HEAD, PUT, DELETE, MKCOL, PROPFIND, COPY, MOVE",
            "DAV": "1, 2",
            "MS-Author-Via": "DAV",
        },
    )


# ── PROPFIND (directory listing / file info) ─────────────────────────────────

@router.api_route("/{path:path}", methods=["PROPFIND"])
@router.api_route("/", methods=["PROPFIND"])
async def webdav_propfind(request: Request, path: str = "", user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    depth = request.headers.get("Depth", "1")
    clean = path.strip("/")

    resolved, is_legacy = _resolve_path(tenant_id, path)
    if not resolved or not resolved.exists():
        raise HTTPException(status_code=404)

    href_base = f"/docs/{path}".rstrip("/") or "/docs"
    props = _stat_to_props(resolved)

    # If we're at the virtual root, override the name
    if not clean:
        props["name"] = ""

    children = []
    if resolved.is_dir() and depth != "0":
        try:
            for child in sorted(resolved.iterdir()):
                if child.name.startswith("."):
                    continue
                child_href = f"{href_base}/{child.name}"
                children.append((child_href, _stat_to_props(child)))
        except PermissionError:
            pass

        # At the virtual root, inject the Legacy folder entry
        if not clean:
            legacy_href = f"{href_base}/{LEGACY_PREFIX}"
            children.append((legacy_href, _virtual_legacy_props()))

    xml = _propfind_xml(href_base, props, children)
    return Response(
        content=xml,
        status_code=207,
        media_type="application/xml; charset=utf-8",
    )


# ── GET (download file) ─────────────────────────────────────────────────────

@router.get("/{path:path}")
async def webdav_get(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    resolved, is_legacy = _resolve_path(tenant_id, path)

    if not resolved or not resolved.exists():
        raise HTTPException(status_code=404)

    if resolved.is_dir():
        # Return a simple HTML listing for browsers
        items = []
        clean = path.strip("/")
        for child in sorted(resolved.iterdir()):
            if child.name.startswith("."):
                continue
            slash = "/" if child.is_dir() else ""
            items.append(f'<li><a href="{child.name}{slash}">{child.name}{slash}</a></li>')
        # Inject Legacy link at virtual root
        if not clean:
            items.append(f'<li><a href="{LEGACY_PREFIX}/">{LEGACY_PREFIX}/</a></li>')
        html = f"<html><body><h1>Index of /docs/{path}</h1><ul>{''.join(items)}</ul></body></html>"
        return Response(content=html, media_type="text/html")

    content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"

    def file_iterator():
        with open(resolved, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        file_iterator(),
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{resolved.name}"',
            "Content-Length": str(resolved.stat().st_size),
        },
    )


# ── PUT (upload / overwrite file) ────────────────────────────────────────────

@router.put("/{path:path}")
async def webdav_put(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    resolved, is_legacy = _resolve_path(tenant_id, path)
    _assert_writable(is_legacy, "PUT")

    if not resolved:
        raise HTTPException(status_code=403, detail="Invalid path")

    # Ensure parent directory exists
    resolved.parent.mkdir(parents=True, exist_ok=True)

    body = await request.body()
    existed = resolved.exists()
    with open(resolved, "wb") as f:
        f.write(body)

    logger.info(f"WebDAV PUT {path} ({len(body)} bytes) by {user['username']}")
    return Response(status_code=204 if existed else 201)


# ── DELETE ───────────────────────────────────────────────────────────────────

@router.delete("/{path:path}")
async def webdav_delete(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    resolved, is_legacy = _resolve_path(tenant_id, path)
    _assert_writable(is_legacy, "DELETE")

    if not resolved or not resolved.exists():
        raise HTTPException(status_code=404)

    if resolved.is_dir():
        if not is_legacy:
            from modules.dms.services.path_safety import assert_deletable_subpath
            assert_deletable_subpath(str(resolved), tenant_id, op="WebDAV directory delete")
        shutil.rmtree(resolved)
    else:
        resolved.unlink()

    logger.info(f"WebDAV DELETE {path} by {user['username']}")
    return Response(status_code=204)


# ── MKCOL (create directory) ─────────────────────────────────────────────────

@router.api_route("/{path:path}", methods=["MKCOL"])
async def webdav_mkcol(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    resolved, is_legacy = _resolve_path(tenant_id, path)
    _assert_writable(is_legacy, "MKCOL")

    if not resolved:
        raise HTTPException(status_code=403)

    if resolved.exists():
        raise HTTPException(status_code=405, detail="Already exists")

    resolved.mkdir(parents=True, exist_ok=True)
    logger.info(f"WebDAV MKCOL {path} by {user['username']}")
    return Response(status_code=201)


# ── COPY / MOVE ─────────────────────────────────────────────────────────────

@router.api_route("/{path:path}", methods=["COPY", "MOVE"])
async def webdav_copy_move(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    is_move = request.method == "MOVE"

    destination = request.headers.get("Destination", "")
    if not destination:
        raise HTTPException(status_code=400, detail="Destination header required")

    # Parse destination path — strip scheme/host/prefix
    from urllib.parse import urlparse
    parsed = urlparse(destination)
    dest_path = parsed.path
    if dest_path.startswith("/docs/"):
        dest_path = dest_path[6:]

    src, src_legacy = _resolve_path(tenant_id, path)
    dst, dst_legacy = _resolve_path(tenant_id, dest_path)

    # MOVE from legacy = write to legacy source (forbidden)
    # MOVE/COPY to legacy dest = write to legacy (forbidden)
    if is_move and src_legacy:
        _assert_writable(True, "MOVE (source)")
    _assert_writable(dst_legacy, request.method + " (destination)")

    if not src or not src.exists():
        raise HTTPException(status_code=404)
    if not dst:
        raise HTTPException(status_code=403)

    dst.parent.mkdir(parents=True, exist_ok=True)

    overwrite = request.headers.get("Overwrite", "T") == "T"
    status = 201
    if dst.exists():
        if not overwrite:
            raise HTTPException(status_code=412)
        status = 204

    if is_move:
        shutil.move(str(src), str(dst))
        logger.info(f"WebDAV MOVE {path} -> {dest_path} by {user['username']}")
    else:
        if src.is_dir():
            shutil.copytree(str(src), str(dst), dirs_exist_ok=True)
        else:
            shutil.copy2(str(src), str(dst))
        logger.info(f"WebDAV COPY {path} -> {dest_path} by {user['username']}")

    return Response(status_code=status)


# ── HEAD ─────────────────────────────────────────────────────────────────────

@router.head("/{path:path}")
async def webdav_head(request: Request, path: str, user: dict = Depends(_get_webdav_user)):
    tenant_id = user["tenant_id"]
    resolved, is_legacy = _resolve_path(tenant_id, path)

    if not resolved or not resolved.exists():
        raise HTTPException(status_code=404)

    content_type = "httpd/unix-directory" if resolved.is_dir() else (
        mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
    )
    headers = {"Content-Type": content_type}
    if not resolved.is_dir():
        headers["Content-Length"] = str(resolved.stat().st_size)

    return Response(status_code=200, headers=headers)
