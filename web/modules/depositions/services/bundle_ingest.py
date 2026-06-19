"""
Deposition bundle ingest — guided triage + dispatch (Step 2/3/4).

  gather_signals(root)          -> manifest (per-file signals incl. portfolio
                                   embedded list + zip central dir)
  await triage(manifest, ...)   -> LLM proposal (units with roles)
  unbundle_portfolio(pdf, out)  -> extract embedded exhibits, label from filename
  dispatch(tenant, matter, root, proposal, user_id)
                                -> create session + register transcript (seeds
                                   depo DAG) + attach video + record exhibits

LLM calls go through the house adapter (module='depositions',
purpose='bundle_triage'). Heavy parse/segment/embed runs in the depo DAG drains.
"""
from __future__ import annotations
import os
import re
import json
import logging

logger = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mpg", ".mpeg", ".m4v", ".wmv", ".mts"}
ASCII_EXTS = {".txt", ".asc", ".lst", ".prn"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z"}
JUNK_NAMES = {"thumbs.db", ".ds_store"}
SNIFF_PDF_MAX = 60 * 1024 * 1024
SNIFF_CHARS = 700
EXH_RE = re.compile(r"EXHIBIT\s*0*([0-9]{1,4})", re.I)


def _basename(p):
    """Basename that also splits Windows paths (embedded files often carry
    V:\\NGProd\\...\\Exhibit 20.pdf), since os.path.basename keeps backslashes."""
    return (p or "").replace("\\", "/").rsplit("/", 1)[-1]


def _exnum(s):
    m = re.match(r"\s*0*(\d+)", str(s or ""))
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# signal gathering
# --------------------------------------------------------------------------- #
def hsize(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.0f}PB"


def _is_junk(name):
    low = name.lower()
    return low in JUNK_NAMES or low.startswith("~$") or low.endswith(".tmp")


def sniff_pdf(path, size):
    try:
        import fitz
        doc = fitz.open(path)
    except Exception as e:
        return {"sniff": f"<open-failed: {e}>"}
    out = {"pages": doc.page_count}
    try:
        emb = doc.embfile_count()
    except Exception:
        emb = 0
    if emb:
        out["embedded_files"] = emb
        if bool(getattr(doc, "is_collection", False)) or emb > 1:
            out["portfolio"] = True
        try:
            out["embedded"] = [
                {"name": n, "size": hsize((doc.embfile_info(n) or {}).get("length")
                                          or (doc.embfile_info(n) or {}).get("size") or 0)}
                for n in doc.embfile_names()[:40]
            ]
        except Exception:
            pass
    if size <= SNIFF_PDF_MAX and doc.page_count:
        try:
            out["sniff"] = (doc[0].get_text() or "").strip().replace("\n", " ")[:SNIFF_CHARS]
        except Exception:
            pass
    doc.close()
    return out


def sniff_text(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(4096).decode("utf-8", "replace").replace("\n", " ").strip()[:SNIFF_CHARS]
    except Exception:
        return ""


def sniff_zip(path):
    import zipfile
    from collections import Counter
    try:
        zf = zipfile.ZipFile(path)
    except Exception as e:
        return {"error": f"<zip-open-failed: {e}>"}
    infos = [i for i in zf.infolist() if not i.is_dir()]
    hist = Counter(os.path.splitext(i.filename)[1].lower() for i in infos)
    return {"entries": len(infos), "ext_histogram": dict(hist),
            "contents": [{"name": os.path.basename(i.filename), "size": hsize(i.file_size)}
                         for i in infos[:40]]}


def gather_signals(root):
    """Walk up to 2 levels; return [{folder, files:[signal,...]}, ...]."""
    groups = {}
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath[len(root):].count(os.sep) >= 2:
            dirnames[:] = []
        rel = os.path.relpath(dirpath, root)
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            ext = os.path.splitext(fn)[1].lower()
            sig = {"file": fn, "ext": ext, "size": hsize(size)}
            if _is_junk(fn):
                sig["sniff"] = "<junk>"
            elif ext == ".pdf":
                sig.update(sniff_pdf(full, size))
            elif ext in ASCII_EXTS:
                sig["sniff"] = sniff_text(full)
            elif ext in ARCHIVE_EXTS:
                sig["archive_contents"] = sniff_zip(full)
            groups.setdefault(rel, []).append(sig)
    return [{"folder": k, "files": v} for k, v in sorted(groups.items())]


# --------------------------------------------------------------------------- #
# LLM triage
# --------------------------------------------------------------------------- #
SYSTEM = """You are a deposition intake triage assistant for a litigation platform.
You receive a directory listing (grouped by sub-folder) of files a paralegal
dropped to ingest one or more depositions. Each file has name, extension, size,
and for PDFs/text an optional first-page 'sniff', page count, portfolio /
embedded-file info; archives carry an 'archive_contents' summary.

Group the files into deposition UNITS (one per deponent/session), assign every
file a ROLE, and recommend exactly which file to PARSE.

Rules:
- A UNIT is usually one deponent sub-folder; deponent name is in the folder/file names.
- PARSE SOURCE preference (best first): (1) ASCII .txt/.asc/.lst; (2) .full.pdf
  with a text layer. A portfolio .full.pdf is still the transcript but its
  exhibits are embedded.
- PROPRIETARY/ENCRYPTED, NOT parseable (role "transcript_proprietary", never the
  parse source): .ptx, .cms, .lef, .sbf, .xmef, .clt, .vid.
- DERIVATIVE renditions (role "transcript_rendition"): .miniprint/.fullprint/
  .rsletter/.index pdf, condensed/word-index.
- EXHIBITS (role "exhibit"): EXHIBIT<nn> files, bare-number PDFs in a deponent
  folder, or a range like "1-55.pdf" (role "exhibit_bundle"). Capture the number.
- VIDEO (role "video"): .mpg/.mp4/etc, ordered by trailing segment number.
- ERRATA (role "errata"): .errata.pdf; read/sign letters (role "read_and_sign").
- WORK PRODUCT (role "work_product", EXCLUDE): attorney memos/summaries/.docx notes.
- ARCHIVES: use 'archive_contents'. If they duplicate files already loose in the
  same folder, role "archive_duplicate" + recommend SKIP. If new material, role
  "archive" + flag what to extract.
- PORTFOLIO: 'embedded' lists attachments (often opaque). Reconcile vs discrete
  EXHIBIT*.pdf: prefer discrete labeled files; if embedded count exceeds discrete,
  flag that the portfolio holds exhibits not present discretely (extract needed).
- JUNK (role "junk", EXCLUDE): Thumbs.db, ~$lock, .tmp.
- A shared "Exhibits" folder NOT inside a deponent folder is UNASSIGNED.

Output STRICT JSON only (no prose/fence):
{"units":[{"deponent","folder","confidence",
  "parse_source":{"file","role","why"},
  "transcript_proprietary":[],"transcript_renditions":[],
  "exhibits":[{"number","file"}],"exhibit_bundles":[],
  "video":[{"order","file"}],"video_sidecars":[],
  "errata":[],"read_and_sign":[],
  "excluded":[{"file","role","why"}],"flags":[]}],
 "unassigned_exhibits":{"folder","files_count","note"},
 "overall_notes":""}"""


async def triage(manifest, tenant_id, matter_id=None, max_tokens=8000):
    from modules.intelligence.anthropic_adapter import call as ai_call, AICallContext
    ctx = AICallContext(tenant_id=tenant_id, module="depositions",
                        purpose="bundle_triage", matter_id=matter_id)
    user = "Directory listing to triage:\n\n" + json.dumps(manifest, indent=1)
    res = await ai_call(ctx, raw_user_prompt=user, raw_system_prompt=SYSTEM,
                        max_tokens_override=max_tokens, http_timeout_override=240.0)
    raw = (res.text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw[4:].strip() if raw[:4].lower() == "json" else raw.strip()
    try:
        parsed = json.loads(raw)
    except Exception:
        lo, hi = raw.find("{"), raw.rfind("}")
        parsed = json.loads(raw[lo:hi + 1]) if lo != -1 and hi > lo else {"_raw": raw}
    parsed["_meta"] = {"model": getattr(res, "model_used", "?"),
                       "tokens": getattr(res, "total_tokens", None),
                       "cost_usd": float(getattr(res, "cost_usd", 0) or 0)}
    return parsed


# --------------------------------------------------------------------------- #
# portfolio unbundle
# --------------------------------------------------------------------------- #
def unbundle_portfolio(pdf_path, out_dir):
    import fitz
    os.makedirs(out_dir, exist_ok=True)
    d = fitz.open(pdf_path)
    out = []
    for name in d.embfile_names():
        info = d.embfile_info(name)
        data = d.embfile_get(name)
        fn = info.get("filename") or name
        if not os.path.splitext(fn)[1]:
            fn += ".pdf"
        dest = os.path.join(out_dir, os.path.basename(fn))
        with open(dest, "wb") as fh:
            fh.write(data)
        m = EXH_RE.search(os.path.basename(fn))
        out.append({"wrote": os.path.basename(dest), "bytes": len(data),
                    "exhibit": m.group(1) if m else None})
    d.close()
    return out


def portfolio_exhibit_numbers(pdf_path):
    """List exhibit numbers embedded in a portfolio PDF, WITHOUT extracting
    (used by triage so the review modal can show 'N embedded exhibits')."""
    import fitz
    nums = []
    try:
        d = fitz.open(pdf_path)
    except Exception:
        return nums
    try:
        for name in d.embfile_names():
            try:
                fn = _basename((d.embfile_info(name) or {}).get("filename") or name)
            except Exception:
                fn = name
            m = EXH_RE.search(fn)
            if m:
                nums.append(m.group(1))
    finally:
        d.close()
    return nums


def extract_portfolio_exhibits(pdf_path, dest_dir, skip_numbers=()):
    """Single-open: extract embedded files that are exhibits with a number NOT
    in skip_numbers into dest_dir. Returns [{number, path}]."""
    import fitz
    skip = set(skip_numbers or ())
    out = []
    try:
        d = fitz.open(pdf_path)
    except Exception:
        return out
    os.makedirs(dest_dir, exist_ok=True)
    try:
        for name in d.embfile_names():
            try:
                fn = _basename((d.embfile_info(name) or {}).get("filename") or name)
            except Exception:
                fn = name
            m = EXH_RE.search(fn)
            if not m or m.group(1) in skip:
                continue
            num = m.group(1)
            if not fn.lower().endswith(".pdf"):
                fn = "Exhibit %s.pdf" % num
            dest = os.path.join(dest_dir, fn)
            try:
                with open(dest, "wb") as fh:
                    fh.write(d.embfile_get(name))
                out.append({"number": num, "path": dest})
                skip.add(num)
            except Exception:
                pass
    finally:
        d.close()
    return out


# --------------------------------------------------------------------------- #
# dispatch (sync — call via run_in_executor)
# --------------------------------------------------------------------------- #
def _abs(root, folder, fname):
    folder = "" if folder in (".", None) else folder
    return os.path.join(root, folder, fname)


def annotate_embedded(proposal, root):
    """Tag each unit with embedded_exhibits=[numbers] when its portfolio parse
    source carries exhibits not present as discrete files (so the review modal
    can show them). Mutates + returns the proposal."""
    for u in (proposal.get("units") or []):
        ps = u.get("parse_source") or {}
        f = ps.get("file")
        if not f or not f.lower().endswith(".pdf"):
            continue
        fp = _abs(root, u.get("folder"), f)
        if not os.path.exists(fp):
            continue
        discrete = {str(e.get("number")) for e in (u.get("exhibits") or []) if e.get("number") is not None}
        emb = sorted({n for n in portfolio_exhibit_numbers(fp) if n not in discrete}, key=lambda x: int(x))
        if emb:
            u["embedded_exhibits"] = emb
    return proposal


def _matter_depo_base(cur, ten, matter_id):
    """Resolve the matter's Depositions/ dir under /mnt/praesidium (which the
    depo workers mount — the legacy share is NOT visible to them, so bundle
    files must be copied here before the DAG can parse them)."""
    cur.execute(
        "SELECT m.matter_name, c.client_name FROM matters m "
        "LEFT JOIN clients c ON c.id = m.client_id AND TRIM(c.tenant_id)=TRIM(%s) "
        "WHERE m.id = CAST(%s AS uuid) AND TRIM(m.tenant_id)=TRIM(%s)",
        (ten, matter_id, ten))
    row = cur.fetchone()
    client = (row[1] if row else None) or "_"
    matter = (row[0] if row else None) or "_"
    return os.path.join("/mnt/praesidium", ten, "matters", client, matter, "Depositions")


def _copy_in(src, dest_dir):
    """Copy src into dest_dir (created if needed), de-duping name. Returns the
    destination path, or None if the source is missing."""
    import shutil
    if not src or not os.path.exists(src):
        return None
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(src)
    dest = os.path.join(dest_dir, base)
    if os.path.abspath(src) == os.path.abspath(dest):
        return dest
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        n = 2
        while os.path.exists(os.path.join(dest_dir, f"{stem} ({n}){ext}")):
            n += 1
        dest = os.path.join(dest_dir, f"{stem} ({n}){ext}")
    shutil.copy2(src, dest)
    return dest


def _probe_dur(p):
    """Duration (seconds) of a media file via ffprobe; 0.0 if unknown."""
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", p], capture_output=True, text=True, timeout=180)
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _concat_video(segments, dest):
    """Stitch the ordered reporter segments AND transcode to a browser-playable
    H.264/AAC MP4 — court-reporter depos are MPEG-1/.mpg which HTML5 <video>
    cannot decode. Writes to a .tmp, VERIFIES the output duration against the
    source before promoting to the final .mp4 — so an interrupted/killed encode
    never leaves a truncated file that looks complete. Returns the .mp4 or None."""
    import subprocess
    segs = [s for s in segments if s and os.path.isfile(s)]
    if not segs:
        return None
    if not dest.lower().endswith(".mp4"):
        dest = os.path.splitext(dest)[0] + ".mp4"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".tmp"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    listfile = None
    if len(segs) == 1:
        cmd += ["-i", segs[0]]
    else:
        listfile = dest + ".concat.txt"
        with open(listfile, "w") as fh:
            for s in segs:
                fh.write("file '%s'\n" % s.replace("'", "'\\''"))
        cmd += ["-f", "concat", "-safe", "0", "-i", listfile]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "25",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", "-f", "mp4", tmp]   # explicit fmt: .tmp ext
    try:
        rc = subprocess.run(cmd, timeout=21600).returncode   # depos can be many hours
    finally:
        if listfile:
            try:
                os.remove(listfile)
            except OSError:
                pass
    src = sum(_probe_dur(s) for s in segs)
    out = _probe_dur(tmp)
    if rc == 0 and out > 0 and (src <= 0 or abs(out - src) < max(20, src * 0.02)):
        os.replace(tmp, dest)        # atomic promote
        return dest
    logger.warning("transcode verify FAILED dest=%s rc=%s src=%.0fs out=%.0fs", dest, rc, src, out)
    try:
        os.remove(tmp)
    except OSError:
        pass
    return None


def _safe(name):
    """Filesystem-safe folder name from a deponent name."""
    s = re.sub(r'[\\/:*?"<>|]+', " ", (name or "").strip()).strip()
    return re.sub(r"\s+", " ", s) or "Deposition"


def _register_document(cur, ten, matter_id, path, doc_type, user_id):
    """Register a file as a DMS document (idempotent on storage_path) so it
    shows in the matter's DMS tree (which walks disk + overlays documents by
    storage_path) and is viewable via /dms/document/{id}/stream. Returns id."""
    import mimetypes
    if not path or not os.path.isfile(path):
        return None
    cur.execute("SELECT id::text FROM documents WHERE storage_path=%s "
                "AND TRIM(tenant_id)=TRIM(%s)", (path, ten))
    row = cur.fetchone()
    if row:
        return row[0]
    fname = os.path.basename(path)
    mime = mimetypes.guess_type(path)[0] or "application/pdf"
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    cur.execute(
        "INSERT INTO documents "
        "  (id, tenant_id, matter_id, filename, original_filename, file_name, "
        "   title, mime_type, file_size, storage_path, document_type, doc_type, "
        "   status, ocr_status, created_by, created_at, updated_at) "
        "VALUES (gen_random_uuid(), %s, CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, "
        "        %s, %s, %s, 'active', 'pending', %s, NOW(), NOW()) "
        "RETURNING id::text",
        (ten, matter_id, fname, fname, fname, fname, mime, size, path,
         doc_type, doc_type, user_id))
    return cur.fetchone()[0]


def _register_dms_index(cur, ten, path, folder_root):
    """Register a file into the DMS indexing pipeline (dms_documents, pending).
    The periodic sweep (jobs.dms_sweep_job.run_sweep) then drives the full
    hybrid pipeline: extract -> OCR (scanned) -> chunk -> embed (vectors) + ES."""
    import hashlib
    if not path or not os.path.isfile(path):
        return
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for blk in iter(lambda: fh.read(1 << 20), b""):
                h.update(blk)
        size = os.path.getsize(path)
    except OSError:
        return
    cur.execute(
        "INSERT INTO dms_documents "
        "  (id, tenant_id, file_path, folder_root, file_hash, file_size_bytes, "
        "   extraction_status, source, updated_at) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, 'pending', "
        "        'deposition_exhibit', NOW()) "
        "ON CONFLICT (tenant_id, file_path) DO NOTHING",
        (ten, path, folder_root, h.hexdigest(), size))


def _enqueue_extract_fast(ten):
    """Deterministically kick the tenant DMS indexing pipeline for the newly
    registered exhibits. extract_fast scans pending dms_documents and auto-chains
    extract -> OCR (scanned) -> parse -> geometry_segment (chunk) -> embed
    (vectors) + ES = full hybrid. (run_sweep is skipped here because it defers
    when another tenant job is active.)"""
    import uuid as _uuid
    import redis as _redis
    import psycopg2
    from rq import Queue
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    jid = str(_uuid.uuid4())
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = True
    try:
        cur = conn.cursor()
        # if a batch is already running/queued for this tenant it will pick up our
        # just-registered pending docs — don't pile on a second 30k-file scan.
        cur.execute(
            "SELECT 1 FROM dms_scan_jobs WHERE TRIM(tenant_id)=%s "
            "AND job_type IN ('extract_fast','extract','extract_ocr','parse') "
            "AND status IN ('queued','running') LIMIT 1", (ten,))
        if cur.fetchone():
            return "deferred:active-batch"
        cur.execute(
            "INSERT INTO dms_scan_jobs (id, tenant_id, job_type, status, queued_at) "
            "VALUES (CAST(%s AS uuid), %s, 'extract_fast', 'queued', NOW())", (jid, ten))
    finally:
        conn.close()
    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    Queue("default", connection=_redis.Redis.from_url(url)).enqueue(
        "jobs.dms_extract_job.run_extract_fast", jid, job_timeout=7200, result_ttl=86400)
    return jid


def _worker_root(root):
    """Workers mount /mnt/legacy (same legacy-pool data as web's /mnt/legacy-local);
    translate so a finalize job running on a worker can read the source files."""
    if root and root.startswith("/mnt/legacy-local"):
        alt = "/mnt/legacy" + root[len("/mnt/legacy-local"):]
        if os.path.isdir(alt):
            return alt
    return root


def _link_exhibit(cur, ten, matter_id, tr, sid, matter_root, number, label, path, user_id):
    """Register the exhibit file as a DMS document (viewer + witness folder), wire
    it into the DMS hybrid index, and link it to the transcript."""
    doc_id = _register_document(cur, ten, matter_id, path, "deposition_exhibit", user_id)
    _register_dms_index(cur, ten, path, matter_root)
    cur.execute(
        "INSERT INTO deposition_exhibit_links "
        "  (tenant_id, matter_id, transcript_id, session_id, document_id, "
        "   document_source, exhibit_number, exhibit_label, notes, marked_by, created_by) "
        "VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, %s, %s, %s, %s, "
        "        'bundle_ingest', %s)",
        (ten, matter_id, tr, sid, (doc_id or None), ("dms" if doc_id else "depo_file"),
         str(number or ""), label, path, user_id))
    return doc_id


def finalize_unit(tenant_id, matter_id, transcript_id, session_id, parse_path,
                  unit, root, user_id=None):
    """RQ job — one per unit, so units finalize IN PARALLEL across the worker pool.
    dispatch() already created the session + registered the transcript (parsing is
    underway in the depo DAG); this does the heavy I/O: stitch the video and
    copy/register/link/index the exhibits. Within the unit, the video stitch and
    the exhibit copies run concurrently in a thread pool."""
    import psycopg2
    from concurrent.futures import ThreadPoolExecutor
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    from modules.depositions.jobs.depo_dag import seed_sync
    ten = (tenant_id or "").strip()
    root = _worker_root(root)
    folder = unit.get("folder")
    deponent = unit.get("deponent") or "Deposition"
    want_video = unit.get("with_video", True)
    want_exhibits = unit.get("with_exhibits", True)
    videos = sorted(unit.get("video") or [], key=lambda v: v.get("order") or 0) if want_video else []

    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        base = _matter_depo_base(cur, ten, matter_id)
        matter_root = os.path.dirname(base)
        unit_dir = os.path.join(base, _safe(deponent))
        ex_dir = os.path.join(unit_dir, "Exhibits")

        # --- exhibit file copies in parallel (fast) ---
        copied = []
        if want_exhibits:
            with ThreadPoolExecutor(max_workers=8) as pool:
                ex_futs = [(e, pool.submit(_copy_in, _abs(root, folder, e["file"]), ex_dir))
                           for e in (unit.get("exhibits") or []) if e.get("file")]
                for e, fut in ex_futs:
                    try:
                        copied.append((e, fut.result()))
                    except Exception:
                        copied.append((e, None))

        # --- video -> hand off to a SEPARATE transcode stage so the transcript
        #     and exhibits land immediately, not behind a multi-hour ffmpeg run ---
        if videos:
            seg_abs = [_abs(root, folder, v["file"]) for v in videos]
            combined = os.path.join(unit_dir, _safe(deponent) + " - video.mp4")
            cur.execute("UPDATE deposition_transcripts SET has_video=true, "
                        "video_status='queued', updated_at=now() WHERE id=CAST(%s AS uuid)",
                        (transcript_id,))
            conn.commit()
            try:
                _enqueue_transcode(ten, matter_id, transcript_id, seg_abs, combined)
            except Exception as e:
                logger.warning("transcode enqueue failed for %s: %s", transcript_id, e)

        seen, ex_rows, emb_rows = set(), 0, 0
        if want_exhibits:
            for e, dest in copied:
                _link_exhibit(cur, ten, matter_id, transcript_id, session_id, matter_root,
                              e.get("number"), e["file"], dest or _abs(root, folder, e["file"]), user_id)
                n = _exnum(e.get("number"))
                if n:
                    seen.add(n)
                ex_rows += 1
            try:
                for pe in extract_portfolio_exhibits(parse_path, ex_dir, seen):
                    _link_exhibit(cur, ten, matter_id, transcript_id, session_id, matter_root,
                                  pe["number"], "Exhibit " + pe["number"], pe["path"], user_id)
                    emb_rows += 1
            except Exception as ee:
                logger.warning("portfolio exhibit extraction failed for %s: %s", transcript_id, ee)
        conn.commit()

        if ex_rows + emb_rows:
            try:
                _enqueue_extract_fast(ten)
            except Exception as e:
                logger.warning("dms extract_fast enqueue failed: %s", e)
        return {"transcript_id": transcript_id, "video_queued": bool(videos),
                "exhibits": ex_rows + emb_rows, "exhibits_embedded": emb_rows}
    finally:
        conn.close()


def transcode_video(tenant_id, matter_id, transcript_id, segments, dest):
    """RQ job — the dedicated transcode stage. Stitch + transcode the depo video
    to a browser-playable H.264/AAC MP4, attach it, and trigger forced-align.
    Tracks deposition_transcripts.video_status (queued->transcoding->ready/failed)
    so the viewer can show progress. Idempotent + retryable."""
    import psycopg2
    import redis as _redis
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    from modules.depositions.jobs.depo_dag import seed_sync
    ten = (tenant_id or "").strip()
    # one transcode per transcript at a time — prevents duplicate encoders racing
    # on the same output file if the job is enqueued/retried more than once.
    rds = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    lock = "depo_transcode_lock:" + str(transcript_id)
    if not rds.set(lock, "1", nx=True, ex=21600):
        logger.info("transcode already running for %s — skip", transcript_id)
        return {"transcript_id": transcript_id, "skipped": "locked"}
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = True
    cur = conn.cursor()
    try:
        # idempotent: already transcoded?
        cur.execute("SELECT video_path FROM deposition_transcripts WHERE id=CAST(%s AS uuid)",
                    (transcript_id,))
        row = cur.fetchone()
        if row and row[0] and row[0].lower().endswith(".mp4") and os.path.isfile(row[0]):
            cur.execute("UPDATE deposition_transcripts SET video_status='ready' "
                        "WHERE id=CAST(%s AS uuid)", (transcript_id,))
            return {"transcript_id": transcript_id, "already": "ready"}
        cur.execute("UPDATE deposition_transcripts SET video_status='transcoding', "
                    "updated_at=now() WHERE id=CAST(%s AS uuid)", (transcript_id,))
        try:
            mp4 = _concat_video(segments, dest)
        except Exception:
            logger.exception("transcode failed for %s", transcript_id)
            mp4 = None
        if not mp4 or not os.path.isfile(mp4):
            cur.execute("UPDATE deposition_transcripts SET video_status='failed', "
                        "updated_at=now() WHERE id=CAST(%s AS uuid)", (transcript_id,))
            return {"transcript_id": transcript_id, "error": "transcode failed"}
        cur.execute("UPDATE deposition_transcripts SET video_path=%s, has_video=true, "
                    "video_status='ready', updated_at=now() WHERE id=CAST(%s AS uuid)",
                    (mp4, transcript_id))
        try:
            seed_sync(ten, transcript_id)
        except Exception as e:
            logger.warning("seed_sync after transcode failed for %s: %s", transcript_id, e)
        return {"transcript_id": transcript_id, "video_path": mp4}
    finally:
        conn.close()
        try:
            rds.delete(lock)
        except Exception:
            pass


def _enqueue_transcode(ten, matter_id, transcript_id, segments, dest):
    import redis as _redis
    from rq import Queue
    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    Queue("ediscovery", connection=_redis.Redis.from_url(url)).enqueue(
        "modules.depositions.services.bundle_ingest.transcode_video",
        ten, matter_id, transcript_id, segments, dest,
        job_timeout="21600", result_ttl=3600)


def _enqueue_finalize(ten, matter_id, transcript_id, session_id, parse_path, unit, root, user_id):
    import redis as _redis
    from rq import Queue
    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    q = Queue("ediscovery", connection=_redis.Redis.from_url(url))
    job = q.enqueue("modules.depositions.services.bundle_ingest.finalize_unit",
                    ten, matter_id, transcript_id, session_id, parse_path, unit, root, user_id,
                    job_timeout="2h", result_ttl=3600)
    return job.id


def dispatch(tenant_id, matter_id, root, proposal, user_id=None):
    """Fast coordinator. Per unit: create the session, copy the (small) parse
    source, register the transcript (seeds the depo DAG so parsing starts NOW),
    register its DMS doc — then enqueue a background finalize_unit job (video +
    exhibits). Those finalize jobs run IN PARALLEL across the ediscovery worker
    pool, and the depo DAG parses all transcripts in parallel too. Returns at once
    so the UI shows the depositions immediately."""
    import psycopg2
    from modules.ediscovery.jobs.geometry_intake import _db_kwargs
    from modules.depositions.jobs.depo_dag import register_transcript, enqueue_pipeline
    ten = (tenant_id or "").strip()
    results = []
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    try:
        cur = conn.cursor()
        base = _matter_depo_base(cur, ten, matter_id)
        for unit in (proposal.get("units") or []):
            ps = unit.get("parse_source") or {}
            folder = unit.get("folder")
            deponent = unit.get("deponent") or "Deposition"
            if not ps.get("file"):
                results.append({"deponent": deponent, "skipped": "no parse_source"})
                continue
            src_parse = _abs(root, folder, ps["file"])
            if not os.path.exists(src_parse):
                results.append({"deponent": deponent, "error": f"missing {src_parse}"})
                continue
            unit_dir = os.path.join(base, _safe(deponent))
            parse_path = _copy_in(src_parse, unit_dir)  # small; must exist before the DAG parses it
            has_video = bool(unit.get("with_video", True) and (unit.get("video") or []))

            cur.execute(
                "INSERT INTO viaticum_sessions (tenant_id, matter_id, session_name, "
                "session_type, status) VALUES (%s, CAST(%s AS uuid), %s, 'deposition', "
                "'active') RETURNING id", (ten, matter_id, deponent))
            sid = cur.fetchone()[0]
            conn.commit()

            reg = register_transcript(ten, parse_path, session_id=sid, matter_id=matter_id,
                                      deponent=deponent, has_video=has_video, seed=True)
            tr = reg["transcript_id"]
            _register_document(cur, ten, matter_id, parse_path, "deposition_transcript", user_id)
            conn.commit()

            fjob = _enqueue_finalize(ten, matter_id, tr, sid, parse_path, unit, root, user_id)
            results.append({"deponent": deponent, "session_id": sid, "transcript_id": tr,
                            "format": reg.get("format"), "finalize_job": fjob})

        # seed the depo DAG drain (parse/segment/embed) across the depo workers
        job = enqueue_pipeline(ten, matter_id)
        return {"units": results, "pipeline_job": job, "parallel": True}
    finally:
        conn.close()
