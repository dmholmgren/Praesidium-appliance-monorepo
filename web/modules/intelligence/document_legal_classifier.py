"""
modules/intelligence/document_legal_classifier.py

Legal classification for the CURATED `documents` registry (what the DMS / Trial
Center read), writing documents.legal_category + legal_meta. Reuses the persisted
classifier's deterministic rule engine and AI-on-residue from
modules.intelligence.document_classifier (both are registry-agnostic: they take a
filename + a text head), but persists onto `documents` instead of
classification_results (which is keyed on the disjoint dms_documents registry).

Structural-first: the classification_rules regex floor over filename + extracted
text resolves the obvious filings (motions, responses, petitions, discovery
responses); AI-on-residue (opt-in) classifies the rest. Idempotent — skips docs
that already carry a legal_category unless force=True.

Plumbed into the DMS extraction pipeline (ocr_pipeline) so new docs classify
automatically; runnable per-matter from the CLI for backfill.

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.document_legal_classifier --tenant T --matter M [--ai] [--force] [--limit N]
  python -m modules.intelligence.document_legal_classifier --tenant T --doc DOC_UUID [--ai]
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.intelligence.document_classifier import (
    _load_rules, _structural, _load_menu, _ai_classify,
)

logger = logging.getLogger(__name__)
DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
_HEAD = 4000


async def _tax_by_id(s):
    rows = (await s.execute(sa_text(
        "SELECT id::text id, code, category FROM document_type_taxonomy WHERE is_active"
    ))).mappings().fetchall()
    return {r["id"]: (r["code"], r["category"]) for r in rows}


async def classify_documents(tenant_id=DEFAULT_TENANT, *, matter_id=None, doc_ids=None,
                             use_ai=False, force=False, limit=None) -> dict:
    """Classify curated documents -> documents.legal_category. Scope by matter or
    explicit ids."""
    tid = (tenant_id or "").strip()
    out = {"scanned": 0, "classified": 0, "rule": 0, "ai": 0, "skipped": 0}
    async with AsyncSessionLocal() as s:
        rules = await _load_rules(s)
        tax = await _tax_by_id(s)
        menu = valid = None
        if use_ai:
            menu, valid = await _load_menu(s)

        where = ["TRIM(d.tenant_id) = TRIM(:t)"]
        params = {"t": tid}
        if doc_ids:
            where.append("d.id = ANY(CAST(:ids AS uuid[]))")
            params["ids"] = doc_ids
        if matter_id:
            where.append("d.matter_id = CAST(:m AS uuid)")
            params["m"] = matter_id
        if not force:
            where.append("d.legal_category IS NULL")
        # Text source: curated extracted/ocr text, else the OCR text the extract
        # job wrote to the dms_documents crawl row (bridged by absolute path).
        sql = ("SELECT d.id::text id, "
               "COALESCE(NULLIF(d.title,''), d.original_filename, d.filename, '') fn, "
               "LEFT(COALESCE(NULLIF(d.extracted_text,''), NULLIF(d.ocr_text,''), dd.content_text, ''), %d) head "
               "FROM documents d "
               "LEFT JOIN LATERAL (SELECT content_text FROM dms_documents x "
               "  WHERE x.file_path = d.storage_path AND TRIM(x.tenant_id) = TRIM(d.tenant_id) "
               "  ORDER BY x.updated_at DESC NULLS LAST LIMIT 1) dd ON TRUE "
               "WHERE " % _HEAD) + " AND ".join(where) + \
              " ORDER BY d.created_at DESC NULLS LAST"
        if limit:
            sql += " LIMIT :lim"; params["lim"] = int(limit)
        rows = (await s.execute(sa_text(sql), params)).mappings().fetchall()

        for r in rows:
            out["scanned"] += 1
            code = conf = method = None
            dec = _structural(r["fn"], r["head"], rules)
            if dec:
                code = (tax.get(dec["type_id"]) or (None,))[0]
                conf, method = dec["confidence"], "rule"
            elif use_ai and valid:
                ai = await _ai_classify(tid, None, r["fn"], r["head"], menu, valid)
                if ai:
                    code, conf, method = ai["code"], ai["confidence"], "ai"
            if not code:
                out["skipped"] += 1
                continue
            await s.execute(sa_text(
                "UPDATE documents SET legal_category = :c, "
                " legal_meta = COALESCE(legal_meta, '{}'::jsonb) || CAST(:meta AS jsonb), updated_at = now() "
                "WHERE id = CAST(:id AS uuid)"),
                {"c": code, "id": r["id"],
                 "meta": json.dumps({"method": method, "confidence": conf})})
            out["classified"] += 1
            out[method] = out.get(method, 0) + 1
        await s.commit()
    logger.info("legal classify tenant=%s matter=%s scanned=%d classified=%d (rule=%d ai=%d)",
                tid, matter_id, out["scanned"], out["classified"], out["rule"], out["ai"])
    return out


def classify_one_sync(tenant_id, document_id):
    """Sync entry for the extraction pipeline (RQ worker). Structural-only +
    best-effort; never raises into the caller."""
    import asyncio
    try:
        return asyncio.run(classify_documents(tenant_id, doc_ids=[document_id], use_ai=False))
    except Exception:
        logger.exception("legal classify (pipeline) failed for %s", document_id)
        return None


async def classify_and_route(tenant_id, document_id):
    """Classify ONE curated doc, then route by type. exhibit_list -> auto-ingest
    its exhibits into the Trial Center (exhibit_list_ingest). Other types just
    carry their legal_category (Pleadings/Motions tabs project off it)."""
    tid = (tenant_id or "").strip()
    out = {"classify": await classify_documents(tid, doc_ids=[document_id], use_ai=False)}
    async with AsyncSessionLocal() as s:
        row = (await s.execute(sa_text(
            "SELECT legal_category, matter_id::text AS matter_id FROM documents "
            "WHERE id = CAST(:d AS uuid)"), {"d": document_id})).mappings().fetchone()
    cat = row["legal_category"] if row else None
    out["legal_category"] = cat
    if cat == "exhibit_list":
        try:
            from modules.depositions.jobs.exhibit_list_ingest import ingest_exhibit_list
            out["ingest"] = await ingest_exhibit_list(tid, document_id,
                                                      matter_id=row["matter_id"])
        except Exception:
            logger.exception("auto exhibit_list ingest failed for %s", document_id)
    return out


def classify_and_route_one_sync(tenant_id, document_id):
    """Sync RQ entry: classify a curated doc and route exhibit lists. Best-effort."""
    import asyncio
    try:
        return asyncio.run(classify_and_route(tenant_id, document_id))
    except Exception:
        logger.exception("classify_and_route failed for %s", document_id)
        return None


if __name__ == "__main__":
    import argparse, asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter", default=None)
    ap.add_argument("--doc", action="append", dest="docs", default=None)
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    print(json.dumps(asyncio.run(classify_documents(
        a.tenant, matter_id=a.matter, doc_ids=a.docs,
        use_ai=a.ai, force=a.force, limit=a.limit)), indent=2))
