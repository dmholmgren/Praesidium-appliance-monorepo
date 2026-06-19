"""document_classifier.py — the persisted, reviewable, correctable document
classifier (Court/Hearing build, Step 2 shared-core item #1).

Writes one live row per document to classification_results, following the
shared-core principle *structural-first, model-on-residue*:

  1. structural — the deterministic classification_rules floor (regex →
     document_type_taxonomy). High precision, small yield. method='rule'.
  2. model-on-residue — only documents no rule resolves go to the mandated
     AI adapter (modules.intelligence.anthropic_adapter, routed on
     classification/document_type). method='ai'. Opt-in via use_ai.

Provenance / correctability (sacred):
  * Nothing is ever overwritten. A new decision SUPERSEDES the prior one by
    pointing the old row's superseded_by_id at the new row; the live decision
    is always `superseded_by_id IS NULL`.
  * A human correction is method='manual' and is NEVER auto-superseded by a
    re-run — re-classification skips any document whose live row is manual.
  * Idempotent: a re-run that would reproduce the existing live decision
    (same type + same source) is a no-op, so re-running churns nothing.

Every batch is grouped under an extraction_runs row (run_type='classification').

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.document_classifier --matter <UUID> [--tenant T]
        [--limit 200] [--ai] [--all] [--debug]
  python -m modules.intelligence.document_classifier --doc <UUID> [--doc <UUID> ...] --ai
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"   # HJMM
CONTENT_HEAD = 4000          # chars of content_text fed to rules + AI
AI_ASSIGN_FLOOR = 0.55       # below this the AI answer is treated as residue
DMS_ROOT = "/mnt/praesidium" # dms_documents.file_path root

# --- AI pacing (mirror of notice_extractor: a burst of unpaced calls trips
#     Cloudflare rate/bot mitigation on the shared egress IP; a missing per-call
#     timeout makes it HANG). All env-tunable. ---
AI_THROTTLE_S = float(os.environ.get("CLASSIFY_AI_THROTTLE_S", "1.2"))
AI_CALL_TIMEOUT = float(os.environ.get("CLASSIFY_AI_TIMEOUT_S", "45"))
AI_MAX_RETRIES = int(os.environ.get("CLASSIFY_AI_RETRIES", "4"))
AI_BACKOFF_BASE = float(os.environ.get("CLASSIFY_AI_BACKOFF_S", "5"))
_TRANSIENT_HINTS = ("blocked", "cloudflare", "429", "rate", "overloaded",
                    "too many", "timeout", "timed out", "502", "503",
                    "unavailable", "connection", "reset")


def _is_transient(err):
    if isinstance(err, asyncio.TimeoutError):
        return True
    s = (str(err) or "").lower()
    return any(h in s for h in _TRANSIENT_HINTS)


# --------------------------------------------------------------------------- #
#  Target resolution                                                          #
# --------------------------------------------------------------------------- #
async def _resolve_targets(s, tid, matter_id, doc_ids, only_unclassified, limit):
    """Return [{id, filename, head}] of dms_documents to classify."""
    where = ["TRIM(d.tenant_id) = TRIM(:tid)"]
    params = {"tid": tid, "lim": limit}

    if doc_ids:
        where.append("d.id = ANY(CAST(:ids AS uuid[]))")
        params["ids"] = list(doc_ids)
    elif matter_id:
        # dms_documents has no matter_id; scope by the matter's disk prefixes
        # (matter_folders.disk_root UNION matters.folder_path) via the shared
        # resolver — folder_path alone is stale/NULL for most matters (v1.1 A3).
        from modules.intelligence.matter_paths import disk_prefixes
        prefixes = await disk_prefixes(s, tid, matter_id)
        if not prefixes:
            return []
        where.append("d.file_path LIKE ANY(:prefixes)")
        params["prefixes"] = prefixes

    if only_unclassified:
        where.append(
            "NOT EXISTS (SELECT 1 FROM classification_results cr "
            "WHERE cr.dms_document_id = d.id AND cr.superseded_by_id IS NULL)")

    sql = (
        "SELECT d.id::text AS id, d.file_path, "
        f"       LEFT(COALESCE(d.content_text,''), {CONTENT_HEAD}) AS head "
        "FROM dms_documents d WHERE " + " AND ".join(where) +
        " ORDER BY d.indexed_at DESC NULLS LAST LIMIT :lim")
    rows = (await s.execute(sa_text(sql), params)).mappings().fetchall()
    return [{"id": r["id"],
             "filename": os.path.basename(r["file_path"] or ""),
             "head": r["head"] or ""} for r in rows]


# --------------------------------------------------------------------------- #
#  Structural pass                                                            #
# --------------------------------------------------------------------------- #
async def _load_rules(s):
    rows = (await s.execute(sa_text(
        "SELECT id::text AS rule_id, rule_name, match_value, confidence, "
        "       document_type_id::text AS type_id "
        "FROM classification_rules WHERE is_active "
        "ORDER BY priority ASC, rule_name ASC"))).mappings().fetchall()
    out = []
    for r in rows:
        try:
            rx = re.compile(r["match_value"], re.IGNORECASE)
        except re.error:
            logger.warning("bad rule regex skipped: %s", r["rule_name"])
            continue
        out.append((rx, r["rule_id"], r["type_id"],
                    float(r["confidence"]), r["rule_name"]))
    return out


def _structural(filename, head, rules):
    haystack = f"{filename}\n{head}"
    for rx, rule_id, type_id, conf, name in rules:
        if rx.search(haystack):
            return {"type_id": type_id, "rule_id": rule_id, "confidence": conf,
                    "method": "rule", "rule_name": name}
    return None


# --------------------------------------------------------------------------- #
#  Model-on-residue pass                                                      #
# --------------------------------------------------------------------------- #
async def _load_menu(s):
    rows = (await s.execute(sa_text(
        "SELECT code, display_name, category, COALESCE(definition,'') def "
        "FROM document_type_taxonomy WHERE is_active "
        "ORDER BY category, sort_order"))).mappings().fetchall()
    by_code = {r["code"]: None for r in rows}  # filled with ids by caller
    menu = "\n".join(
        f"- {r['code']} ({r['category']}): {r['display_name']} — {r['def']}"
        for r in rows)
    return menu, set(by_code)


_AI_SYSTEM = (
    "You are a litigation document-type classifier. Choose the single best "
    "type CODE from the provided menu for the document. Reply with ONLY a JSON "
    'object: {"code": "<code or null>", "confidence": <0..1>}. Use null when no '
    "menu type fits. Judge by the document's nature, not surface keywords.")


async def _ai_classify(tid, user_id, filename, head, menu, valid_codes):
    try:
        from modules.intelligence import anthropic_adapter
    except Exception:
        logger.warning("anthropic_adapter unavailable; AI residue skipped")
        return None
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="classification", purpose="document_type",
        user_id=user_id)
    prompt = (f"Document type menu:\n{menu}\n\n"
              f"Filename: {filename}\n"
              f"Text (truncated):\n{(head or '(no text)')[:3500]}\n\n"
              "Return the JSON object now.")
    res = None
    for attempt in range(AI_MAX_RETRIES + 1):
        try:
            res = await asyncio.wait_for(
                anthropic_adapter.call(
                    ctx, raw_user_prompt=prompt, raw_system_prompt=_AI_SYSTEM,
                    max_tokens_override=120),
                timeout=AI_CALL_TIMEOUT)
            break
        except Exception as e:
            if attempt >= AI_MAX_RETRIES or not _is_transient(e):
                logger.warning("AI classify failed (%s): %s",
                               type(e).__name__, str(e)[:160])
                return None
            delay = min(90.0, AI_BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 2)
            logger.warning("AI transient (%s); attempt %d/%d, backoff %.1fs",
                           type(e).__name__, attempt + 1, AI_MAX_RETRIES, delay)
            await asyncio.sleep(delay)
    if res is None:
        return None
    obj = _extract_json(res.text)
    if not obj:
        return None
    code = obj.get("code")
    if not code or code not in valid_codes:
        return None
    try:
        conf = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < AI_ASSIGN_FLOOR:
        return None
    return {"code": code, "confidence": conf, "method": "ai",
            "model": res.model_used, "in_tok": res.input_tokens,
            "out_tok": res.output_tokens}


def _extract_json(t):
    if not t:
        return None
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Persistence — supersede, never overwrite                                   #
# --------------------------------------------------------------------------- #
async def _live(s, doc_id):
    return (await s.execute(sa_text(
        "SELECT id::text id, document_type_id::text type_id, "
        "       classification_method method, rule_id::text rule_id "
        "FROM classification_results "
        "WHERE dms_document_id = CAST(:d AS uuid) AND superseded_by_id IS NULL "
        "ORDER BY classified_at DESC LIMIT 1"),
        {"d": doc_id})).mappings().fetchone()


async def _persist(s, tid, run_id, doc_id, decision, *, reviewed_by=None):
    """Insert a new live classification, superseding any prior live row.
    Returns 'inserted' | 'skipped' | 'protected'. Never overwrites a manual row
    on an automated pass; idempotent against an identical prior decision."""
    cur = await _live(s, doc_id)
    method = decision["method"]
    if cur:
        # human corrections are sacred — automated passes leave them alone.
        if cur["method"] == "manual" and method != "manual":
            return "protected"
        # idempotent: same type from the same source → no churn.
        same_src = (method != "rule") or (cur["rule_id"] == decision.get("rule_id"))
        if cur["type_id"] == decision["type_id"] and cur["method"] == method \
                and same_src:
            return "skipped"

    new_id = (await s.execute(sa_text(
        "INSERT INTO classification_results "
        "(tenant_id, dms_document_id, extraction_run_id, document_type_id, "
        " classification_method, confidence, rule_id, ai_model_used, "
        " ai_prompt_tokens, ai_completion_tokens, reviewed_by, reviewed_at) "
        "VALUES (:tid, CAST(:d AS uuid), CAST(:run AS uuid), CAST(:tp AS uuid), "
        " :m, :cf, :rule, :model, :itok, :otok, CAST(:rev AS bigint), "
        " CASE WHEN CAST(:rev AS bigint) IS NULL THEN NULL ELSE now() END) "
        "RETURNING id::text"),
        {"tid": tid, "d": doc_id, "run": run_id, "tp": decision["type_id"],
         "m": method, "cf": decision.get("confidence"),
         "rule": decision.get("rule_id"), "model": decision.get("model"),
         "itok": decision.get("in_tok"), "otok": decision.get("out_tok"),
         "rev": reviewed_by})).scalar()

    await s.execute(sa_text(
        "UPDATE classification_results SET superseded_by_id = CAST(:new AS uuid) "
        "WHERE dms_document_id = CAST(:d AS uuid) AND superseded_by_id IS NULL "
        "AND id <> CAST(:new AS uuid)"),
        {"new": new_id, "d": doc_id})
    return "inserted"


# --------------------------------------------------------------------------- #
#  Batch driver                                                               #
# --------------------------------------------------------------------------- #
async def classify_documents(tid=DEFAULT_TENANT, *, matter_id=None, doc_ids=None,
                             only_unclassified=True, limit=200, use_ai=False,
                             user_id=None):
    summary = {"targets": 0, "rule": 0, "ai": 0, "residue": 0,
               "skipped": 0, "protected": 0, "run_id": None}
    async with AsyncSessionLocal() as s:
        targets = await _resolve_targets(
            s, tid, matter_id, doc_ids, only_unclassified, limit)
        summary["targets"] = len(targets)
        if not targets:
            return summary
        rules = await _load_rules(s)
        menu, valid_codes = await _load_menu(s)
        # taxonomy code -> id (for the AI path)
        code_to_id = {r["code"]: r["id"] for r in (await s.execute(sa_text(
            "SELECT code, id::text id FROM document_type_taxonomy WHERE is_active"
        ))).mappings().fetchall()}

        run_id = (await s.execute(sa_text(
            "INSERT INTO extraction_runs "
            "(tenant_id, run_type, source_type, status, document_count, "
            " extraction_model, started_at, created_by) "
            "VALUES (:tid, 'classification', 'dms', 'running', :n, "
            " :model, now(), :uid) RETURNING id::text"),
            {"tid": tid, "n": len(targets),
             "model": ("ai+rules" if use_ai else "rules"),
             "uid": user_id})).scalar()
        summary["run_id"] = run_id
        await s.commit()

        processed = 0
        for t in targets:
            decision = _structural(t["filename"], t["head"], rules)
            src = "rule"
            if not decision and use_ai:
                ai = await _ai_classify(tid, user_id, t["filename"], t["head"],
                                        menu, valid_codes)
                if ai:
                    ai["type_id"] = code_to_id.get(ai["code"])
                    if ai["type_id"]:
                        decision, src = ai, "ai"
                # pace calls so a large residue batch doesn't trip egress limits
                await asyncio.sleep(AI_THROTTLE_S)
            if not decision:
                summary["residue"] += 1
                continue
            outcome = await _persist(s, tid, run_id, t["id"], decision,
                                     reviewed_by=None)
            if outcome == "inserted":
                summary[src] += 1
            elif outcome == "skipped":
                summary["skipped"] += 1
            elif outcome == "protected":
                summary["protected"] += 1
            processed += 1
            if processed % 25 == 0:
                await s.commit()

        await s.execute(sa_text(
            "UPDATE extraction_runs SET status='completed', completed_at=now(), "
            "documents_processed=:p WHERE id = CAST(:run AS uuid)"),
            {"p": processed, "run": run_id})
        await s.commit()
    return summary


async def correct_classification(tid, dms_document_id, code, user_id):
    """Human correction → a method='manual' live row that supersedes prior and
    is itself protected from future automated passes. Returns the new row id."""
    async with AsyncSessionLocal() as s:
        tp = (await s.execute(sa_text(
            "SELECT id::text FROM document_type_taxonomy "
            "WHERE code = :c AND is_active"), {"c": code})).scalar()
        if not tp:
            raise ValueError(f"unknown taxonomy code: {code}")
        run_id = (await s.execute(sa_text(
            "INSERT INTO extraction_runs (tenant_id, run_type, source_type, "
            " status, document_count, documents_processed, started_at, "
            " completed_at, created_by) "
            "VALUES (:tid,'classification','manual','completed',1,1,now(),now(),"
            " :uid) RETURNING id::text"),
            {"tid": tid, "uid": user_id})).scalar()
        outcome = await _persist(
            s, tid, run_id, dms_document_id,
            {"type_id": tp, "method": "manual", "confidence": 1.0},
            reviewed_by=user_id)
        await s.commit()
        new = await _live(s, dms_document_id)
        return {"outcome": outcome, "classification_id": new["id"] if new else None}


async def get_classification(tid, dms_document_id):
    """Live decision + full supersede history for one document."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT cr.id::text id, t.code, t.display_name, t.category, "
            "       cr.classification_method method, cr.confidence, "
            "       cr.ai_model_used, cr.rule_id::text rule_id, "
            "       cr.classified_at, cr.superseded_by_id::text superseded_by, "
            "       cr.reviewed_by "
            "FROM classification_results cr "
            "LEFT JOIN document_type_taxonomy t ON t.id = cr.document_type_id "
            "WHERE cr.dms_document_id = CAST(:d AS uuid) "
            "  AND TRIM(cr.tenant_id) = TRIM(:tid) "
            "ORDER BY cr.classified_at DESC"),
            {"d": dms_document_id, "tid": tid})).mappings().fetchall()
        history = [dict(r) for r in rows]
        live = next((h for h in history if h["superseded_by"] is None), None)
        return {"document_id": dms_document_id, "live": live, "history": history}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="Persisted document classifier")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter", help="matter UUID (scopes by DMS folder)")
    ap.add_argument("--doc", action="append", help="dms_document UUID (repeatable)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--ai", action="store_true", help="enable model-on-residue")
    ap.add_argument("--all", action="store_true",
                    help="reclassify even already-classified docs")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO)
    out = asyncio.run(classify_documents(
        a.tenant, matter_id=a.matter, doc_ids=a.doc,
        only_unclassified=not a.all, limit=a.limit, use_ai=a.ai))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _main()
