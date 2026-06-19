"""depo_dag.py -- deposition evidence pipeline over the shared ledger-as-queue core.

Mirrors modules/ediscovery/jobs/ledger_dag.py (v18.4 ledger-as-queue): the stage
table (deposition_stage_status) IS the queue; SKIP LOCKED batch claims; tiny claim
txn (flip to running, commit, THEN heavy work — never hold a row lock across a
parse); per-unit txn so a poison transcript fails alone; input_hash short-circuits
unchanged work on resume; a stale-claim reaper recovers crashed workers.

Grain (the lesson from the file-move DAG — make the unit the right grain):
  ingest, segment, sync  -> the transcript is the unit
  embed                  -> the qa_unit is the unit (fan-out: one row per Q&A
                            exchange — that's the parallel win, drained in U2)

Stages implemented here (U1): ingest, segment. embed/sync land in U2/U9; their
ledger rows are seeded now and simply wait for those drains.

  ingest   parse the transcript file -> canonical_text + transcript_lines
           (§0: the parser is the only writer of canonical text). Detect
           has_video / scanned / proprietary. _seed_next -> 1 segment row.
  segment  structural Q/A/BY/COLLOQUY segmentation -> transcript_qa_units;
           write qa_role back onto transcript_lines. _seed_next -> 1 embed row
           per qa_unit (fan-out).

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.depo_dag --register --file PATH \
        [--session N] [--matter UUID] [--deponent NAME] [--tenant T]
  python -m modules.depositions.jobs.depo_dag --seed   --transcript <uuid>
  python -m modules.depositions.jobs.depo_dag --drain  ingest|segment \
        [--claim N] [--idle SECS] [--max N] [--force]
  python -m modules.depositions.jobs.depo_dag --status [--transcript <uuid>]
  python -m modules.depositions.jobs.depo_dag --reap   [--stale SECS]
  python -m modules.depositions.jobs.depo_dag --run    --file PATH [--session N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

STAGE_INGEST = "ingest"
STAGE_SEGMENT = "segment"
STAGE_EMBED = "embed"      # drained in U2; seeded by segment here
STAGE_SYNC = "sync"        # U9: forced alignment (transcript grain), drained here
LANES = (STAGE_INGEST, STAGE_SEGMENT, STAGE_EMBED, STAGE_SYNC)   # lanes this module drains

MAX_ATTEMPTS = 4
POLL_SECS = 3
DEFAULT_CLAIM = {STAGE_INGEST: 4, STAGE_SEGMENT: 4, STAGE_EMBED: 24, STAGE_SYNC: 2}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".m4v", ".wmv", ".mts"}

import re as _re
_DEPO_EX_RE = _re.compile(r"\bexhibit\s+(?:(?:no\.?|number)\s*)?(\d{1,4})\b", _re.I)


def _exnum(s):
    m = _re.match(r"\s*0*(\d+)", str(s or ""))
    return m.group(1) if m else None


def link_exhibits_to_transcript(conn, transcript_id):
    """Anchor each exhibit link to the FIRST place its number is referenced in
    the parsed transcript (deposition_exhibit_links.anchor_page/anchor_line), so
    the viewer can jump from an exhibit to where it was used. Idempotent: only
    fills links whose anchor is still NULL. Best-effort — never raises."""
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, exhibit_number FROM deposition_exhibit_links "
            "WHERE transcript_id=CAST(%s AS uuid) AND anchor_page IS NULL "
            "AND exhibit_number IS NOT NULL AND exhibit_number <> ''", (transcript_id,))
        links = cur.fetchall()
        if not links:
            return 0
        want = {}
        for lid, num in links:
            n = _exnum(num)
            if n:
                want.setdefault(n, []).append(lid)
        if not want:
            return 0
        cur.execute(
            "SELECT page, line, text FROM transcript_lines "
            "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line", (transcript_id,))
        found = {}
        for page, line, text in cur.fetchall():
            for m in _DEPO_EX_RE.finditer(text or ""):
                n = _exnum(m.group(1))
                if n in want and n not in found:
                    found[n] = (page, line)
            if len(found) == len(want):
                break
        upd = 0
        for n, (page, line) in found.items():
            for lid in want[n]:
                cur.execute(
                    "UPDATE deposition_exhibit_links SET anchor_page=%s, anchor_line=%s, "
                    "updated_at=now() WHERE id=%s", (page, line, lid))
                upd += 1
        return upd
    except Exception as e:
        logger.warning("link_exhibits_to_transcript(%s) failed: %s", transcript_id, e)
        return 0


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# registration + seeding
# ---------------------------------------------------------------------------

def register_transcript(tenant_id, file_path, session_id=None, matter_id=None,
                        deponent=None, imported_by=None, has_video=False,
                        priority=100, seed=True, transcript_kind="deposition",
                        trial_id=None, volume=None, trial_day=None,
                        title=None) -> dict:
    """Create the deposition_transcripts row for a file and seed its ingest
    ledger row. Idempotent on (tenant, sha256): re-registering the same file
    returns the existing transcript and re-pends ingest. Heavy parse happens in
    the ingest drain, not here."""
    from modules.depositions.parsers.transcript_parsers import detect_format
    tenant = (tenant_id or "").strip()
    sha = _sha256(file_path)
    fmt = detect_format(file_path)
    conn = _connect()
    try:
        cur = conn.cursor()
        # inherit matter from the session if not supplied
        if session_id and not matter_id:
            cur.execute("SELECT matter_id FROM viaticum_sessions WHERE id=%s",
                        (session_id,))
            r = cur.fetchone()
            if r:
                matter_id = r[0]
        cur.execute(
            "INSERT INTO deposition_transcripts "
            "  (tenant_id, session_id, matter_id, deponent, source_format, "
            "   source_file_path, sha256, has_video, status, transcript_kind, "
            "   trial_id, volume, trial_day, title) "
            "VALUES (%s,%s,CAST(%s AS uuid),%s,%s,%s,%s,%s,'pending',%s,"
            "        CAST(%s AS uuid),%s,%s,%s) "
            "ON CONFLICT (tenant_id, sha256) WHERE sha256 IS NOT NULL "
            "DO UPDATE SET session_id=COALESCE(EXCLUDED.session_id, deposition_transcripts.session_id), "
            "  matter_id=COALESCE(EXCLUDED.matter_id, deposition_transcripts.matter_id), "
            "  source_file_path=EXCLUDED.source_file_path, "
            "  transcript_kind=EXCLUDED.transcript_kind, "
            "  trial_id=COALESCE(EXCLUDED.trial_id, deposition_transcripts.trial_id), "
            "  volume=COALESCE(EXCLUDED.volume, deposition_transcripts.volume), "
            "  trial_day=COALESCE(EXCLUDED.trial_day, deposition_transcripts.trial_day), "
            "  title=COALESCE(EXCLUDED.title, deposition_transcripts.title), "
            "  updated_at=now() "
            "RETURNING id::text, session_id, matter_id::text",
            (tenant, session_id, (str(matter_id) if matter_id else None),
             deponent, fmt, file_path, sha, has_video, transcript_kind,
             (str(trial_id) if trial_id else None), volume, trial_day, title))
        tid, sid, mid = cur.fetchone()
        if seed:
            _seed(cur, tenant, tid, sid, mid, STAGE_INGEST, priority)
        conn.commit()
        logger.info("registered transcript %s (%s)%s", tid, fmt,
                    " seeded ingest" if seed else " (pending, ingest not seeded)")
        return {"transcript_id": tid, "session_id": sid, "format": fmt}
    finally:
        conn.close()


def _seed(cur, tenant, transcript_id, session_id, matter_id, stage, priority=100,
          qa_unit_id=None):
    """Seed one ledger row. Transcript-grain (qa_unit_id NULL) and qa_unit-grain
    rows hit their respective partial unique indexes for idempotent re-seed."""
    if qa_unit_id is None:
        cur.execute(
            "INSERT INTO deposition_stage_status "
            "  (tenant_id, session_id, transcript_id, matter_id, stage, state, priority, updated_at) "
            "VALUES (%s,%s,CAST(%s AS uuid),CAST(%s AS uuid),%s,'pending',%s,now()) "
            "ON CONFLICT (tenant_id, transcript_id, stage) WHERE qa_unit_id IS NULL "
            "DO UPDATE SET state='pending', priority=EXCLUDED.priority, "
            "  error_class=NULL, error_message=NULL, updated_at=now()",
            (tenant, session_id, str(transcript_id),
             (str(matter_id) if matter_id else None), stage, priority))
    else:
        cur.execute(
            "INSERT INTO deposition_stage_status "
            "  (tenant_id, session_id, transcript_id, qa_unit_id, matter_id, stage, state, priority, updated_at) "
            "VALUES (%s,%s,CAST(%s AS uuid),CAST(%s AS uuid),CAST(%s AS uuid),%s,'pending',%s,now()) "
            "ON CONFLICT (tenant_id, qa_unit_id, stage) WHERE qa_unit_id IS NOT NULL "
            "DO UPDATE SET state='pending', priority=EXCLUDED.priority, "
            "  error_class=NULL, error_message=NULL, updated_at=now()",
            (tenant, session_id, str(transcript_id), str(qa_unit_id),
             (str(matter_id) if matter_id else None), stage, priority))


def seed_ingest(tenant_id, transcript_id, priority=100) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT session_id, matter_id::text FROM deposition_transcripts "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(transcript_id), tenant))
        r = cur.fetchone()
        if not r:
            raise ValueError("transcript %s not found" % transcript_id)
        _seed(cur, tenant, transcript_id, r[0], r[1], STAGE_INGEST, priority)
        conn.commit()
        return {"seeded": transcript_id}
    finally:
        conn.close()


def seed_sync(tenant_id, transcript_id, priority=100) -> dict:
    tenant = (tenant_id or "").strip()
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT session_id, matter_id::text FROM deposition_transcripts "
                    "WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
                    (str(transcript_id), tenant))
        r = cur.fetchone()
        if not r:
            raise ValueError("transcript %s not found" % transcript_id)
        _seed(cur, tenant, transcript_id, r[0], r[1], STAGE_SYNC, priority)
        conn.commit()
        return {"seeded_sync": transcript_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# claim / finish / reap  (mirrors ledger_dag)
# ---------------------------------------------------------------------------

def _claim(conn, stage, worker_id, n):
    cur = conn.cursor()
    cur.execute(
        "WITH c AS ("
        "  SELECT id FROM deposition_stage_status"
        "  WHERE stage=%s AND state='pending'"
        "  ORDER BY priority, updated_at"
        "  LIMIT %s FOR UPDATE SKIP LOCKED) "
        "UPDATE deposition_stage_status t "
        "SET state='running', worker_id=%s, started_at=now(), "
        "    finished_at=NULL, updated_at=now(), attempt=t.attempt+1 "
        "FROM c WHERE t.id=c.id "
        "RETURNING t.id::text, TRIM(t.tenant_id), t.transcript_id::text, "
        "          t.qa_unit_id::text, t.session_id, t.matter_id::text, "
        "          t.attempt, t.input_hash",
        (stage, n, worker_id))
    rows = cur.fetchall()
    conn.commit()
    return rows


def _finish(conn, row_id, state, duration_ms=None, error_class=None,
            error_message=None, error_detail=None, input_hash=None):
    cur = conn.cursor()
    cur.execute(
        "UPDATE deposition_stage_status SET state=%s, finished_at=now(), "
        "duration_ms=%s, error_class=%s, error_message=%s, "
        "error_detail=%s::jsonb, input_hash=COALESCE(%s, input_hash), "
        "updated_at=now() WHERE id=CAST(%s AS uuid)",
        (state, duration_ms, error_class,
         (error_message[:4000] if error_message else None),
         (json.dumps(error_detail) if error_detail else None),
         input_hash, row_id))
    conn.commit()


def reap(stale_secs=1800, max_attempts=MAX_ATTEMPTS) -> dict:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE deposition_stage_status SET "
            "  state = CASE WHEN attempt >= %s THEN 'failed' ELSE 'pending' END, "
            "  error_class = CASE WHEN attempt >= %s THEN 'StaleClaim' ELSE error_class END, "
            "  error_message = CASE WHEN attempt >= %s THEN 'reaper: attempt cap reached' "
            "                       ELSE error_message END, "
            "  updated_at = now() "
            "WHERE state='running' AND stage = ANY(%s) "
            "  AND started_at < now() - make_interval(secs => %s)",
            (max_attempts, max_attempts, max_attempts,
             list(LANES) + [STAGE_EMBED], stale_secs))
        n = cur.rowcount
        conn.commit()
        if n:
            logger.info("reaper: recovered %d stale claim(s)", n)
        return {"reaped": n}
    finally:
        conn.close()


def status(transcript_id=None) -> list:
    conn = _connect()
    try:
        cur = conn.cursor()
        where, params = "1=1", []
        if transcript_id:
            where = "transcript_id=CAST(%s AS uuid)"
            params.append(str(transcript_id))
        cur.execute(
            "SELECT stage, state, count(*), "
            "       COALESCE(avg(duration_ms) FILTER (WHERE state='done'),0)::int "
            "FROM deposition_stage_status WHERE " + where +
            " GROUP BY stage, state ORDER BY stage, state", params)
        return [{"stage": r[0], "state": r[1], "n": r[2], "avg_ms": r[3]}
                for r in cur.fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# stage units
# ---------------------------------------------------------------------------

def _ingest_unit(conn, row, force=False):
    """parse transcript file -> canonical_text + transcript_lines; seed segment."""
    from modules.depositions.parsers.transcript_parsers import (
        parse_transcript, NotConvertible)
    row_id, tenant, tid, _qa, sid, mid, attempt, prev_hash = row
    t0 = time.time()
    cur = conn.cursor()
    try:
        if attempt > MAX_ATTEMPTS:
            _finish(conn, row_id, "failed", error_class="MaxAttempts",
                    error_message="attempt cap reached")
            return "failed"
        cur.execute("SELECT source_file_path, source_format, sha256, has_video "
                    "FROM deposition_transcripts WHERE id=CAST(%s AS uuid)", (tid,))
        info = cur.fetchone()
        if not info or not info[0]:
            raise FileNotFoundError("transcript/source_file_path not found")
        path, fmt, sha, _hv = info
        if not os.path.exists(path):
            raise FileNotFoundError(path)

        # idempotency short-circuit: same file content, lines already present
        if not force and prev_hash and sha and prev_hash == sha:
            cur.execute("SELECT 1 FROM transcript_lines "
                        "WHERE transcript_id=CAST(%s AS uuid) LIMIT 1", (tid,))
            if cur.fetchone():
                conn.commit()
                _finish(conn, row_id, "done",
                        duration_ms=int((time.time()-t0)*1000),
                        error_detail={"skipped": "unchanged"}, input_hash=sha)
                return "done"

        try:
            parsed = parse_transcript(path, fmt)
        except NotConvertible as nc:
            cur.execute(
                "UPDATE deposition_transcripts SET needs_conversion=true, "
                "status='needs_conversion', updated_at=now() "
                "WHERE id=CAST(%s AS uuid)", (tid,))
            conn.commit()
            _finish(conn, row_id, "failed",
                    duration_ms=int((time.time()-t0)*1000),
                    error_class="NotConvertible",
                    error_message=str(nc),
                    error_detail={"source_format": nc.source_format},
                    input_hash=sha)
            logger.warning("ingest %s not convertible (%s)", tid, nc.source_format)
            return "failed"

        # write canonical text (§0: the only writer) + addressing layer
        cur.execute(
            "UPDATE deposition_transcripts SET canonical_text=%s, page_first=%s, "
            "page_last=%s, line_count=%s, status='ingested', updated_at=now() "
            "WHERE id=CAST(%s AS uuid)",
            (parsed.canonical_text, parsed.page_first, parsed.page_last,
             parsed.line_count, tid))
        cur.execute("DELETE FROM transcript_lines WHERE transcript_id=CAST(%s AS uuid)", (tid,))
        from psycopg2.extras import execute_values
        execute_values(
            cur,
            "INSERT INTO transcript_lines "
            "  (transcript_id, tenant_id, page, line, char_start, char_end, text) "
            "VALUES %s",
            [(tid, tenant, ln.page, ln.line, ln.char_start, ln.char_end, ln.text)
             for ln in parsed.lines],
            page_size=1000)
        _seed(cur, tenant, tid, sid, mid, STAGE_SEGMENT)
        # anchor any exhibit links for this transcript to their first reference
        link_exhibits_to_transcript(conn, tid)
        # U9: queue forced alignment too when a synced video is attached
        cur.execute("SELECT video_path FROM deposition_transcripts "
                    "WHERE id=CAST(%s AS uuid)", (tid,))
        _vp = cur.fetchone()
        if _vp and _vp[0]:
            _seed(cur, tenant, tid, sid, mid, STAGE_SYNC)
        conn.commit()
        _finish(conn, row_id, "done", duration_ms=int((time.time()-t0)*1000),
                error_detail={"lines": parsed.line_count,
                              "pages": [parsed.page_first, parsed.page_last]},
                input_hash=sha)
        return "done"
    except Exception as e:
        conn.rollback()
        _finish(conn, row_id, "failed", duration_ms=int((time.time()-t0)*1000),
                error_class=type(e).__name__, error_message=str(e))
        logger.exception("ingest unit %s failed", tid)
        return "failed"


def _segment_unit(conn, row, force=False):
    """structural Q/A segmentation -> transcript_qa_units; seed embed per unit."""
    from modules.depositions.parsers.qa_segmenter import segment as qa_seg
    from modules.depositions.parsers.trial_segmenter import segment as trial_seg
    row_id, tenant, tid, _qa, sid, mid, attempt, prev_hash = row
    t0 = time.time()
    cur = conn.cursor()
    try:
        if attempt > MAX_ATTEMPTS:
            _finish(conn, row_id, "failed", error_class="MaxAttempts",
                    error_message="attempt cap reached")
            return "failed"
        cur.execute(
            "SELECT page, line, char_start, char_end, text FROM transcript_lines "
            "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line", (tid,))
        rows = cur.fetchall()
        if not rows:
            raise RuntimeError("no transcript_lines to segment (ingest first)")
        lines = [{"page": r[0], "line": r[1], "text": r[4]} for r in rows]
        offsets = {(r[0], r[1]): (r[2], r[3]) for r in rows}

        cur.execute("SELECT transcript_kind FROM deposition_transcripts "
                    "WHERE id=CAST(%s AS uuid)", (tid,))
        kr = cur.fetchone()
        kind = (kr[0] if kr else None) or "deposition"
        seg = trial_seg if kind == "trial" else qa_seg
        units, line_roles = seg(lines)

        # idempotent re-segment: drop prior units (+ their embed ledger rows)
        cur.execute("DELETE FROM deposition_stage_status "
                    "WHERE transcript_id=CAST(%s AS uuid) AND stage=%s",
                    (tid, STAGE_EMBED))
        cur.execute("DELETE FROM transcript_qa_units WHERE transcript_id=CAST(%s AS uuid)", (tid,))

        from psycopg2.extras import execute_values
        rows_to_insert = []
        for u in units:
            cs = offsets.get((u.q_start_page, u.q_start_line), (None, None))[0]
            ce = offsets.get((u.a_end_page, u.a_end_line), (None, None))[1]
            rows_to_insert.append((
                tid, tenant, sid, u.seq, u.examiner, u.witness,
                u.q_start_page, u.q_start_line, u.a_end_page, u.a_end_line,
                cs, ce, u.question_text, u.answer_text, u.is_colloquy,
                getattr(u, "event_type", None)))
        qa_ids = []
        if rows_to_insert:
            qa_ids = execute_values(
                cur,
                "INSERT INTO transcript_qa_units "
                "  (transcript_id, tenant_id, session_id, seq, examiner, witness, "
                "   q_start_page, q_start_line, a_end_page, a_end_line, "
                "   char_start, char_end, question_text, answer_text, is_colloquy, "
                "   event_type) "
                "VALUES %s RETURNING id::text",
                rows_to_insert, page_size=500, fetch=True)
            qa_ids = [r[0] for r in qa_ids]

        # write resolved roles back onto the addressing layer (metadata only;
        # canonical_text is never touched -- §0). transcript_id rides in each
        # VALUES tuple so the set-based UPDATE needs no trailing bind.
        if line_roles:
            execute_values(
                cur,
                "UPDATE transcript_lines AS t SET qa_role = v.role "
                "FROM (VALUES %s) AS v(tid, p, l, role) "
                "WHERE t.transcript_id = v.tid::uuid AND t.page = v.p AND t.line = v.l",
                [(tid, p, l, role) for (p, l, role) in line_roles],
                template="(%s,%s,%s,%s)", page_size=1000)

        # fan-out: one embed row per qa_unit (drained in U2)
        for qid in qa_ids:
            _seed(cur, tenant, tid, sid, mid, STAGE_EMBED, qa_unit_id=qid)
        cur.execute("UPDATE deposition_transcripts SET qa_count=%s, status='segmented', "
                    "updated_at=now() WHERE id=CAST(%s AS uuid)",
                    (len(qa_ids), tid))
        conn.commit()
        _finish(conn, row_id, "done", duration_ms=int((time.time()-t0)*1000),
                error_detail={"qa_units": len(qa_ids)}, input_hash=prev_hash)
        return "done"
    except Exception as e:
        conn.rollback()
        _finish(conn, row_id, "failed", duration_ms=int((time.time()-t0)*1000),
                error_class=type(e).__name__, error_message=str(e))
        logger.exception("segment unit %s failed", tid)
        return "failed"


def _sync_unit(conn, row, force=False):
    """U9 sync lane: forced-align transcript to its video -> timecode_ms.
    Transcript-grain. Delegates to sync_align.sync_transcript (aeneas->whisper)."""
    from modules.depositions.jobs.sync_align import sync_transcript
    row_id, tenant, tid, _qa, sid, mid, attempt, prev_hash = row
    t0 = time.time()
    try:
        if attempt > MAX_ATTEMPTS:
            _finish(conn, row_id, "failed", error_class="MaxAttempts",
                    error_message="attempt cap reached")
            return "failed"
        res = sync_transcript(conn, tenant, tid)
        _finish(conn, row_id, "done", duration_ms=int((time.time()-t0)*1000),
                error_detail={"engine": res.get("engine"),
                              "lines_timecoded": res.get("lines_timecoded"),
                              "coverage": res.get("coverage")},
                input_hash=prev_hash)
        return "done"
    except Exception as e:
        conn.rollback()
        _finish(conn, row_id, "failed", duration_ms=int((time.time()-t0)*1000),
                error_class=type(e).__name__, error_message=str(e))
        logger.exception("sync unit %s failed", tid)
        return "failed"


def _embed_batch(conn, rows, force=False):
    """Batched GPU lane (qa_unit grain): one big embed call per claimed batch,
    per tenant -> transcript_qa_embeddings + ES. Mirrors ledger_dag._embed_batch."""
    from modules.depositions.jobs.embed_qa import embed_units
    out = {"done": 0, "failed": 0}
    by_tenant = {}
    for r in rows:
        by_tenant.setdefault(r[1], []).append(r)
    for tenant, trows in by_tenant.items():
        qids = [r[3] for r in trows if r[3]]
        t0 = time.time()
        try:
            embed_units(tenant, qa_unit_ids=qids, force=force)
            dur = int((time.time() - t0) * 1000 / max(len(trows), 1))
            for r in trows:
                _finish(conn, r[0], "done", duration_ms=dur)
            out["done"] += len(trows)
        except Exception as e:
            for r in trows:
                _finish(conn, r[0], "failed", error_class=type(e).__name__,
                        error_message=str(e))
            out["failed"] += len(trows)
            logger.exception("embed batch failed (%d unit(s))", len(trows))
    return out


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------

def drain(lane, claim=0, idle_secs=90, max_units=0, force=False) -> dict:
    assert lane in LANES, "unknown lane %s" % lane
    n_claim = claim or DEFAULT_CLAIM[lane]
    worker_id = "%s:%s:%d" % (os.environ.get("HOSTNAME", "drain"), lane, os.getpid())
    unit = {STAGE_INGEST: _ingest_unit, STAGE_SEGMENT: _segment_unit,
            STAGE_SYNC: _sync_unit}.get(lane)
    conn = _connect()
    s = {"lane": lane, "worker": worker_id, "claimed": 0, "done": 0, "failed": 0}
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
                r = _embed_batch(conn, rows, force=force)
                s["done"] += r["done"]
                s["failed"] += r["failed"]
                streak = 0 if r["done"] else streak + 1
            else:
                for row in rows:
                    res = unit(conn, row, force=force)
                    s[res] = s.get(res, 0) + 1
                    streak = streak + 1 if res == "failed" else 0
            if streak >= 3:
                pause = min(2 ** streak, 60)
                logger.warning("drain %s: %d consecutive failures, backing off %ds",
                               lane, streak, pause)
                time.sleep(pause)
            if max_units and s["claimed"] >= max_units:
                break
    finally:
        conn.close()
    logger.info("drain %s finished: %s", lane, json.dumps(s))
    return s


def run_transcript(tenant_id, file_path, session_id=None, matter_id=None,
                   deponent=None) -> dict:
    """Test/coordinator entrypoint: register + drain ingest + segment inline."""
    reg = register_transcript(tenant_id, file_path, session_id, matter_id, deponent)
    di = drain(STAGE_INGEST, idle_secs=2, max_units=1)
    ds = drain(STAGE_SEGMENT, idle_secs=2, max_units=1)
    de = drain(STAGE_EMBED, idle_secs=2)
    return {"register": reg, "ingest": di, "segment": ds, "embed": de,
            "status": status(reg["transcript_id"])}


# ---------------------------------------------------------------------------
# parallel coordinator (mirrors ediscovery ledger_dag.run_collection)
#
# Rides a DEDICATED RQ queue ('depositions') served by depo workers. The
# coordinator enqueues sibling lane drains over RQ for parallelism, drains
# ingest itself, then monitors the ledger (scoped to the matter) and re-enqueues
# any lane that has pending units but no live drain — until the depo lanes drain.
# ---------------------------------------------------------------------------

DEPO_QUEUE = "depositions"
_DRAIN_FN = "modules.depositions.jobs.depo_dag.drain"


def _rq_queue(name=DEPO_QUEUE):
    from redis import Redis
    from rq import Queue
    rconn = Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    return Queue(name, connection=rconn)


def enqueue_pipeline(tenant_id, matter_id, ingest_workers=2, segment_workers=2,
                     embed_workers=1) -> str:
    """Web-tier entrypoint: enqueue the coordinator onto the depositions queue.
    Returns the RQ job id. Falls back to a single inline drain if RQ is down."""
    try:
        job = _rq_queue().enqueue(
            "modules.depositions.jobs.depo_dag.run_pipeline",
            tenant_id, matter_id, ingest_workers, segment_workers, embed_workers,
            job_timeout=86400, result_ttl=3600)
        return job.id
    except Exception as e:
        logger.error("enqueue_pipeline failed (%s) -- draining ingest inline", e)
        drain(STAGE_INGEST, idle_secs=5)
        return ""


def run_pipeline(tenant_id, matter_id, ingest_workers=2, segment_workers=2,
                 embed_workers=1) -> dict:
    """Coordinator job (runs on a depositions worker)."""
    tenant = (tenant_id or "").strip()
    mid = str(matter_id) if matter_id else None
    try:
        q = _rq_queue()
        for _ in range(max(int(ingest_workers) - 1, 0)):
            q.enqueue(_DRAIN_FN, STAGE_INGEST, job_timeout=86400, result_ttl=3600)
        for _ in range(int(segment_workers)):
            q.enqueue(_DRAIN_FN, STAGE_SEGMENT, job_timeout=86400, result_ttl=3600)
        for _ in range(int(embed_workers)):
            q.enqueue(_DRAIN_FN, STAGE_EMBED, job_timeout=86400, result_ttl=3600)
        logger.info("depo coordinator: drains enqueued ingest=%s segment=%s embed=%s",
                    ingest_workers, segment_workers, embed_workers)
    except Exception as e:
        logger.error("depo coordinator enqueue failed (%s) -- draining alone", e)

    drain(STAGE_INGEST, idle_secs=120)  # coordinator pulls its weight

    deadline = time.time() + 7200          # 2h ceiling per coordinator
    last_rescue = {}
    while time.time() < deadline:
        reap()
        conn = _connect()
        try:
            cur = conn.cursor()
            if mid:
                cur.execute(
                    "SELECT stage, state, count(*) FROM deposition_stage_status "
                    "WHERE TRIM(tenant_id)=%s AND matter_id=CAST(%s AS uuid) "
                    "  AND stage = ANY(%s) AND state IN ('pending','running') "
                    "GROUP BY stage, state", (tenant, mid, list(LANES)))
            else:
                cur.execute(
                    "SELECT stage, state, count(*) FROM deposition_stage_status "
                    "WHERE TRIM(tenant_id)=%s AND stage = ANY(%s) "
                    "  AND state IN ('pending','running') "
                    "GROUP BY stage, state", (tenant, list(LANES)))
            counts = {}
            for st, state, n in cur.fetchall():
                counts.setdefault(st, {"pending": 0, "running": 0})[state] = n
        finally:
            conn.close()
        if not counts:
            break
        now = time.time()
        for lane, v in counts.items():
            if v["pending"] > 0 and v["running"] == 0 and now - last_rescue.get(lane, 0) >= 60:
                last_rescue[lane] = now
                try:
                    _rq_queue().enqueue(_DRAIN_FN, lane, job_timeout=86400, result_ttl=3600)
                    logger.warning("depo lane %s: %d pending, no live drain -- re-enqueued",
                                   lane, v["pending"])
                except Exception as e:
                    logger.error("depo lane %s rescue enqueue failed: %s", lane, e)
        time.sleep(10)
    logger.info("depo coordinator done for matter %s", mid)
    return {"ok": True, "matter_id": mid}


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Deposition evidence pipeline DAG")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--register", action="store_true")
    ap.add_argument("--file", default=None)
    ap.add_argument("--session", type=int, default=None)
    ap.add_argument("--matter", default=None)
    ap.add_argument("--deponent", default=None)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--seed-sync", action="store_true")
    ap.add_argument("--transcript", default=None)
    ap.add_argument("--drain", default=None, choices=list(LANES))
    ap.add_argument("--claim", type=int, default=0)
    ap.add_argument("--idle", type=int, default=90)
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--reap", action="store_true")
    ap.add_argument("--stale", type=int, default=1800)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--run", action="store_true")
    args = ap.parse_args()

    if args.register:
        if not args.file:
            ap.error("--register requires --file")
        out = register_transcript(args.tenant, args.file, args.session,
                                  args.matter, args.deponent)
    elif args.seed:
        if not args.transcript:
            ap.error("--seed requires --transcript")
        out = seed_ingest(args.tenant, args.transcript)
    elif args.seed_sync:
        if not args.transcript:
            ap.error("--seed-sync requires --transcript")
        out = seed_sync(args.tenant, args.transcript)
    elif args.drain:
        out = drain(args.drain, claim=args.claim, idle_secs=args.idle,
                    max_units=args.max, force=args.force)
    elif args.reap:
        out = reap(stale_secs=args.stale)
    elif args.status:
        out = status(args.transcript)
    elif args.run:
        if not args.file:
            ap.error("--run requires --file")
        out = run_transcript(args.tenant, args.file, args.session,
                             args.matter, args.deponent)
    else:
        ap.error("one of --register/--seed/--drain/--reap/--status/--run required")
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
