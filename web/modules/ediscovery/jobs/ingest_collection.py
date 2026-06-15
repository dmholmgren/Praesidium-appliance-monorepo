"""
Document ingestion pipeline — RQ job running on MAIN-PRD-PROC-01.

Handles all source types:
  - client_collection_dms: copies from DMS file share to eDiscovery originals/
  - client_collection_dedicated: reads from dedicated eDiscovery volume
  - opposing_production: preserves original archive + unpacks
  - internal_collection / third_party_subpoena: same as opposing

Every source type gets the originals/ preservation treatment.
All files hashed at ingestion. Originals set read-only after processing.
"""

import hashlib
import logging
import os
import shutil
import zipfile
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from rq import get_current_job

from core.audit import write_audit
from core.db.base import TenantSession, get_session_factory
def get_storage_service(tenant_id):
    """Storage service stub — opposing_production uses direct file paths, not this service."""
    return None
from modules.ediscovery.models.collections import (
    EdiscoveryCollection, CollectionStatus, SourceType,
)
from modules.ediscovery.models.documents import EdiscoveryDocument
from modules.ediscovery.services.text_extraction import extract_text, extract_email_metadata, extract_email_attachments
from modules.ediscovery.services.embedding import generate_embedding
from modules.ediscovery.services.normalization import normalize as normalize_text
from modules.ediscovery.jobs.split_zip_reassemble import detect_split_zip, reassemble_split_zip
from modules.ediscovery.jobs.folder_decompose import scan_folder_source, build_child_specs

logger = logging.getLogger(__name__)
# ── Real-time progress logging ──────────────────────────────────────────
def _parse_database_url():
    """Extract psycopg2 connection kwargs from DATABASE_URL."""
    from urllib.parse import urlparse
    raw = os.environ.get("DATABASE_URL", "")
    # Strip SQLAlchemy dialect prefix if present
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if raw.startswith(prefix):
            raw = "postgresql://" + raw[len(prefix):]
            break
    parsed = urlparse(raw)
    return {
        "dbname":   parsed.path.lstrip("/") or "praesidium",
        "user":     parsed.username or "praesidium",
        "password": parsed.password or "",
        "host":     parsed.hostname or "172.28.0.1",
        "port":     str(parsed.port or 5432),
    }


def log_progress(tenant_id, collection_id, message, level="info"):
    """Write a progress entry to ediscovery_ingestion_log for live UI polling."""
    try:
        import psycopg2
        conn = psycopg2.connect(**_parse_database_url())
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ediscovery_ingestion_log (tenant_id, collection_id, level, message) "
                "VALUES (%s, %s::uuid, %s, %s)",
                (tenant_id, str(collection_id), level, message[:2000])
            )
        conn.close()
    except Exception as e:
        logger.warning("log_progress failed: %s", e)


def setup_job_logger(log_path, job_name="ingestion"):
    """Create a file logger that writes to a per-job log file."""
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    job_logger = logging.getLogger(f"praesidium.job.{job_name}")
    job_logger.setLevel(logging.DEBUG)
    # Remove existing handlers to avoid duplicates on re-run
    job_logger.handlers = []
    fh = logging.FileHandler(log_path, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    job_logger.addHandler(fh)
    # Also propagate to root logger so docker logs still works
    job_logger.propagate = True
    return job_logger



# File extensions we know how to process
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".msg", ".eml", ".txt", ".rtf", ".csv", ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp",
}

ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".rar", ".pst"}

EMAIL_EXTENSIONS = {".msg", ".eml"}

def extract_pst_to_eml(pst_path: str, output_dir: str) -> list[str]:
    """
    Extract a PST file using pffexport (forensic-grade, from pff-tools).

    pffexport flags:
      -f text   = plain text output (not HTML/RTF)
      -m all    = allocated items + orphan + recovered
      -l log    = audit log — chain of custody artifact
      -t target = output directory basename

    No timeout — large PSTs (4-7GB) can take 3-4 hours.
    Audit log is preserved alongside extracted items for defensibility.

    Falls back to readpst if pffexport is not installed.

    Returns list of absolute paths to extracted files.
    """
    import subprocess

    os.makedirs(output_dir, exist_ok=True)
    pst_basename = Path(pst_path).stem

    # pffexport creates <target>.export/ directory
    pst_output = os.path.join(output_dir, pst_basename)
    audit_log = os.path.join(output_dir, f"{pst_basename}_pffexport_audit.log")

    try:
        result = subprocess.run(
            [
                "pffexport",
                "-f", "text",       # plain text output
                "-m", "all",        # allocated + orphan + recovered
                "-l", audit_log,    # audit log = chain of custody
                "-t", pst_output,   # target directory basename
                pst_path,
            ],
            capture_output=True,
            text=True,
            # No timeout — large PSTs need hours, not minutes
        )

        if result.returncode != 0:
            logger.error(
                "pffexport failed for %s (rc=%d): stderr=%s stdout=%s",
                pst_path, result.returncode,
                result.stderr[:500], result.stdout[:500],
            )
            # Fall back to readpst
            return _extract_pst_readpst_fallback(pst_path, output_dir)
        else:
            logger.info(
                "pffexport completed for %s: %s",
                pst_path, result.stdout.strip()[:200] if result.stdout else "OK",
            )

    except FileNotFoundError:
        logger.warning(
            "pffexport not found — falling back to readpst for %s", pst_path,
        )
        return _extract_pst_readpst_fallback(pst_path, output_dir)

    # Parse audit log for item counts (defensibility)
    if os.path.exists(audit_log):
        try:
            with open(audit_log, "r", encoding="utf-8", errors="replace") as f:
                log_lines = f.readlines()
            item_count = sum(1 for l in log_lines if "Exporting" in l or "exported" in l.lower())
            error_count = sum(1 for l in log_lines if "error" in l.lower() or "unable" in l.lower())
            logger.info(
                "PST %s audit: %d log lines, ~%d items exported, %d errors/warnings",
                pst_basename, len(log_lines), item_count, error_count,
            )
        except Exception as e:
            logger.warning("Could not parse pffexport audit log: %s", e)

    # Collect all extracted files from .export directory
    # pffexport creates: <target>.export/, <target>.orphans/, <target>.recovered/
    extracted = []
    for suffix in (".export", ".orphans", ".recovered"):
        scan_dir = pst_output + suffix
        if os.path.isdir(scan_dir):
            for root, dirs, files in os.walk(scan_dir):
                for fname in files:
                    abs_path = os.path.join(root, fname)
                    # Skip ItemValues.txt debug dumps and zero-byte files
                    if fname == "ItemValues.txt":
                        continue
                    if os.path.getsize(abs_path) == 0:
                        continue
                    extracted.append(abs_path)

    logger.info(
        "PST %s: pffexport extracted %d files to %s",
        os.path.basename(pst_path), len(extracted), pst_output,
    )
    return extracted


def _extract_pst_readpst_fallback(pst_path: str, output_dir: str) -> list[str]:
    """Fallback PST extraction using readpst (no audit log, has timeout issues)."""
    import subprocess

    pst_basename = Path(pst_path).stem
    pst_output = os.path.join(output_dir, pst_basename + "_readpst")
    os.makedirs(pst_output, exist_ok=True)

    try:
        result = subprocess.run(
            ["readpst", "-e", "-o", pst_output, pst_path],
            capture_output=True,
            text=True,
            timeout=14400,  # 4 hour timeout (was 1h, caused truncation)
        )
        if result.returncode != 0:
            logger.error("readpst fallback failed for %s (rc=%d): %s",
                         pst_path, result.returncode, result.stderr)
        else:
            logger.info("readpst fallback extracted %s: %s",
                        pst_path, result.stdout.strip()[:200] if result.stdout else "OK")
    except FileNotFoundError:
        logger.error("Neither pffexport nor readpst found. PST %s skipped.", pst_path)
        return []
    except subprocess.TimeoutExpired:
        logger.error("readpst fallback timed out on %s (>4h)", pst_path)
        return []

    extracted = []
    for root, dirs, files in os.walk(pst_output):
        for fname in files:
            extracted.append(os.path.join(root, fname))

    logger.info("PST %s (readpst fallback): extracted %d files", pst_basename, len(extracted))
    return extracted


def expand_pst_files(files_to_process: list[str], unpacked_path: str) -> list[str]:
    """
    Scan files_to_process for .pst files, extract them via readpst,
    and return a new list with PSTs replaced by their extracted children.
    Non-PST files pass through unchanged.
    """
    expanded = []
    for fpath in files_to_process:
        if Path(fpath).suffix.lower() == ".pst":
            logger.info("Expanding PST: %s", fpath)
            extracted = extract_pst_to_eml(fpath, unpacked_path)
            if extracted:
                expanded.extend(extracted)
                logger.info(
                    "PST %s expanded to %d files",
                    os.path.basename(fpath), len(extracted),
                )
            else:
                logger.warning(
                    "PST %s: no files extracted, keeping in list",
                    os.path.basename(fpath),
                )
                expanded.append(fpath)
        else:
            expanded.append(fpath)
    return expanded




