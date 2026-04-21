"""
modules/connectors/file_ingest_service.py

Business logic for the Windows file agent ingest endpoint.
Receives batches of file records from praesidium_agent.pyw and:
  1. Validates X-Connector-Key against credentials_vault
  2. Upserts file records into dms_documents
  3. Routes OCR-flagged files into dms_ocr_queue
  4. Records production-excluded paths into dms_excluded_paths
  5. Pushes extracted text to Elasticsearch (dms_files_{tenant_id})
  6. Writes a summary row to connector_sync_log

Architecture constraint: discovery documents (productions) NEVER enter DMS.
The excluded_paths array in the payload records them for eDiscovery routing only.
"""
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from elasticsearch import AsyncElasticsearch
from sqlalchemy import text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger(__name__)

ES_URL  = os.environ.get("ELASTICSEARCH_URL", "http://10.10.60.12:9200")
ES_USER = os.environ.get("ELASTICSEARCH_USER", "elastic")
ES_PASS = os.environ.get("ELASTICSEARCH_PASSWORD", "")

# OCR priority for legacy share files — lower than open matter documents (10)
OCR_PRIORITY_LEGACY = 50

# Valid extraction status values from agent v1.2
VALID_EXTRACTION_STATUSES = {
    "extracted", "ocr_required", "metadata_only",
    "error_encrypted", "error_corrupt", "error_permission",
    "error_extract", "error_invalid_path",
}


# ── Auth ───────────────────────────────────────────────────────────────────────

async def validate_connector_key(tenant_id: str, api_key: str) -> bool:
    """
    Validate X-Connector-Key against credentials_vault.
    Key must match (tenant_id, provider='windows_agent', key_type='ingest_api_key').
    Returns True if valid, False otherwise.
    """
    if not api_key or not tenant_id:
        return False
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("""
                    SELECT encrypted_key
                    FROM credentials_vault
                    WHERE TRIM(tenant_id) = :tid
                      AND provider        = 'windows_agent'
                      AND key_type        = 'ingest_api_key'
                    LIMIT 1
                """),
                {"tid": tenant_id.strip()}
            )
            row = result.first()
            if not row:
                return False
            return row[0] == api_key
    except Exception as exc:
        logger.error(f"[file_ingest] Key validation error: {exc}")
        return False


# ── Main ingest handler ───────────────────────────────────────────────────────

