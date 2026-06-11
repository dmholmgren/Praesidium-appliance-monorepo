#!/usr/bin/env python3
"""
jobs/inventory_legacy_filesystem.py
Filesystem Inventory Agent — Phase 1 (catalog + classify + match)

Walks the legacy QNAP mount, catalogs every file and folder into
file_inventory, classifies each entry (document / ediscovery_archive /
ediscovery_production / skip), then runs a matching pass against the
canonical clients and matters tables to propose folder→client/matter
mappings and set priority based on billing activity.

Usage (inside praesidium-web container):
    python3 jobs/inventory_legacy_filesystem.py \
        --root "/mnt/legacy-qnap/D Drive Backup/Clients" \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f

The script is idempotent — each run creates a new scan_run_id.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("inventory_agent")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ── Classification rules ──────────────────────────────────────────────────────

SKIP_EXTENSIONS = {
    ".iso", ".vmdk", ".vhd", ".vhdx", ".ova", ".ovf",
    ".tmp", ".bak", ".swp", ".lnk", ".db", ".ini",
    ".log", ".cache",
}

EDISCOVERY_ARCHIVE_EXTENSIONS = {".pst", ".ost", ".mbox", ".eml", ".msg"}
EDISCOVERY_LOAD_FILE_EXTENSIONS = {".dat", ".opt", ".lfp", ".dii"}
PRODUCTION_ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz"}

PARSEABLE_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".rtf", ".txt", ".xlsx", ".xls",
    ".csv", ".pptx", ".ppt", ".htm", ".html", ".xml",
    ".tif", ".tiff", ".png", ".jpg", ".jpeg",
}

SKIP_FOLDERS = {
    "@Recycle", "$RECYCLE.BIN", "System Volume Information",
    "__MACOSX", ".Trash",
}

PRODUCTION_FOLDER_PATTERNS = [
    re.compile(r"(?i)^produc(tion|ed)"),
    re.compile(r"(?i)^discovery"),
    re.compile(r"(?i)^bates"),
    re.compile(r"(?i)^exhibit"),
    re.compile(r"(?i)^esi\b"),
    re.compile(r"(?i)^native"),
    re.compile(r"(?i)^load\s?file"),
    re.compile(r"(?i)^text\s?extracted"),
]

BATES_PATTERN = re.compile(r"^[A-Z]{2,10}[-_]?\d{4,8}\.\w{3,4}$", re.IGNORECASE)

# Billing activity cutoff — matters with slips after this date are "active"
ACTIVE_CUTOFF = date(2024, 5, 1)


def classify_file(name: str, extension: str, parent_path: str) -> str:
    ext = (extension or "").lower()
    name_lower = name.lower()

    if ext in SKIP_EXTENSIONS or name_lower in {"thumbs.db", "desktop.ini", ".ds_store"}:
        return "skip"
    if ext in EDISCOVERY_ARCHIVE_EXTENSIONS:
        return "ediscovery_archive"
    if ext in EDISCOVERY_LOAD_FILE_EXTENSIONS:
        return "ediscovery_load_file"
    if ext in PRODUCTION_ARCHIVE_EXTENSIONS:
        parent_name = os.path.basename(parent_path).lower()
        for pattern in PRODUCTION_FOLDER_PATTERNS:
            if pattern.search(parent_name):
                return "ediscovery_production"
        return "unknown"
    if BATES_PATTERN.match(name):
        return "ediscovery_production"
    if ext in PARSEABLE_EXTENSIONS:
        return "document"
    return "unknown"


def classify_folder(name: str) -> str:
    for pattern in PRODUCTION_FOLDER_PATTERNS:
        if pattern.search(name):
            return "ediscovery_production"
    return "folder"


def walk_filesystem(root: str) -> list[dict]:
    entries = []
    root_path = Path(root)
    if not root_path.exists():
        log.error("Root path does not exist: %s", root)
        return entries

    log.info("Walking filesystem: %s", root)
    start = time.time()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_FOLDERS and not d.startswith(".")
        ]

        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        parts = Path(rel_dir).parts if rel_dir != "." else ()
        root_folder = parts[0] if parts else None

        if rel_dir != ".":
            folder_name = os.path.basename(dirpath)
            try:
                stat = os.stat(dirpath)
                entries.append({
                    "entry_type": "folder",
                    "full_path": os.path.join(root, rel_dir),
                    "parent_path": os.path.join(root, os.path.dirname(rel_dir)) if depth > 1 else root,
                    "name": folder_name,
                    "extension": None,
                    "size_bytes": None,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                    "depth": depth,
                    "root_folder": root_folder or folder_name,
                    "child_file_count": len(filenames),
                    "child_folder_count": len(dirnames),
                    "classification": classify_folder(folder_name),
                })
            except (OSError, PermissionError) as e:
                log.warning("Cannot stat folder %s: %s", dirpath, e)

        for fname in filenames:
            if fname.lower() in {"thumbs.db", "desktop.ini", ".ds_store"}:
                continue
            ext = os.path.splitext(fname)[1].lower()
            fpath = os.path.join(dirpath, fname)
            try:
                stat = os.stat(fpath)
            except (OSError, PermissionError) as e:
                log.warning("Cannot stat file %s: %s", fpath, e)
                continue

            entries.append({
                "entry_type": "file",
                "full_path": fpath,
                "parent_path": dirpath,
                "name": fname,
                "extension": ext if ext else None,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                "depth": depth + 1 if rel_dir != "." else 1,
                "root_folder": root_folder or None,
                "child_file_count": None,
                "child_folder_count": None,
                "classification": classify_file(fname, ext, dirpath),
            })

    elapsed = time.time() - start
    log.info("Walk complete: %d entries in %.1fs", len(entries), elapsed)
    return entries


def insert_entries(conn, tenant_id: str, scan_run_id: str, entries: list[dict]):
    if not entries:
        return
    log.info("Inserting %d entries into file_inventory...", len(entries))
    start = time.time()

    sql = """
        INSERT INTO file_inventory (
            tenant_id, scan_run_id, entry_type, full_path, parent_path,
            name, extension, size_bytes, modified_at, depth,
            root_folder, child_file_count, child_folder_count,
            classification
        ) VALUES (
            %(tenant_id)s, %(scan_run_id)s::uuid, %(entry_type)s, %(full_path)s,
            %(parent_path)s, %(name)s, %(extension)s, %(size_bytes)s,
            %(modified_at)s, %(depth)s, %(root_folder)s,
            %(child_file_count)s, %(child_folder_count)s,
            %(classification)s
        )
    """
    batch = [{**e, "tenant_id": tenant_id, "scan_run_id": scan_run_id} for e in entries]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, batch, page_size=500)
    conn.commit()

    elapsed = time.time() - start
    log.info("Insert complete: %d rows in %.1fs", len(entries), elapsed)


def run_matching_pass(conn, tenant_id: str, scan_run_id: str):
    log.info("Running matching pass...")
    start = time.time()
    tid = tenant_id

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT root_folder FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND root_folder IS NOT NULL
            ORDER BY root_folder
        """, (tid, scan_run_id))
        folders = [r["root_folder"] for r in cur.fetchall()]
        log.info("  %d distinct root folders to match", len(folders))

        cur.execute("SELECT id, client_name, client_number FROM clients WHERE TRIM(tenant_id) = %s", (tid,))
        clients = cur.fetchall()
        client_by_name_lower = {}
        for c in clients:
            key = (c["client_name"] or "").strip().lower()
            if key:
                client_by_name_lower.setdefault(key, []).append(c)

        cur.execute("""
            SELECT m.id, m.matter_name, m.matter_number, m.client_id,
                   m.status, c.client_name,
                   (SELECT MAX(ts.slip_date) FROM ts_slips ts
                    WHERE ts.source_client_id = m.legacy_id
                      AND TRIM(ts.tenant_id) = TRIM(m.tenant_id)) AS last_slip_date,
                   (SELECT SUM(ts.wip_value) FROM ts_slips ts
                    WHERE ts.source_client_id = m.legacy_id
                      AND TRIM(ts.tenant_id) = TRIM(m.tenant_id)
                      AND ts.wip_value > 0) AS total_wip
            FROM matters m LEFT JOIN clients c ON c.id = m.client_id
            WHERE TRIM(m.tenant_id) = %s
        """, (tid,))
        matters = cur.fetchall()
        matter_by_number = {}
        matters_by_client = {}
        for m in matters:
            if m["matter_number"]:
                matter_by_number[m["matter_number"].strip().lower()] = m
            if m["client_id"]:
                matters_by_client.setdefault(str(m["client_id"]), []).append(m)

        matched = 0
        for folder in folders:
            folder_lower = folder.strip().lower()
            best_client = None
            best_matter = None
            best_method = None
            best_confidence = 0.0

            # 1. Exact
            if folder_lower in client_by_name_lower:
                best_client = client_by_name_lower[folder_lower][0]
                best_method = "exact_client_name"
                best_confidence = 1.0

            # 2. Matter number
            if not best_client:
                for mnum, m in matter_by_number.items():
                    if mnum in folder_lower or mnum.replace(".", "") in folder_lower.replace(".", ""):
                        best_matter = m
                        best_client = {"id": m["client_id"], "client_name": m["client_name"]}
                        best_method = "matter_number_in_folder"
                        best_confidence = 0.95
                        break

            # 3. Prefix
            if not best_client:
                for cname_lower, candidates in client_by_name_lower.items():
                    if len(cname_lower) >= 3 and folder_lower.startswith(cname_lower):
                        best_client = candidates[0]
                        best_method = "client_name_prefix"
                        best_confidence = 0.8
                        break

            # 4. Contains
            if not best_client:
                for cname_lower, candidates in client_by_name_lower.items():
                    if len(cname_lower) >= 4 and cname_lower in folder_lower:
                        best_client = candidates[0]
                        best_method = "client_name_contains"
                        best_confidence = 0.7
                        break

            # 5. Reverse contains
            if not best_client:
                for cname_lower, candidates in client_by_name_lower.items():
                    if len(folder_lower) >= 4 and folder_lower in cname_lower:
                        best_client = candidates[0]
                        best_method = "folder_in_client_name"
                        best_confidence = 0.65
                        break

            # 6. Trigram
            if not best_client:
                try:
                    cur.execute("""
                        SELECT id, client_name, client_number,
                               similarity(LOWER(client_name), %s) AS sim
                        FROM clients WHERE TRIM(tenant_id) = %s
                          AND similarity(LOWER(client_name), %s) > 0.35
                        ORDER BY sim DESC LIMIT 1
                    """, (folder_lower, tid, folder_lower))
                    row = cur.fetchone()
                    if row:
                        best_client = row
                        best_method = "trigram_similarity"
                        best_confidence = float(row["sim"])
                except Exception:
                    pass

            # Billing activity check
            is_active = False
            if best_client and best_client.get("id"):
                for m in matters_by_client.get(str(best_client["id"]), []):
                    if m.get("last_slip_date") and m["last_slip_date"] >= ACTIVE_CUTOFF:
                        is_active = True
                        break

            if best_client:
                cur.execute("""
                    UPDATE file_inventory
                    SET proposed_client_id = %s::uuid,
                        proposed_client_name = %s,
                        proposed_matter_id = %s,
                        proposed_matter_name = %s,
                        match_method = %s,
                        match_confidence = %s,
                        match_status = 'pending',
                        is_active_matter = %s
                    WHERE tenant_id = %s AND scan_run_id = %s::uuid
                      AND root_folder = %s
                """, (
                    str(best_client["id"]),
                    best_client.get("client_name"),
                    str(best_matter["id"]) if best_matter else None,
                    best_matter["matter_name"] if best_matter else None,
                    best_method, best_confidence, is_active,
                    tid, scan_run_id, folder,
                ))
                matched += 1

        conn.commit()

    # Set priority
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE file_inventory SET priority = 'immediate'
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND is_active_matter = true
              AND classification IN ('document', 'ediscovery_archive',
                                     'ediscovery_production', 'ediscovery_load_file')
        """, (tid, scan_run_id))

        cur.execute("""
            UPDATE file_inventory SET priority = 'deferred'
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND is_active_matter = false
              AND classification IN ('ediscovery_archive', 'ediscovery_production',
                                     'ediscovery_load_file')
        """, (tid, scan_run_id))

        cur.execute("""
            UPDATE file_inventory SET priority = 'skip'
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND classification = 'skip'
        """, (tid, scan_run_id))

        conn.commit()

    elapsed = time.time() - start
    log.info("Matching complete: %d/%d folders matched in %.1fs",
             matched, len(folders), elapsed)


def print_summary(conn, tenant_id: str, scan_run_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT entry_type, COUNT(*) as cnt,
                   COALESCE(SUM(size_bytes), 0) as total_bytes
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
            GROUP BY entry_type
        """, (tenant_id, scan_run_id))
        for r in cur.fetchall():
            size_mb = r["total_bytes"] / (1024 * 1024)
            log.info("  %ss: %d (%.1f MB)", r["entry_type"], r["cnt"], size_mb)

        cur.execute("""
            SELECT classification, COUNT(*) as cnt,
                   COALESCE(SUM(size_bytes), 0) as total_bytes
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND entry_type = 'file'
            GROUP BY classification ORDER BY cnt DESC
        """, (tenant_id, scan_run_id))
        log.info("  File classifications:")
        for r in cur.fetchall():
            size_mb = r["total_bytes"] / (1024 * 1024)
            log.info("    %-25s %5d files (%7.1f MB)", r["classification"], r["cnt"], size_mb)

        cur.execute("""
            SELECT priority, is_active_matter, COUNT(*) as cnt
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND entry_type = 'file'
            GROUP BY priority, is_active_matter
            ORDER BY priority, is_active_matter DESC
        """, (tenant_id, scan_run_id))
        log.info("  Priority breakdown:")
        for r in cur.fetchall():
            active = "active" if r["is_active_matter"] else "closed"
            log.info("    %-12s (%s): %d files", r["priority"], active, r["cnt"])

        cur.execute("""
            SELECT classification, COUNT(*) as cnt,
                   COALESCE(SUM(size_bytes), 0) as total_bytes,
                   COUNT(*) FILTER (WHERE is_active_matter = true) as active_count
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND classification LIKE 'ediscovery%%'
            GROUP BY classification
        """, (tenant_id, scan_run_id))
        ediscovery_rows = cur.fetchall()
        if ediscovery_rows:
            log.info("  eDiscovery items flagged:")
            for r in ediscovery_rows:
                size_mb = r["total_bytes"] / (1024 * 1024)
                log.info("    %-25s %d total (%d active) — %.1f MB",
                         r["classification"], r["cnt"], r["active_count"], size_mb)

        cur.execute("""
            SELECT match_method, COUNT(DISTINCT root_folder) as folders,
                   ROUND(AVG(match_confidence)::numeric, 2) as avg_conf
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND match_method IS NOT NULL
            GROUP BY match_method ORDER BY folders DESC
        """, (tenant_id, scan_run_id))
        log.info("  Match methods:")
        for r in cur.fetchall():
            log.info("    %-30s %3d folders (avg conf %.2f)",
                     r["match_method"], r["folders"], float(r["avg_conf"] or 0))

        cur.execute("""
            SELECT DISTINCT root_folder FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND match_method IS NULL AND root_folder IS NOT NULL
            ORDER BY root_folder
        """, (tenant_id, scan_run_id))
        unmatched = [r["root_folder"] for r in cur.fetchall()]
        if unmatched:
            log.info("  Unmatched root folders (%d):", len(unmatched))
            for f in unmatched[:30]:
                log.info("    - %s", f)
            if len(unmatched) > 30:
                log.info("    ... and %d more", len(unmatched) - 30)

        cur.execute("""
            SELECT COALESCE(extension, '(none)') as ext,
                   COUNT(*) as cnt, SUM(size_bytes) as total_bytes
            FROM file_inventory
            WHERE tenant_id = %s AND scan_run_id = %s::uuid
              AND entry_type = 'file'
            GROUP BY extension ORDER BY cnt DESC LIMIT 15
        """, (tenant_id, scan_run_id))
        log.info("  Top file extensions:")
        for r in cur.fetchall():
            size_mb = (r["total_bytes"] or 0) / (1024 * 1024)
            log.info("    %-10s %5d files (%7.1f MB)", r["ext"], r["cnt"], size_mb)


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def main():
    parser = argparse.ArgumentParser(description="Inventory legacy filesystem")
    parser.add_argument("--root", required=True, help="Root path to walk")
    parser.add_argument("--tenant", required=True, help="Tenant ID")
    parser.add_argument("--skip-walk", action="store_true",
                        help="Skip filesystem walk, only re-run matching")
    args = parser.parse_args()

    scan_run_id = str(uuid.uuid4())
    log.info("=== Legacy Filesystem Inventory Agent ===")
    log.info("Root:     %s", args.root)
    log.info("Tenant:   %s", args.tenant)
    log.info("Scan Run: %s", scan_run_id)

    conn = psycopg2.connect(get_db_url())
    try:
        if not args.skip_walk:
            entries = walk_filesystem(args.root)
            insert_entries(conn, args.tenant, scan_run_id, entries)
        else:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT scan_run_id::text FROM file_inventory
                    WHERE tenant_id = %s ORDER BY scan_run_id DESC LIMIT 1
                """, (args.tenant,))
                row = cur.fetchone()
                if row:
                    scan_run_id = row[0]
                    log.info("Using existing scan: %s", scan_run_id)
                else:
                    log.error("No existing scan found.")
                    return

        run_matching_pass(conn, args.tenant, scan_run_id)
        log.info("=== Scan Summary ===")
        print_summary(conn, args.tenant, scan_run_id)
    finally:
        conn.close()

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
