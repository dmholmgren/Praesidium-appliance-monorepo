"""
modules/depositions/jobs/exhibit_list_ingest.py

Turn an exhibit-LIST filing into Trial Center exhibits.

When the ingest router classifies a document as 'exhibit_list', this parses the
list into entries (exhibit number/label, description, Bates range) and resolves
each Bates range against the already-ingested production (ediscovery_documents,
matter-scoped via collection) to the produced document — then creates
trial_exhibits + document_identifiers so the exhibits appear in the Trial Center
automatically. No re-extraction of the underlying docs: the production is assumed
present ("by the time we get an exhibit list we already have the production"),
so resolution is just a Bates-range lookup.

Bates match = same alpha prefix AND the entry's begin number within a produced
doc's [bates_begin .. bates_end] numeric span.

Idempotent: an exhibit already present for (matter_id, exhibit_number) is skipped.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.exhibit_list_ingest --tenant T --doc DMS_DOC_UUID \
        [--matter MATTER_UUID] [--dry]
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

_BATES_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9._-]*?)[ _-]?0*(\d+)\s*$")
_PARTY_PREFIX = {"P": "plaintiff", "PL": "plaintiff", "PX": "plaintiff", "PLF": "plaintiff",
                 "D": "defendant", "DX": "defendant", "DEF": "defendant", "DF": "defendant"}

_SYS = ("You extract a litigation EXHIBIT LIST into structured rows. Return ONLY a "
        "JSON array; each element: {\"number\": str, \"label\": str, "
        "\"bates_begin\": str|null, \"bates_end\": str|null, \"party\": "
        "\"plaintiff\"|\"defendant\"|null}. number is the exhibit designation (e.g. '1', "
        "'A', 'PX-12'); label is a SHORT title. Use the literal Bates numbers as printed "
        "(null if none). Return EVERY exhibit row. No prose, no code fence.")


def _parse_bates(s):
    """'WEIR000123' -> ('WEIR', 123); returns None if not parseable."""
    if not s:
        return None
    m = _BATES_RE.match(str(s))
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def _party_from(number, explicit):
    if explicit in ("plaintiff", "defendant"):
        return explicit
    m = re.match(r"^\s*([A-Za-z]{1,3})", number or "")
    return _PARTY_PREFIX.get(m.group(1).upper()) if m else None


async def _load_text(s, document_id):
    """Body text of the exhibit-list doc — dms_documents.content_text first,
    then the curated documents.extracted_text/ocr_text."""
    r = await s.execute(sa_text(
        "SELECT content_text FROM dms_documents WHERE id = CAST(:d AS uuid)"),
        {"d": document_id})
    row = r.mappings().fetchone()
    if row and (row["content_text"] or "").strip():
        return row["content_text"]
    r = await s.execute(sa_text(
        "SELECT COALESCE(NULLIF(extracted_text,''), ocr_text) AS t "
        "FROM documents WHERE id = CAST(:d AS uuid)"), {"d": document_id})
    row = r.mappings().fetchone()
    return (row["t"] if row else "") or ""


async def _extract_entries(tenant, matter_id, document_id, text):
    """LLM-parse the list into entries via the mandated adapter."""
    from modules.intelligence.anthropic_adapter import call, AICallContext
    if not (text or "").strip():
        return []
    ctx = AICallContext(tenant_id=tenant, module="intelligence", purpose="chat",
                        matter_id=matter_id, document_id=document_id,
                        document_source="dms_documents")
    res = await call(ctx, raw_system_prompt=_SYS,
                     raw_user_prompt="EXHIBIT LIST:\n\n" + text[:22000],
                     max_tokens_override=8000)
    raw = (getattr(res, "text", "") or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[raw.find("["):]
    try:
        data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
    except Exception:
        logger.warning("exhibit_list parse: non-JSON model output for %s", document_id)
        return []
    return data if isinstance(data, list) else []


async def _resolve_bates(s, tenant, matter_id, b_begin):
    """Produced document whose Bates span contains b_begin. Matter-scoped via
    the eDiscovery collection. Returns (doc_id, 'ediscovery') or (None, None)."""
    pb = _parse_bates(b_begin)
    if not pb:
        return None, None
    prefix, num = pb
    rows = (await s.execute(sa_text(
        "SELECT ed.id::text AS id, ed.bates_begin, ed.bates_end "
        "FROM ediscovery_documents ed "
        "JOIN ediscovery_collections ec ON ec.id = ed.collection_id "
        "WHERE TRIM(ed.tenant_id) = TRIM(:t) AND ec.matter_id = CAST(:m AS uuid) "
        "  AND ed.bates_begin ILIKE :pfx"),
        {"t": tenant, "m": matter_id, "pfx": prefix + "%"})).mappings().fetchall()
    for r in rows:
        pbg, pen = _parse_bates(r["bates_begin"]), _parse_bates(r["bates_end"] or r["bates_begin"])
        if not pbg:
            continue
        end_num = pen[1] if pen else pbg[1]
        if pbg[0] == prefix and pbg[1] <= num <= end_num:
            return r["id"], "ediscovery"
    return None, None


async def ingest_exhibit_list(tenant_id, document_id, matter_id=None, commit=True) -> dict:
    """Parse an exhibit-list doc and create Trial Center exhibits from it."""
    tenant = (tenant_id or "").strip()
    out = {"entries": 0, "created": 0, "resolved": 0, "relinked": 0, "skipped": 0, "unresolved": []}
    async with AsyncSessionLocal() as s:
        if not matter_id:
            r = await s.execute(sa_text(
                "SELECT matter_id::text FROM documents WHERE id = CAST(:d AS uuid)"),
                {"d": document_id})
            row = r.mappings().fetchone()
            matter_id = row["matter_id"] if row else None
        if not matter_id:
            out["error"] = "no matter for document"
            return out

        trial_id = (await s.execute(sa_text(
            "SELECT trial_id::text FROM trial_exhibits "
            "WHERE matter_id = CAST(:m AS uuid) AND trial_id IS NOT NULL LIMIT 1"),
            {"m": matter_id})).scalar()

        text = await _load_text(s, document_id)
        entries = await _extract_entries(tenant, matter_id, document_id, text)
        out["entries"] = len(entries)

        for e in entries:
            number = str(e.get("number") or "").strip()
            if not number:
                continue
            doc_id, doc_src = await _resolve_bates(s, tenant, matter_id, e.get("bates_begin"))
            if doc_id:
                out["resolved"] += 1
            else:
                out["unresolved"].append(number)
            party = _party_from(number, e.get("party"))
            notes = ("Bates %s–%s" % (e.get("bates_begin"), e.get("bates_end"))
                     if e.get("bates_begin") else None)
            existing = (await s.execute(sa_text(
                "SELECT id::text, document_id::text FROM trial_exhibits "
                "WHERE matter_id = CAST(:m AS uuid) AND TRIM(tenant_id) = TRIM(:t) "
                "  AND exhibit_number = :n AND COALESCE(party,'') = COALESCE(:p,'')"),
                {"m": matter_id, "t": tenant, "n": number, "p": party})).mappings().fetchone()
            if existing:
                # re-run: backfill the link once the production lands; never clobber a real link
                if existing["document_id"] is None and doc_id:
                    await s.execute(sa_text(
                        "UPDATE trial_exhibits SET document_id = CAST(:doc AS uuid), "
                        " document_source = :src, updated_at = now() WHERE id = CAST(:id AS uuid)"),
                        {"doc": doc_id, "src": doc_src, "id": existing["id"]})
                    out["relinked"] += 1
                    ex_id = existing["id"]
                else:
                    out["skipped"] += 1
                    continue
            else:
                ex_id = (await s.execute(sa_text(
                    "INSERT INTO trial_exhibits "
                    "  (tenant_id, matter_id, trial_id, party, exhibit_number, exhibit_label, "
                    "   document_id, document_source, status, admitted, notes) "
                    "VALUES (TRIM(:t), CAST(:m AS uuid), CAST(:tr AS uuid), :p, :n, :lbl, "
                    "        CAST(:doc AS uuid), :src, 'marked', FALSE, :notes) RETURNING id::text"),
                    {"t": tenant, "m": matter_id, "tr": trial_id, "p": party, "n": number,
                     "lbl": (e.get("label") or None), "doc": doc_id, "src": doc_src,
                     "notes": notes})).scalar()
                out["created"] += 1
            # cross-context identity: exhibit number + Bates range (backfills link on re-run)
            await s.execute(sa_text(
                "INSERT INTO document_identifiers "
                "  (tenant_id, matter_id, document_id, document_source, id_kind, id_value, "
                "   context_ref, source_table, source_id, notes) "
                "VALUES (TRIM(:t), CAST(:m AS uuid), CAST(:doc AS uuid), :src, 'trial_exhibit', "
                "        :n, :ctx, 'trial_exhibits', CAST(:sid AS uuid), :notes) "
                "ON CONFLICT (source_table, source_id, id_kind) DO UPDATE SET "
                "  document_id = EXCLUDED.document_id, document_source = EXCLUDED.document_source"),
                {"t": tenant, "m": matter_id, "doc": doc_id, "src": doc_src, "n": number,
                 "ctx": document_id, "sid": ex_id, "notes": notes})

        if commit:
            await s.commit()
        else:
            await s.rollback()
    logger.info("exhibit_list_ingest doc=%s matter=%s entries=%d created=%d resolved=%d",
                document_id, matter_id, out["entries"], out["created"], out["resolved"])
    return out


if __name__ == "__main__":
    import argparse, asyncio
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--doc", required=True, help="dms_documents.id of the exhibit list")
    ap.add_argument("--matter", default=None)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    print(json.dumps(asyncio.run(
        ingest_exhibit_list(a.tenant, a.doc, matter_id=a.matter, commit=not a.dry)), indent=2))
