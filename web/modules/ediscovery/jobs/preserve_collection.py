"""
preserve_collection.py -- eDiscovery explode-and-map, PRESERVATION half.

The first half of the rework pipeline (handoff sec.6 stages 1-3 + family).
Turns a collection's immutable received files into correct, observable
ediscovery_documents rows + exploded native copies + a family graph. Does
NOT extract text/OCR/language/threads/segments -- that's the enrichment job,
built after this lands and we watch the ledger.

Per collection:
  resolve storage_path -> mkdir native/ text/ alongside originals/
  (rebuild) optional work-product-guarded clean slate
  walk originals/:
    email source (.pst/.ost/.msg/.eml/.mbox) -> parse_email_source -> explode
    loose doc                                 -> single unit
  per unit, writing a ediscovery_stage_status row each step:
    received   -> note the source
    preserved  -> sha256, copy to native/{hash}.ext, set native_path/hashes,
                  dedup-view key (NO deletion of copies)
    exploded   -> row inserted (body + one per attachment)
    family_link-> family_id/parent_id/attachment_index/child_* denorm

Conventions (house): psycopg2 + DATABASE_URL; CAST(%s AS type); TRIM(tenant_id);
native_path stored RELATIVE to the collection root; idempotent by content hash.

CLI:
  python -m modules.ediscovery.jobs.preserve_collection \
      --tenant <uuid> --collection <uuid> [--no-rebuild] [--force] [--dry-run]
  python -m modules.ediscovery.jobs.preserve_collection \
      --tenant <uuid> --matter <uuid> [...]        # loop a matter's collections
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from modules.ediscovery.services.parsed_email import (
    parse_email_source, explode, ExplodedUnit,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

EMAIL_EXTS = {".pst", ".ost", ".msg", ".eml", ".mbox"}
SKIP_NAMES = {"ingestion.log"}                 # housekeeping files in collection dirs
SKIP_SUFFIXES = ("_pffexport_audit.log",
                 ".zip", ".tar", ".tgz", ".tar.gz", ".gz", ".7z", ".rar")  # archive containers: extracted upstream; preserved on disk, not units (.pst handled separately)
MIN_LOOSE_BYTES = 1                            # keep everything for loose files

# Stable namespace for deterministic dedup-group uuids.
_DEDUP_NS = uuid.UUID("d3d9446a-0000-4000-8000-000000000ed5")

STAGES = ("received", "preserved", "exploded", "family_link")

# doc_type vocabulary already in the corpus: text/pdf/image/html/email/
# spreadsheet/word/other.  item_type is the FAMILY ROLE: loose_file|attachment.
_EXT_DOCTYPE = {
    ".pdf": "pdf",
    ".doc": "word", ".docx": "word", ".rtf": "text", ".txt": "text",
    ".xls": "spreadsheet", ".xlsx": "spreadsheet", ".csv": "spreadsheet",
    ".html": "html", ".htm": "html",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".tif": "image",
    ".tiff": "image", ".gif": "image", ".bmp": "image",
    ".ppt": "other", ".pptx": "other",
}


# ---------------------------------------------------------------------------
# db
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


# ---------------------------------------------------------------------------
# pure helpers (unit-testable without db/pypff)
# ---------------------------------------------------------------------------

def classify_doc_type(filename: Optional[str], is_email_body: bool) -> str:
    if is_email_body:
        return "email"
    if not filename:
        return "other"
    ext = Path(filename).suffix.lower()
    return _EXT_DOCTYPE.get(ext, "other")


def native_ext(filename: Optional[str], is_email_body: bool) -> str:
    if is_email_body:
        return "eml"
    if filename and Path(filename).suffix:
        return Path(filename).suffix.lower().lstrip(".") or "bin"
    return "bin"


def rel_native_path(file_hash: str, ext: str) -> str:
    """native_path is RELATIVE to the collection root (handoff invariant)."""
    return "native/%s.%s" % (file_hash, ext)


def dedup_group_uuid(dedup_key: str) -> str:
    return str(uuid.uuid5(_DEDUP_NS, dedup_key))


def sha256_bytes(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def is_email_source(path: str) -> bool:
    return Path(path).suffix.lower() in EMAIL_EXTS


def _should_skip(name: str) -> bool:
    if name in SKIP_NAMES:
        return True
    return any(name.endswith(s) for s in SKIP_SUFFIXES)


def _naive(dt) -> Optional[datetime]:
    """email_date column is timestamp WITHOUT tz -> drop tzinfo."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


