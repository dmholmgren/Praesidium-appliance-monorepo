#!/usr/bin/env python3
"""
jobs/automatch_folders.py

Two-phase subfolder → matter matching:

  Phase 1 (--propose): Fuzzy-match subfolders to existing matters.
     Writes proposals to dms_folder_matches with accepted=NULL (pending).
     Review in admin UI, accept or reject each.

  Phase 2 (--execute): Process accepted matches only.
     Creates matter_folders entries, updates file_inventory.proposed_matter_id,
     moves physical files from Unmatched, updates dms_documents.file_path.

Usage:
    # Propose matches for American Savings
    docker exec praesidium-web python3 /app/jobs/automatch_folders.py \
        --tenant 986c0fee-... --root "American Savings" --propose

    # Propose for ALL root folders
    docker exec praesidium-web python3 /app/jobs/automatch_folders.py \
        --tenant 986c0fee-... --propose

    # After reviewing in admin, execute accepted matches
    docker exec praesidium-web python3 /app/jobs/automatch_folders.py \
        --tenant 986c0fee-... --execute

    # Accept all proposals above a score threshold (no UI needed)
    docker exec praesidium-web python3 /app/jobs/automatch_folders.py \
        --tenant 986c0fee-... --auto-accept 0.90

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("automatch")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
PRAESIDIUM_ROOT = "/mnt/praesidium"


def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:].split("?")[0]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(host=host, port=int(port), dbname=dbname, user=user, password=password)


def _safe_dirname(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name or "unknown").strip().rstrip(".")[:200]


def normalize(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r'\s*\([^)]*\)\s*', ' ', s)
    s = re.sub(r'\s*-\s*', ' ', s)
    s = re.sub(r'[,\.\'\"]+', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def match_score(folder_name: str, matter_name: str) -> float:
    if folder_name == matter_name:
        return 1.0
    fn, mn = normalize(folder_name), normalize(matter_name)
    if fn == mn:
        return 0.95
    if fn.startswith(mn) or mn.startswith(fn):
        shorter = min(len(fn), len(mn))
        longer = max(len(fn), len(mn))
        return 0.7 + 0.2 * (shorter / longer)
    if fn in mn or mn in fn:
        shorter = min(len(fn), len(mn))
        longer = max(len(fn), len(mn))
        return 0.5 + 0.2 * (shorter / longer)
    fw, mw = set(fn.split()), set(mn.split())
    if fw and mw:
        overlap = len(fw & mw)
        total = len(fw | mw)
        if overlap > 0:
            return 0.3 + 0.4 * (overlap / total)
    return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: PROPOSE
# ═══════════════════════════════════════════════════════════════════════════════

MATTER_HIGH = 0.50  # word_similarity floor for a confident exact-matter proposal


def _best_matter_ws(cur, tid, client_id, subfolder, rejected):
    # name-primary scorer (validated ~0.93 p@1): word_similarity of matter_name
    # within the subfolder name, scoped to the client, excluding rejected matters.
    rej = list(rejected) if rejected else None
    cur.execute(
        "SELECT id, matter_name, word_similarity(matter_name, %s) AS s "
        "FROM matters WHERE TRIM(tenant_id)=%s AND client_id=%s "
        "AND (%s::uuid[] IS NULL OR NOT (id = ANY(%s::uuid[]))) "
        "ORDER BY s DESC NULLS LAST LIMIT 1",
        (subfolder, tid, client_id, rej, rej))
    return cur.fetchone()


_LEGGEN = {}

def _legacy_general(cur, tid, client_id):
    # find-or-create the client's "Legacy - General" catch-all matter (idempotent)
    if client_id in _LEGGEN:
        return _LEGGEN[client_id]
    cur.execute(
        "SELECT id FROM matters WHERE TRIM(tenant_id)=%s AND client_id=%s "
        "AND matter_name='Legacy - General' LIMIT 1", (tid, client_id))
    row = cur.fetchone()
    mid = row["id"] if row else None
    if mid is None:
        cur.execute("SELECT client_number FROM clients WHERE id=%s", (client_id,))
        c = cur.fetchone()
        num = (c["client_number"] if c and c["client_number"] else "LEG")
        cur.execute(
            "INSERT INTO matters (id, tenant_id, client_id, matter_number, matter_name, "
            "matter_type, status, created_at, updated_at) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 'Legacy - General', "
            "'legacy_general', 'active', NOW(), NOW()) RETURNING id",
            (tid, client_id, str(num) + "-LEG"))
        mid = cur.fetchone()["id"]
    _LEGGEN[client_id] = mid
    return mid


def run_propose(cur, tid, root_filter=None):
    # Score subfolders by word_similarity, route three ways:
    #   >= MATTER_HIGH -> exact matter ; client known -> Legacy-General ; else orphan.
    # Idempotent: skips accepted/rejected/synced; excludes rejected matters so a
    # re-run routes a rejected folder to its next-best or to Legacy-General.
    root_clause = "AND fi.root_folder = %s" if root_filter else ""
    params = [tid] + ([root_filter] if root_filter else [])
    cur.execute(
        "SELECT fi.root_folder, fi.proposed_client_id, fi.proposed_client_name, count(*) as cnt "
        "FROM file_inventory fi "
        "WHERE fi.tenant_id=%s AND fi.entry_type='file' AND fi.classification='document' "
        "AND fi.proposed_matter_id IS NULL " + root_clause + " "
        "GROUP BY fi.root_folder, fi.proposed_client_id, fi.proposed_client_name "
        "ORDER BY cnt DESC", params)
    roots = cur.fetchall()
    log.info("Found %d root folders with unmatched files", len(roots))

    cur.execute(
        "SELECT folder_path, matter_id, accepted, synced_at "
        "FROM dms_folder_matches WHERE TRIM(tenant_id)=%s", (tid,))
    settled = set(); rejected_by = {}; pending_pairs = set()
    for r in cur.fetchall():
        fp = r["folder_path"]
        if r["accepted"] is True or r["synced_at"] is not None:
            settled.add(fp)
        elif r["accepted"] is False:
            rejected_by.setdefault(fp, set()).add(r["matter_id"])
        else:
            pending_pairs.add((fp, r["matter_id"]))

    cur.execute("SELECT folder_path FROM matter_folders WHERE TRIM(tenant_id)=%s", (tid,))
    already_mapped = {r["folder_path"] for r in cur.fetchall()}

    n_exact = n_general = n_orphan = n_skip = 0
    for root_info in roots:
        root_folder = root_info["root_folder"]
        client_id = root_info["proposed_client_id"]
        cur.execute(
            "SELECT DISTINCT substring(full_path from %s) as subfolder, count(*) as cnt "
            "FROM file_inventory "
            "WHERE root_folder=%s AND tenant_id=%s AND entry_type='file' "
            "AND classification='document' AND proposed_matter_id IS NULL "
            "GROUP BY subfolder ORDER BY cnt DESC",
            (f"/(?:Clients|Docsend|D Drive Backup/Clients|D Drive Backup/Docsend)/{re.escape(root_folder)}/([^/]+)",
             root_folder, tid))
        for sf in cur.fetchall():
            subfolder = sf["subfolder"]; file_count = sf["cnt"]
            if not subfolder:
                continue
            if "." in subfolder and len(subfolder) < 80 and not os.path.isdir(f"/mnt/clients/{root_folder}/{subfolder}"):
                continue
            mf_key = f"{root_folder}/{subfolder}"
            if mf_key in already_mapped or mf_key in settled:
                n_skip += 1; continue
            if client_id is None:
                n_orphan += 1; continue
            best = _best_matter_ws(cur, tid, client_id, subfolder, rejected_by.get(mf_key))
            if best and best["s"] is not None and float(best["s"]) >= MATTER_HIGH:
                target = best["id"]; score = float(best["s"]); n_exact += 1
            else:
                target = _legacy_general(cur, tid, client_id)
                score = float(best["s"]) if best and best["s"] is not None else 0.0
                n_general += 1
            if (mf_key, target) in pending_pairs:
                continue
            cur.execute(
                "INSERT INTO dms_folder_matches "
                "(id, tenant_id, matter_id, folder_path, best_disk_path, disk_root, "
                " disk_file_count, score, accepted, computed_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,NOW())",
                (str(uuid.uuid4()), tid, target, mf_key,
                 f"{root_folder}/{subfolder}", f"/mnt/clients/{root_folder}",
                 file_count, score))
            pending_pairs.add((mf_key, target))

    log.info("")
    log.info("=== Propose Summary (word_similarity name-primary + Legacy-General) ===")
    log.info("  -> exact matter        : %d", n_exact)
    log.info("  -> Legacy - General    : %d", n_general)
    log.info("  orphan (no client)     : %d", n_orphan)
    log.info("  skipped settled/mapped : %d", n_skip)
    log.info("")


def run_auto_accept(cur, tid, threshold):
    """Accept all proposals at or above the score threshold."""
    cur.execute("""
        UPDATE dms_folder_matches SET accepted = true
        WHERE TRIM(tenant_id) = %s AND accepted IS NULL AND score >= %s
    """, (tid, threshold))
    accepted = cur.rowcount

    cur.execute("""
        SELECT count(*) as remaining FROM dms_folder_matches
        WHERE TRIM(tenant_id) = %s AND accepted IS NULL
    """, (tid,))
    remaining = cur.fetchone()["remaining"]

    log.info("Auto-accepted %d proposals (score >= %.2f)", accepted, threshold)
    log.info("Remaining pending: %d", remaining)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: EXECUTE
# ═══════════════════════════════════════════════════════════════════════════════

def run_execute(cur, tid):
    """Process accepted folder matches: create matter_folders, update file_inventory, move files."""

    # Get accepted matches
    cur.execute("""
        SELECT fm.id, fm.matter_id, fm.folder_path, fm.best_disk_path, fm.score,
               m.matter_name
        FROM dms_folder_matches fm
        JOIN matters m ON fm.matter_id = m.id
        WHERE TRIM(fm.tenant_id) = %s AND fm.accepted = true
        ORDER BY fm.folder_path
    """, (tid,))
    accepted = cur.fetchall()
    log.info("Processing %d accepted folder matches", len(accepted))

    # Load existing matter_folders to skip duplicates
    cur.execute("SELECT folder_path FROM matter_folders WHERE TRIM(tenant_id) = %s", (tid,))
    existing_mf = {r["folder_path"] for r in cur.fetchall()}

    stats = {"mf_created": 0, "fi_updated": 0, "docs_moved": 0, "move_failed": 0}

    mf_lookup = {}  # for reassignment

    for match in accepted:
        mf_key = match["folder_path"]
        matter_id = str(match["matter_id"])
        matter_name = match["matter_name"]

        mf_lookup[mf_key] = matter_id

        # Create matter_folders entry
        if mf_key not in existing_mf:
            cur.execute("""
                INSERT INTO matter_folders (id, tenant_id, matter_id, folder_path, disk_root, added_at)
                VALUES (%s, %s, %s, %s, %s, NOW()) ON CONFLICT DO NOTHING
            """, (str(uuid.uuid4()), tid, matter_id, mf_key,
                  f"/mnt/clients/{mf_key}"))
            stats["mf_created"] += 1
            existing_mf.add(mf_key)

        # Update file_inventory
        # folder_path like "American Savings/Luong"
        cur.execute("""
            UPDATE file_inventory SET proposed_matter_id = %s
            WHERE tenant_id = %s AND entry_type = 'file' AND classification = 'document'
              AND proposed_matter_id IS NULL AND full_path LIKE %s
        """, (matter_id, tid, f"%/{mf_key}/%"))
        stats["fi_updated"] += cur.rowcount

    # Now reassign unmatched dms_documents
    if mf_lookup:
        log.info("Reassigning unmatched documents...")
        _reassign_docs(cur, tid, mf_lookup, stats)

    log.info("")
    log.info("=== Execute Summary ===")
    log.info("  matter_folders created: %d", stats["mf_created"])
    log.info("  file_inventory updated: %d", stats["fi_updated"])
    log.info("  Documents moved:        %d", stats["docs_moved"])
    log.info("  Move failures:          %d", stats["move_failed"])


def _reassign_docs(cur, tid, mf_lookup, stats):
    """Move docs from Unmatched to correct matters."""
    unmatched_prefix = f"/mnt/praesidium/{tid}/matters/Unmatched Documents/"

    cur.execute("""
        SELECT d.id, d.file_path, d.folder_root
        FROM dms_documents d
        WHERE TRIM(d.tenant_id) = %s AND d.file_path LIKE %s
    """, (tid, "%/Unmatched Documents/%"))
    docs = cur.fetchall()
    log.info("  Found %d docs in Unmatched", len(docs))

    # Sort lookups longest-first for prefix matching
    sorted_mf = sorted(mf_lookup.items(), key=lambda x: -len(x[0]))

    # Cache matter names
    matter_names = {}
    cur.execute("SELECT id, matter_name FROM matters WHERE TRIM(tenant_id) = %s", (tid,))
    for r in cur.fetchall():
        matter_names[str(r["id"])] = r["matter_name"]

    for doc in docs:
        fpath = doc["file_path"]
        root = doc["folder_root"] or ""

        if unmatched_prefix not in fpath:
            continue

        rel_after_unmatched = fpath[len(unmatched_prefix):]
        original_rel = f"{root}/{rel_after_unmatched}"

        # Find match
        best_matter_id = None
        best_mf_path = None
        for mf_path, matter_id in sorted_mf:
            if original_rel.startswith(mf_path + "/") or original_rel == mf_path:
                best_matter_id = matter_id
                best_mf_path = mf_path
                break
        if not best_matter_id:
            for mf_path, matter_id in sorted_mf:
                if mf_path == root:
                    best_matter_id = matter_id
                    best_mf_path = mf_path
                    break

        if not best_matter_id:
            continue

        matter_name = matter_names.get(best_matter_id, "unknown")

        # Build new path
        if original_rel.startswith(best_mf_path + "/"):
            file_rel = original_rel[len(best_mf_path) + 1:]
        elif original_rel.startswith(root + "/"):
            file_rel = original_rel[len(root) + 1:]
        else:
            file_rel = rel_after_unmatched

        new_dir = Path(PRAESIDIUM_ROOT) / tid / "matters" / _safe_dirname(matter_name)
        new_path = new_dir / file_rel

        try:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if Path(fpath).exists():
                if new_path.exists():
                    stem, suffix = new_path.stem, new_path.suffix
                    c = 1
                    while new_path.exists():
                        new_path = new_path.parent / f"{stem}_{c}{suffix}"
                        c += 1
                shutil.move(fpath, new_path)
            cur.execute("UPDATE dms_documents SET file_path = %s WHERE id = %s",
                        (str(new_path), str(doc["id"])))
            stats["docs_moved"] += 1
        except Exception as exc:
            log.warning("Move failed: %s", exc)
            stats["move_failed"] += 1

    # Clean empty dirs
    unmatched_dir = Path(PRAESIDIUM_ROOT) / tid / "matters" / "Unmatched Documents"
    if unmatched_dir.exists():
        for dirpath, dirnames, filenames in os.walk(str(unmatched_dir), topdown=False):
            if not filenames and not dirnames:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass


# ═══════════════════════════════════════════════════════════════════════════════
# STATUS
# ═══════════════════════════════════════════════════════════════════════════════

def show_status(cur, tid):
    """Show current state of proposals."""
    cur.execute("""
        SELECT 
            count(*) as total,
            count(*) FILTER (WHERE accepted = true) as accepted,
            count(*) FILTER (WHERE accepted = false) as rejected,
            count(*) FILTER (WHERE accepted IS NULL) as pending
        FROM dms_folder_matches WHERE TRIM(tenant_id) = %s
    """, (tid,))
    s = cur.fetchone()
    log.info("Folder Match Proposals:")
    log.info("  Total: %d | Accepted: %d | Rejected: %d | Pending: %d",
             s["total"], s["accepted"], s["rejected"], s["pending"])

    if s["pending"] > 0:
        cur.execute("""
            SELECT fm.folder_path, fm.score, m.matter_name
            FROM dms_folder_matches fm
            JOIN matters m ON fm.matter_id = m.id
            WHERE TRIM(fm.tenant_id) = %s AND fm.accepted IS NULL
            ORDER BY fm.score DESC LIMIT 20
        """, (tid,))
        log.info("  Top pending proposals:")
        for r in cur.fetchall():
            log.info("    %.2f  %-50s → %s", r["score"], r["folder_path"][:50], r["matter_name"])


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default=TENANT_ID)
    parser.add_argument("--root", default=None, help="Only this root_folder")
    parser.add_argument("--propose", action="store_true", help="Phase 1: generate proposals")
    parser.add_argument("--execute", action="store_true", help="Phase 2: process accepted matches")
    parser.add_argument("--auto-accept", type=float, default=None,
                        help="Auto-accept proposals >= this score")
    parser.add_argument("--status", action="store_true", help="Show proposal status")
    args = parser.parse_args()

    if not any([args.propose, args.execute, args.auto_accept is not None, args.status]):
        parser.error("Specify --propose, --execute, --auto-accept <score>, or --status")

    tid = args.tenant.strip()
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if args.status:
        show_status(cur, tid)

    if args.propose:
        run_propose(cur, tid, root_filter=args.root)

    if args.auto_accept is not None:
        run_auto_accept(cur, tid, args.auto_accept)

    if args.execute:
        run_execute(cur, tid)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
