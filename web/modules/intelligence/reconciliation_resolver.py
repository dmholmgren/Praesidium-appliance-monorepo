"""reconciliation_resolver.py — the reconciliation resolver (Court/Hearing §4.3).

The capstone of the shared core. Reconstructs durable `hearings` from the
evidence the upstream units produced, bidirectionally:

  * calendar hearing/trial events (typed + matter-linked by the calendar
    classifier, Step 2b) — "when the firm thought a hearing was set";
  * notice/order extractions with a hearing date (Step 2c) — "what the court
    document says" — each already mirrored into the hearing_signals ledger.

Per §6, one cluster = one hearing: observations are grouped by
(matter, normalized hearing_type); the distinct dates in a cluster are the
reschedule chain (earliest = original_start_at, latest = current_start_at), and
each consecutive move becomes a hearing_reschedules row stamped to its source
witness. hearing_signals for the matched dates get resolved_into_hearing_id set.

Provenance is sacred:
  * hearings carry provenance.locked; a confirmed/corrected hearing is NEVER
    re-touched by a re-run.
  * hearing_reschedules with source='manual' survive the rebuild.
  * idempotent: a hearing's identity is (matter_id, normalized hearing_type), so
    re-runs update in place rather than duplicating.

CLI (inside praesidium-web, cwd /app):
  python -m modules.intelligence.reconciliation_resolver [--tenant T]
        [--matter UUID] [--debug]
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"

# ordered (canonical_type, regex) — first match wins
_BUCKETS = [
    ("jury selection", r"jury selection|voir dire"),
    ("trial setting", r"trial setting|request for trial setting"),
    ("summary judgment", r"summary judgment|\bMSJ\b"),
    ("motion to compel", r"motion to compel"),
    ("pretrial conference", r"pre-?trial"),
    ("docket call", r"docket call|docket"),
    ("status conference", r"status conference"),
    ("scheduling conference", r"scheduling"),
    ("injunction hearing", r"temporary injunction|\bTRO\b|injunction"),
]
_BUCKETS = [(n, re.compile(rx, re.I)) for n, rx in _BUCKETS]


def _norm_htype(subject, event_type=None):
    """Canonical hearing_type for clustering. Trial events collapse to 'trial';
    otherwise a known bucket, else the cleaned 'hearing on ...' phrase so that
    distinct motions in one matter don't merge."""
    if event_type == "trial":
        return "trial"
    s = re.sub(r"^\s*(canceled|cancelled|updated|tentative)\s*:\s*", "",
               subject or "", flags=re.I)
    s = s.rsplit(" - ", 1)[0]          # drop the "- Client/Matter" suffix
    s = re.sub(r"\s+", " ", s).strip()
    for name, rx in _BUCKETS:
        if rx.search(s):
            return name
    low = s.lower()
    if "trial" in low:
        return "trial"
    return low or "hearing"


def _is_cancelled(subject):
    return bool(re.match(r"\s*(canceled|cancelled)\s*:", subject or "", re.I))


# --------------------------------------------------------------------------- #
#  Observation gathering                                                       #
# --------------------------------------------------------------------------- #
async def _observations(s, tid, matter_id):
    """{matter_id: {htype: [obs...]}} from calendar + notice evidence."""
    where_m = "AND e.matter_id = CAST(:mid AS uuid)" if matter_id else ""
    cal = (await s.execute(sa_text(
        "SELECT e.id::text eid, e.matter_id::text mid, e.subject, "
        "       e.start_at::date dt, e.event_type "
        "FROM calendar_events e "
        "WHERE TRIM(e.tenant_id)=TRIM(:t) AND e.matter_id IS NOT NULL "
        "  AND e.event_type IN ('hearing','trial') AND e.start_at IS NOT NULL "
        + where_m), {"t": tid, "mid": matter_id})).mappings().fetchall()

    where_mn = "AND x.matter_id = CAST(:mid AS uuid)" if matter_id else ""
    notes = (await s.execute(sa_text(
        "SELECT x.dms_document_id::text did, x.matter_id::text mid, "
        "       x.hearing_type, x.new_date::date nd, x.prior_date::date pd, "
        "       x.doc_type "
        "FROM hearing_notice_extractions x "
        "WHERE TRIM(x.tenant_id)=TRIM(:t) AND x.superseded_by_id IS NULL "
        "  AND x.matter_id IS NOT NULL AND x.new_date IS NOT NULL "
        + where_mn), {"t": tid, "mid": matter_id})).mappings().fetchall()

    tree = {}
    for r in cal:
        ht = _norm_htype(r["subject"], r["event_type"])
        tree.setdefault(r["mid"], {}).setdefault(ht, []).append({
            "date": r["dt"], "source": "exchange", "event_id": r["eid"],
            "doc_id": None, "subject": r["subject"],
            "cancelled": _is_cancelled(r["subject"])})
    for r in notes:
        ht = _norm_htype(r["hearing_type"] or "hearing")
        src = "order" if r["doc_type"] == "order" else "notice"
        bucket = tree.setdefault(r["mid"], {}).setdefault(ht, [])
        bucket.append({"date": r["nd"], "source": src, "event_id": None,
                       "doc_id": r["did"], "subject": r["hearing_type"],
                       "cancelled": False})
        if r["pd"] and r["pd"] != r["nd"]:
            bucket.append({"date": r["pd"], "source": src, "event_id": None,
                           "doc_id": r["did"], "subject": r["hearing_type"],
                           "cancelled": False, "prior": True})
    return tree


