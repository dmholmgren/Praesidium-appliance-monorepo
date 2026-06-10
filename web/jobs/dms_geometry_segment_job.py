"""
jobs/dms_geometry_segment_job.py
RQ job: DMS GEOMETRY + SEGMENT + CHUNK + EMBED stage -- the tail of the DMS
pipeline (scan -> extract_fast -> extract_ocr -> parse -> [this]).

Runs the unified ingestion downstream for the curated DMS corpus, mirroring the
eDiscovery orchestrator (... -> segment -> chunk -> embed):

  1. geometry  geometry_intake_dms        -> doc_geometry header + doc_layout_tokens
                                              (incl. block/line) via geometry_io.persist_geometry
  2. segment   canonical_segmenter v2     -> document_sections (paragraph, geometry-grounded)
  3. chunk     spine_chunker --corpus dms -> dms_chunks (canonical = geometry canonical; §0-true)
  4. embed     embed_dms_chunks           -> dms_chunk_embeddings.embedding_768 (ModernBERT-768)

Stages are idempotent + gated (geometry: DMS_GATE, PDF-only, skip already-built;
segment/chunk: docs with a live spine; embed: unembedded chunks). geometry,
segment, chunk are required (raise on failure -> job 'failed', resumable on
re-run). embed is best-effort (GPU-heavy, trivially re-runnable) -- a hiccup is
recorded but does not fail a run whose spine + chunks succeeded.

Async (RQ) so a large folder move / drag-drop import never blocks the UI. Tracked
in dms_scan_jobs (job_type='geometry_segment') for the admin poller.

Patent Pending -- Dennis M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

log = logging.getLogger("praesidium.jobs.dms_geometry_segment_job")

TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
SEGMENTER = "/app/jobs/canonical_segmenter.py"
CHUNKER = "/app/jobs/spine_chunker.py"
EMBEDDER = "/app/jobs/embed_dms_chunks.py"


def _get_db_conn():
    import psycopg2
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "")
    at = url.rfind("@")
    userinfo, hostinfo = url[:at], url[at + 1:]
    user, password = userinfo.split(":", 1)
    host_port, dbname = hostinfo.split("/", 1)
    host, port = host_port.rsplit(":", 1)
    return psycopg2.connect(host=host, port=int(port), dbname=dbname.split("?")[0],
                            user=user, password=password)


def _stage(name, cmd):
    """Run a pipeline stage as a subprocess from /app; return (rc, tail)."""
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=86400, cwd="/app")
    tail = ((r.stdout or "") + (r.stderr or ""))[-600:]
    log.info("geometry_segment stage %s rc=%s tail=%s", name, r.returncode, tail)
    return r.returncode, tail


def run_geometry_segment(job_id: str, tenant_id: str = None, redo: bool = False) -> dict:
    """DMS downstream: geometry -> segment -> chunk -> embed. Idempotent + gated."""
    import psycopg2.extras
    conn = _get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        if not tenant_id:
            cur.execute("SELECT tenant_id FROM dms_scan_jobs WHERE id=%s", (job_id,))
            r = cur.fetchone()
            tenant_id = (r["tenant_id"].strip() if r else TENANT_DEFAULT)
        tenant_id = tenant_id.strip()
        cur.execute("UPDATE dms_scan_jobs SET status='running', started_at=NOW() WHERE id=%s", (job_id,))

        T = ["--tenant", tenant_id]

        # 1) geometry over gated DMS PDFs (per-doc commit inside; resumable)
        from modules.ediscovery.jobs.geometry_intake import geometry_intake_dms
        g = geometry_intake_dms(tenant_id, redo=redo)
        log.info("geometry_segment %s: geometry_intake_dms -> %s", job_id, g)
        cur.execute("UPDATE dms_scan_jobs SET files_indexed=%s WHERE id=%s",
                    (int(g.get("pdf_tokenized", 0)), job_id))

        # 2) segment (required)
        rc, _ = _stage("segment", [sys.executable, SEGMENTER, "--corpus", "dms"] + T + ["--all"])
        if rc != 0:
            raise RuntimeError("segment stage failed rc=%s" % rc)

        # 3) chunk (required) -- spine chunker reads the geometry canonical
        rc, _ = _stage("chunk", [sys.executable, CHUNKER, "--corpus", "dms"] + T + ["--all"])
        if rc != 0:
            raise RuntimeError("chunk stage failed rc=%s" % rc)

        # 4) embed (best-effort; idempotently re-runnable)
        embed_rc, embed_tail = _stage("embed", [sys.executable, EMBEDDER] + T + ["--all"])

        # counts
        cur.execute("SELECT count(*) AS n FROM doc_geometry WHERE corpus='dms' AND TRIM(tenant_id)=%s",
                    (tenant_id,))
        headers = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM document_sections s "
                    "JOIN dms_documents d ON d.id = s.dms_document_id "
                    "WHERE TRIM(d.tenant_id)=%s AND s.superseded_by_run_id IS NULL "
                    "AND s.attributes->>'segmenter'='canonical_v2_geom'", (tenant_id,))
        para_segs = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM dms_chunks WHERE TRIM(tenant_id)=%s "
                    "AND source_type='dms_document'", (tenant_id,))
        chunks = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM dms_chunk_embeddings e "
                    "WHERE TRIM(e.tenant_id)=%s AND e.embedding_768 IS NOT NULL", (tenant_id,))
        embeds = cur.fetchone()["n"]

        err = None if embed_rc == 0 else ("embed stage rc=%s (re-runnable): %s" % (embed_rc, embed_tail[-300:]))
        cur.execute("UPDATE dms_scan_jobs SET status='complete', completed_at=NOW(), "
                    "files_discovered=%s, error_message=%s WHERE id=%s", (chunks, err, job_id))
        out = {"geometry": g, "dms_geometry_headers": headers, "paragraph_segments": para_segs,
               "dms_chunks": chunks, "dms_embeddings_768": embeds, "embed_rc": embed_rc}
        log.info("geometry_segment %s complete: %s", job_id, out)
        return out
    except Exception as e:
        log.exception("geometry_segment %s failed", job_id)
        try:
            cur.execute("UPDATE dms_scan_jobs SET status='failed', completed_at=NOW(), "
                        "error_message=%s WHERE id=%s", (str(e)[:4000], job_id))
        except Exception:
            pass
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    import uuid as _uuid
    jid = sys.argv[1] if len(sys.argv) > 1 else str(_uuid.uuid4())
    tid = sys.argv[2] if len(sys.argv) > 2 else TENANT_DEFAULT
    print(run_geometry_segment(jid, tid))