# ---------------------------------------------------------------------------
# stage-status ledger
# ---------------------------------------------------------------------------

def _stage(cur, tenant, doc_id, collection_id, stage, state,
           worker_id=None, input_hash=None, duration_ms=None,
           error_class=None, error_message=None):
    cur.execute(
        """
        INSERT INTO ediscovery_stage_status
            (tenant_id, document_id, collection_id, stage, state, attempt,
             worker_id, input_hash, started_at, finished_at, duration_ms,
             error_class, error_message, updated_at)
        VALUES (%s, CAST(%s AS uuid), CAST(%s AS uuid), %s, %s, 1,
                %s, %s, now(), now(), %s, %s, %s, now())
        ON CONFLICT (tenant_id, document_id, stage) DO UPDATE SET
            state=EXCLUDED.state, attempt=ediscovery_stage_status.attempt+1,
            collection_id=EXCLUDED.collection_id, worker_id=EXCLUDED.worker_id,
            input_hash=EXCLUDED.input_hash, finished_at=now(),
            duration_ms=EXCLUDED.duration_ms, error_class=EXCLUDED.error_class,
            error_message=EXCLUDED.error_message, updated_at=now()
        """,
        (tenant, str(doc_id), str(collection_id), stage, state,
         worker_id, input_hash, duration_ms, error_class,
         (error_message[:4000] if error_message else None)),
    )


def _coarse_log(cur, tenant, collection_id, message, level="info"):
    """Source-level events that have no document_id yet (e.g. a PST that won't
    open) go to the existing collection-scoped log."""
    try:
        cur.execute(
            "INSERT INTO ediscovery_ingestion_log (tenant_id, collection_id, level, message) "
            "VALUES (%s, CAST(%s AS uuid), %s, %s)",
            (tenant, str(collection_id), level, message[:2000]),
        )
    except Exception as e:
        logger.warning("coarse log failed: %s", e)


# ---------------------------------------------------------------------------
# collection resolution + work-product guard + rebuild
# ---------------------------------------------------------------------------

def _resolve_collection(cur, tenant, collection_id):
    cur.execute(
        """SELECT id, matter_id, collection_name, storage_path, source_party
           FROM ediscovery_collections
           WHERE TRIM(tenant_id)=%s AND id=CAST(%s AS uuid)""",
        (tenant, str(collection_id)),
    )
    row = cur.fetchone()
    if not row:
        raise ValueError("collection %s not found for tenant" % collection_id)
    return {
        "id": row[0], "matter_id": row[1], "collection_name": row[2],
        "storage_path": row[3], "source_party": row[4],
    }


def work_product_refs(cur, tenant, collection_id) -> dict:
    """Count attorney work product hanging off this collection's docs.
    Used by the rebuild tripwire."""
    cur.execute(
        """SELECT count(*) FROM privilege_log_entries ple
           JOIN ediscovery_documents d ON ple.document_id=d.id
           WHERE TRIM(d.tenant_id)=%s AND d.collection_id=CAST(%s AS uuid)""",
        (tenant, str(collection_id)),
    )
    n_priv = cur.fetchone()[0]
    cur.execute(
        """SELECT count(*) FROM ediscovery_documents
           WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)
             AND produced_in IS NOT NULL AND produced_in <> ''""",
        (tenant, str(collection_id)),
    )
    n_prod = cur.fetchone()[0]
    return {"privilege_entries": n_priv, "produced_docs": n_prod}


