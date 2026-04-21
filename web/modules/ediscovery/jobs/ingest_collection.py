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
from modules.ediscovery.services.text_extraction import extract_text, extract_email_metadata
from modules.ediscovery.services.embedding import generate_embedding

logger = logging.getLogger(__name__)

# File extensions we know how to process
SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".msg", ".eml", ".txt", ".rtf", ".csv", ".html", ".htm",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp",
}

ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".rar", ".pst"}

EMAIL_EXTENSIONS = {".msg", ".eml"}


def compute_sha256(file_path: str) -> str:
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


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
    # Copy archive to as_received
    archive_name = os.path.basename(archive_path)
    preserved_archive = os.path.join(as_received_path, archive_name)
    shutil.copy2(archive_path, preserved_archive)
    archive_hash = compute_sha256(preserved_archive)

    # Extract based on type
    extracted_files = []
    ext = Path(archive_path).suffix.lower()

    if ext == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(unpacked_path)
            extracted_files = [
                os.path.join(unpacked_path, name)
                for name in zf.namelist()
                if not name.endswith("/")
            ]
    elif ext in (".tar", ".tgz") or archive_path.endswith(".tar.gz"):
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

    # Find .dat file anywhere under originals/unpacked/
    pattern = os.path.join(collection_storage_path, "originals", "unpacked", "**", "*.dat")
    dat_files = glob.glob(pattern, recursive=True)
    if not dat_files:
        return {}

    dat_path = dat_files[0]  # Use first found — should only be one per production
    logger.info("DAT-aware ingest: found load file %s", dat_path)

    rows = parse_dat_file(dat_path)
    if not rows:
        logger.warning("DAT parse returned no rows: %s", dat_path)
        return {}

    # Detect field names — DAT headers vary by producing party
    # Common aliases for each logical field
    BATES_KEYS    = ["Production::Begin Bates", "Begin Bates", "BegBates", "BEGBATES",
                     "Beg Prod", "Begin Production"]
    TEXT_KEYS     = ["Text Precedence", "TEXT_PATH", "Extracted Text Path",
                     "ExtractedTextPath", "TextPath"]
    NATIVE_KEYS   = ["FILE_PATH", "Native File Path", "NativeFilePath",
                     "NATIVE_PATH", "Natives"]
    SUBJECT_KEYS  = ["Subject", "Email Subject", "SUBJECT"]
    FROM_KEYS     = ["From", "Email From", "FROM"]
    TO_KEYS       = ["To", "Email To", "TO"]
    DATE_KEYS     = ["Sent Date/Time", "Date Sent", "Date Created",
                     "Last Modified Date/Time", "DOCDATE"]
    CUSTODIAN_KEYS = ["All Custodians", "Custodian", "CUSTODIAN"]

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
            "bates_end":     _get(row, ["Production::End Bates", "End Bates", "EndBates"]) or bates,
            "image_path":    "",   # filled from OPT file or IMAGES/ scan
            "text_path":     _normalise_dat_path(_get(row, TEXT_KEYS)),
            "native_path":   _normalise_dat_path(_get(row, NATIVE_KEYS)),
            "subject":       _get(row, SUBJECT_KEYS),
            "email_from":    _get(row, FROM_KEYS),
            "email_to":      _get(row, TO_KEYS),
            "doc_date_str":  _get(row, DATE_KEYS),
            "custodian":     _get(row, CUSTODIAN_KEYS),
            "confidentiality": _get(row, ["Confidentiality", "CONFIDENTIALITY"]),
            "md5_hash":      _get(row, ["MD5 Hash", "MD5", "MD5HASH"]),
            "file_name":     _get(row, ["File Name", "FILENAME", "FileName"]),
        }

    # Now scan IMAGES/ to fill image_path for each Bates
    images_dir = os.path.join(collection_storage_path, "originals", "unpacked")
    for root, dirs, files in os.walk(images_dir):
        # Only look in IMAGES directories
        if "IMAGES" not in root.upper() and "IMG" not in root.upper():
            continue
        for fname in files:
            stem = Path(fname).stem.upper()
            if stem in [b.upper() for b in doc_map]:
                # Match by Bates stem
                matched_bates = next(
                    (b for b in doc_map if b.upper() == stem), None
                )
                if matched_bates:
                    abs_path = os.path.join(root, fname)
                    rel_path = os.path.relpath(abs_path, collection_storage_path)
                    doc_map[matched_bates]["image_path"] = rel_path

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

        collection.status = CollectionStatus.processing
        session.commit()

        storage_service = get_storage_service(tenant_id)

        # Step 1: Create directory layout
        create_collection_directories(collection.storage_path)
        originals_as_received = collection.originals_as_received_path()
        originals_unpacked = collection.originals_unpacked_path()
        working_dir = collection.working_path()

        # Step 2: Get files into originals/unpacked/ based on source type
        files_to_process = []

        if collection.source_type == SourceType.client_collection_dms:
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
            SourceType.opposing_production,
            SourceType.third_party_subpoena,
        ):
            # Preserve original archive, then extract
            source_path = collection.dms_source_path or collection.storage_path
            # Use direct filesystem walk -- no storage_service needed
            source_path = collection.dms_source_path or collection.storage_path
            for root, dirs, files in os.walk(source_path):
                if "working" in root:
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
                    elif ext not in {".dat",".opt",".lfp",".log",".csv"}:
                        dst = os.path.join(originals_unpacked, os.path.basename(src_abs))
                        if src_abs != dst:
                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            shutil.copy2(src_abs, dst)
                        files_to_process.append(dst)

            # DAT-aware ingest — if a load file is present, build structured
            # document records (one per Bates) with image/text/native paths
            # instead of treating every file as a separate document.
            dat_doc_map = build_dat_document_map(collection.storage_path)
            if dat_doc_map:
                logger.info("DAT-aware ingest: processing %d Bates records", len(dat_doc_map))
                stats["total"] = len(dat_doc_map)
                collection.total_docs = len(dat_doc_map)
                session.commit()

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
                                # Copy to working/ for consistency
                                working_text_rel = "working/" + Path(file_name).stem + ".txt"
                                working_text_abs = os.path.join(
                                    collection.storage_path, working_text_rel
                                )
                                os.makedirs(os.path.dirname(working_text_abs), exist_ok=True)
                                import shutil as _shutil
                                _shutil.copy2(txt_abs, working_text_abs)
                                working_text_path = working_text_rel
                            except Exception as te:
                                logger.warning("DAT record %s: text read error: %s", bates, te)

                        from sqlalchemy import text as _text
                        session.execute(_text("""
                            INSERT INTO ediscovery_documents (
                                tenant_id, collection_id, file_path, original_path,
                                working_path, native_path, text_path, file_name,
                                file_size, file_hash, mime_type, doc_type,
                                extracted_text, bates_begin, bates_end, custodian,
                                doc_date, email_from, email_to, email_subject,
                                review_status, is_duplicate, ingested_at, created_at
                            ) VALUES (
                                :tenant_id, CAST(:collection_id AS uuid), :file_path, :original_path,
                                :working_path, :native_path, :text_path, :file_name,
                                :file_size, :file_hash, :mime_type, :doc_type,
                                :extracted_text, :bates_begin, :bates_end, :custodian,
                                :doc_date, :email_from, :email_to, :email_subject,
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
                            "custodian":      rec.get("custodian") or collection.source_party,
                            "doc_date":       _parse_dat_date(rec.get("doc_date_str")),
                            "email_from":     rec.get("email_from"),
                            "email_to":       rec.get("email_to"),
                            "email_subject":  rec.get("subject"),
                        })
                        stats["processed"] = stats.get("processed", 0) + 1

                    except Exception as de:
                        logger.error("DAT record %s ingest error: %s", bates, de)
                        stats["errors"] = stats.get("errors", 0) + 1

                session.commit()
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
                        dupe_of_id=original.id if original else None,
                    )
                    session.add(doc)
                    stats["duplicates"] += 1
                    continue

                existing_hashes.add(file_hash)

                # Extract text
                extracted_text, page_count = extract_text(abs_path, doc_type)

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
                if collection.source_type == SourceType.client_collection_dms:
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

        # Final commit — all doc.id values are now flushed
        session.commit()

        # Step 4: Set originals to read-only
        try:
            set_readonly(os.path.join(collection.storage_path, "originals"))
        except OSError as e:
            logger.warning("Could not set originals read-only: %s", str(e))

        # Step 5: Update collection stats
        collection.processed_docs = stats["processed"] + stats["duplicates"]
        collection.status = CollectionStatus.review_ready
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
                    "source_type": collection.source_type.value,
                    "transcripts_drift_triggered": len(transcript_doc_ids),
                    "issue_map_triggered": len(issue_map_trigger_doc_types) > 0,
                },
            )
        except Exception as e:
            logger.warning("write_audit failed (non-blocking): %s", str(e))

    except Exception as e:
        logger.error("ingest_ediscovery_collection failed: %s", e, exc_info=True)
        try: session.rollback()
        except Exception: pass
        raise
    finally:
        try: session.close()
        except Exception: pass
    logger.info(
        "Ingestion complete for collection %d: %s", collection_id, stats,
    )
    return stats
