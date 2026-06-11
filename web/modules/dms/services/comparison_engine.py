"""
COMP 8 — Document Comparison & Redlining (v2 — LO native engine)

POST /api/v1/dms/documents/compare
  - Resolves both document storage paths
  - Converts non-.docx inputs via LibreOffice headless
  - Runs LibreOffice native .uno:CompareDocuments for character-level
    redline with full formatting preservation
  - Stages output to .tmp/redlines/ (purged nightly)
  - Returns download URL; "Save to DMS" is a separate user action

POST /api/v1/dms/documents/compare/{redline_id}/save
  - Copies a staged redline into the matter's DMS folder
  - Creates a documents row with proper provenance

HISTORY:
  v1 — python-docx + difflib.SequenceMatcher paragraph walker.
       Paragraph-level only, lost formatting, mangled source docs
       with existing tracked changes.
  v2 (May 18, 2026) — LibreOffice native .uno:CompareDocuments.
       Character-level fidelity, handles tracked changes in source
       documents, preserves formatting/tables/headers.
       Output staged to .tmp/redlines/ for nightly purge.
"""

import os
import io
import uuid
import logging
import hashlib
import shutil
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import text as sa_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/dms/documents", tags=["dms-compare"])


# ═════════════════════════════════════════════════════════════════════════
# Config
# ═════════════════════════════════════════════════════════════════════════

_DOCX_EXTENSIONS = {".docx"}
_CONVERTIBLE_EXTENSIONS = {
    ".doc", ".docx", ".rtf", ".odt", ".pdf", ".txt", ".html", ".htm",
}
_LO_COMPARE_SCRIPT = Path("/app/modules/dms/services/lo_compare.py")
_LO_PYTHON = "/usr/lib/libreoffice/program/python"
_LO_TIMEOUT = 120  # seconds

# WmlComparer sidecar (primary redline engine; LibreOffice is the fallback)
_REDLINE_URL = os.environ.get(
    "PRAESIDIUM_REDLINE_URL", "http://praesidium-redline:8080/compare")
_REDLINE_TIMEOUT = 120  # seconds
_REDLINE_AUTHOR = "Praesidium"


def _storage_root() -> Path:
    return Path(os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium"))


def _redline_staging_dir(tenant_id: str) -> Path:
    tid = (tenant_id or "").strip()
    return _storage_root() / tid / ".tmp" / "redlines"


def _resolve_storage(storage_path: str) -> Path:
    p = Path(storage_path)
    if not p.is_absolute():
        p = _storage_root() / p
    return p


# ═════════════════════════════════════════════════════════════════════════
# Request / response models
# ═════════════════════════════════════════════════════════════════════════

class CompareRequest(BaseModel):
    doc_a_id: str
    doc_b_id: str


class SaveRedlineRequest(BaseModel):
    filename: Optional[str] = None
    folder_path: Optional[str] = None


# ═════════════════════════════════════════════════════════════════════════
# LibreOffice helpers
# ═════════════════════════════════════════════════════════════════════════

def _convert_to_docx(
    input_path: Path,
    work_dir: Path,
    profile_id: str,
) -> Path:
    """Convert non-.docx to .docx via soffice headless."""
    if input_path.suffix.lower() in _DOCX_EXTENSIONS:
        return input_path

    work_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = work_dir / f"lo-profile-{profile_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "soffice", "--headless", "--norestore", "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir.absolute()}",
        "--convert-to", "docx",
        "--outdir", str(work_dir.absolute()),
        str(input_path.absolute()),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        raise RuntimeError(f"LO conversion failed: {proc.stderr.strip()[:400]}")

    out = work_dir / f"{input_path.stem}.docx"
    if not out.exists():
        candidates = sorted(work_dir.glob("*.docx"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            raise RuntimeError("LO exited 0 but no .docx produced")
        out = candidates[0]
    return out


# ---- WmlComparer sidecar: primary redline engine, LibreOffice is fallback ----

class _RedlineFallback(Exception):
    """Raised when the sidecar declines a pair (HTTP 422) so the caller
    should fall back to the LibreOffice engine."""


def _post_compare_multipart(url, original, revised, author, timeout):
    """POST two .docx files to the sidecar as multipart/form-data using only
    the standard library (no httpx/requests). Returns (status, body_bytes)."""
    boundary = "----praesidium" + uuid.uuid4().hex
    docx_ct = ("application/vnd.openxmlformats-officedocument"
               ".wordprocessingml.document")

    def _file_part(field, filename, data):
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {docx_ct}\r\n\r\n"
        ).encode("utf-8")
        return head + data + b"\r\n"

    def _text_part(field, value):
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
            f"{value}\r\n"
        ).encode("utf-8")

    body = b"".join([
        _file_part("original", "original.docx", original.read_bytes()),
        _file_part("revised", "revised.docx", revised.read_bytes()),
        _text_part("author", author),
        f"--{boundary}--\r\n".encode("utf-8"),
    ])

    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _run_wml_compare(original, revised, output):
    """Run the WmlComparer sidecar; write the redline to `output` on success.
    Raise _RedlineFallback on HTTP 422 (sidecar declined the pair)."""
    status, payload = _post_compare_multipart(
        _REDLINE_URL, original, revised, _REDLINE_AUTHOR, _REDLINE_TIMEOUT,
    )
    if status == 200:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        if not output.exists() or output.stat().st_size == 0:
            raise RuntimeError("sidecar returned 200 but produced empty output")
        return
    raise _RedlineFallback("sidecar HTTP " + str(status))


def _run_compare(
    original: Path, revised: Path, output: Path,
    work_dir: Path, job_id: str,
) -> str:
    """Primary compare path: try the WmlComparer sidecar; on HTTP 422 or any
    transport error fall back to LibreOffice. Returns the engine label that
    actually produced the output."""
    try:
        _run_wml_compare(original, revised, output)
        return "wmlcomparer"
    except (_RedlineFallback, urllib.error.URLError, ConnectionError,
            TimeoutError, OSError) as exc:
        logger.info(
            "[dms-compare] WmlComparer unavailable/declined (%s); "
            "falling back to LibreOffice", exc,
        )
        _run_lo_compare(
            original=original, revised=revised, output=output,
            work_dir=work_dir, job_id=job_id,
        )
        return "libreoffice-native"


def _run_lo_compare(
    original: Path, revised: Path, output: Path,
    work_dir: Path, job_id: str,
) -> None:
    """Run lo_compare.py via LO's embedded Python."""
    profile_dir = work_dir / f"lo-compare-profile-{job_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        _LO_PYTHON, str(_LO_COMPARE_SCRIPT),
        str(original.absolute()), str(revised.absolute()),
        str(output.absolute()), str(profile_dir.absolute()),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_LO_TIMEOUT)

    if proc.returncode != 0:
        raise RuntimeError(
            f"LO compare failed (exit {proc.returncode}): "
            f"stdout={proc.stdout.strip()[:300]} "
            f"stderr={proc.stderr.strip()[:300]}"
        )
    if not output.exists():
        raise RuntimeError(
            f"LO compare exited 0 but output missing: {output}"
        )


# ═════════════════════════════════════════════════════════════════════════
# POST /compare — generate redline, stage to .tmp/redlines/
# ═════════════════════════════════════════════════════════════════════════

