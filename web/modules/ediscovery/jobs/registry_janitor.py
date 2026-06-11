"""Reconcile RQ registries against live workers.

- Started jobs not claimed by any live worker (and older than the grace
  period) are moved to the FailedJobRegistry with an 'orphaned' reason, so
  the GUI and reports never show phantom in-flight work.
- Failed entries older than RQ_FAILED_RETENTION_DAYS (default 7) are deleted.

Run manually:
    docker exec praesidium-proc-worker-1 \\
        python -m modules.ediscovery.jobs.registry_janitor
Scheduled nightly via /etc/cron.d/praesidium-rq-janitor.
"""
import os
from datetime import datetime, timedelta, timezone

from redis import from_url
from rq import Queue
from rq.job import Job
from rq.registry import FailedJobRegistry, StartedJobRegistry
from rq.worker import Worker

QUEUES = ["ediscovery", "ediscovery_proc", "default"]
RETENTION_DAYS = int(os.environ.get("RQ_FAILED_RETENTION_DAYS", "7"))
GRACE_MINUTES = int(os.environ.get("RQ_ORPHAN_GRACE_MINUTES", "15"))


def main() -> None:
    r = from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    now = datetime.now(timezone.utc)
    claimed = set()
    for w in Worker.all(connection=r):
        jid = w.get_current_job_id()
        if jid:
            claimed.add(jid)

    orphaned = 0
    purged = 0
    for qn in QUEUES:
        q = Queue(qn, connection=r)
        sr = StartedJobRegistry(queue=q)
        for jid in list(sr.get_job_ids()):
            if jid in claimed:
                continue
            try:
                job = Job.fetch(jid, connection=r)
            except Exception:
                sr.remove(jid)
                orphaned += 1
                continue
            started = job.started_at
            if started is not None:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if now - started < timedelta(minutes=GRACE_MINUTES):
                    continue
            try:
                sr.remove(job)
                job.set_status("failed")
                FailedJobRegistry(queue=q).add(
                    job, ttl=RETENTION_DAYS * 86400,
                    exc_string="orphaned: no live worker claims this job "
                               "(registry janitor)")
                orphaned += 1
                print(f"orphaned: {qn}/{jid} ({job.func_name})")
            except Exception as e:
                print(f"orphan-move failed: {qn}/{jid}: {e}")

        fr = FailedJobRegistry(queue=q)
        cutoff = now - timedelta(days=RETENTION_DAYS)
        for jid in list(fr.get_job_ids()):
            try:
                job = Job.fetch(jid, connection=r)
                ended = job.ended_at
                if ended is not None and ended.tzinfo is None:
                    ended = ended.replace(tzinfo=timezone.utc)
                if ended is None or ended < cutoff:
                    fr.remove(job, delete_job=True)
                    purged += 1
            except Exception:
                try:
                    fr.remove(jid)
                    purged += 1
                except Exception:
                    pass

    print(f"janitor done: orphaned={orphaned} failed_purged={purged}")


if __name__ == "__main__":
    main()
