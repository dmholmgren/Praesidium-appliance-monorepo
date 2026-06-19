"""embed_qa.py -- embed transcript_qa_units via praesidium-embed (ModernBERT-768)
into transcript_qa_embeddings, and BM25-index the Q&A text into Elasticsearch.

The depo DAG's narrow batched GPU lane (qa_unit grain). Mirrors
modules/ediscovery/jobs/embed_ediscovery_chunks.py: same /embed contract, same
vector(768) space, delete-then-insert idempotency, big batches to saturate the
V100. One row per chunk (long answers split); short exchanges are a single chunk.

  POST {EMBED_URL}/embed {"texts":[...],"input_type":"document"}
    -> {"model","revision","dim":768,"embeddings":[[...768],...]}

ES is best-effort: a down/erroring ES never fails the durable pgvector write
(semantic search U3/U10 works on pgvector alone; BM25 augments it). The Q&A index
is created lazily on first use.

CLI:
  python -m modules.depositions.jobs.embed_qa --tenant T [--transcript UUID]
       [--batch-size 64] [--force] [--build-index]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

EMBED_URL_DEFAULT = os.environ.get("EMBED_URL", "http://praesidium-embed:8000")
ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
_EMBED_BURST = os.environ.get("EMBED_BURST", "0") == "1"
BATCH_DEFAULT = int(os.environ.get("EMBED_BATCH", "256" if _EMBED_BURST else "64"))
QA_INDEX = "transcript_qa"
CHUNK_CHARS = 1500          # split a Q&A exchange longer than this into chunks
EMBED_MAX_CHARS = 8000      # per-text cap sent to the embedder


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _embed(base, texts):
    payload = json.dumps({"texts": texts, "input_type": "document"}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/embed", data=payload,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read().decode())
    return data["embeddings"], data.get("model", "modernbert-768"), data.get("revision", "")


def _vec_literal(v):
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


def _qa_text(question, answer):
    q = (question or "").strip()
    a = (answer or "").strip()
    if q and a:
        return "Q: %s\nA: %s" % (q, a)
    return a or q


def _chunks(text):
    """Split on sentence-ish boundaries into <=CHUNK_CHARS windows (row-per-chunk
    for long answers; short exchanges return a single chunk)."""
    text = text or ""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, buf = [], ""
    for token in text.replace("\n", " \n").split(" "):
        if len(buf) + len(token) + 1 > CHUNK_CHARS and buf:
            out.append(buf.strip())
            buf = ""
        buf += token + " "
    if buf.strip():
        out.append(buf.strip())
    return out or [""]


# --- elasticsearch (best-effort, sync bulk) ---------------------------------

def _es_request(method, path, body=None, timeout=20):
    url = ELASTICSEARCH_URL.rstrip("/") + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _ensure_qa_index():
    try:
        _es_request("HEAD", "/" + QA_INDEX)
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning("es head %s: %s", QA_INDEX, e)
            return False
    except Exception as e:
        logger.warning("es unreachable (%s) -- skipping BM25 index", type(e).__name__)
        return False
    mapping = {
        "mappings": {"properties": {
            "tenant_id": {"type": "keyword"},
            "matter_id": {"type": "keyword"},
            "session_id": {"type": "keyword"},
            "transcript_id": {"type": "keyword"},
            "qa_unit_id": {"type": "keyword"},
            "seq": {"type": "integer"},
            "page": {"type": "integer"},
            "line": {"type": "integer"},
            "examiner": {"type": "keyword"},
            "witness": {"type": "keyword"},
            "is_colloquy": {"type": "boolean"},
            "question_text": {"type": "text", "analyzer": "english"},
            "answer_text": {"type": "text", "analyzer": "english"},
            "qa_text": {"type": "text", "analyzer": "english"},
        }},
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
    }
    try:
        _es_request("PUT", "/" + QA_INDEX, mapping)
        logger.info("es: created index %s", QA_INDEX)
        return True
    except Exception as e:
        logger.warning("es create %s failed: %s", QA_INDEX, e)
        return False


def _es_bulk_index(tenant, units):
    """units: list of dicts with the qa fields. Best-effort bulk upsert."""
    if not units or not _ensure_qa_index():
        return 0
    lines = []
    for u in units:
        _id = "%s:%s" % (tenant, u["qa_unit_id"])
        lines.append(json.dumps({"index": {"_index": QA_INDEX, "_id": _id}}))
        lines.append(json.dumps({"tenant_id": tenant, **u}))
    body = ("\n".join(lines) + "\n").encode()
    try:
        res = _es_request("POST", "/_bulk", body)
        if res.get("errors"):
            logger.warning("es bulk reported item errors for %s docs", len(units))
        return len(units)
    except Exception as e:
        logger.warning("es bulk failed (%s) -- pgvector write stands", type(e).__name__)
        return 0


# --- embed ------------------------------------------------------------------

def embed_units(tenant, qa_unit_ids=None, transcript_id=None, batch_size=BATCH_DEFAULT,
                embed_url=EMBED_URL_DEFAULT, force=False) -> dict:
    """Embed the given qa_units (or a whole transcript). Returns counts."""
    ten = tenant.strip()
    s = {"units": 0, "chunks": 0, "embedded": 0, "batches": 0, "es": 0}
    conn = _connect()
    try:
        cur = conn.cursor()
        where = ["TRIM(q.tenant_id)=%(t)s"]
        params = {"t": ten}
        if qa_unit_ids:
            where.append("q.id = ANY(%(ids)s::uuid[])")
            params["ids"] = list(qa_unit_ids)
        if transcript_id:
            where.append("q.transcript_id = %(tr)s::uuid")
            params["tr"] = str(transcript_id)
        if not force:
            where.append("NOT EXISTS (SELECT 1 FROM transcript_qa_embeddings e "
                         "WHERE e.qa_unit_id=q.id)")
        cur.execute(
            "SELECT q.id::text, q.transcript_id::text, q.session_id, q.seq, "
            "       q.examiner, q.witness, q.q_start_page, q.q_start_line, "
            "       q.is_colloquy, q.question_text, q.answer_text, t.matter_id::text "
            "FROM transcript_qa_units q "
            "JOIN deposition_transcripts t ON t.id=q.transcript_id "
            "WHERE " + " AND ".join(where) + " ORDER BY q.transcript_id, q.seq",
            params)
        rows = cur.fetchall()
        s["units"] = len(rows)
        if not rows:
            return s

        # build the flat chunk worklist (qa_unit -> 1..n chunks)
        work = []   # (qa_unit_id, transcript_id, chunk_number, text, row)
        es_docs = []
        for r in rows:
            (qid, trid, sid, seq, examiner, witness, page, line,
             is_coll, qtext, atext, matter) = r
            full = _qa_text(qtext, atext)
            for ci, ct in enumerate(_chunks(full)):
                work.append((qid, trid, ci, ct[:EMBED_MAX_CHARS]))
            es_docs.append({
                "qa_unit_id": qid, "matter_id": matter, "session_id": sid,
                "transcript_id": trid, "seq": seq, "page": page, "line": line,
                "examiner": examiner, "witness": witness, "is_colloquy": is_coll,
                "question_text": qtext, "answer_text": atext, "qa_text": full})
        s["chunks"] = len(work)

        for i in range(0, len(work), batch_size):
            batch = work[i:i + batch_size]
            texts = [w[3] or "" for w in batch]
            t0 = time.time()
            vecs, model, rev = _embed(embed_url, texts)
            if len(vecs) != len(batch):
                raise RuntimeError("embed count mismatch: %d != %d" % (len(vecs), len(batch)))
            model_id = (model + ("@" + rev if rev else ""))[:64]
            # idempotent: clear prior vectors for these (qa_unit, model) then insert
            qids = list({w[0] for w in batch})
            cur.execute("DELETE FROM transcript_qa_embeddings "
                        "WHERE qa_unit_id = ANY(%s::uuid[]) AND embedding_model=%s",
                        (qids, model_id))
            for (qid, trid, ci, ct), v in zip(batch, vecs):
                cur.execute(
                    "INSERT INTO transcript_qa_embeddings "
                    "(qa_unit_id, transcript_id, tenant_id, embedding_model, "
                    " chunk_number, chunk_text, embedding, embedded_at) "
                    "VALUES (CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, %s, %s, "
                    "        CAST(%s AS vector), now()) "
                    "ON CONFLICT (qa_unit_id, embedding_model, chunk_number) "
                    "DO UPDATE SET chunk_text=EXCLUDED.chunk_text, "
                    "  embedding=EXCLUDED.embedding, embedded_at=now()",
                    (qid, trid, ten, model_id, ci, ct, _vec_literal(v)))
            conn.commit()
            s["embedded"] += len(batch)
            s["batches"] += 1
            logger.info("  embed batch %d: +%d chunk(s) (model=%s)", s["batches"],
                        len(batch), model_id)

        s["es"] = _es_bulk_index(ten, es_docs)
        return s
    finally:
        conn.close()


def build_index() -> dict:
    """Deferred HNSW build (run after a bulk embed). CONCURRENTLY so it never
    locks the table against the drain."""
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = True   # CREATE INDEX CONCURRENTLY cannot run in a txn
    try:
        cur = conn.cursor()
        cur.execute("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_transcript_qa_embedding "
                    "ON transcript_qa_embeddings USING hnsw (embedding vector_cosine_ops)")
        logger.info("built HNSW ix_transcript_qa_embedding")
        return {"built": "ix_transcript_qa_embedding"}
    finally:
        conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", "986c0fee-1390-43bb-ad28-8cd1db6de53f"))
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--batch-size", type=int, default=BATCH_DEFAULT)
    ap.add_argument("--embed-url", default=EMBED_URL_DEFAULT)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--build-index", action="store_true")
    args = ap.parse_args()
    if args.build_index:
        out = build_index()
    else:
        out = embed_units(args.tenant, transcript_id=args.transcript,
                          batch_size=args.batch_size, embed_url=args.embed_url,
                          force=args.force)
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
