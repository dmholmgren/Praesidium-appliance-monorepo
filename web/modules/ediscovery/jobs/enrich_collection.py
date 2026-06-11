"""
enrich_collection.py -- eDiscovery explode-and-map, ENRICHMENT half (v2, parallel).

Three-tier text acquisition:

  Tier 0  PRODUCED text. A compliant production ships per-document extracted
          text (TEXT/ folders and/or a TextPath field in the DAT). Matched by
          file stem / Bates. text_source='produced'. Zero extraction cost and
          the defensible source (it is what the producing party certified).
  Tier 1  NATIVE extraction. email body / pdf / docx / xlsx / csv / html /
          libreoffice legacy -- in a process pool. Dedup by file_hash BEFORE
          dispatch: each unique blob is extracted once, the result is fanned
          out to all duplicate rows.
  Tier 2  OCR is NOT performed inline (v1 double-OCR'd: pytesseract here, then
          tesseract again in the geometry OCR lane). Images and no-text PDFs
          without produced text are DEFERRED: processing_status='ocr_pending',
          stage ocr='pending'. The geometry OCR lane builds the searchable-PDF
          rendition under renditions/ocr/; a SECOND enrich pass (orchestrator
          stage 'enrich2') extracts text from that rendition
          (text_source='rendition_ocr') and completes language.

Resume marker unchanged: stage language='done'. Deferred docs lack it, so the
second pass selects exactly them. Whole job remains idempotent / restartable.

Concurrency: ProcessPoolExecutor (spawn -- max_tasks_per_child requires a
non-fork context), ENRICH_WORKERS (default 8), max_tasks_per_child=50 (bounds
leaks), per-task SIGALRM timeout (ENRICH_EXTRACT_TIMEOUT, default 300s).
BrokenProcessPool (segfaulting parser) is survived: pool is rebuilt, pending
tasks resubmitted; a task that breaks the pool repeatedly is retried isolated
(1-worker one-shot pool) and then marked failed. Sliding submission window
bounds memory.

DB: main process owns the single connection. Writes are batched
(ENRICH_BATCH, default 100 results): one execute_values UPDATE..FROM(VALUES)
for document rows, one execute_values upsert for ledger rows, one commit.

Conventions (house): psycopg2 + DATABASE_URL; CAST(%s AS type); TRIM(tenant_id);
native_path/text_path RELATIVE to the collection root; per-item error isolation.

CLI (inside praesidium-web):
  python -m modules.ediscovery.jobs.enrich_collection \
      --tenant <uuid> --collection <uuid> [--limit N] [--redo] [--dry-run]
  python -m modules.ediscovery.jobs.enrich_collection \
      --tenant <uuid> --matter <uuid> [...]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
import logging
import multiprocessing
import os
import signal
import subprocess
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tuning
# ---------------------------------------------------------------------------

MIN_PDF_TEXT_CHARS = 40      # below this a PDF page-set is treated as scanned
MIN_PRODUCED_CHARS = 20      # produced text shorter than this falls through
LO_TIMEOUT = 120             # libreoffice headless conversion ceiling (s)
TEXT_STORE_CAP = 20_000_000  # never write a single text file larger than this

ENRICH_WORKERS = max(1, int(os.environ.get("ENRICH_WORKERS", "8")))
EXTRACT_TIMEOUT = int(os.environ.get("ENRICH_EXTRACT_TIMEOUT", "300"))
BATCH_COMMIT = max(1, int(os.environ.get("ENRICH_BATCH", "100")))
MAX_TASKS_PER_CHILD = 50
SUBMIT_WINDOW = ENRICH_WORKERS * 4
MAX_TASK_RETRIES = 3         # pool-break resubmits before isolation

STAGES = ("text", "ocr", "language")
AV_EXTS = {".mp3", ".wav", ".m4a", ".wma", ".aac", ".ogg", ".flac", ".amr",
           ".mp4", ".mov", ".avi", ".wmv", ".mkv", ".m4v", ".3gp", ".mpg",
           ".mpeg"}
DELEGATE_DOCTYPES = {"pdf", "word", "spreadsheet", "html", "image", "email", "other"}

# DAT field names that carry a produced-text path (Relativity/Concordance)
DAT_TEXT_KEYS = ["Text Path", "TextPath", "TEXTPATH", "TEXT_PATH", "TEXT",
                 "FULLTEXT", "Extracted Text", "ExtractedText", "TextLink",
                 "OCRPATH", "OCR Path"]


# ---------------------------------------------------------------------------
# db (mirror preserve_collection)
# ---------------------------------------------------------------------------

def _db_kwargs() -> dict:
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    p = urlparse(raw)
    return {
        "dbname": p.path.lstrip("/") or "praesidium",
        "user": p.username or "praesidium",
        "password": p.password or "",
        "host": p.hostname or "172.28.0.1",
        "port": str(p.port or 5432),
    }


def _connect():
    import psycopg2
    conn = psycopg2.connect(**_db_kwargs())
    conn.autocommit = False
    return conn


LEDGER_SQL = """
    INSERT INTO ediscovery_stage_status
        (tenant_id, document_id, collection_id, stage, state, attempt,
         worker_id, input_hash, started_at, finished_at, duration_ms,
         error_class, error_message, updated_at)
    VALUES %s
    ON CONFLICT (tenant_id, document_id, stage) DO UPDATE SET
        state=EXCLUDED.state, attempt=ediscovery_stage_status.attempt+1,
        collection_id=EXCLUDED.collection_id, worker_id=EXCLUDED.worker_id,
        input_hash=EXCLUDED.input_hash, finished_at=now(),
        duration_ms=EXCLUDED.duration_ms, error_class=EXCLUDED.error_class,
        error_message=EXCLUDED.error_message, updated_at=now()
