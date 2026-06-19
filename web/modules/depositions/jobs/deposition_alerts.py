"""deposition_alerts.py -- alert-driven deposition ingestion (scope §7).

Mirrors the onboarding-alert model (Onboarding_Alerts_Handoff_v1): a transcript
landing in the depo intake path raises ONE deterministic, zero-LLM alert per
matter into the shared onboarding_alerts table. No model call fires here -- the
alert only flags that transcripts are owed ingestion and carries an "Ingest"
action. Heavy work (parse/segment/embed) happens only when the user clicks.

  alert_type = DEPOSITION_TRANSCRIPT_PENDING
  count      = number of registered-but-not-yet-ingested transcripts for the matter
  payload    = {transcripts: [{id, deponent, source_format, has_video,
                               source_file_path, sha256}], total}

Idempotent via the uq_onb_alert_live partial-unique index (tenant, matter,
alert_type) WHERE status <> 'dismissed': re-running refreshes the live alert in
place. When all transcripts are ingested the live alert is cleared.

register_pending() is the intake half: it creates the deposition_transcripts row
WITHOUT seeding ingest (status 'pending'), then recomputes the alert. The action
(routes/alerts_api.py) seeds the ingest ledger rows on click.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

ALERT_TYPE = "DEPOSITION_TRANSCRIPT_PENDING"


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def recompute_deposition_alerts(conn, tenant_id, matter_id) -> dict:
    """Deterministic upsert of the matter's deposition-ingestion alert. Reads the
    pending (registered, not yet ingested) transcripts and writes one alert; if
    none remain, clears the live alert. Zero-LLM, zero-queue."""
    tenant = (tenant_id or "").strip()
    cur = conn.cursor()
    cur.execute(
        "SELECT id::text, deponent, source_format, has_video, source_file_path, sha256 "
        "FROM deposition_transcripts "
        "WHERE TRIM(tenant_id) = %s AND matter_id = CAST(%s AS uuid) "
        "  AND status = 'pending' "
        "ORDER BY imported_at",
        (tenant, str(matter_id)))
    rows = cur.fetchall()
    n = len(rows)
    if n == 0:
        # clear any live (non-dismissed) alert -- nothing owed
        cur.execute(
            "DELETE FROM onboarding_alerts "
            "WHERE TRIM(tenant_id) = %s AND matter_id = CAST(%s AS uuid) "
            "  AND alert_type = %s AND status <> 'dismissed'",
            (tenant, str(matter_id), ALERT_TYPE))
        conn.commit()
        return {"alert": None, "pending": 0}

    payload = {"transcripts": [
        {"id": r[0], "deponent": r[1], "source_format": r[2],
         "has_video": r[3], "source_file_path": r[4], "sha256": r[5]}
        for r in rows], "total": n}
    msg = "%d deposition transcript%s pending ingestion" % (n, "" if n == 1 else "s")
    cur.execute(
        "INSERT INTO onboarding_alerts "
        "  (tenant_id, matter_id, alert_type, count, message, status, payload, created_at) "
        "VALUES (%s, CAST(%s AS uuid), %s, %s, %s, 'active', %s::jsonb, now()) "
        "ON CONFLICT (tenant_id, matter_id, alert_type) WHERE status <> 'dismissed' "
        "DO UPDATE SET count = EXCLUDED.count, message = EXCLUDED.message, "
        "  payload = EXCLUDED.payload, status = 'active' "
        "RETURNING id::text",
        (tenant, str(matter_id), ALERT_TYPE, n, msg, json.dumps(payload)))
    alert_id = cur.fetchone()[0]
    conn.commit()
    logger.info("deposition alert %s: %d pending for matter %s", alert_id, n, matter_id)
    return {"alert": alert_id, "pending": n}


def register_pending(tenant_id, file_path, matter_id, session_id=None,
                     deponent=None, has_video=False) -> dict:
    """Intake half: register a transcript WITHOUT seeding ingest, then raise/refresh
    the matter's alert. The actual ingest is seeded by the alert's Ingest action."""
    from modules.depositions.jobs.depo_dag import register_transcript
    reg = register_transcript(tenant_id, file_path, session_id=session_id,
                              matter_id=matter_id, deponent=deponent,
                              has_video=has_video, seed=False)
    conn = _connect()
    try:
        rec = recompute_deposition_alerts(conn, tenant_id, matter_id)
    finally:
        conn.close()
    return {"register": reg, "alert": rec}


def main():
    import argparse, os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Deposition ingestion alerts")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--matter", required=True)
    ap.add_argument("--register", default=None, help="file path to register as pending")
    ap.add_argument("--session", type=int, default=None)
    ap.add_argument("--deponent", default=None)
    ap.add_argument("--recompute", action="store_true")
    args = ap.parse_args()
    if args.register:
        out = register_pending(args.tenant, args.register, args.matter,
                               session_id=args.session, deponent=args.deponent)
    else:
        conn = _connect()
        try:
            out = recompute_deposition_alerts(conn, args.tenant, args.matter)
        finally:
            conn.close()
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
