#!/usr/bin/env python3
"""
jobs/reconcile_praesidium.py
Reconcile /mnt/praesidium disk contents against dms_documents DB.
Finds orphans (on disk, not in DB) and ghosts (in DB, not on disk).
Updates dms_documents.verified_at for confirmed files.

Usage:
  python3 jobs/reconcile_praesidium.py --tenant 986c0fee-...

Writes results to praesidium_reconciliation table.
"""
import argparse, hashlib, logging, os, sys, time, uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")
import psycopg2
import psycopg2.extras

log = logging.getLogger("reconcile")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PRAESIDIUM_ROOT = "/mnt/praesidium"


def get_db_url():
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def hash_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def ensure_table(conn):
    """Verify table exists (DDL applied separately by superuser)."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM praesidium_reconciliation LIMIT 0")
    log.info("Schema verified")


def reconcile(conn, tenant_id, run_id, verify_hash=False):
    tid = tenant_id.strip()
    matters_dir = os.path.join(PRAESIDIUM_ROOT, tid, "matters")
    
    if not os.path.isdir(matters_dir):
        log.error("Matters dir not found: %s", matters_dir)
        return

    # 1. Load all DB docs into a dict keyed by file_path
    log.info("Loading dms_documents from DB...")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT id, file_path, file_hash, file_size_bytes, folder_root
            FROM dms_documents
            WHERE TRIM(tenant_id) = %s
              AND file_path LIKE %s
        """, (tid, PRAESIDIUM_ROOT + "%"))
        db_docs = {}
        for row in cur:
            db_docs[row["file_path"]] = row
    log.info("  %d DB documents loaded", len(db_docs))

    # 2. Walk disk
    log.info("Walking %s ...", matters_dir)
    disk_files = {}
    start = time.time()
    file_count = 0
    for dirpath, dirnames, filenames in os.walk(matters_dir):
        for fname in filenames:
            fpath = os.path.join(dirpath, fname)
            try:
                st = os.stat(fpath)
                disk_files[fpath] = {
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                }
                file_count += 1
                if file_count % 10000 == 0:
                    log.info("  ... scanned %d files", file_count)
            except (OSError, PermissionError):
                pass
    elapsed = time.time() - start
    log.info("  %d disk files scanned in %.1fs", len(disk_files), elapsed)

    # 3. Compare
    orphans = []   # on disk, not in DB
    ghosts = []    # in DB, not on disk
    mismatches = []  # size mismatch
    verified = []  # confirmed match

    db_paths = set(db_docs.keys())
    disk_paths = set(disk_files.keys())

    for path in disk_paths - db_paths:
        info = disk_files[path]
        h = hash_file(path) if verify_hash else None
        orphans.append({
            "file_path": path,
            "issue_type": "orphan",
            "file_size_bytes": info["size"],
            "file_hash": h,
            "folder_root": os.path.dirname(path),
        })

    for path in db_paths - disk_paths:
        doc = db_docs[path]
        ghosts.append({
            "file_path": path,
            "issue_type": "ghost",
            "file_size_bytes": doc["file_size_bytes"],
            "file_hash": doc["file_hash"],
            "db_doc_id": doc["id"],
            "folder_root": doc["folder_root"],
        })

    for path in db_paths & disk_paths:
        doc = db_docs[path]
        disk = disk_files[path]
        if doc["file_size_bytes"] and disk["size"] != doc["file_size_bytes"]:
            mismatches.append({
                "file_path": path,
                "issue_type": "size_mismatch",
                "file_size_bytes": disk["size"],
                "db_doc_id": doc["id"],
                "folder_root": doc["folder_root"],
                "details": {"db_size": doc["file_size_bytes"], "disk_size": disk["size"]},
            })
        else:
            verified.append(str(doc["id"]))

    log.info("Results: %d orphans, %d ghosts, %d mismatches, %d verified",
             len(orphans), len(ghosts), len(mismatches), len(verified))

    # 4. Write issues to reconciliation table
    issues = orphans + ghosts + mismatches
    if issues:
        log.info("Writing %d issues to praesidium_reconciliation...", len(issues))
        with conn.cursor() as cur:
            for issue in issues:
                cur.execute("""
                    INSERT INTO praesidium_reconciliation
                        (tenant_id, run_id, file_path, issue_type, file_size_bytes,
                         file_hash, db_doc_id, folder_root, details)
                    VALUES (%s, %s::uuid, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    tid, run_id, issue["file_path"], issue["issue_type"],
                    issue.get("file_size_bytes"), issue.get("file_hash"),
                    issue.get("db_doc_id"), issue.get("folder_root"),
                    psycopg2.extras.Json(issue.get("details")) if issue.get("details") else None,
                ))
        conn.commit()

    # 5. Stamp verified_at on confirmed docs
    if verified:
        log.info("Stamping verified_at on %d docs...", len(verified))
        batch_size = 500
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            for i in range(0, len(verified), batch_size):
                batch = verified[i:i+batch_size]
                cur.execute("""
                    UPDATE dms_documents SET verified_at = %s
                    WHERE id = ANY(%s::uuid[])
                """, (now, batch))
        conn.commit()

    # 6. Summary
    log.info("=== Reconciliation Summary (run %s) ===", run_id)
    log.info("  Disk files:   %d", len(disk_files))
    log.info("  DB documents: %d", len(db_docs))
    log.info("  Verified:     %d", len(verified))
    log.info("  Orphans:      %d (on disk, not in DB)", len(orphans))
    log.info("  Ghosts:       %d (in DB, not on disk)", len(ghosts))
    log.info("  Mismatches:   %d (size differs)", len(mismatches))

    return {
        "run_id": run_id,
        "disk_files": len(disk_files),
        "db_documents": len(db_docs),
        "verified": len(verified),
        "orphans": len(orphans),
        "ghosts": len(ghosts),
        "mismatches": len(mismatches),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--verify-hash", action="store_true", help="Compute SHA-256 for orphan files")
    args = parser.parse_args()

    run_id = str(uuid.uuid4())
    log.info("=== Praesidium Reconciliation ===")
    log.info("Tenant: %s", args.tenant)
    log.info("Run ID: %s", run_id)

    conn = psycopg2.connect(get_db_url())
    try:
        ensure_table(conn)
        result = reconcile(conn, args.tenant, run_id, verify_hash=args.verify_hash)
    finally:
        conn.close()
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