async def process_file_batch(tenant_id: str, payload: dict) -> dict:
    """
    Process one ingest batch from the Windows agent.

    Payload shape (from praesidium_agent.pyw v1.2):
    {
        "tenant_id":       str,
        "agent_version":   str,
        "sync_timestamp":  ISO datetime str,
        "files": [
            {
                "path":               str,
                "folder_root":        str,
                "size_bytes":         int,
                "modified_at":        ISO str,
                "content_hash":       str | null,
                "extraction_status":  str,
                "ocr_required":       0 | 1,
                "content_text":       str   # only present when extraction_status == "extracted"
            },
            ...
        ],
        "excluded_paths": [
            {
                "path":        str,
                "folder_root": str,
                "matched_term": str,
                "detected_at": ISO str
            },
            ...
        ]
    }

    Returns:
    {
        "accepted":          int,   # files upserted into dms_documents
        "queued_for_ocr":    int,   # files inserted into dms_ocr_queue
        "excluded_recorded": int,   # excluded_paths recorded
        "errors":            int,   # files with error extraction status (recorded, not queued)
        "skipped":           int    # duplicate files with no change
    }
    """
    tid           = tenant_id.strip()
    files         = payload.get("files", [])
    excluded      = payload.get("excluded_paths", [])
    agent_version = payload.get("agent_version", "unknown")
    sync_ts_raw   = payload.get("sync_timestamp", None)
    try:
        sync_ts = datetime.fromisoformat(
            sync_ts_raw.replace("Z", "+00:00")
        ) if sync_ts_raw else datetime.now(timezone.utc)
    except Exception:
        sync_ts = datetime.now(timezone.utc)

    accepted = queued_ocr = excluded_recorded = errors = skipped = 0

    # ── Process files ─────────────────────────────────────────────────────────
    async with AsyncSessionLocal() as session:
        for f in files:
            path             = (f.get("path") or "").strip()
            extraction_status = f.get("extraction_status", "metadata_only")
            ocr_required     = bool(f.get("ocr_required", 0))
            content_text     = f.get("content_text") or None
            file_hash        = f.get("content_hash") or None
            size_bytes       = f.get("size_bytes") or 0
            modified_at_raw  = f.get("modified_at")
            folder_root      = f.get("folder_root") or None

            if not path:
                continue

            # Normalise extraction status
            if extraction_status not in VALID_EXTRACTION_STATUSES:
                extraction_status = "metadata_only"

            # Determine ocr_status for dms_documents
            if extraction_status == "extracted":
                ocr_status = "text_native"
            elif extraction_status == "ocr_required" or ocr_required:
                ocr_status = "ocr_pending"
            elif extraction_status in ("error_encrypted", "error_corrupt",
                                       "error_extract", "error_invalid_path"):
                ocr_status = "error"
                errors += 1
            else:
                ocr_status = "not_applicable"

            # Parse modified_at safely
            try:
                modified_at = datetime.fromisoformat(
                    modified_at_raw.replace("Z", "+00:00")
                ) if modified_at_raw else None
            except Exception:
                modified_at = None

            try:
                # Upsert into dms_documents
                result = await session.execute(
                    text("""
                        INSERT INTO dms_documents (
                            id, tenant_id, file_path, folder_root,
                            file_hash, file_size_bytes, modified_at,
                            content_text, ocr_status, extraction_status,
                            source, agent_version, indexed_at, updated_at
                        ) VALUES (
                            gen_random_uuid(), :tid, :path, :folder_root,
                            :file_hash, :size, :modified_at,
                            :content, :ocr_status, :ext_status,
                            'windows_agent', :agent_ver, NOW(), NOW()
                        )
                        ON CONFLICT (tenant_id, file_path) DO UPDATE SET
                            file_hash         = EXCLUDED.file_hash,
                            file_size_bytes   = EXCLUDED.file_size_bytes,
                            modified_at       = EXCLUDED.modified_at,
                            content_text      = CASE
                                WHEN EXCLUDED.content_text IS NOT NULL
                                THEN EXCLUDED.content_text
                                ELSE dms_documents.content_text
                            END,
                            ocr_status        = EXCLUDED.ocr_status,
                            extraction_status = EXCLUDED.extraction_status,
                            agent_version     = EXCLUDED.agent_version,
                            updated_at        = NOW()
                        RETURNING id,
                            (xmax = 0) AS was_inserted
                    """),
                    {
                        "tid":         tid,
                        "path":        path,
                        "folder_root": folder_root,
                        "file_hash":   file_hash,
                        "size":        size_bytes,
                        "modified_at": modified_at,
                        "content":     content_text,
                        "ocr_status":  ocr_status,
                        "ext_status":  extraction_status,
                        "agent_ver":   agent_version,
                    }
                )
                row = result.first()
                if not row:
                    skipped += 1
                    continue

                doc_id     = row[0]
                was_insert = row[1]

                if not was_insert:
                    skipped += 1
                    # Still update ocr_queue if needed (may have been added this run)
                    if ocr_status != "ocr_pending":
                        accepted += 1
                        continue

                accepted += 1

                # ── OCR queue ─────────────────────────────────────────────────
                if ocr_status == "ocr_pending":
                    await session.execute(
                        text("""
                            INSERT INTO dms_ocr_queue (
                                id, tenant_id, document_id, file_path,
                                priority, status, enqueued_at
                            ) VALUES (
                                gen_random_uuid(), :tid, :doc_id, :path,
                                :priority, 'pending', NOW()
                            )
                            ON CONFLICT (document_id) DO NOTHING
                        """),
                        {
                            "tid":      tid,
                            "doc_id":   doc_id,
                            "path":     path,
                            "priority": OCR_PRIORITY_LEGACY,
                        }
                    )
                    queued_ocr += 1

            except Exception as exc:
                logger.warning(f"[file_ingest] Failed to upsert {path}: {exc}")
                errors += 1
                continue

        # ── Excluded paths ────────────────────────────────────────────────────
        for ep in excluded:
            ep_path = (ep.get("path") or "").strip()
            if not ep_path:
                continue
            try:
                detected_raw = ep.get("detected_at")
                try:
                    detected_at = datetime.fromisoformat(
                        detected_raw.replace("Z", "+00:00")
                    ) if detected_raw else datetime.now(timezone.utc)
                except Exception:
                    detected_at = datetime.now(timezone.utc)

                await session.execute(
                    text("""
                        INSERT INTO dms_excluded_paths (
                            id, tenant_id, path, folder_root,
                            matched_term, detected_at, reported_at
                        ) VALUES (
                            gen_random_uuid(), :tid, :path, :folder_root,
                            :term, :detected_at, NOW()
                        )
                        ON CONFLICT (tenant_id, path) DO UPDATE SET
                            matched_term = EXCLUDED.matched_term,
                            reported_at  = NOW()
                    """),
                    {
                        "tid":         tid,
                        "path":        ep_path,
                        "folder_root": ep.get("folder_root"),
                        "term":        ep.get("matched_term"),
                        "detected_at": detected_at,
                    }
                )
                excluded_recorded += 1
            except Exception as exc:
                logger.warning(f"[file_ingest] Failed to record excluded path {ep_path}: {exc}")

        await session.commit()

    # ── Elasticsearch push ────────────────────────────────────────────────────
    # Only push files with actual content_text — metadata-only and OCR-pending
    # will be pushed after OCR completes.
    text_files = [
        f for f in files
        if f.get("extraction_status") == "extracted"
        and f.get("content_text")
        and (f.get("path") or "").strip()
    ]

    if text_files:
        await _push_to_elasticsearch(tid, text_files, agent_version)

    # ── Log to connector_sync_log ─────────────────────────────────────────────
    total_records = accepted + skipped
    async with AsyncSessionLocal() as session:
        await session.execute(
            text("""
                INSERT INTO connector_sync_log (
                    id, tenant_id, connector_type,
                    started_at, completed_at,
                    records_processed, records_skipped, error_count,
                    triggered_by
                ) VALUES (
                    gen_random_uuid(), :tid, 'windows_agent',
                    :sync_ts, NOW(),
                    :processed, :skipped, :errors,
                    'agent_push'
                )
            """),
            {
                "tid":       tid,
                "sync_ts":   sync_ts,
                "processed": accepted,
                "skipped":   skipped,
                "errors":    errors,
            }
        )
        await session.commit()

    logger.info(
        f"[file_ingest] tenant={tid} accepted={accepted} "
        f"ocr={queued_ocr} excluded={excluded_recorded} "
        f"errors={errors} skipped={skipped}"
    )

    return {
        "accepted":          accepted,
        "queued_for_ocr":    queued_ocr,
        "excluded_recorded": excluded_recorded,
        "errors":            errors,
        "skipped":           skipped,
    }


