"""
Deposition Inbox — zero-click ingest. Staff drop a deposition folder into the
inbox share and it auto-triages + ingests through the guided pipeline.

CLOUD-ANTICIPATING DESIGN
  * POLLING, not inotify — a scheduled scan works identically over a local
    filesystem or an object store (S3 list-objects). No FS-event dependency.
  * Thin STORAGE SEAM (_ls / _newest_mtime / _move / _is_dir) — reimplement
    these over boto3 to run on S3; nothing else changes.
  * Atomic CLAIM (rename to <batch>.processing) so concurrent/autoscaled
    workers never double-ingest the same batch. On S3 use a marker object.
  * STABILITY gate — a batch is ingested only once no file under it has been
    modified for STABLE_SECS, so partial copies aren't grabbed mid-transfer.

Inbox layout (per tenant):
  <root>/<matter_id>/<batch folder>/...     <- drop here  (matter_id in the path)
  <root>/_processed/<batch>-<ts>/           <- archived on success
  <root>/_failed/<batch>/ (+ .error)        <- archived on failure
"""
from __future__ import annotations
import os
import time
import logging
import shutil

logger = logging.getLogger(__name__)

STABLE_SECS = 120          # a batch must be quiescent this long before ingest
PROC_SUFFIX = ".processing"
PRAESIDIUM_ROOT = "/mnt/praesidium"   # cloud: s3://<bucket>


# --------------------------------------------------------------------------- #
# storage seam — swap these four for object-store impls in cloud
# --------------------------------------------------------------------------- #
def _ls(d):
    try:
        return sorted(os.scandir(d), key=lambda e: e.name)
    except OSError:
        return []


def _newest_mtime(d):
    newest = 0.0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def _move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(src, dst)


def _inbox_root(ten):
    return os.path.join(PRAESIDIUM_ROOT, ten.strip(), "_depo_inbox")


def _tenants_with_inbox():
    out = []
    for e in _ls(PRAESIDIUM_ROOT):
        try:
            if e.is_dir() and os.path.isdir(os.path.join(e.path, "_depo_inbox")):
                out.append(e.name)
        except OSError:
            pass
    return out


# --------------------------------------------------------------------------- #
# scan + ingest (cron entrypoint)
# --------------------------------------------------------------------------- #
def scan_and_ingest():
    """Poll every tenant's inbox and ingest each stable, unclaimed batch.
    Idempotent + safe to run concurrently (atomic claim per batch)."""
    import asyncio
    from modules.depositions.services import bundle_ingest as bi
    stats = {"ingested_units": 0, "batches": 0, "skipped": 0, "failed": 0}
    now = time.time()
    for ten in _tenants_with_inbox():
        root = _inbox_root(ten)
        for me in _ls(root):                       # <matter_id>/
            if not me.is_dir() or me.name.startswith("_"):
                continue
            matter_id = me.name
            for be in _ls(me.path):                # <batch folder>/
                if not be.is_dir() or be.name.endswith(PROC_SUFFIX):
                    continue
                if now - _newest_mtime(be.path) < STABLE_SECS:
                    stats["skipped"] += 1
                    continue
                claimed = be.path + PROC_SUFFIX
                try:
                    os.rename(be.path, claimed)    # atomic claim
                except OSError:
                    stats["skipped"] += 1
                    continue
                stats["batches"] += 1
                try:
                    manifest = bi.gather_signals(claimed)
                    proposal = asyncio.run(bi.triage(manifest, ten, matter_id))
                    bi.annotate_embedded(proposal, claimed)
                    out = bi.dispatch(ten, matter_id, claimed, proposal)
                    n = len(out.get("units", []))
                    stats["ingested_units"] += n
                    _move(claimed, os.path.join(root, "_processed",
                                                f"{be.name}-{time.strftime('%Y%m%d%H%M%S')}"))
                    logger.info("depo_inbox: ingested %s/%s -> %d unit(s)",
                                matter_id, be.name, n)
                except Exception as e:
                    logger.exception("depo_inbox: ingest failed for %s/%s",
                                     matter_id, be.name)
                    dst = os.path.join(root, "_failed", be.name)
                    try:
                        _move(claimed, dst)
                        with open(os.path.join(dst, ".error"), "w") as fh:
                            fh.write(str(e)[:2000])
                    except Exception:
                        pass
                    stats["failed"] += 1
    if stats["batches"] or stats["failed"]:
        logger.info("depo_inbox scan: %s", stats)
    return stats


def ensure_inbox(tenant_id, matter_id):
    """Create (and return) the inbox folder for a matter so the UI can reveal
    the drop path to staff."""
    p = os.path.join(_inbox_root(tenant_id), matter_id)
    os.makedirs(p, exist_ok=True)
    return p


if __name__ == "__main__":
    import json
    print(json.dumps(scan_and_ingest(), indent=2))
