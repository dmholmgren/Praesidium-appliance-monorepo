"""
LocalMountStorageAdapter — StorageService implementation for locally-mounted
CIFS shares. Replaces CIFSStorageAdapter (which proxied through the FBRG-01
file bridge). All operations are direct pathlib calls against bind-mounted
filesystem paths — no HTTP, no bridge.

Mount topology (production defaults, overridable via env vars):

    /mnt/clients    (read-only)   legacy HJMM client share
    /mnt/docsend    (read-only)   legacy scan output
    /mnt/praesidium (read-write)  all new tenant data
    /mnt/ediscovery (read-write)  eDiscovery corpora, all tenants

Path prefix routing (what callers pass in):

    clients/foo/bar.pdf          → CLIENTS_ROOT/foo/bar.pdf              (ro)
    docsend/x/y.pdf              → DOCSEND_ROOT/x/y.pdf                  (ro)
    praesidium/matters/123/a.doc → PRAESIDIUM_ROOT/{tenant_id}/matters/123/a.doc
    ediscovery/col-456/a.pdf     → EDISCOVERY_ROOT/{tenant_id}/col-456/a.pdf
    foo/bar.pdf (no prefix)      → CLIENTS_ROOT/foo/bar.pdf              (ro)

Legacy default (no prefix → clients) preserves compatibility with existing
dms_documents.file_path rows that were written under the bridge.

Tenant injection happens inside the adapter for praesidium/ and ediscovery/
prefixes. Legacy mounts (clients/, docsend/) do not get a tenant prefix
because their data predates multi-tenancy.

Writes to read-only mounts raise StorageError before touching the filesystem.
The CIFS mounts are also kernel-level `ro`; this is belt and suspenders.
"""

import hashlib
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.services.storage import StorageService, FileInfo, StorageError


# ── Mount configuration (env-overridable, production defaults) ───────────────

CLIENTS_ROOT    = Path(os.environ.get("CIFS_CLIENTS_MOUNT",    "/mnt/clients"))
DOCSEND_ROOT    = Path(os.environ.get("CIFS_DOCSEND_MOUNT",    "/mnt/docsend"))
PRAESIDIUM_ROOT = Path(os.environ.get("CIFS_PRAESIDIUM_MOUNT", "/mnt/praesidium"))
EDISCOVERY_ROOT = Path(os.environ.get("CIFS_EDISCOVERY_MOUNT", "/mnt/ediscovery"))

# Read-only mounts — writes to these raise StorageError
_READONLY_ROOTS = frozenset({CLIENTS_ROOT.resolve(), DOCSEND_ROOT.resolve()})


# ── MIME type guessing (matches bridge behavior) ─────────────────────────────

_EXTRA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".msg":  "application/vnd.ms-outlook",
    ".eml":  "message/rfc822",
}

