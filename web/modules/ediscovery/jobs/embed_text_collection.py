"""
embed_text_collection.py — TEXT-FIRST embedding for already-ingested eDiscovery
collections, with per-collection backend routing (V100 vs RunPod burst).

WHY THIS EXISTS (lessons from the Marcus productions, 2026-06-15):
  * Load-file productions are ~99% image+text: the docs carry extracted_text but
    have NO renderable native. The spine DAG (_spine_unit) renders a native ->
    PDF -> geometry before chunking, so it FAILS on these ("native_path not
    found" / "render produced no pdf").
  * run_collection / run_collection_full is NOT a way to "add embeddings" to an
    already-ingested collection: its enrich/explode stages RE-INGEST and rewrite
    documents, clobbering Bates / native_file_hash / file_path. Do not use it for
    embeddings on a finished collection.
  * The safe path: docs already have extracted_text, which for eDiscovery equals
    the geometry canonical, so segment+chunk runs straight off the text with no
    render. This module does exactly that, then embeds.

BACKEND ROUTING (2026-06-15):
  * ediscovery_collections.embed_backend selects where a collection's chunks get
    embedded. 'runpod' -> the burst shim (RunPod serverless, autoscaling GPUs);
    anything else ('v100'/NULL/unknown) -> the local V100 service. The choice is
    surfaced as the "Fast embedding" switch in the create modal; this module is
    the job-side half that honors it. Vectors are identical either way (same
    model + revision + prefix + normalization; verified cosine ~1.0).

GUARANTEES:
  * Non-destructive: only writes document_sections, ediscovery_chunks, and
    ediscovery_chunk_embeddings. Never touches ediscovery_documents core fields.
  * Idempotent: skips docs that already have chunks; embed step skips chunks that
    already have an embedding (delete-then-insert per batch).
  * canonical=True embeds one doc per distinct native_file_hash (dedupe); the
    remaining duplicates can be backfilled later with canonical=False.

ENTRYPOINTS (enqueue on the "ediscovery_proc" queue):
  embed_text_collection(tenant_id, collection_id, canonical=False)  # coordinator
  process_batch(tenant_id, doc_ids, embed_url=None)                 # per-batch worker
"""
import logging
import os

logger = logging.getLogger(__name__)
TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
BATCH_SIZE = int(os.environ.get("TEXT_EMBED_BATCH", "400"))
# texts per embed_collection -> shim call. Large so the shim fans many
# sub-batches out to RunPod concurrently (saturates autoscaling workers).
EMBED_CALL_BATCH = int(os.environ.get("TEXT_EMBED_CALL_BATCH", "1536"))
# RunPod burst shim: presents the local /embed contract, forwards to RunPod
# serverless, fails over to the V100 on error. See embed_shim.py.
BURST_EMBED_URL = os.environ.get("BURST_EMBED_URL", "http://praesidium-embed-burst-shim:8011")


def resolve_embed_url(collection_id):
    """Pick the embed endpoint for a collection from its embed_backend setting.
    'runpod' -> burst shim; anything else (incl. NULL/unknown/no id) -> local V100
    (returns None so embed_collection uses its own default)."""
    if not collection_id:
        return None
    from modules.ediscovery.jobs.ledger_dag import _connect
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT embed_backend FROM ediscovery_collections "
                    "WHERE id = CAST(%s AS uuid)", (str(collection_id),))
        row = cur.fetchone()
        conn.close()
        if row and (row[0] or "").strip().lower() == "runpod":
            return BURST_EMBED_URL
    except Exception:
        logger.warning("resolve_embed_url failed for %s; using V100 default",
                       collection_id, exc_info=True)
    return None


def _doc_ids(cur, collection_id, canonical):
    """Doc ids in the collection that carry text and have no chunks yet."""
    if canonical:
        cur.execute(
            "SELECT d.id::text FROM ediscovery_documents d "
            "WHERE d.id IN ("
            "    SELECT DISTINCT ON (lower(d2.native_file_hash)) d2.id "
            "    FROM ediscovery_documents d2 "
            "    WHERE d2.collection_id=CAST(%s AS uuid) AND d2.native_file_hash IS NOT NULL "
            "      AND COALESCE(d2.extracted_text,'')<>'' "
            "    ORDER BY lower(d2.native_file_hash), d2.id) "
            "  AND NOT EXISTS (SELECT 1 FROM ediscovery_chunks c WHERE c.document_id=d.id)",
            (str(collection_id),))
    else:
        cur.execute(
            "SELECT d.id::text FROM ediscovery_documents d "
            "WHERE d.collection_id=CAST(%s AS uuid) AND COALESCE(d.extracted_text,'')<>'' "
            "  AND NOT EXISTS (SELECT 1 FROM ediscovery_chunks c WHERE c.document_id=d.id) "
            "ORDER BY d.bates_begin",
            (str(collection_id),))
    return [r[0] for r in cur.fetchall()]


