"""
ledger_dag.py -- ledger-as-queue per-document ingestion DAG (v18.4 decision).

The stage ledger (ediscovery_stage_status) IS the queue: Postgres holds the
work, RQ only spawns long-lived drain workers. Replaces the serial
whole-collection subprocess orchestrator for everything after enrich.

Lanes (new stage names -- no conflation with legacy geometry/render/ocr rows):

  spine      one claimed per-doc unit: route -> geometry (render inline if
             office/email) -> segment -> chunk -> seed embed_lane.
             Scanned/image docs are handed to ocr_lane and the spine row
             closes as done {routed: ocr}.
  ocr_lane   capped lane: OCR-to-PDF -> geometry -> segment -> chunk ->
             seed embed_lane. Tesseract today; engine swappable after the
             eval-harness bake-off.
  embed_lane batched GPU lane: claims docs in bulk, embeds their chunks in
             large batches against praesidium-embed (saturate the V100).

Mechanics: SKIP LOCKED batch claims; the claim txn is tiny (flip to
processing, commit, THEN heavy work -- never hold a row lock across
geometry/OCR). Per-doc txn commit; poison docs fail alone. input_hash
short-circuit skips unchanged docs on resume. Consecutive-failure backoff
protects against a down dependency (e.g. embed service). Stale-claim reaper
re-pends abandoned work; attempt cap dead-letters it as failed.

Defensibility: canonical text is written only by the deterministic
extractor (sect. 0 invariant preserved); the ledger row carries worker_id,
input_hash, attempt, duration_ms -- the reproducibility record.

CLI (inside praesidium-web, cwd /app):
  python -m modules.ediscovery.jobs.ledger_dag --seed   --collection <id> [--reset]
  python -m modules.ediscovery.jobs.ledger_dag --drain  spine|ocr_lane|embed_lane
        [--claim N] [--idle SECS] [--max N] [--force]
  python -m modules.ediscovery.jobs.ledger_dag --status [--collection <id>]
  python -m modules.ediscovery.jobs.ledger_dag --reap   [--stale SECS]
  python -m modules.ediscovery.jobs.ledger_dag --run    --collection <id>
        [--spine-workers N] [--ocr-workers N] [--embed-workers N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

STAGE_SPINE = "spine"
STAGE_OCR = "ocr_lane"
STAGE_EMBED = "embed_lane"
LANES = (STAGE_SPINE, STAGE_OCR, STAGE_EMBED)

MAX_ATTEMPTS = 4
POLL_SECS = 3
DEFAULT_CLAIM = {STAGE_SPINE: 8, STAGE_OCR: 2, STAGE_EMBED: 24}
EMBED_BATCH = 256          # texts per call to praesidium-embed (GPU saturation)
TENANT_DEFAULT = "986c0fee-1390-43bb-ad28-8cd1db6de53f"


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


# ---------------------------------------------------------------------------
# queue primitives
# ---------------------------------------------------------------------------

def seed(tenant_id, collection_id, priority=100, reset=False) -> dict:
    """Fan-out: one spine row per preservable doc. reset=True re-pends done
    rows too and clears downstream lane rows (full reingest of a collection);
    default re-pends only failed rows (safe resume)."""
    tenant = tenant_id.strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        if reset:
            cur.execute(
                "DELETE FROM ediscovery_stage_status WHERE TRIM(tenant_id)=%s "
                "AND collection_id=CAST(%s AS uuid) AND stage IN (%s, %s)",
                (tenant, str(collection_id), STAGE_OCR, STAGE_EMBED))
        guard = "" if reset else " WHERE ediscovery_stage_status.state = 'failed'"
        cur.execute(
            "INSERT INTO ediscovery_stage_status "
            "  (tenant_id, document_id, collection_id, stage, state, priority, updated_at) "
            "SELECT d.tenant_id, d.id, d.collection_id, %s, 'pending', %s, now() "
            "FROM ediscovery_documents d "
            "WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid) "
            "  AND d.native_path IS NOT NULL "
            "ON CONFLICT (tenant_id, document_id, stage) DO UPDATE SET "
            "  state='pending', priority=EXCLUDED.priority, error_class=NULL, "
            "  error_message=NULL, updated_at=now()" + guard,
            (STAGE_SPINE, priority, tenant, str(collection_id)))
        n = cur.rowcount
        conn.commit()
        logger.info("seed %s: %d spine row(s) pending (reset=%s)", collection_id, n, reset)
        return {"seeded": n, "reset": reset}
    finally:
        conn.close()


def _claim(conn, stage, worker_id, n):
    """Tiny claim txn: flip up to n pending rows to processing and commit.
    Heavy work happens AFTER this commit -- locks are never held across it."""
    cur = conn.cursor()
    cur.execute(
        "WITH c AS ("
        "  SELECT id FROM ediscovery_stage_status"
        "  WHERE stage=%s AND state='pending'"
        "  ORDER BY priority, updated_at"
        "  LIMIT %s FOR UPDATE SKIP LOCKED) "
        "UPDATE ediscovery_stage_status t "
        "SET state='running', worker_id=%s, started_at=now(), "
        "    finished_at=NULL, updated_at=now(), attempt=t.attempt+1 "
        "FROM c WHERE t.id=c.id "
        "RETURNING t.id::text, TRIM(t.tenant_id), t.document_id::text, "
        "          t.collection_id::text, t.attempt, t.input_hash",
        (stage, n, worker_id))
    rows = cur.fetchall()
    conn.commit()
    return rows


def _finish(conn, row_id, state, duration_ms=None, error_class=None,
            error_message=None, error_detail=None, input_hash=None):
    cur = conn.cursor()
    cur.execute(
        "UPDATE ediscovery_stage_status SET state=%s, finished_at=now(), "
        "duration_ms=%s, error_class=%s, error_message=%s, "
        "error_detail=%s::jsonb, input_hash=COALESCE(%s, input_hash), "
        "updated_at=now() WHERE id=CAST(%s AS uuid)",
        (state, duration_ms, error_class,
         (error_message[:4000] if error_message else None),
         (json.dumps(error_detail) if error_detail else None),
         input_hash, row_id))
    conn.commit()


def _seed_next(cur, tenant, doc_id, collection_id, stage, priority=100):
    cur.execute(
        "INSERT INTO ediscovery_stage_status "
        "  (tenant_id, document_id, collection_id, stage, state, priority, updated_at) "
        "VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, 'pending', %s, now()) "
        "ON CONFLICT (tenant_id, document_id, stage) DO UPDATE SET "
        "  state='pending', priority=EXCLUDED.priority, error_class=NULL, "
        "  error_message=NULL, updated_at=now()",
        (tenant, str(doc_id), str(collection_id), stage, priority))


def reap(stale_secs=1800, max_attempts=MAX_ATTEMPTS) -> dict:
    """Re-pend claims abandoned by a dead worker; dead-letter at attempt cap."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE ediscovery_stage_status SET "
            "  state = CASE WHEN attempt >= %s THEN 'failed' ELSE 'pending' END, "
            "  error_class = CASE WHEN attempt >= %s THEN 'StaleClaim' ELSE error_class END, "
            "  error_message = CASE WHEN attempt >= %s THEN 'reaper: attempt cap reached' "
            "                       ELSE error_message END, "
            "  updated_at = now() "
            "WHERE state='running' AND stage = ANY(%s) "
            "  AND started_at < now() - make_interval(secs => %s)",
            (max_attempts, max_attempts, max_attempts, list(LANES), stale_secs))
        n = cur.rowcount
        conn.commit()
        if n:
            logger.info("reaper: recovered %d stale claim(s)", n)
        return {"reaped": n}
    finally:
        conn.close()


