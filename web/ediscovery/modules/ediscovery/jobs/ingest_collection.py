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
from core.database import TenantSession
from core.services import get_storage_service
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

    Returns list of absolute paths to files in originals/unpacked/.
    """
    copied_files = []
    source_files = storage_service.list_files(tenant_id, dms_source_path)

    for src_file in source_files:
        src_abs = storage_service.get_absolute_path(tenant_id, src_file)
        rel_path = os.path.relpath(src_abs, dms_source_path)
        dst_path = os.path.join(originals_unpacked_path, rel_path)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
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
    """
    job = get_current_job()
    stats = {"total": 0, "processed": 0, "duplicates": 0, "errors": 0}

    with TenantSession(tenant_id) as session:
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
            source_files = storage_service.list_files(tenant_id, source_path)

            for src_file in source_files:
                src_abs = storage_service.get_absolute_path(tenant_id, src_file)
                ext = Path(src_abs).suffix.lower()

                if ext in ARCHIVE_EXTENSIONS:
                    archive_hash, extracted = preserve_archive(
                        src_abs, originals_as_received, originals_unpacked,
                    )
                    collection.original_hash = archive_hash
                    collection.original_file_name = os.path.basename(src_abs)
                    files_to_process.extend(extracted)
                else:
                    # Individual file — copy directly to unpacked
                    dst = os.path.join(originals_unpacked, os.path.basename(src_abs))
                    shutil.copy2(src_abs, dst)
                    files_to_process.append(dst)

        else:
            # client_collection_dedicated or internal_collection
            # Files already on eDiscovery volume — copy to originals/unpacked
            source_path = collection.dms_source_path or collection.storage_path
            source_files = storage_service.list_files(tenant_id, source_path)

            for src_file in source_files:
                src_abs = storage_service.get_absolute_path(tenant_id, src_file)
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
                    custodian=collection.source_party,
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
                )
                session.add(doc)
                stats["processed"] += 1

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

        # Final commit
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
                },
            )
        except Exception as e:
            logger.warning("write_audit failed (non-blocking): %s", str(e))

    logger.info(
        "Ingestion complete for collection %d: %s", collection_id, stats,
    )
    return stats
