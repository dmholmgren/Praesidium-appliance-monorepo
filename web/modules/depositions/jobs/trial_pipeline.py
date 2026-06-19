"""trial_pipeline.py -- Module A trial-transcript front door over the shared DAG.

A trial = one trial_proceedings parent + N Reporter's Record volumes. Each volume
is its own deposition_transcripts row (transcript_kind='trial', volume=N) so the
existing depo DAG fans out ingest/segment/embed PER VOLUME (A3 parallelism) with
zero new queue machinery -- we reuse depo_dag wholesale and only add the trial
parent + kind-tagged registration. Citations resolve as '[volume] RR [page]:[line]'.

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.trial_pipeline --new-trial \
        --matter UUID --caption "State v. Doe" [--cause N] [--court "..."] \
        [--coa "Fifth District"] [--tenant T]
  python -m modules.depositions.jobs.trial_pipeline --add-volume \
        --trial UUID --file PATH --volume N [--day D] [--title "..."] \
        [--matter UUID] [--has-video] [--tenant T]
  python -m modules.depositions.jobs.trial_pipeline --run-volume \
        --trial UUID --file PATH --volume N [--matter UUID] [--tenant T]
  python -m modules.depositions.jobs.trial_pipeline --list --trial UUID
"""
from __future__ import annotations

import argparse
import json
import logging
import os

from modules.depositions.jobs import depo_dag

logger = logging.getLogger(__name__)


def _connect():
    return depo_dag._connect()


def create_trial(tenant_id, matter_id=None, caption=None, cause_number=None,
                 trial_court=None, court_of_appeals=None,
                 appellate_cause_number=None, date_start=None, date_end=None,
                 notes=None) -> dict:
    """Create the trial_proceedings parent (the handle Module B pulls the RR by)."""
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO trial_proceedings "
            "  (tenant_id, matter_id, caption, cause_number, trial_court, "
            "   court_of_appeals, appellate_cause_number, date_start, date_end, notes) "
            "VALUES (%s, CAST(%s AS uuid), %s, %s, %s, %s, %s, "
            "        CAST(%s AS date), CAST(%s AS date), %s) "
            "RETURNING id::text",
            (tenant, (str(matter_id) if matter_id else None), caption,
             cause_number, trial_court, court_of_appeals, appellate_cause_number,
             date_start, date_end, notes))
        tid = cur.fetchone()[0]
        conn.commit()
        logger.info("created trial_proceedings %s (%s)", tid, caption or "")
        return {"trial_id": tid, "caption": caption}
    finally:
        conn.close()


def add_volume(tenant_id, trial_id, file_path, volume, matter_id=None,
               trial_day=None, title=None, has_video=False, priority=100,
               seed=True) -> dict:
    """Register one Reporter's Record volume as a trial transcript + seed ingest.
    Inherits matter from the trial parent when not given."""
    tenant = (tenant_id or "").strip()
    if matter_id is None:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT matter_id::text FROM trial_proceedings "
                        "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                        (str(trial_id), tenant))
            r = cur.fetchone()
            if r:
                matter_id = r[0]
        finally:
            conn.close()
    label = title or ("Reporter's Record vol. %s" % volume if volume else
                      "Reporter's Record")
    reg = depo_dag.register_transcript(
        tenant, file_path, matter_id=matter_id, deponent=label,
        has_video=has_video, priority=priority, seed=seed,
        transcript_kind="trial", trial_id=trial_id, volume=volume,
        trial_day=trial_day, title=label)
    reg["trial_id"] = str(trial_id)
    reg["volume"] = volume
    return reg


def run_volume(tenant_id, trial_id, file_path, volume, matter_id=None,
               trial_day=None, title=None, has_video=False) -> dict:
    """Register + drain ingest->segment->embed inline for one volume (test path)."""
    reg = add_volume(tenant_id, trial_id, file_path, volume, matter_id=matter_id,
                     trial_day=trial_day, title=title, has_video=has_video)
    di = depo_dag.drain(depo_dag.STAGE_INGEST, idle_secs=2, max_units=1)
    ds = depo_dag.drain(depo_dag.STAGE_SEGMENT, idle_secs=2, max_units=1)
    de = depo_dag.drain(depo_dag.STAGE_EMBED, idle_secs=2)
    return {"register": reg, "ingest": di, "segment": ds, "embed": de,
            "status": depo_dag.status(reg["transcript_id"])}


def list_volumes(tenant_id, trial_id) -> list:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id::text, volume, trial_day, title, status, qa_count "
            "FROM deposition_transcripts "
            "WHERE trial_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "ORDER BY volume NULLS LAST, imported_at", (str(trial_id), tenant))
        return [{"transcript_id": r[0], "volume": r[1], "trial_day": r[2],
                 "title": r[3], "status": r[4], "qa_count": r[5]}
                for r in cur.fetchall()]
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Trial transcript pipeline (Module A)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--new-trial", action="store_true")
    ap.add_argument("--add-volume", action="store_true")
    ap.add_argument("--run-volume", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--matter", default=None)
    ap.add_argument("--trial", default=None)
    ap.add_argument("--caption", default=None)
    ap.add_argument("--cause", default=None)
    ap.add_argument("--court", default=None)
    ap.add_argument("--coa", default=None)
    ap.add_argument("--file", default=None)
    ap.add_argument("--volume", type=int, default=None)
    ap.add_argument("--day", type=int, default=None)
    ap.add_argument("--title", default=None)
    ap.add_argument("--has-video", action="store_true")
    args = ap.parse_args()

    if args.new_trial:
        out = create_trial(args.tenant, matter_id=args.matter,
                           caption=args.caption, cause_number=args.cause,
                           trial_court=args.court, court_of_appeals=args.coa)
    elif args.add_volume:
        if not (args.trial and args.file):
            ap.error("--add-volume requires --trial and --file")
        out = add_volume(args.tenant, args.trial, args.file, args.volume,
                         matter_id=args.matter, trial_day=args.day,
                         title=args.title, has_video=args.has_video)
    elif args.run_volume:
        if not (args.trial and args.file):
            ap.error("--run-volume requires --trial and --file")
        out = run_volume(args.tenant, args.trial, args.file, args.volume,
                         matter_id=args.matter, trial_day=args.day,
                         title=args.title, has_video=args.has_video)
    elif args.list:
        if not args.trial:
            ap.error("--list requires --trial")
        out = list_volumes(args.tenant, args.trial)
    else:
        ap.error("one of --new-trial/--add-volume/--run-volume/--list required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