def status(collection_id=None) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        where, params = "stage = ANY(%s)", [list(LANES)]
        if collection_id:
            where += " AND collection_id=CAST(%s AS uuid)"
            params.append(str(collection_id))
        cur.execute(
            "SELECT stage, state, count(*), "
            "       COALESCE(avg(duration_ms) FILTER (WHERE state='done'), 0)::int "
            "FROM ediscovery_stage_status WHERE " + where +
            " GROUP BY stage, state ORDER BY stage, state", params)
        return [{"stage": r[0], "state": r[1], "n": r[2], "avg_ms": r[3]}
                for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# per-doc units
# ---------------------------------------------------------------------------

def _doc_info(cur, tenant, doc_id):
    cur.execute(
        "SELECT d.doc_type, d.native_path, d.file_hash, c.storage_path "
        "FROM ediscovery_documents d "
        "JOIN ediscovery_collections c ON c.id = d.collection_id "
        "WHERE TRIM(d.tenant_id)=%s AND d.id=CAST(%s AS uuid)",
        (tenant, str(doc_id)))
    return cur.fetchone()


def _has_live_chunks(cur, doc_id):
    cur.execute("SELECT 1 FROM ediscovery_chunks WHERE document_id=CAST(%s AS uuid) LIMIT 1",
                (str(doc_id),))
    return cur.fetchone() is not None


def _seg_run_id(conn, runs, tenant, corpus="ediscovery"):
    """Lazily create one extraction_runs row per tenant per drain session."""
    if tenant in runs:
        return runs[tenant]
    from jobs.canonical_segmenter import SEGMENTER_GEOM
    rid = str(uuid.uuid4())
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO extraction_runs (id, tenant_id, run_type, source_type, "
        "extraction_model, status, started_at) "
        "VALUES (%s::uuid, %s, 'canonical_segment', %s, %s, 'running', NOW())",
        (rid, tenant, corpus, SEGMENTER_GEOM))
    conn.commit()
    runs[tenant] = rid
    return rid


def _segment_and_chunk(cur, tenant, doc_id, run_id):
    """Shared tail of spine/ocr units. Returns ('ok'|'empty', detail).
    Chunker is ALWAYS forced here: we just superseded the live sections, and
    the chunker's canonical-fingerprint cache would otherwise return chunks
    pointing at superseded section ids."""
    from jobs.canonical_segmenter import process_doc as seg_process
    from jobs.spine_chunker import process_doc as chunk_process
    seg = seg_process(cur, "ediscovery", tenant, doc_id, run_id, False)
    if seg["status"] != "written":
        if seg["status"] in ("empty_canonical", "no_segments"):
            return "empty", {"segment": seg["status"]}
        raise RuntimeError("segment failed: %s" % seg["status"])
    ch = chunk_process(cur, "ediscovery", tenant, doc_id, False, True)
    if ch["status"] != "written":
        raise RuntimeError("chunk failed: %s" % ch["status"])
    return "ok", {"segments": seg.get("segments", 0), "chunks": ch.get("chunks", 0)}


def _spine_unit(conn, runs, row, force=False):
    """route -> geometry (render inline) -> segment -> chunk -> seed embed."""
    from modules.ediscovery.jobs.geometry_intake import (
        _route, _persist_geometry, _render_to_pdf, _mark_pending,
        NATIVE_RENDITION, RENDER_RENDITION, SHEET_EXTS)
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry

    row_id, tenant, doc_id, coll_id, attempt, prev_hash = row
    t0 = time.time()
    cur = conn.cursor()
    try:
        if attempt > MAX_ATTEMPTS:
            _finish(conn, row_id, "failed", error_class="MaxAttempts",
                    error_message="attempt cap reached")
            return "failed"
        info = _doc_info(cur, tenant, doc_id)
        if not info or not info[1]:
            raise FileNotFoundError("doc/native_path not found")
        doc_type, native_path, file_hash, storage_path = info

        # idempotency short-circuit: same input, work already done
        if (not force and prev_hash and file_hash and prev_hash == file_hash
                and _has_live_chunks(cur, doc_id)):
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail={"skipped": "unchanged"}, input_hash=file_hash)
            return "done"

        ext = Path(native_path).suffix.lower()
        lane = "cells" if ext in SHEET_EXTS else _route(native_path)
        abs_path = Path(storage_path) / native_path
        if lane != "ocr" and not abs_path.exists():
            raise FileNotFoundError(str(abs_path))

        if lane == "cells":
            from modules.ediscovery.services.cell_extraction import (
                extract_cells_with_geometry, verify_cell_offsets)
            from modules.ediscovery.services import geometry_io
            res = extract_cells_with_geometry(str(abs_path))
            if res is None or not (res.canonical_text or "").strip():
                _mark_pending(cur, doc_id, "geometry_empty")
                conn.commit()
                _finish(conn, row_id, "done",
                        duration_ms=int((time.time() - t0) * 1000),
                        error_detail={"empty": True, "lane": "cells"},
                        input_hash=file_hash)
                return "empty"
            ok, bad = verify_cell_offsets(res)
            if bad:
                raise ValueError(
                    "cell offset self-check failed: %d bad cells" % bad)
            geometry_io.persist_cells(conn, tenant, "ediscovery", doc_id, res)
            cur.execute(
                "UPDATE ediscovery_documents SET extracted_text=%s, text_source='extract', "
                "processing_status='geometry_done' WHERE id=CAST(%s AS uuid)",
                (res.canonical_text, str(doc_id)))
            run_id = _seg_run_id(conn, runs, tenant)
            state, detail = _segment_and_chunk(cur, tenant, doc_id, run_id)
            detail = dict(detail or {})
            detail.update({"lane": "cells", "sheets": res.sheet_count,
                           "cells": res.cell_count})
            if state == "ok":
                _seed_next(cur, tenant, doc_id, coll_id, STAGE_EMBED)
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail=detail, input_hash=file_hash)
            return "done"

        if lane == "ocr":
            _mark_pending(cur, doc_id, "ocr_pending")
            _seed_next(cur, tenant, doc_id, coll_id, STAGE_OCR)
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail={"routed": "ocr"}, input_hash=file_hash)
            return "routed_ocr"

        if lane == "render":
            out_pdf = os.path.join(storage_path, "working", "render", "%s.pdf" % doc_id)
            made = _render_to_pdf(str(abs_path), doc_type, out_pdf)
            if not made or not os.path.exists(out_pdf):
                raise RuntimeError("render produced no pdf")
            result = extract_pdf_with_geometry(out_pdf)
            rendition = RENDER_RENDITION
        else:  # born-digital pdf
            result = extract_pdf_with_geometry(str(abs_path))
            if (result is None or not getattr(result, "has_text_layer", False)
                    or not (result.canonical_text or "").strip()):
                # scanned pdf -> OCR residue lane
                _mark_pending(cur, doc_id, "ocr_pending")
                _seed_next(cur, tenant, doc_id, coll_id, STAGE_OCR)
                conn.commit()
                _finish(conn, row_id, "done",
                        duration_ms=int((time.time() - t0) * 1000),
                        error_detail={"routed": "ocr", "reason": "no_text_layer"},
                        input_hash=file_hash)
                return "routed_ocr"
            rendition = NATIVE_RENDITION

        if result is None or not (result.canonical_text or "").strip():
            _mark_pending(cur, doc_id, "geometry_empty")
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail={"empty": True}, input_hash=file_hash)
            return "empty"

        _persist_geometry(cur, tenant, doc_id, rendition, result,
                          corpus="ediscovery", write_canonical=True)
        run_id = _seg_run_id(conn, runs, tenant)
        state, detail = _segment_and_chunk(cur, tenant, doc_id, run_id)
        if state == "ok":
            _seed_next(cur, tenant, doc_id, coll_id, STAGE_EMBED)
        conn.commit()
        _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                error_detail=detail, input_hash=file_hash)
        return "done"
    except Exception as e:
        conn.rollback()
        _finish(conn, row_id, "failed", duration_ms=int((time.time() - t0) * 1000),
                error_class=type(e).__name__, error_message=str(e))
        logger.exception("spine unit %s failed", doc_id)
        return "failed"


