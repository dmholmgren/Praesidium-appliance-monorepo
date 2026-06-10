"""
pipeline_orchestrator.py -- eDiscovery ingestion orchestrator (RQ job).

Sequences the validated geometry spine on a single collection:
  extract-archives -> preserve -> enrich -> geometry
    -> render lane -> ocr lane -> segment -> chunk -> embed

Each stage is the exact validated CLI run as a subprocess from /app, so the
orchestrator is a thin, idempotent, restartable sequencer (every stage is
itself resumable). Progress is written to ediscovery_ingestion_log for the live
UI; ediscovery_collections.status is advanced processing -> review_ready/failed.
"""
import os
import sys
import subprocess
import zipfile
import tarfile
import logging
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

logger = logging.getLogger(__name__)

PY = sys.executable
APP = "/app"
TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
ARCHIVE_EXTS = {".zip", ".tar", ".tgz", ".7z"}  # .pst is handled by preserve itself


def _db():
    u = os.environ.get("DATABASE_URL", "")
    for p in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if u.startswith(p):
            u = "postgresql://" + u[len(p):]
            break
    q = urlparse(u)
    return psycopg2.connect(dbname=q.path.lstrip("/") or "praesidium",
                            user=q.username or "praesidium", password=q.password or "",
                            host=q.hostname or "172.28.0.1", port=str(q.port or 5432))


def _log(cur, tenant, cid, msg, level="info"):
    cur.execute("INSERT INTO ediscovery_ingestion_log (tenant_id, collection_id, level, message) "
                "VALUES (%s, %s::uuid, %s, %s)", (tenant, str(cid), level, msg[:2000]))


def _status(cur, tenant, cid, status):
    cur.execute("UPDATE ediscovery_collections SET status=%s, updated_at=now() "
                "WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (status, str(cid), tenant))


def _storage_path(cur, tenant, cid):
    cur.execute("SELECT storage_path FROM ediscovery_collections "
                "WHERE id=%s::uuid AND TRIM(tenant_id)=%s", (str(cid), tenant))
    r = cur.fetchone()
    if not r:
        raise ValueError("collection %s not found" % cid)
    return r[0]


def _extract_archives(storage_path):
    """Extract zip/tar archives in originals/as_received -> originals/unpacked,
    leaving the archive in place (immutable received copy). Returns count."""
    asr = Path(storage_path) / "originals" / "as_received"
    unp = Path(storage_path) / "originals" / "unpacked"
    n = 0
    if not asr.exists():
        return 0
    for f in sorted(asr.rglob("*")):
        if f.is_file() and f.suffix.lower() in ARCHIVE_EXTS:
            dest = unp / f.stem
            dest.mkdir(parents=True, exist_ok=True)
            try:
                if f.suffix.lower() == ".zip":
                    with zipfile.ZipFile(f) as z:
                        z.extractall(dest)
                else:
                    with tarfile.open(f, "r:*") as t:
                        t.extractall(dest)
                n += 1
            except Exception:
                logger.exception("archive extract failed: %s", f)
    return n


def run_ediscovery_pipeline(tenant_id, collection_id, user_id=None):
    tenant = (tenant_id or TENANT_DEFAULT).strip()
    cid = str(collection_id)
    conn = _db()
    conn.autocommit = True
    cur = conn.cursor()

    def prog(m, lvl="info"):
        try:
            _log(cur, tenant, cid, m, lvl)
        except Exception:
            pass
        logger.info("[pipeline %s] %s", cid[:8], m)

    try:
        storage = _storage_path(cur, tenant, cid)
        _status(cur, tenant, cid, "processing")
        prog("Ingestion pipeline started (geometry spine)")

        nz = _extract_archives(storage)
        if nz:
            prog("Extracted %d archive(s) from as_received -> unpacked" % nz)

        C = ["--collection", cid]
        T = ["--tenant", tenant]
        stages = [
            ("preserve",  [PY, "modules/ediscovery/jobs/preserve_collection.py"] + T + C),
            ("custodian", [PY, "-m", "modules.ediscovery.jobs.custodian_registry"] + T + C),
            ("enrich",    [PY, "modules/ediscovery/jobs/enrich_collection.py"] + T + C),
            ("geometry", [PY, "-m", "modules.ediscovery.jobs.geometry_intake"] + C),
            ("render",   [PY, "-m", "modules.ediscovery.jobs.geometry_intake", "--stage", "render"] + C),
            ("ocr",      [PY, "-m", "modules.ediscovery.jobs.geometry_intake", "--stage", "ocr"] + C),
            ("segment",  [PY, "jobs/canonical_segmenter.py", "--corpus", "ediscovery", "--all"] + C),
            ("chunk",    [PY, "jobs/spine_chunker.py", "--corpus", "ediscovery", "--all"] + C),
            ("embed",    [PY, "-m", "modules.ediscovery.jobs.embed_ediscovery_chunks"] + C),
        ]
        for name, cmd in stages:
            prog("stage %s: starting" % name)
            r = subprocess.run(cmd, cwd=APP, capture_output=True, text=True)
            # stage CLIs log to stderr; fall back to it for the summary line
            blob = ((r.stdout or "").strip() or (r.stderr or "").strip())
            lines = [ln for ln in blob.splitlines() if ln.strip()]
            last = lines[-1] if lines else ""
            if r.returncode != 0:
                err = (r.stderr or "").strip().splitlines()
                prog("stage %s FAILED rc=%d: %s" % (name, r.returncode,
                     (err[-1] if err else "")[:400]), "error")
                _status(cur, tenant, cid, "failed")
                raise RuntimeError("stage %s failed (rc=%d)" % (name, r.returncode))
            prog("stage %s: done -- %s" % (name, last[:400]))

        _status(cur, tenant, cid, "review_ready")
        prog("Pipeline complete -- collection ready for review", "success")
        return {"status": "review_ready", "collection_id": cid}
    except Exception as e:
        try:
            _log(cur, tenant, cid, "FATAL: %s" % str(e)[:400], "error")
        except Exception:
            pass
        logger.exception("pipeline failed for %s", cid)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass
