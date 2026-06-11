"""
DMS Version Detection Service
==============================
On file upload, detects:
  1. Exact duplicates (SHA-256 match)
  2. Similar documents (text content similarity via SequenceMatcher)

Returns match candidates for the UI to present as version-link proposals.

Uses difflib.SequenceMatcher for text similarity — deterministic, no API
calls, no embedding dependency.  Works on extracted_text already in the
documents table (populated by the parse backfill) and can also extract
text inline from the uploaded file for comparison.

Patent Pending — Series 2/3 — D.M. Holmgren, Reg. No. 54,168
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import List, Optional

from sqlalchemy import text as sa_text

from core.db.base import AsyncSessionLocal

logger = logging.getLogger("praesidium.dms.version_service")

# ── Thresholds ────────────────────────────────────────────────────────
EXACT_DUPE_THRESHOLD = 1.0        # SHA-256 match
HIGH_SIMILARITY_THRESHOLD = 0.80  # Strong version candidate
MIN_SIMILARITY_THRESHOLD = 0.55   # Floor — below this, not a match
MIN_TEXT_LENGTH = 50              # Docs with < 50 chars of text skip similarity
MAX_CANDIDATES_TO_CHECK = 200     # Cap DB rows fetched for comparison
MAX_CANDIDATES_RETURNED = 5       # Top-N results to return to caller


@dataclass
class VersionCandidate:
    """A document that may be a prior version of the uploaded file."""
    document_id: str
    filename: str
    storage_path: str
    similarity: float          # 0.0–1.0
    match_type: str            # 'exact_duplicate' | 'content_similar'
    version_number: int        # Current version of this doc
    created_at: str
    file_size: Optional[int] = None


@dataclass
class VersionCheckResult:
    """Result of checking an uploaded file against existing matter docs."""
    checksum: str
    extracted_text: str = ""
    is_exact_duplicate: bool = False
    duplicate_of: Optional[VersionCandidate] = None
    similar_documents: List[VersionCandidate] = field(default_factory=list)
    next_version_number: int = 1
    error: Optional[str] = None


# ── Text extraction (inline, from bytes) ─────────────────────────────
def extract_text_from_bytes(content: bytes, filename: str) -> str:
    """Extract text from file content for similarity comparison.

    Handles: docx, pdf, txt/csv/rtf, xlsx.  Returns empty string on
    failure — callers must tolerate that gracefully.
    """
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()

    try:
        if ext == "docx":
            return _extract_docx(content)
        elif ext == "pdf":
            return _extract_pdf(content)
        elif ext in ("txt", "text", "csv", "rtf", "md", "log"):
            return content.decode("utf-8", errors="replace")[:50_000]
        elif ext == "xlsx":
            return _extract_xlsx(content)
    except Exception as e:
        logger.warning("Text extraction failed for %s: %s", filename, e)

    return ""


def _extract_docx(content: bytes) -> str:
    # Try python-docx first (standard .docx)
    try:
        from docx import Document
        doc = Document(io.BytesIO(content))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        text = "\n".join(paragraphs)
        if text.strip():
            return text[:50_000]
    except Exception:
        pass
    # Fallback: extract from raw XML inside the zip
    try:
        import zipfile, re
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            parts = []
            for name in zf.namelist():
                if name.endswith(".xml") and ("document" in name.lower() or "word/" in name.lower()):
                    xml = zf.read(name).decode("utf-8", errors="replace")
                    text = re.sub(r"<[^>]+>", " ", xml)
                    text = re.sub(r"\s+", " ", text).strip()
                    if len(text) > 20:
                        parts.append(text)
            if parts:
                return "\n".join(parts)[:50_000]
    except Exception:
        pass
    return ""

def _extract_pdf(content: bytes) -> str:
    import pdfplumber
    parts = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages[:50]:  # Cap at 50 pages for performance
            text = page.extract_text() or ""
            parts.append(text)
    return "\n".join(parts)[:50_000]


def _extract_xlsx(content: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    parts = []
    for ws in wb.worksheets[:5]:  # Cap at 5 sheets
        for row in ws.iter_rows(max_row=200, values_only=True):
            cells = [str(c) for c in row if c is not None]
            if cells:
                parts.append(" ".join(cells))
    wb.close()
    return "\n".join(parts)[:50_000]


# ── SHA-256 ──────────────────────────────────────────────────────────
def compute_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ── Similarity scoring ───────────────────────────────────────────────
def text_similarity(text_a: str, text_b: str) -> float:
    """Compute similarity ratio between two text strings.

    Uses SequenceMatcher which is O(n^2) worst case but fast on typical
    document text (5-50KB).  We truncate to 20K chars to keep it snappy.
    """
    if not text_a or not text_b:
        return 0.0

    a = text_a[:20_000]
    b = text_b[:20_000]

    return SequenceMatcher(None, a, b).ratio()


# ── Main check function ─────────────────────────────────────────────
async def check_for_versions(
    tenant_id: str,
    matter_id: str,
    content: bytes,
    filename: str,
    target_folder: Optional[str] = None,
) -> VersionCheckResult:
    """Check uploaded file against existing matter documents.

    Steps:
      1. Compute SHA-256 -> exact duplicate check
      2. Extract text from uploaded file
      3. Query matter docs that have extracted_text
      4. Run SequenceMatcher against each -> rank by similarity
      5. Return top candidates above threshold

    Args:
        tenant_id: Tenant UUID (will be trimmed)
        matter_id: Matter UUID
        content: Raw file bytes
        filename: Original filename
        target_folder: Optional — disk subfolder being uploaded to

    Returns:
        VersionCheckResult with match candidates
    """
    tid = tenant_id.strip()
    checksum = compute_checksum(content)
    result = VersionCheckResult(checksum=checksum)

    # ── Step 1: Exact duplicate by checksum ──────────────────────────
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT d.id::text AS document_id,
                           d.filename,
                           d.storage_path,
                           d.version_number,
                           d.file_size,
                           d.created_at::text AS created_at
                    FROM documents d
                    WHERE d.checksum = :cs
                      AND d.matter_id = CAST(:mid AS uuid)
                      AND TRIM(d.tenant_id) = :tid
                    LIMIT 1
                """),
                {"cs": checksum, "mid": matter_id, "tid": tid},
            )
            dupe_row = r.mappings().fetchone()

        if dupe_row:
            candidate = VersionCandidate(
                document_id=dupe_row["document_id"],
                filename=dupe_row["filename"],
                storage_path=dupe_row["storage_path"] or "",
                similarity=1.0,
                match_type="exact_duplicate",
                version_number=dupe_row["version_number"] or 1,
                created_at=dupe_row["created_at"] or "",
                file_size=dupe_row["file_size"],
            )
            result.is_exact_duplicate = True
            result.duplicate_of = candidate
            return result

    except Exception as e:
        logger.error("Duplicate check failed: %s", e)
        result.error = f"Duplicate check error: {e}"
        return result

    # ── Step 2: Extract text from uploaded file ──────────────────────
    uploaded_text = extract_text_from_bytes(content, filename)
    result.extracted_text = uploaded_text

    if len(uploaded_text.strip()) < MIN_TEXT_LENGTH:
        # Can't do meaningful text comparison — return as-is (new doc)
        logger.info(
            "Uploaded file %s has insufficient text (%d chars) for similarity check",
            filename, len(uploaded_text.strip()),
        )
        return result

    # ── Step 3: Fetch candidate docs with extracted text ─────────────
    try:
        async with AsyncSessionLocal() as session:
            r = await session.execute(
                sa_text("""
                    SELECT d.id::text AS document_id,
                           d.filename,
                           d.storage_path,
                           d.extracted_text,
                           d.version_number,
                           d.file_size,
                           d.created_at::text AS created_at,
                           d.parent_doc_id::text AS parent_doc_id
                    FROM documents d
                    WHERE d.matter_id = CAST(:mid AS uuid)
                      AND TRIM(d.tenant_id) = :tid
                      AND d.extracted_text IS NOT NULL
                      AND LENGTH(d.extracted_text) >= :min_len
                    ORDER BY d.created_at DESC
                    LIMIT :lim
                """),
                {
                    "mid": matter_id,
                    "tid": tid,
                    "min_len": MIN_TEXT_LENGTH,
                    "lim": MAX_CANDIDATES_TO_CHECK,
                },
            )
            candidates = r.mappings().fetchall()

    except Exception as e:
        logger.error("Candidate fetch failed: %s", e)
        result.error = f"Candidate fetch error: {e}"
        return result

    if not candidates:
        logger.info("No candidates with extracted text for matter %s", matter_id)
        return result

    # ── Step 4: Score each candidate ─────────────────────────────────
    scored: list[VersionCandidate] = []

    for row in candidates:
        sim = text_similarity(uploaded_text, row["extracted_text"])

        if sim >= MIN_SIMILARITY_THRESHOLD:
            scored.append(
                VersionCandidate(
                    document_id=row["document_id"],
                    filename=row["filename"],
                    storage_path=row["storage_path"] or "",
                    similarity=round(sim, 4),
                    match_type="content_similar",
                    version_number=row["version_number"] or 1,
                    created_at=row["created_at"] or "",
                    file_size=row["file_size"],
                )
            )

    # ── Step 5: Sort and return top matches ──────────────────────────
    scored.sort(key=lambda c: c.similarity, reverse=True)
    result.similar_documents = scored[:MAX_CANDIDATES_RETURNED]

    if result.similar_documents:
        best = result.similar_documents[0]
        logger.info(
            "Best match for %s: %s (%.1f%% similar)",
            filename, best.filename, best.similarity * 100,
        )
        # Pre-compute next version number if user accepts the link
        result.next_version_number = best.version_number + 1

    return result


