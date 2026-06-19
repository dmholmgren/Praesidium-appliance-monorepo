"""sync_align.py -- U9 sync lane: forced alignment of a deposition transcript
to its video, producing line-level timecodes (transcript_lines.timecode_ms).

Engine strategy (Dennis's call): aeneas PRIMARY, whisper FALLBACK.
  - aeneas aligns the *certified* transcript text to the audio -- the official
    record stays the source of truth; we only borrow timings. CPU-light, no torch.
  - whisper fallback runs only when aeneas is unavailable / errors / aligns too
    little (e.g. no usable certified text). It transcribes, then maps the known
    line text onto the word stream so the certified text still wins.

Both engines run as one-shot docker sidecars (the aeneas build won't live on the
platform image's py3.12, and whisper drags torch -- isolation keeps the platform
image clean). The platform image's own ffmpeg extracts the audio; the sidecar
sees only /work/audio.wav + /work/fragments.txt and returns /work/out.json.

The sidecar out.json contract (identical for both engines):
    {"engine": "...", "coverage": <0..1>, "n_in": <int>,
     "fragments": [{"id": "P0001L0005", "begin": <sec>, "end": <sec>, "text": "..."}]}

CLI (inside praesidium-web, cwd /app):
  python -m modules.depositions.jobs.sync_align --tenant T --transcript <uuid>
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)

# --- config (all overridable; defaults work on the live appliance) -----------
FFMPEG = os.environ.get("DEPO_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("DEPO_FFPROBE", "ffprobe")
DOCKER = os.environ.get("DEPO_DOCKER", "docker")
ALIGNER_IMAGE = os.environ.get("DEPO_ALIGNER_IMAGE", "praesidium-depo-aligner:latest")
WHISPER_IMAGE = os.environ.get("DEPO_WHISPER_IMAGE", "praesidium-depo-whisper:latest")
STORAGE_ROOT = os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium")
SCRATCH = os.environ.get("DEPO_SYNC_SCRATCH", os.path.join(STORAGE_ROOT, ".tmp", "depo-sync"))
LANG = os.environ.get("DEPO_SYNC_LANG", "eng")
MIN_COVERAGE = float(os.environ.get("DEPO_SYNC_MIN_COVERAGE", "0.5"))
SIDECAR_TIMEOUT = int(os.environ.get("DEPO_SYNC_TIMEOUT", "5400"))  # 90 min

_FRAG_RE = re.compile(r"P(\d+)L(\d+)")


def _connect():
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


# --- ffmpeg (runs in the platform image; ffmpeg added there for U7) ----------

def _extract_wav(src: str, wav_path: str):
    """Demux a mono 16 kHz PCM WAV from the source video/audio."""
    cmd = [FFMPEG, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
           "-f", "wav", wav_path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
        raise RuntimeError("ffmpeg audio extract failed: %s"
                           % p.stderr.decode("utf-8", "replace")[-800:])


def _probe_duration_ms(src: str):
    cmd = [FFPROBE, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=noprint_wrappers=1:nokey=1", src]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        return int(round(float(p.stdout.decode().strip()) * 1000))
    except Exception:
        return None


# --- fragment manifest -------------------------------------------------------

def _write_fragments(lines, frag_path: str) -> int:
    """lines: [(page, line, text)] in order. Writes aeneas 'parsed' format
    'P{page}L{line}|text', skipping blank text. Returns count written."""
    n = 0
    with open(frag_path, "w", encoding="utf-8") as fh:
        for page, line, text in lines:
            t = (text or "").replace("|", "/").replace("\r", " ").replace("\n", " ").strip()
            if not t:
                continue
            fh.write("P%04dL%04d|%s\n" % (page, line, t))
            n += 1
    return n


# --- sidecar invocation ------------------------------------------------------

def _image_present(image: str) -> bool:
    p = subprocess.run([DOCKER, "image", "inspect", image],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.returncode == 0


def _run_sidecar(image: str, workdir: str) -> dict:
    """docker run --rm -v workdir:/work IMAGE -> parsed out.json (raises on failure).
    workdir is a host path under /mnt/praesidium, so the host daemon's -v
    resolves it 1:1 with what this (containerized) caller sees."""
    out_path = os.path.join(workdir, "out.json")
    err_path = os.path.join(workdir, "error.txt")
    if os.path.exists(out_path):
        os.unlink(out_path)
    cmd = [DOCKER, "run", "--rm", "-v", "%s:/work" % workdir, image]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=SIDECAR_TIMEOUT)
    if p.returncode != 0 or not os.path.exists(out_path):
        reason = ""
        if os.path.exists(err_path):
            with open(err_path) as fh:
                reason = fh.read()[-800:]
        raise RuntimeError("%s sidecar failed (rc=%s): %s | %s"
                           % (image, p.returncode, reason,
                              p.stderr.decode("utf-8", "replace")[-400:]))
    with open(out_path, encoding="utf-8") as fh:
        return json.load(fh)


# --- write timecodes back ----------------------------------------------------

def _apply_timecodes(conn, tenant, tid, result) -> int:
    """Write fragment timings onto transcript_lines.timecode_ms (+ source).
    Returns number of lines timecoded."""
    engine = result.get("engine", "aeneas")
    cur = conn.cursor()
    from psycopg2.extras import execute_values
    rows = []
    for f in result.get("fragments", []):
        m = _FRAG_RE.match(f.get("id", ""))
        if not m:
            continue
        page, line = int(m.group(1)), int(m.group(2))
        ms = int(round(float(f["begin"]) * 1000))
        rows.append((tid, page, line, ms, engine))
    if not rows:
        return 0
    # reset prior auto timecodes for this transcript, preserve manual anchors
    cur.execute(
        "UPDATE transcript_lines SET timecode_ms=NULL, timecode_source=NULL "
        "WHERE transcript_id=CAST(%s AS uuid) "
        "  AND (timecode_source IS NULL OR timecode_source <> 'manual')",
        (str(tid),))
    execute_values(
        cur,
        "UPDATE transcript_lines AS t SET timecode_ms = v.ms, timecode_source = v.eng "
        "FROM (VALUES %s) AS v(tid, p, l, ms, eng) "
        "WHERE t.transcript_id = v.tid::uuid AND t.page = v.p AND t.line = v.l "
        "  AND (t.timecode_source IS NULL OR t.timecode_source <> 'manual')",
        rows, template="(%s,%s,%s,%s,%s)", page_size=1000)
    return len(rows)


# --- orchestration -----------------------------------------------------------

def sync_transcript(conn, tenant, transcript_id) -> dict:
    """Resolve video, extract audio, align (aeneas->whisper), write timecodes."""
    tenant = (tenant or "").strip()
    cur = conn.cursor()
    cur.execute(
        "SELECT video_path, source_file_path, has_video "
        "FROM deposition_transcripts WHERE id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s",
        (str(transcript_id), tenant))
    row = cur.fetchone()
    if not row:
        raise RuntimeError("transcript %s not found for tenant" % transcript_id)
    video_path = row[0]
    if not video_path:
        raise RuntimeError("no video_path on transcript -- attach a synced video first")
    if not os.path.exists(video_path):
        raise RuntimeError("video_path missing on disk: %s" % video_path)

    cur.execute(
        "SELECT page, line, text FROM transcript_lines "
        "WHERE transcript_id=CAST(%s AS uuid) ORDER BY page, line",
        (str(transcript_id),))
    lines = cur.fetchall()
    if not lines:
        raise RuntimeError("no transcript_lines -- ingest the transcript first")

    workdir = os.path.join(SCRATCH, "%s-%d" % (transcript_id, os.getpid()))
    os.makedirs(workdir, exist_ok=True)
    try:
        n_frag = _write_fragments(lines, os.path.join(workdir, "fragments.txt"))
        if n_frag == 0:
            raise RuntimeError("no non-empty transcript lines to align")
        with open(os.path.join(workdir, "lang"), "w") as fh:
            fh.write(LANG)
        _extract_wav(video_path, os.path.join(workdir, "audio.wav"))
        duration_ms = _probe_duration_ms(video_path)

        result, engine_err = None, None
        # PRIMARY: aeneas
        try:
            if not _image_present(ALIGNER_IMAGE):
                raise RuntimeError("aligner image %s not present" % ALIGNER_IMAGE)
            r = _run_sidecar(ALIGNER_IMAGE, workdir)
            if r.get("coverage", 0) < MIN_COVERAGE:
                raise RuntimeError("aeneas coverage %.3f < %.2f"
                                   % (r.get("coverage", 0), MIN_COVERAGE))
            result = r
        except Exception as e:
            engine_err = "aeneas: %s" % e
            logger.warning("aeneas alignment failed, trying whisper fallback: %s", e)

        # FALLBACK: whisper
        if result is None:
            if not _image_present(WHISPER_IMAGE):
                raise RuntimeError(
                    "%s; whisper fallback unavailable (image %s not built)"
                    % (engine_err, WHISPER_IMAGE))
            result = _run_sidecar(WHISPER_IMAGE, workdir)

        n_tc = _apply_timecodes(conn, tenant, transcript_id, result)
        coverage = result.get("coverage")
        if coverage is None and n_frag:
            coverage = round(n_tc / float(n_frag), 4)
        cur.execute(
            "UPDATE deposition_transcripts SET has_timecodes=true, "
            "  sync_engine=%s, sync_coverage=%s, synced_at=now(), "
            "  video_duration_ms=COALESCE(%s, video_duration_ms), updated_at=now() "
            "WHERE id=CAST(%s AS uuid)",
            (result.get("engine"), coverage, duration_ms, str(transcript_id)))
        conn.commit()
        return {"transcript_id": str(transcript_id), "engine": result.get("engine"),
                "lines_timecoded": n_tc, "fragments": n_frag,
                "coverage": coverage, "duration_ms": duration_ms,
                "fallback_note": engine_err}
    finally:
        try:
            shutil.rmtree(workdir)
        except Exception:
            logger.warning("could not clean scratch %s", workdir)



# --- manual correction (piecewise-linear rescale) ----------------------------

def apply_correction(conn, tenant, transcript_id, anchors) -> dict:
    """Lawyer-facing sync correction. The user supplies the TRUE time for one or
    more lines (anchors); we pin those (timecode_source='manual') and rescale the
    remaining auto timecodes by piecewise-linear interpolation between anchors
    (constant shift before the first / after the last). One anchor => pure shift.

    anchors: [{"page": int, "line": int, "ms": int}, ...]
    Returns {"anchors": n_set, "rescaled": n_rescaled}.
    """
    tenant = (tenant or "").strip()
    if not anchors:
        return {"anchors": 0, "rescaled": 0}
    cur = conn.cursor()
    # original auto timecode for each anchor line (x-axis for the rescale)
    pairs = []  # (orig_ms, corrected_ms)
    for a in anchors:
        page, line, corr = int(a["page"]), int(a["line"]), int(a["ms"])
        cur.execute(
            "SELECT timecode_ms FROM transcript_lines "
            "WHERE transcript_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "  AND page=%s AND line=%s",
            (str(transcript_id), tenant, page, line))
        r = cur.fetchone()
        if r is None:
            continue
        if r[0] is not None:
            pairs.append((int(r[0]), corr))
        cur.execute(
            "UPDATE transcript_lines SET timecode_ms=%s, timecode_source='manual' "
            "WHERE transcript_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "  AND page=%s AND line=%s",
            (corr, str(transcript_id), tenant, page, line))
    rescaled = 0
    pairs = sorted({p[0]: p for p in pairs}.values())  # dedupe by orig, sorted
    if pairs:
        cur.execute(
            "SELECT page, line, timecode_ms FROM transcript_lines "
            "WHERE transcript_id=CAST(%s AS uuid) AND TRIM(tenant_id)=%s "
            "  AND timecode_ms IS NOT NULL "
            "  AND (timecode_source IS NULL OR timecode_source <> 'manual')",
            (str(transcript_id), tenant))
        rows = cur.fetchall()
        from psycopg2.extras import execute_values
        updates = []
        for page, line, t in rows:
            updates.append((str(transcript_id), page, line, _remap(int(t), pairs)))
        if updates:
            execute_values(
                cur,
                "UPDATE transcript_lines AS x SET timecode_ms = v.ms "
                "FROM (VALUES %s) AS v(tid, p, l, ms) "
                "WHERE x.transcript_id = v.tid::uuid AND x.page = v.p AND x.line = v.l",
                updates, template="(%s,%s,%s,%s)", page_size=1000)
            rescaled = len(updates)
    cur.execute(
        "UPDATE deposition_transcripts SET has_timecodes=true, updated_at=now() "
        "WHERE id=CAST(%s AS uuid)", (str(transcript_id),))
    conn.commit()
    return {"anchors": len(anchors), "rescaled": rescaled}


def _remap(t, pairs):
    """Piecewise-linear map of original ms t through sorted (orig, corr) anchors."""
    if len(pairs) == 1:
        o, c = pairs[0]
        return max(0, t + (c - o))
    if t <= pairs[0][0]:
        o, c = pairs[0]
        return max(0, t + (c - o))
    if t >= pairs[-1][0]:
        o, c = pairs[-1]
        return max(0, t + (c - o))
    for i in range(len(pairs) - 1):
        o0, c0 = pairs[i]
        o1, c1 = pairs[i + 1]
        if o0 <= t <= o1:
            if o1 == o0:
                return c0
            frac = (t - o0) / float(o1 - o0)
            return int(round(c0 + frac * (c1 - c0)))
    return t

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="Deposition sync lane (forced alignment)")
    ap.add_argument("--tenant", default=os.environ.get("TENANT_ID", ""))
    ap.add_argument("--transcript", required=True)
    args = ap.parse_args()
    conn = _connect()
    try:
        out = sync_transcript(conn, args.tenant, args.transcript)
    finally:
        conn.close()
    logger.info("DONE %s", json.dumps(out, default=str))


if __name__ == "__main__":
    main()