def _guess_content_type(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    if ext in _EXTRA_TYPES:
        return _EXTRA_TYPES[ext]
    guess, _ = mimetypes.guess_type(name)
    return guess or "application/octet-stream"


# ── Path resolution ──────────────────────────────────────────────────────────

def _resolve_mount(rel_path: str, tenant_id: str) -> tuple[Path, Path, bool]:
    """
    Route a caller-supplied path to its filesystem location.

    Returns (resolved_absolute_path, mount_root, is_readonly).
    Raises StorageError for path traversal, missing tenant_id on writable
    mounts, or other resolution failures.
    """
    clean = (rel_path or "").lstrip("/")

    if clean.startswith("clients/") or clean == "clients":
        mount_root = CLIENTS_ROOT
        subpath = clean[len("clients"):].lstrip("/")
        readonly = True

    elif clean.startswith("docsend/") or clean == "docsend":
        mount_root = DOCSEND_ROOT
        subpath = clean[len("docsend"):].lstrip("/")
        readonly = True

    elif clean.startswith("praesidium/") or clean == "praesidium":
        tid = (tenant_id or "").strip()
        if not tid:
            raise StorageError("tenant_id required for praesidium/ paths")
        mount_root = PRAESIDIUM_ROOT / tid
        subpath = clean[len("praesidium"):].lstrip("/")
        readonly = False

    elif clean.startswith("ediscovery/") or clean == "ediscovery":
        tid = (tenant_id or "").strip()
        if not tid:
            raise StorageError("tenant_id required for ediscovery/ paths")
        mount_root = EDISCOVERY_ROOT / tid
        subpath = clean[len("ediscovery"):].lstrip("/")
        readonly = False

    else:
        # Legacy default: no prefix → clients (bridge-compatible behavior)
        mount_root = CLIENTS_ROOT
        subpath = clean
        readonly = True

    resolved = (mount_root / subpath).resolve() if subpath else mount_root.resolve()

    # Path traversal check — resolved must remain under mount_root
    try:
        resolved.relative_to(mount_root.resolve())
    except ValueError:
        raise StorageError(f"Path traversal denied: {rel_path}")

    return resolved, mount_root, readonly


def _file_info(abs_path: Path, caller_path: str) -> FileInfo:
    """Build a FileInfo from a resolved filesystem path.
    caller_path is the path the caller passed in (prefixed form), preserved
    so the FileInfo.path round-trips back to the caller."""
    try:
        st = abs_path.stat()
        size = st.st_size if abs_path.is_file() else 0
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        size = 0
        mtime = None

    return FileInfo(
        path=caller_path,
        name=abs_path.name,
        size=size,
        content_type=_guess_content_type(abs_path.name),
        modified_at=mtime,
        checksum=None,
        is_directory=abs_path.is_dir(),
        metadata={},
    )


# ── Adapter ──────────────────────────────────────────────────────────────────

class LocalMountStorageAdapter(StorageService):
    """
    StorageService implementation reading/writing directly from bind-mounted
    CIFS shares. No network, no bridge, no httpx.

    Callers pass logical paths with mount prefixes (clients/, docsend/,
    praesidium/, ediscovery/). The adapter resolves to filesystem paths,
    injects tenant_id for writable mounts, enforces read-only restrictions,
    and protects against path traversal.
    """

    def __init__(self):
        # Mount roots are module-level (imported once). No per-instance state.
        pass

    # ── Core operations ──────────────────────────────────────────────────────

    async def upload(
        self,
        tenant_id: str,
        path: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> FileInfo:
        resolved, _, readonly = _resolve_mount(path, tenant_id)
        if readonly:
            raise StorageError(f"Mount is read-only: cannot upload to {path}")

        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(content)
        return _file_info(resolved, path)

    async def download(self, tenant_id: str, path: str) -> bytes:
        resolved, _, _ = _resolve_mount(path, tenant_id)
        if not resolved.exists():
            raise StorageError(f"File not found: {path}")
        if not resolved.is_file():
            raise StorageError(f"Not a file: {path}")
        return resolved.read_bytes()

    async def delete(self, tenant_id: str, path: str) -> bool:
        resolved, _, readonly = _resolve_mount(path, tenant_id)
        if readonly:
            raise StorageError(f"Mount is read-only: cannot delete {path}")
        if not resolved.exists():
            return False
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
        return True

    async def list(
        self,
        tenant_id: str,
        prefix: str = "",
        recursive: bool = False,
    ) -> list[FileInfo]:
        try:
            resolved, _, _ = _resolve_mount(prefix, tenant_id)
        except StorageError:
            return []

        if not resolved.exists() or not resolved.is_dir():
            return []

        # Preserve caller's prefix so returned paths round-trip correctly
        prefix_clean = (prefix or "").strip("/")

        results: list[FileInfo] = []
        iterator = resolved.rglob("*") if recursive else sorted(resolved.iterdir())

        for item in iterator:
            try:
                # Build the caller-facing path: prefix + relative-from-resolved
                rel = item.relative_to(resolved)
                caller_path = f"{prefix_clean}/{rel}" if prefix_clean else str(rel)
                results.append(_file_info(item, caller_path))
            except (OSError, ValueError):
                continue

        return results

    async def get_url(
        self,
        tenant_id: str,
        path: str,
        expires: int = 3600,
    ) -> str:
        """
        Return a browser-accessible URL for the file. Since we're no longer
        serving via the bridge, callers route through a local FastAPI endpoint
        that streams from the mount. That route is outside this adapter's
        concern — we just produce the URL shape the app already recognizes.
        """
        # URL-encode would be ideal but stays minimal to match bridge behavior.
        # Consumers of this URL should expect to need their own encoding.
        return f"/dms/legacy/download?path={path}"

    async def create_folder(self, tenant_id: str, path: str) -> bool:
        resolved, _, readonly = _resolve_mount(path, tenant_id)
        if readonly:
            raise StorageError(f"Mount is read-only: cannot create folder at {path}")
        resolved.mkdir(parents=True, exist_ok=True)
        return True

    async def move(
        self,
        tenant_id: str,
        src_path: str,
        dst_path: str,
    ) -> FileInfo:
        src_resolved, _, src_readonly = _resolve_mount(src_path, tenant_id)
        dst_resolved, _, dst_readonly = _resolve_mount(dst_path, tenant_id)

        if src_readonly:
            raise StorageError(f"Source mount is read-only: cannot move from {src_path}")
        if dst_readonly:
            raise StorageError(f"Destination mount is read-only: cannot move to {dst_path}")
        if not src_resolved.exists():
            raise StorageError(f"Source not found: {src_path}")

        dst_resolved.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_resolved), str(dst_resolved))
        return _file_info(dst_resolved, dst_path)

    async def exists(self, tenant_id: str, path: str) -> bool:
        try:
            resolved, _, _ = _resolve_mount(path, tenant_id)
            return resolved.exists()
        except StorageError:
            return False

    async def stat(self, tenant_id: str, path: str) -> FileInfo:
        resolved, _, _ = _resolve_mount(path, tenant_id)
        if not resolved.exists():
            raise StorageError(f"File not found: {path}")
        return _file_info(resolved, path)

    async def checksum(self, tenant_id: str, path: str) -> str:
        resolved, _, _ = _resolve_mount(path, tenant_id)
        if not resolved.exists():
            raise StorageError(f"File not found: {path}")
        if not resolved.is_file():
            raise StorageError(f"Not a file: {path}")

        h = hashlib.sha256()
        with open(resolved, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()

    async def close(self):
        """No-op: no persistent connections to release."""
        pass


# ── Factory ──────────────────────────────────────────────────────────────────

_singleton: Optional[LocalMountStorageAdapter] = None


def get_storage_adapter() -> LocalMountStorageAdapter:
    """
    Return the process-wide storage adapter singleton.
    Future: select adapter from tenants.storage_adapter column when we
    support multiple storage backends per tenant (S3, SharePoint, etc.).
    """
    global _singleton
    if _singleton is None:
        _singleton = LocalMountStorageAdapter()
    return _singleton