def _ocr_unit(conn, runs, row, force=False):
    """OCR residue: OCR-to-PDF -> geometry -> segment -> chunk -> seed embed."""
    from modules.ediscovery.jobs.geometry_intake import (
        _persist_geometry, _ocr_to_pdf, _mark_pending, OCR_RENDITION)
    from modules.ediscovery.services.geometry_extraction import extract_pdf_with_geometry

    row_id, tenant, doc_id, coll_id, attempt, prev_hash = row
    t0 = time.time()
    cur = conn.cursor()
    try:
        if attempt > MAX_ATTEMPTS:
            _finish(conn, row_id, "failed", error_class="MaxAttempts",
                    error_message="attempt cap reached")
            return "failed"
        info = _doc_info(cur, tenant, doc_id)
        if not info or not info[1]:
            raise FileNotFoundError("doc/native_path not found")
        doc_type, native_path, file_hash, storage_path = info
        if (not force and prev_hash and file_hash and prev_hash == file_hash
                and _has_live_chunks(cur, doc_id)):
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail={"skipped": "unchanged"}, input_hash=file_hash)
            return "done"
        abs_path = Path(storage_path) / native_path
        if not abs_path.exists():
            raise FileNotFoundError(str(abs_path))
        out_pdf = os.path.join(storage_path, "working", "ocr", "%s.pdf" % doc_id)
        made = _ocr_to_pdf(str(abs_path), doc_type, out_pdf)
        if not made or not os.path.exists(out_pdf):
            raise RuntimeError("ocr produced no pdf")
        result = extract_pdf_with_geometry(out_pdf)
        if result is None or not (result.canonical_text or "").strip():
            _mark_pending(cur, doc_id, "geometry_empty")
            conn.commit()
            _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                    error_detail={"empty": True}, input_hash=file_hash)
            return "empty"
        _persist_geometry(cur, tenant, doc_id, OCR_RENDITION, result,
                          corpus="ediscovery", write_canonical=True)
        run_id = _seg_run_id(conn, runs, tenant)
        state, detail = _segment_and_chunk(cur, tenant, doc_id, run_id)
        if state == "ok":
            _seed_next(cur, tenant, doc_id, coll_id, STAGE_EMBED)
        conn.commit()
        _finish(conn, row_id, "done", duration_ms=int((time.time() - t0) * 1000),
                error_detail=detail, input_hash=file_hash)
        return "done"
    except Exception as e:
        conn.rollback()
        _finish(conn, row_id, "failed", duration_ms=int((time.time() - t0) * 1000),
                error_class=type(e).__name__, error_message=str(e))
        logger.exception("ocr unit %s failed", doc_id)
        return "failed"