# --------------------------------------------------------------------------- #
#  Hearing upsert                                                             #
# --------------------------------------------------------------------------- #
async def _matter_meta(s, tid, matter_id):
    r = (await s.execute(sa_text(
        "SELECT judge, court FROM matters WHERE id=CAST(:m AS uuid) "
        "AND TRIM(tenant_id)=TRIM(:t)"),
        {"m": matter_id, "t": tid})).mappings().fetchone()
    return (r["judge"] if r else None, r["court"] if r else None)


async def _upsert_hearing(s, tid, matter_id, htype, obs):
    dates = sorted({o["date"] for o in obs})
    has_notice = any(o["source"] in ("notice", "order") for o in obs)
    has_cal = any(o["source"] == "exchange" for o in obs)
    notice_doc = next((o["doc_id"] for o in obs if o["doc_id"]), None)
    # latest non-cancelled date = the operative setting (fall back to latest)
    live_dates = sorted({o["date"] for o in obs if not o["cancelled"]}) or dates
    current = live_dates[-1]
    original = dates[0]
    current_eid = next((o["event_id"] for o in obs
                        if o["event_id"] and o["date"] == current), None)
    judge, court = await _matter_meta(s, tid, matter_id)
    conf = 0.9 if (has_notice and has_cal) else (0.75 if has_cal else 0.7)
    notice_status = "attached" if has_notice else "pending"
    prov = json.dumps({"source": "reconciler", "locked": False,
                       "n_observations": len(obs), "n_dates": len(dates),
                       "sources": sorted({o["source"] for o in obs})})

    existing = (await s.execute(sa_text(
        "SELECT id::text id, provenance->>'locked' locked "
        "FROM hearings WHERE matter_id=CAST(:m AS uuid) "
        "AND TRIM(tenant_id)=TRIM(:t) AND lower(coalesce(hearing_type,''))=:ht "
        "ORDER BY created_at LIMIT 1"),
        {"m": matter_id, "t": tid, "ht": htype.lower()})).mappings().fetchone()

    if existing and existing["locked"] == "true":
        return existing["id"], "locked"
    if existing:
        await s.execute(sa_text(
            "UPDATE hearings SET original_start_at=CAST(:o AS date), "
            " current_start_at=CAST(:c AS date), current_event_id=CAST(:ev AS uuid), "
            " notice_status=:ns, notice_document_id=CAST(:nd AS uuid), "
            " judge=:jg, courtroom=COALESCE(courtroom,:ct), confidence=:cf, "
            " provenance=CAST(:pv AS jsonb), updated_at=now() "
            "WHERE id=CAST(:id AS uuid)"),
            {"o": original, "c": current, "ev": current_eid, "ns": notice_status,
             "nd": notice_doc, "jg": judge, "ct": court, "cf": conf, "pv": prov,
             "id": existing["id"]})
        return existing["id"], "updated"
    hid = (await s.execute(sa_text(
        "INSERT INTO hearings (tenant_id, matter_id, current_event_id, "
        " hearing_type, judge, courtroom, status, original_start_at, "
        " current_start_at, notice_status, notice_document_id, confidence, "
        " provenance) "
        "VALUES (:t, CAST(:m AS uuid), CAST(:ev AS uuid), :ht, :jg, :ct, "
        " 'scheduled', CAST(:o AS date), CAST(:c AS date), :ns, "
        " CAST(:nd AS uuid), :cf, CAST(:pv AS jsonb)) RETURNING id::text"),
        {"t": tid, "m": matter_id, "ev": current_eid, "ht": htype, "jg": judge,
         "ct": court, "o": original, "c": current, "ns": notice_status,
         "nd": notice_doc, "cf": conf, "pv": prov})).scalar()
    return hid, "created"


