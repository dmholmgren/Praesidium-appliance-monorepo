#!/usr/bin/env python3
"""
jobs/promote_inventory_matches.py (v2 — fixed performance)
Promotes file_inventory match proposals into dms_folder_matches and matter_folders.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("promote_matches")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return "host=localhost port=5432 dbname=praesidium user=praesidium"
    return url.replace("postgresql+asyncpg://", "postgresql://")


def main():
    parser = argparse.ArgumentParser(description="Promote inventory matches")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--min-confidence", type=float, default=0.65)
    parser.add_argument("--active-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    tid = args.tenant.strip()
    conn = psycopg2.connect(get_db_url())

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # ── 1. Get legacy_storage connector config ────────────────────
            cur.execute("""
                SELECT config FROM tenant_connectors
                WHERE TRIM(tenant_id) = %s AND connector = 'legacy_storage'
                  AND is_active = true
            """, (tid,))
            row = cur.fetchone()
            mount_path = (row["config"] or {}).get("mount_path", "/mnt/legacy-qnap/D Drive Backup") if row else "/mnt/legacy-qnap/D Drive Backup"

            log.info("=== Promote Inventory Matches ===")
            log.info("Tenant:         %s", tid)
            log.info("Mount path:     %s", mount_path)
            log.info("Min confidence: %.2f", args.min_confidence)

            # ── 2. Precompute file counts per root_folder (single pass) ──
            log.info("Computing file counts per root folder...")
            cur.execute("""
                SELECT root_folder, COUNT(*) as file_count
                FROM file_inventory
                WHERE tenant_id = %s AND entry_type = 'file'
                  AND root_folder IS NOT NULL
                GROUP BY root_folder
            """, (tid,))
            file_counts = {r["root_folder"]: r["file_count"] for r in cur.fetchall()}
            log.info("  %d root folders with file counts", len(file_counts))

            # ── 3. Get distinct matched root folders (no subquery) ────────
            active_clause = "AND is_active_matter = true" if args.active_only else ""
            cur.execute(f"""
                SELECT DISTINCT ON (root_folder)
                       root_folder,
                       proposed_client_id,
                       proposed_client_name,
                       proposed_matter_id,
                       match_method,
                       match_confidence,
                       is_active_matter
                FROM file_inventory
                WHERE tenant_id = %s
                  AND match_method IS NOT NULL
                  AND match_confidence >= %s
                  AND root_folder IS NOT NULL
                  {active_clause}
                ORDER BY root_folder, match_confidence DESC
            """, (tid, args.min_confidence))
            proposals = cur.fetchall()
            log.info("Found %d folder match proposals", len(proposals))

            # ── 4. Promote each proposal ──────────────────────────────────
            promoted = 0
            skipped_existing = 0
            skipped_no_matter = 0

            for p in proposals:
                root_folder = p["root_folder"]
                client_id = p["proposed_client_id"]
                matter_id = p["proposed_matter_id"]
                file_count = file_counts.get(root_folder, 0)

                disk_path = root_folder
                disk_root = mount_path + "/Clients"

                # Find best matter if not already matched
                if not matter_id and client_id:
                    cur.execute("""
                        SELECT m.id FROM matters m
                        LEFT JOIN ts_slips ts
                            ON ts.source_client_id = m.legacy_id
                           AND TRIM(ts.tenant_id) = TRIM(m.tenant_id)
                        WHERE TRIM(m.tenant_id) = %s
                          AND m.client_id = %s::uuid
                        GROUP BY m.id
                        ORDER BY MAX(ts.slip_date) DESC NULLS LAST
                        LIMIT 1
                    """, (tid, str(client_id)))
                    mrow = cur.fetchone()
                    if mrow:
                        matter_id = mrow["id"]
                    else:
                        skipped_no_matter += 1
                        continue

                if args.dry_run:
                    log.info("  [DRY] %s -> %s (matter=%s conf=%.2f files=%d %s)",
                             root_folder, p["proposed_client_name"],
                             str(matter_id)[:8] if matter_id else "?",
                             float(p["match_confidence"]),
                             file_count, p["match_method"])
                    promoted += 1
                    continue

                # Check if already mapped
                cur.execute("""
                    SELECT id FROM dms_folder_matches
                    WHERE TRIM(tenant_id) = %s AND folder_path = %s
                """, (tid, disk_path))
                if cur.fetchone():
                    skipped_existing += 1
                    continue

                # Insert dms_folder_matches
                match_id = str(uuid.uuid4())
                cur.execute("""
                    INSERT INTO dms_folder_matches (
                        id, tenant_id, matter_id, folder_path,
                        best_disk_path, disk_root, disk_file_count,
                        score, accepted
                    ) VALUES (%s::uuid, %s, %s::uuid, %s, %s, %s, %s, %s, false)
                    ON CONFLICT DO NOTHING
                """, (
                    match_id, tid, str(matter_id), disk_path,
                    disk_root + "/" + disk_path, disk_root, file_count,
                    float(p["match_confidence"]),
                ))

                # Insert matter_folders
                cur.execute("""
                    INSERT INTO matter_folders (
                        id, tenant_id, matter_id, folder_path,
                        disk_root, file_count
                    ) VALUES (gen_random_uuid(), %s, %s::uuid, %s, %s, %s)
                    ON CONFLICT (matter_id, folder_path) DO UPDATE
                    SET disk_root = EXCLUDED.disk_root,
                        file_count = EXCLUDED.file_count
                """, (tid, str(matter_id), disk_path, disk_root, file_count))

                promoted += 1

            if not args.dry_run:
                conn.commit()

            log.info("=== Promotion Summary ===")
            log.info("  Promoted:            %d", promoted)
            log.info("  Skipped (exists):    %d", skipped_existing)
            log.info("  Skipped (no matter): %d", skipped_no_matter)

    finally:
        conn.close()

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