# -- intake throughput tuning ------------------------------------------------
INGEST_HASH_WORKERS = max(1, int(os.environ.get("INGEST_HASH_WORKERS", "12")))
INGEST_EXTRACT_WORKERS = max(1, int(os.environ.get("INGEST_EXTRACT_WORKERS", "8")))
# Intake is hash + stat + insert; text extraction, normalization and
# embeddings belong to the parallel DAG stages. INGEST_INLINE_TEXT=1
# restores the old inline behavior for collections run without the DAG.
INGEST_INLINE_TEXT = os.environ.get("INGEST_INLINE_TEXT", "0") == "1"

_HASH_CACHE: dict = {}


def _hash_one(path: str):
    try:
        s = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1048576), b""):
                s.update(chunk)
        return s.hexdigest()
    except Exception:
        return None


def prehash_files(paths, workers: int = 0) -> int:
    """Fill the sha256 cache in parallel ahead of the ingest loops.
    Hashing 788K files / 250 GB serially inside the per-doc loop was a
    dominant intake cost; this front-loads it across a process pool."""
    import concurrent.futures as _cf
    import multiprocessing as _mp
    todo = [p for p in paths if p not in _HASH_CACHE]
    if not todo:
        return 0
    w = workers or INGEST_HASH_WORKERS
    ctx = _mp.get_context("spawn")
    n = 0
    with _cf.ProcessPoolExecutor(max_workers=w, mp_context=ctx) as ex:
        for p, h in zip(todo, ex.map(_hash_one, todo, chunksize=64)):
            if h:
                _HASH_CACHE[p] = h
                n += 1
    return n


def _extract_zip_parallel(archive_path: str, unpacked_path: str) -> list:
    """Threaded ZIP extraction (INGEST_EXTRACT_WORKERS, default 8).
    zlib inflation releases the GIL, so threads scale to the I/O ceiling;
    each thread owns its own ZipFile handle. Directories are pre-created
    to avoid extract-time races."""
    import concurrent.futures as _cf
    with zipfile.ZipFile(archive_path, "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
    dirs = {os.path.dirname(n) for n in names}
    for d in sorted(dirs):
        if d:
            os.makedirs(os.path.join(unpacked_path, d), exist_ok=True)

    def _worker(shard):
        out = []
        with zipfile.ZipFile(archive_path, "r") as z:
            for n in shard:
                z.extract(n, unpacked_path)
                out.append(os.path.join(unpacked_path, n))
        return out

    w = INGEST_EXTRACT_WORKERS
    shards = [names[i::w] for i in range(w)]
    files = []
    with _cf.ThreadPoolExecutor(max_workers=w) as ex:
        for res in ex.map(_worker, shards):
            files.extend(res)
    return files


def compute_sha256(file_path: str) -> str:
    """Compute SHA-256 hash of a file (consults the prehash cache first)."""
    cached = _HASH_CACHE.get(file_path)
    if cached:
        return cached
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    h = sha256.hexdigest()
    _HASH_CACHE[file_path] = h
    return h


def create_collection_directories(storage_path: str) -> None:
    """Create the standard directory layout for a collection."""
    for subdir in [
        "originals/as_received",
        "originals/unpacked",
        "working",
        "productions",
    ]:
        os.makedirs(os.path.join(storage_path, subdir), exist_ok=True)


def copy_dms_to_originals(
    dms_source_path: str,
    originals_unpacked_path: str,
    storage_service,
    tenant_id: str,
) -> list[str]:
    """
    Copy client documents from DMS file share to eDiscovery originals/unpacked/.
    Uses direct filesystem walk -- storage_service is kept as parameter for
    backward compatibility but is not used.
    Returns list of absolute paths to files in originals/unpacked/.
    """
    copied_files = []
    for root, dirs, files in os.walk(dms_source_path):
        if "working" in root:
            continue
        for fname in files:
            src_abs = os.path.join(root, fname)
            rel_path = os.path.relpath(src_abs, dms_source_path)
            dst_path = os.path.join(originals_unpacked_path, rel_path)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            if src_abs != dst_path:
                shutil.copy2(src_abs, dst_path)
            copied_files.append(dst_path)
    return copied_files


def preserve_archive(
    archive_path: str,
    as_received_path: str,
    unpacked_path: str,
) -> tuple[str, list[str]]:
    """
    Copy original archive to as_received/, extract to unpacked/.
    Returns (archive_hash, list_of_extracted_file_paths).
    """
    # Copy archive to as_received (skip if already there)
    archive_name = os.path.basename(archive_path)
    preserved_archive = os.path.join(as_received_path, archive_name)
    if os.path.realpath(archive_path) != os.path.realpath(preserved_archive):
        shutil.copy2(archive_path, preserved_archive)

    # Extract based on type
    extracted_files = []
    ext = Path(archive_path).suffix.lower()

    if ext == ".zip":
        # Resume-aware extraction: a sidecar manifest records the archive
        # signature, its sha256, and the extracted relpaths. A verified hit
        # skips both re-extraction and the full archive re-hash on requeue.
        import json as _json
        manifest_path = os.path.join(
            os.path.dirname(unpacked_path.rstrip("/")),
            ".extract-manifest-" + archive_name + ".json")
        try:
            _st = os.stat(preserved_archive)
            sig = {"size": _st.st_size, "mtime": int(_st.st_mtime)}
        except OSError:
            sig = None
        cached = None
        if sig is not None:
            try:
                with open(manifest_path) as _mf:
                    cached = _json.load(_mf)
            except Exception:
                cached = None
        if (cached and cached.get("sig") == sig
                and cached.get("files") and cached.get("archive_hash")):
            files = [os.path.join(unpacked_path, f) for f in cached["files"]]
            probe = files[:100] + files[-100:]
            if all(os.path.exists(p) for p in probe):
                _HASH_CACHE[preserved_archive] = cached["archive_hash"]
                logger.info(
                    "Extraction resume: manifest verified for %s (%d files); "
                    "skipping re-extract and archive re-hash",
                    archive_name, len(files))
                return cached["archive_hash"], files
            logger.info("Extraction manifest stale for %s; re-extracting",
                        archive_name)
        archive_hash = compute_sha256(preserved_archive)
        extracted_files = _extract_zip_parallel(archive_path, unpacked_path)
        try:
            with open(manifest_path, "w") as _mf:
                _json.dump({
                    "sig": sig,
                    "archive_hash": archive_hash,
                    "files": [os.path.relpath(p, unpacked_path)
                              for p in extracted_files],
                }, _mf)
        except Exception as _me:
            logger.warning("extract manifest write failed (non-blocking): %s", _me)
        return archive_hash, extracted_files

    archive_hash = compute_sha256(preserved_archive)
    if ext in (".tar", ".tgz") or archive_path.endswith(".tar.gz"):
        with tarfile.open(archive_path, "r:*") as tf:
            tf.extractall(unpacked_path)
            extracted_files = [
                os.path.join(unpacked_path, m.name)
                for m in tf.getmembers()
                if m.isfile()
            ]
    else:
        # For PST and other formats we can't directly unpack here,
        # copy the raw file to unpacked/ for downstream processing
        dst = os.path.join(unpacked_path, archive_name)
        shutil.copy2(archive_path, dst)
        extracted_files = [dst]

    return archive_hash, extracted_files


def set_readonly(directory: str) -> None:
    """Set all files in a directory tree to read-only."""
    for root, dirs, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            os.chmod(fpath, 0o444)
        for dname in dirs:
            dpath = os.path.join(root, dname)
            os.chmod(dpath, 0o555)


def get_mime_type(file_path: str) -> str:
    """Determine MIME type from extension."""
    import mimetypes
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def get_doc_type(file_path: str) -> str:
    """Classify document type from extension."""
    ext = Path(file_path).suffix.lower()
    type_map = {
        ".pdf": "pdf", ".doc": "word", ".docx": "word",
        ".xls": "spreadsheet", ".xlsx": "spreadsheet",
        ".ppt": "presentation", ".pptx": "presentation",
        ".msg": "email", ".eml": "email",
        ".txt": "text", ".rtf": "text", ".csv": "data",
        ".html": "html", ".htm": "html",
        ".jpg": "image", ".jpeg": "image", ".png": "image",
        ".tif": "image", ".tiff": "image", ".gif": "image",
        ".bmp": "image",
    }
    return type_map.get(ext, "other")


# ---------------------------------------------------------------------------
# DAT / Opticon load file parser — Relativity opposing production support
# ---------------------------------------------------------------------------

# Relativity DAT delimiters (Concordance standard)
DAT_FIELD_SEP  = "þ"   # þ  (thorn)   — field separator
DAT_QUOTE_CHAR = ""   # ¶  (pilcrow) — text qualifier (not always present)

def parse_dat_file(dat_path: str) -> list[dict]:
    """
    Parse a Relativity/Concordance .dat load file.
    Returns list of dicts keyed by header field names.
    Handles UTF-8 with BOM and Windows-1252 encodings.
    """
    rows = []
    for enc in ("utf-8-sig", "windows-1252", "utf-8"):
        try:
            with open(dat_path, encoding=enc, errors="replace") as f:
                lines = f.read().splitlines()
            break
        except Exception:
            continue
    else:
        return rows

    if not lines:
        return rows

    headers = [h.strip(DAT_QUOTE_CHAR).strip() for h in lines[0].split(DAT_FIELD_SEP)]

    for line in lines[1:]:
        if not line.strip():
            continue
        vals = [v.strip(DAT_QUOTE_CHAR) for v in line.split(DAT_FIELD_SEP)]
        # Pad short rows
        while len(vals) < len(headers):
            vals.append("")
        rows.append(dict(zip(headers, vals)))

    return rows


def _normalise_dat_path(raw: str) -> str:
    """Normalise a DAT file path to a POSIX relative path under originals/unpacked/."""
    if not raw:
        return ""
    # Strip leading .\ or ./
    p = raw.lstrip('./')
    # Normalise backslashes
    p = p.replace("\\", "/")
    return "originals/unpacked/" + p.lstrip('/')



def _parse_csv_load_file(csv_path: str) -> list[dict]:
    """
    Parse a Concordance CSV load file (e.g. Volume001.csv).
    Returns list of dicts keyed by header field names — same format as parse_dat_file().
    Handles UTF-8 with BOM and Windows-1252 encodings.
    """
    import csv as _csv
    rows = []
    for enc in ("utf-8-sig", "windows-1252", "utf-8"):
        try:
            with open(csv_path, encoding=enc, errors="replace", newline="") as f:
                reader = _csv.DictReader(f)
                for row in reader:
                    # Strip whitespace from keys and values
                    cleaned = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k}
                    if cleaned:
                        rows.append(cleaned)
            break
        except Exception:
            continue

    logger.info("CSV load file parsed: %d rows from %s", len(rows), csv_path)
    return rows