def _rebuild_clean_slate(cur, tenant, collection_id):
    """Ordered delete so the self-FK (parent_id/container_id) and NO-ACTION
    document_* children don't block. CASCADE handles chunks/segments/annotations.
    Caller has already cleared the work-product tripwire."""
    cid = (tenant, str(collection_id))

    # 1. break self-references inside the collection's docs
    cur.execute(
        """UPDATE ediscovery_documents SET parent_id=NULL, container_id=NULL
           WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)""", cid)

    # 2. delete NO-ACTION extraction-derived children scoped to these docs
    derived = (
        "document_sections", "document_metadata", "document_parties",
        "document_entities", "document_citations", "document_clauses",
        "document_contacts", "document_deadlines", "document_defined_terms",
        "document_business_terms", "document_propositions", "document_section_tags",
    )
    for tbl in derived:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_name=%s AND column_name='ediscovery_document_id')", (tbl,))
        if not cur.fetchone()[0]:
            continue
        cur.execute(
            "DELETE FROM %s WHERE ediscovery_document_id IN "
            "(SELECT id FROM ediscovery_documents "
            " WHERE TRIM(tenant_id)=%%s AND collection_id=CAST(%%s AS uuid))" % tbl,
            cid)

    # 3. stage ledger for these docs
    cur.execute(
        """DELETE FROM ediscovery_stage_status
           WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)""", cid)

    # 4. the docs themselves (CASCADE clears chunks/segments/annotations)
    cur.execute(
        """DELETE FROM ediscovery_documents
           WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)""", cid)
    return cur.rowcount


def _rebuild_clean_disk(coll_root: Path):
    """On rebuild, derived dirs (native/, text/) are regenerated from originals/,
    so wipe their CONTENTS to avoid orphaned renditions accumulating across runs.
    Body .eml renditions are not byte-stable across runs, so stale ones would
    otherwise leak. NEVER touches originals/ (the immutable received copy)."""
    import shutil
    for sub in ("native", "text"):
        d = coll_root / sub
        if not d.is_dir():
            continue
        for child in d.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# row insert + family wiring
# ---------------------------------------------------------------------------

_INSERT_SQL = """
INSERT INTO ediscovery_documents
    (tenant_id, collection_id, item_type, is_attachment, doc_type, native_type,
     original_path, native_path, native_file_hash, file_hash,
     dedup_group_id, is_duplicate, deduped_custodians, custodian, custodian_source,
     email_from, email_to, email_cc, email_bcc, email_subject,
     email_message_id, email_in_reply_to, email_references, email_date,
     email_thread_id, conversation_index, thread_id, is_thread_parent,
     processing_status)
VALUES
    (%s, CAST(%s AS uuid), %s, %s, %s, %s,
     %s, %s, %s, %s,
     CAST(%s AS uuid), %s, %s, %s, %s,
     %s, %s, %s, %s, %s,
     %s, %s, %s, %s,
     %s, %s, %s, %s,
     'preserved')
RETURNING id
"""


def _insert_unit(cur, tenant, collection_id, custodian, custodian_source, unit: ExplodedUnit,
                 file_hash: str, native_path: str, native_type: str,
                 dedup_gid: str, is_dup: bool, deduped_custodians: Optional[str]):
    is_body = (unit.role == "email_body")
    h = unit.headers or {}
    refs = h.get("references") or []
    refs_text = " ".join(refs) if refs else None
    thread_root = (refs[0] if refs else h.get("message_id")) if is_body else None
    is_thread_parent = bool(is_body and h.get("message_id") and h.get("message_id") == thread_root)
    doc_type = classify_doc_type(unit.filename, is_body)

    cur.execute(_INSERT_SQL, (
        tenant, str(collection_id),
        ("attachment" if unit.is_attachment else "loose_file"),
        unit.is_attachment, doc_type, native_type,
        (unit.filename or None), native_path, file_hash, file_hash,
        dedup_gid, is_dup, deduped_custodians, custodian, custodian_source,
        (h.get("from") or None) if is_body else None,
        (h.get("to") or None) if is_body else None,
        (h.get("cc") or None) if is_body else None,
        (h.get("bcc") or None) if is_body else None,
        (h.get("subject") or None) if is_body else None,
        (h.get("message_id") or None) if is_body else None,
        (h.get("in_reply_to") or None) if is_body else None,
        refs_text if is_body else None,
        _naive(h.get("date")) if is_body else None,
        thread_root, (h.get("conversation_index") or None) if is_body else None,
        thread_root, is_thread_parent,
    ))
    return cur.fetchone()[0]


