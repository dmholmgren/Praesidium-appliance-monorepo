"""
reclaim_collection_storage.py -- free the on-disk files of a removed eDiscovery
collection.

Operator companion to teardown_collection: that clears the database rows, this
clears the bytes. Kept separate and explicit because clearing files is
destructive. Walks the collection's storage subtree bottom-up, unlinking files
then removing the emptied directories (no shell, no shutil -- plain os ops with
hard guards).

Safety:
  * resolves the real path and accepts ONLY paths strictly under
    /mnt/ediscovery that are at least <tenant>/<matter>/<collection> deep --
    never a tenant root, matter root, or the storage root itself;
  * if the ediscovery_collections row still exists with a non-'failed' status,
    refuses unless force=True (never touch a live collection's files);
  * dry_run reports what it would free without touching anything.

Pass storage_path explicitly when the collection row (and its storage_path) is
already gone -- the usual case after the UI "Remove" action drops the row.

CLI:
  python -m modules.ediscovery.jobs.reclaim_collection_storage \
      [--collection <uuid>] [--storage-path <abs path>] [--force] [--dry-run]
"""
import os
import argparse
import logging
import psycopg2
from urllib.parse import urlparse

logger = logging.getLogger(__name__)
STORAGE_ROOT = "/mnt/ediscovery"


def _connect():
    raw = os.environ.get("DATABASE_URL", "")
    for pref in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(pref):
            raw = "postgresql://" + raw[len(pref):]
            break
    u = urlparse(raw)
    return psycopg2.connect(
        dbname=u.path.lstrip("/") or "praesidium",
        user=u.username, password=u.password,
        host=u.hostname or "127.0.0.1", port=u.port or 5432,
    )


def _lookup(collection_id):
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT status, storage_path FROM ediscovery_collections "
                    "WHERE id=%s", (str(collection_id),))
        row = cur.fetchone()
        return (row[0], row[1]) if row else (None, None)
    finally:
        conn.close()


def _safe_under_storage(path):
    """True only for a real path strictly inside STORAGE_ROOT, >=3 levels deep
    (tenant/matter/collection)."""
    if not path:
        return False
    rp = os.path.realpath(path)
    root = os.path.realpath(STORAGE_ROOT)
    if rp == root or not rp.startswith(root + os.sep):
        return False
    parts = [p for p in rp[len(root) + 1:].split(os.sep) if p]
    return len(parts) >= 3


def reclaim_collection_storage(collection_id=None, storage_path=None,
                               force=False, dry_run=False):
    """Free the on-disk storage of a removed collection. Returns a dict."""
    status = None
    if collection_id and not storage_path:
        status, storage_path = _lookup(collection_id)
        if storage_path is None:
            return {"error": "collection row gone; pass storage_path explicitly",
                    "collection_id": str(collection_id)}
    elif collection_id:
        status, _ = _lookup(collection_id)

    if not storage_path:
        return {"error": "no storage_path resolved"}
    if not _safe_under_storage(storage_path):
        return {"error": "unsafe path (must be >=3 levels under %s)" % STORAGE_ROOT,
                "storage_path": storage_path}
    if status and status != "failed" and not force:
        return {"error": "collection row present, status=%s; refusing without "
                "force" % status, "storage_path": storage_path}

    base = os.path.realpath(storage_path)
    if not os.path.isdir(base):
        return {"storage_path": base, "files": 0, "bytes": 0, "note": "nothing on disk"}

    files = bytes_freed = dirs = 0
    for root, dnames, fnames in os.walk(base, topdown=False):
        for fn in fnames:
            fp = os.path.join(root, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            if not dry_run:
                try:
                    os.remove(fp)
                except OSError as e:
                    logger.warning("could not unlink %s: %s", fp, e)
                    continue
            files += 1
            bytes_freed += sz
        for dn in dnames:
            if not dry_run:
                try:
                    os.rmdir(os.path.join(root, dn))
                except OSError:
                    pass
            dirs += 1
    if not dry_run:
        try:
            os.rmdir(base)
        except OSError:
            pass
    res = {"storage_path": base, "files": files, "dirs": dirs,
           "bytes": bytes_freed, "mb": round(bytes_freed / (1024 * 1024), 1),
           "dry_run": dry_run}
    logger.info("reclaim %s", res)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collection")
    ap.add_argument("--storage-path")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(reclaim_collection_storage(
        collection_id=a.collection, storage_path=a.storage_path,
        force=a.force, dry_run=a.dry_run))


if __name__ == "__main__":
    main()
