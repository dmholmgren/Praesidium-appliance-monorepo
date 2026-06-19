"""clip_render.py -- U7 clip-as-object renderer.

Turns a designation's page:line span into a reusable video-clip object:
resolve the span to [in_ms, out_ms] from transcript_lines.timecode_ms, cut the
source video with ffmpeg, drop the .mp4 into the matter DMS tree as a first
-class document (so it is browsable / re-attachable like any other file), and
record it on depo_clips (in_ms/out_ms, rendered_path, object_uuid->documents.id,
render_status). Mirrors designation_report.py's "subprocess to a binary baked
into the web image" pattern (soffice there, ffmpeg here) -- no new service.

Auto-split: render_segments() takes a list of contiguous [in,out] spans, cuts
each independently and concats, so a reel of several designations (or one
designation broken by an interleaved counter-designation) does not bleed
across the gaps. A single contiguous span is one cut, no concat.

Config:
  DEPO_FFMPEG        ffmpeg binary (default "ffmpeg"); live the moment ffmpeg
                     is in the web image.
  DEPO_CLIP_TAIL_MS  run-out appended after the last designated line when no
                     following timecode bounds it (timecodes mark line starts).
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import subprocess
import tempfile
import uuid

logger = logging.getLogger(__name__)

FFMPEG = os.environ.get("DEPO_FFMPEG", "ffmpeg")
TAIL_MS = int(os.environ.get("DEPO_CLIP_TAIL_MS", "3000"))
PRAESIDIUM_ROOT = os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium")
CLIP_FOLDER = "Deposition Clips"


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------

def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


def _key(p, l):
    return (p or 0) * 100000 + (l or 0)


# ---------------------------------------------------------------------------
# span -> timecode resolution
# ---------------------------------------------------------------------------

def resolve_span_ms(conn, tenant, transcript_id, sp, sl, ep, el):
    """[in_ms, out_ms] for a page:line span from per-line timecodes.

    in  = first timecoded line at/after the span start.
    out = the first line that starts *after* the span end (so the last
          designated line plays in full), else last line's start + TAIL_MS.
    Returns None when the span has no timecodes (un-synced transcript).
    """
    cur = conn.cursor()
    lo, hi = _key(sp, sl), _key(ep, el)
    cur.execute(
        "SELECT page, line, timecode_ms FROM transcript_lines "
        "WHERE transcript_id = CAST(%s AS uuid) AND TRIM(tenant_id) = TRIM(%s) "
        "AND timecode_ms IS NOT NULL ORDER BY page, line",
        (transcript_id, tenant))
    in_ms = out_ms = last_tc = None
    for (p, l, tc) in cur.fetchall():
        k = _key(p, l)
        if lo <= k <= hi:
            if in_ms is None:
                in_ms = tc
            last_tc = tc
        elif k > hi and in_ms is not None:
            out_ms = tc
            break
    if in_ms is None:
        return None
    if out_ms is None:
        out_ms = (last_tc if last_tc is not None else in_ms) + TAIL_MS
    return (int(in_ms), int(max(out_ms, in_ms + 1)))


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def build_cut_cmd(src, in_ms, out_ms, out_path):
    """ffmpeg command for one [in,out] cut (re-encoded for frame accuracy)."""
    ss = max(0, in_ms) / 1000.0
    dur = max(0.001, (out_ms - in_ms) / 1000.0)
    return [FFMPEG, "-y", "-ss", "%.3f" % ss, "-i", src, "-t", "%.3f" % dur,
            "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac",
            "-movflags", "+faststart", out_path]


def _run(cmd):
    logger.info("ffmpeg: %s", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed (rc=%d): %s" % (r.returncode, (r.stderr or "")[-600:]))


def render_segments(src, segments, out_path):
    """Cut one or more [in_ms,out_ms] spans into a single mp4 at out_path."""
    if not segments:
        raise RuntimeError("no segments to render")
    if len(segments) == 1:
        _run(build_cut_cmd(src, segments[0][0], segments[0][1], out_path))
        return out_path
    tmpd = tempfile.mkdtemp(prefix="depoclip_")
    parts = []
    for i, (a, b) in enumerate(segments):
        pp = os.path.join(tmpd, "p%03d.mp4" % i)
        _run(build_cut_cmd(src, a, b, pp))
        parts.append(pp)
    listf = os.path.join(tmpd, "concat.txt")
    with open(listf, "w") as fh:
        for pp in parts:
            fh.write("file '%s'\n" % pp.replace("'", "'\\''"))
    _run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", listf,
          "-c", "copy", "-movflags", "+faststart", out_path])
    return out_path


# ---------------------------------------------------------------------------
# DMS
# ---------------------------------------------------------------------------

def _matter_clip_dir(conn, tenant, matter_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT m.matter_name, c.client_name FROM matters m "
        "LEFT JOIN clients c ON m.client_id = c.id AND TRIM(m.tenant_id)=TRIM(c.tenant_id) "
        "WHERE m.id = CAST(%s AS uuid) AND TRIM(m.tenant_id)=TRIM(%s)",
        (matter_id, tenant))
    row = cur.fetchone()
    if not row or not row[0] or not row[1]:
        return None
    matter, client = row[0], row[1]
    p = os.path.join(PRAESIDIUM_ROOT, tenant.strip(), "matters", client, matter, CLIP_FOLDER)
    os.makedirs(p, exist_ok=True)
    return p


def _register_doc(conn, tenant, matter_id, path, created_by=None):
    """Insert a documents row for the rendered clip; returns its uuid."""
    doc_id = str(uuid.uuid4())
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for ch in iter(lambda: fh.read(1 << 20), b""):
            h.update(ch)
    fname = os.path.basename(path)
    mime = mimetypes.guess_type(fname)[0] or "video/mp4"
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO documents (id, tenant_id, matter_id, filename, original_filename, "
        "mime_type, file_size, storage_path, checksum, version_number, status, "
        "created_by, created_at, updated_at) "
        "VALUES (CAST(%s AS uuid), %s, CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, 1, "
        "'active', %s, NOW(), NOW()) ON CONFLICT DO NOTHING",
        (doc_id, tenant.strip(), matter_id, fname, fname, mime, size, path,
         h.hexdigest(), created_by))
    return doc_id


# ---------------------------------------------------------------------------
# render a clip object
# ---------------------------------------------------------------------------

def render_clip(conn, tenant, clip_id, created_by=None):
    """Render depo_clips.{clip_id} -> mp4 in the matter DMS tree + register it.

    Idempotent re-render: overwrites the file at the same name and refreshes
    the documents row. Raises with an actionable message when prerequisites
    (video, timecodes) are missing -- the caller surfaces that to the UI.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT c.id::text, c.transcript_id::text, c.in_ms, c.out_ms, "
        "       d.matter_id::text, d.start_page, d.start_line, d.end_page, d.end_line, "
        "       t.video_path, t.has_video, t.deponent "
        "FROM depo_clips c "
        "JOIN depo_designations d ON d.id = c.designation_id AND TRIM(d.tenant_id)=TRIM(c.tenant_id) "
        "JOIN deposition_transcripts t ON t.id = c.transcript_id AND TRIM(t.tenant_id)=TRIM(c.tenant_id) "
        "WHERE c.id = CAST(%s AS uuid) AND TRIM(c.tenant_id)=TRIM(%s)",
        (clip_id, tenant))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("clip not found")
    (cid, tx, in_ms, out_ms, matter_id, sp, sl, ep, el,
     video_path, has_video, deponent) = row

    if not video_path or not has_video:
        raise RuntimeError("transcript has no source video -- attach a synced "
                           "video (set deposition_transcripts.video_path) first")
    if not os.path.exists(video_path):
        raise RuntimeError("source video missing on disk: %s" % video_path)

    if in_ms is None or out_ms is None:
        span = resolve_span_ms(conn, tenant, tx, sp, sl, ep, el)
        if not span:
            raise RuntimeError("no timecodes for this span -- run the sync lane "
                               "(U9) or ingest a timecoded transcript")
        in_ms, out_ms = span

    clip_dir = _matter_clip_dir(conn, tenant, matter_id)
    if not clip_dir:
        raise RuntimeError("could not resolve matter DMS root")
    safe = "".join(ch for ch in (deponent or "Depo")
                   if ch.isalnum() or ch in " -_").strip()[:40] or "Depo"
    fname = "%s p%s-%s_%s.mp4" % (safe, sp, ep, cid[:8])
    out_path = os.path.join(clip_dir, fname)

    cur.execute("UPDATE depo_clips SET render_status='rendering', in_ms=%s, "
                "out_ms=%s, updated_at=NOW() WHERE id=CAST(%s AS uuid)",
                (in_ms, out_ms, cid))
    conn.commit()
    try:
        render_segments(video_path, [(in_ms, out_ms)], out_path)
    except Exception as e:
        cur.execute("UPDATE depo_clips SET render_status='error', updated_at=NOW() "
                    "WHERE id=CAST(%s AS uuid)", (cid,))
        conn.commit()
        logger.exception("render_clip failed for %s", cid)
        raise

    doc_id = _register_doc(conn, tenant, matter_id, out_path, created_by)
    cur.execute("UPDATE depo_clips SET render_status='rendered', rendered_path=%s, "
                "object_uuid=%s, updated_at=NOW() WHERE id=CAST(%s AS uuid)",
                (out_path, doc_id, cid))
    conn.commit()
    return {"clip_id": cid, "doc_id": doc_id, "path": out_path,
            "in_ms": int(in_ms), "out_ms": int(out_ms),
            "duration_ms": int(out_ms - in_ms), "render_status": "rendered"}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Render a deposition clip to mp4.")
    ap.add_argument("--tenant", required=True)
    ap.add_argument("--clip", required=True, help="depo_clips.id")
    a = ap.parse_args()
    conn = _connect()
    try:
        print(render_clip(conn, a.tenant, a.clip))
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
