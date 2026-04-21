"""Chat 4 — RQ Job Registration.

All heavy AI jobs dispatch to PROC-01 queue.
These are registered in the RQ worker configuration.
"""
from __future__ import annotations

# ── Jobs dispatched to PROC-01 ────────────────────────────────
#
# 1. generate_matter_summary_job(tenant_id, matter_id)
#    Location: modules.dashboard.services.case_summary
#    Queue: proc01
#    Timeout: 10m
#    Trigger: New matter created, significant docs added
#
# 2. generate_critical_date_memo_job(tenant_id, matter_id, document_id)
#    Location: modules.dashboard.services.real_estate
#    Queue: proc01
#    Timeout: 5m
#    Trigger: Real estate contract uploaded
#
# 3. compare_title_dates_job(tenant_id, matter_id, title_document_id)
#    Location: modules.dashboard.services.real_estate
#    Queue: proc01
#    Timeout: 5m
#    Trigger: Title company letter received
#
# 4. detect_commitments_job(tenant_id)
#    Location: modules.dashboard.services.task_system
#    Queue: proc01
#    Timeout: 15m
#    Schedule: Daily via RQ scheduler
#
# ── Registration ──────────────────────────────────────────────

JOB_REGISTRY = {
    "generate_matter_summary": {
        "func": "modules.dashboard.services.case_summary.generate_matter_summary_job",
        "queue": "proc01",
        "timeout": "10m",
    },
    "generate_critical_date_memo": {
        "func": "modules.dashboard.services.real_estate.generate_critical_date_memo_job",
        "queue": "proc01",
        "timeout": "5m",
    },
    "compare_title_dates": {
        "func": "modules.dashboard.services.real_estate.compare_title_dates_job",
        "queue": "proc01",
        "timeout": "5m",
    },
    "detect_commitments": {
        "func": "modules.dashboard.services.task_system.detect_commitments_job",
        "queue": "proc01",
        "timeout": "15m",
        "schedule": "daily",
    },
}


def enqueue_job(job_name: str, *args, **kwargs):
    """Enqueue a registered job to the appropriate queue."""
    import os
    from redis import Redis
    from rq import Queue

    reg = JOB_REGISTRY.get(job_name)
    if not reg:
        raise ValueError(f"Unknown job: {job_name}")

    redis_url = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
    q = Queue(reg["queue"], connection=Redis.from_url(redis_url))

    # Import the function dynamically
    module_path, func_name = reg["func"].rsplit(".", 1)
    import importlib
    mod = importlib.import_module(module_path)
    func = getattr(mod, func_name)

    return q.enqueue(func, *args, job_timeout=reg["timeout"], **kwargs)