def build_dat_document_map(collection_storage_path: str) -> dict:
    """
    Scan a collection's unpacked directory for a .dat load file.
    Parse it and return a dict keyed by Bates number with paths for:
        image_path   — relative path to IMAGE file (file_path)
        text_path    — relative path to TEXT companion
        native_path  — relative path to NATIVE file
        metadata     — dict of all DAT fields for this record

    Returns empty dict if no .dat found or parse fails.
    """
    import glob

    # Find .dat or .csv load file anywhere under originals/unpacked/
    pattern_dat = os.path.join(collection_storage_path, "originals", "unpacked", "**", "*.dat")
    pattern_csv = os.path.join(collection_storage_path, "originals", "unpacked", "**", "*.csv")
    dat_files = glob.glob(pattern_dat, recursive=True)
    csv_files = glob.glob(pattern_csv, recursive=True)

    load_file_path = None
    load_file_format = None

    if dat_files:
        load_file_path = dat_files[0]
        load_file_format = "dat"
    elif csv_files:
        load_file_path = csv_files[0]
        load_file_format = "csv"
    else:
        return {}

    logger.info("Load-file-aware ingest: found %s load file %s", load_file_format, load_file_path)

    if load_file_format == "dat":
        rows = parse_dat_file(load_file_path)
    else:
        rows = _parse_csv_load_file(load_file_path)

    if not rows:
        logger.warning("Load file parse returned no rows: %s", load_file_path)
        return {}

    # Detect field names — DAT headers vary by producing party
    # Common aliases for each logical field
    BATES_KEYS    = ["Production::Begin Bates", "Begin Bates", "BegBates", "BEGBATES",
                     "Beg Prod", "Begin Production",
                     "BEGDOC#", "DOCID"]
    TEXT_KEYS     = ["Text Precedence", "TEXT_PATH", "Extracted Text Path",
                     "ExtractedTextPath", "TextPath", "TEXTLINK"]
    NATIVE_KEYS   = ["FILE_PATH", "Native File Path", "NativeFilePath",
                     "NATIVE_PATH", "Natives", "DOCLINK"]
    SUBJECT_KEYS  = ["Subject", "Email Subject", "SUBJECT", "ESUBJECT"]
    FROM_KEYS     = ["From", "Email From", "FROM", "EAUTHOR"]
    TO_KEYS       = ["To", "Email To", "TO"]
    DATE_KEYS     = ["Sent Date/Time", "Date Sent", "Date Created",
                     "Last Modified Date/Time", "DOCDATE", "DATECREATED", "DATESENT"]
    CUSTODIAN_KEYS = ["All Custodians", "Custodian", "CUSTODIAN"]
    NUMPAGES_KEYS  = ["NUMPAGES", "Page Count", "PageCount"]
    MD5_KEYS       = ["MD5 Hash", "MD5", "MD5HASH", "HASHMD5"]
    FILENAME_KEYS  = ["File Name", "FILENAME", "FileName"]

    def _get(row: dict, keys: list) -> str:
        for k in keys:
            if k in row and row[k]:
                return row[k].strip()
        return ""

    doc_map = {}
    for row in rows:
        bates = _get(row, BATES_KEYS)
        if not bates:
            continue

        doc_map[bates] = {
            "bates_begin":   bates,
            "bates_end":     _get(row, ["Production::End Bates", "End Bates", "EndBates", "ENDDOC#"]) or bates,
            "image_path":    "",   # filled from OPT file or IMAGES/ scan
            "text_path":     _normalise_dat_path(_get(row, TEXT_KEYS)),
            "native_path":   _normalise_dat_path(_get(row, NATIVE_KEYS)),
            "subject":       _get(row, SUBJECT_KEYS),
            "email_from":    _get(row, FROM_KEYS),
            "email_to":      _get(row, TO_KEYS),
            "doc_date_str":  _get(row, DATE_KEYS),
            "custodian":     _get(row, CUSTODIAN_KEYS),
            "confidentiality": _get(row, ["Confidentiality", "CONFIDENTIALITY"]),
            "md5_hash":      _get(row, MD5_KEYS),
            "file_name":     _get(row, FILENAME_KEYS),
            "num_pages":     _get(row, NUMPAGES_KEYS),
            "native_link":   _get(row, ["DOCLINK", "Native File Path", "NativeFilePath", "NATIVE_PATH"]),
            "text_link":     _get(row, ["TEXTLINK", "Extracted Text Path", "ExtractedTextPath", "TEXT_PATH"]),
        }

    # Now scan IMAGES/ to fill image_path for each Bates.
    # O(1) per file via a single uppercased index. (The previous version
    # rebuilt an uppercased list of EVERY Bates record per image file and
    # then scanned it again -- O(files x bates), days of CPU at 788K files.)
    upper_idx = {b.upper(): b for b in doc_map}
    images_dir = os.path.join(collection_storage_path, "originals", "unpacked")
    for root, dirs, files in os.walk(images_dir):
        # Only look in IMAGES directories
        if "IMAGES" not in root.upper() and "IMG" not in root.upper():
            continue
        for fname in files:
            stem = Path(fname).stem.upper()
            matched_bates = upper_idx.get(stem)
            if matched_bates is None and "_" in stem:
                # multi-page image convention: BATES_0001.tif
                matched_bates = upper_idx.get(stem.rsplit("_", 1)[0])
            if matched_bates:
                abs_path = os.path.join(root, fname)
                rel_path = os.path.relpath(abs_path, collection_storage_path)
                doc_map[matched_bates]["image_path"] = rel_path

    # Resolve TEXT companions by Bates-stem match against the on-disk tree.
    # DAT path claims frequently don't survive re-rooting (an extra top-level
    # folder from zip extraction, flattening, etc.) -- verify, else stem-match.
    txt_idx = {}
    for root, dirs, files in os.walk(images_dir):
        for fname in files:
            if fname.lower().endswith(".txt"):
                txt_idx.setdefault(
                    Path(fname).stem.upper(),
                    os.path.relpath(os.path.join(root, fname), collection_storage_path),
                )
    _fixed_txt = 0
    for _bates, _rec in doc_map.items():
        _tp = _rec.get("text_path") or ""
        if _tp and os.path.exists(os.path.join(collection_storage_path, _tp)):
            continue
        _hit = txt_idx.get(_bates.upper())
        if _hit:
            _rec["text_path"] = _hit
            _fixed_txt += 1
    if _fixed_txt:
        logger.info("DAT-aware ingest: stem-matched %d text companions on disk", _fixed_txt)

    logger.info("DAT-aware ingest: mapped %d documents from load file", len(doc_map))
    return doc_map


def _parse_dat_date(date_str: str):
    """Parse common Relativity date formats to a Python date."""
    if not date_str:
        return None
    from datetime import datetime as dt
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def match_dms_document(
    session: TenantSession,
    tenant_id: str,
    file_path: str,
    file_hash: str,
) -> Optional[int]:
    """
    Try to match an ingested file to an existing DMS document record.
    Match on file hash first (strongest), then file path.
    Returns DMS document ID or None.
    """
    from modules.dms.models import Document

    # Try hash match first
    dms_doc = session.query(Document).filter(
        Document.tenant_id == tenant_id,
        Document.file_hash == file_hash,
    ).first()
    if dms_doc:
        return dms_doc.id

    # Try path match (file_name match as fallback)
    file_name = os.path.basename(file_path)
    dms_doc = session.query(Document).filter(
        Document.tenant_id == tenant_id,
        Document.file_name == file_name,
    ).first()
    if dms_doc:
        return dms_doc.id

    return None