def _embed_batch(conn, rows):
    """Batched GPU lane: one big embed call per claimed batch, per tenant."""
    from modules.ediscovery.jobs.embed_ediscovery_chunks import embed_collection
    t0 = time.time()
    by_tenant = {}
    for r in rows:
        by_tenant.setdefault(r[1], []).append(r)
    out = {"done": 0, "failed": 0}
    for tenant, trows in by_tenant.items():
        ids = [r[2] for r in trows]
        try:
            from modules.ediscovery.jobs.embed_text_collection import resolve_embed_url
            _ec = conn.cursor()
            _ec.execute("SELECT id::text, collection_id::text FROM ediscovery_documents "
                        "WHERE id = ANY(%s::uuid[])", (ids,))
            _coll = dict(_ec.fetchall())
            _by = {}
            for _d in ids:
                _by.setdefault(_coll.get(_d), []).append(_d)
            for _cid, _dids in _by.items():
                _kw = {"docs": _dids, "batch_size": EMBED_BATCH}
                _url = resolve_embed_url(_cid)
                if _url:
                    _kw["embed_url"] = _url
                embed_collection(tenant, **_kw)
            dur = int((time.time() - t0) * 1000 / max(len(trows), 1))
            for r in trows:
                _finish(conn, r[0], "done", duration_ms=dur)
            out["done"] += len(trows)
        except Exception as e:
            for r in trows:
                _finish(conn, r[0], "failed", error_class=type(e).__name__,
                        error_message=str(e))
            out["failed"] += len(trows)
            logger.exception("embed batch failed (%d docs)", len(trows))
    return out


