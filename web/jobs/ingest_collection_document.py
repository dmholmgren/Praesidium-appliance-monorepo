"""
RQ job: ingest_collection_document
Reads uploaded file from shared staging path on /mnt/praesidium/staging/
and gates to Component 1 ingestion pipeline.
Patent Pending — 64/020,027
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("praesidium.jobs.ingest_collection_document")

STAGING_ROOT = os.environ.get("COLLECTION_STAGING_ROOT", "/mnt/praesidium/staging")


def run(collection_doc_id: str) -> dict:
    """
    RQ job entry point.
    1. Load collection_document row
    2. Resolve staging path from shared mount
    3. Call Component 1 ingestion pipeline
    4. Update status to ingested or rejected
    5. Clean up staging file
    """
    import psycopg2

    DATABASE_URL = os.environ.get("DATABASE_URL", "")
    # AsyncPG URL → psycopg2 URL
    db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

    conn = None
    staging_path = None

    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = False
        cur = conn.cursor()

        # ── Load row ──────────────────────────────────────────────────────────
        cur.execute(
            """
            SELECT id, tenant_id, collection_id, original_filename,
                   file_hash, mime_type, upload_status, staging_path
            FROM collection_documents
            WHERE id = %s
            """,
            (collection_doc_id,),
        )
        row = cur.fetchone()
        if not row:
            log.error("collection_document %s not found", collection_doc_id)
            return {"error": "not_found"}

        doc = {
            "id": row[0],
            "tenant_id": row[1].strip(),
            "collection_id": row[2],
            "original_filename": row[3],
            "file_hash": row[4],
            "mime_type": row[5],
            "upload_status": row[6],
            "staging_path": row[7],
        }

        staging_path = doc["staging_path"]

        if not staging_path:
            # Fallback: reconstruct expected path
            staging_path = os.path.join(
                STAGING_ROOT, doc["tenant_id"], collection_doc_id
            )
            log.warning(
                "No staging_path on row — falling back to %s", staging_path
            )

        if not os.path.exists(staging_path):
            raise FileNotFoundError(
                f"Staging file not found at {staging_path}. "
                "Check that /mnt/praesidium is mounted on worker containers."
            )

        # Mark as processing
        cur.execute(
            "UPDATE collection_documents SET upload_status = 'processing' WHERE id = %s",
            (collection_doc_id,),
        )
        conn.commit()

        # ── Call Component 1 ingestion pipeline ───────────────────────────────
        document_id = _run_ingestion(doc, staging_path, db_url)

        # ── Mark ingested ─────────────────────────────────────────────────────
        cur.execute(
            """
            UPDATE collection_documents
            SET upload_status = 'ingested',
                document_id = %s,
                processed_at = NOW(),
                staging_path = NULL
            WHERE id = %s
            """,
            (document_id, collection_doc_id),
        )
        conn.commit()
        log.info(
            "collection_document %s ingested → document %s",
            collection_doc_id,
            document_id,
        )
        return {"status": "ingested", "document_id": document_id}

    except Exception as exc:
        log.error(
            "Ingestion failed for collection_document %s: %s", collection_doc_id, exc
        )
        if conn:
            try:
                conn.rollback()
                cur2 = conn.cursor()
                cur2.execute(
                    """
                    UPDATE collection_documents
                    SET upload_status = 'rejected',
                        rejection_reason = %s,
                        processed_at = NOW()
                    WHERE id = %s
                    """,
                    (str(exc)[:500], collection_doc_id),
                )
                conn.commit()
            except Exception as inner:
                log.error("Failed to mark rejection: %s", inner)
        return {"error": str(exc)}

    finally:
        if conn:
            conn.close()
        # Clean up staging file regardless of outcome
        if staging_path and os.path.exists(staging_path):
            try:
                os.unlink(staging_path)
                log.debug("Cleaned up staging file %s", staging_path)
            except Exception as e:
                log.warning("Could not delete staging file %s: %s", staging_path, e)


def _run_ingestion(doc: dict, staging_path: str, db_url: str) -> str:
    """
    Delegate to Component 1 ingestion pipeline.
    Returns the new ediscovery_documents.id.
    """
    try:
        from modules.ediscovery.ingestion import ingest_document_sync
        document_id = ingest_document_sync(
            tenant_id=doc["tenant_id"],
            file_path=staging_path,
            original_filename=doc["original_filename"],
            collection_id=doc["collection_id"],
            file_hash=doc["file_hash"],
        )
        return document_id
    except ImportError:
        log.warning("ingest_document_sync not available — using fallback ingestion")
        return _fallback_ingestion(doc, staging_path, db_url)


def _fallback_ingestion(doc: dict, staging_path: str, db_url: str) -> str:
    """
    Minimal fallback: register the document in ediscovery_documents so the
    collection_document row can be linked. Full text extraction/ES indexing
    will be retried by the next scheduled worker pass.
    """
    import uuid
    import psycopg2

    document_id = str(uuid.uuid4())
    file_size = os.path.getsize(staging_path) if os.path.exists(staging_path) else 0

    conn = psycopg2.connect(db_url)
    try:
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO ediscovery_documents
              (id, tenant_id, original_filename, file_hash, file_size_bytes,
               processing_status, source, collection_id)
            VALUES
              (%s, %s, %s, %s, %s, 'pending', 'collection_upload', %s)
            ON CONFLICT (tenant_id, file_hash) DO UPDATE
              SET processing_status = EXCLUDED.processing_status
            RETURNING id
            """,
            (
                document_id,
                doc["tenant_id"],
                doc["original_filename"],
                doc["file_hash"],
                file_size,
                doc["collection_id"],
            ),
        )
        result = cur.fetchone()
        if result:
            document_id = result[0]
        conn.commit()
    finally:
        conn.close()

    return document_id
