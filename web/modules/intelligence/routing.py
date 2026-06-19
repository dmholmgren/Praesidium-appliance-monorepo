"""routing.py — forward-loop routing + alert inbox + motion workspaces.

The triage step of §5: a classified document is auto-routed to where it belongs,
or — when that can't be done confidently — an alert is raised so a human routes
it. Built on the persisted classifier (2a), matter resolver, and reconciler (2d).

Routing by classifier code:
  deposition  -> transcript: auto-route to the matter's deposition surface, else
                 a route_transcript alert.
  motion*     -> ensure a motion_workspace (one per motion that yields a hearing),
                 auto-populate (link the hearing the reconciler built, if any),
                 and raise missing_filing alerts for the gaps.
  response/reply -> link to the matter's motion_workspace, else a route_response
                 alert for a human to pick the motion.

Alerts are idempotent per (alert_type, source_kind, source_id) and obey
snooze-that-returns: snooze sets snoozed_until; the surface query brings it back
once that date passes; only dismiss_no_action (affirmative) stops it forever.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)
DEFAULT_TENANT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
DMS_ROOT = "/mnt/praesidium"

MOTION_CODES = {"motion", "motion_to_dismiss", "plea_to_jurisdiction",
                "special_appearance"}
RESPONSE_CODES = {"response", "reply"}
TRANSCRIPT_CODES = {"deposition"}
EXHIBIT_LIST_CODES = {"exhibit_list"}
ROUTABLE = MOTION_CODES | RESPONSE_CODES | TRANSCRIPT_CODES | EXHIBIT_LIST_CODES

# motion code -> reconciled hearing_type bucket (for auto-linking the hearing)
_MOTION_TO_HTYPE = {
    "motion": "motion to compel", "motion_to_dismiss": "motion to dismiss",
    "plea_to_jurisdiction": "plea to the jurisdiction",
    "special_appearance": "special appearance",
}


# --------------------------------------------------------------------------- #
#  Alert upsert (idempotent) + lifecycle                                       #
# --------------------------------------------------------------------------- #
async def _raise(s, tid, alert_type, title, source_kind, source_id, *,
                 matter_id=None, suggested=None, severity="warning", detail=None):
    """Idempotent: re-running triage updates an open alert rather than dup'ing,
    and never reopens one a human already resolved/dismissed."""
    return (await s.execute(sa_text(
        "INSERT INTO routing_alerts "
        "(tenant_id, alert_type, title, detail, source_kind, source_id, "
        " matter_id, suggested, severity) "
        "VALUES (:t,:at,:ti,CAST(:d AS jsonb),:sk,:sid,CAST(:m AS uuid),"
        " CAST(:sg AS jsonb),:sev) "
        "ON CONFLICT (tenant_id, alert_type, source_kind, source_id) DO UPDATE SET "
        "  title=EXCLUDED.title, detail=EXCLUDED.detail, matter_id=EXCLUDED.matter_id, "
        "  suggested=EXCLUDED.suggested, updated_at=now() "
        "WHERE routing_alerts.status IN ('open','snoozed') "
        "RETURNING id::text, status"),
        {"t": tid, "at": alert_type, "ti": title, "d": json.dumps(detail or {}),
         "sk": source_kind, "sid": source_id, "m": matter_id,
         "sg": json.dumps(suggested or {}), "sev": severity})).mappings().fetchone()


async def _resolved_marker(s, tid, alert_type, title, source_kind, source_id, *,
                           matter_id=None, resolution="auto_routed", target=None):
    """Record an auto-routed item as a resolved alert (audit trail, not inbox)."""
    await s.execute(sa_text(
        "INSERT INTO routing_alerts "
        "(tenant_id, alert_type, title, source_kind, source_id, matter_id, "
        " status, resolution, resolved_target, resolved_at) "
        "VALUES (:t,:at,:ti,:sk,:sid,CAST(:m AS uuid),'resolved',:res,:tg,now()) "
        "ON CONFLICT (tenant_id, alert_type, source_kind, source_id) DO UPDATE SET "
        "  resolution=EXCLUDED.resolution, resolved_target=EXCLUDED.resolved_target, "
        "  updated_at=now() "
        "WHERE routing_alerts.status NOT IN ('dismissed_no_action')"),
        {"t": tid, "at": alert_type, "ti": title, "sk": source_kind,
         "sid": source_id, "m": matter_id, "res": resolution, "tg": target})


async def _matter_for_path(s, tid, file_path):
    from modules.intelligence.matter_paths import matter_for_path
    return await matter_for_path(s, tid, file_path)


# --------------------------------------------------------------------------- #
#  Motion workspace (auto-populated)                                           #
# --------------------------------------------------------------------------- #
async def _ensure_motion_workspace(s, tid, matter_id, doc_id, motion_type, title):
    existing = (await s.execute(sa_text(
        "SELECT id::text, hearing_id::text, response_document_id::text "
        "FROM motion_workspaces WHERE matter_id=CAST(:m AS uuid) "
        "AND motion_document_id=CAST(:d AS uuid)"),
        {"m": matter_id, "d": doc_id})).mappings().fetchone()
    # auto-link a reconciled hearing of the matching type, if one exists
    htype = _MOTION_TO_HTYPE.get(motion_type, "")
    hid = (await s.execute(sa_text(
        "SELECT id::text FROM hearings WHERE matter_id=CAST(:m AS uuid) "
        "AND TRIM(tenant_id)=TRIM(:t) AND lower(coalesce(hearing_type,'')) LIKE :ht "
        "ORDER BY created_at LIMIT 1"),
        {"m": matter_id, "t": tid, "ht": "%"+htype+"%" if htype else "%motion%"})).scalar()
    autopop = {"motion_type": motion_type, "hearing_linked": bool(hid)}
    if existing:
        await s.execute(sa_text(
            "UPDATE motion_workspaces SET motion_type=:mt, "
            " hearing_id=COALESCE(hearing_id, CAST(:h AS uuid)), "
            " status=CASE WHEN COALESCE(hearing_id,CAST(:h AS uuid)) IS NOT NULL "
            "   AND status='open' THEN 'hearing_set' ELSE status END, "
            " autopop=CAST(:ap AS jsonb), updated_at=now() "
            "WHERE id=CAST(:id AS uuid)"),
            {"mt": motion_type, "h": hid, "ap": json.dumps(autopop),
             "id": existing["id"]})
        return existing["id"], bool(existing["hearing_id"] or hid), \
            bool(existing["response_document_id"])
    wsid = (await s.execute(sa_text(
        "INSERT INTO motion_workspaces (tenant_id, matter_id, title, "
        " motion_document_id, motion_type, hearing_id, status, autopop) "
        "VALUES (:t, CAST(:m AS uuid), :ti, CAST(:d AS uuid), :mt, "
        " CAST(:h AS uuid), :st, CAST(:ap AS jsonb)) RETURNING id::text"),
        {"t": tid, "m": matter_id, "ti": title, "d": doc_id, "mt": motion_type,
         "h": hid, "st": "hearing_set" if hid else "open",
         "ap": json.dumps(autopop)})).scalar()
    return wsid, bool(hid), False


# --------------------------------------------------------------------------- #
#  Triage driver                                                               #
# --------------------------------------------------------------------------- #
async def triage(tid=DEFAULT_TENANT, *, matter_id=None, limit=1000, backfill=False):
    summary = {"scanned": 0, "auto_routed": 0, "alerts": 0, "motion_workspaces": 0,
               "transcripts": 0, "responses": 0}
    async with AsyncSessionLocal() as s:
        async def araise(*a, **k):
            # backfill of historical docs builds workspaces + auto-route markers
            # but must NOT flood the live inbox with stale "needs filing" alerts
            return None if backfill else await araise( *a, **k)
        where = ["TRIM(d.tenant_id)=TRIM(:t)", "cr.superseded_by_id IS NULL",
                 "t.code = ANY(:codes)"]
        params = {"t": tid, "codes": list(ROUTABLE), "lim": limit}
        if matter_id:
            from modules.intelligence.matter_paths import disk_prefixes
            prefixes = await disk_prefixes(s, tid, matter_id)
            if prefixes:
                where.append("d.file_path LIKE ANY(:pfxs)")
                params["pfxs"] = prefixes
        docs = (await s.execute(sa_text(
            "SELECT d.id::text id, d.file_path, t.code "
            "FROM dms_documents d "
            "JOIN classification_results cr ON cr.dms_document_id=d.id "
            "JOIN document_type_taxonomy t ON t.id=cr.document_type_id "
            "WHERE " + " AND ".join(where) +
            " ORDER BY d.indexed_at DESC NULLS LAST LIMIT :lim"),
            params)).mappings().fetchall()

        for d in docs:
            summary["scanned"] += 1
            code = d["code"]
            mid = await _matter_for_path(s, tid, d["file_path"])
            fname = (d["file_path"] or "").rsplit("/", 1)[-1]

            if code in TRANSCRIPT_CODES:
                summary["transcripts"] += 1
                if mid:
                    await _resolved_marker(s, tid, "route_transcript", fname,
                                           "document", d["id"], matter_id=mid,
                                           resolution="auto_routed",
                                           target=f"/depositions/home/{mid}")
                    summary["auto_routed"] += 1
                else:
                    a = await araise( "route_transcript",
                                     f"Transcript needs routing: {fname}",
                                     "document", d["id"], severity="warning",
                                     suggested={"workspace_kind": "deposition"})
                    if a: summary["alerts"] += 1

            elif code in EXHIBIT_LIST_CODES:
                if not mid:
                    a = await araise("needs_routing",
                                     f"Exhibit list needs a matter: {fname}",
                                     "document", d["id"], severity="warning",
                                     suggested={"workspace_kind": "trial"})
                    if a: summary["alerts"] += 1
                    continue
                try:
                    from modules.depositions.jobs.exhibit_list_ingest import ingest_exhibit_list
                    res = await ingest_exhibit_list(tid, d["id"], matter_id=mid)
                    summary["exhibits_created"] = summary.get("exhibits_created", 0) + res.get("created", 0)
                    await _resolved_marker(s, tid, "route_exhibit_list", fname,
                                           "document", d["id"], matter_id=mid,
                                           resolution="auto_routed",
                                           target=f"/trial/home/{mid}")
                    summary["auto_routed"] += 1
                except Exception:
                    logger.exception("exhibit_list ingest failed for %s", d["id"])

            elif code in MOTION_CODES:
                if not mid:
                    a = await araise( "needs_routing",
                                     f"Motion needs a matter: {fname}",
                                     "document", d["id"], severity="warning",
                                     suggested={"workspace_kind": "motion"})
                    if a: summary["alerts"] += 1
                    continue
                wsid, has_hearing, has_resp = await _ensure_motion_workspace(
                    s, tid, mid, d["id"], code, fname)
                summary["motion_workspaces"] += 1
                summary["auto_routed"] += 1
                # gaps -> missing_filing alerts (snooze/dismiss manage the noise)
                # distinct source_id per gap so both can coexist in the inbox
                if not has_hearing:
                    a = await araise( "missing_filing",
                                     f"No hearing set for motion: {fname}",
                                     "motion_workspace", wsid+":hearing", matter_id=mid,
                                     severity="info",
                                     detail={"gap": "hearing", "workspace_id": wsid,
                                             "motion_doc": d["id"]})
                    if a: summary["alerts"] += 1
                if not has_resp:
                    a = await araise( "missing_filing",
                                     f"No response on file for motion: {fname}",
                                     "motion_workspace", wsid+":response", matter_id=mid,
                                     severity="info",
                                     detail={"gap": "response", "workspace_id": wsid,
                                             "motion_doc": d["id"]})
                    if a: summary["alerts"] += 1

            elif code in RESPONSE_CODES:
                summary["responses"] += 1
                if not mid:
                    a = await araise( "route_response",
                                     f"Response needs a matter: {fname}",
                                     "document", d["id"], severity="warning")
                    if a: summary["alerts"] += 1
                    continue
                # link to a motion workspace lacking a response
                ws = (await s.execute(sa_text(
                    "SELECT id::text FROM motion_workspaces "
                    "WHERE matter_id=CAST(:m AS uuid) AND response_document_id IS NULL "
                    "ORDER BY created_at LIMIT 2"),
                    {"m": mid})).mappings().fetchall()
                if len(ws) == 1:
                    await s.execute(sa_text(
                        "UPDATE motion_workspaces SET response_document_id=CAST(:r AS uuid), "
                        " updated_at=now() WHERE id=CAST(:w AS uuid)"),
                        {"r": d["id"], "w": ws[0]["id"]})
                    await _resolved_marker(s, tid, "route_response", fname,
                                           "document", d["id"], matter_id=mid,
                                           resolution="auto_routed",
                                           target="motion_workspace:"+ws[0]["id"])
                    summary["auto_routed"] += 1
                    # clear the matching "no response" gap alert
                    await s.execute(sa_text(
                        "UPDATE routing_alerts SET status='resolved', "
                        " resolution='auto_filled', resolved_at=now() "
                        "WHERE tenant_id=:t AND alert_type='missing_filing' "
                        "AND source_id=:w AND status IN ('open','snoozed')"),
                        {"t": tid, "w": ws[0]["id"]+":response"})
                else:
                    a = await araise( "route_response",
                                     f"Response — which motion? {fname}",
                                     "document", d["id"], matter_id=mid,
                                     severity="warning",
                                     detail={"candidates": len(ws)})
                    if a: summary["alerts"] += 1
        await s.commit()
    return summary


# --------------------------------------------------------------------------- #
#  Inbox surface + alert lifecycle                                             #
# --------------------------------------------------------------------------- #
async def surface_alerts(tid=DEFAULT_TENANT, *, matter_id=None, include_resolved=False):
    """The daily inbox: open alerts + snoozed ones whose snooze has expired."""
    async with AsyncSessionLocal() as s:
        where = ["TRIM(tenant_id)=TRIM(:t)"]
        params = {"t": tid}
        if include_resolved:
            where.append("status IN ('open','snoozed','resolved')")
        else:
            where.append("(status='open' OR (status='snoozed' AND snoozed_until <= CURRENT_DATE))")
        if matter_id:
            where.append("matter_id=CAST(:m AS uuid)")
            params["m"] = matter_id
        rows = (await s.execute(sa_text(
            "SELECT id::text, alert_type, title, detail, source_kind, source_id, "
            "       matter_id::text, suggested, severity, status, snoozed_until, "
            "       snooze_count, created_at "
            "FROM routing_alerts WHERE " + " AND ".join(where) +
            " ORDER BY (severity='critical') DESC, (severity='warning') DESC, created_at"),
            params)).mappings().fetchall()
        # mark surfaced
        ids = [r["id"] for r in rows]
        if ids:
            await s.execute(sa_text(
                "UPDATE routing_alerts SET last_surfaced_at=now() "
                "WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids})
            await s.commit()
        return {"alerts": [dict(r) for r in rows], "count": len(rows)}


async def snooze_alert(tid, alert_id, days=1, actor=None):
    until = dt.date.today() + dt.timedelta(days=max(1, int(days)))
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "UPDATE routing_alerts SET status='snoozed', snoozed_until=CAST(:u AS date), "
            " snooze_count=snooze_count+1, updated_at=now() "
            "WHERE id=CAST(:a AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"u": until, "a": alert_id, "t": tid})
        await s.commit()
        return {"alert_id": alert_id, "status": "snoozed",
                "snoozed_until": until.isoformat()}


async def dismiss_alert(tid, alert_id, reason=None, actor=None):
    """Affirmative: this does NOT need a workspace/routing. Never returns."""
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "UPDATE routing_alerts SET status='dismissed_no_action', "
            " resolution=COALESCE(:r,'no_action_needed'), resolved_by=CAST(:a AS bigint), "
            " resolved_at=now(), updated_at=now() "
            "WHERE id=CAST(:al AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"r": reason, "a": actor, "al": alert_id, "t": tid})
        await s.commit()
        return {"alert_id": alert_id, "status": "dismissed_no_action"}


async def route_alert(tid, alert_id, *, matter_id=None, target=None, actor=None):
    """Human routes it: stamp matter/target and resolve."""
    async with AsyncSessionLocal() as s:
        await s.execute(sa_text(
            "UPDATE routing_alerts SET status='resolved', resolution='routed', "
            " matter_id=COALESCE(CAST(:m AS uuid), matter_id), resolved_target=:tg, "
            " resolved_by=CAST(:a AS bigint), resolved_at=now(), updated_at=now() "
            "WHERE id=CAST(:al AS uuid) AND TRIM(tenant_id)=TRIM(:t)"),
            {"m": matter_id, "tg": target, "a": actor, "al": alert_id, "t": tid})
        await s.commit()
        return {"alert_id": alert_id, "status": "resolved"}


async def list_motion_workspaces(tid, matter_id):
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(sa_text(
            "SELECT mw.id::text, mw.title, mw.motion_type, mw.status, mw.outcome, "
            "       mw.hearing_id::text, mw.response_document_id::text, "
            "       mw.proposed_order_document_id::text, mw.autopop, "
            "       h.current_start_at hearing_date, h.hearing_type "
            "FROM motion_workspaces mw LEFT JOIN hearings h ON h.id=mw.hearing_id "
            "WHERE mw.matter_id=CAST(:m AS uuid) AND TRIM(mw.tenant_id)=TRIM(:t) "
            "ORDER BY mw.created_at"),
            {"m": matter_id, "t": tid})).mappings().fetchall()
        return {"matter_id": matter_id, "motions": [dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #
def _main():
    import argparse, asyncio
    ap = argparse.ArgumentParser(description="Forward-loop routing + alerts")
    ap.add_argument("--tenant", default=DEFAULT_TENANT)
    ap.add_argument("--matter")
    ap.add_argument("--surface", action="store_true", help="print the inbox")
    ap.add_argument("--backfill", action="store_true",
                    help="historical: build workspaces, suppress inbox alerts")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if a.surface:
        print(json.dumps(asyncio.run(surface_alerts(a.tenant, matter_id=a.matter)),
                         indent=2, default=str))
    else:
        print(json.dumps(asyncio.run(triage(a.tenant, matter_id=a.matter,
                                             backfill=a.backfill)),
                         indent=2, default=str))


if __name__ == "__main__":
    _main()