# ---------------------------------------------------------------------------
# drain worker
# ---------------------------------------------------------------------------

def drain(lane, claim=0, idle_secs=90, max_units=0, force=False) -> dict:
    """Long-lived drain worker for one lane. Exits after idle_secs with no
    pending work (RQ job completes; re-enqueued per collection run)."""
    assert lane in LANES, "unknown lane %s" % lane
    n_claim = claim or DEFAULT_CLAIM[lane]
    worker_id = "%s:%s:%d" % (os.environ.get("HOSTNAME", "drain"), lane, os.getpid())
    conn = _connect()
    runs: dict = {}
    s = {"lane": lane, "worker": worker_id, "claimed": 0, "done": 0,
         "failed": 0, "routed_ocr": 0, "empty": 0}
    idle_since = None
    streak = 0
    logger.info("drain %s starting (claim=%d idle=%ds max=%s force=%s)",
                lane, n_claim, idle_secs, max_units or "inf", force)
    try:
        while True:
            rows = _claim(conn, lane, worker_id, n_claim)
            if not rows:
                if idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > idle_secs:
                    break
                time.sleep(POLL_SECS)
                continue
            idle_since = None
            s["claimed"] += len(rows)

            if lane == STAGE_EMBED:
                r = _embed_batch(conn, rows)
                s["done"] += r["done"]
                s["failed"] += r["failed"]
                streak = 0 if r["done"] else streak + 1
            else:
                unit = _spine_unit if lane == STAGE_SPINE else _ocr_unit
                for row in rows:
                    res = unit(conn, runs, row, force=force)
                    key = res if res in ("done", "failed", "routed_ocr", "empty") else "done"
                    s[key] += 1
                    if res == "failed":
                        streak += 1
                    else:
                        streak = 0
            if streak >= 3:  # dependency likely down -- back off, don't burn the queue
                pause = min(2 ** streak, 60)
                logger.warning("drain %s: %d consecutive failures, backing off %ds",
                               lane, streak, pause)
                time.sleep(pause)
            if max_units and s["claimed"] >= max_units:
                break
    finally:
        cur = conn.cursor()
        for rid in runs.values():
            try:
                cur.execute("UPDATE extraction_runs SET status='completed' "
                            "WHERE id=CAST(%s AS uuid)", (rid,))
                conn.commit()
            except Exception:
                conn.rollback()
        conn.close()
    logger.info("drain %s finished: %s", lane, json.dumps(s))
    return s


