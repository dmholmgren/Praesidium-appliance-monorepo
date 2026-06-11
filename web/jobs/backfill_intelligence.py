#!/usr/bin/env python3
"""
jobs/backfill_intelligence.py
=============================
Backfill matter intelligence extraction across all matters with DMS documents.

For each matter that has a disk_root in matter_folders:
  1. Find the top 3 highest-value documents (title commitments, contracts, pleadings first)
  2. Run Claude extraction against each
  3. Write structured primitives (role-constrained parties, GF numbers, identifiers, deal points)

Usage:
  sudo docker exec praesidium-web python3 /app/jobs/backfill_intelligence.py [--dry-run] [--limit N] [--matter-type litigation|transactional] [--matter-id UUID]

Rate limiting: ~2s per Claude call. For 1,380 matters × 3 docs = ~4,140 calls.
At $0.003/call (Sonnet) that's ~$12.50 total. Runtime ~2.5 hours.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/app")
os.chdir("/app")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_intelligence")

MAX_DOCS_PER_MATTER = 3


async def backfill(dry_run=False, limit=None, matter_type_filter=None, matter_id_filter=None):
    from core.db.base import AsyncSessionLocal
    from sqlalchemy import text as sa_text
    from modules.intelligence.matter_extract import (
        _get_api_key, _call_claude, _write_matter_extraction,
        _write_litigation_extraction,
        SYSTEM_MATTER_PROVISION, SYSTEM_LITIGATION_EXTRACT,
    )

    tid = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

    api_key = await _get_api_key(tid)
    if not api_key:
        log.error("No API key found")
        return
    log.info("API key loaded: ...%s", api_key[-8:])

    # Get all matters with disk_root folders
    async with AsyncSessionLocal() as session:
        filters = ["TRIM(mf.tenant_id) = :tid", "mf.disk_root IS NOT NULL", "mf.disk_root != ''"]
        params = {"tid": tid}

        if matter_type_filter:
            filters.append("m.matter_type = :mt")
            params["mt"] = matter_type_filter
        if matter_id_filter:
            filters.append("m.id = CAST(:mid AS uuid)")
            params["mid"] = matter_id_filter

        where = " AND ".join(filters)
        rows = await session.execute(sa_text(f"""
            SELECT DISTINCT m.id::text AS matter_id, m.matter_name, m.matter_type,
                   mf.disk_root, m.gf_number, m.cause_number
            FROM matter_folders mf
            JOIN matters m ON m.id = mf.matter_id
            WHERE {where}
            ORDER BY m.matter_name
        """), params)
        matters = [dict(r) for r in rows.mappings().fetchall()]

    if limit:
        matters = matters[:limit]

    log.info("Found %d matters with disk_root folders", len(matters))

    # Stats
    total_matters = len(matters)
    processed = 0
    skipped = 0
    total_contacts = 0
    total_deal_points = 0
    total_identifiers = 0
    total_api_calls = 0
    errors = 0

    for mi, matter in enumerate(matters, 1):
        matter_id = matter["matter_id"]
        matter_name = matter["matter_name"]
        matter_type = matter["matter_type"] or "transactional"
        disk_root = matter["disk_root"]

        # Skip if already has intelligence (gf_number or cause_number populated)
        # unless running with --matter-id (explicit request)
        if not matter_id_filter:
            has_gf = bool(matter.get("gf_number"))
            has_cause = bool(matter.get("cause_number"))
            if has_gf or has_cause:
                async with AsyncSessionLocal() as session:
                    row = await session.execute(sa_text("""
                        SELECT COUNT(*) as cnt FROM matter_contacts
                        WHERE matter_id = CAST(:mid AS uuid) AND TRIM(tenant_id) = :tid
                          AND role_code IS NOT NULL AND role_code != 'other'
                    """), {"mid": matter_id, "tid": tid})
                    classified = row.scalar()
                if classified and classified >= 3:
                    skipped += 1
                    continue

        # Find top docs from DMS
        async with AsyncSessionLocal() as session:
            docs = await session.execute(sa_text("""
                SELECT id::text AS doc_id,
                       REVERSE(SPLIT_PART(REVERSE(file_path), '/', 1)) AS filename,
                       LEFT(content_text, 50000) AS txt,
                       LENGTH(content_text) AS txt_len
                FROM dms_documents
                WHERE TRIM(tenant_id) = :tid
                  AND file_path LIKE :prefix
                  AND content_text IS NOT NULL
                  AND LENGTH(content_text) > 500
                ORDER BY
                    CASE
                        WHEN file_path ILIKE '%%title%%commit%%' THEN 0
                        WHEN file_path ILIKE '%%purchase%%sale%%' THEN 1
                        WHEN file_path ILIKE '%%contract%%' THEN 2
                        WHEN file_path ILIKE '%%agreement%%' THEN 3
                        WHEN file_path ILIKE '%%complaint%%' THEN 4
                        WHEN file_path ILIKE '%%petition%%' THEN 5
                        WHEN file_path ILIKE '%%amended%%petition%%' THEN 6
                        WHEN file_path ILIKE '%%scheduling%%order%%' THEN 7
                        WHEN file_path ILIKE '%%loi%%' THEN 8
                        WHEN file_path ILIKE '%%letter%%intent%%' THEN 9
                        ELSE 20
                    END,
                    LENGTH(content_text) DESC
                LIMIT :lim
            """), {"tid": tid, "prefix": f"{disk_root}%", "lim": MAX_DOCS_PER_MATTER})
            doc_rows = [dict(r) for r in docs.mappings().fetchall()]

        if not doc_rows:
            skipped += 1
            continue

        is_litigation = matter_type == "litigation"
        prompt = SYSTEM_LITIGATION_EXTRACT if is_litigation else SYSTEM_MATTER_PROVISION

        log.info("[%d/%d] %s (%s) — %d docs, disk_root=%s",
                 mi, total_matters, matter_name, matter_type, len(doc_rows), disk_root[-30:])

        if dry_run:
            for d in doc_rows:
                log.info("  DRY RUN: would extract %s (%d chars)", d["filename"], d["txt_len"])
            processed += 1
            continue

        matter_contacts = 0
        matter_dp = 0
        matter_ids = 0

        for doc in doc_rows:
            filename = doc["filename"]
            txt = doc["txt"]
            if not txt or len(txt) < 500:
                continue

            header = f"[Document: {filename}]\n\n"
            try:
                result = await _call_claude(api_key, prompt, header + txt)
                total_api_calls += 1
            except Exception as e:
                log.warning("  Claude call failed for %s: %s", filename, str(e)[:200])
                errors += 1
                continue

            if not result:
                log.warning("  Claude returned None for %s", filename)
                errors += 1
                continue

            try:
                if is_litigation:
                    stats = await _write_litigation_extraction(
                        tid, matter_id, None, result, 0)
                else:
                    stats = await _write_matter_extraction(
                        tid, matter_id, None, result, 0)

                matter_contacts += stats.get("contacts", 0)
                matter_dp += stats.get("deal_points", 0)
                matter_ids += stats.get("identifiers", 0)

                log.info("  %s → %d contacts, %d deal_points, %d identifiers",
                         filename, stats.get("contacts", 0),
                         stats.get("deal_points", 0), stats.get("identifiers", 0))
            except Exception as e:
                log.warning("  Write failed for %s: %s", filename, str(e)[:200])
                errors += 1

        total_contacts += matter_contacts
        total_deal_points += matter_dp
        total_identifiers += matter_ids
        processed += 1

    log.info("=" * 60)
    log.info("BACKFILL COMPLETE")
    log.info("  Matters processed: %d", processed)
    log.info("  Matters skipped:   %d (already had intelligence)", skipped)
    log.info("  API calls:         %d", total_api_calls)
    log.info("  Contacts created:  %d", total_contacts)
    log.info("  Deal points:       %d", total_deal_points)
    log.info("  Identifiers:       %d", total_identifiers)
    log.info("  Errors:            %d", errors)
    log.info("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill matter intelligence extraction")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be extracted without calling Claude")
    parser.add_argument("--limit", type=int, help="Process only N matters")
    parser.add_argument("--matter-type", choices=["litigation", "transactional"], help="Filter by matter type")
    parser.add_argument("--matter-id", help="Process a single matter by UUID")
    args = parser.parse_args()

    asyncio.run(backfill(
        dry_run=args.dry_run,
        limit=args.limit,
        matter_type_filter=args.matter_type,
        matter_id_filter=args.matter_id,
    ))