# ── Version link creation ────────────────────────────────────────────
async def create_version_link(
    tenant_id: str,
    new_doc_id: str,
    parent_doc_id: str,
) -> dict:
    """Link a newly uploaded document as a new version of an existing doc.

    Sets parent_doc_id and version_number on the new document.
    Returns the updated version info.
    """
    tid = tenant_id.strip()

    async with AsyncSessionLocal() as session:
        # Get the max version number in the chain
        r = await session.execute(
            sa_text("""
                WITH RECURSIVE chain AS (
                    SELECT id, parent_doc_id, version_number
                    FROM documents
                    WHERE id = CAST(:pid AS uuid)
                      AND TRIM(tenant_id) = :tid
                    UNION ALL
                    SELECT d.id, d.parent_doc_id, d.version_number
                    FROM documents d
                    JOIN chain c ON d.parent_doc_id = c.id
                    WHERE TRIM(d.tenant_id) = :tid
                )
                SELECT COALESCE(MAX(version_number), 1) AS max_ver
                FROM chain
            """),
            {"pid": parent_doc_id, "tid": tid},
        )
        max_ver = r.scalar() or 1
        new_ver = max_ver + 1

        # Find the chain root (the original doc without a parent)
        r2 = await session.execute(
            sa_text("""
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_doc_id
                    FROM documents
                    WHERE id = CAST(:pid AS uuid)
                      AND TRIM(tenant_id) = :tid
                    UNION ALL
                    SELECT d.id, d.parent_doc_id
                    FROM documents d
                    JOIN ancestors a ON a.parent_doc_id = d.id
                    WHERE TRIM(d.tenant_id) = :tid
                )
                SELECT id::text FROM ancestors
                WHERE parent_doc_id IS NULL
                LIMIT 1
            """),
            {"pid": parent_doc_id, "tid": tid},
        )
        root_id = r2.scalar() or parent_doc_id

        # Update the new document
        await session.execute(
            sa_text("""
                UPDATE documents
                SET parent_doc_id = CAST(:pid AS uuid),
                    version_number = :ver,
                    updated_at = NOW()
                WHERE id = CAST(:nid AS uuid)
                  AND TRIM(tenant_id) = :tid
            """),
            {"pid": parent_doc_id, "nid": new_doc_id, "ver": new_ver, "tid": tid},
        )
        await session.commit()

    return {
        "document_id": new_doc_id,
        "parent_doc_id": parent_doc_id,
        "chain_root_id": root_id,
        "version_number": new_ver,
    }