async def _rebuild_reschedules(s, tid, hearing_id, obs):
    """Append-only move log rebuilt from the date chain; manual rows preserved."""
    await s.execute(sa_text(
        "DELETE FROM hearing_reschedules WHERE hearing_id=CAST(:h AS uuid) "
        "AND source <> 'manual'"), {"h": hearing_id})
    # one representative observation per distinct date (prefer a documented one)
    by_date = {}
    for o in obs:
        cur = by_date.get(o["date"])
        if cur is None or (o["doc_id"] and not cur["doc_id"]):
            by_date[o["date"]] = o
    chain = [by_date[d] for d in sorted(by_date)]
    n = 0
    for i in range(1, len(chain)):
        frm, to = chain[i - 1], chain[i]
        delta = (to["date"] - frm["date"]).days
        await s.execute(sa_text(
            "INSERT INTO hearing_reschedules (hearing_id, tenant_id, sequence_no, "
            " from_start_at, to_start_at, delta_days, source, source_document_id, "
            " confidence, detected_at) "
            "VALUES (CAST(:h AS uuid), :t, :sq, CAST(:f AS date), CAST(:to AS date), "
            " :dl, :src, CAST(:doc AS uuid), :cf, now()) "
            "ON CONFLICT (hearing_id, sequence_no) DO UPDATE SET "
            " from_start_at=EXCLUDED.from_start_at, to_start_at=EXCLUDED.to_start_at, "
            " delta_days=EXCLUDED.delta_days, source=EXCLUDED.source, "
            " source_document_id=EXCLUDED.source_document_id"),
            {"h": hearing_id, "t": tid, "sq": i, "f": frm["date"], "to": to["date"],
             "dl": delta, "src": to["source"], "doc": to["doc_id"], "cf": 0.8})
        n += 1
    return n


async def _resolve_signals(s, tid, hearing_id, matter_id, dates):
    if not dates:
        return 0
    res = await s.execute(sa_text(
        "UPDATE hearing_signals SET resolved_into_hearing_id=CAST(:h AS uuid) "
        "WHERE TRIM(tenant_id)=TRIM(:t) AND matter_id=CAST(:m AS uuid) "
        "AND candidate_date = ANY(CAST(:ds AS date[])) "
        "AND (resolved_into_hearing_id IS NULL "
        "     OR resolved_into_hearing_id=CAST(:h AS uuid))"),
        {"h": hearing_id, "t": tid, "m": matter_id, "ds": list(dates)})
    return res.rowcount or 0


# --------------------------------------------------------------------------- #
#  Driver                                                                      #
# --------------------------------------------------------------------------- #
async def reconcile(tid=DEFAULT_TENANT, *, matter_id=None):
    summary = {"matters": 0, "hearings_created": 0, "hearings_updated": 0,
               "locked_skipped": 0, "reschedules": 0, "signals_resolved": 0,
               "reset_hearings": 0}
    async with AsyncSessionLocal() as s:
        tree = await _observations(s, tid, matter_id)
        summary["matters"] = len(tree)
        for mid, by_type in tree.items():
            for htype, obs in by_type.items():
                hid, action = await _upsert_hearing(s, tid, mid, htype, obs)
                if action == "locked":
                    summary["locked_skipped"] += 1
                    continue
                summary["hearings_created" if action == "created"
                        else "hearings_updated"] += 1
                resched = await _rebuild_reschedules(s, tid, hid, obs)
                summary["reschedules"] += resched
                if resched:
                    summary["reset_hearings"] += 1
                dates = sorted({o["date"] for o in obs})
                summary["signals_resolved"] += await _resolve_signals(
                    s, tid, hid, mid, dates)
            await s.commit()
    return summary


