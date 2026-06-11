"""
M-DESK C1 — Compare worker (RQ job).

Function entrypoint:

    run_compare_job(*, job_id, tenant_id, doc_a_id, doc_b_id,
                     requesting_user_id) -> dict

Enqueued by modules.desktop.compare_router. Runs inside any of the 8
praesidium-proc-worker-* containers.

Pipeline:

  1. Look up both documents.{doc_a,doc_b} rows by id, scoped to tenant.
     Validate that storage_path resolves to an actual file.

  2. If non-.docx, convert via LibreOffice headless (per-job profile
     isolation for concurrency).

  3. Run LibreOffice native .uno:CompareDocuments via lo_compare.py.
     This produces a .docx with real OOXML tracked changes at
     character-level fidelity — handles formatting, tables,
     headers/footers, and crucially, resolves any existing tracked
     changes in the source documents before comparing.

  4. Save to /mnt/praesidium/{tenant_id}/.tmp/redlines/{job_id}.docx
     (staging area, purged nightly by purge_redline_cache.py).
     "Save to DMS" copies from staging to the matter folder.

  5. Return result dict (RQ stores in job.result).

# PATENT-S4-CANDIDATE:
# Server-side compare is the fallback fidelity path. Primary compare is
# C5 Word.Application.CompareDocuments client-side. The Series 4 claim
# is the END-TO-END chain: server-arbitrated checkout state guarantees
# that any compare run between two version rows reflects mutually
# exclusive edits, never racing parallel edits. This worker is a
# downstream consumer of that guarantee.

HISTORY:
  v1 (May 6, 2026)  — python-docx + difflib.SequenceMatcher paragraph walker.
                       Paragraph-level granularity only, lost formatting,
                       could not handle tracked changes in source documents.
  v2 (May 18, 2026) — Rewired to LibreOffice native .uno:CompareDocuments.
                       Character-level fidelity, handles tracked changes,
                       preserves formatting. Output staged to .tmp/redlines/.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import urllib.request
import urllib.error
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
# Filesystem helpers
# ═════════════════════════════════════════════════════════════════════════

_DOCX_EXTENSIONS = {".docx"}
_CONVERTIBLE_EXTENSIONS = {
    ".doc", ".docx", ".rtf", ".odt", ".pdf", ".txt", ".html", ".htm",
}

# Path to the LO UNO compare script (deployed alongside this module)
_LO_COMPARE_SCRIPT = Path("/app/modules/dms/services/lo_compare.py")
_LO_PYTHON = "/usr/lib/libreoffice/program/python"

# WmlComparer sidecar (primary redline engine; LibreOffice is the fallback)
_REDLINE_URL = os.environ.get(
    "PRAESIDIUM_REDLINE_URL", "http://praesidium-redline:8080/compare")
_REDLINE_TIMEOUT = 120  # seconds
_REDLINE_AUTHOR = "Praesidium"


def _storage_root() -> Path:
    return Path(os.environ.get("PRAESIDIUM_STORAGE_ROOT", "/mnt/praesidium"))


def _redline_staging_dir(tenant_id: str) -> Path:
    """Staging area for redline output. Purged nightly."""
    tid = (tenant_id or "").strip()
    return _storage_root() / tid / ".tmp" / "redlines"


def _resolve_storage(storage_path: str) -> Path:
    """Accept absolute or relative storage_path values for backward compat."""
    p = Path(storage_path)
    if not p.is_absolute():
        p = _storage_root() / p
    return p


# ═════════════════════════════════════════════════════════════════════════
# LibreOffice conversion (non-.docx inputs)
# ═════════════════════════════════════════════════════════════════════════

def _convert_to_docx(
    input_path: Path,
    work_dir: Path,
    profile_id: str,
    timeout_seconds: int = 90,
) -> Path:
    """Convert non-.docx to .docx via soffice headless.

    Per-job profile dir is REQUIRED to avoid concurrent invocations
    fighting over the default profile lock.
    """
    if input_path.suffix.lower() in _DOCX_EXTENSIONS:
        return input_path

    work_dir.mkdir(parents=True, exist_ok=True)
    profile_dir = work_dir / f"lo-profile-{profile_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        "soffice",
        "--headless",
        "--norestore",
        "--nofirststartwizard",
        f"-env:UserInstallation=file://{profile_dir.absolute()}",
        "--convert-to", "docx",
        "--outdir", str(work_dir.absolute()),
        str(input_path.absolute()),
    ]

    logger.info("[m-desk-compare] LibreOffice convert: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"LibreOffice conversion failed (exit {proc.returncode}): "
            f"stderr={proc.stderr.strip()[:400]}"
        )

    out = work_dir / f"{input_path.stem}.docx"
    if not out.exists():
        candidates = sorted(
            work_dir.glob("*.docx"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise RuntimeError(
                "LibreOffice exited 0 but no .docx produced in "
                f"{work_dir}; stderr={proc.stderr.strip()[:200]}"
            )
        out = candidates[0]
    return out


# ═════════════════════════════════════════════════════════════════════════
# LibreOffice native compare via UNO
# ═════════════════════════════════════════════════════════════════════════

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
    original_path: Path,
    revised_path: Path,
    output_path: Path,
    work_dir: Path,
    job_id: str,
) -> str:
    """Primary compare path: try the WmlComparer sidecar; on HTTP 422 or any
    transport error fall back to LibreOffice. Returns the engine label that
    actually produced the output."""
    try:
        _run_wml_compare(original_path, revised_path, output_path)
        return "wmlcomparer"
    except (_RedlineFallback, urllib.error.URLError, ConnectionError,
            TimeoutError, OSError) as exc:
        logger.info(
            "[m-desk-compare] WmlComparer unavailable/declined (%s); "
            "falling back to LibreOffice", exc,
        )
        _run_lo_compare(
            original_path=original_path,
            revised_path=revised_path,
            output_path=output_path,
            work_dir=work_dir,
            job_id=job_id,
        )
        return "libreoffice-native"


def _run_lo_compare(
    original_path: Path,
    revised_path: Path,
    output_path: Path,
    work_dir: Path,
    job_id: str,
    timeout_seconds: int = 120,
) -> None:
    """Run lo_compare.py via LibreOffice's embedded Python.

    This uses the UNO .uno:CompareDocuments dispatch which produces
    native OOXML tracked changes at character-level fidelity.
    It resolves existing tracked changes before comparing — the exact
    fix for the source-doc-with-tracked-changes problem.

    CRITICAL: LibreOffice 25+ requires -env:UserInstallation, not
    --user-profile. The lo_compare.py script handles this via
    os.environ["UserInstallation"].
    """
    profile_dir = work_dir / f"lo-compare-profile-{job_id}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # lo_compare.py expects: <original> <revised> <output> <profile_dir>
    cmd = [
        _LO_PYTHON,
        str(_LO_COMPARE_SCRIPT),
        str(original_path.absolute()),
        str(revised_path.absolute()),
        str(output_path.absolute()),
        str(profile_dir.absolute()),
    ]

    logger.info("[m-desk-compare] LO native compare: %s", " ".join(cmd))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"LO compare failed (exit {proc.returncode}): "
            f"stdout={proc.stdout.strip()[:300]} "
            f"stderr={proc.stderr.strip()[:300]}"
        )

    if not output_path.exists():
        raise RuntimeError(
            f"LO compare exited 0 but output missing: {output_path}; "
            f"stdout={proc.stdout.strip()[:200]}"
        )

    logger.info(
        "[m-desk-compare] LO compare produced %s (%d bytes)",
        output_path, output_path.stat().st_size,
    )


# ═════════════════════════════════════════════════════════════════════════
# DB lookup (sync — workers run synchronously)
# ═════════════════════════════════════════════════════════════════════════

def _load_document_sync(doc_id: str, tenant_id: str) -> Optional[dict]:
    """Sync DB lookup for document row."""
    from sqlalchemy import text as sa_text
    from core.db.base import get_session_factory

    SessionLocal = get_session_factory()
    with SessionLocal() as session:
        result = session.execute(
            sa_text("""
                SELECT
                  id::text       AS id,
                  tenant_id,
                  filename, original_filename, mime_type, file_size,
                  storage_path, document_type, doc_type, title
                FROM documents
                WHERE id = CAST(:doc AS uuid)
                  AND TRIM(tenant_id) = :tid
                LIMIT 1
            """),
            {"doc": doc_id, "tid": (tenant_id or "").strip()},
        )
        row = result.mappings().first()
        return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════
# RQ entrypoint
# ═════════════════════════════════════════════════════════════════════════

def run_compare_job(
    *,
    job_id: str,
    tenant_id: str,
    doc_a_id: str,
    doc_b_id: str,
    requesting_user_id: int,
    author_label: Optional[str] = None,
) -> dict[str, Any]:
    """RQ job entrypoint. Run a doc-vs-doc compare via LibreOffice native
    .uno:CompareDocuments and persist a redlined .docx to staging.

    Always returns a dict — never raises out of this function.
    """
    started = time.monotonic()
    work_dir = Path(f"/tmp/m-desk-compare-{job_id}")
    result: dict[str, Any] = {
        "status":             "failed",
        "job_id":             job_id,
        "tenant_id":          (tenant_id or "").strip(),
        "doc_a_id":           doc_a_id,
        "doc_b_id":           doc_b_id,
        "requesting_user_id": int(requesting_user_id),
        "output_path":        None,
        "checksum":           None,
        "size_bytes":         None,
        "diff_stats":         None,
        "duration_seconds":   None,
        "error":              None,
        "completed_at":       None,
    }

    try:
        # Load both rows
        doc_a = _load_document_sync(doc_a_id, tenant_id)
        doc_b = _load_document_sync(doc_b_id, tenant_id)
        if not doc_a:
            result["error"] = f"document A not found: {doc_a_id}"
            return result
        if not doc_b:
            result["error"] = f"document B not found: {doc_b_id}"
            return result

        # Validate paths
        if not doc_a.get("storage_path"):
            result["error"] = "document A has no storage_path"
            return result
        if not doc_b.get("storage_path"):
            result["error"] = "document B has no storage_path"
            return result

        path_a = _resolve_storage(doc_a["storage_path"])
        path_b = _resolve_storage(doc_b["storage_path"])
        if not path_a.exists():
            result["error"] = f"document A storage missing: {path_a}"
            return result
        if not path_b.exists():
            result["error"] = f"document B storage missing: {path_b}"
            return result

        # Reject unsupported formats
        for tag, p in (("A", path_a), ("B", path_b)):
            ext = p.suffix.lower()
            if ext and ext not in _CONVERTIBLE_EXTENSIONS:
                result["error"] = (
                    f"document {tag} extension {ext!r} not supported "
                    f"(allowed: {sorted(_CONVERTIBLE_EXTENSIONS)})"
                )
                return result

        # Convert each to .docx if needed
        work_dir.mkdir(parents=True, exist_ok=True)
        docx_a = _convert_to_docx(path_a, work_dir, profile_id=f"{job_id}-a")
        docx_b = _convert_to_docx(path_b, work_dir, profile_id=f"{job_id}-b")

        # Build redline output via LO native compare
        staging_dir = _redline_staging_dir(tenant_id)
        staging_dir.mkdir(parents=True, exist_ok=True)
        output_path = staging_dir / f"{job_id}.docx"

        engine_used = _run_compare(
            original_path=docx_a,
            revised_path=docx_b,
            output_path=output_path,
            work_dir=work_dir,
            job_id=job_id,
        )

        # Hash + size
        out_bytes = output_path.read_bytes()
        checksum = hashlib.sha256(out_bytes).hexdigest()

        result.update({
            "status":           "completed",
            "output_path":      str(output_path),
            "checksum":         checksum,
            "size_bytes":       len(out_bytes),
            "diff_stats":       {"engine": engine_used},
            "duration_seconds": round(time.monotonic() - started, 3),
            "completed_at":     datetime.now(timezone.utc).isoformat(),
        })
        logger.info(
            "[m-desk-compare] job=%s done tenant=%s a=%s b=%s "
            "duration=%.2fs out=%s size=%d",
            job_id, result["tenant_id"], doc_a_id, doc_b_id,
            result["duration_seconds"], output_path, len(out_bytes),
        )
        return result

    except subprocess.TimeoutExpired as exc:
        logger.exception("[m-desk-compare] job=%s LibreOffice timeout", job_id)
        result["error"] = f"LibreOffice timeout: {exc}"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result
    except Exception as exc:
        logger.exception("[m-desk-compare] job=%s failed", job_id)
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        return result
    finally:
        try:
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            logger.warning("[m-desk-compare] work_dir cleanup failed: %s", work_dir)