def _wire_family(cur, id_by_local: dict, units: list, root_local: int):
    """Second pass: set family_id (tree root) on all, parent_id from local map,
    attachment_index, and denormalize child_count/child_doc_ids/child_filenames
    on each parent."""
    family_uuid = id_by_local[root_local]

    # family_id on every unit in the tree (loop avoids uuid[] cast pitfalls)
    for u in units:
        cur.execute(
            "UPDATE ediscovery_documents SET family_id=CAST(%s AS uuid) "
            "WHERE id=CAST(%s AS uuid)",
            (str(family_uuid), str(id_by_local[u.local_id])),
        )

    # parent_id + attachment_index per child
    children_by_parent: dict = {}
    for u in units:
        if u.parent_local_id is None:
            continue
        parent_uuid = id_by_local[u.parent_local_id]
        cur.execute(
            "UPDATE ediscovery_documents SET parent_id=CAST(%s AS uuid), "
            "attachment_index=%s WHERE id=CAST(%s AS uuid)",
            (str(parent_uuid), u.attachment_index, str(id_by_local[u.local_id])),
        )
        children_by_parent.setdefault(u.parent_local_id, []).append(u)

    # child_* denorm on each parent
    for parent_local, kids in children_by_parent.items():
        ids = [str(id_by_local[k.local_id]) for k in kids]
        names = [(k.filename or "") for k in kids]
        cur.execute(
            "UPDATE ediscovery_documents SET child_count=%s, child_doc_ids=%s, "
            "child_filenames=%s WHERE id=CAST(%s AS uuid)",
            (len(kids), json.dumps(ids), json.dumps(names),
             str(id_by_local[parent_local])),
        )


# ---------------------------------------------------------------------------
# native storage
# ---------------------------------------------------------------------------