def process_batch(tenant_id, doc_ids, embed_url=None):
    """Segment+chunk a batch of docs from their existing text (no render), then
    embed just those docs via embed_url (V100 default if None). Safe to re-run."""
    from modules.ediscovery.jobs.ledger_dag import _connect, _seg_run_id, _segment_and_chunk
    from modules.ediscovery.jobs.embed_ediscovery_chunks import embed_collection
    tenant = (tenant_id or TENANT_DEFAULT).strip()
    conn = _connect()
    conn.autocommit = False
    cur = conn.cursor()
    runs = {}
    run_id = _seg_run_id(conn, runs, tenant)
    ok = empty = fail = 0
    for doc_id in doc_ids:
        try:
            state, _detail = _segment_and_chunk(cur, tenant, doc_id, run_id)
            conn.commit()
            if state == "ok":
                ok += 1
            else:
                empty += 1
        except Exception as e:
            conn.rollback()
            fail += 1
            logger.warning("text-embed seg/chunk failed %s: %s", str(doc_id)[:8], e)
    conn.close()
    kw = {"docs": list(doc_ids), "batch_size": EMBED_CALL_BATCH}
    if embed_url:
        kw["embed_url"] = embed_url
    res = embed_collection(tenant, **kw)
    out = {"docs": len(doc_ids), "seg_ok": ok, "empty": empty, "fail": fail,
           "embedded": res.get("embedded"), "embed_url": embed_url or "v100-default"}
    logger.info("text-embed batch done: %s", out)
    return out


def embed_text_collection(tenant_id, collection_id, canonical=False,
                          batch_size=BATCH_SIZE, enqueue=True):
    """Coordinator: find docs needing chunks and fan out process_batch jobs over
    the ediscovery_proc queue (or run inline when enqueue=False, for small sets).
    Routes embeds to the collection's embed_backend (V100 or RunPod burst)."""
    from modules.ediscovery.jobs.ledger_dag import _connect
    tenant = (tenant_id or TENANT_DEFAULT).strip()
    embed_url = resolve_embed_url(collection_id)
    conn = _connect()
    cur = conn.cursor()
    ids = _doc_ids(cur, str(collection_id), canonical)
    conn.close()
    logger.info("text-embed %s: %d docs need chunks (canonical=%s, backend=%s)",
                collection_id, len(ids), canonical, "runpod" if embed_url else "v100")
    if not ids:
        return {"docs": 0, "batches": 0}
    if not enqueue:
        results = []
        for i in range(0, len(ids), batch_size):
            results.append(process_batch(tenant, ids[i:i + batch_size], embed_url=embed_url))
        return {"docs": len(ids), "ran_inline": len(results), "backend": "runpod" if embed_url else "v100"}
    from redis import Redis
    from rq import Queue
    q = Queue("ediscovery_proc",
              connection=Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0")))
    fn = "modules.ediscovery.jobs.embed_text_collection.process_batch"
    n = 0
    for i in range(0, len(ids), batch_size):
        q.enqueue(fn, tenant, ids[i:i + batch_size], embed_url,
                  job_timeout=7200, result_ttl=3600)
        n += 1
    logger.info("text-embed %s: enqueued %d batch(es) of <=%d (backend=%s)",
                collection_id, n, batch_size, "runpod" if embed_url else "v100")
    return {"docs": len(ids), "batches": n, "backend": "runpod" if embed_url else "v100"}


def chunk_batch(tenant_id, doc_ids):
    """Seg+chunk only (no embed) — decoupled chunking lane for bulk runs.
    Embedding is handled separately by dedicated feeders streaming to the burst
    shim, so the scarce RunPod workers stay saturated instead of bursty."""
    from modules.ediscovery.jobs.ledger_dag import _connect, _seg_run_id, _segment_and_chunk
    tenant = (tenant_id or TENANT_DEFAULT).strip()
    conn = _connect()
    conn.autocommit = False
    cur = conn.cursor()
    runs = {}
    run_id = _seg_run_id(conn, runs, tenant)
    ok = empty = fail = 0
    for doc_id in doc_ids:
        try:
            state, _d = _segment_and_chunk(cur, tenant, doc_id, run_id)
            conn.commit()
            if state == "ok":
                ok += 1
            else:
                empty += 1
        except Exception as e:
            conn.rollback()
            fail += 1
            logger.warning("chunk_batch seg/chunk failed %s: %s", str(doc_id)[:8], e)
    conn.close()
    return {"docs": len(doc_ids), "seg_ok": ok, "empty": empty, "fail": fail, "mode": "chunk_only"}


def chunk_text_collection(tenant_id, collection_id, canonical=False, batch_size=BATCH_SIZE):
    """Enqueue chunk_batch jobs (seg+chunk only) for docs that still need chunks."""
    from modules.ediscovery.jobs.ledger_dag import _connect
    tenant = (tenant_id or TENANT_DEFAULT).strip()
    conn = _connect()
    cur = conn.cursor()
    ids = _doc_ids(cur, str(collection_id), canonical)
    conn.close()
    if not ids:
        return {"docs": 0, "batches": 0}
    from redis import Redis
    from rq import Queue
    q = Queue("ediscovery_proc",
              connection=Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0")))
    fn = "modules.ediscovery.jobs.embed_text_collection.chunk_batch"
    n = 0
    for i in range(0, len(ids), batch_size):
        q.enqueue(fn, tenant, ids[i:i + batch_size], job_timeout=7200, result_ttl=3600)
        n += 1
    logger.info("chunk-only %s: enqueued %d batch(es) of <=%d", collection_id, n, batch_size)
    return {"docs": len(ids), "batches": n}