def _apply_normalization(session, tenant_id, doc_id, extracted_text, doc_type, email_meta):
    """
    Run normalization on extracted_text and write results to DB.
    Sets: normalized_text, normalized_metadata, detected_language,
          text_source, ocr_status, translation_status.
    Inserts email_segments rows for emails.
    Returns the normalize() result dict.
    """
    import json as _json
    import uuid as _uuid
    from sqlalchemy import text as _sa_text

    if not extracted_text:
        # Still set provenance columns even with no text
        session.execute(_sa_text("""
            UPDATE ediscovery_documents
            SET text_source = 'extract',
                ocr_status = CASE WHEN doc_type IN ('pdf', 'image') THEN 'queued' ELSE 'not_needed' END
            WHERE id = CAST(:did AS uuid)
        """), {"did": str(doc_id)})
        return None

    try:
        norm_result = normalize_text(extracted_text, doc_type, email_meta or {})
    except Exception as e:
        logger.warning("normalize() failed for doc %s: %s", doc_id, e)
        return None

    normalized_text = norm_result.get("normalized_text")
    normalized_metadata = norm_result.get("normalized_metadata") or {}
    segments = norm_result.get("segments") or []

    detected_lang = normalized_metadata.get("detected_language", "en")
    translation_status = "not_needed" if detected_lang == "en" else "queued"
    ocr_status = "skipped_has_text" if extracted_text else "queued"
    if doc_type not in ("pdf", "image"):
        ocr_status = "not_needed"

    session.execute(_sa_text("""
        UPDATE ediscovery_documents
        SET normalized_text = :norm_text,
            normalized_metadata = CAST(:norm_meta AS jsonb),
            detected_language = :lang,
            text_source = 'extract',
            ocr_status = :ocr_status,
            translation_status = :trans_status
        WHERE id = CAST(:did AS uuid)
    """), {
        "norm_text": normalized_text,
        "norm_meta": _json.dumps(normalized_metadata),
        "lang": detected_lang,
        "ocr_status": ocr_status,
        "trans_status": translation_status,
        "did": str(doc_id),
    })

    # Insert email segments
    for seg in segments:
        seg_id = str(_uuid.uuid4())
        session.execute(_sa_text("""
            INSERT INTO email_segments
                (id, tenant_id, document_id, segment_index, author,
                 sent_date, normalized_hash, content, is_top, created_at)
            VALUES
                (CAST(:sid AS uuid), :tid, CAST(:did AS uuid), :idx, :author,
                 CAST(:sent AS timestamptz), :nhash, :content, :is_top, now())
        """), {
            "sid": seg_id,
            "tid": tenant_id,
            "did": str(doc_id),
            "idx": seg.get("segment_index", 0),
            "author": (seg.get("author") or "")[:255],
            "sent": seg.get("sent_date"),
            "nhash": (seg.get("normalized_hash") or "")[:64],
            "content": seg.get("content"),
            "is_top": seg.get("is_top", False),
        })

    return norm_result