def _store_native(coll_root: Path, file_hash: str, ext: str, data: bytes,
                  dry_run: bool) -> str:
    """Copy bytes to native/{hash}.ext (idempotent: skip if present).
    Returns the RELATIVE native_path."""
    rel = rel_native_path(file_hash, ext)
    if dry_run:
        return rel
    dest = coll_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    return rel


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def preserve_collection(tenant_id: str, collection_id: str,
                        rebuild: bool = True, force: bool = False,
                        dry_run: bool = False) -> dict:
    tenant = tenant_id.strip()
    worker_id = os.environ.get("HOSTNAME", "preserve")
    summary = {"emails": 0, "attachments": 0, "loose": 0, "duplicates": 0,
               "bytes": 0, "unit_errors": 0, "source_errors": 0,
               "rebuilt": 0, "dry_run": dry_run}

    conn = _connect()
    try:
        cur = conn.cursor()
        coll = _resolve_collection(cur, tenant, collection_id)
        coll_root = Path(coll["storage_path"])
        originals = coll_root / "originals"
        if not originals.is_dir():
            # some collections store received files at the collection root
            originals = coll_root
        coll_default = coll.get("source_party") or None

        logger.info("preserve_collection: %s (%s) root=%s rebuild=%s force=%s dry=%s",
                    coll["collection_name"], collection_id, coll_root, rebuild, force, dry_run)

        # ---- work-product tripwire ----
        wp = work_product_refs(cur, tenant, collection_id)
        if (wp["privilege_entries"] or wp["produced_docs"]) and rebuild and not force:
            conn.rollback()
            raise PermissionError(
                "REFUSING rebuild: collection %s has work product "
                "(privilege_entries=%d, produced_docs=%d). Re-run with force=True "
                "to blow it out." % (collection_id, wp["privilege_entries"], wp["produced_docs"]))

        # ---- clean slate ----
        if rebuild and not dry_run:
            summary["rebuilt"] = _rebuild_clean_slate(cur, tenant, collection_id)
            logger.info("rebuild: cleared %d existing docs", summary["rebuilt"])
            _rebuild_clean_disk(coll_root)
        elif not rebuild:
            cur.execute(
                """SELECT count(*) FROM ediscovery_stage_status
                   WHERE TRIM(tenant_id)=%s AND collection_id=CAST(%s AS uuid)
                     AND stage='preserved' AND state='done'""",
                (tenant, str(collection_id)))
            if cur.fetchone()[0] > 0:
                logger.info("non-rebuild: collection already has preserved docs; skipping")
                conn.commit()
                return summary

        if not dry_run:
            (coll_root / "native").mkdir(parents=True, exist_ok=True)
            (coll_root / "text").mkdir(parents=True, exist_ok=True)

        # in-run dedup tracking (sufficient for a fresh rebuild; see note in handoff)
        seen_dedup: dict = {}

        # ---- walk originals/ ----
        for src in sorted(_iter_files(originals)):
            name = src.name
            if _should_skip(name):
                continue
            try:
                if is_email_source(str(src)):
                    summary["emails"] += _process_email_source(
                        conn, cur, tenant, collection_id, coll_default, coll_root,
                        src, seen_dedup, summary, worker_id, dry_run)
                else:
                    _process_loose(
                        conn, cur, tenant, collection_id, coll_default, coll_root,
                        src, seen_dedup, summary, worker_id, dry_run)
            except Exception as e:
                summary["source_errors"] += 1
                logger.exception("source failed: %s", src)
                _coarse_log(cur, tenant, collection_id,
                            "source %s failed: %s" % (name, e), level="error")
                conn.commit()  # keep the coarse log; move on

        conn.commit()
        return summary
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _iter_files(root: Path):
    skip_dirs = {"native", "text", "work", "working", "productions"}
    for dirpath, dirs, files in os.walk(root):
        # never descend into derived/non-original dirs
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fn in files:
            yield Path(dirpath) / fn


def _persist_tree(conn, cur, tenant, collection_id, custodian, custodian_source, coll_root,
                  units: list, seen_dedup: dict, summary: dict, worker_id: str,
                  dry_run: bool, source_path: str):
    """Insert one exploded tree (email family or single loose unit), wire family,
    write the ledger. Returns nothing; updates summary."""
    id_by_local: dict = {}
    root_local = units[0].local_id

    for u in units:
        is_body = (u.role == "email_body")
        data = u.data if u.data is not None else (
            _email_body_native_bytes(u) if is_body else b"")
        fh = sha256_bytes(data)
        ext = native_ext(u.filename, is_body)
        ntype = (u.content_type or ext)

        # dedup-view key: message identity for bodies, file hash otherwise
        dkey = _body_dedup_key(u) if is_body else fh
        dgid = dedup_group_uuid(dkey)
        is_dup = dkey in seen_dedup
        deduped_custodians = None
        if is_dup:
            summary["duplicates"] += 1
            seen_dedup[dkey].add(custodian or "")
            deduped_custodians = json.dumps(sorted(c for c in seen_dedup[dkey] if c))
        else:
            seen_dedup[dkey] = {custodian or ""}

        native_path = _store_native(coll_root, fh, ext, data, dry_run)
        summary["bytes"] += len(data)

        if dry_run:
            doc_id = uuid.uuid4()  # synthetic for the dry-run plan
        else:
            doc_id = _insert_unit(cur, tenant, collection_id, custodian, custodian_source, u,
                                  fh, native_path, ntype, dgid, is_dup,
                                  deduped_custodians)
            _stage(cur, tenant, doc_id, collection_id, "received", "done",
                   worker_id=worker_id, input_hash=fh)
            _stage(cur, tenant, doc_id, collection_id, "preserved", "done",
                   worker_id=worker_id, input_hash=fh)
            _stage(cur, tenant, doc_id, collection_id, "exploded", "done",
                   worker_id=worker_id, input_hash=fh)
        id_by_local[u.local_id] = doc_id

        if u.role == "attachment":
            summary["attachments"] += 1

    if not dry_run:
        _wire_family(cur, id_by_local, units, root_local)
        for u in units:
            _stage(cur, tenant, id_by_local[u.local_id], collection_id,
                   "family_link", "done", worker_id=worker_id)
    conn.commit()