"""
LEDGER_TPL = ("(%s, %s::uuid, %s::uuid, %s, %s, 1, %s, %s, now(), now(), "
              "%s, %s, %s, now())")

DOC_UPDATE_SQL = """
    UPDATE ediscovery_documents d SET
        extracted_text   = v.extracted_text,
        text_source      = v.text_source,
        text_path        = v.text_path,
        ocr_performed    = v.ocr_performed::boolean,
        ocr_status       = v.ocr_status,
        detected_language = v.detected_language,
        translation_status = v.translation_status,
        processing_status  = v.processing_status
    FROM (VALUES %s)
      AS v(id, extracted_text, text_source, text_path, ocr_performed,
           ocr_status, detected_language, translation_status, processing_status)
    WHERE d.id = v.id::uuid
"""

DEFER_UPDATE_SQL = """
    UPDATE ediscovery_documents
    SET processing_status='ocr_pending',
        extracted_text=NULL, text_source=NULL, text_path=NULL,
        ocr_performed=false, ocr_status='pending',
        detected_language=NULL, translation_status=NULL
    WHERE id = ANY(%s::uuid[])
"""


# ---------------------------------------------------------------------------
# text extractors -- each returns plain text (may be "")
# ---------------------------------------------------------------------------

class _Stripper(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._buf = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._buf.append(data)

    def text(self) -> str:
        import re
        return re.sub(r"\n{3,}", "\n\n", "\n".join(self._buf)).strip()


def _decode(data: bytes) -> str:
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:
        pass
    return data.decode("utf-8", "replace")


def _strip_html(data: bytes) -> str:
    p = _Stripper()
    try:
        p.feed(_decode(data))
    except Exception:
        return _decode(data)
    return p.text()


def _email_body_text(path: Path) -> str:
    """Body text from a stored .eml (text/plain preferred, html stripped fallback)."""
    if path.suffix.lower() == ".msg":
        import extract_msg
        m = extract_msg.openMsg(str(path))
        try:
            hdr = []
            for label, v in (("From", m.sender), ("To", m.to), ("Cc", m.cc),
                             ("Date", str(m.date or "")), ("Subject", m.subject)):
                if v:
                    hdr.append("%s: %s" % (label, v))
            body = (m.body or "")
            if not (body or "").strip() and getattr(m, "htmlBody", None):
                raw = m.htmlBody
                body = _strip_html(raw if isinstance(raw, bytes)
                                   else str(raw).encode("utf-8", "replace"))
            return ("\n".join(hdr) + "\n\n" + (body or "")).strip()
        finally:
            try:
                m.close()
            except Exception:
                pass
    import email
    from email import policy
    with open(path, "rb") as f:
        msg = email.message_from_binary_file(f, policy=policy.default)
    plains, htmls = [], []
    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = (part.get_content_disposition() or "")
            if disp == "attachment":
                continue
            try:
                payload = part.get_payload(decode=True) or b""
            except Exception:
                continue
            if ctype == "text/plain":
                plains.append(_decode(payload))
            elif ctype == "text/html":
                htmls.append(_strip_html(payload))
    else:
        try:
            payload = msg.get_payload(decode=True) or b""
        except Exception:
            payload = b""
        if msg.get_content_type() == "text/html":
            htmls.append(_strip_html(payload))
        else:
            plains.append(_decode(payload))
    body = "\n".join(t for t in plains if t.strip()) or \
           "\n".join(t for t in htmls if t.strip())
    hdr = []
    for label, key in (("From", "from"), ("To", "to"), ("Cc", "cc"),
                       ("Date", "date"), ("Subject", "subject")):
        v = msg.get(key.capitalize()) or msg.get(label)
        if v:
            hdr.append("%s: %s" % (label, v))
    head = "\n".join(hdr)
    return (head + "\n\n" + body).strip() if head else body.strip()


def _pdf_text(path: Path) -> str:
    import fitz
    out = []
    with fitz.open(path) as doc:
        for page in doc:
            out.append(page.get_text("text"))
    return "\n".join(out).strip()


def _docx_text(path: Path) -> str:
    import docx
    d = docx.Document(str(path))
    parts = [p.text for p in d.paragraphs]
    for tbl in d.tables:
        for row in tbl.rows:
            parts.append("\t".join(c.text for c in row.cells))
    return "\n".join(t for t in parts if t and t.strip()).strip()


def _xlsx_text(path: Path) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append("# %s" % ws.title)
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                out.append("\t".join(cells))
    wb.close()
    return "\n".join(out).strip()


def _csv_text(path: Path) -> str:
    import csv
    out = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f):
            out.append("\t".join(row))
    return "\n".join(out).strip()


def _libreoffice_to(path: Path, target: str) -> str:
    """Headless convert to txt|csv|pdf in a scratch profile, read the result."""
    with tempfile.TemporaryDirectory() as td:
        profile = os.path.join(td, "louser")
        cmd = [
            "libreoffice", "--headless", "--norestore",
            "-env:UserInstallation=file://%s" % profile,
            "--convert-to", target, "--outdir", td, str(path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=LO_TIMEOUT, check=False)
        except Exception:
            return ""
        produced = list(Path(td).glob("*." + target.split(":")[0]))
        if not produced:
            return ""
        out = produced[0]
        if target.startswith("txt"):
            return _decode(out.read_bytes()).strip()
        if target.startswith("csv"):
            return _csv_text(out)
        if target.startswith("pdf"):
            return _pdf_text(out)
    return ""


def _office_legacy_text(path: Path, doc_type: str) -> str:
    ext = path.suffix.lower()
    if doc_type == "spreadsheet" or ext in (".xls", ".ods"):
        return _libreoffice_to(path, "csv:Text - txt - csv (StarCalc)") or _libreoffice_to(path, "txt")
    return _libreoffice_to(path, "txt:Text") or _libreoffice_to(path, "pdf")


def detect_lang(text: str) -> Optional[str]:
    t = (text or "").strip()
    if len(t) < 20:
        return None
    try:
        from langdetect import detect, DetectorFactory
        DetectorFactory.seed = 0
        return detect(t)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# pool worker -- runs in a spawned child; per-task SIGALRM timeout
# ---------------------------------------------------------------------------

def _worker_init():
    def _on_alarm(signum, frame):
        raise TimeoutError("extract timeout after %ss" % EXTRACT_TIMEOUT)
    signal.signal(signal.SIGALRM, _on_alarm)


def _worker_extract(task: dict) -> dict:
    """task: {key, kind: produced|rendition|native, abs_path, doc_type}
    returns {key, status: ok|defer|thin_produced|error, text, src, used_ocr,
             lang, error_class, error_message, duration_ms}"""
    t0 = time.time()
    out = {"key": task["key"], "status": "ok", "text": "", "src": "extract",
           "used_ocr": False, "lang": None,
           "error_class": None, "error_message": None, "duration_ms": 0}
    signal.alarm(EXTRACT_TIMEOUT)
    try:
        kind = task["kind"]
        p = Path(task["abs_path"])
        if not p.exists():
            raise FileNotFoundError(str(p))

        if kind == "produced":
            text = _decode(p.read_bytes()).strip()
            if len(text) < MIN_PRODUCED_CHARS:
                # produced text is garbage/empty -> caller falls back
                out["status"] = "thin_produced"
                out["text"] = text
            else:
                out["text"], out["src"] = text, "produced"

        elif kind == "rendition":
            text = _pdf_text(p)
            out["text"], out["src"], out["used_ocr"] = text, "rendition_ocr", True

        else:  # native -- extension is authoritative; doc_type is a hint
            dt = task["doc_type"]
            ext = p.suffix.lower()
            if ext in AV_EXTS:
                # audio/video: no text lane (transcription is future work)
                out["text"], out["src"] = "", "media"
            elif dt == "email" or ext in (".eml", ".msg"):
                out["text"], out["src"] = _email_body_text(p), "email_body"
            elif ext == ".pdf":
                text = _pdf_text(p)
                if len(text) >= MIN_PDF_TEXT_CHARS:
                    out["text"], out["src"] = text, "extract"
                else:
                    out["status"] = "defer"     # scanned -> OCR lane, not here
                    out["text"] = text
            elif ext == ".docx":
                out["text"] = _docx_text(p)
            elif dt == "word":
                out["text"] = _office_legacy_text(p, dt)
            elif ext == ".xlsx":
                out["text"] = _xlsx_text(p)
            elif ext == ".csv":
                out["text"] = _csv_text(p)
            elif dt == "spreadsheet":
                out["text"] = _office_legacy_text(p, dt)
            elif dt == "html" or ext in (".html", ".htm"):
                out["text"] = _strip_html(p.read_bytes())
            else:
                out["text"] = _office_legacy_text(p, dt)

        if out["status"] == "ok":
            out["lang"] = detect_lang(out["text"])
    except Exception as e:
        out["status"] = "error"
        out["error_class"] = type(e).__name__
        out["error_message"] = str(e)[:1000]
    finally:
        signal.alarm(0)
    if out["text"]:
        out["text"] = out["text"].replace("\x00", "")   # PG text rejects NUL
    out["duration_ms"] = int((time.time() - t0) * 1000)
    return out


# ---------------------------------------------------------------------------
# produced-text index (Tier 0)
# ---------------------------------------------------------------------------

# Relativity DAT delimiters (Concordance standard) -- mirrors ingest_collection
DAT_FIELD_SEP = "\u00fe"    # thorn -- field separator
DAT_QUOTE_CHAR = "\u0014"   # DC4   -- text qualifier (not always present)


def _parse_dat_file(dat_path: str) -> list:
    """Parse a Relativity/Concordance .dat load file -> list of row dicts.
    Self-contained copy of ingest_collection.parse_dat_file (this job runs
    script-style; modules.* is not importable)."""
    rows: list = []
    lines = None
    for enc in ("utf-8-sig", "windows-1252", "utf-8"):
        try:
            with open(dat_path, encoding=enc, errors="replace") as f:
                lines = f.read().splitlines()
            break
        except Exception:
            continue
    if not lines:
        return rows
    headers = [h.strip(DAT_QUOTE_CHAR).strip() for h in lines[0].split(DAT_FIELD_SEP)]
    for line in lines[1:]:
        if not line.strip():
            continue
        vals = [v.strip(DAT_QUOTE_CHAR) for v in line.split(DAT_FIELD_SEP)]
        while len(vals) < len(headers):
            vals.append("")
        rows.append(dict(zip(headers, vals)))
    return rows


def _normalise_rel(raw: str) -> str:
    if not raw:
        return ""
    p = raw.replace("\\", "/").lstrip("./")
    return p.lstrip("/")


def _build_produced_text_index(coll_root: Path) -> dict:
    """stem(lower) -> absolute path of the produced .txt.
    Sources: (a) any *.txt under originals/ inside a directory whose path
    contains 'text' (case-insensitive) -- the TEXT/ volume convention;
    (b) TextPath-style fields in any parsed .dat load file."""
    idx: dict = {}
    base = coll_root / "originals"
    if base.exists():
        for txt in base.rglob("*.txt"):
            try:
                parent = str(txt.parent).lower()
            except Exception:
                continue
            if "text" in os.path.basename(parent) or "/text" in parent.replace("\\", "/"):
                idx.setdefault(txt.stem.lower(), str(txt))

    # DAT TextPath fields
    if base.exists():
        for dat in list(base.rglob("*.dat"))[:10]:
            try:
                rows = _parse_dat_file(str(dat))
            except Exception:
                continue
            for row in rows:
                rawp = ""
                for k in DAT_TEXT_KEYS:
                    if row.get(k):
                        rawp = row[k]
                        break
                if not rawp:
                    continue
                rel = _normalise_rel(rawp)
                cand = None
                for prefix in ("originals/unpacked/", "originals/", ""):
                    c = coll_root / (prefix + rel)
                    if c.exists():
                        cand = c
                        break
                if cand is None:
                    continue
                idx.setdefault(Path(rel).stem.lower(), str(cand))
    return idx


def _find_rendition(coll_root: Path, doc_id: str) -> Optional[str]:
    for sub in ("renditions/ocr", "working/ocr"):
        c = coll_root / sub / ("%s.pdf" % doc_id)
        if c.exists():
            return str(c)
    return None


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def _resolve_collection(cur, tenant, collection_id):
    cur.execute(
        """SELECT id, matter_id, collection_name, storage_path
           FROM ediscovery_collections
           WHERE TRIM(tenant_id)=%s AND id=CAST(%s AS uuid)""",
        (tenant, str(collection_id)))
    row = cur.fetchone()
    if not row:
        raise ValueError("collection %s not found for tenant" % collection_id)
    return {"id": row[0], "matter_id": row[1],
            "collection_name": row[2], "storage_path": row[3]}


def _select_units(cur, tenant, collection_id, redo, limit):
    done_clause = "" if redo else (
        " AND NOT EXISTS (SELECT 1 FROM ediscovery_stage_status s "
        "  WHERE s.tenant_id=d.tenant_id AND s.document_id=d.id "
        "    AND s.stage='language' AND s.state='done')")
    sql = (
        "SELECT d.id::text, d.doc_type, d.native_path, d.file_hash, "
        "       d.is_attachment, d.file_name, d.bates_begin "
        "FROM ediscovery_documents d "
        "WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid) "
        "  AND d.native_path IS NOT NULL" + done_clause +
        " ORDER BY d.family_id NULLS FIRST, d.is_attachment, d.attachment_index NULLS FIRST")
    if limit:
        sql += " LIMIT %d" % int(limit)
    cur.execute(sql, (tenant, str(collection_id)))
    return cur.fetchall()


def _store_text(coll_root: Path, file_hash: str, text: str, dry_run: bool) -> Optional[str]:
    if not text:
        return None
    rel = "text/%s.txt" % file_hash
    if dry_run:
        return rel
    dest = coll_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    blob = text.encode("utf-8", "replace")[:TEXT_STORE_CAP]
    tmp = dest.with_suffix(".txt.part")
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, dest)
    return rel


class _Flusher:
    """Accumulates doc updates / ledger rows / deferrals; flushes in batches."""

    def __init__(self, conn, tenant, collection_id, worker_id, dry_run):
        self.conn, self.cur = conn, conn.cursor()
        self.tenant, self.cid, self.wid = tenant, str(collection_id), worker_id
        self.dry = dry_run
        self.doc_rows, self.ledger_rows, self.defer_ids = [], [], []
        self.n_since = 0

    def doc(self, row):       # tuple matching DOC_UPDATE_SQL VALUES
        self.doc_rows.append(row)

    def ledger(self, doc_id, stage, state, input_hash=None, duration_ms=None,
               error_class=None, error_message=None):
        self.ledger_rows.append(
            (self.tenant, doc_id, self.cid, stage, state, self.wid, input_hash,
             duration_ms, error_class,
             (error_message.replace("\x00", "")[:4000] if error_message else None)))

    def defer(self, doc_id):
        self.defer_ids.append(doc_id)

    def tick(self):
        self.n_since += 1
        if self.n_since >= BATCH_COMMIT:
            self.flush()

    def flush(self):
        if self.dry:
            self.doc_rows, self.ledger_rows, self.defer_ids = [], [], []
            self.n_since = 0
            return
        from psycopg2.extras import execute_values
        try:
            if self.doc_rows:
                execute_values(self.cur, DOC_UPDATE_SQL, self.doc_rows,
                               template=None, page_size=200)
            if self.defer_ids:
                self.cur.execute(DEFER_UPDATE_SQL, (self.defer_ids,))
            if self.ledger_rows:
                execute_values(self.cur, LEDGER_SQL, self.ledger_rows,
                               template=LEDGER_TPL, page_size=500)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.doc_rows, self.ledger_rows, self.defer_ids = [], [], []
            self.n_since = 0


def enrich_collection(tenant_id: str, collection_id: str,
                      redo: bool = False, limit: int = 0,
                      dry_run: bool = False) -> dict:
    tenant = tenant_id.strip()
    worker_id = os.environ.get("HOSTNAME", "enrich")
    s = {"produced_ok": 0, "text_ok": 0, "rendition_ok": 0, "ocr_deferred": 0,
         "empty": 0, "errors": 0, "dup_fanout": 0, "lang": {},
         "workers": ENRICH_WORKERS, "dry_run": dry_run}

    conn = _connect()
    try:
        cur = conn.cursor()
        coll = _resolve_collection(cur, tenant, collection_id)
        coll_root = Path(coll["storage_path"])
        units = _select_units(cur, tenant, collection_id, redo, limit)
        produced_idx = _build_produced_text_index(coll_root)
        logger.info("enrich %s (%s): %d unit(s) workers=%d produced_idx=%d root=%s dry=%s",
                    coll["collection_name"], collection_id, len(units),
                    ENRICH_WORKERS, len(produced_idx), coll_root, dry_run)

        fl = _Flusher(conn, tenant, collection_id, worker_id, dry_run)

        # ---- plan: per-doc tier-0/rendition tasks; hash-grouped native tasks
        tasks = {}          # key -> task dict
        key_docs = {}       # key -> [(doc_id, doc_type, file_hash)]
        native_grp = {}     # file_hash -> key (dedupe)

        for (doc_id, doc_type, native_path, file_hash, _is_att,
             file_name, bates_begin) in units:
            stem = Path(file_name or native_path).stem.lower()
            produced = produced_idx.get(stem) or \
                (produced_idx.get(str(bates_begin).lower()) if bates_begin else None)
            dt = doc_type if doc_type in DELEGATE_DOCTYPES else "other"

            if produced:
                key = "p:" + doc_id
                tasks[key] = {"key": key, "kind": "produced",
                              "abs_path": produced, "doc_type": dt,
                              "fallback_native": str(coll_root / native_path)}
                key_docs[key] = [(doc_id, dt, file_hash)]
                continue

            rend = _find_rendition(coll_root, doc_id)
            if rend:
                key = "r:" + doc_id
                tasks[key] = {"key": key, "kind": "rendition",
                              "abs_path": rend, "doc_type": dt}
                key_docs[key] = [(doc_id, dt, file_hash)]
                continue

            if dt == "image":
                # never OCR inline -- straight to the OCR lane
                fl.defer(doc_id)
                fl.ledger(doc_id, "text", "skipped", input_hash=file_hash)
                fl.ledger(doc_id, "ocr", "pending", input_hash=file_hash)
                fl.ledger(doc_id, "language", "pending", input_hash=file_hash)
                s["ocr_deferred"] += 1
                fl.tick()
                continue

            hkey = file_hash or ("nohash:" + doc_id)
            if hkey in native_grp:
                key_docs[native_grp[hkey]].append((doc_id, dt, file_hash))
                s["dup_fanout"] += 1
            else:
                key = "n:" + hkey
                native_grp[hkey] = key
                tasks[key] = {"key": key, "kind": "native",
                              "abs_path": str(coll_root / native_path),
                              "doc_type": dt}
                key_docs[key] = [(doc_id, dt, file_hash)]

        fl.flush()  # image deferrals

        # ---- result application
        def apply(res):
            key = res["key"]
            task = tasks[key]
            docs = key_docs[key]
            status = res["status"]

            if status == "thin_produced" and task.get("fallback_native"):
                # produced text is thin -> try native (stub kept as last resort)
                task["kind"] = "native"
                task["thin_text"] = res.get("text") or ""
                task["produced_src"] = task["abs_path"]
                task["abs_path"] = task["fallback_native"]
                task.pop("fallback_native", None)
                return task  # caller resubmits

            if status == "error" and task.get("thin_text") is not None:
                # native fallback failed but a produced stub exists -- the stub
                # IS the produced record (e.g. AV-native placeholder text)
                res = {"key": key, "status": "ok", "text": task["thin_text"],
                       "src": "produced", "used_ocr": False, "lang": None,
                       "duration_ms": res.get("duration_ms")}
                status = "ok"

            text = (res.get("text") or "").strip()
            lang = res.get("lang")
            dur = res.get("duration_ms")

            for (doc_id, dt, file_hash) in docs:
                if status == "error":
                    s["errors"] += 1
                    fl.ledger(doc_id, "text", "failed", input_hash=file_hash,
                              duration_ms=dur, error_class=res["error_class"],
                              error_message=res["error_message"])
                elif status == "defer":
                    s["ocr_deferred"] += 1
                    fl.defer(doc_id)
                    fl.ledger(doc_id, "text", "skipped", input_hash=file_hash,
                              duration_ms=dur)
                    fl.ledger(doc_id, "ocr", "pending", input_hash=file_hash)
                    fl.ledger(doc_id, "language", "pending", input_hash=file_hash)
                else:  # ok
                    src = res["src"]
                    used_ocr = bool(res.get("used_ocr"))
                    if src == "produced":
                        # point text_path at the produced file itself (no copy)
                        psrc = task.get("produced_src", task["abs_path"])
                        text_rel = (os.path.relpath(psrc, coll_root)
                                    if text else None)
                        s["produced_ok"] += 1
                    else:
                        if "stored_rel" not in res:   # store once per result
                            res["stored_rel"] = _store_text(
                                coll_root, file_hash or doc_id, text, dry_run)
                        text_rel = res["stored_rel"]
                        if src == "rendition_ocr":
                            s["rendition_ok"] += 1
                        elif text:
                            s["text_ok"] += 1
                    if not text:
                        s["empty"] += 1
                    tstatus = None
                    if text:
                        tstatus = "not_required" if (lang in (None, "en")) else "pending"
                    fl.doc((doc_id, text or None, src, text_rel,
                            used_ocr,
                            ("completed" if used_ocr and text else
                             ("empty" if used_ocr else None)),
                            lang, tstatus, "enriched"))
                    # text stage: 'done' when text came from the doc itself
                    # (native/produced); 'skipped' when it came via OCR rendition
                    fl.ledger(doc_id, "text",
                              ("skipped" if src == "rendition_ocr"
                               else ("done" if text else "skipped")),
                              input_hash=file_hash, duration_ms=dur)
                    fl.ledger(doc_id, "ocr",
                              "done" if used_ocr else "skipped",
                              input_hash=file_hash)
                    fl.ledger(doc_id, "language", "done", input_hash=file_hash)
                    if lang:
                        s["lang"][lang] = s["lang"].get(lang, 0) + 1
                fl.tick()
            return None

        # ---- pool: spawn ctx (max_tasks_per_child needs non-fork),
        # sliding submission window, broken-pool survival with isolation.
        todo = list(tasks.values())
        retries: dict = {}
        ctx = multiprocessing.get_context("spawn")

        def new_pool():
            return cf.ProcessPoolExecutor(
                max_workers=ENRICH_WORKERS, mp_context=ctx,
                initializer=_worker_init,
                max_tasks_per_child=MAX_TASKS_PER_CHILD)

        def run_isolated(t):
            iso = cf.ProcessPoolExecutor(max_workers=1, mp_context=ctx,
                                         initializer=_worker_init)
            try:
                return iso.submit(_worker_extract, t).result(
                    timeout=EXTRACT_TIMEOUT + 60)
            except Exception as e:
                return {"key": t["key"], "status": "error", "text": "",
                        "src": "extract", "used_ocr": False, "lang": None,
                        "error_class": type(e).__name__,
                        "error_message": "isolated: " + str(e)[:900],
                        "duration_ms": None}
            finally:
                iso.shutdown(wait=False, cancel_futures=True)

        pool = new_pool()
        inflight = {}
        try:
            it = iter(todo)
            exhausted = False
            while inflight or not exhausted:
                while not exhausted and len(inflight) < SUBMIT_WINDOW:
                    try:
                        t = next(it)
                    except StopIteration:
                        exhausted = True
                        break
                    inflight[pool.submit(_worker_extract, t)] = t
                if not inflight:
                    break

                done, _ = cf.wait(inflight, return_when=cf.FIRST_COMPLETED)
                broken = False
                broken_tasks = []
                resubs = []
                for fut in done:
                    t = inflight.pop(fut)
                    try:
                        res = fut.result()
                    except BrokenProcessPool:
                        broken = True
                        broken_tasks.append(t)
                        continue
                    except Exception as e:
                        res = {"key": t["key"], "status": "error", "text": "",
                               "src": "extract", "used_ocr": False,
                               "lang": None,
                               "error_class": type(e).__name__,
                               "error_message": str(e)[:1000],
                               "duration_ms": None}
                    r = apply(res)
                    if r is not None:
                        resubs.append(r)

                if broken:
                    # pool is dead: every still-inflight future is lost too
                    survivors = list(inflight.values()) + broken_tasks
                    inflight.clear()
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    pool = new_pool()
                    logger.warning("worker pool broke; rebuilt, requeueing %d task(s)",
                                   len(survivors))
                    for t in survivors:
                        retries[t["key"]] = retries.get(t["key"], 0) + 1
                        if retries[t["key"]] >= MAX_TASK_RETRIES:
                            r = apply(run_isolated(t))
                            if r is not None:
                                resubs.append(r)
                        else:
                            resubs.append(t)

                for t in resubs:
                    inflight[pool.submit(_worker_extract, t)] = t
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

        fl.flush()
        return s
    finally:
        conn.close()


def _collections_for_matter(cur, tenant, matter_id):
    cur.execute(
        """SELECT DISTINCT ON (storage_path) id, collection_name, storage_path
           FROM ediscovery_collections
           WHERE TRIM(tenant_id)=%s AND matter_id=CAST(%s AS uuid)
           ORDER BY storage_path, collection_name""",
        (tenant, str(matter_id)))
    return [(r[0], r[1]) for r in cur.fetchall()]


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--collection")
    g.add_argument("--matter")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import json
    if args.collection:
        out = enrich_collection(args.tenant, args.collection,
                                redo=args.redo, limit=args.limit, dry_run=args.dry_run)
        logger.info("DONE %s", json.dumps(out))
    else:
        conn = _connect(); cur = conn.cursor()
        colls = _collections_for_matter(cur, args.tenant.strip(), args.matter)
        conn.close()
        for cid, cname in colls:
            logger.info("=== collection %s (%s) ===", cname, cid)
            try:
                out = enrich_collection(args.tenant, str(cid),
                                        redo=args.redo, limit=args.limit, dry_run=args.dry_run)
                logger.info("DONE %s -> %s", cname, json.dumps(out))
            except Exception as e:
                logger.error("collection %s failed: %s", cname, e)


if __name__ == "__main__":
    main()