def ingest_ediscovery_collection(
    tenant_id: str,
    collection_id: int,
    user_id: int,
) -> dict:
    """
    Main ingestion RQ job — dispatched to MAIN-PRD-PROC-01.

    Steps:
    1. Create directory layout
    2. Copy/preserve originals based on source_type
    3. For each file: hash, dedup, extract text, create DB record
    4. Set originals/ to read-only
    5. Update collection stats
    6. Fire drift detection hooks for any transcripts ingested
    """
    job = get_current_job()
    stats = {"total": 0, "processed": 0, "duplicates": 0, "errors": 0}

    session = get_session_factory()()
    try:
        collection = session.query(EdiscoveryCollection).filter(
            EdiscoveryCollection.id == collection_id,
            EdiscoveryCollection.tenant_id == tenant_id,
        ).one()

        # Set up per-job file logger
        log_path = os.path.join(collection.storage_path, "ingestion.log")
        jlog = setup_job_logger(log_path, f"ingest-{collection_id}")
        jlog.info("=" * 60)
        jlog.info("INGESTION JOB STARTED")
        jlog.info("Collection: %s (id=%s)", collection.collection_name or collection.name, collection_id)
        jlog.info("Source: %s", collection.dms_source_path or collection.storage_path)
        jlog.info("Storage: %s", collection.storage_path)
        jlog.info("=" * 60)

        collection.status = "processing"
        session.commit()

        # ── Folder decomposition ────────────────────────────────────
        # If the source is a directory with multiple archives/subfolders,
        # automatically create child collections and enqueue them.
        # The parent becomes a tracker that rolls up child progress.
        raw_source = collection.dms_source_path or ""
        source_paths_check = [p.strip() for p in raw_source.split(",") if p.strip()]

        if (len(source_paths_check) == 1
            and os.path.isdir(source_paths_check[0])
            and collection.source_type in ("opposing_production", "client_documents",
                                            "third_party_subpoena", "internal_collection")):

            scan = scan_folder_source(source_paths_check[0])
            # ── Load-file production guard ──────────────────────────────────
            # A .dat/.opt anywhere under the source means the whole tree is
            # ONE Concordance/Relativity production keyed by a single load
            # file. Never decompose it: per-VOL children have no load file and
            # fall back to a flat walk that double-counts IMAGES + TEXT (the
            # historic 788k-vs-391k doubling). Force a single DAT-aware ingest.
            import glob as _gl_lf
            if (_gl_lf.glob(os.path.join(source_paths_check[0], "**", "*.dat"), recursive=True)
                    or _gl_lf.glob(os.path.join(source_paths_check[0], "**", "*.opt"), recursive=True)):
                if scan["should_decompose"]:
                    jlog.info("Load file present — overriding decompose (was %s); "
                              "ingesting as one DAT-aware collection.", scan["reason"])
                    log_progress(tenant_id, collection_id,
                                 "Load file detected — ingesting as one production "
                                 "(no decompose)")
                scan["should_decompose"] = False
                scan["reason"] = "load_file_present"
            jlog.info("Folder scan: %s — %d archives, %d subfolders, %d loose files, %s total",
                       scan["reason"], len(scan["archives"]), len(scan["subfolders"]),
                       len(scan["loose_files"]),
                       f"{scan['total_size_bytes'] / (1024**3):.1f}GB")
            log_progress(tenant_id, collection_id,
                         f"Scanned source folder: {scan['total_file_count']} files, "
                         f"{scan['total_size_bytes'] / (1024**3):.1f}GB — {scan['reason']}")

            if scan["should_decompose"]:
                child_specs = build_child_specs(scan, collection.collection_name or collection.name)
                jlog.info("Decomposing into %d child collections", len(child_specs))
                log_progress(tenant_id, collection_id,
                             f"Decomposing into {len(child_specs)} sub-collections")

                # Create child collections and enqueue each
                import uuid as _uuid_mod
                from redis import Redis
                from rq import Queue
                REDIS_URL = os.environ.get("REDIS_URL", "redis://10.10.60.12:6379/0")
                redis_conn = Redis.from_url(REDIS_URL)
                q = Queue("ediscovery", connection=redis_conn)

                child_ids = []
                ediscovery_root = os.environ.get("EDISCOVERY_STORAGE_ROOT", "/mnt/ediscovery")

                for spec in child_specs:
                    child_id = str(_uuid_mod.uuid4())
                    child_storage = os.path.join(
                        ediscovery_root, tenant_id, str(collection.matter_id),
                        spec["name"].replace(" ", "_").replace("/", "_")[:100],
                    )
                    from sqlalchemy import text as _sa_text
                    session.execute(_sa_text("""
                        INSERT INTO ediscovery_collections
                            (id, tenant_id, matter_id, collection_name, name,
                             storage_path, source_type, source_party,
                             dms_source_path, status, parent_collection_id,
                             received_by)
                        VALUES
                            (CAST(:cid AS uuid), :tid, CAST(:mid AS uuid), :cname, :cname,
                             :spath, :stype, :sparty,
                             :dms_path, 'collecting', CAST(:parent_id AS uuid),
                             :uid)
                    """), {
                        "cid": child_id,
                        "tid": tenant_id,
                        "mid": str(collection.matter_id),
                        "cname": spec["name"][:255],
                        "spath": child_storage,
                        "stype": collection.source_type,
                        "sparty": collection.source_party,
                        "dms_path": spec["source_path"],
                        "parent_id": str(collection_id),
                        "uid": user_id,
                    })
                    child_ids.append(child_id)

                    jlog.info("  Created child: %s → %s", spec["name"], spec["source_desc"])
                    log_progress(tenant_id, collection_id,
                                 f"Created sub-collection: {spec['name']}")

                session.commit()

                # Enqueue child ingest jobs
                for cid in child_ids:
                    q.enqueue(
                        "modules.ediscovery.jobs.ingest_collection.ingest_ediscovery_collection",
                        tenant_id, cid, user_id,
                        job_timeout="24h",
                    )

                # Enqueue rollup monitor job
                q.enqueue(
                    "modules.ediscovery.jobs.ingest_collection.rollup_parent_collection",
                    tenant_id, str(collection_id), child_ids,
                    job_timeout="48h",
                )

                jlog.info("Decomposition complete — %d child jobs enqueued", len(child_ids))
                log_progress(tenant_id, collection_id,
                             f"Decomposition complete — {len(child_ids)} sub-collections queued for processing",
                             "success")

                # Parent stays in 'processing' — rollup job will set it to review_ready
                # when all children are done.
                return {"status": "decomposed", "children": len(child_ids)}

        # ── Normal ingestion (no decomposition needed) ──────────────        log_progress(tenant_id, collection_id, "Ingestion started — scanning source paths")

        storage_service = get_storage_service(tenant_id)

        # Step 1: Create directory layout
        create_collection_directories(collection.storage_path)
        originals_as_received = collection.originals_as_received_path()
        originals_unpacked = collection.originals_unpacked_path()
        working_dir = collection.working_path()

        # Step 2: Get files into originals/unpacked/ based on source type
        files_to_process = []

        if collection.source_type == "client_collection_dms":
            # Copy from DMS file share to eDiscovery originals
            if not collection.dms_source_path:
                raise ValueError("dms_source_path required for client_collection_dms")
            files_to_process = copy_dms_to_originals(
                dms_source_path=collection.dms_source_path,
                originals_unpacked_path=originals_unpacked,
                storage_service=storage_service,
                tenant_id=tenant_id,
            )
            logger.info(
                "Copied %d files from DMS to originals for collection %d",
                len(files_to_process), collection_id,
            )

        elif collection.source_type in (
            "opposing_production",
            "third_party_subpoena",
            "client_documents",
        ):
            # Preserve original archive, then extract
            raw_source = collection.dms_source_path or collection.storage_path
            # Support comma-separated paths (individual file selection from UI)
            source_paths = [p.strip() for p in raw_source.split(",") if p.strip()]

            def _process_source_entry(entry_path):
                """Process a single file or directory entry."""
                if os.path.isfile(entry_path):
                    # Individual file — process directly
                    ext = Path(entry_path).suffix.lower()
                    if ext in ARCHIVE_EXTENSIONS:
                        archive_hash, extracted = preserve_archive(
                            entry_path, originals_as_received, originals_unpacked,
                        )
                        collection.original_hash = archive_hash
                        collection.original_file_name = os.path.basename(entry_path)
                        files_to_process.extend(extracted)
                        logger.info("Processed archive %s: %d files extracted",
                                    os.path.basename(entry_path), len(extracted))
                        log_progress(tenant_id, collection_id, f"Extracted {os.path.basename(entry_path)}: {len(extracted)} files")
                    else:
                        dst = os.path.join(originals_unpacked, os.path.basename(entry_path))
                        if entry_path != dst:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(entry_path, dst)
                        # Load files are preserved for build_dat_document_map but
                        # are metadata, not documents -- never process as docs.
                        if ext not in {".dat", ".opt", ".lfp", ".log", ".csv"}:
                            files_to_process.append(dst)
                elif os.path.isdir(entry_path):
                    # Directory — walk it, preserving relative structure.
                    # Load files (.dat/.opt/.lfp/.csv) ARE copied into unpacked/
                    # so build_dat_document_map can find them, but are NOT added
                    # to files_to_process (metadata, not documents). Relative
                    # paths prevent basename collisions across subdirectories.
                    _LOAD_FILE_EXTS = {".dat", ".opt", ".lfp", ".log", ".csv"}
                    for root, dirs, files in os.walk(entry_path):
                        if "working" in root or "unpacked" in root or "productions" in root:
                            continue
                        for fname in files:
                            src_abs = os.path.join(root, fname)
                            ext = Path(src_abs).suffix.lower()
                            if ext in ARCHIVE_EXTENSIONS:
                                archive_hash, extracted = preserve_archive(
                                    src_abs, originals_as_received, originals_unpacked,
                                )
                                collection.original_hash = archive_hash
                                collection.original_file_name = os.path.basename(src_abs)
                                files_to_process.extend(extracted)
                            else:
                                rel = os.path.relpath(src_abs, entry_path)
                                dst = os.path.join(originals_unpacked, rel)
                                if src_abs != dst:
                                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                                    shutil.copy2(src_abs, dst)
                                if ext not in _LOAD_FILE_EXTS:
                                    files_to_process.append(dst)
                else:
                    logger.warning("Source path not found: %s", entry_path)

            # ── Split ZIP detection ──────────────────────────────────
            # Relativity exports can be split across multiple ZIP segments
            # (e.g., Production.001.zip, .002.zip, .003.zip). Detect this
            # pattern and reassemble before extraction.
            is_split, split_base, split_segments = detect_split_zip(source_paths)
            if is_split:
                jlog.info("Detected split Relativity ZIP: %s (%d segments)", split_base, len(split_segments))
                log_progress(tenant_id, collection_id,
                             f"Detected split ZIP: {split_base} ({len(split_segments)} segments) — reassembling")
                try:
                    reassembled_path = reassemble_split_zip(
                        split_segments, split_base, originals_as_received,
                        jlog=jlog, log_progress_fn=log_progress,
                        tenant_id=tenant_id, collection_id=str(collection_id),
                    )
                    # Replace source_paths with the single reassembled ZIP
                    source_paths = [reassembled_path]
                    jlog.info("Split ZIP reassembled — proceeding with single archive: %s", reassembled_path)
                except Exception as rze:
                    jlog.error("Split ZIP reassembly failed: %s", rze)
                    log_progress(tenant_id, collection_id,
                                 f"Split ZIP reassembly FAILED: {rze}", "error")
                    raise

            for sp in source_paths:
                jlog.info("Processing source entry: %s", sp)
                _process_source_entry(sp)

            logger.info("Total files after source processing: %d", len(files_to_process))
            log_progress(tenant_id, collection_id, f"Source scan complete: {len(files_to_process)} files found")
            try:
                import time as _t
                _t0 = _t.time()
                _n = prehash_files(files_to_process)
                log_progress(tenant_id, collection_id,
                             f"Pre-hashed {_n} files in {int(_t.time() - _t0)}s "
                             f"({INGEST_HASH_WORKERS} workers)")
            except Exception as _ph:
                logger.warning("prehash failed; falling back to serial hashing: %s", _ph)

            # DAT-aware ingest — if a load file is present, build structured
            # document records (one per Bates) with image/text/native paths
            # instead of treating every file as a separate document.
            dat_doc_map = build_dat_document_map(collection.storage_path)
            if not dat_doc_map:
                import glob as _glob
                _stray = _glob.glob(os.path.join(collection.storage_path, "originals",
                                                 "unpacked", "**", "*.dat"), recursive=True) \
                       + _glob.glob(os.path.join(collection.storage_path, "originals",
                                                 "unpacked", "**", "*.opt"), recursive=True)
                if _stray:
                    logger.error("LOAD FILE PRESENT BUT UNMAPPED (%s) -- falling back "
                                 "to flat ingest. Check DAT header aliases.", _stray[:3])
                    log_progress(tenant_id, collection_id,
                                 f"WARNING: load file present ({os.path.basename(_stray[0])}) "
                                 f"but 0 documents mapped -- ingesting flat. "
                                 f"Check DAT header aliases.", "warning")
            if dat_doc_map:
                logger.info("DAT-aware ingest: processing %d Bates records", len(dat_doc_map))
                stats["total"] = len(dat_doc_map)
                collection.total_docs = len(dat_doc_map)
                session.commit()

                # psycopg2 rejects NUL (0x00) in any text field; strip it from
                # every string we insert so one poisoned record can't kill the run.
                def _nz(v):
                    return v.replace("\x00", "") if isinstance(v, str) else v

                # Incremental commits: a SAVEPOINT per record + a batch commit
                # every N, so a mid-run crash keeps partial progress instead of
                # rolling back everything (the old single end-of-loop commit).
                _dat_since_commit = 0
                _DAT_BATCH = 500

                for bates, rec in dat_doc_map.items():
                    try:
                        # Image path is the primary file_path (what we show first)
                        img_path = rec.get("image_path") or ""
                        native_path = rec.get("native_path") or ""
                        text_path = rec.get("text_path") or ""

                        # Determine primary file for hashing and mime detection
                        primary_rel = img_path or native_path
                        primary_abs = os.path.join(
                            collection.storage_path, primary_rel
                        ) if primary_rel else None

                        if not primary_abs or not os.path.exists(primary_abs):
                            logger.warning("DAT record %s: no primary file found", bates)
                            stats["errors"] = stats.get("errors", 0) + 1
                            continue

                        file_hash = compute_sha256(primary_abs)
                        file_name = rec.get("file_name") or os.path.basename(primary_abs)
                        file_size = os.path.getsize(primary_abs)
                        mime = get_mime_type(primary_abs)
                        doc_type = get_doc_type(primary_abs)

                        # Extract text from text companion if available
                        extracted_text = None
                        working_text_path = None
                        txt_abs = os.path.join(
                            collection.storage_path, text_path
                        ) if text_path else None

                        if txt_abs and os.path.exists(txt_abs):
                            try:
                                with open(txt_abs, encoding="utf-8", errors="replace") as tf:
                                    extracted_text = tf.read()
                                # Copy to working/. Name by Bates (sanitized,
                                # capped) — deriving the name from file_name/
                                # subject blew past the 255-char fs limit
                                # (Errno 36) and that exception killed the run.
                                import re as _re_wn
                                _safe_bates = _re_wn.sub(r"[^A-Za-z0-9._-]", "_", bates)[:120] or "doc"
                                working_text_rel = "working/" + _safe_bates + ".txt"
                                working_text_abs = os.path.join(
                                    collection.storage_path, working_text_rel
                                )
                                os.makedirs(os.path.dirname(working_text_abs), exist_ok=True)
                                import shutil as _shutil
                                _shutil.copy2(txt_abs, working_text_abs)
                                working_text_path = working_text_rel
                            except Exception as te:
                                logger.warning("DAT record %s: text read error: %s", bates, te)

                        # ── Normalize extracted text for DAT record ──
                        import json as _json_dat
                        _norm_text_dat = None
                        _norm_meta_dat = None
                        _det_lang_dat = None
                        _trans_status_dat = 'not_needed'
                        _ocr_status_dat = 'not_needed'
                        if extracted_text:
                            try:
                                _dat_email_meta = {}
                                if rec.get("email_from"):
                                    _dat_email_meta["from"] = rec["email_from"]
                                if rec.get("email_to"):
                                    _dat_email_meta["to"] = rec["email_to"]
                                if rec.get("subject"):
                                    _dat_email_meta["subject"] = rec["subject"]
                                _nr = normalize_text(extracted_text, doc_type, _dat_email_meta)
                                _norm_text_dat = _nr.get("normalized_text")
                                _nm = _nr.get("normalized_metadata") or {}
                                _norm_meta_dat = _json_dat.dumps(_nm)
                                _det_lang_dat = _nm.get("detected_language", "en")
                                _trans_status_dat = "not_needed" if _det_lang_dat == "en" else "queued"
                                if doc_type in ("pdf", "image"):
                                    _ocr_status_dat = "skipped_has_text"
                            except Exception as _ne:
                                logger.warning("DAT normalize failed for %s: %s", bates, _ne)

                        # Strip NUL (0x00) from body-text fields before insert.
                        extracted_text = _nz(extracted_text)
                        _norm_text_dat = _nz(_norm_text_dat)
                        _norm_meta_dat = _nz(_norm_meta_dat)
                        file_name = _nz(file_name)
                        from sqlalchemy import text as _text
                        _dat_sp = session.begin_nested()
                        session.execute(_text("""
                            INSERT INTO ediscovery_documents (
                                tenant_id, collection_id, file_path, original_path,
                                working_path, native_path, text_path, file_name,
                                file_size, file_hash, mime_type, doc_type,
                                extracted_text, bates_begin, bates_end, custodian,
                                doc_date, email_from, email_to, email_subject,
                                normalized_text, normalized_metadata, detected_language,
                                text_source, ocr_status, translation_status,
                                review_status, is_duplicate, ingested_at, created_at
                            ) VALUES (
                                :tenant_id, CAST(:collection_id AS uuid), :file_path, :original_path,
                                :working_path, :native_path, :text_path, :file_name,
                                :file_size, :file_hash, :mime_type, :doc_type,
                                :extracted_text, :bates_begin, :bates_end, :custodian,
                                :doc_date, :email_from, :email_to, :email_subject,
                                :norm_text, CAST(:norm_meta AS jsonb), :det_lang,
                                'extract', :ocr_status, :trans_status,
                                'unreviewed', false, now(), now()
                            )
                        """), {
                            "tenant_id":      tenant_id,
                            "collection_id":  str(collection_id),
                            "file_path":      img_path or primary_rel,
                            "original_path":  primary_rel,
                            "working_path":   working_text_path,
                            "native_path":    native_path,
                            "text_path":      text_path,
                            "file_name":      file_name,
                            "file_size":      file_size,
                            "file_hash":      file_hash,
                            "mime_type":      mime,
                            "doc_type":       doc_type,
                            "extracted_text": extracted_text,
                            "bates_begin":    bates,
                            "bates_end":      rec.get("bates_end") or bates,
                            "custodian":      _nz(rec.get("custodian") or collection.source_party),
                            "doc_date":       _parse_dat_date(rec.get("doc_date_str")),
                            "email_from":     _nz(rec.get("email_from")),
                            "email_to":       _nz(rec.get("email_to")),
                            "email_subject":  _nz(rec.get("subject")),
                            "norm_text":      _norm_text_dat,
                            "norm_meta":      _norm_meta_dat,
                            "det_lang":       _det_lang_dat,
                            "ocr_status":     _ocr_status_dat,
                            "trans_status":   _trans_status_dat,
                        })
                        _dat_sp.commit()
                        stats["processed"] = stats.get("processed", 0) + 1
                        _dat_since_commit += 1
                        if _dat_since_commit >= _DAT_BATCH:
                            session.commit()
                            collection.processed_docs = stats["processed"]
                            session.commit()
                            _dat_since_commit = 0
                            log_progress(tenant_id, collection_id,
                                         f"Ingested {stats['processed']}/{stats['total']} "
                                         f"documents ({stats.get('errors', 0)} errors)")

                    except Exception as de:
                        try:
                            _dat_sp.rollback()
                        except Exception:
                            pass
                        logger.error("DAT record %s ingest error: %s", bates, de)
                        stats["errors"] = stats.get("errors", 0) + 1

                session.commit()

                # Image productions (TIFF/JPG-per-page) have no browser-renderable
                # primary file. Stitch each doc's page images into a multi-page
                # PDF rendition; the review viewer prefers rendition_path.
                try:
                    from modules.ediscovery.jobs.image_rendition import stitch_collection_images
                    _st = stitch_collection_images(tenant_id, str(collection_id),
                                                   collection.storage_path)
                    if _st.get("stitched"):
                        log_progress(tenant_id, collection_id,
                                     f"PDF renditions built for {_st['stitched']} image documents")
                except Exception as _se:
                    logger.warning("image rendition stitch failed: %s", _se)

                # Skip the flat file processing below — DAT handled everything
                files_to_process = []

            # DAT-aware processing: look for a load file in the extracted files.
            # If found, parse it and build a Bates-keyed document map.
            # This replaces the flat file-per-row model with one-row-per-document,
            # correctly linking IMAGE (file_path), TEXT (working_path), and
            # NATIVE (native_path) as attributes of a single logical document.
            dat_files = [f for f in files_to_process
                         if Path(f).suffix.lower() == '.dat']

            if dat_files:
                dat_path = dat_files[0]  # use first DAT if multiple
                logger.info("Found DAT load file: %s", dat_path)
                dat_records = parse_dat_file(dat_path)

                if dat_records:
                    # Switch to DAT-driven mode: replace files_to_process with
                    # only the IMAGE files (one per Bates), using DAT for metadata.
                    # TEXT and NATIVE paths come from the DAT record.
                    collection._dat_records = dat_records
                    collection._dat_unpacked_root = originals_unpacked

                    # Only process IMAGE files as primary — suppress TEXT/NATIVES
                    # from the flat list since they're handled via DAT metadata.
                    image_files = [
                        f for f in files_to_process
                        if 'IMAGE' in f.upper() or 'IMG' in f.upper()
                        or Path(f).suffix.lower() == '.pdf'
                        and 'TEXT' not in f.upper()
                        and 'NATIVE' not in f.upper()
                        and Path(f).suffix.lower() != '.dat'
                        and Path(f).suffix.lower() != '.opt'
                    ]
                    files_to_process = image_files if image_files else [
                        f for f in files_to_process
                        if Path(f).suffix.lower() not in {'.dat', '.opt', '.txt'}
                        and 'TEXT' not in f.upper()
                    ]
                    logger.info("DAT mode: %d Bates records, %d image files to process",
                                len(dat_records), len(files_to_process))

        else:
            # client_collection_dedicated or internal_collection
            # Files already on eDiscovery volume — copy to originals/unpacked
            source_files = []
            for root, dirs, files in os.walk(source_path):
                if "working" in root:
                    continue
                for fname in files:
                    source_files.append(os.path.join(root, fname))

            for src_abs in source_files:
                ext = Path(src_abs).suffix.lower()

                if ext in ARCHIVE_EXTENSIONS:
                    archive_hash, extracted = preserve_archive(
                        src_abs, originals_as_received, originals_unpacked,
                    )
                    collection.original_hash = archive_hash
                    files_to_process.extend(extracted)
                else:
                    dst = os.path.join(originals_unpacked, os.path.basename(src_abs))
                    if src_abs != dst:
                        shutil.copy2(src_abs, dst)
                    files_to_process.append(dst)


        # ── PST expansion: explode PST files into individual EMLs ──────────
        # Must run AFTER all source types populate files_to_process and
        # BEFORE the per-file processing loop. readpst extracts individual
        # emails that the existing .eml handler processes normally.
        if any(Path(f).suffix.lower() == '.pst' for f in files_to_process):
            originals_unpacked = collection.originals_unpacked_path()
            files_to_process = expand_pst_files(files_to_process, originals_unpacked)
            logger.info("Post-PST expansion: %d files to process", len(files_to_process))
        log_progress(tenant_id, collection_id, f"PST expansion complete: {len(files_to_process)} files total")

        if files_to_process:
            stats["total"] = len(files_to_process)
            collection.total_docs = len(files_to_process)
            session.commit()

        # Build hash set for dedup within this collection
        existing_hashes = set(
            row[0] for row in session.query(EdiscoveryDocument.file_hash).filter(
                EdiscoveryDocument.tenant_id == tenant_id,
                EdiscoveryDocument.collection_id == collection_id,
            ).all()
        )

        # Collect transcript doc IDs — drift hooks fire after final commit
        # (doc.id not available until after session.commit flushes the INSERT)
        transcript_doc_ids = []
        # Track issue-map-eligible doc types ingested this run.
        # Fired once after commit — not per-doc — to avoid duplicate jobs.
        issue_map_trigger_doc_types = set()

        # Step 3: Process each file
        for idx, abs_path in enumerate(files_to_process):
            try:
                if job:
                    job.meta["progress"] = f"{idx + 1}/{stats['total']}"
                    job.save_meta()

                file_hash = compute_sha256(abs_path)
                file_name = os.path.basename(abs_path)
                rel_path = os.path.relpath(abs_path, collection.storage_path)
                file_size = os.path.getsize(abs_path)
                mime = get_mime_type(abs_path)
                doc_type = get_doc_type(abs_path)

                # Exact dedup check
                if file_hash in existing_hashes:
                    # Find the original document this is a duplicate of
                    original = session.query(EdiscoveryDocument).filter(
                        EdiscoveryDocument.tenant_id == tenant_id,
                        EdiscoveryDocument.collection_id == collection_id,
                        EdiscoveryDocument.file_hash == file_hash,
                        EdiscoveryDocument.is_duplicate == False,
                    ).first()

                    doc = EdiscoveryDocument(
                        tenant_id=tenant_id,
                        collection_id=collection_id,
                        file_path=rel_path,
                        original_path=rel_path,
                        file_name=file_name,
                        file_size=file_size,
                        file_hash=file_hash,
                        mime_type=mime,
                        doc_type=doc_type,
                        is_duplicate=True,
                        dupe_of_id=None,  # schema mismatch: dupe_of_id is bigint, id is uuid
                    )
                    session.add(doc)
                    stats["duplicates"] += 1
                    continue

                existing_hashes.add(file_hash)

                # Extract text -- deferred to the parallel enrich stage
                # unless explicitly restored (INGEST_INLINE_TEXT=1). Deferring
                # also skips inline normalization and per-doc embedding calls.
                if INGEST_INLINE_TEXT:
                    extracted_text, page_count = extract_text(abs_path, doc_type)
                else:
                    extracted_text, page_count = "", None

                # Write extracted text to working directory
                working_text_path = None
                if extracted_text:
                    working_text_rel = f"working/{Path(file_name).stem}.txt"
                    working_text_abs = os.path.join(
                        collection.storage_path, working_text_rel,
                    )
                    os.makedirs(os.path.dirname(working_text_abs), exist_ok=True)
                    with open(working_text_abs, "w", encoding="utf-8") as f:
                        f.write(extracted_text)
                    working_text_path = working_text_rel

                # Parse email metadata if applicable
                email_meta = {}
                if doc_type == "email":
                    email_meta = extract_email_metadata(abs_path)

                # Generate embedding vector
                embedding = None
                if extracted_text:
                    embedding = generate_embedding(extracted_text)

                # Try DMS cross-reference
                dms_doc_id = None
                if collection.source_type == "client_collection_dms":
                    dms_doc_id = match_dms_document(
                        session, tenant_id, abs_path, file_hash,
                    )

                # ── DAT-aware metadata enrichment ───────────────────────────────
                # If this collection has a parsed DAT, look up metadata by Bates.
                # The Bates number is the file stem (e.g. Marcus1274733).
                dat_meta: dict = {}
                native_path_rel: Optional[str] = None
                bates_begin: Optional[str] = None

                dat_records = getattr(collection, '_dat_records', None)
                if dat_records:
                    bates_stem = Path(file_name).stem  # e.g. Marcus1274733
                    dat_meta = dat_records.get(bates_stem, {})
                    if dat_meta:
                        bates_begin = dat_meta.get('bates_begin') or bates_stem

                        # Resolve native file path from DAT
                        native_rel = dat_meta.get('native_path', '')
                        if native_rel:
                            unpacked_root = getattr(collection, '_dat_unpacked_root',
                                                    collection.storage_path)
                            # DAT paths are relative like VOL00001/NATIVES/NATIVE001/file.mp3
                            native_abs = os.path.join(unpacked_root, native_rel)
                            if os.path.exists(native_abs):
                                native_path_rel = _rel(native_abs, collection.storage_path)
                            else:
                                # Try finding by Bates stem under NATIVES/
                                found = _find_file_by_bates(bates_stem, unpacked_root, 'NATIVES')
                                if not found:
                                    found = _find_file_by_bates(
                                        bates_stem, unpacked_root, 'VOL00001/NATIVES')
                                if found:
                                    native_path_rel = _rel(found, collection.storage_path)

                        # Override working_path with DAT text path if present
                        text_rel = dat_meta.get('text_path', '')
                        if text_rel and not working_text_path:
                            unpacked_root = getattr(collection, '_dat_unpacked_root',
                                                    collection.storage_path)
                            text_abs = os.path.join(unpacked_root, text_rel)
                            if os.path.exists(text_abs):
                                working_text_path = _rel(text_abs, collection.storage_path)

                        # Enrich email/doc metadata from DAT
                        if dat_meta.get('email_from') and not email_meta.get('from'):
                            email_meta['from'] = dat_meta['email_from']
                        if dat_meta.get('email_to') and not email_meta.get('to'):
                            email_meta['to'] = dat_meta['email_to']
                        if dat_meta.get('email_cc') and not email_meta.get('cc'):
                            email_meta['cc'] = dat_meta['email_cc']
                        if dat_meta.get('subject') and not email_meta.get('subject'):
                            email_meta['subject'] = dat_meta['subject']

                # Custodian: DAT overrides collection default
                custodian = (dat_meta.get('custodian') or
                             collection.source_party or '')

                doc = EdiscoveryDocument(
                    tenant_id=tenant_id,
                    collection_id=collection_id,
                    file_path=rel_path,
                    original_path=rel_path,
                    working_path=working_text_path,
                    file_name=file_name,
                    file_size=file_size,
                    file_hash=file_hash,
                    mime_type=mime,
                    doc_type=doc_type,
                    dms_document_id=dms_doc_id,
                    extracted_text=extracted_text,
                    page_count=page_count,
                    custodian=custodian,
                    doc_date=email_meta.get("date"),
                    email_from=email_meta.get("from"),
                    email_to=email_meta.get("to"),
                    email_cc=email_meta.get("cc"),
                    email_subject=email_meta.get("subject"),
                    email_date=email_meta.get("date"),
                    email_thread_id=email_meta.get("thread_id"),
                    email_message_id=email_meta.get("message_id"),
                    email_in_reply_to=email_meta.get("in_reply_to"),
                    email_references=email_meta.get("references"),
                    embedding=embedding,
                    is_duplicate=False,
                    bates_begin=bates_begin,
                    bates_end=bates_begin,  # single-page bates; updated post-commit if range
                )
                # Store native_path via raw SQL after flush — model doesn't have the column yet
                session.add(doc)
                session.flush()  # get doc.id

                if native_path_rel and doc.id:
                    from sqlalchemy import text as sa_text
                    session.execute(sa_text(
                        "UPDATE ediscovery_documents SET native_path = :np "
                        "WHERE id = :did"
                    ), {"np": native_path_rel, "did": doc.id})

                # ── Normalization: clean text + segments + language detect ──
                if extracted_text and doc.id:
                    try:
                        _apply_normalization(
                            session, tenant_id, doc.id,
                            extracted_text, doc_type, email_meta,
                        )
                    except Exception as norm_err:
                        logger.warning("Normalization failed for doc %s (non-blocking): %s",
                                       doc.id, norm_err)

                stats["processed"] += 1

                # Mark transcript for drift hook — fired after final commit
                # when doc.id is guaranteed flushed to DB
                if doc_type == "transcript":
                    transcript_doc_ids.append(doc)

                # Track issue-map-eligible doc types for post-commit trigger
                _ISSUE_MAP_TYPES = {"pleading", "motion", "order", "expert_report"}
                if doc_type in _ISSUE_MAP_TYPES:
                    issue_map_trigger_doc_types.add(doc_type)

            except Exception as e:
                logger.error("Error processing %s: %s", abs_path, str(e))
                stats["errors"] += 1
                continue

            # Commit in batches of 100
            if (idx + 1) % 100 == 0:
                session.commit()
                logger.info(
                    "Collection %d: processed %d/%d",
                    collection_id, idx + 1, stats["total"],
                )
                log_progress(tenant_id, collection_id, f"Processed {idx+1}/{stats['total']} files ({stats.get('duplicates',0)} dupes, {stats.get('errors',0)} errors)")

        # Final commit — all doc.id values are now flushed
        session.commit()

        # Step 4: Set originals to read-only
        try:
            set_readonly(os.path.join(collection.storage_path, "originals"))
        except OSError as e:
            logger.warning("Could not set originals read-only: %s", str(e))

        # Step 5: Update collection stats
        collection.processed_docs = stats["processed"] + stats["duplicates"]
        collection.document_count = stats["processed"] + stats["duplicates"]
        collection.status = "review_ready"
        log_progress(tenant_id, collection_id, f"Ingestion complete: {stats['processed']} processed, {stats['duplicates']} duplicates, {stats['errors']} errors (normalization wired in)", "success")
        session.commit()

        # Step 6: Fire drift detection hooks for transcripts ingested this run.
        # Local import — drift_detection has no dependency on ingest_collection,
        # but keeping the import local is safer for RQ worker process isolation.
        # Non-blocking: hook failure must never abort ingestion or change status.
        if transcript_doc_ids:
            try:
                from modules.ediscovery.drift_detection import maybe_trigger_drift_from_transcript
                matter_id_str = str(collection.matter_id)
                for transcript_doc in transcript_doc_ids:
                    try:
                        maybe_trigger_drift_from_transcript(
                            tenant_id=tenant_id,
                            matter_id=matter_id_str,
                            doc_id=str(transcript_doc.id),
                        )
                        logger.info(
                            "drift hook fired for transcript doc_id=%s matter=%s",
                            transcript_doc.id,
                            matter_id_str,
                        )
                    except Exception as e:
                        logger.warning(
                            "maybe_trigger_drift_from_transcript failed for doc_id=%s (non-blocking): %s",
                            transcript_doc.id,
                            str(e),
                        )
            except ImportError as e:
                logger.warning(
                    "drift_detection import failed — transcript drift hooks skipped: %s", str(e)
                )

        # Step 7: Fire issue map hook if any trigger-eligible docs were ingested.
        # Fires once per ingest run. maybe_trigger_issue_map is idempotent.
        # Non-blocking: hook failure must never abort ingestion.
        if issue_map_trigger_doc_types:
            try:
                from modules.ediscovery.issue_map import maybe_trigger_issue_map
                matter_id_str = str(collection.matter_id)
                trigger_doc_type = next(iter(issue_map_trigger_doc_types))
                try:
                    maybe_trigger_issue_map(
                        tenant_id=tenant_id,
                        matter_id=matter_id_str,
                        doc_type=trigger_doc_type,
                        doc_id="batch",
                    )
                    logger.info(
                        "issue_map hook fired for matter=%s trigger_types=%s",
                        matter_id_str,
                        issue_map_trigger_doc_types,
                    )
                except Exception as e:
                    logger.warning(
                        "maybe_trigger_issue_map failed (non-blocking): %s", str(e)
                    )
            except ImportError as e:
                logger.warning(
                    "issue_map import failed — issue map hook skipped: %s", str(e)
                )

        # write_audit signature (Chat 0): write_audit(session, action, table_name, record_id, details)
        # Wrapped in try/except — audit failure must never block ingestion
        try:
            write_audit(
                session,
                "ediscovery_ingestion_complete",
                "ediscovery_collections",
                collection_id,
                {
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "total": stats["total"],
                    "processed": stats["processed"],
                    "duplicates": stats["duplicates"],
                    "errors": stats["errors"],
                    "source_type": collection.source_type,
                    "transcripts_drift_triggered": len(transcript_doc_ids),
                    "issue_map_triggered": len(issue_map_trigger_doc_types) > 0,
                },
            )
        except Exception as e:
            logger.warning("write_audit failed (non-blocking): %s", str(e))

    except Exception as e:
        logger.error("ingest_ediscovery_collection failed: %s", e, exc_info=True)
        log_progress(tenant_id, collection_id, f"FATAL: {str(e)[:500]}", "error")
        try: session.rollback()
        except Exception: pass
        raise
    finally:
        try: session.close()
        except Exception: pass
    logger.info(
        "Ingestion complete for collection %s: %s", collection_id, stats,
    )
    try:
        jlog.info("=" * 60)
        jlog.info("INGESTION COMPLETE")
        jlog.info("Total: %d, Processed: %d, Duplicates: %d, Errors: %d", stats['total'], stats['processed'], stats['duplicates'], stats['errors'])
        jlog.info("=" * 60)
        # Close file handler
        for h in jlog.handlers[:]:
            h.close()
            jlog.removeHandler(h)
    except Exception:
        pass
    return stats