def _email_body_native_bytes(u: ExplodedUnit) -> bytes:
    """Native rendition for an email body. Uses the unit's own native bytes when
    present (.eml/.msg); for PST messages (no native_bytes) serialize a
    reconstructed RFC822 .eml from headers + body. The PST itself remains the
    true original in originals/; this is the per-message native rendition.
    Dedup uses the message-hash key, so re-serialization differing byte-for-byte
    is fine."""
    if u.data is not None:
        return u.data
    from email.message import EmailMessage
    h = u.headers or {}
    m = EmailMessage()
    for hdr, key in (("From", "from"), ("To", "to"), ("Cc", "cc"),
                     ("Subject", "subject"), ("Message-ID", "message_id"),
                     ("In-Reply-To", "in_reply_to")):
        v = h.get(key)
        if v:
            m[hdr] = v
    refs = h.get("references") or []
    if refs:
        m["References"] = " ".join(refs)
    d = h.get("date")
    if d:
        try:
            from email.utils import format_datetime
            m["Date"] = format_datetime(d)
        except Exception:
            pass
    body = u.body_text or ""
    m.set_content(body if body else "")
    if u.body_html:
        try:
            m.add_alternative(u.body_html, subtype="html")
        except Exception:
            pass
    try:
        return m.as_bytes()
    except Exception:
        return (body or "").encode("utf-8", "replace")