@router.post("/compare")
async def compare_documents(body: CompareRequest, request: Request):
    """Compare two documents using LibreOffice native engine.

    Stages the redlined .docx to .tmp/redlines/ (purged nightly).
    Returns a redline_id and download URL. User clicks "Save to DMS"
    to commit to the matter folder.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    user_id = _get_user_id(request)

    if body.doc_a_id == body.doc_b_id:
        raise HTTPException(400, detail="Cannot compare a document to itself")

    # Look up both documents
    async with AsyncSessionLocal() as session:
        result_a = await session.execute(
            sa_text("""
                SELECT id::text AS id, filename, storage_path, matter_id::text AS matter_id
                FROM documents
                WHERE id = CAST(:doc AS uuid) AND TRIM(tenant_id) = :tid
            """),
            {"doc": body.doc_a_id, "tid": tenant_id},
        )
        doc_a = result_a.mappings().first()

        result_b = await session.execute(
            sa_text("""
                SELECT id::text AS id, filename, storage_path, matter_id::text AS matter_id
                FROM documents
                WHERE id = CAST(:doc AS uuid) AND TRIM(tenant_id) = :tid
            """),
            {"doc": body.doc_b_id, "tid": tenant_id},
        )
        doc_b = result_b.mappings().first()

    if not doc_a or not doc_b:
        raise HTTPException(404, detail="Document(s) not found")

    # Resolve and validate storage paths
    path_a = _resolve_storage(doc_a["storage_path"])
    path_b = _resolve_storage(doc_b["storage_path"])
    if not path_a.exists():
        raise HTTPException(404, detail=f"Document A storage missing: {path_a.name}")
    if not path_b.exists():
        raise HTTPException(404, detail=f"Document B storage missing: {path_b.name}")

    # Validate extensions
    for tag, p in (("A", path_a), ("B", path_b)):
        ext = p.suffix.lower()
        if ext and ext not in _CONVERTIBLE_EXTENSIONS:
            raise HTTPException(
                400,
                detail=f"Document {tag} format {ext} not supported for comparison",
            )

    # Generate redline
    redline_id = str(uuid.uuid4())
    work_dir = Path(f"/tmp/dms-compare-{redline_id}")

    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        # Convert to .docx if needed
        docx_a = _convert_to_docx(path_a, work_dir, profile_id=f"{redline_id}-a")
        docx_b = _convert_to_docx(path_b, work_dir, profile_id=f"{redline_id}-b")

        # Run LO native compare
        staging_dir = _redline_staging_dir(tenant_id)
        output_path = staging_dir / f"{redline_id}.docx"

        engine_used = _run_compare(
            original=docx_a,
            revised=docx_b,
            output=output_path,
            work_dir=work_dir,
            job_id=redline_id,
        )

        out_bytes = output_path.read_bytes()
        checksum = hashlib.sha256(out_bytes).hexdigest()
        name_a = doc_a.get("filename") or body.doc_a_id
        name_b = doc_b.get("filename") or body.doc_b_id

        logger.info(
            "[dms-compare] redline=%s tenant=%s a=%s b=%s size=%d",
            redline_id, tenant_id, body.doc_a_id, body.doc_b_id, len(out_bytes),
        )

        return {
            "redline_id":   redline_id,
            "redline_name": f"REDLINE_{_stem(name_a)}_vs_{_stem(name_b)}.docx",
            "size_bytes":   len(out_bytes),
            "checksum":     checksum,
            "download_url": f"/api/v1/dms/documents/compare/{redline_id}/download",
            "save_url":     f"/api/v1/dms/documents/compare/{redline_id}/save",
            "engine":       engine_used,
            "matter_id":    doc_a.get("matter_id"),
            "doc_a_name":   name_a,
            "doc_b_name":   name_b,
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(504, detail="LibreOffice comparison timed out")
    except Exception as exc:
        logger.exception("[dms-compare] compare failed: %s", exc)
        raise HTTPException(500, detail=f"Comparison failed: {exc}")
    finally:
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════
# GET /compare/{redline_id}/download — stream staged redline
# ═════════════════════════════════════════════════════════════════════════

@router.get("/compare/{redline_id}/download")
async def download_redline(redline_id: str, request: Request):
    """Stream a staged redline .docx for preview."""
    from fastapi.responses import Response

    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    staging_dir = _redline_staging_dir(tenant_id)
    output_path = staging_dir / f"{redline_id}.docx"

    if not output_path.exists():
        raise HTTPException(
            410,
            detail="Redline has been purged. Re-run the comparison to regenerate.",
        )

    return Response(
        content=output_path.read_bytes(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": f'attachment; filename="redline-{redline_id}.docx"',
        },
    )


# ═════════════════════════════════════════════════════════════════════════
# POST /compare/{redline_id}/save — commit staged redline to DMS
# ═════════════════════════════════════════════════════════════════════════

@router.post("/compare/{redline_id}/save")
async def save_redline_to_dms(
    redline_id: str,
    body: SaveRedlineRequest,
    request: Request,
):
    """Copy a staged redline into the matter's DMS folder and create
    a documents row with proper provenance.

    If filename or folder_path are not provided, defaults to the
    matter's root folder with an auto-generated name.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    user_id = _get_user_id(request)

    # Find the staged file
    staging_dir = _redline_staging_dir(tenant_id)
    staged_path = staging_dir / f"{redline_id}.docx"

    if not staged_path.exists():
        raise HTTPException(
            410,
            detail="Redline has been purged. Re-run the comparison to regenerate.",
        )

    staged_bytes = staged_path.read_bytes()
    checksum = hashlib.sha256(staged_bytes).hexdigest()
    filename = body.filename or f"Redline-{redline_id[:8]}.docx"
    now = datetime.now(timezone.utc).isoformat()
    doc_id = str(uuid.uuid4())

    # Determine destination path. If folder_path provided, use it;
    # otherwise we need a matter_id to construct the path.
    if body.folder_path:
        dest_dir = _resolve_storage(body.folder_path)
    else:
        # Default: put it in the root of the tenant storage
        dest_dir = _storage_root() / tenant_id.strip()

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    # Copy from staging to DMS
    shutil.copy2(str(staged_path), str(dest_path))

    # Make storage_path relative for the documents table
    try:
        rel_path = str(dest_path.relative_to(_storage_root()))
    except ValueError:
        rel_path = str(dest_path)

    # Insert documents row
    async with AsyncSessionLocal() as session:
        await session.execute(
            sa_text("""
                INSERT INTO documents
                    (id, tenant_id, filename, storage_path, mime_type,
                     file_size, checksum, created_at, updated_at,
                     uploaded_by, document_type)
                VALUES
                    (CAST(:id AS uuid), :tid, :fn, :sp, :mt,
                     :sz, :cs, :now, :now,
                     :by, 'redline')
            """),
            {
                "id": doc_id, "tid": tenant_id, "fn": filename,
                "sp": rel_path,
                "mt": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "sz": len(staged_bytes), "cs": checksum,
                "now": now, "by": user_id,
            },
        )
        await session.commit()

    logger.info(
        "[dms-compare] saved redline=%s as doc=%s to %s",
        redline_id, doc_id, dest_path,
    )

    return {
        "document_id": doc_id,
        "filename":    filename,
        "storage_path": rel_path,
        "size_bytes":  len(staged_bytes),
        "checksum":    checksum,
    }