def rollup_parent_collection(tenant_id: str, parent_collection_id: str, child_ids: list[str]) -> dict:
    """
    Monitor child collections and update parent stats when all are done.
    Runs as an RQ job, polls every 30 seconds.
    """
    import time
    from sqlalchemy import text as _sa_text

    session = get_session_factory()()
    max_wait = 48 * 3600  # 48 hours
    poll_interval = 30
    elapsed = 0

    try:
        while elapsed < max_wait:
            # Check child statuses
            result = session.execute(_sa_text("""
                SELECT status, COUNT(*) AS cnt,
                       COALESCE(SUM(total_docs), 0) AS total,
                       COALESCE(SUM(processed_docs), 0) AS processed,
                       COALESCE(SUM(reviewed_docs), 0) AS reviewed
                FROM ediscovery_collections
                WHERE parent_collection_id = CAST(:pid AS uuid)
                  AND tenant_id = :tid
                GROUP BY status
            """), {"pid": parent_collection_id, "tid": tenant_id})

            status_counts = {}
            total_docs = 0
            processed_docs = 0
            reviewed_docs = 0
            for row in result:
                status_counts[row[0]] = row[1]
                total_docs += row[2]
                processed_docs += row[3]
                reviewed_docs += row[4]

            # Update parent stats
            session.execute(_sa_text("""
                UPDATE ediscovery_collections
                SET total_docs = :total, processed_docs = :processed,
                    reviewed_docs = :reviewed, updated_at = now()
                WHERE id = CAST(:pid AS uuid) AND tenant_id = :tid
            """), {
                "total": total_docs, "processed": processed_docs,
                "reviewed": reviewed_docs,
                "pid": parent_collection_id, "tid": tenant_id,
            })
            session.commit()

            # Check if all children are terminal
            total_children = sum(status_counts.values())
            terminal = status_counts.get("review_ready", 0) + status_counts.get("failed", 0)

            if terminal >= total_children and total_children > 0:
                # All done — set parent status
                if status_counts.get("failed", 0) > 0 and status_counts.get("review_ready", 0) == 0:
                    final_status = "failed"
                elif status_counts.get("failed", 0) > 0:
                    final_status = "review_ready"  # partial success
                else:
                    final_status = "review_ready"

                session.execute(_sa_text("""
                    UPDATE ediscovery_collections
                    SET status = :status, updated_at = now()
                    WHERE id = CAST(:pid AS uuid) AND tenant_id = :tid
                """), {"status": final_status, "pid": parent_collection_id, "tid": tenant_id})
                session.commit()

                log_progress(
                    tenant_id, parent_collection_id,
                    f"All {total_children} sub-collections complete. "
                    f"{status_counts.get('review_ready', 0)} succeeded, "
                    f"{status_counts.get('failed', 0)} failed. "
                    f"Total: {total_docs} docs, {processed_docs} processed.",
                    "success",
                )
                return {
                    "status": final_status,
                    "children": total_children,
                    "total_docs": total_docs,
                    "processed_docs": processed_docs,
                }

            # Log progress
            log_progress(
                tenant_id, parent_collection_id,
                f"Waiting on sub-collections: {status_counts}. "
                f"{processed_docs}/{total_docs} docs processed.",
            )

            time.sleep(poll_interval)
            elapsed += poll_interval

        # Timed out
        session.execute(_sa_text("""
            UPDATE ediscovery_collections
            SET status = 'failed', updated_at = now()
            WHERE id = CAST(:pid AS uuid) AND tenant_id = :tid
        """), {"pid": parent_collection_id, "tid": tenant_id})
        session.commit()
        log_progress(tenant_id, parent_collection_id,
                     "Rollup timed out after 48 hours", "error")
        return {"status": "timeout"}

    except Exception as e:
        logger.error("Rollup failed for parent %s: %s", parent_collection_id, e)
        log_progress(tenant_id, parent_collection_id,
                     f"Rollup error: {e}", "error")
        raise
    finally:
        session.close()
