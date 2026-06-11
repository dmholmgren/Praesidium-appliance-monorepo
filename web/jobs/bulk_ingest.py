#!/usr/bin/env python3
"""
jobs/bulk_ingest.py

Bulk Document Ingestion — file_inventory → Praesidium DMS

Eight-stage pipeline per document:
  1. Copy file from legacy QNAP to Praesidium matter tree
  2. SHA-256 hash
  3. Text extraction (pdfplumber/docx/OCR/etc.)
  4. Heuristic classification
  5. Insert dms_documents row
  6. Extraction template engine → primitives (sections, parties, terms, deadlines)
  7. Chunk text into ~512-token windows with overlap
  8. Voyage law-2 embedding → dms_chunks + dms_chunk_embeddings (1024-dim)

Supports --workers N for parallel processing. Each worker gets its own DB
connection and processes a partition of the batch. Embedding calls batched
per-worker (128 chunks per Voyage API call).

Usage:
    # 12 parallel workers, 5000 docs
    docker exec praesidium-web python3 /app/jobs/bulk_ingest.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f \
        --batch-size 5000 --workers 12

    # Single-threaded (default)
    docker exec praesidium-web python3 /app/jobs/bulk_ingest.py \
        --tenant 986c0fee-1390-43bb-ad28-8cd1db6de53f \
        --batch-size 500

    --after 2024-01-01 --before 2026-01-01
    --client "Liberty"
    --skip-text          # skip text extraction
    --skip-extraction    # skip template engine
    --skip-embed         # skip chunking + embedding
    --dry-run            # count only

Patent Pending — 64/015,486 + 64/020,027 + 64/033,333
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing as mp
import os
import re
import shutil
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

import psycopg2
import psycopg2.extras

log = logging.getLogger("bulk_ingest")

TENANT_ID = "986c0fee-1390-43bb-ad28-8cd1db6de53f"
PRAESIDIUM_ROOT = "/mnt/praesidium"
UNMATCHED_MATTER_NAME = "Unmatched Documents"
TEXT_CAP = 200000

CHUNK_TARGET_CHARS = 2000
CHUNK_OVERLAP_CHARS = 200
CHUNK_MIN_CHARS = 100

VOYAGE_MODEL = "voyage-law-2"
VOYAGE_DIMS = 1024
VOYAGE_BATCH_SIZE = 128
VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"


def get_db_conn():
    url = os.environ.get("DATABASE_URL", "")
    url = url.replace("postgresql+asyncpg://", "postgresql://")
    at = url.rfind("@")
    rest = url[at + 1:]
    userinfo = url[len("postgresql://"):at]
    colon = userinfo.rfind(":")
    user = userinfo[:colon]
    password = userinfo[colon + 1:]
    slash = rest.find("/")
    hostport = rest[:slash]
    dbname = rest[slash + 1:].split("?")[0]
    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
    else:
        host, port = hostport, "5432"
    return psycopg2.connect(
        host=host, port=int(port), dbname=dbname, user=user, password=password,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UNMATCHED MATTER
# ═══════════════════════════════════════════════════════════════════════════════

def ensure_unmatched_matter(conn, tid: str) -> str:
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT id FROM matters WHERE TRIM(tenant_id)=%s AND matter_name=%s LIMIT 1",
                (tid, UNMATCHED_MATTER_NAME))
    row = cur.fetchone()
    if row:
        cur.close()
        return str(row["id"])
    cur.execute("SELECT id FROM clients WHERE TRIM(tenant_id)=%s AND client_name='Unmatched' LIMIT 1", (tid,))
    cr = cur.fetchone()
    if cr:
        cid = str(cr["id"])
    else:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO clients (id,tenant_id,client_name,created_at,updated_at) VALUES(%s,%s,'Unmatched',NOW(),NOW())", (cid, tid))
    mid = str(uuid.uuid4())
    cur.execute("INSERT INTO matters (id,tenant_id,client_id,matter_name,matter_number,matter_type,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())",
                (mid, tid, cid, UNMATCHED_MATTER_NAME, 'UNMATCHED', 'internal', 'open'))
    conn.commit()
    Path(PRAESIDIUM_ROOT, tid, "matters", UNMATCHED_MATTER_NAME).mkdir(parents=True, exist_ok=True)
    log.info("Created '%s' matter: %s", UNMATCHED_MATTER_NAME, mid)
    cur.close()
    return mid


# ═══════════════════════════════════════════════════════════════════════════════
# FILE COPY
# ═══════════════════════════════════════════════════════════════════════════════

def _safe_dirname(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '_', name or "unknown").strip().rstrip(".")[:200]


def copy_to_praesidium_tree(source_path: str, tid: str, matter_name: str, root_folder: str) -> str | None:
    matter_dir = Path(PRAESIDIUM_ROOT) / tid / "matters" / _safe_dirname(matter_name)
    try:
        source = Path(source_path)
        parts = source.parts
        root_idx = next((i for i, p in enumerate(parts) if p == root_folder), None)
        if root_idx is not None and root_idx + 1 < len(parts):
            target = matter_dir / Path(*parts[root_idx + 1:])
        else:
            target = matter_dir / source.name
    except Exception:
        target = matter_dir / Path(source_path).name

    if target.exists():
        stem, suffix = target.stem, target.suffix
        c = 1
        while target.exists():
            target = target.parent / f"{stem}_{c}{suffix}"
            c += 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target)
        return str(target)
    except (OSError, PermissionError, FileNotFoundError) as exc:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════

DOC_TYPE_PATTERNS = {
    "pleading": {"markers": [
        re.compile(r"(?i)(cause\s+no|case\s+no|civil\s+action|in\s+the\s+(district|county|circuit)\s+court)"),
        re.compile(r"(?i)(plaintiff|defendant|petitioner|respondent)"),
        re.compile(r"(?i)(motion|brief|complaint|answer|petition|order|judgment)"),
    ], "min": 2},
    "correspondence": {"markers": [
        re.compile(r"(?i)(dear\s+(mr|ms|mrs|counsel|judge)|re:|sincerely|regards|very\s+truly\s+yours)"),
        re.compile(r"(?i)(via\s+(email|facsimile|hand\s+delivery|certified\s+mail))"),
    ], "min": 1},
    "contract": {"markers": [
        re.compile(r"(?i)(agreement|contract|lease|deed|note|instrument|covenant)"),
        re.compile(r"(?i)(whereas|now\s+therefore|witnesseth|hereinafter)"),
        re.compile(r"(?i)(consideration|indemnif|warrant|represent)"),
    ], "min": 2},
    "discovery": {"markers": [
        re.compile(r"(?i)(interrogator|request\s+for\s+(production|admission)|subpoena\s+duces)"),
        re.compile(r"(?i)(propound|serve|answer\s+(to|the)\s+(following|interrogator))"),
    ], "min": 1},
    "deposition": {"markers": [
        re.compile(r"(?i)(deposition|oral\s+examination|sworn\s+testimony)"),
        re.compile(r"(?i)(q\.\s|a\.\s|by\s+(mr|ms|mrs)\.\s+\w+:)"),
        re.compile(r"(?i)(court\s+reporter|notary\s+public|certified\s+shorthand)"),
    ], "min": 2},
    "financial_statement": {"markers": [
        re.compile(r"(?i)(balance\s+sheet|income\s+statement|profit\s+and\s+loss|trial\s+balance)"),
        re.compile(r"(?i)(total\s+(assets|liabilities|equity|revenue)|net\s+(income|loss))"),
    ], "min": 1},
    "invoice": {"markers": [
        re.compile(r"(?i)(invoice|bill\s+to|amount\s+due|payment\s+terms|remit\s+to)"),
        re.compile(r"(?i)(invoice\s+(number|#|no)|total\s+due|balance\s+due)"),
    ], "min": 1},
    "real_estate": {"markers": [
        re.compile(r"(?i)(warranty\s+deed|deed\s+of\s+trust|promissory\s+note|closing\s+statement)"),
        re.compile(r"(?i)(title\s+(commitment|policy|insurance)|survey|legal\s+description)"),
        re.compile(r"(?i)(grantor|grantee|borrower|lender|beneficiary|trustee)"),
    ], "min": 2},
    "corporate": {"markers": [
        re.compile(r"(?i)(articles\s+of\s+(incorporation|organization)|bylaws|operating\s+agreement)"),
        re.compile(r"(?i)(certificate\s+of\s+(formation|good\s+standing)|registered\s+agent)"),
        re.compile(r"(?i)(board\s+of\s+directors|shareholder|member|manager)"),
    ], "min": 2},
    "email": {"markers": [
        re.compile(r"(?i)(from:|to:|cc:|bcc:|subject:|sent:|date:)"),
        re.compile(r"(?i)(@\w+\.\w+)"),
    ], "min": 2},
    "scheduling_order": {"markers": [
        re.compile(r"(?i)(scheduling\s+order|docket\s+control\s+order|case\s+management\s+order)"),
        re.compile(r"(?i)(discovery\s+(?:cut[\- ]?off|deadline)|trial\s+(?:date|setting)|mediation)"),
    ], "min": 1},
}

FOLDER_BOOSTS = {
    "deposition": ["deposition", "depo", "transcript"],
    "discovery": ["discovery", "rfp", "rog"],
    "correspondence": ["correspondence", "letters", "emails"],
    "pleading": ["pleading", "motion", "brief", "filing"],
    "financial_statement": ["financial", "accounting", "bank"],
    "contract": ["contract", "agreement", "lease"],
    "real_estate": ["real estate", "title"],
    "corporate": ["corporate", "formation", "entity"],
    "scheduling_order": ["scheduling", "docket control"],
}


def classify_text(text: str, file_path: str) -> tuple[str, float]:
    if not text or len(text.strip()) < 20:
        return "unknown", 0.0
    scores = {}
    snippet = text[:5000]
    for dt, cfg in DOC_TYPE_PATTERNS.items():
        n = sum(1 for m in cfg["markers"] if m.search(snippet))
        if n >= cfg["min"]:
            scores[dt] = n / len(cfg["markers"])
    fl = (file_path or "").lower()
    for dt, kws in FOLDER_BOOSTS.items():
        for kw in kws:
            if kw in fl:
                scores[dt] = min(1.0, scores.get(dt, 0.0) + (0.2 if dt in scores else 0.3))
                break
    if not scores:
        return "unknown", 0.3
    best = max(scores, key=scores.get)
    return best, round(min(1.0, scores[best]), 2)


EXT_MAP = {
    "pdf": "pdf", "docx": "word", "doc": "word",
    "txt": "text", "rtf": "text",
    "xlsx": "spreadsheet", "xls": "spreadsheet",
    "htm": "html", "html": "html",
    "msg": "email", "eml": "email",
    "csv": "text", "xml": "text",
    "tif": "image", "tiff": "image",
    "png": "image", "jpg": "image", "jpeg": "image",
}


# ═══════════════════════════════════════════════════════════════════════════════
# CHUNKING
# ═══════════════════════════════════════════════════════════════════════════════

def chunk_text(text: str) -> list[dict]:
    if not text or len(text.strip()) < CHUNK_MIN_CHARS:
        return []
    chunks = []
    pos = 0
    idx = 0
    while pos < len(text):
        end = min(pos + CHUNK_TARGET_CHARS, len(text))
        if end < len(text):
            ws = max(pos + CHUNK_TARGET_CHARS - 400, pos)
            we = min(pos + CHUNK_TARGET_CHARS + 200, len(text))
            window = text[ws:we]
            for brk in ["\n\n", "\n", ". ", "; "]:
                bi = window.rfind(brk)
                if bi >= 0:
                    end = ws + bi + len(brk)
                    break
        content = text[pos:end].strip()
        if len(content) >= CHUNK_MIN_CHARS:
            chunks.append({
                "content": content, "char_start": pos, "char_end": end,
                "page_start": text[:pos].count("\f") + 1,
                "page_end": text[:end].count("\f") + 1,
                "chunk_index": idx,
            })
            idx += 1
        pos = max(pos + 1, end - CHUNK_OVERLAP_CHARS)
    return chunks


# ═══════════════════════════════════════════════════════════════════════════════
# VOYAGE EMBEDDING
# ═══════════════════════════════════════════════════════════════════════════════

def embed_chunks_voyage(texts: list[str]) -> list[list[float] | None]:
    import urllib.request
    api_key = os.environ.get("VOYAGE_API_KEY", "")
    if not api_key:
        return [None] * len(texts)
    result: list[list[float] | None] = [None] * len(texts)
    for bs in range(0, len(texts), VOYAGE_BATCH_SIZE):
        bt = texts[bs:bs + VOYAGE_BATCH_SIZE]
        req = urllib.request.Request(
            VOYAGE_API_URL,
            data=json.dumps({"model": VOYAGE_MODEL, "input": bt, "input_type": "document"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            for item in data.get("data", []):
                result[bs + item["index"]] = item["embedding"]
        except Exception as exc:
            log.warning("Voyage batch failed (offset=%d): %s", bs, exc)
    return result


def write_chunks_and_embeddings(cur, tid, doc_id, matter_id, client_id, run_id,
                                chunks, embeddings, file_path, file_type) -> int:
    written = 0
    for i, chunk in enumerate(chunks):
        cid = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO dms_chunks (id,tenant_id,source_type,source_id,matter_id,client_id,
                run_id,chunk_index,char_start,char_end,content,token_count,chunk_metadata,
                chunked_at,file_path,file_type,page_number)
            VALUES (%s,%s,'dms_document',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,NOW(),%s,%s,%s)
        """, (cid, tid, doc_id, matter_id, client_id, run_id,
              chunk["chunk_index"], chunk["char_start"], chunk["char_end"],
              chunk["content"], len(chunk["content"]) // 4,
              json.dumps({"page_start": chunk["page_start"], "page_end": chunk["page_end"]}),
              file_path, file_type, chunk["page_start"]))
        if embeddings and i < len(embeddings) and embeddings[i]:
            vec = "[" + ",".join(str(v) for v in embeddings[i]) + "]"
            cur.execute("""
                INSERT INTO dms_chunk_embeddings (id,tenant_id,chunk_id,embedding_model,embedded_at,embedding_1024)
                VALUES (%s,%s,%s,%s,NOW(),%s::vector)
            """, (str(uuid.uuid4()), tid, cid, VOYAGE_MODEL, vec))
        written += 1
    return written


