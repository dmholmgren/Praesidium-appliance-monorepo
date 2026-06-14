"""verify_collection.py -- terminal completeness + immutable manifest stage.

Read-only over the corpus (never mutates documents or renditions). Produces ONE
append-only ediscovery_manifests row reconciling:
  - preserve completeness : received/preserved/exploded/family_link done == doc_count
  - extraction completeness: text done+skipped == doc_count; ocr/language not pending
  - failed-extraction scan : ediscovery_stage_status.error_class set (or state error),
                             plus doc-level error processing/ocr status
  - disk-source accountability (only if originals/ present): non-skipped source files
                             vs primary (non-attachment) docs -> catches dropped sources
Flips the collection to review_ready only when reconciled clean. Idempotent: each
run appends a fresh manifest (history preserved). --dry-run writes nothing.

Blocking discrepancies prevent reconciliation; warnings are recorded but do not
(e.g. a genuinely empty file extracting to no text).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PRESERVE_STAGES = ("received", "preserved", "exploded", "family_link")


def _db_kwargs() -> dict:
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    p = urlparse(raw)
    return {"dbname": p.path.lstrip("/") or "praesidium", "user": p.username or "praesidium",
            "password": p.password or "", "host": p.hostname or "172.28.0.1",
            "port": str(p.port or 5432)}


def _connect():
    import psycopg2
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _resolve_collection(cur, tenant, cid):
    cur.execute("SELECT collection_name, storage_path, status FROM ediscovery_collections "
                "WHERE TRIM(tenant_id)=%s AND id=CAST(%s AS uuid)", (tenant, str(cid)))
    r = cur.fetchone()
    if not r:
        raise SystemExit("collection not found: %s" % cid)
    return {"name": r[0], "storage_path": r[1], "status": r[2]}


def _ledger_counts(cur, tenant, cid) -> dict:
    cur.execute("SELECT stage, state, count(*) FROM ediscovery_stage_status "
                "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid) "
                "GROUP BY stage, state", (tenant, str(cid)))
    out: dict = {}
    for stage, state, n in cur.fetchall():
        out.setdefault(stage, {})[state] = n
    return out


def _scalar(cur, sql, args) -> int:
    cur.execute(sql, args)
    return cur.fetchone()[0]


def _failed_scan(cur, tenant, cid):
    ledger_err = _scalar(cur,
        "SELECT count(DISTINCT document_id) FROM ediscovery_stage_status "
        "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid) "
        "AND (state IN ('error','failed') OR error_class IS NOT NULL)",
        (tenant, str(cid)))
    doc_err = _scalar(cur,
        "SELECT count(*) FROM ediscovery_documents "
        "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid) "
        "AND (processing_status IN ('error','failed','extract_error') "
        "     OR ocr_status IN ('error','failed'))",
        (tenant, str(cid)))
    cur.execute(
        "SELECT document_id::text, "
        "       btrim(COALESCE(error_class,'')||' '||COALESCE(left(error_message,160),'')) "
        "FROM ediscovery_stage_status "
        "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid) "
        "AND (state IN ('error','failed') OR error_class IS NOT NULL) LIMIT 25",
        (tenant, str(cid)))
    sample = [{"doc_id": r[0], "reason": r[1]} for r in cur.fetchall()]
    suspicious_empty = _scalar(cur,
        "SELECT count(*) FROM ediscovery_documents d "
        "WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid) "
        "AND COALESCE(d.processing_status,'') <> 'ocr_pending' "
        "AND (d.extracted_text IS NULL OR length(btrim(d.extracted_text))=0) "
        "AND EXISTS (SELECT 1 FROM ediscovery_stage_status s "
        "            WHERE s.document_id=d.id AND s.stage='text' AND s.state='done')",
        (tenant, str(cid)))
    return ledger_err, doc_err, suspicious_empty, sample


def _disk_sources(coll_root: Path) -> Optional[int]:
    """Non-skipped source files in originals/, or None if the dir is absent."""
    originals = coll_root / "originals"
    if not originals.is_dir():
        return None
    try:
        from modules.ediscovery.jobs.preserve_collection import _iter_files, _should_skip
    except Exception as e:
        logger.warning("disk-source check unavailable: %s", e)
        return None
    n = 0
    for f in _iter_files(originals):
        if not _should_skip(f.name):
            n += 1
    return n


def _reconcile(doc_count, ledger, ledger_err, doc_err, suspicious_empty,
               source_count, primary_docs):
    """Return (reconciled, blocking[], warnings[])."""
    blocking, warnings = [], []
    g = lambda st, stt: ledger.get(st, {}).get(stt, 0)

    for st in PRESERVE_STAGES:
        d = g(st, "done")
        if d != doc_count:
            blocking.append("%s: %d/%d done" % (st, d, doc_count))

    text_terminal = g("text", "done") + g("text", "skipped")
    if text_terminal != doc_count:
        blocking.append("text: %d/%d terminal (done+skipped)" % (text_terminal, doc_count))
    if g("ocr", "pending") > 0:
        blocking.append("ocr lane not drained: %d pending" % g("ocr", "pending"))
    if g("language", "pending") > 0:
        blocking.append("language incomplete: %d pending" % g("language", "pending"))
    if ledger_err:
        blocking.append("%d doc(s) with ledger error/error_class" % ledger_err)
    if doc_err:
        blocking.append("%d doc(s) with error processing/ocr status" % doc_err)
    if source_count is not None and primary_docs < source_count:
        blocking.append("source accountability: %d primary docs < %d disk sources "
                        "(possible dropped source)" % (primary_docs, source_count))

    if suspicious_empty:
        warnings.append("%d doc(s) extracted empty (text done, no text)" % suspicious_empty)
    if source_count is None:
        warnings.append("originals/ not on disk: disk-source reconciliation skipped")

    return (len(blocking) == 0), blocking, warnings


def verify_collection(tenant_id: str, collection_id: str,
                      dry_run: bool = False, flip: bool = True) -> dict:
    tenant = tenant_id.strip()
    worker_id = os.environ.get("HOSTNAME", "verify")
    conn = _connect()
    try:
        cur = conn.cursor()
        coll = _resolve_collection(cur, tenant, collection_id)
        coll_root = Path(coll["storage_path"])

        doc_count = _scalar(cur,
            "SELECT count(*) FROM ediscovery_documents "
            "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)",
            (tenant, str(collection_id)))
        primary_docs = _scalar(cur,
            "SELECT count(*) FROM ediscovery_documents "
            "WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid) AND is_attachment=false",
            (tenant, str(collection_id)))
        ledger = _ledger_counts(cur, tenant, collection_id)
        ledger_err, doc_err, suspicious_empty, sample = _failed_scan(cur, tenant, collection_id)
        source_count = _disk_sources(coll_root)

        reconciled, blocking, warnings = _reconcile(
            doc_count, ledger, ledger_err, doc_err, suspicious_empty, source_count, primary_docs)
        discrepancies = blocking + ["(warn) " + w for w in warnings]
        failed_count = ledger_err + doc_err
        deferred_ocr = ledger.get("ocr", {}).get("pending", 0)

        canon = json.dumps({
            "collection_id": str(collection_id), "doc_count": doc_count,
            "source_count": source_count, "primary_docs": primary_docs,
            "failed_count": failed_count, "stage_counts": ledger,
            "reconciled": reconciled,
        }, sort_keys=True)
        manifest_sha = hashlib.sha256(canon.encode("utf-8")).hexdigest()

        manifest = {
            "collection_id": str(collection_id), "collection_name": coll["name"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "doc_count": doc_count, "source_count": source_count, "primary_docs": primary_docs,
            "deferred_ocr": deferred_ocr, "failed_count": failed_count,
            "suspicious_empty": suspicious_empty, "stage_counts": ledger,
            "discrepancies": discrepancies, "reconciled": reconciled,
            "manifest_sha": manifest_sha, "dry_run": dry_run,
        }

        logger.info("verify %s (%s): docs=%d primary=%d sources=%s failed=%d defer_ocr=%d reconciled=%s",
                    coll["name"], collection_id, doc_count, primary_docs,
                    source_count, failed_count, deferred_ocr, reconciled)
        for d in blocking:
            logger.warning("  BLOCK: %s", d)
        for w in warnings:
            logger.info("  warn:  %s", w)

        if not dry_run:
            cur.execute(
                "INSERT INTO ediscovery_manifests "
                "(tenant_id, collection_id, created_by, doc_count, source_count, deferred_ocr, "
                " failed_count, stage_counts, failed_sample, discrepancies, reconciled, manifest_sha) "
                "VALUES (%s, CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (tenant, str(collection_id), worker_id, doc_count, source_count, deferred_ocr,
                 failed_count, json.dumps(ledger), json.dumps(sample),
                 json.dumps(discrepancies), reconciled, manifest_sha))
            if reconciled and flip and coll["status"] != "review_ready":
                cur.execute("UPDATE ediscovery_collections SET status='review_ready' "
                            "WHERE TRIM(tenant_id)=%s AND id=CAST(%s AS uuid)",
                            (tenant, str(collection_id)))
                logger.info("  collection -> review_ready")
            conn.commit()
        return manifest
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--collection", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-flip", action="store_true")
    args = ap.parse_args()
    out = verify_collection(args.tenant, args.collection,
                            dry_run=args.dry_run, flip=not args.no_flip)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
