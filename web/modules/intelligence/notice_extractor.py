"""notice_extractor.py — the single notice/order extractor (Court/Hearing §4.2).

Given a court document the persisted classifier typed as notice | order |
scheduling_order | subpoena, extract the fields reschedule capture / transcript
routing / the trial dashboard all need: hearing_type, new_date, prior_date (if a
reset), moving_party, plus court / judge / cause_number.

Structural-first → model-assist:
  * structural — deterministically harvest every date, the cause number, court
    and judge from the text (cheap, exact). These anchor and validate whatever
    the model says.
  * model-assist — given the text + the harvested dates, the mandated adapter
    assigns ROLES that regex can't reliably ("which date is the new setting vs
    the prior one", "is this a reset", hearing_type, moving party). method is
    'hybrid' when both ran, 'rule' / 'ai' when only one did.

Output: a hearing_notice_extractions row (supersede/correct chain) + one
hearing_signals evidence row per candidate date (new / prior / filing), upserted
idempotently per (document, date, signal_type). Manual corrections protected.

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.notice_extractor [--tenant T] [--matter UUID]
        [--doc UUID ...] [--limit N] [--ai] [--all] [--debug]
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import random
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
DMS_ROOT = "/mnt/praesidium"
COURT_TYPES = ("notice", "order", "scheduling_order", "subpoena")
HEAD = 6000

# --- AI pacing (a burst of unpaced calls trips Cloudflare rate/bot mitigation
#     on the shared egress IP; a missing per-call timeout makes it HANG). All
#     env-tunable so a large backfill can slow down without a code change. ---
AI_THROTTLE_S = float(os.environ.get("NOTICE_AI_THROTTLE_S", "1.2"))   # min gap between calls
AI_CALL_TIMEOUT = float(os.environ.get("NOTICE_AI_TIMEOUT_S", "45"))   # fail fast, never hang
AI_MAX_RETRIES = int(os.environ.get("NOTICE_AI_RETRIES", "4"))
AI_BACKOFF_BASE = float(os.environ.get("NOTICE_AI_BACKOFF_S", "5"))
_TRANSIENT_HINTS = ("blocked", "cloudflare", "429", "rate", "overloaded",
                    "too many", "timeout", "timed out", "502", "503",
                    "unavailable", "connection", "reset")


def _is_transient(err):
    """A retryable failure: a timeout, or a rate/block/upstream hiccup. A
    Cloudflare challenge page or a 429/overload is transient (back off and
    retry); a malformed-prompt or auth error is not."""
    if isinstance(err, asyncio.TimeoutError):
        return True
    s = (str(err) or "").lower()
    return any(h in s for h in _TRANSIENT_HINTS)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
_MON_RX = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I)
_NUM_RX = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
_CAUSE_RX = re.compile(
    r"(?:Trial Court Cause|Cause|Case|No)\.?\s*(?:No\.?)?\s*"
    r"([A-Z]{1,3}-?\d{2,4}-\d{3,}[-A-Z]*|\d{2,4}-\d{3,}-\d{2,4}[-A-Z]*)", re.I)
_COURT_RX = re.compile(
    r"(\d{1,3}(?:st|nd|rd|th)\s+Judicial District|\d{1,3}(?:st|nd|rd|th)\s+"
    r"District Court|Court of Appeals[^.\n]{0,40}|County Court at Law[^.\n]{0,20})",
    re.I)
_JUDGE_RX = re.compile(r"(?:Honorable|Hon\.|Judge)\s+([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,3})")
_RESET_RX = re.compile(
    r"\b(reset|re-?set|continued|rescheduled|re-?scheduled|passed|moved|"
    r"previously set|formerly set|originally set|new setting|new date)\b", re.I)
_HTYPE_RX = [
    ("summary judgment", r"summary judgment|\bMSJ\b|\bMSj\b"),
    ("pretrial conference", r"pre-?trial conference|pre-?trial"),
    ("docket call", r"docket call"),
    ("status conference", r"status conference"),
    ("trial", r"\b(bench|jury)?\s*trial\b"),
    ("motion to compel", r"motion to compel"),
    ("temporary injunction", r"temporary injunction|TRO"),
    ("hearing", r"\bhearing\b"),
]
_HTYPE_RX = [(name, re.compile(rx, re.I)) for name, rx in _HTYPE_RX]


def _parse_dates(text):
    """All parseable dates in order of appearance: [(iso_date, raw)]."""
    found = []
    for m in _MON_RX.finditer(text):
        try:
            d = dt.date(int(m.group(3)), _month_num(m.group(1)), int(m.group(2)))
            found.append((d.isoformat(), m.group(0)))
        except (ValueError, KeyError):
            pass
    for m in _NUM_RX.finditer(text):
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yr += 2000 if yr < 100 else 0
        try:
            d = dt.date(yr, mo, da)
            if 2000 <= d.year <= 2100:
                found.append((d.isoformat(), m.group(0)))
        except ValueError:
            pass
    # de-dup preserving order
    seen, out = set(), []
    for iso, raw in found:
        if iso not in seen:
            seen.add(iso)
            out.append({"date": iso, "raw": raw})
    return out


def _month_num(tok):
    return _MONTHS[_full_month(tok)]


def _full_month(tok):
    t = tok.lower().rstrip(".")
    for name in _MONTHS:
        if name.startswith(t[:3]):
            return name
    raise KeyError(tok)


def _first(rx, text, group=1):
    m = rx.search(text)
    return (m.group(group).strip() if m else None)


def _structural(text):
    dates = _parse_dates(text)
    htype = next((name for name, rx in _HTYPE_RX if rx.search(text)), None)
    return {
        "all_dates": dates,
        "cause_number": _first(_CAUSE_RX, text),
        "court": _first(_COURT_RX, text, 1),
        "judge": _first(_JUDGE_RX, text),
        "hearing_type": htype,
        "reset_language": bool(_RESET_RX.search(text)),
    }


# --------------------------------------------------------------------------- #
#  Model assist                                                               #
# --------------------------------------------------------------------------- #
_SYS = (
    "You extract scheduling facts from a court document (notice/order/subpoena). "
    "Return ONLY JSON with keys: hearing_type (short label or null), new_date "
    "(YYYY-MM-DD of the setting/hearing this document sets, or null), prior_date "
    "(YYYY-MM-DD of a previous setting if this resets/continues one, else null), "
    "is_reset (bool), moving_party (who sought it, or null), confidence (0..1). "
    "Only use dates that appear in the text. A filing/transmittal date is NOT a "
    "hearing date — leave new_date null if the document sets no hearing.")


async def _ai_extract(tid, text, struct):
    try:
        from modules.intelligence import anthropic_adapter
    except Exception:
        return None
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="classification", purpose="notice_extract")
    datelist = ", ".join(d["date"] for d in struct["all_dates"][:20]) or "(none)"
    prompt = (f"Dates present: {datelist}\n"
              f"Cause: {struct['cause_number']}  Court: {struct['court']}\n\n"
              f"Document text:\n{text[:5000]}\n\nReturn the JSON now.")
    res = None
    for attempt in range(AI_MAX_RETRIES + 1):
        try:
            res = await asyncio.wait_for(
                anthropic_adapter.call(
                    ctx, raw_user_prompt=prompt, raw_system_prompt=_SYS,
                    max_tokens_override=220),
                timeout=AI_CALL_TIMEOUT)
            break
        except Exception as e:
            if attempt >= AI_MAX_RETRIES or not _is_transient(e):
                logger.warning("AI extract failed (%s): %s",
                               type(e).__name__, str(e)[:160])
                return None
            delay = min(90.0, AI_BACKOFF_BASE * (2 ** attempt)) + random.uniform(0, 2)
            logger.warning("AI transient (%s); attempt %d/%d, backoff %.1fs",
                           type(e).__name__, attempt + 1, AI_MAX_RETRIES, delay)
            await asyncio.sleep(delay)
    if res is None:
        return None
    obj = _json(res.text)
    if obj is None:
        return None
    obj["_model"] = res.model_used
    return obj


def _json(t):
    if not t:
        return None
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    try:
        return json.loads(t)
    except Exception:
        return None


def _valid_date(s, struct):
    """Accept a model date only if it actually appears among harvested dates."""
    if not s:
        return None
    s = str(s)[:10]
    known = {d["date"] for d in struct["all_dates"]}
    return s if s in known else None


# --------------------------------------------------------------------------- #
#  Persistence                                                                #
# --------------------------------------------------------------------------- #
def _to_date(v):
    """ISO 'YYYY-MM-DD' (or date) -> datetime.date for asyncpg date binds."""
    if v is None or isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


async def _live(s, doc_id):
    return (await s.execute(sa_text(
        "SELECT id::text id, method FROM hearing_notice_extractions "
        "WHERE dms_document_id=CAST(:d AS uuid) AND superseded_by_id IS NULL "
        "ORDER BY created_at DESC LIMIT 1"), {"d": doc_id})).mappings().fetchone()


async def _persist(s, tid, run_id, doc_id, matter_id, doc_type, fields,
                   *, reviewed_by=None):
    cur = await _live(s, doc_id)
    method = fields["method"]
    if cur and cur["method"] == "manual" and method != "manual":
        return "protected", None
    new_id = (await s.execute(sa_text(
        "INSERT INTO hearing_notice_extractions "
        "(tenant_id, dms_document_id, matter_id, doc_type, hearing_type, "
        " new_date, prior_date, is_reset, moving_party, court, judge, "
        " cause_number, all_dates, confidence, method, model, extracted, "
        " extractor_run_id, reviewed_by, reviewed_at) "
        "VALUES (:t, CAST(:d AS uuid), CAST(:m AS uuid), :dt, :ht, "
        " CAST(:nd AS date), CAST(:pd AS date), :rs, :mp, :ct, :jg, :cn, "
        " CAST(:ad AS jsonb), :cf, :me, :mo, CAST(:ex AS jsonb), "
        " CAST(:run AS uuid), CAST(:rv AS bigint), "
        " CASE WHEN CAST(:rv AS bigint) IS NULL THEN NULL ELSE now() END) "
        "RETURNING id::text"),
        {"t": tid, "d": doc_id, "m": matter_id, "dt": doc_type,
         "ht": fields["hearing_type"], "nd": _to_date(fields["new_date"]),
         "pd": _to_date(fields["prior_date"]), "rs": fields["is_reset"],
         "mp": fields["moving_party"], "ct": fields["court"],
         "jg": fields["judge"], "cn": fields["cause_number"],
         "ad": json.dumps(fields["all_dates"]), "cf": fields["confidence"],
         "me": method, "mo": fields.get("model"),
         "ex": json.dumps(fields.get("raw", {})), "run": run_id,
         "rv": reviewed_by})).scalar()
    await s.execute(sa_text(
        "UPDATE hearing_notice_extractions SET superseded_by_id=CAST(:n AS uuid) "
        "WHERE dms_document_id=CAST(:d AS uuid) AND superseded_by_id IS NULL "
        "AND id <> CAST(:n AS uuid)"), {"n": new_id, "d": doc_id})

    # evidence ledger: one signal per candidate date (idempotent upsert)
    sigs = []
    if fields["new_date"]:
        sigs.append((fields["new_date"], "new"))
    if fields["prior_date"]:
        sigs.append((fields["prior_date"], "prior"))
    if not sigs and fields["all_dates"]:
        sigs.append((fields["all_dates"][0]["date"], "filing"))
    for cdate, role in sigs:
        await s.execute(sa_text(
            "INSERT INTO hearing_signals "
            "(tenant_id, matter_id, signal_type, source_ref, candidate_date, "
            " party, match_score, source_document_id, extraction_id, date_role) "
            "VALUES (:t, CAST(:m AS uuid), :st, :ref, CAST(:cd AS date), :pty, "
            " :ms, CAST(:d AS uuid), CAST(:ex AS uuid), :role) "
            "ON CONFLICT (source_document_id, candidate_date, signal_type) "
            "WHERE source_document_id IS NOT NULL "
            "DO UPDATE SET extraction_id=EXCLUDED.extraction_id, "
            " date_role=EXCLUDED.date_role, match_score=EXCLUDED.match_score, "
            " party=EXCLUDED.party, matter_id=EXCLUDED.matter_id"),
            {"t": tid, "m": matter_id, "st": doc_type, "ref": doc_id,
             "cd": _to_date(cdate), "pty": fields["moving_party"],
             "ms": fields["confidence"], "d": doc_id, "ex": new_id, "role": role})
    return "inserted", new_id


# --------------------------------------------------------------------------- #
#  Target resolution                                                          #
# --------------------------------------------------------------------------- #
async def _matter_for_path(s, tid, file_path):
    """Match a dms file path to its matter (shared resolver: disk_root first)."""
    from modules.intelligence.matter_paths import matter_for_path
    return await matter_for_path(s, tid, file_path)


async def _targets(s, tid, matter_id, doc_ids, only_new, limit):
    where = ["TRIM(d.tenant_id)=TRIM(:tid)",
             "t.code = ANY(:types)",
             "cr.superseded_by_id IS NULL"]
    params = {"tid": tid, "types": list(COURT_TYPES), "lim": limit}
    if doc_ids:
        where.append("d.id = ANY(CAST(:ids AS uuid[]))")
        params["ids"] = list(doc_ids)
    elif matter_id:
        # shared resolver: disk_root UNION folder_path (folder_path stale/NULL)
        from modules.intelligence.matter_paths import disk_prefixes
        prefixes = await disk_prefixes(s, tid, matter_id)
        if not prefixes:
            return []
        where.append("d.file_path LIKE ANY(:pfxs)")
        params["pfxs"] = prefixes
    if only_new:
        where.append("NOT EXISTS (SELECT 1 FROM hearing_notice_extractions x "
                     "WHERE x.dms_document_id=d.id AND x.superseded_by_id IS NULL)")
    sql = ("SELECT d.id::text id, d.file_path, t.code doc_type, "
           f"       LEFT(COALESCE(d.content_text,''), {HEAD}) head "
           "FROM dms_documents d "
           "JOIN classification_results cr ON cr.dms_document_id=d.id "
           "JOIN document_type_taxonomy t ON t.id=cr.document_type_id "
           "WHERE " + " AND ".join(where) +
           " ORDER BY d.indexed_at DESC NULLS LAST LIMIT :lim")
    return (await s.execute(sa_text(sql), params)).mappings().fetchall()


# --------------------------------------------------------------------------- #
#  Batch driver                                                               #
# --------------------------------------------------------------------------- #
async def extract_notices(tid=DEFAULT_TENANT, *, matter_id=None, doc_ids=None,
                          only_new=True, limit=200, use_ai=False, user_id=None):
    summary = {"targets": 0, "extracted": 0, "with_hearing_date": 0,
               "resets": 0, "protected": 0, "run_id": None}
    async with AsyncSessionLocal() as s:
        targets = await _targets(s, tid, matter_id, doc_ids, only_new, limit)
        summary["targets"] = len(targets)
        if not targets:
            return summary
        run_id = (await s.execute(sa_text(
            "INSERT INTO extraction_runs (tenant_id, run_type, source_type, "
            " status, document_count, extraction_model, started_at) "
            "VALUES (:t,'notice_extract','dms','running',:n,:mo,now()) "
            "RETURNING id::text"),
            {"t": tid, "n": len(targets),
             "mo": "ai+rules" if use_ai else "rules"})).scalar()
        summary["run_id"] = run_id
        await s.commit()

        done = 0
        for t in targets:
            text = t["head"] or ""
            struct = _structural(text)
            matter = await _matter_for_path(s, tid, t["file_path"])
            fields = {
                "hearing_type": struct["hearing_type"],
                "new_date": None, "prior_date": None,
                "is_reset": struct["reset_language"],
                "moving_party": None,
                "court": struct["court"], "judge": struct["judge"],
                "cause_number": struct["cause_number"],
                "all_dates": struct["all_dates"],
                "confidence": 0.4 if struct["all_dates"] else 0.2,
                "method": "rule", "model": None, "raw": {},
            }
            if use_ai:
                ai = await _ai_extract(tid, text, struct)
                if ai:
                    fields["method"] = "hybrid"
                    fields["model"] = ai.get("_model")
                    fields["raw"] = ai
                    fields["hearing_type"] = ai.get("hearing_type") or fields["hearing_type"]
                    fields["new_date"] = _valid_date(ai.get("new_date"), struct)
                    fields["prior_date"] = _valid_date(ai.get("prior_date"), struct)
                    fields["is_reset"] = bool(ai.get("is_reset")) or (
                        fields["prior_date"] is not None)
                    fields["moving_party"] = (ai.get("moving_party") or "")[:255] or None
                    try:
                        fields["confidence"] = float(ai.get("confidence") or 0.5)
                    except (TypeError, ValueError):
                        fields["confidence"] = 0.5
                # pace calls so a large batch doesn't trip egress rate limits
                await asyncio.sleep(AI_THROTTLE_S)
            outcome, _ = await _persist(s, tid, run_id, t["id"], matter,
                                        t["doc_type"], fields, reviewed_by=None)
            if outcome == "protected":
                summary["protected"] += 1
                continue
            summary["extracted"] += 1
            if fields["new_date"]:
                summary["with_hearing_date"] += 1
            if fields["is_reset"]:
                summary["resets"] += 1
            done += 1
            if done % 20 == 0:
                await s.commit()

        await s.execute(sa_text(
            "UPDATE extraction_runs SET status='completed', completed_at=now(), "
            "documents_processed=:p WHERE id=CAST(:r AS uuid)"),
            {"p": done, "r": run_id})
        await s.commit()
    return summary


async def correct_extraction(tid, dms_document_id, patch, user_id):
    """Human correction → a method='manual' extraction that supersedes prior and
    is protected from re-runs. `patch` overrides any of the structured fields."""
    async with AsyncSessionLocal() as s:
        cur = (await s.execute(sa_text(
            "SELECT doc_type, matter_id::text, hearing_type, new_date, prior_date, "
            "       is_reset, moving_party, court, judge, cause_number, all_dates "
            "FROM hearing_notice_extractions "
            "WHERE dms_document_id=CAST(:d AS uuid) AND superseded_by_id IS NULL "
            "ORDER BY created_at DESC LIMIT 1"),
            {"d": dms_document_id})).mappings().fetchone()
        base = dict(cur) if cur else {"doc_type": "notice", "matter_id": None,
            "hearing_type": None, "new_date": None, "prior_date": None,
            "is_reset": False, "moving_party": None, "court": None, "judge": None,
            "cause_number": None, "all_dates": []}
        fields = {
            "hearing_type": patch.get("hearing_type", base["hearing_type"]),
            "new_date": patch.get("new_date", str(base["new_date"]) if base["new_date"] else None),
            "prior_date": patch.get("prior_date", str(base["prior_date"]) if base["prior_date"] else None),
            "is_reset": patch.get("is_reset", base["is_reset"]),
            "moving_party": patch.get("moving_party", base["moving_party"]),
            "court": patch.get("court", base["court"]),
            "judge": patch.get("judge", base["judge"]),
            "cause_number": patch.get("cause_number", base["cause_number"]),
            "all_dates": base["all_dates"] or [],
            "confidence": 1.0, "method": "manual", "model": None,
            "raw": {"corrected": True},
        }
        run_id = (await s.execute(sa_text(
            "INSERT INTO extraction_runs (tenant_id, run_type, source_type, status,"
            " document_count, documents_processed, started_at, completed_at) "
            "VALUES (:t,'notice_extract','manual','completed',1,1,now(),now()) "
            "RETURNING id::text"), {"t": tid})).scalar()
        outcome, nid = await _persist(s, tid, run_id, dms_document_id,
                                      base["matter_id"], base["doc_type"], fields,
                                      reviewed_by=user_id)
        await s.commit()
        return {"outcome": outcome, "extraction_id": nid}


async def get_extraction(tid, dms_document_id):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT id::text, doc_type, hearing_type, new_date, prior_date, "
            "       is_reset, moving_party, court, judge, cause_number, "
            "       confidence, method, model, created_at, "
            "       superseded_by_id::text superseded_by "
            "FROM hearing_notice_extractions "
            "WHERE dms_document_id=CAST(:d AS uuid) AND TRIM(tenant_id)=TRIM(:t) "
            "ORDER BY created_at DESC"),
            {"d": dms_document_id, "t": tid})).mappings().fetchall()
        hist = [dict(r) for r in rows]
        sigs = (await s.execute(sa_text(
            "SELECT candidate_date, date_role, signal_type, party "
            "FROM hearing_signals WHERE source_document_id=CAST(:d AS uuid) "
            "ORDER BY candidate_date"), {"d": dms_document_id})).mappings().fetchall()
        return {"document_id": dms_document_id,
                "live": next((h for h in hist if h["superseded_by"] is None), None),
                "history": hist, "signals": [dict(x) for x in sigs]}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="Notice/order extractor")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter")
    ap.add_argument("--doc", action="append")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO)
    out = asyncio.run(extract_notices(
        a.tenant, matter_id=a.matter, doc_ids=a.doc, only_new=not a.all,
        limit=a.limit, use_ai=a.ai))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _main()
