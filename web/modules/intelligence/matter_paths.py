"""matter_paths.py — shared matter <-> disk-path resolver (v1.1 §A3).

`dms_documents` has no matter_id; documents are located only by their on-disk
`file_path` under /mnt/praesidium/<tenant>/matters/... . Two columns describe a
matter's location and they DISAGREE across the corpus:

  * matters.folder_path        — display path, often stale or NULL (918/1527 null)
  * matter_folders.disk_root   — the real ingest root (908/1527 present; 506 of
                                 those disagree with folder_path)

Scoping by folder_path alone (what the classifier and notice extractor did)
silently finds 0 documents for most matters — e.g. Victory's folder_path is
"Weir Brothers/Victory Companies" but its 459 docs live under "Weir/Victory".

This module is the single place that knows how to turn a matter into the set of
LIKE prefixes that cover its documents (disk_root UNION folder_path), and the
reverse (a file_path back to its matter). Every consumer scopes the same way.
"""
from __future__ import annotations

from sqlalchemy import text as sa_text

DMS_ROOT = "/mnt/praesidium"


async def disk_prefixes(s, tid, matter_id):
    """All distinct '<path>/%' LIKE prefixes that cover a matter's documents.

    Canonical disk_root(s) first, then the folder_path-derived prefix as a
    fallback/extra. Empty list => matter has no resolvable location.
    """
    t = tid.strip()
    prefixes = []
    rows = (await s.execute(sa_text(
        "SELECT disk_root FROM matter_folders "
        "WHERE matter_id = CAST(:m AS uuid) AND COALESCE(disk_root,'') <> ''"),
        {"m": matter_id})).mappings().fetchall()
    for r in rows:
        prefixes.append(r["disk_root"].rstrip("/") + "/%")
    fp = (await s.execute(sa_text(
        "SELECT folder_path FROM matters WHERE id = CAST(:m AS uuid) "
        "AND TRIM(tenant_id) = TRIM(:t)"),
        {"m": matter_id, "t": tid})).scalar()
    if fp:
        prefixes.append(f"{DMS_ROOT}/{t}/matters/{fp.rstrip('/')}/%")
    seen, out = set(), []
    for p in prefixes:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


async def matter_for_path(s, tid, file_path):
    """Reverse lookup: the matter that owns a dms file_path.

    Prefer the longest matching disk_root (most specific), fall back to the
    longest matching folder_path. Both are tenant-safe (disk_root embeds the
    tenant id in the path; folder_path query is tenant-scoped).
    """
    row = (await s.execute(sa_text(
        "SELECT matter_id::text id FROM matter_folders "
        "WHERE COALESCE(disk_root,'') <> '' AND :fp LIKE disk_root || '/%' "
        "ORDER BY length(disk_root) DESC LIMIT 1"),
        {"fp": file_path})).mappings().fetchone()
    if row:
        return row["id"]
    row = (await s.execute(sa_text(
        "SELECT id::text id FROM matters "
        "WHERE TRIM(tenant_id) = TRIM(:t) AND COALESCE(folder_path,'') <> '' "
        "  AND :fp LIKE '%/matters/' || folder_path || '/%' "
        "ORDER BY length(folder_path) DESC LIMIT 1"),
        {"t": tid, "fp": file_path})).mappings().fetchone()
    return row["id"] if row else None