def _body_dedup_key(u: ExplodedUnit) -> str:
    """Normalized message identity for an email_body unit (handoff sec.3.2)."""
    h = u.headers or {}
    import re
    def cw(s):
        return re.sub(r"\s+", " ", s or "").strip()
    parts = [
        cw(h.get("from")).lower(), cw(h.get("to")).lower(), cw(h.get("cc")).lower(),
        re.sub(r"^\s*(re|fw|fwd|aw|wg)\s*:\s*", "", cw(h.get("subject")), flags=re.I).lower(),
        (h.get("date").isoformat() if h.get("date") else ""),
        cw(u.body_text or u.body_html or ""),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()


def _process_email_source(conn, cur, tenant, collection_id, coll_default, coll_root,
                          src: Path, seen_dedup, summary, worker_id, dry_run) -> int:
    n = 0
    for pe in parse_email_source(str(src)):
        units = explode(pe)
        cust, csource = resolve_custodian(coll_default, coll_root, src, pe)
        _persist_tree(conn, cur, tenant, collection_id,
                      cust, csource, coll_root,
                      units, seen_dedup, summary, worker_id, dry_run, str(src))
        n += 1
    return n


def _custodian_from_pe(pe) -> Optional[str]:
    return (pe.headers.get("from") or None)


# ---------------------------------------------------------------------------
# custodian resolution (per-document, with pinned provenance)
#   priority: pst_owner > folder > collection-default > email_from > unresolved
#   collection != custodian: the most specific structural signal wins and the
#   source is recorded so dedup / cross-custodian reporting is auditable.
#   Ambiguity is flagged 'unresolved' (conservative inclusion), never silently
#   defaulted to a wrong custodian.
# ---------------------------------------------------------------------------

_PST_EXTS = {".pst", ".ost"}


def _humanize_custodian(s: Optional[str]) -> Optional[str]:
    import re
    if not s:
        return None
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _custodian_from_folder(coll_root: Path, src: Path) -> Optional[str]:
    """First path segment under originals/as_received (or originals/) when the
    file sits in a subfolder -- loose productions are commonly foldered by
    custodian. A file directly in as_received yields no folder custodian."""
    for base in (coll_root / "originals" / "as_received", coll_root / "originals"):
        try:
            rel = Path(src).relative_to(base)
        except ValueError:
            continue
        if len(rel.parts) >= 2:
            return _humanize_custodian(rel.parts[0])
        return None
    return None


def resolve_custodian(coll_default, coll_root: Path, src: Path, pe=None):
    """Return (custodian, custodian_source). Most-specific structural signal wins."""
    ext = Path(src).suffix.lower()
    if ext in _PST_EXTS:
        c = _humanize_custodian(Path(src).stem)
        if c:
            return c, "pst_owner"
    c = _custodian_from_folder(coll_root, src)
    if c:
        return c, "folder"
    if coll_default:
        return coll_default, "collection"
    if pe is not None:
        c = _custodian_from_pe(pe)
        if c:
            return c, "email_from"
    return None, "unresolved"


def _rel_original_path(coll_root: Path, src: Path) -> str:
    """original_path for a loose doc: its path RELATIVE to the received root, so
    folder structure (often itself metadata in a loose production) is preserved
    instead of collapsing to the bare filename. Falls back to the basename.
    native_ext/classify_doc_type still read the suffix correctly off a relpath."""
    for base in (coll_root / "originals", coll_root):
        try:
            return str(src.relative_to(base))
        except ValueError:
            continue
    return src.name


def _process_loose(conn, cur, tenant, collection_id, coll_default, coll_root,
                   src: Path, seen_dedup, summary, worker_id, dry_run):
    data = src.read_bytes()
    if len(data) < MIN_LOOSE_BYTES:
        return
    unit = ExplodedUnit(
        role="loose", local_id=0, parent_local_id=None,
        is_attachment=False, attachment_index=None, filename=_rel_original_path(coll_root, src),
        content_type=None, data=data,
    )
    cust, csource = resolve_custodian(coll_default, coll_root, src, None)
    _persist_tree(conn, cur, tenant, collection_id, cust, csource, coll_root,
                  [unit], seen_dedup, summary, worker_id, dry_run, str(src))
    summary["loose"] += 1


# ---------------------------------------------------------------------------
# cli
# ---------------------------------------------------------------------------

def _collections_for_matter(cur, tenant, matter_id) -> list:
    """Distinct collections for a matter, de-duplicated by storage_path so the
    duplicate collection rows don't double-ingest the same originals dir."""
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
    ap.add_argument("--no-rebuild", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rebuild = not args.no_rebuild
    if args.collection:
        out = preserve_collection(args.tenant, args.collection,
                                  rebuild=rebuild, force=args.force, dry_run=args.dry_run)
        logger.info("DONE %s", json.dumps(out))
    else:
        conn = _connect()
        cur = conn.cursor()
        colls = _collections_for_matter(cur, args.tenant.strip(), args.matter)
        conn.close()
        logger.info("matter %s -> %d distinct collections", args.matter, len(colls))
        for cid, cname in colls:
            logger.info("=== collection %s (%s) ===", cname, cid)
            try:
                out = preserve_collection(args.tenant, str(cid),
                                          rebuild=rebuild, force=args.force,
                                          dry_run=args.dry_run)
                logger.info("DONE %s -> %s", cname, json.dumps(out))
            except Exception as e:
                logger.error("collection %s failed: %s", cname, e)


if __name__ == "__main__":
    main()
