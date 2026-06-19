"""seeding_session.py - AI-guided backfill seeding (Court/Hearing 6 / 10).

Historical reconstruction of a matter's hearing history is interactive and
LLM-guided, NOT a silent batch (architecture 6). Three independent witnesses
land evidence in the hearing_signals ledger; the signals are clustered into
proposed hearings + reschedule chains; an LLM narrates the reconstruction and
flags weak (single-witness / proxy-date) proposals; the attorney ratifies; and
only THEN is the hearings primitive committed (locked).

Witnesses (architecture 6):
  * doc      - notice/order extractions (notice_extractor, already in ledger)
  * calendar - typed hearing/trial/deposition events ("when the firm thought a
               hearing was set") - the one-time Exchange snapshot
  * time     - attorney time records as an INDEPENDENT OCCURRENCE WITNESS:
               "Prepare for and attend Arnold deposition" proves the deposition
               happened; "Correspondence re trial setting" corroborates one
               exists. This is the patent-novel collector (Series 4).

Flow:  collect -> build_proposals -> narrate (-> revise loop) -> ratify.
Nothing touches the hearings primitive until ratify. The whole conversation is
persisted to seeding_sessions for chain-of-custody (non-negotiable 3).

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.seeding_session --matter UUID
        [--collect] [--propose] [--narrate] [--tenant T]
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal
from modules.intelligence.reconciliation_resolver import (
    _norm_htype, _is_cancelled, _matter_meta,
    _upsert_hearing, _rebuild_reschedules, _resolve_signals)

logger = logging.getLogger(__name__)
DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# hearing_reschedules.source enum: notice|order|exchange|time_signal|manual
NOTICE_SIGNALS = ("notice", "order", "scheduling_order", "subpoena")

# coarse hearing type from a time-entry description (first match wins)
_TIME_TYPE = [
    ("trial", r"\btrial\b"),
    ("deposition", r"\bdepo(?:sition)?\b"),
    ("summary judgment", r"summary judgment|\bMSJ\b"),
    ("docket call", r"docket call|\bdocket\b"),
    ("pretrial conference", r"pre-?trial"),
    ("status conference", r"status conference"),
    ("temporary injunction", r"temporary injunction|\bTRO\b|injunction"),
    ("hearing", r"\bhearing\b"),
]
_TIME_TYPE = [(n, re.compile(rx, re.I)) for n, rx in _TIME_TYPE]
# strong: the work IS the event happening (date ~= event date)
_OCCUR = re.compile(
    r"\b(attend(?:ed|ing)?|appear(?:ed|ance|ing)?|argu(?:e|ed|ing)|"
    r"conduct(?:ed)?|defend(?:ed)?|took|examin\w*|cross-examin\w*|"
    r"present(?:ed)?\s+(?:at|argument))\b", re.I)
_PREP_ATTEND = re.compile(r"prepar\w*\s+for\s+and\s+attend", re.I)
# weak: a scheduling communication around (not on) the event date
_REFERENCE = re.compile(
    r"\b(correspond\w*|review\w*|confer\w*|notice|setting|schedul\w*|"
    r"continuance|reset|pass\w*)\b", re.I)


def _classify_time_entry(desc):
    """(hearing_type, role, score) for a time entry, or None if not court-event.

    role 'occurred' => date is the event date (occurrence witness, strong);
    role 'reference' => date is a proxy near the event (corroboration, weak).
    """
    if not desc:
        return None
    htype = next((n for n, rx in _TIME_TYPE if rx.search(desc)), None)
    if not htype:
        return None
    if _PREP_ATTEND.search(desc) or _OCCUR.search(desc):
        return htype, "occurred", 0.7
    if _REFERENCE.search(desc):
        return htype, "reference", 0.4
    return htype, "reference", 0.3


def _to_date(v):
    if v is None or isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


async def _matter_name_token(s, tid, matter_id):
    """Longest distinctive alpha token of the matter name for subject-matching
    calendar events that were never matter-linked."""
    nm = (await s.execute(sa_text(
        "SELECT matter_name FROM matters WHERE id=CAST(:m AS uuid) "
        "AND TRIM(tenant_id)=TRIM(:t)"),
        {"m": matter_id, "t": tid})).scalar()
    if not nm:
        return None
    toks = [w for w in re.findall(r"[A-Za-z]{4,}", nm)
            if w.lower() not in ("companies", "company", "matter", "appeal",
                                 "brothers", "estate", "trust")]
    toks.sort(key=len, reverse=True)
    return toks[0] if toks else None


# --------------------------------------------------------------------------- #
#  Collectors -> hearing_signals ledger                                       #
# --------------------------------------------------------------------------- #
async def _collect_calendar(s, tid, matter_id):
    """Typed hearing/trial/depo events -> exchange_event signals (collector
    owns this signal_type for the matter: wipe + rewrite)."""
    tok = await _matter_name_token(s, tid, matter_id)
    params = {"t": tid, "m": matter_id, "tok": f"%{tok}%" if tok else None}
    rows = (await s.execute(sa_text(
        "SELECT id::text eid, subject, start_at::date d, event_type "
        "FROM calendar_events "
        "WHERE TRIM(tenant_id)=TRIM(:t) AND start_at IS NOT NULL "
        "  AND event_type IN ('hearing','trial','deposition') "
        "  AND (matter_id=CAST(:m AS uuid) "
        "       OR (CAST(:tok AS text) IS NOT NULL AND matter_id IS NULL AND subject ILIKE CAST(:tok AS text))) "
        "ORDER BY start_at"), params)).mappings().fetchall()
    await s.execute(sa_text(
        "DELETE FROM hearing_signals WHERE matter_id=CAST(:m AS uuid) "
        "AND signal_type='exchange_event'"), {"m": matter_id})
    for r in rows:
        cancelled = _is_cancelled(r["subject"])
        await s.execute(sa_text(
            "INSERT INTO hearing_signals (tenant_id, matter_id, signal_type, "
            " source_ref, candidate_date, party, match_score, date_role) "
            "VALUES (:t, CAST(:m AS uuid), 'exchange_event', :ref, "
            " CAST(:cd AS date), :pty, :ms, :role)"),
            {"t": tid, "m": matter_id, "ref": r["eid"], "cd": r["d"],
             "pty": (r["subject"] or "")[:255],
             "ms": 0.6, "role": "cancelled" if cancelled else "scheduled"})
    return {"events": len(rows)}


async def _collect_time(s, tid, matter_id):
    """Time records -> time_entry signals (the occurrence witness)."""
    rows = (await s.execute(sa_text(
        "SELECT id::text tid_, COALESCE(date, entry_date) d, description, "
        "       timekeeper_name "
        "FROM time_entries WHERE matter_id=CAST(:m AS uuid) "
        "  AND COALESCE(date, entry_date) IS NOT NULL "
        "ORDER BY COALESCE(date, entry_date)"),
        {"m": matter_id})).mappings().fetchall()
    await s.execute(sa_text(
        "DELETE FROM hearing_signals WHERE matter_id=CAST(:m AS uuid) "
        "AND signal_type='time_entry'"), {"m": matter_id})
    kept = 0
    occ = 0
    for r in rows:
        c = _classify_time_entry(r["description"])
        if not c:
            continue
        htype, role, score = c
        if role == "occurred":
            occ += 1
        await s.execute(sa_text(
            "INSERT INTO hearing_signals (tenant_id, matter_id, signal_type, "
            " source_ref, candidate_date, party, match_score, date_role) "
            "VALUES (:t, CAST(:m AS uuid), 'time_entry', :ref, "
            " CAST(:cd AS date), :pty, :ms, :role)"),
            {"t": tid, "m": matter_id, "ref": r["tid_"], "cd": r["d"],
             "pty": (r["timekeeper_name"] or "")[:255], "ms": score,
             "role": role})
        kept += 1
    return {"time_entries_scanned": len(rows), "signals": kept,
            "occurrence": occ}


async def collect(tid, matter_id):
    """Run all three witnesses; return the collector summary."""
    async with AsyncSessionLocal() as s:
        doc = (await s.execute(sa_text(
            "SELECT count(*) FROM hearing_signals "
            "WHERE matter_id=CAST(:m AS uuid) AND signal_type=ANY(:types)"),
            {"m": matter_id, "types": list(NOTICE_SIGNALS)})).scalar()
        cal = await _collect_calendar(s, tid, matter_id)
        tim = await _collect_time(s, tid, matter_id)
        summary = {"doc": {"signals": doc}, "calendar": cal, "time": tim}
        await _save_session(s, tid, matter_id, status="collecting",
                            collectors=summary)
        await s.commit()
    return summary


# --------------------------------------------------------------------------- #
#  Observation gathering + clustering (propose, never commit)                 #
# --------------------------------------------------------------------------- #
async def _seed_observations(s, tid, matter_id):
    """{htype: [obs]} from all three witnesses for ONE matter. obs carries the
    witness so single-source can be detected."""
    tree = {}

    def add(ht, o):
        tree.setdefault(ht, []).append(o)

    # calendar
    tok = await _matter_name_token(s, tid, matter_id)
    cal = (await s.execute(sa_text(
        "SELECT id::text eid, subject, start_at::date d, event_type "
        "FROM calendar_events WHERE TRIM(tenant_id)=TRIM(:t) "
        "  AND start_at IS NOT NULL AND event_type IN ('hearing','trial','deposition') "
        "  AND (matter_id=CAST(:m AS uuid) "
        "       OR (CAST(:tok AS text) IS NOT NULL AND matter_id IS NULL AND subject ILIKE CAST(:tok AS text)))"),
        {"t": tid, "m": matter_id,
         "tok": f"%{tok}%" if tok else None})).mappings().fetchall()
    for r in cal:
        ht = _norm_htype(r["subject"], r["event_type"])
        add(ht, {"date": r["d"], "source": "exchange", "witness": "calendar",
                 "event_id": r["eid"], "doc_id": None, "time_entry_id": None,
                 "subject": r["subject"], "cancelled": _is_cancelled(r["subject"]),
                 "role": "scheduled", "score": 0.6})

    # notice / order extractions
    notes = (await s.execute(sa_text(
        "SELECT dms_document_id::text did, hearing_type, new_date::date nd, "
        "       prior_date::date pd, doc_type, confidence "
        "FROM hearing_notice_extractions WHERE TRIM(tenant_id)=TRIM(:t) "
        "  AND superseded_by_id IS NULL AND matter_id=CAST(:m AS uuid) "
        "  AND new_date IS NOT NULL"),
        {"t": tid, "m": matter_id})).mappings().fetchall()
    for r in notes:
        ht = _norm_htype(r["hearing_type"] or "hearing")
        src = "order" if r["doc_type"] == "order" else "notice"
        add(ht, {"date": r["nd"], "source": src, "witness": "doc",
                 "event_id": None, "doc_id": r["did"], "time_entry_id": None,
                 "subject": r["hearing_type"], "cancelled": False,
                 "role": "new", "score": float(r["confidence"] or 0.5)})
        if r["pd"] and r["pd"] != r["nd"]:
            add(ht, {"date": r["pd"], "source": src, "witness": "doc",
                     "event_id": None, "doc_id": r["did"], "time_entry_id": None,
                     "subject": r["hearing_type"], "cancelled": False,
                     "role": "prior", "score": float(r["confidence"] or 0.5)})

    # time records (occurrence witness)
    tes = (await s.execute(sa_text(
        "SELECT id::text teid, COALESCE(date, entry_date) d, description, "
        "       timekeeper_name "
        "FROM time_entries WHERE matter_id=CAST(:m AS uuid) "
        "  AND COALESCE(date, entry_date) IS NOT NULL"),
        {"m": matter_id})).mappings().fetchall()
    for r in tes:
        c = _classify_time_entry(r["description"])
        if not c:
            continue
        ht, role, score = c
        add(ht, {"date": r["d"], "source": "time_signal", "witness": "time",
                 "event_id": None, "doc_id": None, "time_entry_id": r["teid"],
                 "subject": (r["description"] or "")[:140], "cancelled": False,
                 "role": role, "score": score})
    return tree


def _proposal_for(htype, obs):
    """Summarize a cluster into a confirmable proposal (no DB write)."""
    witnesses = sorted({o["witness"] for o in obs})
    by_date = {}
    for o in obs:
        d = by_date.setdefault(o["date"], {
            "date": o["date"].isoformat(), "witnesses": set(), "sources": set(),
            "roles": set(), "score": 0.0, "cancelled": False})
        d["witnesses"].add(o["witness"])
        d["sources"].add(o["source"])
        d["roles"].add(o["role"])
        d["score"] = max(d["score"], o["score"])
        d["cancelled"] = d["cancelled"] or o["cancelled"]
    dates = []
    for d in sorted(by_date.values(), key=lambda x: x["date"]):
        proxy = d["witnesses"] == {"time"} and d["roles"] <= {"reference"}
        dates.append({
            "date": d["date"], "witnesses": sorted(d["witnesses"]),
            "sources": sorted(d["sources"]), "roles": sorted(d["roles"]),
            "score": round(d["score"], 2), "cancelled": d["cancelled"],
            "proxy_date": proxy})
    # strong dates = anything not proxy-only and not cancelled, for the chain
    strong = [d["date"] for d in dates
              if not d["proxy_date"] and not d["cancelled"]] or \
             [d["date"] for d in dates]
    return {
        "key": htype,
        "hearing_type": htype,
        "witnesses": witnesses,
        "single_source": len(witnesses) == 1,
        "n_dates": len(dates),
        "n_resets": max(0, len(strong) - 1),
        "original_start_at": strong[0] if strong else None,
        "current_start_at": strong[-1] if strong else None,
        "dates": dates,
        "confidence": round(
            0.9 if len(witnesses) >= 2 else max(d["score"] for d in dates), 2),
    }


async def build_proposals(tid, matter_id):
    async with AsyncSessionLocal() as s:
        tree = await _seed_observations(s, tid, matter_id)
        proposals = [_proposal_for(ht, obs) for ht, obs in tree.items()]
        proposals.sort(key=lambda p: (p["current_start_at"] or ""))
        payload = {"matter_id": matter_id, "n_proposals": len(proposals),
                   "single_source": sum(1 for p in proposals if p["single_source"]),
                   "proposals": proposals}
        await _save_session(s, tid, matter_id, status="proposed",
                            proposal=payload)
        await s.commit()
    return payload


# --------------------------------------------------------------------------- #
#  LLM narration (the deliberate AI exception)                                #
# --------------------------------------------------------------------------- #
_SYS = (
    "You are a litigation paralegal reconstructing a matter's hearing history "
    "from triangulated evidence. Three independent witnesses contribute: court "
    "DOCUMENTS (notices/orders), the firm CALENDAR (what was scheduled), and "
    "attorney TIME RECORDS (an independent occurrence witness - 'attend X' "
    "proves X happened; a time 'reference' date is only a proxy NEAR the event). "
    "For the proposed hearings given, write a concise narration (a few sentences "
    "each at most). You MUST explicitly flag any hearing that is SINGLE-SOURCE "
    "(only one witness) or whose date is a PROXY (time-reference only), and pose "
    "ONE targeted confirm/correct question per flagged item. End with a short "
    "'Recommended confirmations' list. Do not invent dates not in the evidence.")


def _proposals_brief(payload):
    lines = []
    for p in payload["proposals"]:
        flags = []
        if p["single_source"]:
            flags.append(f"SINGLE-SOURCE ({p['witnesses'][0]})")
        if any(d["proxy_date"] for d in p["dates"]):
            flags.append("PROXY-DATE")
        flag = f"  [{', '.join(flags)}]" if flags else ""
        chain = " -> ".join(
            d["date"] + ("(x)" if d["cancelled"] else "") for d in p["dates"])
        wt = "/".join(p["witnesses"])
        lines.append(
            f"- {p['hearing_type']}: dates {chain}; witnesses {wt}; "
            f"resets {p['n_resets']}{flag}")
    return "\n".join(lines)


async def narrate(tid, matter_id, *, extra_note=None):
    payload = await _get_proposal(tid, matter_id)
    if not payload or not payload.get("proposals"):
        return {"error": "no proposals; run collect + proposals first"}
    try:
        from modules.intelligence import anthropic_adapter
    except Exception as e:
        return {"error": f"adapter unavailable: {e}"}
    ctx = anthropic_adapter.AICallContext(
        tenant_id=tid, module="intelligence", purpose="hearing_seed")
    user = (f"Matter has {payload['n_proposals']} proposed hearings "
            f"({payload['single_source']} single-source).\n\n"
            f"{_proposals_brief(payload)}\n")
    if extra_note:
        user += (f"\nAttorney note to incorporate: {extra_note}\n"
                 "Re-narrate taking this into account.")
    user += "\nNarrate now."
    try:
        res = await anthropic_adapter.call(
            ctx, raw_user_prompt=user, raw_system_prompt=_SYS,
            max_tokens_override=1500)
    except Exception as e:
        logger.warning("seed narrate failed: %s", e)
        return {"error": f"narration failed: {e}"}
    async with AsyncSessionLocal() as s:
        await _save_session(
            s, tid, matter_id, status="narrated", narration=res.text,
            model=res.model_used, input_tokens=res.input_tokens,
            output_tokens=res.output_tokens,
            append_turn={"role": "assistant", "text": res.text,
                         "kind": "narration"})
        if extra_note:
            await _append_turn(s, tid, matter_id,
                               {"role": "attorney", "text": extra_note,
                                "kind": "revise"})
        await s.commit()
    return {"narration": res.text, "model": res.model_used,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens}


async def revise(tid, matter_id, note):
    """Cheap steering loop: re-narrate with the attorney's note appended; the
    note is persisted as provenance."""
    return await narrate(tid, matter_id, extra_note=note)


# --------------------------------------------------------------------------- #
#  Ratify -> commit the hearings primitive (locked)                           #
# --------------------------------------------------------------------------- #
async def ratify(tid, matter_id, decisions, *, user_id=None):
    """decisions: {hearing_key: {"action": accept|reject,
                                  "edits": {hearing_type?, current_start_at?,
                                            original_start_at?, ...}}}.
    Accepted proposals are committed to hearings + hearing_reschedules and
    LOCKED (provenance.locked=true) so future resolver runs never overwrite the
    attorney-ratified reconstruction. The whole decision set is persisted."""
    committed = []
    async with AsyncSessionLocal() as s:
        sess = await _live_session(s, tid, matter_id)
        seed_session_id = sess["id"] if sess else None
        tree = await _seed_observations(s, tid, matter_id)
        for htype, obs in tree.items():
            dec = decisions.get(htype) or decisions.get(htype.lower())
            if not dec or dec.get("action") != "accept":
                continue
            edits = dec.get("edits") or {}
            commit_type = edits.get("hearing_type", htype)
            # Only AUTHORITATIVE observations set the hearing date + reschedule
            # chain. A time 'reference' entry (e.g. "correspondence re trial
            # setting") is a proxy that corroborates the hearing exists; it is
            # NOT a reschedule to that date. It stays in the ledger as a signal.
            auth = [o for o in obs
                    if not (o["witness"] == "time" and o["role"] == "reference")]
            if not auth:
                committed.append({"key": htype, "hearing_id": None,
                                  "action": "no_authoritative_dates"})
                continue
            obs = auth
            hid, action = await _upsert_hearing(s, tid, matter_id, commit_type, obs)
            if action == "locked":
                committed.append({"key": htype, "hearing_id": hid,
                                  "action": "already_locked"})
                continue
            # apply attorney date edits
            sets, params = [], {"h": hid}
            for col in ("current_start_at", "original_start_at"):
                if edits.get(col):
                    d = _to_date(edits[col])
                    if d:
                        sets.append(f"{col}=CAST(:{col} AS date)")
                        params[col] = d
            if sets:
                await s.execute(sa_text(
                    "UPDATE hearings SET " + ", ".join(sets) +
                    " WHERE id=CAST(:h AS uuid)"), params)
            # lock + stamp provenance with the seeding session
            await s.execute(sa_text(
                "UPDATE hearings SET status='confirmed', "
                " provenance = jsonb_set(jsonb_set(COALESCE(provenance,'{}'::jsonb), "
                "   '{locked}','true'), '{seed_session_id}', to_jsonb(CAST(:sid AS text))), "
                " updated_at=now() WHERE id=CAST(:h AS uuid)"),
                {"h": hid, "sid": seed_session_id or ""})
            resched = await _rebuild_reschedules(s, tid, hid, obs)
            dates = sorted({o["date"] for o in obs})
            await _resolve_signals(s, tid, hid, matter_id, dates)
            committed.append({"key": htype, "hearing_id": hid, "action": action,
                              "reschedules": resched})
        await _save_session(s, tid, matter_id, status="ratified",
                            ratified={"decisions": decisions,
                                      "committed": committed},
                            ratified_by=user_id)
        await s.commit()
    return {"matter_id": matter_id, "committed": committed,
            "n_committed": len(committed)}


# --------------------------------------------------------------------------- #
#  Session persistence (one live per matter; supersede on re-seed)            #
# --------------------------------------------------------------------------- #
async def _live_session(s, tid, matter_id):
    return (await s.execute(sa_text(
        "SELECT id::text id, status, collectors, proposal, transcript, "
        "       narration, model, ratified, created_at, updated_at "
        "FROM seeding_sessions WHERE matter_id=CAST(:m AS uuid) "
        "AND TRIM(tenant_id)=TRIM(:t) AND superseded_by_id IS NULL "
        "ORDER BY created_at DESC LIMIT 1"),
        {"m": matter_id, "t": tid})).mappings().fetchone()


async def _save_session(s, tid, matter_id, *, status=None, collectors=None,
                        proposal=None, narration=None, model=None,
                        input_tokens=None, output_tokens=None, ratified=None,
                        ratified_by=None, append_turn=None, created_by=None):
    cur = await _live_session(s, tid, matter_id)
    if not cur:
        sid = (await s.execute(sa_text(
            "INSERT INTO seeding_sessions (tenant_id, matter_id, status, "
            " created_by) VALUES (:t, CAST(:m AS uuid), 'collecting', "
            " CAST(:cb AS bigint)) RETURNING id::text"),
            {"t": tid, "m": matter_id, "cb": created_by})).scalar()
    else:
        sid = cur["id"]
    sets = ["updated_at=now()"]
    params = {"id": sid}
    if status:
        sets.append("status=:st")
        params["st"] = status
    if collectors is not None:
        sets.append("collectors=CAST(:co AS jsonb)")
        params["co"] = json.dumps(collectors, default=str)
    if proposal is not None:
        sets.append("proposal=CAST(:pr AS jsonb)")
        params["pr"] = json.dumps(proposal, default=str)
    if narration is not None:
        sets.append("narration=:na")
        params["na"] = narration
    if model is not None:
        sets.append("model=:mo")
        params["mo"] = model
    if input_tokens is not None:
        sets.append("input_tokens=:it")
        params["it"] = input_tokens
    if output_tokens is not None:
        sets.append("output_tokens=:ot")
        params["ot"] = output_tokens
    if ratified is not None:
        sets.append("ratified=CAST(:ra AS jsonb)")
        params["ra"] = json.dumps(ratified, default=str)
    if ratified_by is not None:
        sets.append("ratified_by=CAST(:rb AS bigint)")
        params["rb"] = ratified_by
    await s.execute(sa_text(
        "UPDATE seeding_sessions SET " + ", ".join(sets) +
        " WHERE id=CAST(:id AS uuid)"), params)
    if append_turn is not None:
        await _append_turn_by_id(s, sid, append_turn)
    return sid


async def _append_turn(s, tid, matter_id, turn):
    cur = await _live_session(s, tid, matter_id)
    if cur:
        await _append_turn_by_id(s, cur["id"], turn)


async def _append_turn_by_id(s, sid, turn):
    await s.execute(sa_text(
        "UPDATE seeding_sessions SET transcript = transcript || CAST(:tn AS jsonb), "
        " updated_at=now() WHERE id=CAST(:id AS uuid)"),
        {"id": sid, "tn": json.dumps([turn], default=str)})


async def _get_proposal(tid, matter_id):
    async with AsyncSessionLocal() as s:
        cur = await _live_session(s, tid, matter_id)
        if not cur or not cur["proposal"]:
            return None
        p = cur["proposal"]
        return p if isinstance(p, dict) else json.loads(p)


async def get_session(tid, matter_id):
    async with AsyncSessionLocal() as s:
        cur = await _live_session(s, tid, matter_id)
        if not cur:
            return {"matter_id": matter_id, "session": None}
        out = dict(cur)
        for k in ("collectors", "proposal", "ratified"):
            if isinstance(out.get(k), str):
                out[k] = json.loads(out[k])
        if isinstance(out.get("transcript"), str):
            out["transcript"] = json.loads(out["transcript"])
        return {"matter_id": matter_id, "session": out}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="AI-guided hearing seeding")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter", required=True)
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--narrate", action="store_true")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO)

    async def run():
        out = {}
        if a.collect or not (a.propose or a.narrate):
            out["collect"] = await collect(a.tenant, a.matter)
        if a.propose or a.narrate or not a.collect:
            out["proposals"] = await build_proposals(a.tenant, a.matter)
        if a.narrate:
            out["narrate"] = await narrate(a.tenant, a.matter)
        return out
    print(json.dumps(asyncio.run(run()), indent=2, default=str))


if __name__ == "__main__":
    _main()
