"""
Path-safety guards for destructive filesystem operations on the matter tree.

Invariant: a recursive directory delete (shutil.rmtree) may only target a path
that lives STRICTLY INSIDE a matter. The matters root, a client/uuid directory,
and a matter root itself are NEVER recursively deletable. This prevents a single
bad folder-delete or WebDAV DELETE from blowing out an entire matter tree.

Two on-disk layouts exist, so callers pass the minimum safe depth (segments
below /mnt/praesidium/{tenant}/matters):
  - named layout  {client}/{matter}/{sub}    -> matter root at depth 2, use min_depth=3
  - uuid  layout  {matter_uuid}/{sub}         -> matter root at depth 1, use min_depth=2

Patent Pending.
"""
import os
from fastapi import HTTPException

STORAGE_ROOT = os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium")


def matters_root(tenant_id: str) -> str:
    return os.path.realpath(
        os.path.join(STORAGE_ROOT, (tenant_id or "").strip(), "matters")
    )


def is_deletable_subpath(path: str, tenant_id: str, min_depth: int = 3) -> bool:
    if not path:
        return False
    base = matters_root(tenant_id)
    real = os.path.realpath(path)
    rel = os.path.relpath(real, base)
    if rel == "." or rel.startswith(".."):
        return False
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    return len(parts) >= min_depth


def assert_deletable_subpath(path: str, tenant_id: str, op: str = "delete",
                             min_depth: int = 3) -> None:
    if not is_deletable_subpath(path, tenant_id, min_depth):
        raise HTTPException(
            status_code=403,
            detail=(
                f"Refusing recursive {op}: '{path}' is a matter root or shared "
                "directory. Only subfolders/files inside a matter may be deleted."
            ),
        )