# ── Elasticsearch ─────────────────────────────────────────────────────────────

async def _push_to_elasticsearch(
    tenant_id: str, files: list[dict], agent_version: str
) -> None:
    """
    Bulk index extracted text into dms_files_{tenant_id}.
    One doc per file. Uses file_path as the document ID (SHA256 truncated).
    Silently skips if ES is unavailable — indexing is best-effort.
    """
    index_name = f"dms_files_{tenant_id.strip().lower().replace('-','_').replace(' ','_')}"

    try:
        es_kwargs: dict[str, Any] = {"hosts": [ES_URL]}
        if ES_USER and ES_PASS:
            es_kwargs["basic_auth"] = (ES_USER, ES_PASS)
        es_kwargs["verify_certs"] = False

        es = AsyncElasticsearch(**es_kwargs)

        # Ensure index exists with minimal mapping
        if not await es.indices.exists(index=index_name):
            await es.indices.create(
                index=index_name,
                body={
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": {
                        "properties": {
                            "tenant_id":    {"type": "keyword"},
                            "file_path":    {"type": "keyword"},
                            "folder_root":  {"type": "keyword"},
                            "content_text": {"type": "text", "analyzer": "english"},
                            "file_hash":    {"type": "keyword"},
                            "source":       {"type": "keyword"},
                            "indexed_at":   {"type": "date"},
                        }
                    }
                }
            )

        # Build bulk body
        operations = []
        for f in files:
            path    = f["path"]
            doc_id  = hashlib.sha256(
                f"{tenant_id}:{path}".encode()
            ).hexdigest()[:32]

            operations.append({"index": {"_index": index_name, "_id": doc_id}})
            operations.append({
                "tenant_id":    tenant_id.strip(),
                "file_path":    path,
                "folder_root":  f.get("folder_root"),
                "content_text": f.get("content_text", ""),
                "file_hash":    f.get("content_hash"),
                "source":       "windows_agent",
                "agent_version": agent_version,
                "indexed_at":   datetime.now(timezone.utc).isoformat(),
            })

        if operations:
            resp = await es.bulk(operations=operations)
            if resp.get("errors"):
                error_items = [
                    i for i in resp.get("items", [])
                    if i.get("index", {}).get("error")
                ]
                logger.warning(
                    f"[file_ingest] ES bulk: {len(error_items)} errors "
                    f"out of {len(files)} docs for tenant {tenant_id}"
                )

        await es.close()

    except Exception as exc:
        # ES push is best-effort — never fail the ingest because of ES
        logger.warning(f"[file_ingest] ES push failed (non-fatal): {exc}")


# ── Connector source registration ─────────────────────────────────────────────

async def ensure_connector_source(
    tenant_id: str, folder_root: str, agent_version: str
) -> None:
    """
    Ensure a connector_sources row exists for this folder_root.
    Upserts on (tenant_id, connector_type, source_name).
    Called once per unique folder_root in a batch.
    """
    tid = tenant_id.strip()
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(
                text("""
                    INSERT INTO connector_sources (
                        id, tenant_id, connector_type, source_name,
                        source_config, status, last_sync_at, updated_at
                    ) VALUES (
                        gen_random_uuid(), :tid, 'windows_agent', :src,
                        :cfg, 'active', NOW(), NOW()
                    )
                    ON CONFLICT (tenant_id, connector_type, source_name)
                    DO UPDATE SET
                        last_sync_at = NOW(),
                        status       = 'active',
                        updated_at   = NOW()
                """),
                {
                    "tid": tid,
                    "src": folder_root,
                    "cfg": f'{{"agent_version": "{agent_version}"}}',
                }
            )
            await session.commit()
    except Exception as exc:
        logger.warning(f"[file_ingest] connector_source upsert failed: {exc}")
