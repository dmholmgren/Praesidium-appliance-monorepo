"""calendar_classifier.py — hearing/event-type classifier + matter-link resolver
for calendar_events (Court/Hearing build, Step 2 shared-core, Gap #3).

Two persisted, reviewable, correctable passes over calendar_events, both following
the invariant proven for the document classifier (structural-first → model-on-
residue; supersede/correct; idempotent; manual decisions protected from re-runs):

  A. event typing  — sets calendar_events.event_type (+ _source/_confidence),
     the value the §2 typed-event → workspace projector dispatches on. Types:
     hearing | trial | deposition | meeting | deadline | admin | personal | other.
     Deadlines are tested FIRST because a deadline subject often names the very
     hearing it is a deadline for ("deadline for hearing on dispositive motions").

  B. matter-link   — resolves matter_id via the shared-resolver signals
     (cause number exact, matter/client name tokens) and records a
     calendar_matter_assignments row (assigned_by rule|ai|manual, confidence,
     signals, was_corrected/original_matter_id). matter_id is denormalized onto
     calendar_events. Many calendar matters have no matter row yet — those stay
     unresolved (NULL), honestly, rather than be force-fit.

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.calendar_classifier [--tenant T] [--limit N]
        [--ai] [--all] [--types-only | --link-only] [--debug]
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"   # HJMM
LINK_FLOOR = 0.50            # structural matter-link assignment floor
AI_LINK_FLOOR = 0.60
AI_TYPE_FLOOR = 0.55

# Event-type regexes, FIRST match wins. Order is deliberate (deadline before the
# court-setting words it may contain; trial/hearing/depo before the generic
# meeting bucket).
_TYPE_RULES = [
    ("deadline", 0.92, re.compile(
        r"\b(deadline|due date|\bdue\b|last day|closes?|expires?|to be filed|"
        r"exchanged|disclosure deadline|discovery (closes|deadline)|"
        r"expert deadline|dispositive motion deadline|statute of limitations|"
        r"\bSOL\b|file .*(brief|order|motion) by|will issue (his|her|its) ruling)\b",
        re.I)),
    ("hearing", 0.90, re.compile(
        r"\b(hearing|docket call|docket|pre-?trial conference|\bpre-?trial\b|"
        r"status conference|scheduling conference|oral argument|show cause|"
        r"motion to compel hearing|\bMSJ\b|summary judgment hearing|"
        r"temporary injunction|TRO hearing|announcement|setting)\b", re.I)),
    ("trial", 0.88, re.compile(
        r"\b(bench trial|jury trial|trial setting|jury selection|voir dire)\b|"
        r"(^|[-–:]\s*)trial\b", re.I)),
    ("deposition", 0.90, re.compile(
        r"\b(deposition|oral deposition|video deposition|depo of)\b|\bdepo\b",
        re.I)),
    ("meeting", 0.82, re.compile(
        r"\b(conference call|call with|\bcall\b|meeting|meet\b|mediation|"
        r"teams meeting|zoom|google meet|discussion|consult(ation)?|kickoff|"
        r"check-?in|interview|intake|invitation)\b", re.I)),
    ("personal", 0.80, re.compile(
        r"\b(birthday|anniversary|vacation|holiday|out of office|\bOOO\b|"
        r"unavailable|lunch|dentist|doctor|cardiolog|physical|appointment|"
        r"\bappt\b|personal)\b", re.I)),
    ("admin", 0.78, re.compile(
        r"\b(reminder|replace|filters|maintenance|renew|invoice|payroll|"
        r"time and billing|timesheet|cle\b|bar dues|order supplies)\b", re.I)),
]

_TYPE_SYSTEM = (
    "You classify a law-firm calendar event into exactly one type: hearing, "
    "trial, deposition, meeting, deadline, admin, or personal. A 'deadline' is a "
    "date by which something must be done (even if it names a hearing). A "
    "'hearing' is an in-court setting before a judge. Reply ONLY with JSON: "
    '{"event_type": "<type or null>", "confidence": <0..1>}.')


# --------------------------------------------------------------------------- #
#  Loaders                                                                     #
# --------------------------------------------------------------------------- #
async def _events(s, tid, only_untyped, only_unlinked, limit, dimension):
    where = ["TRIM(e.tenant_id) = TRIM(:tid)"]
    params = {"tid": tid, "lim": limit}
    if dimension == "type" and only_untyped:
        where.append("e.event_type IS NULL")
    if dimension == "link" and only_unlinked:
        where.append("NOT EXISTS (SELECT 1 FROM calendar_matter_assignments a "
                     "WHERE a.event_id = e.id)")
    sql = ("SELECT e.id::text id, COALESCE(e.subject,'') subject, "
           "       COALESCE(e.body_preview,'') body, COALESCE(e.location,'') loc, "
           "       COALESCE(e.organizer_name,'') org, e.event_type, "
           "       e.event_type_source "
           "FROM calendar_events e WHERE " + " AND ".join(where) +
           " ORDER BY e.start_at DESC NULLS LAST LIMIT :lim")
    return (await s.execute(sa_text(sql), params)).mappings().fetchall()


_STOP = set("the a an of to in on for and or with from that this v vs versus llc "
            "inc lp llp co corp company the appeal matter case re et al order "
            "trust fund properties property holdings group general legacy test".split())


def _tokens(*texts):
    out = set()
    for t in texts:
        for w in re.findall(r"[A-Za-z][A-Za-z'&-]{3,}", (t or "").lower()):
            if w not in _STOP:
                out.add(w)
    return out


async def _matters(s, tid):
    rows = (await s.execute(sa_text(
        "SELECT m.id::text id, COALESCE(m.matter_name,'') name, "
        "       COALESCE(m.cause_number,'') cause, COALESCE(m.folder_path,'') folder, "
        "       COALESCE(c.client_name,'') client "
        "FROM matters m LEFT JOIN clients c ON c.id = m.client_id "
        "WHERE TRIM(m.tenant_id) = TRIM(:tid)"), {"tid": tid})).mappings().fetchall()
    out = []
    for r in rows:
        leaf = r["folder"].split("/")[-1] if r["folder"] else ""
        out.append({
            "id": r["id"], "name": r["name"], "cause": r["cause"].strip(),
            "client": r["client"],
            "name_tokens": _tokens(r["name"], leaf),
            "client_tokens": _tokens(r["client"]),
        })
    return out


# --------------------------------------------------------------------------- #
#  Structural passes                                                          #
# --------------------------------------------------------------------------- #
def _type_structural(subject, body):
    hay = f"{subject}\n{body}"
    for etype, conf, rx in _TYPE_RULES:
        if rx.search(hay):
            return etype, conf
    return None, None


def _link_structural(subject, body, loc, matters):
    """Best matter for the event by cause-number + name/client token overlap."""
    hay_words = _tokens(subject, body, loc)
    hay_raw = f"{subject} {body} {loc}"
    best, best_score, best_sig = None, 0.0, None
    for m in matters:
        score, sig = 0.0, {}
        if m["cause"] and len(m["cause"]) >= 5 and m["cause"] in hay_raw:
            score += 0.6
            sig["cause"] = m["cause"]
        nhits = sorted(m["name_tokens"] & hay_words)
        if nhits:
            score += min(0.45, 0.22 * len(nhits))
            sig["name_tokens"] = nhits
        chits = sorted(m["client_tokens"] & hay_words)
        if chits:
            score += min(0.2, 0.12 * len(chits))
            sig["client_tokens"] = chits
        if score > best_score:
            best, best_score, best_sig = m, score, sig
    if best and best_score >= LINK_FLOOR:
        return best["id"], round(min(best_score, 0.95), 3), best_sig
    return None, None, None


# --------------------------------------------------------------------------- #
#  AI residue                                                                  #
# --------------------------------------------------------------------------- #
async def _ai_type(tid, subject, body, loc):
    try:
        from modules.intelligence import anthropic_adapter
    except Exception:
        return None, None
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="classification", purpose="calendar_event_type")
    prompt = (f"Subject: {subject}\nLocation: {loc}\n"
              f"Notes: {(body or '')[:600]}\n\nReturn the JSON now.")
    try:
        res = await anthropic_adapter.call(
            ctx, raw_user_prompt=prompt, raw_system_prompt=_TYPE_SYSTEM,
            max_tokens_override=60)
    except Exception as e:
        logger.warning("AI type failed (%s): %s", type(e).__name__, e)
        return None, None
    obj = _json(res.text) or {}
    et = obj.get("event_type")
    try:
        conf = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    valid = {"hearing", "trial", "deposition", "meeting", "deadline",
             "admin", "personal"}
    if et in valid and conf >= AI_TYPE_FLOOR:
        return et, conf
    return None, None


async def _ai_link(tid, subject, body, loc, matters):
    try:
        from modules.intelligence import anthropic_adapter
    except Exception:
        return None, None, None
    menu = "\n".join(f"- {m['id']}: {m['name']} (client {m['client']}"
                     + (f", cause {m['cause']}" if m['cause'] else "") + ")"
                     for m in matters)
    sys = ("You match a calendar event to the single best matter from the menu, "
           "or null if none clearly fits. Reply ONLY JSON: "
           '{"matter_id": "<uuid or null>", "confidence": <0..1>}.')
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="classification", purpose="calendar_matter_link")
    prompt = (f"Matter menu:\n{menu}\n\nEvent subject: {subject}\n"
              f"Location: {loc}\nNotes: {(body or '')[:500]}\n\nReturn JSON now.")
    try:
        res = await anthropic_adapter.call(
            ctx, raw_user_prompt=prompt, raw_system_prompt=sys,
            max_tokens_override=80)
    except Exception as e:
        logger.warning("AI link failed (%s): %s", type(e).__name__, e)
        return None, None, None
    obj = _json(res.text) or {}
    mid = obj.get("matter_id")
    valid = {m["id"] for m in matters}
    try:
        conf = float(obj.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    if mid in valid and conf >= AI_LINK_FLOOR:
        return mid, conf, {"ai": True}
    return None, None, None


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


# --------------------------------------------------------------------------- #
#  Persistence — typing                                                        #
# --------------------------------------------------------------------------- #
async def _write_type(s, tid, event_id, etype, conf, source, *, cur_type, cur_src):
    if cur_src == "manual" and source != "manual":
        return "protected"
    if cur_type == etype and cur_src == source:
        return "skipped"
    await s.execute(sa_text(
        "UPDATE calendar_events SET event_type=:et, event_type_source=:src, "
        "event_type_confidence=:cf, event_typed_at=now() "
        "WHERE id = CAST(:id AS uuid)"),
        {"et": etype, "src": source, "cf": conf, "id": event_id})
    return "inserted"


# --------------------------------------------------------------------------- #
#  Persistence — matter link (supersede, never overwrite)                      #
# --------------------------------------------------------------------------- #
async def _live_link(s, event_id):
    return (await s.execute(sa_text(
        "SELECT id::text id, matter_id::text matter_id, assigned_by, was_corrected "
        "FROM calendar_matter_assignments WHERE event_id = CAST(:e AS uuid)"),
        {"e": event_id})).mappings().fetchone()


async def _write_link(s, tid, event_id, matter_id, conf, assigned_by, signals,
                      *, user_id=None, corrected=False):
    """One live assignment per event (email_matter_assignments family). A
    correction updates the row in place, stamping was_corrected + the prior
    matter into original_matter_id. Manual rows are protected from automated
    re-runs; an identical decision is a no-op."""
    cur = await _live_link(s, event_id)
    if cur:
        if cur["assigned_by"] == "manual" and assigned_by != "manual":
            return "protected"
        if cur["matter_id"] == matter_id and cur["assigned_by"] == assigned_by:
            return "skipped"
        original = cur["matter_id"] if corrected else None
        await s.execute(sa_text(
            "UPDATE calendar_matter_assignments SET matter_id=CAST(:m AS uuid), "
            " assigned_by=:by, confidence=:cf, signals=CAST(:sig AS jsonb), "
            " was_corrected = was_corrected OR :corr, "
            " original_matter_id = COALESCE(CAST(:orig AS uuid), original_matter_id), "
            " assigned_user_id = COALESCE(CAST(:uid AS bigint), assigned_user_id), "
            " assigned_at = now() "
            "WHERE id = CAST(:rid AS uuid)"),
            {"m": matter_id, "by": assigned_by, "cf": conf,
             "sig": json.dumps(signals or {}), "corr": corrected,
             "orig": original, "uid": user_id, "rid": cur["id"]})
    else:
        await s.execute(sa_text(
            "INSERT INTO calendar_matter_assignments "
            "(tenant_id, event_id, matter_id, assigned_by, confidence, signals, "
            " was_corrected, assigned_user_id) "
            "VALUES (:t, CAST(:e AS uuid), CAST(:m AS uuid), :by, :cf, "
            " CAST(:sig AS jsonb), :corr, CAST(:uid AS bigint))"),
            {"t": tid, "e": event_id, "m": matter_id, "by": assigned_by,
             "cf": conf, "sig": json.dumps(signals or {}), "corr": corrected,
             "uid": user_id})
    await s.execute(sa_text(
        "UPDATE calendar_events SET matter_id = CAST(:m AS uuid) "
        "WHERE id = CAST(:e AS uuid)"), {"m": matter_id, "e": event_id})
    return "inserted"


# --------------------------------------------------------------------------- #
#  Batch driver                                                                #
# --------------------------------------------------------------------------- #
async def _run(s, tid, n, model):
    return (await s.execute(sa_text(
        "INSERT INTO extraction_runs (tenant_id, run_type, source_type, status, "
        " document_count, extraction_model, started_at) "
        "VALUES (:t,'calendar_classify','calendar','running',:n,:model,now()) "
        "RETURNING id::text"), {"t": tid, "n": n, "model": model})).scalar()


async def classify_calendar(tid=DEFAULT_TENANT, *, do_types=True, do_link=True,
                            only_new=True, limit=1000, use_ai=False, user_id=None):
    summary = {"type": {"rule": 0, "ai": 0, "residue": 0, "skipped": 0,
                        "protected": 0},
               "link": {"rule": 0, "ai": 0, "residue": 0, "skipped": 0,
                        "protected": 0},
               "events": 0}
    async with AsyncSessionLocal() as s:
        matters = await _matters(s, tid) if do_link else []
        # union of events needing either pass
        evs = {}
        if do_types:
            for r in await _events(s, tid, only_new, only_new, limit, "type"):
                evs[r["id"]] = r
        if do_link:
            for r in await _events(s, tid, only_new, only_new, limit, "link"):
                evs.setdefault(r["id"], r)
        summary["events"] = len(evs)
        if not evs:
            return summary
        run_id = await _run(s, tid, len(evs), "ai+rules" if use_ai else "rules")
        summary["run_id"] = run_id
        await s.commit()

        i = 0
        for e in evs.values():
            # ---- typing ----
            if do_types and (not only_new or e["event_type"] is None):
                et, conf = _type_structural(e["subject"], e["body"])
                src = "rule"
                if not et and use_ai:
                    et, conf = await _ai_type(tid, e["subject"], e["body"], e["loc"])
                    src = "ai" if et else src
                if not et:
                    summary["type"]["residue"] += 1
                else:
                    out = await _write_type(s, tid, e["id"], et, conf, src,
                                            cur_type=e["event_type"],
                                            cur_src=e["event_type_source"])
                    _tally(summary["type"], out, src)
            # ---- matter link ----
            if do_link:
                mid, conf, sig = _link_structural(e["subject"], e["body"],
                                                  e["loc"], matters)
                by = "rule"
                if not mid and use_ai:
                    mid, conf, sig = await _ai_link(tid, e["subject"], e["body"],
                                                    e["loc"], matters)
                    by = "ai" if mid else by
                if not mid:
                    summary["link"]["residue"] += 1
                else:
                    out = await _write_link(s, tid, e["id"], mid, conf, by, sig,
                                            user_id=user_id)
                    _tally(summary["link"], out, by)
            i += 1
            if i % 50 == 0:
                await s.commit()

        await s.execute(sa_text(
            "UPDATE extraction_runs SET status='completed', completed_at=now(), "
            "documents_processed=:p WHERE id=CAST(:r AS uuid)"),
            {"p": i, "r": run_id})
        await s.commit()
    return summary


def _tally(bucket, outcome, src):
    if outcome == "inserted":
        bucket[src] += 1
    elif outcome in ("skipped", "protected"):
        bucket[outcome] += 1


# --------------------------------------------------------------------------- #
#  Corrections + read                                                          #
# --------------------------------------------------------------------------- #
async def correct_event_type(tid, event_id, event_type, user_id):
    async with AsyncSessionLocal() as s:
        cur = (await s.execute(sa_text(
            "SELECT event_type, event_type_source FROM calendar_events "
            "WHERE id=CAST(:e AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"e": event_id, "t": tid})).mappings().fetchone()
        out = await _write_type(s, tid, event_id, event_type, 1.0, "manual",
                                cur_type=cur["event_type"] if cur else None,
                                cur_src=cur["event_type_source"] if cur else None)
        await s.commit()
        return {"outcome": out, "event_type": event_type}


async def correct_matter(tid, event_id, matter_id, user_id):
    async with AsyncSessionLocal() as s:
        out = await _write_link(s, tid, event_id, matter_id, 1.0, "manual",
                                {"manual": True}, user_id=user_id, corrected=True)
        await s.commit()
        live = await _live_link(s, event_id)
        return {"outcome": out, "assignment_id": live["id"] if live else None}


async def get_event(tid, event_id):
    async with AsyncSessionLocal() as s:
        ev = (await s.execute(sa_text(
            "SELECT id::text, subject, event_type, event_type_source, "
            "       event_type_confidence, matter_id::text "
            "FROM calendar_events WHERE id=CAST(:e AS uuid) "
            "AND TRIM(tenant_id)=TRIM(:t)"),
            {"e": event_id, "t": tid})).mappings().fetchone()
        link = (await s.execute(sa_text(
            "SELECT a.id::text, a.matter_id::text, m.matter_name, a.assigned_by, "
            "       a.confidence, a.signals, a.was_corrected, "
            "       a.original_matter_id::text, a.assigned_at "
            "FROM calendar_matter_assignments a "
            "LEFT JOIN matters m ON m.id=a.matter_id "
            "WHERE a.event_id=CAST(:e AS uuid)"),
            {"e": event_id})).mappings().fetchone()
        return {"event": dict(ev) if ev else None,
                "matter_link": dict(link) if link else None}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="Calendar event typing + matter-link")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--all", action="store_true", help="reprocess already-decided")
    ap.add_argument("--types-only", action="store_true")
    ap.add_argument("--link-only", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO)
    out = asyncio.run(classify_calendar(
        a.tenant, do_types=not a.link_only, do_link=not a.types_only,
        only_new=not a.all, limit=a.limit, use_ai=a.ai))
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    _main()