# --------------------------------------------------------------------------- #
#  Confirm / correct (attorney-confirmable, sacred)                           #
# --------------------------------------------------------------------------- #
async def confirm_hearing(tid, hearing_id, user_id):
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "UPDATE hearings SET status='confirmed', "
            " provenance = jsonb_set(COALESCE(provenance,'{}'::jsonb),'{locked}','true'), "
            " updated_at=now() "
            "WHERE id=CAST(:h AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"h": hearing_id, "t": tid})
        await s.commit()
        return {"confirmed": (r.rowcount or 0) > 0, "hearing_id": hearing_id}


async def correct_hearing(tid, hearing_id, patch, user_id):
    cols = {"hearing_type": "hearing_type", "judge": "judge",
            "courtroom": "courtroom", "outcome": "outcome", "status": "status",
            "notice_status": "notice_status"}
    sets, params = [], {"h": hearing_id, "t": tid}
    for k, col in cols.items():
        if k in patch and patch[k] is not None:
            sets.append(f"{col}=:{k}")
            params[k] = patch[k]
    for k in ("current_start_at", "original_start_at"):
        if k in patch and patch[k]:
            try:
                params[k] = dt.date.fromisoformat(str(patch[k])[:10])
                sets.append(f"{k}=CAST(:{k} AS date)")
            except ValueError:
                pass
    if not sets:
        return {"corrected": False, "reason": "no fields"}
    async with AsyncSessionLocal() as s:
        r = await s.execute(sa_text(
            "UPDATE hearings SET " + ", ".join(sets) + ", "
            " provenance = jsonb_set(COALESCE(provenance,'{}'::jsonb),'{locked}','true'), "
            " updated_at=now() "
            "WHERE id=CAST(:h AS uuid) AND TRIM(tenant_id)=TRIM(:t)"), params)
        await s.commit()
        return {"corrected": (r.rowcount or 0) > 0, "hearing_id": hearing_id}


async def list_hearings(tid, matter_id):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT h.id::text, h.hearing_type, h.status, h.notice_status, "
            "       h.original_start_at, h.current_start_at, h.judge, "
            "       h.confidence, h.provenance, "
            "       (SELECT count(*) FROM hearing_reschedules r "
            "        WHERE r.hearing_id=h.id) reset_count "
            "FROM hearings h WHERE h.matter_id=CAST(:m AS uuid) "
            "AND TRIM(h.tenant_id)=TRIM(:t) ORDER BY h.current_start_at"),
            {"m": matter_id, "t": tid})).mappings().fetchall()
        return {"matter_id": matter_id, "hearings": [dict(r) for r in rows]}


async def get_hearing(tid, hearing_id):
    async with AsyncSessionLocal() as s:
        h = (await s.execute(sa_text(
            "SELECT h.id::text, h.matter_id::text, h.hearing_type, h.status, "
            "       h.notice_status, h.original_start_at, h.current_start_at, "
            "       h.judge, h.courtroom, h.confidence, h.provenance "
            "FROM hearings h WHERE h.id=CAST(:h AS uuid) "
            "AND TRIM(h.tenant_id)=TRIM(:t)"),
            {"h": hearing_id, "t": tid})).mappings().fetchone()
        res = (await s.execute(sa_text(
            "SELECT sequence_no, from_start_at, to_start_at, delta_days, source, "
            "       source_document_id::text, confidence "
            "FROM hearing_reschedules WHERE hearing_id=CAST(:h AS uuid) "
            "ORDER BY sequence_no"), {"h": hearing_id})).mappings().fetchall()
        sigs = (await s.execute(sa_text(
            "SELECT candidate_date, signal_type, date_role, source_document_id::text "
            "FROM hearing_signals WHERE resolved_into_hearing_id=CAST(:h AS uuid) "
            "ORDER BY candidate_date"), {"h": hearing_id})).mappings().fetchall()
        return {"hearing": dict(h) if h else None,
                "reschedules": [dict(r) for r in res],
                "signals": [dict(x) for x in sigs]}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse
    import asyncio
    ap = argparse.ArgumentParser(description="Reconciliation resolver")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO)
    print(json.dumps(asyncio.run(reconcile(a.tenant, matter_id=a.matter)),
                     indent=2, default=str))


if __name__ == "__main__":
    _main()