# ---------------------------------------------------------------------------
# collection coordinator (v2 pipeline entrypoint)
# ---------------------------------------------------------------------------

def run_collection(tenant_id, collection_id, user_id=None,
                   spine_workers=8, ocr_workers=2, embed_workers=1) -> dict:
    """v2 orchestrator: collection-level stages stay serial (preserve,
    custodian, enrich); everything after fans out through the ledger DAG.
    The coordinating worker seeds, enqueues sibling drains over RQ, drains
    spine itself, then monitors the ledger to completion."""
    import subprocess
    spine_workers = int(os.environ.get("SPINE_WORKERS", spine_workers))
    ocr_workers = int(os.environ.get("OCR_WORKERS", ocr_workers))
    embed_workers = int(os.environ.get("EMBED_WORKERS", embed_workers))
    from modules.ediscovery.jobs.pipeline_orchestrator import (
        _db, _log, _status, _storage_path, _extract_archives, PY, APP)

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
        logger.info("[dag %s] %s", cid[:8], m)

    try:
        storage = _storage_path(cur, tenant, cid)
        _status(cur, tenant, cid, "processing")
        prog("Ingestion v2 started (ledger DAG)")
        nz = _extract_archives(storage)
        if nz:
            prog("Extracted %d archive(s)" % nz)

        C, T = ["--collection", cid], ["--tenant", tenant]
        for name, cmd in [
            ("preserve",  [PY, "modules/ediscovery/jobs/preserve_collection.py"] + T + C),
            ("custodian", [PY, "-m", "modules.ediscovery.jobs.custodian_registry"] + T + C),
            ("enrich",    [PY, "modules/ediscovery/jobs/enrich_collection.py"] + T + C),
        ]:
            prog("stage %s: starting" % name)
            r = subprocess.run(cmd, cwd=APP, capture_output=True, text=True)
            if r.returncode != 0:
                tail = (r.stderr or "").strip().splitlines()
                prog("stage %s FAILED rc=%d: %s" % (name, r.returncode,
                     (tail[-1] if tail else "")[:400]), "error")
                _status(cur, tenant, cid, "failed")
                raise RuntimeError("stage %s failed" % name)
            prog("stage %s: done" % name)

        n = seed(tenant, cid)["seeded"]
        prog("DAG seeded: %d spine unit(s)" % n)

        # sibling drains over RQ (lane caps = number of drain jobs)
        try:
            from redis import Redis
            from rq import Queue
            rconn = Redis.from_url(os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0"))
            fn = "modules.ediscovery.jobs.ledger_dag.drain"
            qp = Queue("ediscovery_proc", connection=rconn)
            qo = Queue("ocr", connection=rconn)
            for _ in range(max(spine_workers - 1, 0)):
                qp.enqueue(fn, STAGE_SPINE, job_timeout=86400, result_ttl=3600)
            for _ in range(ocr_workers):
                qo.enqueue(fn, STAGE_OCR, job_timeout=86400, result_ttl=3600)
            for _ in range(embed_workers):
                qp.enqueue(fn, STAGE_EMBED, job_timeout=86400, result_ttl=3600)
            prog("drains enqueued: spine=%d ocr=%d embed=%d"
                 % (spine_workers, ocr_workers, embed_workers))
        except Exception as e:
            prog("RQ enqueue failed (%s) -- coordinator drains alone" % e, "error")

        drain(STAGE_SPINE, idle_secs=120)  # coordinator pulls its weight

        # monitor ledger to completion; rescue lanes whose drains idled
        # out while an upstream lane was still producing into them
        # (orphaned pending units -- observed on b4a23cda 2026-06-10)
        deadline = time.time() + 86400
        last_rescue = {}
        last_report = 0.0
        last_snapshot = None
        while time.time() < deadline:
            reap()
            cur.execute(
                "SELECT stage, state, count(*) FROM ediscovery_stage_status "
                "WHERE collection_id=%s::uuid AND stage = ANY(%s) "
                "  AND state IN ('pending','running') "
                "GROUP BY stage, state", (cid, list(LANES)))
            counts = {}
            for _stage, _state, _n in cur.fetchall():
                counts.setdefault(_stage, {"pending": 0, "running": 0})
                counts[_stage][_state] = _n
            if not counts:
                break

            now = time.time()
            snapshot = sorted((k, v["pending"], v["running"])
                              for k, v in counts.items())
            if now - last_report >= 60 and snapshot != last_snapshot:
                prog("DAG progress: " + "; ".join(
                    "%s pending=%d running=%d" % (k, p, r)
                    for k, p, r in snapshot))
                last_report, last_snapshot = now, snapshot

            for _lane, v in counts.items():
                if v["pending"] > 0 and v["running"] == 0 \
                        and now - last_rescue.get(_lane, 0) >= 60:
                    last_rescue[_lane] = now
                    try:
                        _q = qo if _lane == STAGE_OCR else qp
                        _q.enqueue(fn, _lane, job_timeout=86400,
                                   result_ttl=3600)
                        prog("lane %s: %d pending with no active drain -- "
                             "re-enqueued drain" % (_lane, v["pending"]),
                             "warning")
                    except Exception as e:
                        prog("lane %s rescue enqueue failed: %s"
                             % (_lane, e), "error")
            time.sleep(10)

        cur.execute(
            "SELECT stage, count(*) FROM ediscovery_stage_status "
            "WHERE collection_id=%s::uuid AND stage = ANY(%s) AND state='failed' "
            "GROUP BY stage", (cid, list(LANES)))
        fails = {r[0]: r[1] for r in cur.fetchall()}
        if fails:
            prog("DAG complete with failures: %s (QC surface)" % fails, "error")
        _status(cur, tenant, cid, "review_ready")
        prog("Pipeline v2 complete -- collection ready for review", "success")
        return {"status": "review_ready", "collection_id": cid, "failed": fails}
    except Exception as e:
        try:
            _log(cur, tenant, cid, "FATAL: %s" % str(e)[:400], "error")
        except Exception:
            pass
        logger.exception("dag pipeline failed for %s", cid)
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Ledger-as-queue ingestion DAG")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", TENANT_DEFAULT))
    ap.add_argument("--collection", default=None)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--drain", default=None, choices=list(LANES))
    ap.add_argument("--claim", type=int, default=0)
    ap.add_argument("--idle", type=int, default=90)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reap", action="store_true")
    ap.add_argument("--stale", type=int, default=1800)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--spine-workers", type=int, default=8)
    ap.add_argument("--ocr-workers", type=int, default=2)
    ap.add_argument("--embed-workers", type=int, default=1)
    args = ap.parse_args()

    if args.seed:
        if not args.collection:
            ap.error("--seed requires --collection")
        out = seed(args.tenant, args.collection, reset=args.reset)
    elif args.drain:
        out = drain(args.drain, claim=args.claim, idle_secs=args.idle,
                    max_units=args.max, force=args.force)
    elif args.reap:
        out = reap(stale_secs=args.stale)
    elif args.status:
        out = status(args.collection)
    elif args.run:
        if not args.collection:
            ap.error("--run requires --collection")
        out = run_collection(args.tenant, args.collection,
                             spine_workers=args.spine_workers,
                             ocr_workers=args.ocr_workers,
                             embed_workers=args.embed_workers)
    else:
        ap.error("one of --seed/--drain/--reap/--status/--run required")
    logger.info("DONE %s", json.dumps(out))


if __name__ == "__main__":
    main()


def run_collection_full(tenant_id, collection_id, user_id=None,
                        spine_workers=12, ocr_workers=4, embed_workers=3) -> dict:
    """GUI/API entrypoint: legacy intake first (source copy, archive/PST
    expansion, hashing, dedupe, ediscovery_documents rows), then the v2
    ledger DAG over the resulting substrate.

    Interim composition until intake is ported to a standalone stage in
    the ingestion rework. Known limitation: if intake decomposes a folder
    source into child collections, the children self-enqueue on the legacy
    path and do not get the DAG.
    """
    from modules.ediscovery.jobs.ingest_collection import (
        ingest_ediscovery_collection)
    intake = ingest_ediscovery_collection(tenant_id, collection_id, user_id)
    dag = run_collection(tenant_id, collection_id, user_id,
                         spine_workers=spine_workers,
                         ocr_workers=ocr_workers,
                         embed_workers=embed_workers)
    return {"intake": intake, "dag": dag}