# ═════════════════════════════════════════════════════════════════════════
# Helpers

# ═════════════════════════════════════════════════════════════════════════
# POST /compare/batch — multi-pair redline, save directly to DMS
# ═════════════════════════════════════════════════════════════════════════

class BatchPair(BaseModel):
    doc_a_id: str
    doc_b_id: str


class BatchCompareRequest(BaseModel):
    matter_id: str
    pairs: list  # list of {doc_a_id, doc_b_id}
    target_folder: Optional[str] = None  # subfolder within matter, e.g. "99-Redlines"


@router.post("/compare/batch")
async def batch_compare_documents(body: BatchCompareRequest, request: Request):
    """Run multiple 2-on-1 comparisons sequentially.

    Each output is saved directly to the matter's DMS folder
    (not staged to .tmp/redlines/) as redline_{stemA}_v_{stemB}.docx.
    Returns results array with per-pair status.
    """
    from core.db.base import AsyncSessionLocal

    tenant_id = (getattr(request.state, "tenant_id", "") or "").strip()
    user_id = _get_user_id(request)

    if not body.pairs:
        raise HTTPException(400, detail="No pairs specified")
    if len(body.pairs) > 20:
        raise HTTPException(400, detail="Maximum 20 pairs per batch")

    # Resolve matter root for DMS save
    matter_root = None
    async with AsyncSessionLocal() as session:
        r = await session.execute(
            sa_text("""
                SELECT m.matter_name, c.client_name
                FROM matters m
                LEFT JOIN clients c ON m.client_id = c.id
                    AND TRIM(m.tenant_id) = TRIM(c.tenant_id)
                WHERE m.id = CAST(:mid AS uuid)
                  AND TRIM(m.tenant_id) = :tid
            """),
            {"mid": body.matter_id, "tid": tenant_id},
        )
        row = r.mappings().first()
        if row and row["client_name"] and row["matter_name"]:
            matter_root = _storage_root() / tenant_id.strip() / "matters" / row["client_name"] / row["matter_name"]

    if not matter_root or not matter_root.exists():
        raise HTTPException(404, detail="Matter folder not found")

    # Determine save directory
    if body.target_folder:
        save_dir = matter_root / body.target_folder
    else:
        save_dir = matter_root / "99-Redlines"
    save_dir.mkdir(parents=True, exist_ok=True)

    # Fetch all documents in one query
    doc_ids = set()
    for p in body.pairs:
        doc_ids.add(p["doc_a_id"] if isinstance(p, dict) else p.doc_a_id)
        doc_ids.add(p["doc_b_id"] if isinstance(p, dict) else p.doc_b_id)

    docs_map = {}
    async with AsyncSessionLocal() as session:
        for did in doc_ids:
            r = await session.execute(
                sa_text("""
                    SELECT id::text AS id, filename, storage_path,
                           matter_id::text AS matter_id
                    FROM documents
                    WHERE id = CAST(:doc AS uuid) AND TRIM(tenant_id) = :tid
                """),
                {"doc": did, "tid": tenant_id},
            )
            row = r.mappings().first()
            if row:
                docs_map[did] = dict(row)

    # Process each pair sequentially (LO soffice semaphore)
    results = []
    for idx, pair_raw in enumerate(body.pairs):
        pair = pair_raw if isinstance(pair_raw, dict) else pair_raw.dict()
        a_id = pair["doc_a_id"]
        b_id = pair["doc_b_id"]

        pair_result = {
            "index": idx,
            "doc_a_id": a_id,
            "doc_b_id": b_id,
            "status": "error",
        }

        if a_id == b_id:
            pair_result["error"] = "Cannot compare a document to itself"
            results.append(pair_result)
            continue

        doc_a = docs_map.get(a_id)
        doc_b = docs_map.get(b_id)
        if not doc_a or not doc_b:
            pair_result["error"] = "Document not found"
            results.append(pair_result)
            continue

        path_a = _resolve_storage(doc_a["storage_path"])
        path_b = _resolve_storage(doc_b["storage_path"])
        if not path_a.exists() or not path_b.exists():
            pair_result["error"] = "Storage file missing"
            results.append(pair_result)
            continue

        # Validate extensions
        skip = False
        for p in (path_a, path_b):
            ext = p.suffix.lower()
            if ext and ext not in _CONVERTIBLE_EXTENSIONS:
                pair_result["error"] = f"Format {ext} not supported"
                skip = True
                break
        if skip:
            results.append(pair_result)
            continue

        # Run comparison
        job_id = str(uuid.uuid4())
        work_dir = Path(f"/tmp/dms-compare-batch-{job_id}")

        try:
            work_dir.mkdir(parents=True, exist_ok=True)

            docx_a = _convert_to_docx(path_a, work_dir, profile_id=f"{job_id}-a")
            docx_b = _convert_to_docx(path_b, work_dir, profile_id=f"{job_id}-b")

            # Output filename: redline_{stemA}_v_{stemB}.docx
            stem_a = _stem(doc_a.get("filename") or a_id)
            stem_b = _stem(doc_b.get("filename") or b_id)
            out_filename = f"redline_{stem_a}_v_{stem_b}.docx"

            # Avoid collision
            out_path = save_dir / out_filename
            if out_path.exists():
                out_filename = f"redline_{stem_a}_v_{stem_b}_{job_id[:6]}.docx"
                out_path = save_dir / out_filename

            _run_compare(
                original=docx_a,
                revised=docx_b,
                output=out_path,
                work_dir=work_dir,
                job_id=job_id,
            )

            out_bytes = out_path.read_bytes()
            checksum = hashlib.sha256(out_bytes).hexdigest()

            # Insert documents row
            doc_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            try:
                rel_path = str(out_path.relative_to(_storage_root()))
            except ValueError:
                rel_path = str(out_path)

            async with AsyncSessionLocal() as session:
                await session.execute(
                    sa_text("""
                        INSERT INTO documents
                            (id, tenant_id, matter_id, filename, storage_path,
                             mime_type, file_size, checksum,
                             created_at, updated_at, uploaded_by,
                             document_type)
                        VALUES
                            (CAST(:id AS uuid), :tid, CAST(:mid AS uuid),
                             :fn, :sp, :mt, :sz, :cs,
                             :now, :now, :by, 'redline')
                    """),
                    {
                        "id": doc_id, "tid": tenant_id,
                        "mid": body.matter_id,
                        "fn": out_filename, "sp": rel_path,
                        "mt": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        "sz": len(out_bytes), "cs": checksum,
                        "now": now, "by": user_id,
                    },
                )
                await session.commit()

            pair_result.update({
                "status": "done",
                "document_id": doc_id,
                "filename": out_filename,
                "storage_path": rel_path,
                "size_bytes": len(out_bytes),
                "checksum": checksum,
            })

            logger.info(
                "[batch-compare] pair %d/%d done: %s (%d bytes)",
                idx + 1, len(body.pairs), out_filename, len(out_bytes),
            )

        except subprocess.TimeoutExpired:
            pair_result["error"] = "LibreOffice timed out"
        except Exception as exc:
            logger.exception("[batch-compare] pair %d failed: %s", idx, exc)
            pair_result["error"] = str(exc)[:200]
        finally:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)

        results.append(pair_result)

    succeeded = sum(1 for r in results if r["status"] == "done")
    failed = len(results) - succeeded

    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
        "target_folder": str(save_dir.relative_to(matter_root)),
        "results": results,
    }


# ═════════════════════════════════════════════════════════════════════════

def _get_user_id(request: Request) -> str:
    cu = getattr(request.state, "current_user", None)
    if cu is None:
        return "anonymous"
    return str(getattr(cu, "id", cu))


def _stem(filename: str) -> str:
    """Return filename without extension."""
    return Path(filename).stem if filename else "unknown"
