#!/usr/bin/env python3
"""IMG_RENDITION_BACKFILL_V1 -- modules/ediscovery/jobs/backfill_image_renditions.py

Background backfill + standing drainer for image-native PDF renditions.

Why: TIFF/JPEG natives route to the OCR lane (tesseract -> searchable PDF ->
geometry tokens, rendition_path linked) -- but only during a pipeline run,
capped by OCR_INLINE_LIMIT. Docs ingested before the geometry spine, or past
the inline cap, never get a rendition, so the viewer falls back to slow
on-demand conversion. This job:

  Phase 1 (queue):  image natives with rendition_path IS NULL and
                    processing_status IN ('preserved','pending','geometry_empty')
                    -> 'ocr_pending'.  Statuses with existing geometry
                    (geometry_done/enriched) are NEVER touched: re-OCR would
                    re-persist tokens against a new rendition and risk
                    desyncing canonical offsets (Sect. 0).
  Phase 2 (drain):  per collection with ocr_pending docs, invoke the existing
                    OCR lane (geometry_intake --stage ocr). Collections whose
                    pipeline is actively 'processing' are skipped to avoid
                    double-tokenizing against the inline stage.

Modes: full (default), --queue-only, --drain-only (cron drainer), --dry-run.
flock guard prevents overlapping runs.
"""
import argparse
import fcntl
import json
import logging
import os
import re
import subprocess
import sys

logger = logging.getLogger("img_rendition_backfill")

IMG_EXT_RE = r"\.(jpe?g|tiff?|png|gif|bmp)$"
SAFE_STATUSES = ("preserved", "pending", "geometry_empty")
LOCK_PATH = "/tmp/img_rendition_backfill.lock"
PY = sys.executable
APP = "/app"


def _db():
    import psycopg2
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    p = urlparse(raw)
    conn = psycopg2.connect(dbname=p.path.lstrip("/") or "praesidium",
                            user=p.username or "praesidium",
                            password=p.password or "",
                            host=p.hostname or "172.28.0.1",
                            port=str(p.port or 5432))
    conn.autocommit = False
    return conn


def queue_phase(conn, tenant, collection, dry_run):
    cur = conn.cursor()
    where = ("TRIM(tenant_id)=%s AND native_path IS NOT NULL "
             "AND rendition_path IS NULL "
             "AND lower(native_path) ~ %s "
             "AND processing_status IN %s")
    params = [tenant, IMG_EXT_RE, SAFE_STATUSES]
    if collection:
        where += " AND collection_id=CAST(%s AS uuid)"
        params.append(str(collection))
    if dry_run:
        cur.execute("SELECT collection_id::text, count(*) FROM ediscovery_documents "
                    "WHERE " + where + " GROUP BY 1", params)
        rows = cur.fetchall()
        conn.rollback()
    else:
        cur.execute("UPDATE ediscovery_documents SET processing_status='ocr_pending', "
                    "updated_at=now() WHERE " + where + " RETURNING collection_id::text",
                    params)
        from collections import Counter
        c = Counter(r[0] for r in cur.fetchall())
        rows = sorted(c.items())
        conn.commit()
    total = sum(n for _, n in rows)
    logger.info("queue phase: %d doc(s) across %d collection(s)%s",
                total, len(rows), " [dry-run]" if dry_run else "")
    for cid, n in rows:
        logger.info("  queued %5d  collection %s", n, cid)
    return total


def drain_phase(conn, tenant, collection, limit, dry_run):
    cur = conn.cursor()
    sql = ("SELECT d.collection_id::text, count(*), max(ec.collection_name), "
           "max(ec.status) "
           "FROM ediscovery_documents d "
           "JOIN ediscovery_collections ec ON ec.id = d.collection_id "
           "WHERE TRIM(d.tenant_id)=%s AND d.processing_status='ocr_pending' "
           "  AND d.native_path IS NOT NULL ")
    params = [tenant]
    if collection:
        sql += " AND d.collection_id=CAST(%s AS uuid)"
        params.append(str(collection))
    sql += " GROUP BY 1 ORDER BY 2"
    cur.execute(sql, params)
    targets = cur.fetchall()
    conn.rollback()
    if not targets:
        logger.info("drain phase: nothing pending")
        return 0
    done = 0
    for cid, n, name, cstatus in targets:
        if cstatus == "processing":
            logger.info("skip %s (%s): pipeline active, inline ocr stage owns it", name, cid)
            continue
        logger.info("drain %s (%s): %d pending", name, cid, n)
        if dry_run:
            continue
        cmd = ["nice", "-n", "10", PY, "-m",
               "modules.ediscovery.jobs.geometry_intake",
               "--stage", "ocr", "--tenant", tenant, "--collection", cid]
        if limit:
            cmd += ["--limit", str(limit)]
        r = subprocess.run(cmd, cwd=APP, capture_output=True, text=True)
        tail = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        logger.info("  rc=%d %s", r.returncode, (tail[-1] if tail else "")[:300])
        if r.returncode == 0:
            done += n
    return done


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get(
        "TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--collection", default=None)
    ap.add_argument("--queue-only", action="store_true")
    ap.add_argument("--drain-only", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="per-collection lane limit (0 = all)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("another backfill/drain is running; exiting")
        return

    tenant = args.tenant.strip()
    conn = _db()
    try:
        out = {"queued": 0, "drained": 0}
        if not args.drain_only:
            out["queued"] = queue_phase(conn, tenant, args.collection, args.dry_run)
        if not args.queue_only:
            out["drained"] = drain_phase(conn, tenant, args.collection,
                                         args.limit, args.dry_run)
        logger.info("DONE %s", json.dumps(out))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