# ═══════════════════════════════════════════════════════════════════════════════
# WORKER FUNCTION (runs in child process)
# ═══════════════════════════════════════════════════════════════════════════════

def _worker_process(args_tuple):
    """
    Process a partition of documents. Runs in a child process with its own
    DB connection. Returns stats dict.
    """
    (worker_id, partition, tid, run_id, unmatched_matter_id,
     matter_cache, client_cache, skip_text, skip_extraction, skip_embed) = args_tuple

    # Set up logging for this worker
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s W{worker_id:02d} %(levelname)s: %(message)s",
    )
    wlog = logging.getLogger(f"worker-{worker_id}")

    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()

    # Lazy imports inside worker (fork-safe)
    from modules.ediscovery.services.text_extraction import extract_text
    if not skip_extraction:
        from jobs.extraction_template_engine import run_extraction_template

    stats = {
        "copied": 0, "copy_failed": 0,
        "extracted": 0, "extract_failed": 0, "classified": 0,
        "primitives": 0, "primitive_failed": 0,
        "sections": 0, "parties": 0, "defined_terms": 0, "deadlines": 0,
        "chunks": 0, "embeddings": 0, "by_type": {},
    }

    # Embedding accumulator for this worker
    embed_queue = []  # (doc_id, matter_id, client_id, chunk, file_path, ext)
    embed_texts = []

    def flush():
        nonlocal embed_queue, embed_texts
        if not embed_queue or skip_embed:
            embed_queue, embed_texts = [], []
            return 0
        embeddings = embed_chunks_voyage(embed_texts)
        written = 0
        i = 0
        while i < len(embed_queue):
            did = embed_queue[i][0]
            dc, de = [], []
            while i < len(embed_queue) and embed_queue[i][0] == did:
                _, mid, cid, ch, fp, ft = embed_queue[i]
                dc.append(ch)
                de.append(embeddings[i] if embeddings else None)
                i += 1
            written += write_chunks_and_embeddings(cur, tid, did, mid, cid, run_id, dc, de, fp, ft)
        embed_queue, embed_texts = [], []
        return written

    t0 = time.time()

    for i, row in enumerate(partition):
        fpath = row["full_path"]
        fname = row["name"] or Path(fpath).name
        ext = (row["extension"] or "").lower().lstrip(".")
        matter_id = str(row["proposed_matter_id"]) if row["proposed_matter_id"] else unmatched_matter_id
        matter_name = matter_cache.get(matter_id, UNMATCHED_MATTER_NAME)
        client_id = str(row["proposed_client_id"]) if row["proposed_client_id"] else client_cache.get(matter_id)

        # 1. Copy
        new_path = copy_to_praesidium_tree(fpath, tid, matter_name, row["root_folder"] or "")
        if not new_path:
            stats["copy_failed"] += 1
            continue
        stats["copied"] += 1

        # 2. Hash
        try:
            with open(new_path, "rb") as f:
                file_hash = hashlib.sha256(f.read(65536)).hexdigest()
        except Exception:
            file_hash = None

        # 3. Text
        text, page_count = None, None
        if not skip_text:
            try:
                text, page_count = extract_text(new_path, EXT_MAP.get(ext, "text"))
                if text:
                    text = text[:TEXT_CAP]
                    stats["extracted"] += 1
                else:
                    stats["extract_failed"] += 1
            except Exception:
                stats["extract_failed"] += 1

        # 4. Classify
        doc_type, confidence = ("unknown", 0.0)
        if text and len(text.strip()) > 20:
            doc_type, confidence = classify_text(text, fpath)
            stats["classified"] += 1
        stats["by_type"][doc_type] = stats["by_type"].get(doc_type, 0) + 1

        # 5. dms_documents
        doc_id = str(uuid.uuid4())
        ocr_status = "not_applicable"
        if ext == "pdf":
            ocr_status = "ocr_complete" if page_count and page_count > 0 else "ocr_pending"
        elif ext in ("tif", "tiff", "png", "jpg", "jpeg"):
            ocr_status = "ocr_complete" if text else "ocr_failed"

        cur.execute("""
            INSERT INTO dms_documents (id,tenant_id,file_path,folder_root,file_hash,
                file_size_bytes,modified_at,content_text,ocr_status,extraction_status,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'bulk_ingest') ON CONFLICT DO NOTHING
        """, (doc_id, tid, new_path, row["root_folder"], file_hash,
              row["size_bytes"], row["modified_at"], text, ocr_status,
              "text_extracted" if text else "pending"))

        # 6. Extraction engine
        if not skip_extraction and text and len(text.strip()) > 50:
            try:
                result = run_extraction_template(
                    tenant_id=tid, document_id=doc_id, run_id=run_id,
                    content_text=text, document_type_code=doc_type,
                    source_table="dms", matter_id=matter_id, file_path=new_path,
                )
                if not result.get("error"):
                    stats["primitives"] += 1
                    stats["sections"] += result.get("sections", 0)
                    stats["parties"] += result.get("parties", 0)
                    stats["defined_terms"] += result.get("defined_terms", 0)
                    stats["deadlines"] += result.get("deadlines", 0)
                else:
                    stats["primitive_failed"] += 1
            except Exception:
                stats["primitive_failed"] += 1

        # 7+8. Chunk + embed queue
        if text and len(text.strip()) > CHUNK_MIN_CHARS and not skip_embed:
            chunks = chunk_text(text)
            stats["chunks"] += len(chunks)
            for ch in chunks:
                embed_queue.append((doc_id, matter_id, client_id, ch, new_path, ext))
                embed_texts.append(ch["content"])
            if len(embed_texts) >= VOYAGE_BATCH_SIZE:
                stats["embeddings"] += flush()

        # Progress every 100 docs
        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            wlog.info("[%d/%d] cp=%d txt=%d pri=%d chk=%d emb=%d (%.1f/s)",
                      i+1, len(partition), stats["copied"], stats["extracted"],
                      stats["primitives"], stats["chunks"], stats["embeddings"], rate)

    # Final flush
    if embed_texts:
        stats["embeddings"] += flush()

    cur.close()
    conn.close()

    elapsed = time.time() - t0
    wlog.info("Done: %d docs in %.1fs (%.1f/s)", len(partition), elapsed,
              len(partition)/elapsed if elapsed else 0)
    return stats


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Bulk ingest from file_inventory")
    parser.add_argument("--tenant", default=TENANT_ID)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--workers", type=int, default=1, help="Parallel worker processes (default 1)")
    parser.add_argument("--after", default=None, help="YYYY-MM-DD")
    parser.add_argument("--before", default=None, help="YYYY-MM-DD")
    parser.add_argument("--client", default=None, help="Client name substring")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument("--skip-extraction", action="store_true")
    parser.add_argument("--skip-embed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    tid = args.tenant.strip()
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    unmatched_matter_id = ensure_unmatched_matter(conn, tid)

    # Mark eDiscovery for later
    cur.execute("""
        UPDATE file_inventory SET priority = 'ediscovery_pending'
        WHERE tenant_id = %s AND entry_type = 'file'
          AND classification LIKE 'ediscovery%%'
          AND (priority IS NULL OR priority NOT IN ('ediscovery_pending','ediscovery_complete'))
    """, (tid,))
    if cur.rowcount:
        log.info("Marked %d eDiscovery items for later", cur.rowcount)

    # Query eligible
    where = [
        "fi.tenant_id = %s", "fi.entry_type = 'file'", "fi.classification = 'document'",
        "fi.full_path NOT IN (SELECT file_path FROM dms_documents WHERE TRIM(tenant_id) = %s)",
    ]
    params = [tid, tid]
    if args.after:
        where.append("fi.modified_at >= %s"); params.append(args.after)
    if args.before:
        where.append("fi.modified_at < %s"); params.append(args.before)
    if args.client:
        where.append("fi.proposed_client_name ILIKE %s"); params.append(f"%{args.client}%")

    cur.execute(f"SELECT count(*) as cnt FROM file_inventory fi WHERE {' AND '.join(where)}", params)
    total_eligible = cur.fetchone()["cnt"]

    log.info("=== Bulk Ingestion ===")
    log.info("Eligible: %d | Batch: %d | Workers: %d | Embed: %s",
             total_eligible, args.batch_size, args.workers,
             "voyage-law-2" if not args.skip_embed else "OFF")

    if args.dry_run:
        cur.execute(f"""
            SELECT COALESCE(fi.proposed_client_name, '(unmatched)') as client, count(*) as cnt
            FROM file_inventory fi WHERE {' AND '.join(where)}
            GROUP BY fi.proposed_client_name ORDER BY cnt DESC LIMIT 30
        """, params)
        for r in cur.fetchall():
            log.info("  %-40s %d", r["client"], r["cnt"])
        log.info("  TOTAL: %d", total_eligible)
        return

    # Extraction run
    run_id = str(uuid.uuid4())
    cur.execute("""
        INSERT INTO extraction_runs (id,tenant_id,run_type,source_type,status,document_count,started_at)
        VALUES (%s,%s,'bulk_ingest','file_inventory','processing',%s,NOW())
    """, (run_id, tid, min(args.batch_size, total_eligible)))

    # Fetch batch
    cur.execute(f"""
        SELECT fi.id as inv_id, fi.full_path, fi.name, fi.extension,
               fi.size_bytes, fi.modified_at, fi.root_folder,
               fi.proposed_client_id, fi.proposed_client_name, fi.proposed_matter_id
        FROM file_inventory fi WHERE {' AND '.join(where)}
        ORDER BY fi.modified_at DESC NULLS LAST LIMIT %s
    """, params + [args.batch_size])
    batch = cur.fetchall()
    log.info("Fetched %d documents", len(batch))

    # Build caches (serializable for child processes)
    matter_cache, client_cache = {}, {}
    cur.execute("SELECT id, matter_name, client_id FROM matters WHERE TRIM(tenant_id) = %s", (tid,))
    for r in cur.fetchall():
        matter_cache[str(r["id"])] = r["matter_name"]
        if r["client_id"]:
            client_cache[str(r["id"])] = str(r["client_id"])
    matter_cache[unmatched_matter_id] = UNMATCHED_MATTER_NAME

    cur.close()
    conn.close()

    # ── Partition and dispatch ────────────────────────────────────────
    n_workers = min(args.workers, len(batch))
    if n_workers < 1:
        n_workers = 1

    # Round-robin partition to balance file sizes across workers
    partitions = [[] for _ in range(n_workers)]
    for i, row in enumerate(batch):
        partitions[i % n_workers].append(row)

    log.info("Dispatching to %d workers: %s",
             n_workers, [len(p) for p in partitions])

    t0 = time.time()

    worker_args = [
        (wid, partitions[wid], tid, run_id, unmatched_matter_id,
         matter_cache, client_cache,
         args.skip_text, args.skip_extraction, args.skip_embed)
        for wid in range(n_workers)
    ]

    if n_workers == 1:
        # Single-threaded — no fork overhead
        all_stats = [_worker_process(worker_args[0])]
    else:
        # Use fork-based multiprocessing
        mp.set_start_method("fork", force=True)
        with mp.Pool(processes=n_workers) as pool:
            all_stats = pool.map(_worker_process, worker_args)

    # ── Merge stats ───────────────────────────────────────────────────
    merged = {
        "copied": 0, "copy_failed": 0,
        "extracted": 0, "extract_failed": 0, "classified": 0,
        "primitives": 0, "primitive_failed": 0,
        "sections": 0, "parties": 0, "defined_terms": 0, "deadlines": 0,
        "chunks": 0, "embeddings": 0, "by_type": {},
    }
    for s in all_stats:
        for k in ["copied", "copy_failed", "extracted", "extract_failed", "classified",
                   "primitives", "primitive_failed", "sections", "parties",
                   "defined_terms", "deadlines", "chunks", "embeddings"]:
            merged[k] += s.get(k, 0)
        for dt, cnt in s.get("by_type", {}).items():
            merged["by_type"][dt] = merged["by_type"].get(dt, 0) + cnt

    # Finalize extraction_run
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        UPDATE extraction_runs SET status='completed', completed_at=NOW(),
            documents_processed=%s, documents_failed=%s WHERE id=%s
    """, (merged["primitives"], merged["primitive_failed"] + merged["copy_failed"], run_id))
    cur.close()
    conn.close()

    elapsed = time.time() - t0

    log.info("")
    log.info("=== Bulk Ingestion Summary ===")
    log.info("  Run:      %s", run_id)
    log.info("  Time:     %.1f min (%.1f docs/sec across %d workers)",
             elapsed / 60, len(batch) / elapsed if elapsed else 0, n_workers)
    log.info("  ── Pipeline ──")
    log.info("  Copy:     %d ok / %d fail", merged["copied"], merged["copy_failed"])
    log.info("  Text:     %d ok / %d fail", merged["extracted"], merged["extract_failed"])
    log.info("  Classify: %d", merged["classified"])
    log.info("  Prims:    %d ok / %d fail", merged["primitives"], merged["primitive_failed"])
    log.info("  Chunks:   %d", merged["chunks"])
    log.info("  Embeds:   %d", merged["embeddings"])
    log.info("  ── Extracted ──")
    log.info("  Sec=%d  Party=%d  Term=%d  Deadline=%d",
             merged["sections"], merged["parties"], merged["defined_terms"], merged["deadlines"])
    log.info("  ── Remaining ──")
    log.info("  %d docs left", total_eligible - len(batch))
    log.info("  Types: %s",
             ", ".join(f"{k}={v}" for k, v in sorted(merged["by_type"].items(), key=lambda x: -x[1])))
    log.info("=== Run again for next batch ===")


if __name__ == "__main__":
    main()
